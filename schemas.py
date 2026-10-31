"""Tool schemas exposed to the model."""

from __future__ import annotations

TOOL_ENQUEUE = {
    "name": "autonomy_enqueue",
    "description": (
        "Add a task to the autonomous work queue. The tick runner claims queued tasks and "
        "executes them in a fresh headless Hermes session without the user present. Use for "
        "work that can finish unattended: research, monitoring setup, refactors, data gathering. "
        "Give a self-contained prompt (the runner has none of this conversation) and an "
        "acceptance check in 'verify' so success is judgeable."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Short imperative title, < 60 chars."},
            "prompt": {"type": "string", "description": "Full self-contained instructions for a fresh session."},
            "verify": {"type": "string", "description": "How to tell it succeeded (command to run or check to make)."},
            "priority": {"type": "integer", "description": "1 = highest, 9 = lowest. Default 5."},
            "tags": {"type": "string", "description": "Comma-separated tags for grouping."},
            "max_attempts": {"type": "integer", "description": "Retries before it stays failed. Default 2."},
            "needs_approval": {"type": "boolean", "description": "Queue but do not run until approved."},
        },
        "required": ["title", "prompt"],
    },
}

TOOL_LIST = {
    "name": "autonomy_list",
    "description": "List autonomous queue tasks by status (queued/running/done/failed/cancelled/blocked/all).",
    "parameters": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "description": "Filter. Default 'queued'."},
            "limit": {"type": "integer", "description": "Max rows. Default 20."},
        },
    },
}

TOOL_CLAIM = {
    "name": "autonomy_claim",
    "description": (
        "Claim the next queued task (marks it running) so it can be executed now in this session "
        "instead of waiting for the tick runner. Returns the task, or null when the queue is empty."
    ),
    "parameters": {"type": "object", "properties": {}},
}

TOOL_FINISH = {
    "name": "autonomy_finish",
    "description": "Close a task claimed with autonomy_claim: status done/failed/blocked/cancelled plus a short result.",
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "status": {"type": "string", "enum": ["done", "failed", "blocked", "cancelled"]},
            "result": {"type": "string", "description": "What was produced / where it landed."},
            "error": {"type": "string", "description": "Why it failed, when status=failed."},
        },
        "required": ["task_id", "status"],
    },
}

TOOL_STATUS = {
    "name": "autonomy_status",
    "description": "Queue health: counts by status, runs and failures today, config, last run, recent results.",
    "parameters": {"type": "object", "properties": {}},
}

TOOL_CONTROL = {
    "name": "autonomy_control",
    "description": (
        "Control the autonomous loop: pause/resume (kill switch), approve a task queued with "
        "needs_approval, cancel a queued task, requeue a failed one, or set config "
        "(max_runs_per_day, max_runtime_seconds, quiet_hours, model, enabled)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["pause", "resume", "cancel", "requeue", "config", "get_config"]},
            "task_id": {"type": "string", "description": "Required for approve/cancel/requeue."},
            "config": {"type": "object", "description": "Key/value pairs for action=config."},
        },
        "required": ["action"],
    },
}

ALL = [TOOL_ENQUEUE, TOOL_LIST, TOOL_CLAIM, TOOL_FINISH, TOOL_STATUS, TOOL_CONTROL]
