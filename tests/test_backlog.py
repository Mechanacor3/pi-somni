#!/usr/bin/env python3
"""A backlog: more unreviewed records than one dream takes. Nights and daydreams work through it oldest first."""
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from somni.store import DAYDREAM_OVERLAP, SomniError, SomniMemory, _handle_request  # noqa: E402


class BacklogTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ws = pathlib.Path(tmp.name)
        self.m = SomniMemory.create(self.ws, "s", "l", "a")
        self.m.set_phase("open")
        self.ids = self.m.record_many([("ordinary_observation", f"USER: note number {i}") for i in range(30)])
        self.m.set_phase("closed")

    def request(self, method, **args):
        return _handle_request(self.m, method, args, writable=True, viewed=set())

    def recent(self, limit=12):
        return self.request("recent", record_limit=limit, backlog=True)

    def dream(self, through=None, ref=None):
        proposal = {"schema": 1, "operations": [] if ref is None else [
            {"op": "add", "text": f"Lesson from {ref}.", "status": "uncertain", "salience": 2, "evidence": [{"ref": ref}]}]}
        return self.request("dream", proposal_json=json.dumps(proposal), **({"through": through} if through else {}))

    def test_a_night_reviews_the_oldest_unreviewed_part_first_and_binds_its_pass_there(self):
        first = self.recent()
        self.assertEqual(([r["id"] for r in first["records"]][:2], first["through"], first["remaining"]),
                         (self.ids[:2], 12, 18))
        self.assertEqual(len(first["records"]), 12)
        pass1 = self.dream(first["through"], self.ids[3])
        self.assertEqual((pass1["transcript_count"], pass1.get("backlog"), pass1.get("live")), (12, True, None))
        with self.assertRaisesRegex(SomniError, "evidence reference"):
            self.dream(first["through"], self.ids[20])  # a part's pass may cite only records up to its end
        second = self.recent()
        self.assertEqual(second["records"][0]["id"], self.ids[12 - DAYDREAM_OVERLAP])  # a little context before
        self.assertEqual(second["reviewed_through"], self.ids[12 - DAYDREAM_OVERLAP - 1])
        self.assertEqual((second["through"], second["remaining"]), (24, 6))
        self.dream(second["through"])
        last = self.recent()  # what is left fits in one pass: an ordinary night, bound to the head
        self.assertNotIn("through", last)
        self.assertEqual(self.dream()["transcript_count"], 30)
        self.assertEqual(self.m.load().day_lessons(), [])  # backlog passes are not daydreams
        self.assertEqual(self.request("dream_status")["undreamed"], 0)

    def test_a_record_cited_twice_keeps_its_first_citation_and_both_quotes_must_hold(self):
        twice = {"op": "add", "text": "Notes are numbered.", "status": "verified", "salience": 2,
                 "evidence": [{"ref": self.ids[0], "quote": "note number"}, {"ref": self.ids[0], "quote": "number 0"}]}
        self.request("dream", proposal_json=json.dumps({"schema": 1, "operations": [twice]}))
        self.assertEqual(self.m.load().claims["n000001"]["evidence"], [{"ref": self.ids[0], "quote": "note number"}])
        bad = {**twice, "evidence": [{"ref": self.ids[0], "quote": "note number"}, {"ref": self.ids[0], "quote": "nope"}]}
        with self.assertRaisesRegex(SomniError, f"quote for {self.ids[0]} must occur exactly"):
            self.request("dream", proposal_json=json.dumps({"schema": 1, "operations": [bad]}))
        with self.assertRaisesRegex(SomniError, "'t999999' is not in the transcript"):
            self.request("dream", proposal_json=json.dumps({"schema": 1, "operations": [
                {**twice, "evidence": [{"ref": "t999999"}], "status": "uncertain"}]}))

    def test_status_counts_the_users_unreviewed_turns_and_when_the_last_dream_ran(self):
        status = self.request("dream_status")
        self.assertEqual((status["undreamed"], status["undreamed_turns"], status["last_at"]), (30, 30, None))
        self.dream(self.recent()["through"])
        self.assertEqual(self.request("dream_status")["undreamed_turns"], 18)
        self.assertIsNotNone(self.request("dream_status")["last_at"])
        self.assertEqual(self.m.load().status()["undreamed"], 18)

    def test_a_daydream_facing_a_backlog_reviews_its_oldest_part(self):
        proc = subprocess.run([sys.executable, "-I", str(ROOT / "somni/store.py"), "--workspace", str(self.ws),
                               "--subject-id", "s", "serve", "--daydream"], input="", capture_output=True, text=True,
                              timeout=30, env={"SOMNI_DREAM_RECORDS": "12", "PATH": "/usr/bin:/bin"})
        ready = json.loads(proc.stdout.splitlines()[0])["result"]
        self.assertEqual(ready["daydream_head"], 30)  # the store's own window is 120: no backlog yet
        self.m.set_phase("open")
        self.m.record_many([("ordinary_observation", f"USER: more {i}") for i in range(130)])
        self.m.set_phase("closed")
        proc = subprocess.run([sys.executable, "-I", str(ROOT / "somni/store.py"), "--workspace", str(self.ws),
                               "--subject-id", "s", "serve", "--daydream"], input="", capture_output=True, text=True,
                              timeout=30, env={"PATH": "/usr/bin:/bin"})
        self.assertEqual(json.loads(proc.stdout.splitlines()[0])["result"]["daydream_head"], 120)


if __name__ == "__main__":
    unittest.main()
