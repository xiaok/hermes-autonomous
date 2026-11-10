"""End-to-end tests for scripts/followups.py and scripts/wake.py.

Every test gets its own throwaway HERMES_HOME and drives the real CLI through a
subprocess, the same way cron does. Standard library only:

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
FOLLOWUPS = ROOT / "scripts" / "followups.py"
WAKE = ROOT / "scripts" / "wake.py"
OWNER = "4242"
CJK = re.compile(r"[一-鿿]")
WM0 = time.time() - 3600  # a realistic cursor: recent enough for the fresh-board floor

# Stand-in for the `hermes` CLI: `cron list [--json]` over a jobs.json next to it
# (only reached when doctor cannot read the real store), `cron edit`, which edits
# the real store $HERMES_HOME/cron/jobs.json when there is one (else the jobs.json
# next to it), and `send`, which answers with send_reply.json (default: a
# delivered message) and logs its stdin body to sends.jsonl; every call is
# appended to calls.jsonl.
FAKE_HERMES = '''#!{python}
import json, os, sys
from pathlib import Path
here = Path(__file__).resolve().parent
args = sys.argv[1:]
with open(here / "calls.jsonl", "a") as f:
    f.write(json.dumps(args) + "\\n")
if args[:1] == ["send"]:
    body = sys.stdin.read() if args[-2:] == ["--file", "-"] else None
    with open(here / "sends.jsonl", "a") as f:
        f.write(json.dumps({{"args": args, "body": body}}) + "\\n")
    reply = here / "send_reply.json"
    doc = json.loads(reply.read_text()) if reply.exists() else {{
        "success": True, "message_id": "m-1", "mirrored": True}}
    print(json.dumps(doc, indent=2))
    sys.exit(0 if doc.get("success") and not doc.get("error") else 1)
jobs = json.loads((here / "jobs.json").read_text())
store = Path(os.environ["HERMES_HOME"]) / "cron" / "jobs.json"
if args[:2] == ["cron", "list"]:
    print(json.dumps(jobs) if "--json" in args else "(text)")
elif args[:2] == ["cron", "edit"]:
    doc = json.loads(store.read_text()) if store.exists() else None
    target = doc["jobs"] if doc is not None else jobs
    job = next(j for j in target if j["id"] == args[2])
    rest = args[3:]
    for flag, val in zip(rest[::2], rest[1::2]):
        job[flag[2:].replace("-", "_")] = val
    if doc is not None:
        store.write_text(json.dumps(doc))
    else:
        (here / "jobs.json").write_text(json.dumps(jobs))
else:
    sys.exit(2)
'''


def daytime_tz() -> str:
    """A fixed-offset zone where it is mid-day right now, so night rules never
    flip a test depending on when it runs."""
    from zoneinfo import ZoneInfo
    for off in range(-12, 15):
        name = f"Etc/GMT{-off:+d}"  # POSIX sign: Etc/GMT-3 is UTC+3
        if 10 <= datetime.now(ZoneInfo(name)).hour <= 20:
            return name
    raise RuntimeError("no daytime zone found")


def tz_at_hour(hour: int) -> str:
    """A fixed-offset zone whose local hour is ``hour`` right now. Etc/GMT-14..+12
    covers all 24 hours, so this never depends on when the test runs."""
    from zoneinfo import ZoneInfo
    for off in range(-12, 15):
        name = f"Etc/GMT{-off:+d}"
        if datetime.now(ZoneInfo(name)).hour == hour:
            return name
    raise RuntimeError(f"no zone at hour {hour}")


def local_hour(tz: str) -> int:
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo(tz)).hour


class Home(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="hermes-home-"))
        self.addCleanup(shutil.rmtree, self.home, True)
        # The suite may itself run inside a Hermes chat session: its HERMES_SESSION_*
        # would make /followups think a (non-owner) chat user is calling.
        session = mock.patch.dict(os.environ, {k: v for k, v in os.environ.items()
                                               if not k.startswith("HERMES_SESSION_")}, clear=True)
        session.start()
        self.addCleanup(session.stop)
        gateway = mock.patch.dict(sys.modules, {"gateway": None, "gateway.session_context": None})
        gateway.start()
        self.addCleanup(gateway.stop)
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith("AUTONOMY_") and k != "HERMES_HOME"}
        self.env["HERMES_HOME"] = str(self.home)
        self.tz = daytime_tz()
        # setup runs doctor, which shells out to `hermes`: never reach the real one.
        self.bin = self.home / "fakebin"
        self.bin.mkdir()
        (self.bin / "hermes").write_text(FAKE_HERMES.format(python=sys.executable))
        (self.bin / "hermes").chmod(0o755)
        Home.set_jobs(self, [])  # the fake CLI's list; Doctor overrides set_jobs for the store
        self.env["PATH"] = f"{self.bin}{os.pathsep}{self.env.get('PATH', '')}"

    # -- helpers ---------------------------------------------------------------

    def set_jobs(self, jobs: list[dict]):
        (self.bin / "jobs.json").write_text(json.dumps(jobs))

    def hermes_calls(self) -> list[list[str]]:
        p = self.bin / "calls.jsonl"
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []

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
        """messages: (timestamp, role, content, user_id[, chat_type='dm'])."""
        messages = [m if len(m) == 5 else (*m, "dm") for m in messages]
        c = sqlite3.connect(self.home / "state.db")
        c.executescript(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, chat_type TEXT,"
            " title TEXT, user_id TEXT);"
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT,"
            " timestamp REAL, role TEXT, content TEXT);")
        for uid, ct in {(m[3], m[4]) for m in messages}:
            c.execute("INSERT INTO sessions VALUES(?,?,?,?,?)",
                      (f"s-{uid}-{ct}", "telegram", ct, "t", uid))
        c.executemany("INSERT INTO messages(session_id,timestamp,role,content) VALUES(?,?,?,?)",
                      [(f"s-{m[3]}-{m[4]}", m[0], m[1], m[2]) for m in messages])
        c.commit()
        c.close()


class FreshInstall(Home):
    def test_setup_then_wake_and_digest(self):
        self.assertEqual(self.wake(), '{"mode": "setup-needed"}')
        self.setup_owner()
        self.assertEqual(self.wake(), "quiet")

        d = json.loads(self.fu("digest").stdout)
        for k in ("now", "watermark", "next_watermark", "messages", "followups",
                  "due_count", "mode", "night", "muted", "dev_log"):
            self.assertIn(k, d)
        self.assertEqual(d["messages"], [])
        self.assertEqual(d["mode"], "quiet")  # no messages, nothing due
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

    def test_cron_digest_wrapper(self):
        # runpy.run_path does not put scripts/ on sys.path: followups.py must not
        # import its siblings. Install this checkout where the wrapper looks first.
        self.setup_owner()
        plugins = self.home / "plugins"
        plugins.mkdir()
        (plugins / "hermes-autonomous").symlink_to(ROOT, target_is_directory=True)
        env = {k: v for k, v in self.env.items() if k != "PYTHONPATH"}
        p = subprocess.run([sys.executable, str(ROOT / "cron" / "followup-digest.py")],
                           capture_output=True, text=True, env=env, cwd=self.home, timeout=30)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(json.loads(p.stdout)["mode"], "quiet")


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

    def test_decision_score_is_logged_verbatim(self):
        fid = self.add("x")
        score = {"relevance": 2, "change": 1, "action": 1, "timing": 2, "risk": 0}
        doc = {"decisions": [{"id": fid, "action": "silent", "reason": "r", "score": score}],
               "watermark": 1.0}
        self.apply(doc)
        payload = self.db().execute("SELECT payload FROM log").fetchone()[0]
        self.assertEqual(json.loads(payload)["decisions"][0]["score"], score)

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
        self.apply({"watermark": WM0})
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

    def test_score_not_an_object(self):
        d = [{"id": self.fid, "action": "silent", "reason": "r", "score": 7}]
        self.assert_rejected(self.good(decisions=d), "score")

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
        wm = round(time.time(), 3)
        f.write_text(json.dumps({"watermark": wm}), encoding="utf-8")
        self.fu("apply", "--file", str(f))
        got = self.db().execute("SELECT last_ts FROM watermark").fetchone()[0]
        self.assertEqual(got, wm)


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
        if not out.startswith("review ids "):
            return []
        return [int(i) for i in out[len("review ids "):].split(",")]

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
        self.assertEqual(self.wake(), "held muted")
        self.assertEqual(json.loads(self.fu("digest").stdout)["muted"], {"risk": "ignored*3"})

    def test_feedback_older_than_30_days_expires(self):
        care = self.add("care-due", "care")
        self.past("care", "annoyed", days_ago=31)
        self.assertEqual(self.woke_ids(), [care])

    def test_all_muted_is_held_muted(self):
        self.add("care-due", "care")
        self.past("care", "annoyed")
        self.assertEqual(self.wake(), "held muted")


class DevLog(Home):
    """The run's final reply is delivered to a chat; the developer log in it is opt-in."""

    def settings_file(self) -> Path:
        return self.home / "plugin-data" / "hermes-autonomous" / "settings.json"

    def digest_dev_log(self):
        return json.loads(self.fu("digest").stdout)["dev_log"]

    def test_default_off(self):
        self.setup_owner()
        self.assertIs(json.loads(self.settings_file().read_text())["dev_log"], False)
        self.assertIs(self.digest_dev_log(), False)

    def test_setup_toggles(self):
        self.setup_owner()
        self.fu("setup", "--dev-log")
        self.assertIs(json.loads(self.settings_file().read_text())["dev_log"], True)
        self.assertIs(self.digest_dev_log(), True)
        # Re-running setup without the flag keeps it.
        self.fu("setup", "--timezone", self.tz)
        self.assertIs(self.digest_dev_log(), True)
        self.fu("setup", "--no-dev-log")
        self.assertIs(self.digest_dev_log(), False)

    def test_old_settings_without_key_is_off(self):
        self.setup_owner()
        s = json.loads(self.settings_file().read_text())
        del s["dev_log"]
        self.settings_file().write_text(json.dumps(s))
        self.assertIs(self.digest_dev_log(), False)

    def test_readme_cron_is_wake_gated_and_local(self):
        for name in ("README.md", "README.zh-CN.md"):
            with self.subTest(name=name):
                text = (ROOT / name).read_text(encoding="utf-8")
                cmd = text.split("hermes cron create", 1)[1].split("```", 1)[0]
                self.assertIn("--monitor-script followup-wake.py", cmd)
                self.assertIn("--deliver local --failure-deliver origin", cmd)
                self.assertNotIn("--deliver origin", cmd)


class IncludeGroups(Home):
    def setUp(self):
        super().setUp()
        self.setup_owner()
        self.wm = time.time() - 3600
        self.apply({"watermark": self.wm})
        self.state_db([
            (self.wm + 1, "user", "in my dm", OWNER, "dm"),
            (self.wm + 2, "user", "a group member said this", OWNER, "group"),
        ])

    def texts(self) -> list[str]:
        return [m["text"] for m in json.loads(self.fu("digest").stdout)["messages"]]

    def test_default_reads_dm_only(self):
        settings = json.loads((self.home / "plugin-data" / "hermes-autonomous"
                               / "settings.json").read_text())
        self.assertIs(settings["include_groups"], False)
        self.assertEqual(self.texts(), ["in my dm"])

    def test_setup_include_groups_reads_groups(self):
        self.fu("setup", "--include-groups")
        self.assertEqual(self.texts(), ["in my dm", "a group member said this"])
        self.fu("setup", "--no-include-groups")
        self.assertEqual(self.texts(), ["in my dm"])

    def add_session_message(self, source: str, chat_type: str, text: str, ts: float) -> None:
        """A thread / forum topic / channel is its own session row with its own chat_type."""
        sid = f"s-{chat_type}"
        c = sqlite3.connect(self.home / "state.db")
        c.execute("INSERT INTO sessions VALUES(?,?,?,'t',?)", (sid, source, chat_type, OWNER))
        c.execute("INSERT INTO messages(session_id,timestamp,role,content)"
                  " VALUES(?,?,'user',?)", (sid, ts, text))
        c.commit()
        c.close()

    def add_thread_message(self, text: str = "in a thread") -> None:
        """A Discord thread is its own session row: chat_type='thread'."""
        self.add_session_message("discord", "thread", text, self.wm + 3)

    def add_forum_and_channel_messages(self) -> None:
        """Telegram forum-group topics are chat_type='forum'; channels are 'channel'."""
        self.add_session_message("telegram", "forum", "in a forum topic", self.wm + 4)
        self.add_session_message("discord", "channel", "in a channel", self.wm + 5)

    def test_default_skips_forum_and_channel(self):
        self.add_forum_and_channel_messages()
        self.assertEqual(self.texts(), ["in my dm"])

    def test_include_groups_reads_forum_and_channel(self):
        """Forum topics and channels are multi-member too, so include_groups covers them."""
        self.add_forum_and_channel_messages()
        self.fu("setup", "--include-groups")
        self.assertEqual(self.texts(), ["in my dm", "a group member said this",
                                        "in a forum topic", "in a channel"])

    def test_wake_counts_forum_messages_when_enabled(self):
        """The wake gate must count exactly what digest reads — forum topics included."""
        c = self.db()
        c.execute("UPDATE kv SET v=? WHERE k='last_capture'", (str(time.time() - 3 * 3600),))
        c.commit()
        sc = sqlite3.connect(self.home / "state.db")
        sc.execute("DELETE FROM messages WHERE content IN ('in my dm','a group member said this')")
        sc.commit()
        sc.close()
        self.add_session_message("telegram", "forum", "in a forum topic", self.wm + 4)
        self.assertEqual(self.wake(), "quiet")  # forum topics are off by default
        self.fu("setup", "--include-groups")
        woke = self.wake()
        self.assertEqual(woke, "capture", f"a forum message did not wake the gate: {woke!r}")

    def test_include_groups_reads_threads_too(self):
        """Threads are group-like (several members can post), so include_groups covers them."""
        self.add_thread_message()
        self.assertEqual(self.texts(), ["in my dm"])
        self.fu("setup", "--include-groups")
        self.assertEqual(self.texts(), ["in my dm", "a group member said this", "in a thread"])

    def test_wake_counts_thread_messages_when_enabled(self):
        """The wake gate must count exactly what digest reads — threads included."""
        c = self.db()
        c.execute("UPDATE kv SET v=? WHERE k='last_capture'", (str(time.time() - 3 * 3600),))
        c.commit()
        sc = sqlite3.connect(self.home / "state.db")
        sc.execute("DELETE FROM messages WHERE content IN ('in my dm','a group member said this')")
        sc.commit()
        sc.close()
        self.add_thread_message()
        self.assertEqual(self.wake(), "quiet")  # groups and threads are off by default
        self.fu("setup", "--include-groups")
        woke = self.wake()
        self.assertEqual(woke, "capture", f"a thread message did not wake the gate: {woke!r}")

    def test_wake_counts_only_what_digest_reads(self):
        c = self.db()
        c.execute("UPDATE kv SET v=? WHERE k='last_capture'", (str(time.time() - 3 * 3600),))
        c.commit()
        sc = sqlite3.connect(self.home / "state.db")
        sc.execute("DELETE FROM messages WHERE content='in my dm'")
        sc.commit()
        sc.close()
        self.assertEqual(self.wake(), "quiet")  # only a group message: not read by default
        self.fu("setup", "--include-groups")
        self.assertEqual(self.wake(), "capture")


class CaptureCadence(Home):
    """Cadence follows kv.last_capture; the watermark is only the message cursor."""

    def setUp(self):
        super().setUp()
        self.setup_owner()
        self.now = time.time()

    def state(self, watermark: float, last_capture: float | None):
        c = self.db()
        c.execute("INSERT INTO watermark(surface,last_ts) VALUES('messages',?)", (watermark,))
        if last_capture is not None:
            c.execute("INSERT INTO kv(k,v) VALUES('last_capture',?)", (str(last_capture),))
        c.commit()
        self.state_db([(self.now - 30, "user", "new message", OWNER)])

    def test_fresh_watermark_old_capture_wakes(self):
        self.state(watermark=self.now - 60, last_capture=self.now - 3 * 3600)
        self.assertEqual(self.wake(), "capture")

    def test_old_watermark_recent_capture_stays_quiet(self):
        self.state(watermark=self.now - 5 * 3600, last_capture=self.now - 60)
        self.assertEqual(self.wake(), "quiet")

    def test_old_db_without_last_capture_falls_back_to_watermark(self):
        self.state(watermark=self.now - 3 * 3600, last_capture=None)
        self.assertEqual(self.wake(), "capture")

    def test_apply_does_not_rearm_capture(self):
        # apply moves the watermark to the last message but stamps last_capture = now.
        self.state_db([(self.now - 30, "user", "new message", OWNER)])
        self.apply({"watermark": self.now - 3 * 3600})
        self.assertEqual(self.wake(), "quiet")


class ReadOnlyCommands(Home):
    def setUp(self):
        super().setUp()
        self.setup_owner()

    def test_why_and_log_empty_state(self):
        before = self.snapshot()
        why = self.fu("why").stdout
        self.assertIn("(none)", why)
        self.assertIn("no reviews yet", why)
        self.assertIn("no reviews yet", self.fu("log").stdout)
        self.assertEqual(self.snapshot(), before)

    def test_why_and_log_with_data(self):
        a, b = self.add("a", why="care"), self.add("b", why="utility")
        self.apply({"decisions": [{"id": a, "action": "silent", "reason": "no news"},
                                  {"id": b, "action": "act_report", "reason": "rain"}],
                    "sent": {"id": b, "msg_id": "1"},
                    "outcomes": [{"id": b, "value": "annoyed"}], "watermark": 1.0})
        self.apply({"watermark": 1.0})
        before = self.snapshot()
        why = self.fu("why").stdout
        self.assertIn("utility: annoyed", why)
        self.assertIn(f"#{a} silent  no news", why)
        self.assertIn(f"#{b} act_report  rain", why)
        log = self.fu("log").stdout.splitlines()
        self.assertEqual(len(log), 2)
        self.assertIn("mode=capture  sent=0  decisions=0", log[0])  # newest first
        self.assertIn("mode=review   sent=1  decisions=2", log[1])
        self.assertNotIn("{", "".join(log))
        self.assertEqual(self.snapshot(), before)

    def test_chat_command_routes_why_and_log(self):
        import importlib.util
        from unittest import mock
        spec = importlib.util.spec_from_file_location("hermes_autonomous_plugin", ROOT / "__init__.py")
        plugin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(plugin)
        with mock.patch.dict(os.environ, self.env, clear=True):  # the subprocess inherits it
            self.assertIn("no reviews yet", plugin._followups_command("log"))
            self.assertIn("Muted categories", plugin._followups_command("why"))


class WakeHold(Home):
    """Due items must never become a review at night or inside the send cooldown:
    the gate says capture (if there is something to capture) or a constant held line."""

    def setUp(self):
        super().setUp()
        self.setup_owner()
        self.now = time.time()
        self.due = self.add("due-item", why="utility")

    def at_hour(self, hour: int) -> str:
        """Point the configured zone at a fixed offset where it is ``hour`` now and
        wake. Returns None if the hour rolled over mid-call (only at :59:59)."""
        tz = tz_at_hour(hour)
        self.fu("setup", "--timezone", tz)
        out = self.wake()
        return out if local_hour(tz) == hour else None

    def arm_capture(self):
        c = self.db()
        c.execute("INSERT INTO kv(k,v) VALUES('last_capture',?)", (str(self.now - 3 * 3600),))
        c.commit()
        self.state_db([(self.now - 30, "user", "new message", OWNER)])

    def set_last_send(self, ago_s: float):
        c = self.db()
        c.execute("INSERT INTO kv(k,v) VALUES('last_send',?) "
                  "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (str(self.now - ago_s),))
        c.commit()

    def test_night_hours_hold_review(self):
        for hour in (23, 0, 3, 7):
            with self.subTest(hour=hour):
                out = self.at_hour(hour)
                if out is not None:
                    self.assertEqual(out, "held night")

    def test_night_with_capture_wakes_capture_only(self):
        self.arm_capture()
        for hour in (23, 3, 7):
            with self.subTest(hour=hour):
                out = self.at_hour(hour)
                if out is not None:
                    self.assertEqual(out, "capture")

    def test_day_edges_do_review(self):
        # Control: the same state at 08:xx and 22:xx is a review, so the holds above
        # come from the night rule and not from something else.
        for hour in (8, 22):
            with self.subTest(hour=hour):
                out = self.at_hour(hour)
                if out is not None:
                    self.assertEqual(out, f"review ids {self.due}")

    def test_cooldown_holds_review(self):
        self.set_last_send(30 * 60)
        self.assertEqual(self.wake(), "held cooling")
        self.set_last_send(89 * 60)
        self.assertEqual(self.wake(), "held cooling")

    def test_cooldown_with_capture_wakes_capture_only(self):
        self.set_last_send(30 * 60)
        self.arm_capture()
        self.assertEqual(self.wake(), "capture")

    def test_cooldown_over_reviews(self):
        self.set_last_send(91 * 60)
        self.assertEqual(self.wake(), f"review ids {self.due}")


class PromptContract(Home):
    """prompts/review.md tells the agent what to feed `apply`. If the examples there
    drift from what apply accepts, the agent is taught to write rejected documents."""

    PROMPT = ROOT / "prompts" / "review.md"

    def setUp(self):
        super().setUp()
        self.setup_owner()
        self.text = self.PROMPT.read_text(encoding="utf-8")
        # The examples use ids up to 15 (§4 "ids": [12, 15]); make all of them real.
        for i in range(1, 16):
            self.assertEqual(self.add(f"existing-{i}"), i)

    def example_doc(self) -> dict:
        """The heredoc in §5: python3 ... apply <<'JSON' ... JSON."""
        import re
        m = re.search(r"followups\.py\" apply <<'JSON'\n(.*?)\nJSON\n", self.text, re.S)
        self.assertIsNotNone(m, "apply heredoc example not found in prompts/review.md")
        return json.loads(m.group(1))

    def inline_snippets(self, prefix: str) -> list[str]:
        """Backticked inline examples starting with ``prefix``, e.g. `"sent": {...}`."""
        import re
        return re.findall(r"`(" + re.escape(prefix) + r"[^`]*)`", self.text)

    def test_heredoc_example_applies(self):
        doc = self.example_doc()
        # Guard the extraction itself: it must be the full example, not a fragment.
        self.assertTrue({"new_followups", "decisions", "sent", "watermark"} <= set(doc), doc)
        # `followups.py send` records the DM itself; apply must not be taught to book it again.
        self.assertIsNone(doc["sent"])
        # act_report / ask ids were delivered by `followups.py send` before apply runs.
        c = self.db()
        for d in doc["decisions"]:
            if d["action"] != "silent":
                c.execute("UPDATE followups SET sent_msg_id='m-0', sent_at=? WHERE id=?",
                          (time.time(), d["id"]))
        c.commit()
        out = self.apply(doc)
        self.assertTrue(out["ok"], out)
        c = self.db()
        self.assertIsNotNone(c.execute(
            "SELECT 1 FROM followups WHERE key=?", (doc["new_followups"][0]["key"],)).fetchone())
        got = c.execute("SELECT last_ts FROM watermark WHERE surface='messages'").fetchone()[0]
        self.assertEqual(got, doc["watermark"])

    def test_sent_variants_apply(self):
        # §4: the exit-4 fallback receipt (msg_id "<msg_id>" is a placeholder but a
        # valid non-empty string).
        snippets = self.inline_snippets('"sent": {')
        self.assertGreaterEqual(len(snippets), 1, snippets)
        for s in snippets:
            with self.subTest(s=s):
                doc = json.loads("{" + s + ', "watermark": 1.0}')
                self.assertIn("text", doc["sent"])
                self.assertTrue(self.apply(doc)["ok"])

    def test_outcome_variants_apply(self):
        # §5: one example per outcome value.
        snippets = self.inline_snippets('{"id": ')
        outcomes = [json.loads(s) for s in snippets if '"value"' in s]
        self.assertGreaterEqual(len(outcomes), 4, snippets)
        for o in outcomes:
            with self.subTest(o=o):
                self.assertTrue(self.apply({"outcomes": [o], "watermark": 1.0})["ok"])

    def test_resend_rules_live_in_due_section(self):
        # The no-resend rules must sit in §3 (handling due items), where they are read
        # before the scoring, not somewhere the agent reaches after deciding.
        s3 = self.text.split("## 3.", 1)[1].split("## 4.", 1)[0]
        for needle in ("sends >= 1", "event passed", "`why=care` and `sends >= 2`", "sent_text"):
            self.assertIn(needle, s3)

    def test_final_reply_silent_unless_dev_log(self):
        # The final reply is delivered to the user's chat: default must be [SILENT],
        # the developer log only behind dev_log=true.
        s6 = self.text.split("## 6.", 1)[1].split("## 7.", 1)[0]
        self.assertIn("`dev_log` is not true (the default) → the final reply must be `[SILENT]`", s6)
        self.assertIn("Only when `dev_log=true`", s6)

    def test_empty_parts_as_documented(self):
        # §5: "Parts you don't have get an empty list or null; watermark is required."
        doc = {"new_followups": [], "outcomes": None, "close": [], "decisions": [],
               "sent": None, "watermark": 1.0}
        self.assertTrue(self.apply(doc)["ok"])


class PromptLanguage(unittest.TestCase):
    """Both prompts are in English, but the owner must be answered in their own
    language. Guard the rule so a later edit can't silently drop it."""

    def test_language_rule_in_both_prompts(self):
        for name in ("review.md", "decide.md"):
            with self.subTest(prompt=name):
                text = (ROOT / "prompts" / name).read_text(encoding="utf-8")
                paras = [p for p in text.split("\n\n")
                         if "owner's own language" in p and "message you send to the owner" in p]
                self.assertTrue(paras, f"language rule missing from prompts/{name}")
                self.assertIn("most recent messages", paras[0])


class SendCount(Home):
    """sends / sent_text: how often an item really went out and what it said last.
    The review needs both to tell "already told him" from "never told him"."""

    def setUp(self):
        super().setUp()
        self.setup_owner()

    def digest_item(self, fid: int) -> dict:
        items = json.loads(self.fu("digest").stdout)["followups"]
        return next(f for f in items if f["id"] == fid)

    def test_new_item_defaults(self):
        fid = self.add("x")
        r = self.row(fid)
        self.assertEqual((r["sends"], r["sent_text"]), (0, ""))

    def test_only_real_sends_count(self):
        fid = self.add("rain")
        self.apply({"decisions": [{"id": fid, "action": "silent", "reason": "r"}],
                    "watermark": 1.0})
        self.fu("checked", str(fid))
        r = self.row(fid)
        self.assertEqual((r["checks"], r["sends"]), (2, 0))  # checks still counts looks

        self.apply({"sent": {"id": fid, "msg_id": "1", "text": "明天下雨"}, "watermark": 1.0})
        self.fu("sent", str(fid), "--msg-id", "2", "--text", "今天还在下")
        r = self.row(fid)
        self.assertEqual((r["checks"], r["sends"]), (4, 2))
        self.assertEqual(r["sent_text"], "今天还在下")  # the last one wins

        self.apply({"decisions": [{"id": fid, "action": "silent", "reason": "r"}],
                    "watermark": 1.0})
        self.assertEqual(self.row(fid)["sends"], 2)

    def test_sent_ids_record_text_on_each(self):
        a, b = self.add("a"), self.add("b")
        self.apply({"sent": {"ids": [a, b], "msg_id": "7", "text": "two in one"},
                    "watermark": 1.0})
        for fid in (a, b):
            r = self.row(fid)
            self.assertEqual((r["sends"], r["sent_text"]), (1, "two in one"))

    def test_sent_without_text_still_counts(self):
        fid = self.add("x")
        self.apply({"sent": {"id": fid, "msg_id": "1"}, "watermark": 1.0})
        r = self.row(fid)
        self.assertEqual((r["sends"], r["sent_text"]), (1, ""))

    def test_sent_text_not_a_string_rolls_back(self):
        fid = self.add("x")
        before = self.snapshot()
        out = self.apply({"sent": {"id": fid, "msg_id": "1", "text": 5}, "watermark": 1.0},
                         ok=False)
        self.assertIn("sent.text", out["error"])
        self.assertEqual(self.snapshot(), before)

    def test_long_text_is_truncated_in_db_and_digest(self):
        fid = self.add("x")
        long = "长" * 5000
        self.apply({"sent": {"id": fid, "msg_id": "1", "text": long}, "watermark": 1.0})
        self.assertEqual(self.row(fid)["sent_text"], "长" * 1200)
        item = self.digest_item(fid)
        self.assertEqual(item["sent_text"], "长" * 300)
        self.fu("sent", str(fid), "--msg-id", "2", "--text", "x" * 5000)
        self.assertEqual(len(self.row(fid)["sent_text"]), 1200)

    def test_digest_carries_sends_and_sent_text(self):
        fid = self.add("x")
        item = self.digest_item(fid)
        self.assertEqual((item["sends"], item["sent_text"]), (0, ""))
        self.apply({"sent": {"id": fid, "msg_id": "1", "text": "好点没？"}, "watermark": 1.0})
        item = self.digest_item(fid)
        self.assertEqual((item["sends"], item["sent_text"]), (1, "好点没？"))

    def test_upsert_keeps_sends_and_text(self):
        fid = self.add("trip")
        self.apply({"sent": {"id": fid, "msg_id": "1", "text": "t"}, "watermark": 1.0})
        self.apply({"new_followups": [{"key": "trip", "why": "care", "kind": "k",
                                       "subject": "v2", "revisit_at": "+1d"}],
                    "watermark": 1.0})
        r = self.row(fid)
        self.assertEqual((r["sends"], r["sent_text"]), (1, "t"))


class OldDbMigration(Home):
    """A followups.db from before sends/sent_text (and before sent_at/outcome)."""

    def test_rows_sent_before_the_column_backfill_to_one(self):
        path = self.home / "plugin-data" / "hermes-autonomous"
        path.mkdir(parents=True)
        c = sqlite3.connect(path / "followups.db")
        c.executescript(
            "CREATE TABLE followups (id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE,"
            " kind TEXT, why TEXT, subject TEXT, quote TEXT, hint TEXT, revisit_at REAL,"
            " status TEXT DEFAULT 'active', checks INTEGER DEFAULT 0, last_checked REAL,"
            " note TEXT, created_at REAL, sent_at REAL, sent_msg_id TEXT);")
        c.execute("INSERT INTO followups(key,why,subject,revisit_at,sent_at,sent_msg_id)"
                  " VALUES('sent','care','s',?,?,?)", (time.time() + 3600, time.time() - 86400, "42"))
        c.execute("INSERT INTO followups(key,why,subject,revisit_at)"
                  " VALUES('never','care','s',?)", (time.time() + 3600,))
        c.commit()
        c.close()

        self.setup_owner()  # migration + backfill
        rows = {r["key"]: r["sends"] for r in self.db().execute("SELECT key,sends FROM followups")}
        self.assertEqual(rows, {"sent": 1, "never": 0})
        # idempotent
        self.setup_owner()
        rows = {r["key"]: r["sends"] for r in self.db().execute("SELECT key,sends FROM followups")}
        self.assertEqual(rows, {"sent": 1, "never": 0})

    def test_old_db_gains_columns_with_defaults(self):
        path = self.home / "plugin-data" / "hermes-autonomous"
        path.mkdir(parents=True)
        c = sqlite3.connect(path / "followups.db")
        c.executescript(
            "CREATE TABLE followups (id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE,"
            " kind TEXT, why TEXT, subject TEXT, quote TEXT, hint TEXT, revisit_at REAL,"
            " status TEXT DEFAULT 'active', checks INTEGER DEFAULT 0, last_checked REAL,"
            " note TEXT, created_at REAL);")
        c.execute("INSERT INTO followups(key,kind,why,subject,revisit_at,checks)"
                  " VALUES('old','k','care','s',?,3)", (time.time() + 3600,))
        c.commit()
        c.close()

        self.setup_owner()  # opens the DB: migration runs here
        cols = {r["name"]: r for r in self.db().execute("PRAGMA table_info(followups)")}
        for name in ("sent_at", "sent_msg_id", "outcome", "sends", "sent_text"):
            self.assertIn(name, cols)
        self.assertEqual(cols["sends"]["notnull"], 1)
        self.assertEqual(cols["sent_text"]["notnull"], 1)

        r = self.db().execute("SELECT * FROM followups WHERE key='old'").fetchone()
        self.assertEqual((r["sends"], r["sent_text"], r["checks"]), (0, "", 3))
        item = json.loads(self.fu("digest").stdout)["followups"][0]
        self.assertEqual((item["sends"], item["sent_text"]), (0, ""))

        # Opening again is a no-op, and a send now counts from zero.
        self.apply({"sent": {"id": r["id"], "msg_id": "1", "text": "hi"}, "watermark": 1.0})
        r = self.db().execute("SELECT * FROM followups WHERE key='old'").fetchone()
        self.assertEqual((r["sends"], r["sent_text"], r["checks"]), (1, "hi", 4))


class DigestTruncationAndNoise(Home):
    def setUp(self):
        super().setUp()
        self.setup_owner()
        self.wm = time.time() - 3600
        self.apply({"watermark": self.wm})

    def digest(self) -> dict:
        return json.loads(self.fu("digest").stdout)

    def test_keeps_tail_250_and_cursor_is_last_kept(self):
        n = 300
        self.state_db([(self.wm + 1 + i * 0.5, "user" if i % 2 else "assistant", f"m{i}", OWNER)
                       for i in range(n)])
        d = self.digest()
        texts = [m["text"] for m in d["messages"]]
        self.assertEqual(len(texts), 250)
        self.assertEqual(texts, [f"m{i}" for i in range(n - 250, n)])
        self.assertEqual(d["new_messages"], 250)
        self.assertEqual(d["next_watermark"], self.wm + 1 + (n - 1) * 0.5)

    def test_trailing_noise_does_not_move_cursor(self):
        n = 260
        msgs = [(self.wm + 1 + i, "user", f"m{i}", OWNER) for i in range(n)]
        msgs.append((self.wm + 1000, "user", "[CONTEXT COMPACTION] summary", OWNER))
        self.state_db(msgs)
        d = self.digest()
        self.assertEqual([m["text"] for m in d["messages"]], [f"m{i}" for i in range(10, n)])
        self.assertEqual(d["next_watermark"], self.wm + n)  # last kept, not the noise

    def test_compaction_messages_are_dropped(self):
        self.state_db([
            (self.wm + 1, "user", "real one", OWNER),
            (self.wm + 2, "assistant", "[CONTEXT COMPACTION] earlier turns summarised", OWNER),
            (self.wm + 3, "user", "  [CONTEXT COMPACTION — leading space", OWNER),
            (self.wm + 4, "assistant", "real two", OWNER),
        ])
        d = self.digest()
        self.assertEqual([m["text"] for m in d["messages"]], ["real one", "real two"])
        self.assertFalse(any("CONTEXT COMPACTION" in m["text"] for m in d["messages"]))

    def test_noise_does_not_count_toward_the_cap(self):
        msgs = []
        for i in range(260):
            text = f"[CONTEXT COMPACTION {i}]" if i % 13 == 0 else f"m{i}"
            msgs.append((self.wm + 1 + i, "user", text, OWNER))
        self.state_db(msgs)
        texts = [m["text"] for m in self.digest()["messages"]]
        self.assertEqual(len(texts), 240)  # 20 noise dropped first, so nothing truncated
        self.assertEqual(texts[0], "m1")

    def test_background_process_notices_are_not_user_messages(self):
        # These arrive as role=user in the owner's DM. Counting them woke the
        # agent to find "no user messages", and its log woke it again.
        self.state_db([
            (self.wm + 1, "user", "real question", OWNER),
            (self.wm + 2, "user", "[IMPORTANT: Background process proc_1 completed normally (exit code 0).", OWNER),
            (self.wm + 3, "user", "Background process proc_2 finished", OWNER),
        ])
        d = self.digest()
        self.assertEqual([m["text"] for m in d["messages"]], ["real question"])

    def test_wake_ignores_background_notices_only(self):
        c = self.db()
        c.execute("UPDATE kv SET v=? WHERE k='last_capture'", (str(time.time() - 3 * 3600),))
        c.commit()
        self.state_db([(self.wm + 5, "user",
                        "[IMPORTANT: Background process proc_1 completed normally (exit code 0).", OWNER)])
        self.assertEqual(self.digest()["messages"], [])
        self.assertEqual(self.wake(), "quiet")

    def test_wake_ignores_compaction_only_traffic(self):
        # Wake and digest must agree on what counts as a new message: otherwise a
        # lone compaction turn wakes a capture every 2h that digests nothing and
        # never moves the cursor past it.
        c = self.db()
        c.execute("UPDATE kv SET v=? WHERE k='last_capture'", (str(time.time() - 3 * 3600),))
        c.commit()
        self.state_db([(self.wm + 5, "user", "[CONTEXT COMPACTION] summary", OWNER)])
        self.assertEqual(self.digest()["messages"], [])
        self.assertEqual(self.wake(), "quiet")


class Doctor(Home):
    FIX = ["--monitor-script", "followup-wake.py", "--deliver", "local",
           "--failure-deliver", "origin"]

    def setUp(self):
        super().setUp()
        self.setup_owner()
        scripts = self.home / "scripts"
        scripts.mkdir()
        for name in ("followup-wake.py", "followup-digest.py"):
            (scripts / name).write_text("")
        (self.bin / "calls.jsonl").unlink(missing_ok=True)  # forget setup's own doctor run
        self.set_jobs([])

    def test_create_command_uses_a_positional_prompt(self):
        # `hermes cron create` has no --prompt flag; the prompt is positional.
        # A suggestion that cannot be pasted is worse than no suggestion.
        spec = importlib.util.spec_from_file_location("followups_probe", FOLLOWUPS)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        cmd = mod._create_commands()[-1]
        self.assertNotIn("--prompt", cmd)
        self.assertIn("prompts/review.md", cmd)
        self.assertLess(cmd.index("prompts/review.md"), cmd.index("--name"))

    def job(self, **over) -> dict:
        """One entry shaped like Hermes 0.22.2's own store: unset fields are null,
        not absent, and there is no failure_deliver until `cron edit` sets one."""
        j = {"id": "a1b2c3", "name": "proactive follow-ups",
             "prompt": "Read /x/prompts/review.md and follow it", "skills": [], "skill": None,
             "model": None, "provider": None, "base_url": None,
             "script": "followup-digest.py", "no_agent": False,
             "monitor_script": "followup-wake.py", "monitor_url": None, "monitor_state": None,
             "context_from": None,
             "schedule": {"kind": "cron", "expr": "*/15 * * * *"},
             "schedule_display": "*/15 * * * *", "repeat": {"times": None, "completed": 0},
             "enabled": True, "state": "scheduled", "paused_at": None, "paused_reason": None,
             "created_at": "2026-10-01T09:00:00+00:00", "next_run_at": None,
             "last_run_at": None, "last_status": None, "last_error": None,
             "deliver": "local", "origin": None, "enabled_toolsets": None, "workdir": None}
        j.update(over)
        return j

    def set_jobs(self, jobs: list[dict]):
        """Doctor reads $HERMES_HOME/cron/jobs.json first; write that, not the fake CLI."""
        store = self.home / "cron" / "jobs.json"
        store.parent.mkdir(exist_ok=True)
        store.write_text(json.dumps({"jobs": jobs, "updated_at": "2026-11-01T00:00:00+00:00"}))

    def store_jobs(self) -> list[dict]:
        return json.loads((self.home / "cron" / "jobs.json").read_text())["jobs"]

    def listed_via_cli(self) -> bool:
        return any(c[:2] == ["cron", "list"] for c in self.hermes_calls())

    def doctor(self, *args: str):
        p = self.run_cli(FOLLOWUPS, "doctor", "--json", *args)
        return p, json.loads(p.stdout)

    def edits(self) -> list[list[str]]:
        return [c for c in self.hermes_calls() if c[:2] == ["cron", "edit"]]

    def test_healthy_job_is_ok_and_fix_does_not_edit(self):
        self.set_jobs([self.job()])
        p, rep = self.doctor()
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertTrue(rep["ok"])
        self.assertEqual([j["id"] for j in rep["jobs"]], ["a1b2c3"])
        p, rep = self.doctor("--fix")
        self.assertTrue(rep["ok"])
        self.assertEqual(self.edits(), [])
        self.assertFalse(self.listed_via_cli(), "a readable store must not fall back to the CLI")

    def test_store_without_failure_deliver_is_healthy(self):
        # The real store carries no failure_deliver for this job; doctor must not
        # treat its absence as broken (and so must not keep editing the job).
        job = self.job()
        self.assertNotIn("failure_deliver", job)
        self.set_jobs([job])
        _, rep = self.doctor("--fix")
        self.assertTrue(rep["ok"], rep)
        self.assertEqual(self.edits(), [])

    def test_disabled_job_is_still_found(self):
        self.set_jobs([self.job(enabled=False, state="paused", monitor_script=None)])
        _, rep = self.doctor()
        self.assertEqual([j["id"] for j in rep["jobs"]], ["a1b2c3"])
        self.assertIn("job a1b2c3: monitor_script",
                      {c["name"] for c in rep["checks"] if c["status"] == "WARN"})

    def test_falls_back_to_the_cli_without_a_store(self):
        (self.home / "cron" / "jobs.json").unlink()
        Home.set_jobs(self, [self.job()])  # only the fake CLI knows the job now
        _, rep = self.doctor()
        self.assertTrue(rep["ok"], rep)
        self.assertEqual([j["id"] for j in rep["jobs"]], ["a1b2c3"])
        self.assertTrue(self.listed_via_cli())

    def test_unreadable_store_falls_back_to_the_cli(self):
        (self.home / "cron" / "jobs.json").write_text("{not json")
        Home.set_jobs(self, [self.job()])
        _, rep = self.doctor()
        self.assertEqual([j["id"] for j in rep["jobs"]], ["a1b2c3"])
        self.assertTrue(self.listed_via_cli())

    def test_old_job_warns_and_fix_edits_it_in_place(self):
        other = self.job(id="zzz999", name="unrelated", prompt="check the mailbox",
                         script="mailbox.py", monitor_script=None, deliver="origin")
        self.set_jobs([self.job(monitor_script=None, deliver="origin"), other])
        p, rep = self.doctor()
        self.assertEqual(p.returncode, 1)
        self.assertFalse(rep["ok"])
        warns = {c["name"] for c in rep["checks"] if c["status"] == "WARN"}
        self.assertEqual(warns, {"job a1b2c3: monitor_script", "job a1b2c3: deliver"})
        self.assertEqual(self.edits(), [], "doctor without --fix must not edit")

        p, rep = self.doctor("--fix")
        self.assertTrue(rep["ok"], rep)
        self.assertEqual(self.edits(), [["cron", "edit", "a1b2c3", *self.FIX]])
        self.assertTrue(all(c["fixed"] for c in rep["checks"] if c["status"] == "WARN"))
        # The edit landed in the store: ours fixed, the unrelated job untouched.
        ours, theirs = self.store_jobs()
        self.assertEqual((ours["monitor_script"], ours["deliver"], ours["failure_deliver"]),
                         ("followup-wake.py", "local", "origin"))
        self.assertEqual(theirs, other)
        # Idempotent: the job is now right, a second --fix leaves it alone.
        _, rep = self.doctor("--fix")
        self.assertTrue(rep["ok"])
        self.assertEqual(len(self.edits()), 1)
        self.assertFalse(self.listed_via_cli())

    def test_missing_wake_script_is_not_wired_in(self):
        (self.home / "scripts" / "followup-wake.py").unlink()
        self.set_jobs([self.job(monitor_script=None)])
        _, rep = self.doctor("--fix")
        self.assertFalse(rep["ok"])
        self.assertEqual(self.edits(), [])
        self.assertTrue(any("cron edit a1b2c3" in c for c in rep["suggested_commands"]))

    def test_missing_script_file_fails(self):
        (self.home / "scripts" / "followup-digest.py").unlink()
        self.set_jobs([self.job()])
        _, rep = self.doctor()
        fails = [c["name"] for c in rep["checks"] if c["status"] == "FAIL"]
        self.assertEqual(fails, ["job a1b2c3: script file"])

    def test_matches_by_prompt_or_chinese_name(self):
        self.set_jobs([self.job(id="j1", name="x", script=None),
                       self.job(id="j2", name="主动跟进", prompt="", script=None)])
        _, rep = self.doctor()
        self.assertEqual([j["id"] for j in rep["jobs"]], ["j1", "j2"])
        self.assertIn("WARN", [c["status"] for c in rep["checks"] if c["name"] == "cron"])

    def test_no_job_prints_create_command_and_never_edits(self):
        p = self.run_cli(FOLLOWUPS, "doctor", "--fix")
        self.assertEqual(p.returncode, 1)
        self.assertIn("hermes cron create", p.stdout)
        for flag in ("--monitor-script followup-wake.py", "--script followup-digest.py",
                     "--deliver local", "--failure-deliver origin"):
            self.assertIn(flag, p.stdout)
        self.assertEqual(self.edits(), [])

    def test_json_output_is_stable(self):
        p = self.run_cli(FOLLOWUPS, "doctor", "--json")
        rep = json.loads(p.stdout)
        self.assertEqual(set(rep), {"ok", "checks", "jobs", "suggested_commands"})
        for c in rep["checks"]:
            self.assertEqual(set(c), {"name", "status", "detail", "fixed"})
            self.assertIn(c["status"], ("OK", "WARN", "FAIL"))

    def test_missing_settings_fail_but_defaults_only_inform(self):
        (self.home / "plugin-data" / "hermes-autonomous" / "settings.json").write_text("{}")
        self.set_jobs([self.job()])
        _, rep = self.doctor()
        status = {c["name"]: c["status"] for c in rep["checks"]}
        self.assertEqual(status["settings.owner_id"], "FAIL")
        self.assertEqual(status["settings.dm_target"], "FAIL")
        self.assertEqual(status["settings.timezone"], "OK")
        self.assertFalse(rep["ok"])

    def test_text_output_is_english_but_keeps_job_names(self):
        self.set_jobs([self.job(id="j2", name="主动跟进", monitor_script=None)])
        p = self.run_cli(FOLLOWUPS, "doctor")
        self.assertEqual(p.returncode, 1)
        self.assertIn("[OK   ] settings.owner_id", p.stdout)
        self.assertIn("[WARN ] job j2: monitor_script", p.stdout)
        self.assertIn("cron edit j2", p.stdout)
        self.assertIn("not ok", p.stdout)
        self.assertIn("主动跟进", p.stdout)  # the user's own job name, not translated
        self.assertIsNone(CJK.search(p.stdout.replace("主动跟进", "")), p.stdout)

    def test_setup_prints_warn_summary_but_succeeds(self):
        p = self.fu("setup", "--dev-log")  # no cron job exists: doctor warns
        json.loads(p.stdout)  # stdout stays one JSON document
        self.assertIn("[WARN] cron", p.stderr)
        self.assertLessEqual(len([l for l in p.stderr.splitlines() if l.strip().startswith("[")]), 5)
        self.assertEqual(self.edits(), [])


class DbMode(Home):
    def path(self) -> Path:
        return self.home / "plugin-data" / "hermes-autonomous" / "followups.db"

    def test_new_db_is_0600(self):
        self.fu("list")
        self.assertEqual(stat.S_IMODE(self.path().stat().st_mode), 0o600)

    def test_existing_0644_db_is_tightened(self):
        self.fu("list")
        self.path().chmod(0o644)
        self.fu("list")
        self.assertEqual(stat.S_IMODE(self.path().stat().st_mode), 0o600)


def load_plugin():
    spec = importlib.util.spec_from_file_location("hermes_autonomous_plugin", ROOT / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class PluginCommandHome(Home):
    """/followups must hand the gateway's current profile home to its helper."""

    def setUp(self):
        super().setUp()
        self.other = Path(tempfile.mkdtemp(prefix="hermes-other-"))
        self.addCleanup(shutil.rmtree, self.other, True)
        self.plugin = load_plugin()

    def capture_env(self) -> dict:
        seen = {}

        def fake_run(cmd, **kw):
            seen.update(kw.get("env") or {})
            return subprocess.CompletedProcess(cmd, 0, "ok", "")

        with mock.patch.object(self.plugin.subprocess, "run", fake_run):
            self.assertEqual(self.plugin._followups_command("list"), "ok")
        return seen

    def fake_constants(self, home: Path):
        return mock.patch.dict(sys.modules, {"hermes_constants": types.SimpleNamespace(
            get_hermes_home=lambda: home)})

    def test_prefers_hermes_constants_over_environ(self):
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(self.other)}), \
                self.fake_constants(self.home):
            env = self.capture_env()
        self.assertEqual(env["HERMES_HOME"], str(self.home))
        self.assertEqual(env.get("PATH"), os.environ.get("PATH"))  # rest is inherited

    def test_falls_back_to_environ_without_hermes_constants(self):
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(self.other)}), \
                mock.patch.dict(sys.modules, {"hermes_constants": None}):
            env = self.capture_env()
        self.assertEqual(env["HERMES_HOME"], str(self.other))

    def test_falls_back_to_default_home(self):
        environ = {k: v for k, v in os.environ.items() if k != "HERMES_HOME"}
        with mock.patch.dict(os.environ, environ, clear=True), \
                mock.patch.dict(sys.modules, {"hermes_constants": None}):
            env = self.capture_env()
        self.assertEqual(env["HERMES_HOME"], str(Path.home() / ".hermes"))

    def test_usage_and_errors_are_english(self):
        def silent(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 0, "", "")

        def hang(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))

        with mock.patch.object(self.plugin.subprocess, "run", silent):
            usage = self.plugin._followups_command("")
        with mock.patch.object(self.plugin.subprocess, "run", hang):
            timeout = self.plugin._followups_command("doctor")
        with mock.patch.object(self.plugin, "SCRIPT", self.other / "missing.py"):
            missing = self.plugin._followups_command("list")
        self.assertTrue(usage.startswith("Usage: /followups"), usage)
        self.assertTrue(timeout.startswith("Failed to read: TimeoutExpired"), timeout)
        self.assertTrue(missing.startswith("Cannot find"), missing)
        for text in (usage, timeout, missing):
            self.assertIsNone(CJK.search(text), text)

    def test_real_helper_reads_current_profile_board(self):
        self.add("only-in-current-profile")
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(self.other)}), \
                self.fake_constants(self.home):
            out = self.plugin._followups_command("list")
        self.assertIn("only-in-current-profile", out)
        self.assertFalse((self.other / "plugin-data" / "hermes-autonomous" / "followups.db").exists())


class ReadmeDisclosure(unittest.TestCase):
    def test_both_readmes_disclose_agent_shell_and_decider(self):
        for name in ("README.md", "README.zh-CN.md"):
            text = (ROOT / name).read_text(encoding="utf-8")
            for needle in ("hermes send", "hermes -z", "followups.py apply", "scripts/decide.py"):
                self.assertIn(needle, text, f"{name}: {needle}")


if __name__ == "__main__":
    unittest.main()


class Send(Home):
    """`followups.py send`: the send gate and the receipt live in code, not in the prompt."""

    BODY = "☔ Tokyo tomorrow: rain\nTake an umbrella\n"

    def setUp(self):
        super().setUp()
        self.setup_owner()
        self.a = self.add("trip", why="utility")
        self.b = self.add("throat", why="care")

    def send(self, ids, body: str | None = None, rc: int = 0) -> dict:
        p = self.run_cli(FOLLOWUPS, "send", "--ids", ",".join(str(i) for i in ids),
                         stdin=self.BODY if body is None else body)
        self.assertEqual(p.returncode, rc, p.stdout + p.stderr)
        return json.loads(p.stdout)

    def sends(self) -> list[dict]:
        f = self.bin / "sends.jsonl"
        return [json.loads(l) for l in f.read_text().splitlines()] if f.exists() else []

    def kv(self) -> dict:
        return {r["k"]: float(r["v"]) for r in self.db().execute("SELECT * FROM kv")}

    def set_last_send(self, ago_s: float):
        c = self.db()
        c.execute("INSERT INTO kv(k,v) VALUES('last_send',?) ON CONFLICT(k) DO UPDATE SET "
                  "v=excluded.v", (str(time.time() - ago_s),))
        c.commit()

    def assert_refused(self, ids, reason: str, body: str | None = None):
        before = self.snapshot()
        out = self.send(ids, body, rc=2)
        self.assertEqual((out["ok"], out["refused"]), (False, reason), out)
        self.assertEqual(self.sends(), [], "hermes send must not be called")
        self.assertEqual(self.snapshot(), before)

    def test_send_records_immediately_without_apply(self):
        t0 = time.time()
        out = self.send([self.b, self.a])
        self.assertEqual(out, {"ok": True, "msg_id": "m-1", "ids": [self.b, self.a], "sent": 2})
        # argv list straight to hermes, body on stdin, stripped.
        [call] = self.sends()
        self.assertEqual(call["args"], ["send", "--to", f"telegram:{OWNER}", "--json",
                                        "--file", "-"])
        self.assertEqual(call["body"], self.BODY.strip())
        for fid in (self.a, self.b):
            r = self.row(fid)
            self.assertEqual((r["sends"], r["checks"], r["sent_msg_id"], r["sent_text"]),
                             (1, 1, "m-1", self.BODY.strip()))
            self.assertAlmostEqual(r["sent_at"], t0, delta=30)
            self.assertAlmostEqual(r["revisit_at"], r["sent_at"] + 86400, delta=0.001)
        self.assertAlmostEqual(self.kv()["last_send"], self.row(self.a)["sent_at"], delta=0.001)
        # No apply ever ran: the log is empty and the receipt stands on its own.
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM log").fetchone()[0], 0)

    def test_body_is_never_shell_interpreted(self):
        body = 'it\'s "$HOME" `id` $(rm -rf /) ; echo hi'
        self.send([self.a], body)
        self.assertEqual(self.sends()[0]["body"], body)
        self.assertEqual(self.row(self.a)["sent_text"], body)

    def test_cooling_refused(self):
        self.set_last_send(30 * 60)
        self.assert_refused([self.a], "cooling")

    def test_night_refused(self):
        tz = tz_at_hour(3)
        self.fu("setup", "--timezone", tz)
        if local_hour(tz) == 3:  # the hour did not roll over mid-test
            self.assert_refused([self.a], "night")

    def test_muted_category_refused(self):
        c = self.db()
        ts = time.time() - 86400
        c.execute("INSERT INTO followups(key,kind,why,subject,status,sent_at,revisit_at,outcome)"
                  " VALUES('old','k','care','s','closed',?,?,'annoyed')", (ts, ts + 86400))
        c.commit()
        self.assert_refused([self.a, self.b], "muted")
        self.send([self.a])  # the other category still goes out

    def test_four_ids_refused(self):
        more = [self.add(f"x{i}", why="utility") for i in range(2)]
        self.assert_refused([self.a, self.b, *more], "too_many_ids")

    def test_seven_lines_refused_not_truncated(self):
        self.assert_refused([self.a], "too_many_lines", "\n".join(f"line {i}" for i in range(7)))
        # Six lines with blank separators is fine.
        self.send([self.a], "\n\n".join(f"line {i}" for i in range(6)))

    def test_empty_body_refused(self):
        self.assert_refused([self.a], "empty_text", "  \n\n ")

    def test_item_state_refused(self):
        later = self.add("later", revisit="+5h")
        self.assert_refused([later], "not_due")
        self.assert_refused([999], "not_found")
        self.apply({"close": [{"id": self.b}], "watermark": 1.0})
        self.assert_refused([self.b], "not_active")
        self.assert_refused([self.a, self.a], "bad_ids")

    def test_no_message_id_is_a_failure_and_writes_nothing(self):
        for reply in ({"success": True, "message_id": None},
                      {"success": True, "message_id": "None"},
                      {"success": True},
                      {"success": True, "skipped": True, "reason": "cron_auto_delivery_duplicate_target"},
                      {"success": False, "error": "chat not found"}):
            with self.subTest(reply=reply):
                (self.bin / "send_reply.json").write_text(json.dumps(reply))
                before = self.snapshot()
                out = self.send([self.a], rc=3)
                self.assertFalse(out["ok"])
                self.assertEqual(self.snapshot(), before)
        self.assertEqual(len(self.sends()), 5)  # it was tried, and nothing was booked

    def test_repeat_is_refused_not_resent(self):
        self.send([self.a])
        # Even with the cooldown out of the way, the same ids are no longer due.
        self.set_last_send(3 * 3600)
        out = self.send([self.a], rc=2)
        self.assertEqual(out["refused"], "not_due")
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(self.row(self.a)["sends"], 1)

    def test_apply_with_same_msg_id_is_not_counted_twice(self):
        self.send([self.a, self.b])
        sent_at = self.row(self.a)["sent_at"]
        last_send = self.kv()["last_send"]
        out = self.apply({"decisions": [{"id": self.a, "action": "act_report", "reason": "r"}],
                          "sent": {"ids": [self.a, self.b], "msg_id": "m-1", "text": "x"},
                          "watermark": 1.0})
        self.assertEqual((out["sent"], out["already_recorded"]), ([], [self.a, self.b]))
        for fid in (self.a, self.b):
            r = self.row(fid)
            self.assertEqual((r["sends"], r["checks"], r["sent_at"]), (1, 1, sent_at))
        self.assertEqual(self.kv()["last_send"], last_send)
        # A different msg_id is a different message: still booked (old prompts).
        self.apply({"sent": {"id": self.a, "msg_id": "m-2", "text": "y"}, "watermark": 1.0})
        self.assertEqual(self.row(self.a)["sends"], 2)


class Gate(Home):
    """digest and wake read one send gate; wake prints one constant line per state."""

    def setUp(self):
        super().setUp()
        self.setup_owner()

    def digest(self) -> dict:
        return json.loads(self.fu("digest").stdout)

    def item(self, d: dict, fid: int) -> dict:
        return next(f for f in d["followups"] if f["id"] == fid)

    def wake_twice(self) -> str:
        a, b = self.run_cli(WAKE), self.run_cli(WAKE)
        self.assertEqual(a.stdout.encode(), b.stdout.encode(), "wake must be byte-stable")
        return a.stdout.strip()

    def set_last_send(self, ago_s: float):
        c = self.db()
        c.execute("INSERT INTO kv(k,v) VALUES('last_send',?) ON CONFLICT(k) DO UPDATE SET "
                  "v=excluded.v", (str(time.time() - ago_s),))
        c.commit()

    def mute(self, why: str):
        ts = time.time() - 86400
        c = self.db()
        # sends=1: a row with sent_at is a row that went out, and connect() now
        # backfills that, so the fixture must start consistent or a refusal would
        # look like a database change.
        c.execute("INSERT INTO followups(key,kind,why,subject,status,sent_at,sends,revisit_at,outcome)"
                  " VALUES(?,?,?,?,?,?,?,?,?)", (f"m-{why}", "k", why, "s", "closed", ts, 1,
                                               ts + 86400, "annoyed"))
        c.commit()

    def test_quiet(self):
        self.add("later", revisit="+5h")
        d = self.digest()
        self.assertEqual((d["mode"], d["may_send"], d["cooling"], d["night"]),
                         ("quiet", False, False, False))
        self.assertEqual(self.wake_twice(), "quiet")

    def test_may_send(self):
        b, a = self.add("b", why="utility"), self.add("a", why="care")
        d = self.digest()
        self.assertEqual((d["mode"], d["may_send"]), ("review", True))
        for fid in (a, b):
            self.assertEqual((self.item(d, fid)["held"], self.item(d, fid)["held_reason"]),
                             (False, None))
        self.assertEqual(self.wake_twice(), f"review ids {min(a, b)},{max(a, b)}")

    def test_cooling(self):
        fid = self.add("x")
        self.set_last_send(10 * 60)
        d = self.digest()
        self.assertEqual((d["mode"], d["may_send"], d["cooling"]), ("quiet", False, True))
        self.assertEqual((self.item(d, fid)["due"], self.item(d, fid)["held"],
                          self.item(d, fid)["held_reason"]), (True, True, "cooling"))
        self.assertEqual(self.wake_twice(), "held cooling")

    def test_night(self):
        fid = self.add("x")
        tz = tz_at_hour(23)
        self.fu("setup", "--timezone", tz)
        d = self.digest()
        out = self.wake_twice()
        if local_hour(tz) != 23:
            return  # the hour rolled over mid-test
        self.assertEqual((d["mode"], d["may_send"], d["night"]), ("quiet", False, True))
        self.assertEqual(self.item(d, fid)["held_reason"], "night")
        self.assertEqual(out, "held night")

    def test_muted_partial_and_full(self):
        care, util = self.add("care", why="care"), self.add("util", why="utility")
        self.mute("care")
        d = self.digest()
        self.assertTrue(d["may_send"])
        self.assertEqual(self.item(d, care)["held_reason"], "muted")
        self.assertFalse(self.item(d, util)["held"])
        self.assertEqual(self.wake_twice(), f"review ids {util}")
        self.mute("utility")
        d = self.digest()
        self.assertEqual((d["mode"], d["may_send"]), ("quiet", False))
        self.assertEqual(self.wake_twice(), "held muted")

    def test_no_sendable_item_when_may_send_false(self):
        self.add("x")
        self.add("y", why="utility")
        self.set_last_send(5 * 60)
        d = self.digest()
        self.assertFalse(d["may_send"])
        self.assertTrue(all(f["held"] for f in d["followups"] if f["due"]))

    def test_capture_beats_hold_and_digest_mode_capture(self):
        self.add("x")
        self.set_last_send(10 * 60)
        now = time.time()
        c = self.db()
        c.execute("INSERT INTO kv(k,v) VALUES('last_capture',?)", (str(now - 3 * 3600),))
        c.execute("INSERT INTO watermark(surface,last_ts) VALUES('messages',?)", (now - 3600,))
        c.commit()
        self.state_db([(now - 30, "user", "new message", OWNER)])
        self.assertEqual(self.digest()["mode"], "capture")
        self.assertEqual(self.wake_twice(), "capture")

    def test_wake_has_no_gate_of_its_own(self):
        src = WAKE.read_text(encoding="utf-8")
        for needle in ("SEND_COOLDOWN", "last_send", "muted_whys", ".hour"):
            self.assertNotIn(needle, src)
        self.assertIn("send_gate(", src)


class PromptSend(unittest.TestCase):
    TEXT = (ROOT / "prompts" / "review.md").read_text(encoding="utf-8")

    def test_send_goes_through_the_helper(self):
        s4 = self.TEXT.split("## 4.", 1)[1].split("## 5.", 1)[0]
        self.assertIn("followups.py\" send --ids 12,15 <<'MSG'", s4)
        self.assertNotIn("hermes send --to", self.TEXT)
        self.assertIn("**Do not** work around it with your own `hermes send`", s4)
        for gate in ("night", "cooldown", "muted", "at most 3 ids", "6 non-blank lines"):
            self.assertIn(gate, s4)

    def test_no_false_claim_about_state_db(self):
        self.assertNotIn("not written to state.db", self.TEXT)
        self.assertIn('"mirrored": true', self.TEXT)


class OwnerOnlyCommand(Home):
    """/followups shows quotes from the owner's private chats: owner only."""

    def setUp(self):
        super().setUp()
        self.setup_owner()
        self.plugin = load_plugin()
        os.environ["HERMES_HOME"] = str(self.home)  # restored by Home's environ patch
        self.calls = []

    def fake_run(self, cmd, **kw):
        self.calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "ok", "")

    def run_as(self, raw: str, user: str | None = None, chat: str | None = None,
               via_gateway: bool = True) -> str:
        vals = {k: v for k, v in (("HERMES_SESSION_USER_ID", user),
                                  ("HERMES_SESSION_CHAT_TYPE", chat)) if v is not None}
        if via_gateway:
            ctx = types.SimpleNamespace(get_session_env=lambda n, d="": vals.get(n, d))
            source = mock.patch.dict(sys.modules, {
                "gateway": types.SimpleNamespace(session_context=ctx),
                "gateway.session_context": ctx})
        else:  # no gateway module: fall back to the process environment
            source = mock.patch.dict(os.environ, vals)
        with source, mock.patch.object(self.plugin.subprocess, "run", self.fake_run):
            return self.plugin._followups_command(raw)

    def test_non_owner_is_refused_and_nothing_runs(self):
        for raw in ("list", "close 1", "doctor --fix"):
            with self.subTest(raw=raw):
                self.assertEqual(self.run_as(raw, user="9999", chat="dm"),
                                 "This command is only available to the owner.")
        self.assertEqual(self.calls, [])

    def test_non_owner_refused_via_environ_fallback(self):
        out = self.run_as("list", user="9999", chat="group", via_gateway=False)
        self.assertEqual(out, "This command is only available to the owner.")
        self.assertEqual(self.calls, [])

    def test_owner_is_allowed(self):
        self.assertEqual(self.run_as("list", user=OWNER, chat="dm"), "ok")
        self.assertEqual(len(self.calls), 1)

    def test_owner_allowed_via_environ_fallback(self):
        self.assertEqual(self.run_as("list", user=OWNER, chat="dm", via_gateway=False), "ok")

    def test_no_session_user_keeps_working(self):
        self.assertEqual(self.run_as("list"), "ok")
        self.assertEqual(self.run_as("list", via_gateway=False), "ok")
        self.assertEqual(len(self.calls), 2)

    def test_unconfigured_owner_refuses_a_chat_user(self):
        (self.home / "plugin-data" / "hermes-autonomous" / "settings.json").unlink()
        self.assertEqual(self.run_as("list", user=OWNER, chat="dm"),
                         "This command is only available to the owner.")
        self.assertEqual(self.calls, [])

    def test_owner_from_autonomy_env(self):
        with mock.patch.dict(os.environ, {"AUTONOMY_OWNER_ID": "777"}):
            self.assertEqual(self.run_as("list", user="777", chat="dm"), "ok")
            self.assertEqual(self.run_as("list", user=OWNER, chat="dm"),
                             "This command is only available to the owner.")

    def test_writes_outside_dm_say_where_the_output_goes(self):
        note = "Note: this command's output is shown in the current chat."
        for raw in ("close 1", "checked 1", "outcome 1 --value accepted",
                    "add --key k", "doctor --fix"):
            with self.subTest(raw=raw):
                out = self.run_as(raw, user=OWNER, chat="group")
                self.assertEqual(out.splitlines()[-1], note)
                self.assertTrue(out.startswith("ok"))
        for raw, chat in (("close 1", "dm"), ("list", "group"), ("doctor", "thread"),
                          ("why", "group")):
            with self.subTest(raw=raw, chat=chat):
                self.assertEqual(self.run_as(raw, user=OWNER, chat=chat), "ok")
        self.assertEqual(self.run_as("close 1"), "ok")  # CLI: no chat type, no note


class ApplyValidation(Home):
    """P2-B: the extra checks apply makes; every rejection writes nothing."""

    def setUp(self):
        super().setUp()
        self.setup_owner()
        self.a, self.b, self.c, self.d = (self.add(k) for k in ("a", "b", "c", "d"))
        self.apply({"watermark": WM0})
        self.before = self.snapshot()

    def iso(self, offset_s: float) -> str:
        return datetime.fromtimestamp(time.time() + offset_s, timezone.utc).isoformat()

    def rejected(self, doc: dict, needle: str):
        doc.setdefault("watermark", 1000.0)
        out = self.apply(doc, ok=False)
        self.assertIn(needle, out["error"])
        self.assertEqual(self.snapshot(), self.before)

    def nf(self, **over) -> dict:
        f = {"key": "n", "why": "care", "kind": "k", "subject": "s", "revisit_at": "+1d"}
        f.update(over)
        return f

    # 1. watermark
    def test_watermark_cannot_move_backwards(self):
        self.rejected({"watermark": WM0 - 0.5}, "watermark would move backwards")

    def test_watermark_may_stay_or_advance(self):
        self.apply({"watermark": WM0})
        self.apply({"watermark": WM0 + 500})
        out = self.apply({"watermark": WM0 + 400}, ok=False)
        self.assertIn("watermark would move backwards", out["error"])

    # 2. times in the future
    def test_revisit_at_in_the_past(self):
        self.rejected({"new_followups": [self.nf(revisit_at=self.iso(-3600))]}, "is in the past")

    def test_revisit_at_now_is_allowed(self):
        out = self.apply({"new_followups": [self.nf(revisit_at="now")], "watermark": WM0})
        self.assertTrue(out["ok"])

    def test_snooze_must_be_strictly_future(self):
        for snooze in (self.iso(-60), time.time() - 1):
            with self.subTest(snooze=snooze):
                self.rejected({"decisions": [{"id": self.a, "action": "silent", "reason": "r",
                                              "snooze": snooze}]}, "is not in the future")

    # 3. why
    def test_why_must_be_a_known_category(self):
        self.rejected({"new_followups": [self.nf(why="love")]},
                      "why must be care|utility|risk|curiosity")

    def test_why_is_normalised_to_lowercase(self):
        out = self.apply({"new_followups": [self.nf(why=" Curiosity ")], "watermark": WM0})
        self.assertEqual(self.row(out["added"][0])["why"], "curiosity")

    def test_add_checks_and_lowercases_why(self):
        p = self.fu("add", "--key", "x", "--kind", "k", "--why", "love", "--subject", "s",
                    "--revisit-at", "+1d", ok=False)
        self.assertEqual(p.returncode, 2)
        self.assertIn("why must be care|utility|risk|curiosity", json.loads(p.stdout)["error"])
        self.assertEqual(self.snapshot(), self.before)
        fid = json.loads(self.fu("add", "--key", "x", "--kind", "k", "--why", "RISK",
                                 "--subject", "s", "--revisit-at", "+1d").stdout)["id"]
        self.assertEqual(self.row(fid)["why"], "risk")

    # 4. sent
    def test_sent_at_most_three_distinct_ids(self):
        self.rejected({"sent": {"ids": [self.a, self.b, self.c, self.d], "msg_id": "1"}},
                      "at most 3")

    def test_sent_duplicate_ids_are_collapsed(self):
        out = self.apply({"sent": {"ids": [self.a, self.a, self.b, self.a], "msg_id": "1"},
                          "watermark": WM0})
        self.assertEqual(out["sent"], [self.a, self.b])
        self.assertEqual(self.row(self.a)["sends"], 1)

    def test_sent_id_must_be_active(self):
        self.fu("close", str(self.a))
        self.before = self.snapshot()
        self.rejected({"sent": {"id": self.a, "msg_id": "1"}}, f"followup {self.a} is closed, not active")

    def test_sent_id_must_exist(self):
        self.rejected({"sent": {"id": 999, "msg_id": "1"}}, "no followup 999")

    def test_sent_text_at_most_six_lines(self):
        self.rejected({"sent": {"id": self.a, "msg_id": "1", "text": "l\n\n" * 7}},
                      "7 lines; at most 6")
        self.assertTrue(self.apply({"sent": {"id": self.a, "msg_id": "1", "text": "l\n\n" * 6},
                                    "watermark": WM0})["ok"])

    # 5. act_report / ask need a receipt
    def test_act_report_without_receipt(self):
        for action in ("act_report", "ask"):
            with self.subTest(action=action):
                self.rejected({"decisions": [{"id": self.a, "action": action, "reason": "r"}]},
                              "act_report/ask without a send receipt")

    def test_ask_with_earlier_receipt_or_receipt_in_doc(self):
        c = self.db()
        c.execute("UPDATE followups SET sent_msg_id='m-9' WHERE id=?", (self.a,))
        c.commit()
        out = self.apply({"decisions": [{"id": self.a, "action": "ask", "reason": "r"},
                                        {"id": self.b, "action": "act_report", "reason": "r"}],
                          "sent": {"id": self.b, "msg_id": "m-10"}, "watermark": WM0})
        self.assertTrue(out["ok"])

    # 6. close / outcome on a missing id
    def test_close_and_outcome_missing_id_fail(self):
        for args in (("close", "999"), ("outcome", "999", "--value", "accepted")):
            with self.subTest(args=args):
                p = self.fu(*args, ok=False)
                self.assertEqual(p.returncode, 2, p.stdout)
                self.assertEqual(json.loads(p.stdout), {"ok": False, "error": "no followup 999"})
        self.assertEqual(self.snapshot(), self.before)
        self.fu("close", str(self.a))
        self.fu("outcome", str(self.b), "--value", "accepted")


class SendTimeout(Home):
    """P2-D: a timed-out `hermes send` may have delivered: cool down, do not mark sent."""

    def setUp(self):
        super().setUp()
        self.setup_owner()
        self.a = self.add("trip")
        os.environ["HERMES_HOME"] = str(self.home)
        os.environ["PATH"] = self.env["PATH"]
        os.environ["AUTONOMY_TZ"] = self.tz
        spec = importlib.util.spec_from_file_location("followups_send_timeout", FOLLOWUPS)
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)

    def test_timeout_starts_cooldown_and_leaves_item_unsent(self):
        import argparse
        import contextlib
        import io
        body = self.home / "body.txt"
        body.write_text("hello", encoding="utf-8")

        def hang(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))

        buf = io.StringIO()
        t0 = time.time()
        with mock.patch.object(self.mod.subprocess, "run", hang), contextlib.redirect_stdout(buf):
            rc = self.mod.cmd_send(argparse.Namespace(ids=str(self.a), text_file=str(body)))
        self.assertEqual(rc, 3)
        out = json.loads(buf.getvalue())
        self.assertEqual((out["ok"], out["refused"], out["note"]),
                         (False, "timeout", "message may have been delivered"))
        r = self.row(self.a)
        self.assertEqual((r["sent_at"], r["sent_msg_id"], r["sends"]), (None, None, 0))
        last_send = float(self.db().execute("SELECT v FROM kv WHERE k='last_send'").fetchone()[0])
        self.assertAlmostEqual(last_send, t0, delta=30)
        # The cooldown now holds the next attempt instead of sending twice.
        p = self.run_cli(FOLLOWUPS, "send", "--ids", str(self.a), stdin="hello")
        self.assertEqual(p.returncode, 2)
        self.assertEqual(json.loads(p.stdout)["refused"], "cooling")


class NoAuthorPaths(unittest.TestCase):
    """P2-C: shipped code resolves the plugin from HERMES_HOME or its own location."""

    def test_cron_wrappers(self):
        for name in ("followup-wake.py", "followup-digest.py"):
            src = (ROOT / "cron" / name).read_text(encoding="utf-8")
            with self.subTest(name=name):
                self.assertNotIn('"opt"', src)
                self.assertIn('home / "plugins" / "hermes-autonomous"', src)
                self.assertIn("Path(__file__).resolve().parent.parent", src)

    def test_review_prompt_plugin_root(self):
        text = (ROOT / "prompts" / "review.md").read_text(encoding="utf-8")
        self.assertIn("PLUGIN=${PLUGIN:-${HERMES_HOME:-$HOME/.hermes}/plugins/hermes-autonomous}",
                      text)
        self.assertNotIn("opt/hermes-autonomous", text)

    def test_decide_needs_hermes_on_path(self):
        src = (ROOT / "scripts" / "decide.py").read_text(encoding="utf-8")
        self.assertIn('CHAT = shutil.which("hermes")\n', src)
        self.assertNotIn("hermes-agent/.hermes", src)
        scenario = ROOT / "scenarios" / "01-throat.json"
        env = {k: v for k, v in os.environ.items() if k != "PATH"}
        empty = tempfile.mkdtemp(prefix="empty-path-")
        self.addCleanup(shutil.rmtree, empty, True)
        env["PATH"] = empty
        p = subprocess.run([sys.executable, str(ROOT / "scripts" / "decide.py"),
                            "--scenario", str(scenario)],
                           capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("hermes not found on PATH", p.stderr)


class DecidePromptContract(Home):
    """prompts/decide.md shows the decider's output shape. Its new_followups are the
    same records the live run hands to `apply`, so the example must be accepted as is."""

    PROMPT = ROOT / "prompts" / "decide.md"

    def output_example(self) -> dict:
        text = self.PROMPT.read_text(encoding="utf-8")
        m = re.search(r"## Output\s+```json\n(.*?)\n```", text, re.S)
        self.assertIsNotNone(m, "output example not found in prompts/decide.md")
        return json.loads(m.group(1))

    def test_new_followups_example_applies(self):
        self.setup_owner()
        nf = self.output_example()["new_followups"]
        self.assertGreaterEqual(len(nf), 1)
        p = self.fu("apply", stdin=json.dumps({"new_followups": nf, "watermark": WM0}), ok=False)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        out = json.loads(p.stdout)
        self.assertTrue(out["ok"], out)
        self.assertEqual(len(out["added"]), len(nf))
        for f in nf:
            r = self.db().execute("SELECT kind, why, subject FROM followups WHERE key=?",
                                  (f["key"],)).fetchone()
            self.assertEqual(tuple(r), (f["kind"], f["why"], f["subject"]))


class WakeDigestAgree(Home):
    """wake and digest read one send_gate. For the same DB, wake's line and digest's
    mode / may_send / held_reason must say the same thing:
    review ids X ↔ may_send=true and X = the due, unheld ids;
    held R      ↔ may_send=false, every due item held, R among their reasons."""

    def setUp(self):
        super().setUp()
        self.setup_owner()

    def set_last_send(self, ago_s: float):
        c = self.db()
        c.execute("INSERT INTO kv(k,v) VALUES('last_send',?) ON CONFLICT(k) DO UPDATE SET "
                  "v=excluded.v", (str(time.time() - ago_s),))
        c.commit()

    def mute(self, why: str):
        ts = time.time() - 86400
        c = self.db()
        c.execute("INSERT INTO followups(key,kind,why,subject,status,sent_at,sends,revisit_at,outcome)"
                  " VALUES(?,?,?,?,?,?,?,?,?)", (f"m-{why}-{ts}", "k", why, "s", "closed", ts, 1,
                                               ts + 86400, "annoyed"))
        c.commit()

    def check(self) -> str:
        """Run wake and digest on the current state and assert they agree."""
        line = self.wake()
        d = json.loads(self.fu("digest").stdout)
        due = [f for f in d["followups"] if f["due"]]
        free = sorted(f["id"] for f in due if not f["held"])
        reasons = {f["held_reason"] for f in due if f["held"]}
        if line.startswith("review ids "):
            self.assertTrue(d["may_send"], (line, d))
            self.assertEqual(d["mode"], "review")
            self.assertEqual(line, "review ids " + ",".join(map(str, free)))
        elif line.startswith("held "):
            self.assertFalse(d["may_send"], (line, d))
            self.assertNotEqual(d["mode"], "review")
            self.assertTrue(due and not free, (line, d))
            self.assertIn(line[len("held "):], reasons)
        elif line == "capture":
            self.assertFalse(d["may_send"], (line, d))
            self.assertEqual(free, [])
        else:
            self.assertEqual(line, "quiet")
            self.assertFalse(d["may_send"], (line, d))
            self.assertEqual(free, [])
            self.assertEqual(d["mode"], "quiet")
        # And the other direction: may_send=true only ever pairs with a review line.
        self.assertEqual(d["may_send"], line.startswith("review ids "), (line, d))
        return line

    def test_nothing(self):
        self.assertEqual(self.check(), "quiet")

    def test_not_yet_due(self):
        self.add("later", revisit="+5h")
        self.assertEqual(self.check(), "quiet")

    def test_due(self):
        a, b = self.add("a"), self.add("b", why="utility")
        self.add("later", revisit="+5h")
        self.assertEqual(self.check(), f"review ids {a},{b}")

    def test_cooling(self):
        self.add("a")
        self.set_last_send(10 * 60)
        self.assertEqual(self.check(), "held cooling")
        self.set_last_send(91 * 60)
        self.assertTrue(self.check().startswith("review ids "))

    def test_muted_partial_then_full(self):
        self.add("c", why="care")
        u = self.add("u", why="utility")
        self.mute("care")
        self.assertEqual(self.check(), f"review ids {u}")
        self.mute("utility")
        self.assertEqual(self.check(), "held muted")

    def test_cooling_and_muted_mixed(self):
        self.add("c", why="care")
        self.add("u", why="utility")
        self.mute("care")
        self.set_last_send(10 * 60)
        self.assertEqual(self.check(), "held cooling")

    def test_night(self):
        self.add("a")
        tz = tz_at_hour(23)
        self.fu("setup", "--timezone", tz)
        line = self.check()
        if local_hour(tz) == 23:  # else the hour rolled over mid-test; check() still held
            self.assertEqual(line, "held night")

    def test_already_sent_is_not_due(self):
        fid = self.add("a")
        c = self.db()
        now = time.time()
        c.execute("UPDATE followups SET sent_at=?, sent_msg_id='m-1', revisit_at=? WHERE id=?",
                  (now, now + 86400, fid))
        c.commit()
        self.assertEqual(self.check(), "quiet")

    def test_capture_while_held(self):
        self.add("a")
        self.set_last_send(10 * 60)
        now = time.time()
        c = self.db()
        c.execute("INSERT INTO kv(k,v) VALUES('last_capture',?)", (str(now - 3 * 3600),))
        c.execute("INSERT INTO watermark(surface,last_ts) VALUES('messages',?)", (now - 3600,))
        c.commit()
        self.state_db([(now - 30, "user", "new message", OWNER)])
        self.assertEqual(self.check(), "capture")
