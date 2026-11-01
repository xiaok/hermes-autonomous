#!/usr/bin/env python3
"""Offline decider: feed a scenario, get the follow-up judgment.

Deterministic gates (night / cooldown / due / caps) live here in code; the
judgement itself is one LLM call with no tools. This is the same rubric the
cron review uses, extracted so scenarios can be evaluated without touching the
live board.

    python3 scripts/decide.py --scenario scenarios/example.json
    python3 scripts/decide.py --scenario s.json --json     # raw result only
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

def local_tz():
    name = os.environ.get("AUTONOMY_TZ") or ""
    if not name:
        try:
            p = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")) / \
                "plugin-data" / "hermes-autonomous" / "settings.json"
            name = json.loads(p.read_text(encoding="utf-8")).get("timezone") or ""
        except Exception:
            name = ""
    if name:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(str(name))
        except Exception:
            pass
    return datetime.now().astimezone().tzinfo or timezone.utc
ROOT = Path(__file__).resolve().parent.parent
CHAT = shutil.which("hermes") or str(Path.home() / ".hermes/hermes-agent/.hermes/bin/hermes")


def parse_now(s: str | None) -> datetime:
    if not s:
        return datetime.now(tz=local_tz())
    if s.lower() == "now":
        return datetime.now(tz=local_tz())
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=local_tz())


def parse_when(s: str, now: datetime) -> datetime:
    s = (s or "").strip()
    m = re.fullmatch(r"\+(\d+)([hmd])", s)
    if m:
        n = int(m.group(1))
        unit = {"h": "hours", "m": "minutes", "d": "days"}[m.group(2)]
        return now + timedelta(**{unit: n})
    if s.lower() == "now":
        return now
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=local_tz())


def build_prompt(scenario: dict, now: datetime) -> str:
    rubric = (ROOT / "prompts/decide.md").read_text(encoding="utf-8")
    lines = [rubric, "", "## 场景", "", f"现在：{now.isoformat(timespec='minutes')}", ""]
    lines.append("### 对话")
    for m in scenario.get("history", []):
        role = "他" if m.get("role") == "user" else "你"
        lines.append(f"- [{m.get('t','')}] {role}：{m.get('text','').strip()}")
    lines.append("")
    lines.append("### 现行跟进点")
    fs = scenario.get("followups", [])
    if not fs:
        lines.append("（无）")
    for f in fs:
        lines.append(
            f"- id={f.get('id')} why={f.get('why')} subject={f.get('subject')} "
            f"quote={f.get('quote','')!r} revisit_at={f.get('revisit_at')} "
            f"sent_at={f.get('sent_at')} checks={f.get('checks',0)}"
        )
    lines.append("")
    lines.append("现在给出 JSON：")
    return "\n".join(lines)


def call_llm(prompt: str, model: str = "") -> str:
    # -z prints only the final response text (no banner, no prompt echo)
    cmd = [CHAT, "-z", prompt, "-t", ""]
    if model:
        cmd += ["-m", model]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300,
                          cwd=str(Path.home()))
    if proc.returncode != 0 and not proc.stdout.strip():
        raise RuntimeError((proc.stderr or f"exit {proc.returncode}")[-2000:])
    return proc.stdout


def extract_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError(f"no JSON in output: {text[:300]}")
    return json.loads(text[start:end + 1])


def apply_gates(result: dict, scenario: dict, now: datetime) -> dict:
    """Code-enforced caps: night, cooldown, <=3 items, <=6 lines."""
    night = now.hour < 8 or now.hour >= 23
    last_send = scenario.get("last_send_at")
    cooling = False
    if last_send:
        cooling = (now - parse_when(last_send, now)).total_seconds() < 90 * 60

    gates = {"night": night, "cooldown": cooling}
    msg = result.get("message")
    if msg and (night or cooling):
        result.setdefault("silent_actions", []).append(
            f"消息被闸门拦下（{'夜间' if night else '冷却中'}）：{msg[:60]}…")
        result["message"] = None
        msg = None
    if msg:
        blocks = [b for b in re.split(r"\n(?=\S{0,2}?\S*[：:])", msg.strip()) if b.strip()]
        out = "\n".join(blocks[:3]) if len(blocks) > 3 else msg
        if len(blocks) > 3:
            gates["trimmed_to_3_items"] = True
        lines = out.splitlines()
        if len(lines) > 6:
            out = "\n".join(lines[:6])
            gates["trimmed_to_6_lines"] = True
        result["message"] = out
    gates["sent"] = bool(result.get("message"))
    result["gates"] = gates
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", required=True)
    ap.add_argument("--model", default="")
    ap.add_argument("--json", action="store_true", help="raw JSON result only")
    args = ap.parse_args()

    scenario = json.loads(Path(args.scenario).read_text(encoding="utf-8"))
    now = parse_now(scenario.get("now"))
    prompt = build_prompt(scenario, now)

    try:
        raw = call_llm(prompt, args.model)
        result = extract_json(raw)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1

    result = apply_gates(result, scenario, now)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    print(f"新跟进点：{len(result.get('new_followups') or [])}")
    for f in result.get("new_followups") or []:
        print(f"  + [{f.get('why')}] {f.get('subject')} → {f.get('revisit_at')}")
    print("判断：")
    for d in result.get("decisions") or []:
        print(f"  #{d.get('id')} {d.get('action'):<10} {d.get('reason','')}")
    print("静默动作：" + ("；".join(result.get("silent_actions") or []) or "无"))
    print("\n消息：\n" + (result.get("message") or "（无，静默）"))
    print(f"\n闸门：{result.get('gates')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
