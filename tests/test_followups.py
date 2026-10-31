"""End-to-end tests for scripts/followups.py and scripts/wake.py.

Every test gets its own throwaway HERMES_HOME and drives the real CLI through a
subprocess, the same way cron does. Standard library only:

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FOLLOWUPS = ROOT / "scripts" / "followups.py"
WAKE = ROOT / "scripts" / "wake.py"
OWNER = "4242"


def daytime_tz() -> str:
    """A fixed-offset zone where it is mid-day right now, so night rules never
    flip a test depending on when it runs."""
    from zoneinfo import ZoneInfo
    for off in range(-12, 15):
        name = f"Etc/GMT{-off:+d}"  # POSIX sign: Etc/GMT-3 is UTC+3
        if 10 <= datetime.now(ZoneInfo(name)).hour <= 20:
            return name
    raise RuntimeError("no daytime zone found")


class Home(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="hermes-home-"))
        self.addCleanup(shutil.rmtree, self.home, True)
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith("AUTONOMY_") and k != "HERMES_HOME"}
        self.env["HERMES_HOME"] = str(self.home)
        self.tz = daytime_tz()

    # -- helpers ---------------------------------------------------------------

    def run_cli(self, script: Path, *args: str, stdin: str | None = None):
        return subprocess.run([sys.executable, str(script), *args], input=stdin,
                              capture_output=True, text=True, env=self.env, timeout=30)

    def fu(self, *args: str, stdin: str | None = None, ok: bool = True):
        p = self.run_cli(FOLLOWUPS, *args, stdin=stdin)
        if ok:
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return p

    def wake(self) -> str:
        p = self.run_cli(WAKE)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return p.stdout.strip()

    def setup_owner(self):
        self.fu("setup", "--owner-id", OWNER, "--dm-target", f"telegram:{OWNER}",
                "--timezone", self.tz)

    def apply(self, doc: dict, ok: bool = True):
        p = self.fu("apply", stdin=json.dumps(doc), ok=ok)
        out = json.loads(p.stdout)
        if not ok:
            self.assertNotEqual(p.returncode, 0, p.stdout)
            self.assertFalse(out["ok"])
        return out

    def add(self, key: str, why: str = "care", revisit: str = "-1h") -> int:
        # Aware ISO: a naive one would be read in the configured zone, not ours.
        at = datetime.fromtimestamp(time.time() + int(revisit[:-1]) * 3600,
                                    timezone.utc).isoformat()
        out = json.loads(self.fu("add", "--key", key, "--kind", "k", "--why", why,
                                 "--subject", key, "--revisit-at", at).stdout)
        return out["id"]

    def db(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.home / "plugin-data" / "hermes-autonomous" / "followups.db")
        c.row_factory = sqlite3.Row
        self.addCleanup(c.close)
        return c

    def row(self, fid: int) -> sqlite3.Row:
        return self.db().execute("SELECT * FROM followups WHERE id=?", (fid,)).fetchone()

    def snapshot(self) -> dict:
        c = self.db()
        return {t: sorted(tuple(r) for r in c.execute(f"SELECT * FROM {t}"))
                for t in ("followups", "watermark", "kv", "log")}

    def state_db(self, messages: list[tuple]) -> None:
        """messages: (timestamp, role, content, user_id)."""
        c = sqlite3.connect(self.home / "state.db")
        c.executescript(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, chat_type TEXT,"
            " title TEXT, user_id TEXT);"
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT,"
            " timestamp REAL, role TEXT, content TEXT);")
        for uid in {m[3] for m in messages}:
            c.execute("INSERT INTO sessions VALUES(?,?,?,?,?)",
                      (f"s-{uid}", "telegram", "dm", "t", uid))
        c.executemany("INSERT INTO messages(session_id,timestamp,role,content) VALUES(?,?,?,?)",
                      [(f"s-{m[3]}", m[0], m[1], m[2]) for m in messages])
        c.commit()
        c.close()


class FreshInstall(Home):
    def test_setup_then_wake_and_digest(self):
        self.assertEqual(self.wake(), '{"mode": "setup-needed"}')
        self.setup_owner()
        self.assertEqual(self.wake(), "quiet")

        d = json.loads(self.fu("digest").stdout)
        for k in ("now", "watermark", "next_watermark", "messages", "followups",
                  "due_count", "mode", "night", "muted"):
            self.assertIn(k, d)
        self.assertEqual(d["messages"], [])
        self.assertEqual(d["mode"], "capture")
        self.assertIs(d["night"], False)
        self.assertEqual(d["muted"], {})
        # No messages: next_watermark is the current (default) watermark, as a raw float.
        self.assertIsInstance(d["next_watermark"], float)
        self.assertAlmostEqual(d["next_watermark"], time.time() - 12 * 3600, delta=60)

    def test_digest_next_watermark_is_last_message_raw_ts(self):
        self.setup_owner()
        wm = 1761800000.5
        self.apply({"watermark": wm})
        self.state_db([
            (wm - 10, "user", "before the cursor", OWNER),
            (wm + 1.25, "user", "hi", OWNER),
            (wm + 7.987654321, "assistant", "hello", OWNER),
            (wm + 99, "user", "someone else", "9999"),
        ])
        d = json.loads(self.fu("digest").stdout)
        self.assertEqual([m["text"] for m in d["messages"]], ["hi", "hello"])
        self.assertEqual(d["next_watermark"], wm + 7.987654321)

        # Round trip: feeding next_watermark back stores it bit-for-bit.
        self.apply({"watermark": d["next_watermark"]})
        d2 = json.loads(self.fu("digest").stdout)
        self.assertEqual(d2["messages"], [])
        self.assertEqual(d2["next_watermark"], wm + 7.987654321)


class Apply(Home):
    def setUp(self):
        super().setUp()
        self.setup_owner()

    def test_full_apply_writes_everything(self):
        quiet = self.add("checkup", why="care")
        tell = self.add("trip", why="utility")
        wm = 1761800123.456789
        doc = {
            "new_followups": [{"key": "throat-2026-10-30", "why": "care", "kind": "health",
                               "subject": "喉咙痛", "quote": "嗓子疼", "hint": "问好点没",
                               "revisit_at": "+12h"}],
            "decisions": [{"id": quiet, "action": "silent", "reason": "no news"},
                          {"id": tell, "action": "act_report", "reason": "rain"}],
            "sent": {"id": tell, "msg_id": "2311"},
            "watermark": wm,
        }
        t0 = time.time()
        out = self.apply(doc)
        self.assertTrue(out["ok"])

        c = self.db()
        new = c.execute("SELECT * FROM followups WHERE key='throat-2026-10-30'").fetchone()
        self.assertEqual((new["why"], new["subject"], new["quote"], new["hint"]),
                         ("care", "喉咙痛", "嗓子疼", "问好点没"))
        self.assertAlmostEqual(new["revisit_at"], t0 + 12 * 3600, delta=30)

        q = self.row(quiet)  # silent → checked with backoff (checks was 0 → +1d)
        self.assertEqual(q["checks"], 1)
        self.assertIsNotNone(q["last_checked"])
        self.assertAlmostEqual(q["revisit_at"], t0 + 86400, delta=30)
        self.assertIsNone(q["sent_at"])
        self.assertEqual(q["status"], "active")

        s = self.row(tell)  # sent receipt; act_report is not auto-closed
        self.assertEqual(s["sent_msg_id"], "2311")
        self.assertEqual(s["checks"], 1)
        self.assertAlmostEqual(s["sent_at"], t0, delta=30)
        self.assertAlmostEqual(s["revisit_at"], s["sent_at"] + 86400, delta=0.001)
        self.assertEqual(s["status"], "active")

        kv = {r["k"]: float(r["v"]) for r in c.execute("SELECT * FROM kv")}
        self.assertAlmostEqual(kv["last_send"], s["sent_at"], delta=0.001)
        self.assertIn("last_capture", kv)

        got = c.execute("SELECT last_ts FROM watermark WHERE surface='messages'").fetchone()[0]
        self.assertEqual(got, wm)  # exact, not minute-rounded

        logs = c.execute("SELECT * FROM log").fetchall()
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["kind"], "apply")
        self.assertEqual(json.loads(logs[0]["payload"]), doc)
        self.assertAlmostEqual(logs[0]["run_at"], t0, delta=30)

        # A sent item is no longer due, so the wake gate stays quiet (cooldown too).
        self.assertEqual(self.wake(), "quiet")

    def test_silent_backoff_and_snooze(self):
        fid = self.add("x")
        self.db().execute("UPDATE followups SET checks=2 WHERE id=?", (fid,)).connection.commit()
        t0 = time.time()
        self.apply({"decisions": [{"id": fid, "action": "silent", "reason": "r"}],
                    "watermark": 1.0})
        self.assertAlmostEqual(self.row(fid)["revisit_at"], t0 + 4 * 86400, delta=30)
        self.apply({"decisions": [{"id": fid, "action": "silent", "reason": "r",
                                   "snooze": "+2h"}], "watermark": 1.0})
        self.assertAlmostEqual(self.row(fid)["revisit_at"], t0 + 2 * 3600, delta=30)
        self.assertEqual(self.row(fid)["checks"], 4)

    def test_sent_ids_covers_several_items_in_one_message(self):
        a, b = self.add("a"), self.add("b")
        self.apply({"sent": {"ids": [a, b], "msg_id": 77}, "watermark": 1.0})
        self.assertEqual([self.row(i)["sent_msg_id"] for i in (a, b)], ["77", "77"])

    def test_upsert_keeps_delivery_state(self):
        fid = self.add("trip")
        self.apply({"sent": {"id": fid, "msg_id": "9"}, "watermark": 1.0,
                    "outcomes": [{"id": fid, "value": "ignored"}]})
        before = self.row(fid)
        self.apply({"new_followups": [{"key": "trip", "why": "risk", "kind": "k",
                                       "subject": "trip v2", "revisit_at": "+3d"}],
                    "watermark": 1.0})
        after = self.row(fid)
        self.assertEqual((after["subject"], after["why"]), ("trip v2", "risk"))
        for col in ("sent_at", "sent_msg_id", "checks", "outcome", "status", "created_at"):
            self.assertEqual(after[col], before[col], col)

    def test_outcomes_and_close(self):
        a, b = self.add("a"), self.add("b")
        self.apply({"outcomes": [{"id": a, "value": "accepted", "close": True}],
                    "close": [{"id": b, "note": "done"}], "watermark": 1.0})
        self.assertEqual((self.row(a)["outcome"], self.row(a)["status"]), ("accepted", "closed"))
        self.assertEqual((self.row(b)["status"], self.row(b)["note"]), ("closed", "done"))


class ApplyRollback(Home):
    """Each bad document must leave the DB byte-for-byte as it was."""

    def setUp(self):
        super().setUp()
        self.setup_owner()
        self.fid = self.add("existing")
        self.apply({"watermark": 1000.0})
        self.before = self.snapshot()

    def assert_rejected(self, doc: dict, needle: str):
        out = self.apply(doc, ok=False)
        self.assertIn(needle, out["error"])
        self.assertEqual(self.snapshot(), self.before)

    def good(self, **over) -> dict:
        doc = {
            "new_followups": [{"key": "new-one", "why": "care", "kind": "k",
                               "subject": "s", "revisit_at": "+1d"}],
            "decisions": [{"id": self.fid, "action": "silent", "reason": "r"}],
            "sent": None,
            "watermark": 2000.0,
        }
        doc.update(over)
        return doc

    def test_unknown_decision_id_rolls_back_earlier_upsert(self):
        self.assert_rejected(self.good(decisions=[
            {"id": self.fid, "action": "silent", "reason": "r"},
            {"id": 999, "action": "silent", "reason": "r"}]), "no followup 999")

    def test_failure_after_every_other_write_rolls_back(self):
        # upsert, silent check, and sent all run before the watermark is validated.
        doc = self.good(decisions=[], sent={"id": self.fid, "msg_id": "1"}, watermark="soon")
        self.assert_rejected(doc, "watermark")

    def test_missing_key(self):
        nf = [{"why": "care", "kind": "k", "subject": "s", "revisit_at": "+1d"}]
        self.assert_rejected(self.good(new_followups=nf), "missing key")

    def test_missing_why(self):
        nf = [{"key": "k1", "why": "care", "kind": "k", "subject": "s", "revisit_at": "+1d"},
              {"key": "k2", "why": "", "kind": "k", "subject": "s", "revisit_at": "+1d"}]
        self.assert_rejected(self.good(new_followups=nf), "new_followups[1]: missing why")

    def test_bad_action(self):
        self.assert_rejected(self.good(decisions=[{"id": self.fid, "action": "close"}]), "action")

    def test_sent_without_msg_id(self):
        self.assert_rejected(self.good(decisions=[], sent={"id": self.fid, "msg_id": ""}),
                             "msg_id")

    def test_sent_unknown_id(self):
        self.assert_rejected(self.good(sent={"id": 555, "msg_id": "1"}), "no followup 555")

    def test_silent_and_sent_conflict(self):
        self.assert_rejected(self.good(sent={"id": self.fid, "msg_id": "1"}), "silent but also sent")

    def test_missing_watermark(self):
        doc = self.good()
        del doc["watermark"]
        self.assert_rejected(doc, "watermark")

    def test_future_watermark(self):
        self.assert_rejected(self.good(watermark=time.time() + 3600), "future")

    def test_unknown_top_level_field(self):
        self.assert_rejected(self.good(new_followup=[]), "unknown field")

    def test_bad_revisit_at(self):
        nf = [{"key": "k", "why": "care", "kind": "k", "subject": "s", "revisit_at": "tomorrow"}]
        self.assert_rejected(self.good(new_followups=nf), "bad time")

    def test_not_json(self):
        p = self.fu("apply", stdin="{nope", ok=False)
        self.assertEqual(p.returncode, 2)
        self.assertIn("cannot read JSON", json.loads(p.stdout)["error"])
        self.assertEqual(self.snapshot(), self.before)

    def test_file_flag(self):
        f = self.home / "run.json"
        f.write_text(json.dumps({"watermark": 3000.5}), encoding="utf-8")
        self.fu("apply", "--file", str(f))
        got = self.db().execute("SELECT last_ts FROM watermark").fetchone()[0]
        self.assertEqual(got, 3000.5)


class WakeMuted(Home):
    def setUp(self):
        super().setUp()
        self.setup_owner()

    def past(self, why: str, outcome: str, days_ago: float = 1) -> None:
        """A followup that was sent and got this reaction."""
        ts = time.time() - days_ago * 86400
        c = self.db()
        c.execute("INSERT INTO followups(key,kind,why,subject,status,sent_at,revisit_at,outcome)"
                  " VALUES(?,?,?,?,?,?,?,?)",
                  (f"{why}-{outcome}-{ts}", "k", why, "s", "closed", ts, ts + 86400, outcome))
        c.commit()

    def woke_ids(self) -> list[int]:
        out = self.wake()
        return [] if out == "quiet" else json.loads(out)["ids"]

    def test_annoyed_mutes_its_category_only(self):
        care, util = self.add("care-due", "care"), self.add("util-due", "utility")
        self.assertEqual(self.woke_ids(), [care, util])
        self.past("care", "annoyed")
        self.assertEqual(self.woke_ids(), [util])
        muted = json.loads(self.fu("digest").stdout)["muted"]
        self.assertEqual(muted, {"care": "annoyed"})

    def test_three_ignored_mutes_two_do_not(self):
        risk = self.add("risk-due", "risk")
        self.past("risk", "ignored")
        self.past("risk", "ignored")
        self.assertEqual(self.woke_ids(), [risk])
        self.past("risk", "ignored")
        self.assertEqual(self.wake(), "quiet")
        self.assertEqual(json.loads(self.fu("digest").stdout)["muted"], {"risk": "ignored*3"})

    def test_feedback_older_than_30_days_expires(self):
        care = self.add("care-due", "care")
        self.past("care", "annoyed", days_ago=31)
        self.assertEqual(self.woke_ids(), [care])

    def test_all_muted_is_quiet(self):
        self.add("care-due", "care")
        self.past("care", "annoyed")
        self.assertEqual(self.wake(), "quiet")


if __name__ == "__main__":
    unittest.main()
