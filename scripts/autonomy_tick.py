#!/usr/bin/env python3
"""Ticker: claim one queued task and run it in a fresh headless Hermes session.

Intended to be called by a Hermes cron job (no_agent script, deliver=origin), e.g.
``*/10 9-23 * * *``. Prints a one-paragraph report on stdout only when it did
something; silence means "nothing to do", which keeps the chat quiet.

Guardrails: kill switch, daily run cap, failure cap, quiet hours, per-run timeout.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import store  # noqa: E402  (local module, path set above)


def _hermes_bin() -> str:
    for cand in (
        os.environ.get("HERMES_BIN"),
        shutil.which("hermes"),
        str(Path.home() / ".hermes/hermes-agent/.hermes/bin/hermes"),
    ):
        if cand and Path(cand).exists():
            return cand
    return "hermes"


def _in_quiet_hours(quiet: list) -> bool:
    try:
        start, end = int(quiet[0]), int(quiet[1])
    except Exception:
        return False
    hour = datetime.now().hour
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end  # wraps midnight


def _gate(cfg: dict) -> str:
    if not cfg.get("enabled", True):
        return "paused"
    if _in_quiet_hours(cfg.get("quiet_hours") or [0, 8]):
        return "quiet-hours"
    today = store.runs_today()
    if today["runs"] >= int(cfg.get("max_runs_per_day", 12)):
        return "daily-run-cap"
    if today["failures"] >= int(cfg.get("max_failures_per_day", 4)):
        return "failure-cap"
    return ""


def _build_prompt(task: dict) -> str:
    parts = [
        "[AUTONOMOUS RUN] 你在无人监督的情况下执行一个队列任务。",
        f"任务：{task['title']}",
        "",
        task["prompt"].strip(),
    ]
    if task.get("verify"):
        parts += ["", f"验收标准：{task['verify']}"]
    parts += [
        "",
        "规则：不要向用户提问（没人回答）。能做完就做完；做不完就把当前状态、卡点和下一步写清楚。",
        "最后输出一段不超过 200 字的结果摘要，说明产物在哪里、是否通过验收。",
    ]
    return "\n".join(parts)


def main() -> int:
    cfg = store.load_config()
    blocked = _gate(cfg)
    if blocked:
        if blocked != "quiet-hours" and blocked != "paused":
            print(f"autonomous: skipped ({blocked})")
        return 0

    task = store.claim_next()
    if not task:
        return 0

    timeout = int(cfg.get("max_runtime_seconds", 900))
    cmd = [_hermes_bin(), "chat", "-q", _build_prompt(task)]
    model = (cfg.get("model") or "").strip()
    if model:
        cmd[2:2] = ["--model", model]

    started = time.time()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(Path.home()),
        )
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        code = proc.returncode
    except subprocess.TimeoutExpired:
        out, err, code = "", f"timeout after {timeout}s", 124
    except Exception as exc:  # noqa: BLE001
        out, err, code = "", f"{type(exc).__name__}: {exc}", 1

    ok = code == 0
    summary = (out or err)[-4000:]
    store.finish(
        task["id"],
        "done" if ok else "failed",
        result=summary,
        error="" if ok else (err or f"exit {code}")[-2000:],
        exit_code=code,
        run_id=task.get("run_id", ""),
    )

    took = int(time.time() - started)
    head = "✅" if ok else "❌"
    tail = summary.strip().splitlines()[-6:]
    print(f"{head} autonomous · {task['title']} ({task['id']}) · {took}s · exit {code}")
    if tail:
        print("\n".join(tail))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
