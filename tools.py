"""Tool handlers. Every handler returns a JSON string; exceptions become error JSON."""

from __future__ import annotations

import json
from typing import Any, Callable

from . import store


def _ok(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _fail(err: Exception) -> str:
    return json.dumps({"success": False, "error": f"{type(err).__name__}: {err}"}, ensure_ascii=False)


def _wrap(fn: Callable[..., Any]) -> Callable[..., str]:
    def inner(params: dict[str, Any], **kwargs: Any) -> str:
        try:
            return _ok(fn(params or {}, **kwargs))
        except Exception as exc:  # noqa: BLE001 - tool surface must not raise
            return _fail(exc)
    return inner


@_wrap
def handle_enqueue(p: dict[str, Any], **_: Any) -> dict[str, Any]:
    task = store.enqueue(
        title=str(p.get("title") or "").strip(),
        prompt=str(p.get("prompt") or "").strip(),
        verify=str(p.get("verify") or ""),
        priority=int(p.get("priority") or 5),
        tags=str(p.get("tags") or ""),
        source="agent",
        max_attempts=int(p.get("max_attempts") or 2),
        needs_approval=True,   # agent-created work always waits for the user
    )
    return {"success": True, "task": task}


@_wrap
def handle_list(p: dict[str, Any], **_: Any) -> dict[str, Any]:
    status = str(p.get("status") or "queued")
    limit = max(1, min(200, int(p.get("limit") or 20)))
    return {"success": True, "tasks": store.list_tasks(status, limit)}


@_wrap
def handle_claim(_p: dict[str, Any], **_: Any) -> dict[str, Any]:
    task = store.claim_next()
    return {"success": True, "task": task, "empty": task is None}


@_wrap
def handle_finish(p: dict[str, Any], **_: Any) -> dict[str, Any]:
    tid = str(p.get("task_id") or "")
    if not tid:
        raise ValueError("task_id required")
    task = store.finish(
        tid,
        status=str(p.get("status") or "done"),
        result=str(p.get("result") or ""),
        error=str(p.get("error") or ""),
    )
    return {"success": True, "task": task}


@_wrap
def handle_status(_p: dict[str, Any], **_: Any) -> dict[str, Any]:
    return {"success": True, **store.status()}


@_wrap
def handle_control(p: dict[str, Any], **_: Any) -> dict[str, Any]:
    action = str(p.get("action") or "")
    tid = str(p.get("task_id") or "")
    if action == "pause":
        return {"success": True, "config": store.save_config({**store.load_config(), "enabled": False})}
    if action == "resume":
        return {"success": True, "config": store.save_config({**store.load_config(), "enabled": True})}
    if action in ("cancel", "requeue"):
        if not tid:
            raise ValueError(f"task_id required for {action}")
        if action == "cancel":
            return {"success": True, "task": store.cancel(tid)}
        return {"success": True, "task": store.requeue(tid)}
    if action == "config":
        cfg = p.get("config") or {}
        if not isinstance(cfg, dict):
            raise ValueError("config must be an object")
        merged = store.load_config()
        merged.update(cfg)
        return {"success": True, "config": store.save_config(merged)}
    if action == "get_config":
        return {"success": True, "config": store.load_config()}
    raise ValueError(f"unknown action {action!r}")
