"""hermes-autonomous — proactive follow-ups for Hermes Agent.

The plugin is deliberately thin: one ``/followups`` command to look at and edit
the board. Nothing runs on its own — the review loop is an ordinary Hermes cron
job that runs ``scripts/followups.py digest`` and then follows
``prompts/review.md``; the cheap wake gate is ``scripts/wake.py``.

No hooks, no background threads, no network calls, no credentials.
"""

from __future__ import annotations

import json
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

def _session_env(name: str) -> str:
    """The calling session's variable: the gateway binds these per turn, so prefer
    its accessor over os.environ (which may belong to another session or none)."""
    try:
        from gateway.session_context import get_session_env
        return str(get_session_env(name, "") or "")
    except Exception:  # noqa: BLE001 - outside the gateway, or an older Hermes
        return os.environ.get(name, "")


def _owner_id(home: str) -> str:
    """Same lookup as followups.py: AUTONOMY_OWNER_ID, then settings.json."""
    if os.environ.get("AUTONOMY_OWNER_ID"):
        return os.environ["AUTONOMY_OWNER_ID"]
    try:
        p = Path(home) / "plugin-data" / "hermes-autonomous" / "settings.json"
        return str(json.loads(p.read_text(encoding="utf-8")).get("owner_id") or "")
    except Exception:  # noqa: BLE001 - not set up yet, or unreadable
        return ""


DENIED = "This command is only available to the owner."
SHOWN_HERE = "Note: this command's output is shown in the current chat."
_WRITES = {"close", "checked", "outcome", "add"}

_USAGE = (
    "Usage: /followups [list|due|why|log|doctor [--fix]|close <id>|checked <id>|outcome <id> --value …]\n"
    "No arguments = show everything (including closed). why = muted categories + recent decisions; "
    "log = last 10 reviews; doctor = check config and the cron job (read-only), "
    "doctor --fix = repair the cron job in place."
)


def _followups_command(raw_args: str) -> str:
    """Thin chat wrapper over scripts/followups.py so the user can see the board."""
    if not SCRIPT.exists():
        return f"Cannot find {SCRIPT} (the plugin directory is incomplete)"

    home = _hermes_home()
    # The board quotes the owner's private chats: in a group anyone can type the
    # command, so only the owner may run it. No user id (CLI, tests) = a local
    # caller, who already has the files.
    user_id = _session_env("HERMES_SESSION_USER_ID").strip()
    chat_type = _session_env("HERMES_SESSION_CHAT_TYPE").strip().lower()
    if user_id and user_id != _owner_id(home).strip():
        return DENIED

    args = (raw_args or "").split()
    allowed = {"list", "due", "close", "checked", "outcome", "add", "why", "log", "doctor"}
    sub = args[0] if args and args[0] in allowed else "list"
    writes = sub in _WRITES or (sub == "doctor" and "--fix" in args)
    note = f"\n{SHOWN_HERE}" if writes and chat_type and chat_type != "dm" else ""
    cmd = [sys.executable, str(SCRIPT), sub]
    if sub == "list" and "--all" not in args:
        cmd.append("--all")
    cmd += args[1:]

    try:
        env = {**os.environ, "HERMES_HOME": home}
        # doctor shells out to `hermes cron list/edit`; give it room.
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env,
                              timeout=120 if sub == "doctor" else 20)
    except Exception as exc:  # noqa: BLE001 - a chat command must never raise
        return f"Failed to read: {type(exc).__name__}: {exc}{note}"
    out = (proc.stdout or "").strip()
    if out:
        return out + note
    err = (proc.stderr or "").strip().splitlines()
    return (f"Failed to read: {err[-1]}" if err else _USAGE) + note


def register(ctx) -> None:
    ctx.register_command(
        "followups",
        _followups_command,
        description="Show or edit the proactive follow-up board (what I'm quietly tracking)",
        args_hint="[list|due|why|log|doctor [--fix]|close <id>|checked <id>]",
    )


__all__ = ["register"]
