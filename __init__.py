"""hermes-autonomous — proactive follow-ups for Hermes Agent.

The plugin is deliberately thin: one ``/followups`` command to look at and edit
the board. Nothing runs on its own — the review loop is an ordinary Hermes cron
job that runs ``scripts/followups.py digest`` and then follows
``prompts/review.md``; the cheap wake gate is ``scripts/wake.py``.

No hooks, no background threads, no network calls, no credentials.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "scripts" / "followups.py"


def _hermes_home() -> str:
    """The gateway's current profile home, so the helper reads the same board."""
    try:
        from hermes_constants import get_hermes_home
        return str(get_hermes_home())
    except Exception:  # noqa: BLE001 - outside Hermes, or an older Hermes
        return os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")

_USAGE = (
    "用法：/followups [list|due|why|log|doctor [--fix]|close <id>|checked <id>|outcome <id> --value …]\n"
    "不带参数 = 看全部（含已关闭）。why = 哪些类别被静默 + 最近决策；log = 最近 10 次复盘；"
    "doctor = 检查配置和定时任务（只读），doctor --fix = 原地修正定时任务。"
)


def _followups_command(raw_args: str) -> str:
    """Thin chat wrapper over scripts/followups.py so the user can see the board."""
    if not SCRIPT.exists():
        return f"找不到 {SCRIPT}（插件目录不完整）"

    args = (raw_args or "").split()
    allowed = {"list", "due", "close", "checked", "outcome", "add", "why", "log", "doctor"}
    sub = args[0] if args and args[0] in allowed else "list"
    cmd = [sys.executable, str(SCRIPT), sub]
    if sub == "list" and "--all" not in args:
        cmd.append("--all")
    cmd += args[1:]

    try:
        env = {**os.environ, "HERMES_HOME": _hermes_home()}
        # doctor shells out to `hermes cron list/edit`; give it room.
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env,
                              timeout=120 if sub == "doctor" else 20)
    except Exception as exc:  # noqa: BLE001 - a chat command must never raise
        return f"读取失败：{type(exc).__name__}: {exc}"
    out = (proc.stdout or "").strip()
    if out:
        return out
    err = (proc.stderr or "").strip().splitlines()
    return f"读取失败：{err[-1]}" if err else _USAGE


def register(ctx) -> None:
    ctx.register_command(
        "followups",
        _followups_command,
        description="Show or edit the proactive follow-up board (what I'm quietly tracking)",
        args_hint="[list|due|why|log|doctor [--fix]|close <id>|checked <id>]",
    )


__all__ = ["register"]
