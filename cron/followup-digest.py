#!/usr/bin/env python3
"""Cron context script (copy this into ~/.hermes/scripts/).

Runs the follow-up digest from wherever the plugin is installed.
"""

import os
import runpy
import sys
from pathlib import Path

home = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
candidates = [
    home / "plugins" / "hermes-autonomous" / "scripts" / "followups.py",
    Path.home() / "opt" / "hermes-autonomous" / "scripts" / "followups.py",
    Path(__file__).resolve().parent.parent / "scripts" / "followups.py",
]
target = next((p for p in candidates if p.exists()), None)
if target is None:
    sys.exit(0)

sys.argv = [str(target), "digest"]
runpy.run_path(str(target), run_name="__main__")
