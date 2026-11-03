#!/usr/bin/env python3
"""Cheap, deterministic wake-gate for the proactivity layer.

Runs every few minutes with **no LLM**. Hermes' cron `monitor` hashes the exact
bytes printed here and wakes the agent only when they differ from last time, so
every state prints one constant line:

quiet                nothing to do
capture              ≥1 new owner message and the last capture ≥ 2h ago
review ids 12,15     due followups that may be sent now (ids ascending)
held night|cooling|muted   something is due but the send gate holds all of it

Whether a DM may go out is decided by ``followups.send_gate`` — the same call
digest and ``followups.py send`` make. Nothing here re-derives night, cooldown
or mute.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

CAPTURE_EVERY_S = 2 * 3600   # owner sends few messages; 8 was unreachable
MIN_NEW_MSGS = 1

QUIET = "quiet"


def home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))


# Schema lives in followups.py only; sharing its connect() keeps every table in
# place no matter which entry point touches the DB first.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from followups import NOISE, _get_kv as get_kv, chat_types, connect, send_gate  # noqa: E402


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
            "AND m.content NOT LIKE '[INTERNAL%' AND m.content NOT LIKE '[ASYNC%' "
            # Whatever digest drops as noise must not count here either, or it wakes
            # a capture that digests nothing and never moves the cursor past it.
            + "AND ltrim(m.content) NOT LIKE ? " * len(NOISE),
            (since, *types, owner, *(n + "%" for n in NOISE)),
        ).fetchone()[0]
    finally:
        c.close()


def wake_line(conn, now: float | None = None) -> str:
    now = time.time() if now is None else now
    # Two clocks: the watermark is the last message already digested (apply sets it
    # to that message's time); kv.last_capture is when the last capture ran. Cadence
    # follows last_capture; DBs from before apply wrote it fall back to the watermark.
    row = conn.execute("SELECT last_ts FROM watermark WHERE surface='messages'").fetchone()
    watermark = float(row["last_ts"]) if row else 0.0
    last_capture = get_kv(conn, "last_capture", watermark)

    gate = send_gate(conn, now)
    if gate["may_send"]:
        return "review ids " + ",".join(str(i) for i in sorted(gate["sendable"]))
    # capture: cheap bookkeeping, never messages the user
    if (now - last_capture) >= CAPTURE_EVERY_S and count_new_human(conn, watermark) >= MIN_NEW_MSGS:
        return "capture"
    if gate["hold"]:
        return f"held {gate['hold']}"
    return QUIET


def main() -> int:
    if not owner_id():
        # No owner configured: wake once so the agent can say so, then stay quiet
        # (the payload is constant, so the monitor gate suppresses repeats).
        print('{"mode": "setup-needed"}')
        return 0
    print(wake_line(connect()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
