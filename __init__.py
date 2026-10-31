"""hermes-autonomous — a self-directed work queue for Hermes.

Registers six tools plus ``/autonomy``. Execution is driven by
``scripts/autonomy_tick.py`` (run from a Hermes cron job): it claims one queued
task and executes it in a fresh headless session, so the loop survives gateway
restarts and never blocks the chat you are talking in.
"""

from __future__ import annotations

import json

from . import schemas, store, tools

_TOOLS = [
    (schemas.TOOL_ENQUEUE, tools.handle_enqueue),
    (schemas.TOOL_LIST, tools.handle_list),
    (schemas.TOOL_CLAIM, tools.handle_claim),
    (schemas.TOOL_FINISH, tools.handle_finish),
    (schemas.TOOL_STATUS, tools.handle_status),
    (schemas.TOOL_CONTROL, tools.handle_control),
]


def _fmt_task(t: dict) -> str:
    flags = []
    if t.get("needs_approval") and not t.get("approved"):
        flags.append("needs-approval")
    tail = f" [{','.join(flags)}]" if flags else ""
    return f"- {t['id']} p{t['priority']} {t['title']}{tail}"


def _autonomy_command(raw_args: str) -> str:
    parts = (raw_args or "").strip().split(None, 1)
    sub = parts[0].lower() if parts else "status"
    rest = parts[1] if len(parts) > 1 else ""

    if sub in ("status", "st"):
        s = store.status()
        c = s["counts"]
        cfg = s["config"]
        lines = [
            f"autonomous: {'ON' if cfg.get('enabled') else 'PAUSED'} · "
            f"queued {c['queued']} · running {c['running']} · done {c['done']} · failed {c['failed']}",
            f"today: {s['today']['runs']}/{cfg.get('max_runs_per_day')} runs, {s['today']['failures']} failures",
        ]
        if s.get("last_run"):
            lr = s["last_run"]
            lines.append(f"last: {lr['task_id']} exit={lr['exit_code']} at {int(lr['started_at'])}")
        return "\n".join(lines)

    if sub in ("list", "ls"):
        name = rest or "queued"
        rows = store.list_tasks(name, 30)
        if not rows:
            return f"队列为空（{name}）。"
        return "\n".join(_fmt_task(t) for t in rows)

    if sub in ("add",):
        if "|" not in rest:
            return "用法：/autonomy add 标题 | 自包含的任务说明"
        title, prompt = rest.split("|", 1)
        t = store.enqueue(title.strip(), prompt.strip(), source="slash")
        return f"已入队 {t['id']}：{t['title']}"

    if sub in ("cancel", "rm"):
        if not rest:
            return "用法：/autonomy cancel <task_id>"
        t = store.cancel(rest.strip())
        return f"{t['id']} → {t['status']}"

    if sub in ("approve",):
        if not rest:
            return "用法：/autonomy approve <task_id>"
        t = store.approve(rest.strip(), True)
        return f"{t['id']} 已批准"

    if sub in ("requeue", "retry"):
        if not rest:
            return "用法：/autonomy requeue <task_id>"
        t = store.requeue(rest.strip())
        return f"{t['id']} → {t['status']}"

    if sub in ("pause", "off"):
        store.save_config({**store.load_config(), "enabled": False})
        return "autonomous 已暂停（tick 会空转退出）"

    if sub in ("resume", "on"):
        store.save_config({**store.load_config(), "enabled": True})
        return "autonomous 已恢复"

    if sub in ("config", "cfg"):
        if not rest:
            return json.dumps(store.load_config(), ensure_ascii=False, indent=2)
        try:
            patch = json.loads(rest)
        except Exception:
            return "用法：/autonomy config {\"max_runs_per_day\": 6}"
        merged = store.load_config()
        merged.update(patch if isinstance(patch, dict) else {})
        return json.dumps(store.save_config(merged), ensure_ascii=False)

    if sub in ("claim", "next"):
        t = store.claim_next()
        if not t:
            return "没有可执行的任务。"
        return f"claimed {t['id']}：{t['title']}\n---\n{t['prompt']}"

    return (
        "用法：/autonomy [status|list [status]|add 标题|说明|cancel <id>|approve <id>|"
        "requeue <id>|claim|pause|resume|config [json]]"
    )


def _followups_command(raw_args: str) -> str:
    """Thin chat wrapper over scripts/followups.py so the user can see the board."""
    import subprocess
    from pathlib import Path

    script = Path(__file__).resolve().parent / "scripts/followups.py"
    if not script.exists():
        return "找不到 followups.py（仓库不在 ~/opt/hermes-autonomous）"
    args = (raw_args or "").split()
    allowed = {"list", "due", "add", "close", "checked"}
    sub = args[0] if args and args[0] in allowed else "list"
    cmd = ["python3", str(script), sub]
    if sub == "list" and "--all" not in args:
        cmd.append("--all")
    cmd += [a for a in args[1:] if sub != a]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception as exc:  # noqa: BLE001
        return f"读取失败：{exc}"
    return out or "（空）"


def register(ctx) -> None:
    for schema, handler in _TOOLS:
        ctx.register_tool(
            name=schema["name"],
            toolset="autonomy",
            schema=schema,
            handler=handler,
            description=schema["description"],
        )
    ctx.register_command(
        "autonomy",
        _autonomy_command,
        description="Autonomous work queue: status, list, add, cancel, approve, claim, pause/resume",
        args_hint="[status|list|add|cancel|approve|requeue|claim|pause|resume|config]",
    )
    ctx.register_command(
        "followups",
        _followups_command,
        description="Show or edit the proactive follow-up board (what I'm quietly tracking)",
        args_hint="[list|due|close <id>]",
    )


__all__ = ["register"]
