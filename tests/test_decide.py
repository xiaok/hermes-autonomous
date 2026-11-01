"""Unit tests for scripts/decide.py apply_gates (pure function, no model call).

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from decide import apply_gates  # noqa: E402

TZ = timezone(timedelta(hours=8))
DAY = datetime(2026, 11, 1, 14, 0, tzinfo=TZ)
NIGHT = datetime(2026, 11, 1, 23, 30, tzinfo=TZ)
EARLY = datetime(2026, 11, 1, 7, 59, tzinfo=TZ)


def recent(now: datetime, minutes: int = 30) -> dict:
    return {"last_send_at": (now - timedelta(minutes=minutes)).isoformat()}


class ApplyGatesTest(unittest.TestCase):
    def assertBlocked(self, out: dict) -> None:
        self.assertIsNone(out["message"])
        self.assertFalse(out["gates"]["sent"])
        self.assertTrue(any("闸门拦下" in s for s in out.get("silent_actions", [])))

    def test_night_blocks(self):
        for now in (NIGHT, EARLY):
            with self.subTest(now=now):
                out = apply_gates({"message": "晚上好"}, {}, now)
                self.assertBlocked(out)
                self.assertTrue(out["gates"]["night"])
                self.assertFalse(out["gates"]["cooldown"])

    def test_cooldown_blocks(self):
        out = apply_gates({"message": "跟进一下"}, recent(DAY), DAY)
        self.assertBlocked(out)
        self.assertFalse(out["gates"]["night"])
        self.assertTrue(out["gates"]["cooldown"])

    def test_night_and_cooldown_block(self):
        out = apply_gates({"message": "跟进一下"}, recent(NIGHT), NIGHT)
        self.assertBlocked(out)
        self.assertTrue(out["gates"]["night"])
        self.assertTrue(out["gates"]["cooldown"])

    def test_blocked_long_message_not_resurrected_by_trim(self):
        msg = "\n".join(f"第{i}行" for i in range(10))
        out = apply_gates({"message": msg}, {}, NIGHT)
        self.assertBlocked(out)
        self.assertNotIn("trimmed_to_6_lines", out["gates"])

    def test_daytime_passes(self):
        out = apply_gates({"message": "下午好"}, {}, DAY)
        self.assertEqual(out["message"], "下午好")
        self.assertTrue(out["gates"]["sent"])
        self.assertFalse(out["gates"]["night"])
        self.assertFalse(out["gates"]["cooldown"])
        self.assertNotIn("silent_actions", out)

    def test_cooldown_expired_passes(self):
        out = apply_gates({"message": "下午好"}, recent(DAY, minutes=91), DAY)
        self.assertEqual(out["message"], "下午好")
        self.assertTrue(out["gates"]["sent"])

    def test_no_message(self):
        out = apply_gates({"message": None}, {}, DAY)
        self.assertIsNone(out["message"])
        self.assertFalse(out["gates"]["sent"])

    def test_trim_to_3_items(self):
        msg = "\n".join(f"事项{i}：内容{i}" for i in range(1, 6))
        out = apply_gates({"message": msg}, {}, DAY)
        self.assertTrue(out["gates"]["trimmed_to_3_items"])
        self.assertEqual(out["message"].splitlines(),
                         ["事项1：内容1", "事项2：内容2", "事项3：内容3"])
        self.assertTrue(out["gates"]["sent"])

    def test_trim_to_6_lines(self):
        msg = "\n".join(f"第{i}行" for i in range(10))
        out = apply_gates({"message": msg}, {}, DAY)
        self.assertTrue(out["gates"]["trimmed_to_6_lines"])
        self.assertEqual(len(out["message"].splitlines()), 6)
        self.assertTrue(out["gates"]["sent"])


if __name__ == "__main__":
    unittest.main()
