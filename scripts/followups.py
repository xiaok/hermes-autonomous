#!/usr/bin/env python3
"""Follow-up store + digest for the proactivity layer.

A "followup" is not an unfinished task — it is a moment worth coming back to.
It can be purely emotional (ask how the sore throat is), purely practical
(the trip is tomorrow, it will rain), or speculative (the NFT minted last
night — check the floor).

Subcommands
-----------
digest [--hours N]     JSON for the review agent: new messages + active followups
add ...                create a followup
list [--all]           show followups
due [--at ISO]         only those whose revisit_at has arrived
checked ID [--snooze ISO]  mark "already looked at it"; optional next revisit
close ID [--note S]    retire it for good
watermark [--set ISO]  read or set the message cursor
apply [--file F]       write back a whole review run (JSON, stdin by default) in one transaction
why                    muted categories + decisions of the last 5 runs (read-only)
log                    one line per recent review run (read-only)
doctor [--fix] [--json]  check settings + this plugin's cron job; --fix edits the job in place
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

def local_tz():
    """Timezone: explicit setting > AUTONOMY_TZ > system local. Never hardcode a zone."""
    name = os.environ.get("AUTONOMY_TZ") or ""
    try:
        name = name or (load_settings() or {}).get("timezone") or ""
    except Exception:
        name = name or ""
    if name:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(str(name))
        except Exception:
            pass
    return datetime.now().astimezone().tzinfo or timezone.utc


MAX_MSG_CHARS = 1200
MAX_MESSAGES = 250
MAX_SENT_TEXT = 1200        # stored
MAX_SENT_TEXT_DIGEST = 300  # shown to the review agent

# Who owns this install lives in settings.json (or AUTONOMY_OWNER_ID), never in
# the repo: only the owner's own sessions are read (DM only unless include_groups).
SETTINGS_KEYS = ("owner_id", "dm_target")


def settings_path() -> Path:
    return db_path().parent / "settings.json"


def load_settings() -> dict:
    p = settings_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def setting(key: str, env: str) -> str:
    val = os.environ.get(env) or (load_settings() or {}).get(key) or ""
    if not val:
        raise SystemExit(
            f"not configured: {key}. Run: followups.py setup --owner-id <telegram_user_id> "
            f"--dm-target telegram:<chat_id>  (or set {env})")
    return str(val)


def _is_night() -> bool:
    hour = datetime.now(tz=local_tz()).hour
    return hour < 8 or hour >= 23


def owner_id() -> str:
    return setting("owner_id", "AUTONOMY_OWNER_ID")


def chat_types() -> tuple[str, ...]:
    """DM sessions only, unless setup --include-groups: in a group, other members'
    messages land in the owner's session too, so reading groups is opt-in. Discord
    (and Slack) threads are group-like — several members can post in one thread, and
    Hermes records them as chat_type='thread' — so they follow the same switch
    instead of adding a third flag that would only be forgotten. Same for 'forum'
    (Telegram forum-group topics) and 'channel': every non-DM chat_type Hermes
    records is multi-member. (A Telegram private-chat topic stays 'dm'.)"""
    if (load_settings() or {}).get("include_groups") is True:
        return ("dm", "group", "thread", "forum", "channel")
    return ("dm",)


def dev_log() -> bool:
    """Developer log as the run's final reply: opt-in (setup --dev-log). The cron job
    delivers that reply to a chat, so on by default it spams real users every 15 min."""
    return (load_settings() or {}).get("dev_log") is True


def dm_target() -> str:
    return setting("dm_target", "AUTONOMY_DM_TARGET")


NOISE = (
    "[CONTEXT COMPACTION",
    "[INTERNAL NOTIFICATION",
    "[ASYNC DELEGATION",
    "[OUT-OF-BAND USER MESSAGE",
)


def db_path() -> Path:
    home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    p = Path(home) / "plugin-data" / "hermes-autonomous"
    p.mkdir(parents=True, exist_ok=True)
    return p / "followups.db"


def connect() -> sqlite3.Connection:
    path = db_path()
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    # The board holds quotes from private chats: owner-only, new or pre-existing.
    if path.exists() and (path.stat().st_mode & 0o777) != 0o600:
        os.chmod(path, 0o600)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS followups (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          key TEXT UNIQUE,
          kind TEXT, why TEXT,
          subject TEXT, quote TEXT,
          hint TEXT,
          revisit_at REAL,
          status TEXT DEFAULT 'active',
          checks INTEGER DEFAULT 0,
          last_checked REAL,
          note TEXT,
          created_at REAL
        );
        CREATE TABLE IF NOT EXISTS watermark (surface TEXT PRIMARY KEY, last_ts REAL);
        CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS log (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          run_at REAL, kind TEXT, payload TEXT
        );
        """
    )
    # Migration: older DBs lack the delivery/outcome columns. `hermes send` output is
    # not written back into state.db, so this table is the only receipt we have.
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(followups)")}
    # sends / sent_text: how many times it actually went out and what was said last,
    # so the review can tell "sent once already" from "never sent". `checks` counts
    # silent looks and sends alike and cannot answer that.
    for name, decl in (("sent_at", "REAL"), ("sent_msg_id", "TEXT"), ("outcome", "TEXT"),
                       ("sends", "INTEGER NOT NULL DEFAULT 0"),
                       ("sent_text", "TEXT NOT NULL DEFAULT ''")):
        if name not in cols:
            conn.execute(f"ALTER TABLE followups ADD COLUMN {name} {decl}")
    return conn


def _iso(ts: float | None) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, local_tz()).isoformat(timespec="minutes")


def _parse(when: str) -> float:
    if not when:
        raise SystemExit("empty time")
    if when.lower() in ("now",):
        return time.time()
    if when.startswith("+") and when.endswith("h"):
        return time.time() + float(when[1:-1]) * 3600
    if when.startswith("+") and when.endswith("d"):
        return time.time() + float(when[1:-1]) * 86400
    d = datetime.fromisoformat(when)
    if d.tzinfo is None:
        d = d.replace(tzinfo=local_tz())
    return d.timestamp()


def _get_wm(conn) -> float:
    r = conn.execute("SELECT last_ts FROM watermark WHERE surface='messages'").fetchone()
    return r["last_ts"] if r else time.time() - 12 * 3600


def _set_wm(conn, ts: float) -> None:
    conn.execute(
        "INSERT INTO watermark(surface,last_ts) VALUES('messages',?) "
        "ON CONFLICT(surface) DO UPDATE SET last_ts=excluded.last_ts",
        (ts,),
    )


def _messages_since(since: float) -> tuple[list[dict], float | None]:
    """User and assistant turns after ``since`` in the owner's sessions, plus the
    raw timestamp of the last one returned (None if none) — the exact cursor the
    next apply should store."""
    state = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")) / "state.db"
    if not state.exists():
        return [], None
    types = chat_types()
    c = sqlite3.connect(f"file:{state}?mode=ro", uri=True, timeout=30)
    c.row_factory = sqlite3.Row
    rows = c.execute(
        f"""
        SELECT m.timestamp, m.role, m.content, s.source, s.chat_type, s.title
        FROM messages m JOIN sessions s ON s.id = m.session_id
        WHERE m.timestamp > ? AND m.role IN ('user','assistant')
          AND m.content IS NOT NULL AND length(trim(m.content)) > 0
          -- Sessions owned by the owner, DM only by default. A group session's
          -- 'user' turns can be other members: only read them with include_groups.
          AND s.chat_type IN ({','.join('?' * len(types))}) AND s.user_id = ?
          AND m.content NOT LIKE '[Cron delivery:%'
        ORDER BY m.timestamp ASC
        """,
        (since, *types, owner_id()),
    ).fetchall()
    out = []
    last_ts = None
    for r in rows:
        text = (r["content"] or "").strip()
        if any(text.startswith(n) for n in NOISE):
            continue
        last_ts = r["timestamp"]
        out.append(
            {
                "t": _iso(r["timestamp"]),
                "role": r["role"],
                "src": f"{r['source']}/{r['chat_type'] or '-'}",
                "text": text[:MAX_MSG_CHARS],
            }
        )
    # A busy day (hundreds of turns) must not blow up the prompt: keep the tail,
    # which is where the live threads are.
    if len(out) > MAX_MESSAGES:
        out = out[-MAX_MESSAGES:]
    return out, last_ts


MUTE_WINDOW_S = 30 * 86400
MUTE_IGNORED_AFTER = 3


def muted_whys(conn, now: float | None = None) -> dict[str, str]:
    """Categories (``why``) the owner has pushed back on in the last 30 days.

    One ``annoyed`` or three ``ignored`` on sent followups mutes that category:
    its items stay in the table but no longer wake the agent into review.
    Lives here, not in wake.py: cron runs this file via runpy with no sys.path
    entry for scripts/, so it must never import its siblings.
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


def cmd_digest(args) -> int:
    conn = connect()
    wm = since = _get_wm(conn)
    if args.hours:
        since = max(since, time.time() - args.hours * 3600)
    msgs, last_ts = _messages_since(since)
    now = time.time()
    active = [
        {
            "id": r["id"], "key": r["key"], "kind": r["kind"], "why": r["why"],
            "subject": r["subject"], "quote": r["quote"], "hint": r["hint"],
            "revisit_at": _iso(r["revisit_at"]),
            "due": (r["revisit_at"] or 0) <= now and not (r["sent_at"] and r["sent_at"] >= (r["revisit_at"] or 0)),
            "checks": r["checks"], "last_checked": _iso(r["last_checked"]),
            "status": r["status"],
            "sent_at": _iso(r["sent_at"]), "sent_msg_id": r["sent_msg_id"],
            "sends": r["sends"], "sent_text": (r["sent_text"] or "")[:MAX_SENT_TEXT_DIGEST],
            "outcome": r["outcome"],
        }
        for r in conn.execute("SELECT * FROM followups WHERE status='active' ORDER BY revisit_at").fetchall()
    ]
    payload = {
        "now": _iso(now),
        "watermark": _iso(since),
        # Raw float, not _iso(): minute rounding would drop or replay messages.
        # It is the last message *shown here*, so anything arriving between this
        # digest and the apply is still ahead of the cursor next run.
        "next_watermark": last_ts if last_ts is not None else wm,
        "new_messages": len(msgs),
        "messages": msgs,
        "followups": active,
        "due_count": sum(1 for f in active if f["due"]),
        "mode": "review" if any(f["due"] for f in active) else "capture",
        "night": _is_night(),
        "muted": muted_whys(conn, now),
        "dev_log": dev_log(),
        "instructions": "See prompts/review.md: produce one apply JSON (watermark = next_watermark); "
                        "to DM the owner, hermes send first, then write back with one followups.py apply.",
    }
    print(json.dumps(payload, ensure_ascii=False))
    return 0


def cmd_add(args) -> int:
    conn = connect()
    cur = conn.execute(
        "INSERT INTO followups(key,kind,why,subject,quote,hint,revisit_at,created_at) "
        "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
        "why=excluded.why,subject=excluded.subject,quote=excluded.quote,"
        "hint=excluded.hint,revisit_at=excluded.revisit_at "
        "RETURNING id",
        (args.key, args.kind, args.why, args.subject, args.quote, args.hint or "",
         _parse(args.revisit_at), time.time()),
    )
    print(json.dumps({"ok": True, "id": cur.fetchone()[0]}))
    return 0


def cmd_list(args) -> int:
    conn = connect()
    q = "SELECT * FROM followups"
    if not args.all:
        q += " WHERE status='active'"
    q += " ORDER BY revisit_at"
    rows = conn.execute(q).fetchall()
    for r in rows:
        print(f"#{r['id']:<3} [{r['status']:<6}] {_iso(r['revisit_at'])[:16]:<16} "
              f"{r['why']:<9} {r['subject']}  ({r['kind']}) checks={r['checks']}")
    if not rows:
        print("(none)")
    return 0


def cmd_due(args) -> int:
    conn = connect()
    ts = _parse(args.at) if args.at else time.time()
    rows = conn.execute(
        "SELECT * FROM followups WHERE status='active' AND COALESCE(revisit_at,0)<=? "
        "ORDER BY revisit_at", (ts,)
    ).fetchall()
    print(json.dumps([dict(r) | {"revisit_at": _iso(r["revisit_at"])} for r in rows], ensure_ascii=False))
    return 0


def cmd_checked(args) -> int:
    """Mark as looked-at. Always moves revisit_at forward: a checked item that stays
    due would wake the gate every 15 minutes forever."""
    conn = connect()
    r = conn.execute("SELECT checks, revisit_at FROM followups WHERE id=?", (args.id,)).fetchone()
    if r is None:
        print(json.dumps({"ok": False, "error": f"no followup {args.id}"}))
        raise SystemExit(2)
    backoff = min(86400 * (2 ** min(r["checks"], 3)), 7 * 86400)
    nxt = _parse(args.snooze) if args.snooze else time.time() + backoff
    conn.execute(
        "UPDATE followups SET checks=checks+1, last_checked=?, revisit_at=? WHERE id=?",
        (time.time(), nxt, args.id),
    )
    print(json.dumps({"ok": True, "id": args.id, "next": _iso(nxt)}))
    return 0


def _set_kv(conn, k: str, v: float) -> None:
    conn.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                 (k, str(v)))


def cmd_setup(args) -> int:
    """Write this install's owner id / DM target. Nothing identifying ships in the repo."""
    cur = load_settings()
    if args.owner_id:
        cur["owner_id"] = str(args.owner_id)
    if args.dm_target:
        cur["dm_target"] = str(args.dm_target)
    if args.timezone:
        cur["timezone"] = str(args.timezone)
    if args.include_groups is not None:
        cur["include_groups"] = args.include_groups
    if args.dev_log is not None:
        cur["dev_log"] = args.dev_log
    cur.setdefault("include_groups", False)
    cur.setdefault("dev_log", False)
    settings_path().write_text(json.dumps(cur, indent=2), encoding="utf-8")
    saved = settings_path()
    os.chmod(saved, 0o600)
    connect().close()  # create the DB + schema so digest/wake work right after setup
    print(json.dumps({"ok": True, "path": str(saved), "settings": cur}, ensure_ascii=False))
    # Read-only health check. stderr keeps stdout a single JSON document; a WARN
    # here is advice, never a setup failure.
    try:
        bad = [c for c in _doctor(fix=False)["checks"] if c["status"] != "OK"]
    except Exception as e:  # noqa: BLE001
        bad = [{"status": "WARN", "name": "doctor", "detail": f"{type(e).__name__}: {e}"}]
    if bad:
        print("doctor:", file=sys.stderr)
        for c in bad[:5]:
            print(f"  [{c['status']}] {c['name']}: {c['detail']}", file=sys.stderr)
        print("  details: followups.py doctor   (fix the cron job: followups.py doctor --fix)",
              file=sys.stderr)
    return 0


def cmd_dm_target(_args) -> int:
    print(dm_target())
    return 0


def cmd_mark(args) -> int:
    """Advance the wake gate: 'capture' after extraction, 'sent' after a message."""
    conn = connect()
    now = time.time()
    keys = []
    if args.capture:
        _set_kv(conn, "last_capture", now)
        _set_wm(conn, now)          # one cursor, not two
        keys.append("last_capture")
    if args.sent:
        _set_kv(conn, "last_send", now)
        keys.append("last_send")
    if not keys:
        raise SystemExit("mark needs --capture and/or --sent")
    print(json.dumps({"ok": True, "marked": keys}))
    return 0


def cmd_sent(args) -> int:
    """Record a delivered message: the only receipt that survives `hermes send`.

    A send with no message_id is a failure, not a success — recording it anyway
    would mark an undelivered followup as done and start the cooldown for nothing.
    """
    if not (args.msg_id or "").strip():
        print(json.dumps({"ok": False, "error": "msg-id required: the send failed, do not mark it sent"}))
        raise SystemExit(2)
    conn = connect()
    now = time.time()
    cur = conn.execute(
        "UPDATE followups SET sent_at=?, sent_msg_id=?, checks=checks+1, "
        "sends=sends+1, sent_text=?, revisit_at=? WHERE id=?",
        (now, args.msg_id.strip(), (args.text or "")[:MAX_SENT_TEXT], now + 86400, args.id),
    )
    if cur.rowcount == 0:
        print(json.dumps({"ok": False, "error": f"no followup {args.id}"}))
        raise SystemExit(2)
    _set_kv(conn, "last_send", now)
    print(json.dumps({"ok": True, "id": args.id, "msg_id": args.msg_id or ""}))
    return 0


def cmd_outcome(args) -> int:
    conn = connect()
    if args.value not in ("accepted", "ignored", "annoyed", "useless", "unknown"):
        raise SystemExit("outcome must be accepted|ignored|annoyed|useless|unknown")
    conn.execute("UPDATE followups SET outcome=? WHERE id=?", (args.value, args.id))
    print(json.dumps({"ok": True, "id": args.id, "outcome": args.value}))
    return 0


def cmd_close(args) -> int:
    conn = connect()
    conn.execute(
        "UPDATE followups SET status='closed', note=COALESCE(?,note) WHERE id=?", (args.note, args.id)
    )
    print(json.dumps({"ok": True}))
    return 0


class ApplyError(Exception):
    """A bad apply document. Raised inside the transaction, so it rolls back."""


APPLY_KEYS = ("new_followups", "decisions", "sent", "watermark", "outcomes", "close")
FOLLOWUP_FIELDS = ("key", "why", "subject", "quote", "revisit_at", "hint", "kind")
FOLLOWUP_REQUIRED = ("key", "why", "subject", "kind", "revisit_at")  # same as `add`
ACTIONS = ("silent", "act_report", "ask")
OUTCOMES = ("accepted", "ignored", "annoyed", "useless", "unknown")


def _list(doc: dict, name: str) -> list:
    v = doc.get(name)
    if v is None:
        return []
    if not isinstance(v, list):
        raise ApplyError(f"{name}: must be a list")
    return v


def _obj(v, where: str, allowed: tuple) -> dict:
    if not isinstance(v, dict):
        raise ApplyError(f"{where}: must be an object")
    extra = sorted(set(v) - set(allowed))
    if extra:
        raise ApplyError(f"{where}: unknown field(s) {extra}")
    return v


def _id(conn, v, where: str) -> int:
    if not isinstance(v, int) or isinstance(v, bool):
        raise ApplyError(f"{where}: id must be an integer, got {v!r}")
    if conn.execute("SELECT 1 FROM followups WHERE id=?", (v,)).fetchone() is None:
        raise ApplyError(f"{where}: no followup {v}")
    return v


def _when(v, where: str) -> float:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    if not isinstance(v, str) or not v.strip():
        raise ApplyError(f"{where}: required (ISO time, 'now', '+12h', '+2d')")
    try:
        return _parse(v.strip())
    except (ValueError, SystemExit):
        raise ApplyError(f"{where}: bad time {v!r}") from None


def _apply(conn, doc: dict) -> dict:
    """Every write of one review run. Caller owns BEGIN/COMMIT/ROLLBACK."""
    now = time.time()
    res: dict = {"added": [], "outcomes": [], "closed": [], "checked": [], "sent": []}

    for i, f in enumerate(_list(doc, "new_followups")):
        where = f"new_followups[{i}]"
        _obj(f, where, FOLLOWUP_FIELDS)
        for k in FOLLOWUP_REQUIRED:
            if k == "revisit_at":
                continue
            if not isinstance(f.get(k), str) or not f[k].strip():
                raise ApplyError(f"{where}: missing {k}")
        # Same upsert as `add`: sent_at / checks / outcome / status survive.
        row = conn.execute(
            "INSERT INTO followups(key,kind,why,subject,quote,hint,revisit_at,created_at) "
            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
            "why=excluded.why,subject=excluded.subject,quote=excluded.quote,"
            "hint=excluded.hint,revisit_at=excluded.revisit_at "
            "RETURNING id",
            (f["key"].strip(), f["kind"], f["why"], f["subject"], f.get("quote") or "",
             f.get("hint") or "", _when(f.get("revisit_at"), f"{where}.revisit_at"), now),
        ).fetchone()
        res["added"].append(row[0])

    for i, o in enumerate(_list(doc, "outcomes")):
        where = f"outcomes[{i}]"
        _obj(o, where, ("id", "value", "close", "note"))
        fid = _id(conn, o.get("id"), where)
        if o.get("value") not in OUTCOMES:
            raise ApplyError(f"{where}: value must be {'|'.join(OUTCOMES)}")
        conn.execute("UPDATE followups SET outcome=? WHERE id=?", (o["value"], fid))
        res["outcomes"].append(fid)
        if o.get("close"):
            conn.execute("UPDATE followups SET status='closed', note=COALESCE(?,note) WHERE id=?",
                         (o.get("note"), fid))
            res["closed"].append(fid)

    for i, c in enumerate(_list(doc, "close")):
        where = f"close[{i}]"
        _obj(c, where, ("id", "note"))
        fid = _id(conn, c.get("id"), where)
        conn.execute("UPDATE followups SET status='closed', note=COALESCE(?,note) WHERE id=?",
                     (c.get("note"), fid))
        res["closed"].append(fid)

    sent = doc.get("sent")
    sent_ids: list[int] = []
    if sent is not None:
        _obj(sent, "sent", ("id", "ids", "msg_id", "text"))
        if ("id" in sent) == ("ids" in sent):
            raise ApplyError("sent: give exactly one of id / ids")
        raw_ids = sent["ids"] if "ids" in sent else [sent["id"]]
        if not isinstance(raw_ids, list) or not raw_ids:
            raise ApplyError("sent.ids: must be a non-empty list")
        sent_ids = [_id(conn, v, "sent") for v in raw_ids]
        # A send with no message_id is a failure: never record it as delivered.
        msg_id = str(sent.get("msg_id") or "").strip()
        if not msg_id:
            raise ApplyError("sent.msg_id: required — the send failed, do not mark it sent")
        sent_text = sent.get("text")
        if sent_text is None:
            sent_text = ""
        if not isinstance(sent_text, str):
            raise ApplyError("sent.text: must be a string (the message as sent)")
        sent_text = sent_text[:MAX_SENT_TEXT]

    seen: set[int] = set()
    for i, d in enumerate(_list(doc, "decisions")):
        where = f"decisions[{i}]"
        _obj(d, where, ("id", "action", "reason", "snooze", "score"))
        # score is the decider's rubric: not interpreted, only kept in the log payload.
        if "score" in d and not isinstance(d["score"], dict):
            raise ApplyError(f"{where}: score must be an object")
        fid = _id(conn, d.get("id"), where)
        if fid in seen:
            raise ApplyError(f"{where}: duplicate decision for followup {fid}")
        seen.add(fid)
        action = d.get("action")
        if action not in ACTIONS:
            raise ApplyError(f"{where}: action must be {'|'.join(ACTIONS)}, got {action!r}")
        if d.get("snooze") is not None and action != "silent":
            raise ApplyError(f"{where}: snooze only applies to action=silent")
        if action != "silent":
            continue  # act_report / ask: the send receipt or a later outcome settles it
        if fid in sent_ids:
            raise ApplyError(f"{where}: followup {fid} is marked silent but also sent")
        # Same as `checked`: always move revisit_at forward, or it stays due forever.
        checks = conn.execute("SELECT checks FROM followups WHERE id=?", (fid,)).fetchone()[0]
        backoff = min(86400 * (2 ** min(checks or 0, 3)), 7 * 86400)
        nxt = _when(d["snooze"], f"{where}.snooze") if d.get("snooze") is not None else now + backoff
        conn.execute("UPDATE followups SET checks=checks+1, last_checked=?, revisit_at=? WHERE id=?",
                     (now, nxt, fid))
        res["checked"].append({"id": fid, "next": _iso(nxt)})

    for fid in sent_ids:
        conn.execute("UPDATE followups SET sent_at=?, sent_msg_id=?, checks=checks+1, "
                     "sends=sends+1, sent_text=?, revisit_at=? WHERE id=?",
                     (now, msg_id, sent_text, now + 86400, fid))
        res["sent"].append(fid)
    if sent_ids:
        _set_kv(conn, "last_send", now)

    wm = doc.get("watermark")
    if not isinstance(wm, (int, float)) or isinstance(wm, bool) or not wm > 0:
        raise ApplyError("watermark: required, the raw next_watermark float from digest")
    if wm > now + 60:
        raise ApplyError(f"watermark: {wm} is in the future; it would skip messages")
    _set_wm(conn, float(wm))
    _set_kv(conn, "last_capture", now)
    res["watermark"] = float(wm)

    res["log_id"] = conn.execute(
        "INSERT INTO log(run_at,kind,payload) VALUES(?,?,?)",
        (now, "apply", json.dumps(doc, ensure_ascii=False)),
    ).lastrowid
    return res


def cmd_apply(args) -> int:
    """Write back one whole review run — followups, decisions, send receipt,
    watermark, log — in a single transaction. All or nothing."""
    def fail(msg: str) -> int:
        print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False))
        return 2

    try:
        if args.file and args.file != "-":
            raw = Path(args.file).read_text(encoding="utf-8")
        else:
            if sys.stdin.isatty():
                return fail("apply: pass the JSON on stdin or with --file")
            raw = sys.stdin.read()
        doc = json.loads(raw)
    except (OSError, ValueError) as e:
        return fail(f"apply: cannot read JSON: {e}")
    if not isinstance(doc, dict):
        return fail("apply: top level must be a JSON object")
    extra = sorted(set(doc) - set(APPLY_KEYS))
    if extra:
        return fail(f"apply: unknown field(s) {extra}")

    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        res = _apply(conn, doc)
        conn.execute("COMMIT")
    except Exception as e:  # noqa: BLE001 - any failure must roll everything back
        if conn.in_transaction:  # some errors (disk full, ...) already rolled back
            conn.execute("ROLLBACK")
        msg = str(e) if isinstance(e, ApplyError) else f"{type(e).__name__}: {e}"
        return fail(f"apply rolled back, nothing written: {msg}")
    print(json.dumps({"ok": True, **res}, ensure_ascii=False))
    return 0


def cmd_watermark(args) -> int:
    conn = connect()
    if args.set:
        _set_wm(conn, _parse(args.set))
        print(json.dumps({"ok": True, "watermark": args.set}))
    else:
        print(json.dumps({"watermark": _iso(_get_wm(conn))}))
    return 0


def _recent_runs(conn, n: int) -> list[tuple[float, dict]]:
    """Newest-first (run_at, apply doc). A payload that is not a JSON object reads as {}."""
    out = []
    for r in conn.execute("SELECT run_at, payload FROM log ORDER BY id DESC LIMIT ?", (n,)):
        try:
            doc = json.loads(r["payload"] or "")
        except ValueError:
            doc = None
        out.append((r["run_at"], doc if isinstance(doc, dict) else {}))
    return out


def _sent_ids(doc: dict) -> list:
    s = doc.get("sent")
    if not isinstance(s, dict):
        return []
    ids = s.get("ids") if "ids" in s else [s.get("id")]
    return ids if isinstance(ids, list) else []


def cmd_why(_args) -> int:
    conn = connect()
    muted = muted_whys(conn)
    print(f"Muted categories (last 30 days: muted after 1 annoyed / {MUTE_IGNORED_AFTER} ignored):")
    for why, reason in sorted(muted.items()):
        print(f"  {why}: {reason}")
    if not muted:
        print("  (none)")
    runs = _recent_runs(conn, 5)
    print("Decisions from the last 5 reviews:")
    for run_at, doc in runs:
        decisions = [d for d in doc.get("decisions") or [] if isinstance(d, dict)]
        if not decisions:
            print(f"  {_iso(run_at)[:16]}  (no decisions)")
        for d in decisions:
            reason = str(d.get("reason") or "").replace("\n", " ")[:80]
            print(f"  {_iso(run_at)[:16]}  #{d.get('id')} {d.get('action')}  {reason}".rstrip())
    if not runs:
        print("  (no reviews yet)")
    return 0


def cmd_log(_args) -> int:
    conn = connect()
    runs = _recent_runs(conn, 10)
    for run_at, doc in runs:
        decisions = doc.get("decisions") or []
        sent = _sent_ids(doc)
        # The apply doc carries no mode; a run that decided or sent something was a review.
        mode = "review" if decisions or sent else "capture"
        print(f"{_iso(run_at)[:16]}  mode={mode:<7}  sent={len(sent)}  decisions={len(decisions)}")
    if not runs:
        print("(no reviews yet)")
    return 0


# -- doctor ------------------------------------------------------------------
# Upgrades have no post-update hook, so old installs keep whatever cron job the
# README of the day told them to create. doctor finds that job and, with --fix,
# edits it in place. It never creates, removes or recreates a job.

PLUGIN_DIR = Path(__file__).resolve().parent.parent
WAKE_SCRIPT = "followup-wake.py"
DIGEST_SCRIPT = "followup-digest.py"
JOB_NAMES = ("proactive follow-ups", "主动跟进")
FIX_ARGS = ("--monitor-script", WAKE_SCRIPT, "--deliver", "local", "--failure-deliver", "origin")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))


def _hermes(*args: str, timeout: float = 30) -> tuple[int, str, str]:
    """(returncode, stdout, stderr); 127 = no hermes on PATH, 124 = timed out."""
    exe = shutil.which("hermes")
    if not exe:
        return 127, "", "hermes not found on PATH"
    try:
        p = subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "", f"hermes {' '.join(args)} timed out"
    except OSError as e:
        return 126, "", f"{type(e).__name__}: {e}"
    return p.returncode, p.stdout or "", p.stderr or ""


def _last_line(*texts: str) -> str:
    for t in texts:
        lines = [l for l in (t or "").strip().splitlines() if l.strip()]
        if lines:
            return lines[-1].strip()
    return ""


def _parse_cron_text(out: str) -> list[dict]:
    """Best effort over the human `hermes cron list` output: a job header line
    `<id> [state]` followed by indented `Key: value` lines."""
    jobs: list[dict] = []
    for raw in _ANSI.sub("", out).splitlines():
        head = re.match(r"^\s*([A-Za-z0-9_.-]{4,})\s+\[[^\]]*\]", raw)
        if head:
            jobs.append({"id": head.group(1)})
            continue
        kv = re.match(r"^\s+([A-Za-z][A-Za-z _-]*?)\s*:\s+(.*\S)\s*$", raw)
        if kv and jobs:
            jobs[-1][re.sub(r"[\s-]+", "_", kv.group(1).strip().lower())] = kv.group(2)
    return jobs


def _list_cron_jobs() -> tuple[list[dict] | None, str]:
    """(jobs, error). Reads Hermes' own job store first: it is the source the CLI
    renders, it carries the real field names, and it includes disabled jobs. The
    CLI is only a fallback for stores we cannot read."""
    store = _hermes_home() / "cron" / "jobs.json"
    try:
        doc = json.loads(store.read_text(encoding="utf-8"))
        jobs = doc.get("jobs") if isinstance(doc, dict) else doc
        if isinstance(jobs, dict):
            jobs = list(jobs.values())
        if isinstance(jobs, list):
            return [j for j in jobs if isinstance(j, dict)], ""
    except (OSError, ValueError):
        pass
    rc, out, err = _hermes("cron", "list", "--json")
    if rc == 127:
        return None, err
    if rc == 0:
        try:
            doc = json.loads(out)
        except ValueError:
            doc = None
        jobs = doc.get("jobs") if isinstance(doc, dict) else doc
        if isinstance(jobs, list):
            return [j for j in jobs if isinstance(j, dict)], ""
    rc, out, err = _hermes("cron", "list")
    if rc != 0:
        return None, _last_line(err, out) or f"hermes cron list exited {rc}"
    return _parse_cron_text(out), ""


def _norm_job(j: dict) -> dict:
    def g(*keys):
        for k in keys:
            v = j.get(k)
            if v not in (None, "", [], {}):
                return v
        return None

    def s(v) -> str:
        if isinstance(v, list):
            return ",".join(str(x) for x in v)
        return "" if v is None else str(v)

    return {
        "id": s(g("id", "job_id")),
        "name": s(g("name")),
        "prompt": s(g("prompt")),
        "script": s(g("script")),
        "monitor_script": s(g("monitor_script", "monitor", "monitorScript")),
        "deliver": s(g("deliver", "delivery")),
        "failure_deliver": s(g("failure_deliver", "failureDeliver")),
    }


def _is_ours(job: dict) -> bool:
    name = job["name"].lower()
    return ("prompts/review.md" in job["prompt"]
            or Path(job["script"]).name == DIGEST_SCRIPT
            or any(n in name for n in JOB_NAMES))


def _cmd(argv) -> str:
    return " ".join(shlex.quote(a) for a in argv)


def _create_commands() -> list[str]:
    scripts = _hermes_home() / "scripts"
    return [
        f"mkdir -p {shlex.quote(str(scripts))} && cp {shlex.quote(str(PLUGIN_DIR / 'cron'))}/*.py "
        f"{shlex.quote(str(scripts))}/",
        _cmd(["hermes", "cron", "create", "*/15 * * * *", "--name", JOB_NAMES[0],
              "--monitor-script", WAKE_SCRIPT, "--script", DIGEST_SCRIPT,
              "--prompt", f"Read {PLUGIN_DIR / 'prompts' / 'review.md'} and follow it",
              "--deliver", "local", "--failure-deliver", "origin"]),
    ]


def _doctor(fix: bool) -> dict:
    checks: list[dict] = []
    suggest: list[str] = []

    def check(name: str, status: str, detail: str) -> dict:
        c = {"name": name, "status": status, "detail": detail, "fixed": False}
        checks.append(c)
        return c

    # a. settings
    cur = load_settings()
    for key, env in (("owner_id", "AUTONOMY_OWNER_ID"), ("dm_target", "AUTONOMY_DM_TARGET")):
        if os.environ.get(env) or cur.get(key):
            check(f"settings.{key}", "OK", "set" + (f" (from {env})" if os.environ.get(env) else ""))
        else:
            check(f"settings.{key}", "FAIL",
                  f"missing: run followups.py setup --{key.replace('_', '-')} …")
    defaults = {"timezone": "system local time", "include_groups": "false (DMs only)",
                "dev_log": "false (runs reply [SILENT])"}
    for key, default in defaults.items():
        if key in cur:
            check(f"settings.{key}", "OK", str(cur[key]))
        else:
            check(f"settings.{key}", "OK", f"not set, using default: {default}")

    # b. the cron job(s) this plugin owns
    raw, err = _list_cron_jobs()
    jobs: list[dict] = []
    if raw is None:
        check("cron", "FAIL", f"cannot list cron jobs: {err}")
    else:
        jobs = [j for j in (_norm_job(r) for r in raw) if _is_ours(j)]
        if not jobs:
            check("cron", "WARN", "no follow-up cron job found: nothing will ever run. Create it with "
                                  "the commands below")
            suggest += _create_commands()
        elif len(jobs) > 1:
            check("cron", "WARN", f"{len(jobs)} follow-up jobs found ({', '.join(j['id'] for j in jobs)}): "
                                  "the review runs once per job. Remove the extras with "
                                  "hermes cron remove <id>")
        else:
            check("cron", "OK", f"job {jobs[0]['id']} ({jobs[0]['name'] or 'unnamed'})")

    # c. each job
    scripts = _hermes_home() / "scripts"
    for job in jobs:
        jid = job["id"] or "?"
        broken: list[dict] = []
        if job["monitor_script"]:
            check(f"job {jid}: monitor_script", "OK", job["monitor_script"])
        else:
            broken.append(check(f"job {jid}: monitor_script", "WARN",
                                "no monitor script: the agent runs unconditionally every 15 minutes"))
        deliver = job["deliver"].strip().lower()
        if deliver == "local":
            check(f"job {jid}: deliver", "OK", "local")
        elif not deliver:
            check(f"job {jid}: deliver", "OK", "not reported by hermes; its default applies")
        else:
            broken.append(check(f"job {jid}: deliver", "WARN",
                                f"deliver={job['deliver']}: every normal run posts its final reply to "
                                "that chat; use local"))
        for field in ("script", "monitor_script"):
            val = job[field]
            if not val:
                continue
            path = Path(val).expanduser()
            path = path if path.is_absolute() else scripts / path
            if path.exists():
                check(f"job {jid}: {field} file", "OK", str(path))
            else:
                check(f"job {jid}: {field} file", "FAIL",
                      f"{path} does not exist: the job fails every run. Copy the helpers back")
                suggest.append(_create_commands()[0])
        if not broken:
            continue
        edit = ["hermes", "cron", "edit", jid, *FIX_ARGS]
        # Pointing the job at a monitor script that is not there would make every
        # run fail (and --failure-deliver origin would then post that every 15 min).
        wake_ok = (scripts / WAKE_SCRIPT).exists()
        if not fix or not job["id"] or not wake_ok:
            if not wake_ok:
                suggest.append(_create_commands()[0])
            suggest.append(_cmd(edit))
            continue
        rc, out, err = _hermes(*edit[1:])
        for c in broken:
            c["fixed"] = rc == 0
            c["detail"] += (f" — fixed: {_cmd(edit)}" if rc == 0 else
                            f" — fix failed ({_last_line(err, out) or f'exit {rc}'}): {_cmd(edit)}")
        if rc != 0:
            suggest.append(_cmd(edit))

    ok = all(c["status"] == "OK" or c["fixed"] for c in checks)
    return {"ok": ok, "checks": checks, "jobs": jobs,
            "suggested_commands": list(dict.fromkeys(suggest))}


def cmd_doctor(args) -> int:
    rep = _doctor(fix=args.fix)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False))
    else:
        for c in rep["checks"]:
            status = "FIXED" if c["fixed"] else c["status"]
            print(f"[{status:<5}] {c['name']}: {c['detail']}")
        if rep["suggested_commands"]:
            print("\nRun:" if not args.fix else "\nCould not fix automatically; run:")
            for cmd in rep["suggested_commands"]:
                print(f"  {cmd}")
            if not args.fix and any(" cron edit " in c for c in rep["suggested_commands"]):
                print("  (or: followups.py doctor --fix)")
        print("\nok" if rep["ok"] else "\nnot ok")
    return 0 if rep["ok"] else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("digest")
    d.add_argument("--hours", type=float, default=0)
    d.set_defaults(fn=cmd_digest)

    a = sub.add_parser("add")
    a.add_argument("--key", required=True)
    a.add_argument("--kind", required=True)
    a.add_argument("--why", required=True, help="care|utility|risk|curiosity")
    a.add_argument("--subject", required=True)
    a.add_argument("--quote", default="")
    a.add_argument("--hint", default="")
    a.add_argument("--revisit-at", required=True, help="ISO time, 'now', '+12h', '+2d'")
    a.set_defaults(fn=cmd_add)

    l = sub.add_parser("list")
    l.add_argument("--all", action="store_true")
    l.set_defaults(fn=cmd_list)

    u = sub.add_parser("due")
    u.add_argument("--at", default="")
    u.set_defaults(fn=cmd_due)

    ck = sub.add_parser("checked")
    ck.add_argument("id", type=int)
    ck.add_argument("--snooze", default="", help="next revisit, e.g. +1d")
    ck.set_defaults(fn=cmd_checked)

    se = sub.add_parser("sent")
    se.add_argument("id", type=int)
    se.add_argument("--msg-id", default="", help="message_id from `hermes send --json`")
    se.add_argument("--text", default="", help="the message body as sent")
    se.set_defaults(fn=cmd_sent)

    oc = sub.add_parser("outcome")
    oc.add_argument("id", type=int)
    oc.add_argument("--value", required=True,
                    help="accepted|ignored|annoyed|useless|unknown")
    oc.set_defaults(fn=cmd_outcome)

    su = sub.add_parser("setup")
    su.add_argument("--owner-id", default="")
    su.add_argument("--dm-target", default="")
    su.add_argument("--timezone", default="", help="IANA name, e.g. Asia/Shanghai")
    su.add_argument("--include-groups", action=argparse.BooleanOptionalAction, default=None,
                    help="also read group/thread/forum/channel sessions (other members' "
                         "messages included); default off")
    su.add_argument("--dev-log", action=argparse.BooleanOptionalAction, default=None,
                    help="write a developer log as each run's final reply (internal "
                         "testing); default off = final reply is [SILENT]")
    su.set_defaults(fn=cmd_setup)

    dmt = sub.add_parser("dm-target")
    dmt.set_defaults(fn=cmd_dm_target)

    mk = sub.add_parser("mark")
    mk.add_argument("--capture", action="store_true")
    mk.add_argument("--sent", action="store_true")
    mk.set_defaults(fn=cmd_mark)

    cl = sub.add_parser("close")
    cl.add_argument("id", type=int)
    cl.add_argument("--note", default="")
    cl.set_defaults(fn=cmd_close)

    ap_ = sub.add_parser("apply")
    ap_.add_argument("--file", default="", help="apply JSON (default: stdin)")
    ap_.set_defaults(fn=cmd_apply)

    w = sub.add_parser("watermark")
    w.add_argument("--set", default="")
    w.set_defaults(fn=cmd_watermark)

    wy = sub.add_parser("why")
    wy.set_defaults(fn=cmd_why)

    lg = sub.add_parser("log")
    lg.set_defaults(fn=cmd_log)

    dr = sub.add_parser("doctor")
    dr.add_argument("--fix", action="store_true",
                    help="edit matching cron jobs in place (monitor script, deliver local)")
    dr.add_argument("--json", action="store_true")
    dr.set_defaults(fn=cmd_doctor)

    args = ap.parse_args()
    rc = args.fn(args)
    return rc if isinstance(rc, int) else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(0)
