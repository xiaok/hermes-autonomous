#!/usr/bin/env python3
"""Cron monitor gate (copy this into ~/.hermes/scripts/).

Resolves the plugin wherever it is installed and runs its wake.py.
"""

import os
import runpy
import sys
from pathlib import Path

home = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
candidates = [
    home / "plugins" / "hermes-autonomous" / "scripts" / "wake.py",
    Path.home() / "opt" / "hermes-autonomous" / "scripts" / "wake.py",
    Path(__file__).resolve().parent.parent / "scripts" / "wake.py",
]
target = next((p for p in candidates if p.exists()), None)
if target is None:
    print("quiet")          # nothing installed: stay silent, never crash the scheduler
    sys.exit(0)

sys.argv = [str(target)]
runpy.run_path(str(target), run_name="__main__")
