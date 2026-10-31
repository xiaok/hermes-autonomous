"""SQLite-backed work queue for hermes-autonomous.

Deliberately dependency-free (stdlib only) so the same module works inside the
plugin process and from the standalone tick runner (scripts/autonomy_tick.py).

State lives in ``<hermes home>/plugin-data/hermes-autonomous/queue.db`` — never
inside the installed plugin dir, which is replaced on update.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Optional

PLUGIN_NAME = "hermes-autonomous"
STATUSES = ("queued", "running", "done", "failed", "cancelled", "blocked")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id           TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    prompt       TEXT NOT NULL,
    verify       TEXT NOT NULL DEFAULT '',
    priority     INTEGER NOT NULL DEFAULT 5,
    status       TEXT NOT NULL DEFAULT 'queued',
    tags         TEXT NOT NULL DEFAULT '',
    source       TEXT NOT NULL DEFAULT '',
    created_at   REAL NOT NULL,
    started_at   REAL,
    finished_at  REAL,
    attempts     INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 2,
    needs_approval INTEGER NOT NULL DEFAULT 0,
    approved     INTEGER NOT NULL DEFAULT 0,
    result       TEXT NOT NULL DEFAULT '',
    error        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, priority, created_at);
CREATE TABLE IF NOT EXISTS runs (
    id          TEXT PRIMARY KEY,
    task_id     TEXT NOT NULL,
    started_at  REAL NOT NULL,
    finished_at REAL,
    exit_code   INTEGER,
    output      TEXT NOT NULL DEFAULT ''
);
"""

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "max_runs_per_day": 12,
    "max_runtime_seconds": 900,
    "quiet_hours": [0, 8],          # local hours [start, end): no runs
    "max_failures_per_day": 4,
    "model": "",
}


def data_dir() -> Path:
    home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    root = Path(home) / "plugin-data" / PLUGIN_NAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def config_path() -> Path:
    return data_dir() / "config.json"


def load_config() -> dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    p = config_path()
    if p.exists():
        try:
            cfg.update(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            pass
    return cfg


def save_config(cfg: dict[str, Any]) -> dict[str, Any]:
    merged = dict(DEFAULT_CONFIG)
    merged.update({k: v for k, v in cfg.items() if k in DEFAULT_CONFIG})
    config_path().write_text(json.dumps(merged, indent=2), encoding="utf-8")
    return merged


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(data_dir() / "queue.db", timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    return conn


def _row(r: sqlite3.Row) -> dict[str, Any]:
    return {k: r[k] for k in r.keys()}


def enqueue(
    title: str,
    prompt: str,
    verify: str = "",
    priority: int = 5,
    tags: str = "",
    source: str = "",
    max_attempts: int = 2,
    needs_approval: bool = False,
) -> dict[str, Any]:
    tid = uuid.uuid4().hex[:12]
    with connect() as conn:
        conn.execute(
            "INSERT INTO tasks (id,title,prompt,verify,priority,status,tags,source,"
            "created_at,max_attempts,needs_approval) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (tid, title, prompt, verify, int(priority), "queued", tags, source,
             time.time(), int(max_attempts), int(bool(needs_approval))),
        )
    return get_task(tid) or {}


def get_task(task_id: str) -> Optional[dict[str, Any]]:
    with connect() as conn:
        r = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    return _row(r) if r else None


def list_tasks(status: str = "", limit: int = 20) -> list[dict[str, Any]]:
    q = "SELECT * FROM tasks"
    args: list[Any] = []
    if status and status != "all":
        q += " WHERE status=?"
        args.append(status)
    q += " ORDER BY priority ASC, created_at ASC LIMIT ?"
    args.append(int(limit))
    with connect() as conn:
        return [_row(r) for r in conn.execute(q, args).fetchall()]


def claim_next() -> Optional[dict[str, Any]]:
    """Atomically move the next eligible task to ``running`` and return it."""
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        r = conn.execute(
            "SELECT * FROM tasks WHERE status='queued' AND (needs_approval=0 OR approved=1) "
            "ORDER BY priority ASC, created_at ASC LIMIT 1"
        ).fetchone()
        if not r:
            conn.execute("COMMIT")
            return None
        tid = r["id"]
        now = time.time()
        conn.execute(
            "UPDATE tasks SET status='running', started_at=?, attempts=attempts+1 WHERE id=?",
            (now, tid),
        )
        run_id = uuid.uuid4().hex[:12]
        conn.execute("INSERT INTO runs (id,task_id,started_at) VALUES (?,?,?)", (run_id, tid, now))
        conn.execute("COMMIT")
        task = _row(r)
    task.update(status="running", started_at=now, attempts=task["attempts"] + 1)
    task["run_id"] = run_id
    return task


def finish(task_id: str, status: str, result: str = "", error: str = "",
           exit_code: Optional[int] = None, run_id: str = "") -> dict[str, Any]:
    if status not in STATUSES:
        raise ValueError(f"bad status {status!r}")
    now = time.time()
    with connect() as conn:
        conn.execute(
            "UPDATE tasks SET status=?, finished_at=?, result=?, error=? WHERE id=?",
            (status, now, result[:20000], error[:4000], task_id),
        )
        if run_id:
            conn.execute(
                "UPDATE runs SET finished_at=?, exit_code=?, output=? WHERE id=?",
                (now, exit_code, result[:20000], run_id),
            )
    return get_task(task_id) or {}


def approve(task_id: str, approved: bool = True) -> dict[str, Any]:
    with connect() as conn:
        conn.execute("UPDATE tasks SET approved=? WHERE id=?", (int(approved), task_id))
    return get_task(task_id) or {}


def cancel(task_id: str) -> dict[str, Any]:
    with connect() as conn:
        conn.execute(
            "UPDATE tasks SET status='cancelled', finished_at=? WHERE id=? AND status IN ('queued','blocked')",
            (time.time(), task_id),
        )
    return get_task(task_id) or {}


def requeue(task_id: str) -> dict[str, Any]:
    with connect() as conn:
        conn.execute("UPDATE tasks SET status='queued', started_at=NULL WHERE id=?", (task_id,))
    return get_task(task_id) or {}


def runs_today() -> dict[str, int]:
    start = time.time() - (time.time() % 86400)
    with connect() as conn:
        total = conn.execute("SELECT COUNT(*) c FROM runs WHERE started_at>=?", (start,)).fetchone()["c"]
        bad = conn.execute(
            "SELECT COUNT(*) c FROM tasks WHERE finished_at>=? AND status='failed'", (start,)
        ).fetchone()["c"]
    return {"runs": total, "failures": bad}


def status() -> dict[str, Any]:
    with connect() as conn:
        rows = conn.execute("SELECT status, COUNT(*) c FROM tasks GROUP BY status").fetchall()
        last = conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        recent = conn.execute(
            "SELECT id,title,status,finished_at FROM tasks WHERE finished_at IS NOT NULL "
            "ORDER BY finished_at DESC LIMIT 5"
        ).fetchall()
    counts = {s: 0 for s in STATUSES}
    for r in rows:
        counts[r["status"]] = r["c"]
    return {
        "counts": counts,
        "today": runs_today(),
        "config": load_config(),
        "last_run": _row(last) if last else None,
        "recent": [_row(r) for r in recent],
    }
