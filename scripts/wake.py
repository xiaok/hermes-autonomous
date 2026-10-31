#!/usr/bin/env python3
"""Cheap, deterministic wake-gate for the proactivity layer.

Runs every few minutes with **no LLM**. Prints a small JSON blob only when there
is a reason to wake the agent; otherwise prints the constant ``quiet`` so Hermes'
cron `monitor` gate skips the agent run entirely.

Conditions (v1)
---------------
capture  last capture ≥ 2h ago and ≥1 new owner message      → update followups, never message
due      a followup's revisit_at has arrived                 → review it, maybe message
hold     something is due but a message went out < 90 min ago → stay quiet this tick
night    23:00–08:00                                          → capture only, never message

Output is timestamp-free on purpose: identical output must mean "nothing new".
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

def local_tz():
    name = os.environ.get("AUTONOMY_TZ") or ""
    if not name:
        try:
            p = home() / "plugin-data" / "hermes-autonomous" / "settings.json"
            name = json.loads(p.read_text(encoding="utf-8")).get("timezone") or ""
        except Exception:
            name = ""
    if name:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(str(name))
        except Exception:
            pass
    return datetime.now().astimezone().tzinfo or timezone.utc
CAPTURE_EVERY_S = 2 * 3600   # owner sends few messages; 8 was unreachable
MIN_NEW_MSGS = 1
SEND_COOLDOWN_S = 90 * 60

QUIET = "quiet"


def home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))


# Schema lives in followups.py only; sharing its connect() keeps every table in
# place no matter which entry point touches the DB first.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from followups import chat_types, connect  # noqa: E402


def get_kv(conn, k: str, default: float = 0.0) -> float:
    r = conn.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    try:
        return float(r["v"]) if r else default
    except (TypeError, ValueError):
        return default


def set_kv(conn, k: str, v: float) -> None:
    conn.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                 (k, str(v)))


MUTE_WINDOW_S = 30 * 86400
MUTE_IGNORED_AFTER = 3


def muted_whys(conn, now: float | None = None) -> dict[str, str]:
    """Categories (``why``) the owner has pushed back on in the last 30 days.

    One ``annoyed`` or three ``ignored`` on sent followups mutes that category:
    its items stay in the table but no longer wake the agent into review.
    """
    now = time.time() if now is None else now
    rows = conn.execute(
        "SELECT why, outcome, COUNT(*) AS n FROM followups "
        "WHERE sent_at IS NOT NULL AND sent_at>=? AND why IS NOT NULL "
        "AND outcome IN ('annoyed','ignored') GROUP BY why, outcome",
        (now - MUTE_WINDOW_S,),
    ).fetchall()
    muted: dict[str, str] = {}
    for r in rows:
        if r["outcome"] == "annoyed":
            muted[r["why"]] = "annoyed"
        elif r["n"] >= MUTE_IGNORED_AFTER:
            muted.setdefault(r["why"], f"ignored*{r['n']}")
    return muted


def owner_id() -> str:
    """Owner id comes from settings.json / env, never from the repo."""
    val = os.environ.get("AUTONOMY_OWNER_ID", "")
    if val:
        return val
    p = home() / "plugin-data" / "hermes-autonomous" / "settings.json"
    try:
        return str(json.loads(p.read_text(encoding="utf-8")).get("owner_id") or "")
    except Exception:
        return ""


def count_new_human(conn, since: float) -> int:
    """User turns in the owner's sessions — same chat types the digest reads."""
    state = home() / "state.db"
    if not state.exists():
        return 0
    owner = owner_id()
    if not owner:
        return 0
    types = chat_types()
    c = sqlite3.connect(f"file:{state}?mode=ro", uri=True, timeout=30)
    try:
        return c.execute(
            "SELECT COUNT(*) FROM messages m JOIN sessions s ON s.id=m.session_id "
            f"WHERE m.timestamp>? AND m.role='user' AND s.chat_type IN ({','.join('?' * len(types))}) "
            "AND s.user_id=? AND m.content NOT LIKE '[Cron delivery:%' "
            "AND m.content NOT LIKE '[INTERNAL%' AND m.content NOT LIKE '[ASYNC%'",
            (since, *types, owner),
        ).fetchone()[0]
    finally:
        c.close()


def main() -> int:
    conn = connect()
    now = time.time()
    hour = datetime.now(tz=local_tz()).hour
    night = hour < 8 or hour >= 23

    # Two clocks: the watermark is the last message already digested (apply sets it
    # to that message's time); kv.last_capture is when the last capture ran. Cadence
    # follows last_capture; DBs from before apply wrote it fall back to the watermark.
    row = conn.execute("SELECT last_ts FROM watermark WHERE surface='messages'").fetchone()
    watermark = float(row["last_ts"]) if row else 0.0
    last_capture = get_kv(conn, "last_capture", watermark)

    if not owner_id():
        # No owner configured: wake once so the agent can say so, then stay quiet
        # (the payload is constant, so the monitor gate suppresses repeats).
        print('{"mode": "setup-needed"}')
        return 0

    feedback = []

    # 1) capture: cheap bookkeeping, never messages the user
    need_capture = (now - last_capture) >= CAPTURE_EVERY_S
    if need_capture and count_new_human(conn, watermark) >= MIN_NEW_MSGS:
        feedback.append("capture")

    # 2) due followups, minus categories the owner has muted by feedback
    muted = muted_whys(conn, now)
    due = conn.execute(
        "SELECT id, why FROM followups WHERE status='active' AND COALESCE(revisit_at,0)<=? "
        "AND (sent_at IS NULL OR sent_at < revisit_at) "
        "ORDER BY revisit_at", (now,)
    ).fetchall()
    due_ids = [r["id"] for r in due if r["why"] not in muted][:10]
    if due_ids:
        last_send = get_kv(conn, "last_send", 0.0)
        cooling = (now - last_send) < SEND_COOLDOWN_S
        feedback.append("due-hold" if (cooling or night) else "due")
        if due_ids and not (cooling or night):
            print(json.dumps({"mode": "review", "ids": due_ids, "night": False}))
            return 0

    # 3) capture-only wake
    if feedback and "capture" in feedback:
        print(json.dumps({"mode": "capture", "night": night}))
        return 0

    print(QUIET)
    return 0


if __name__ == "__main__":
    sys.exit(main())
