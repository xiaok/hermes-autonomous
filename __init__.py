"""hermes-autonomous — proactive follow-ups for Hermes Agent.

The plugin is deliberately thin: one ``/followups`` command to look at and edit
the board. Nothing runs on its own — the review loop is an ordinary Hermes cron
job that runs ``scripts/followups.py digest`` and then follows
``prompts/review.md``; the cheap wake gate is ``scripts/wake.py``.

No hooks, no background threads, no network calls, no credentials.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "scripts" / "followups.py"

_USAGE = (
    "用法：/followups [list|due|close <id>|checked <id>|outcome <id> --value …]\n"
    "不带参数 = 看全部（含已关闭）。"
)


def _followups_command(raw_args: str) -> str:
    """Thin chat wrapper over scripts/followups.py so the user can see the board."""
    if not SCRIPT.exists():
        return f"找不到 {SCRIPT}（插件目录不完整）"

    args = (raw_args or "").split()
    allowed = {"list", "due", "close", "checked", "outcome", "add"}
    sub = args[0] if args and args[0] in allowed else "list"
    cmd = [sys.executable, str(SCRIPT), sub]
    if sub == "list" and "--all" not in args:
        cmd.append("--all")
    cmd += args[1:]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
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
        args_hint="[list|due|close <id>|checked <id>]",
    )


__all__ = ["register"]
