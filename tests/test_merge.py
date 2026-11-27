#!/usr/bin/env python3
"""Merging forks: two stores grown from one snapshot, brought back together the way git merges branches."""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from somni import merge as M  # noqa: E402
from somni import snapshot as N  # noqa: E402
from somni.store import SomniMemory, _handle_request  # noqa: E402


def say(memory, *texts, now=None):
    with mock.patch.dict(os.environ, {"SOMNI_NOW": now} if now else {}):
        memory.set_phase("open")
        ids = memory.record_many([("ordinary_observation", f"USER: {text}") for text in texts])
        memory.set_phase("closed")
    return ids


def learn(memory, *operations, now=None, env=None):
    with mock.patch.dict(os.environ, {**({"SOMNI_NOW": now} if now else {}), **(env or {})}):
        return memory.dream({"schema": 1, "operations": list(operations)})


def add(text, ref, quote, status="verified"):
    return {"op": "add", "text": text, "status": status, "salience": 3, "evidence": [{"ref": ref, "quote": quote}]}


class MergeTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        for name in ("a", "b"):
            (self.root / name).mkdir()
        self.a = SomniMemory.create(self.root / "a", "pi", "l", "x")
        said = say(self.a, "deploys go out from release branches", "tests run with make check")
        learn(self.a, add("Deploy from release branches.", said[0], "release branches"))
        self.base = self.root / "base.tar.gz"
        N.snapshot(self.a, self.base)
        N.restore(self.base, self.root / "b", SomniMemory)
        self.b = SomniMemory(self.root / "b", "pi").load()

    def merge_b_into_a(self, **options):
        out = self.root / f"b-{len(list(self.root.glob('b-*.tar.gz')))}.tar.gz"
        N.snapshot(self.b.load(), out)
        return M.merge(self.root / "a", "pi", out, label="b", **options)

    def test_both_sides_learned_something_and_the_merge_keeps_both(self):
        mine = say(self.a, "the staging database is called stg-main")
        learn(self.a, add("Staging's database is stg-main.", mine[0], "stg-main"))
        theirs = say(self.b, "deploys now go out from main, not release branches")
        learn(self.b, {"op": "update", "claim_id": "n000001", "text": "Deploy from main.",
                       "evidence": [{"ref": theirs[0], "quote": "deploys now go out from main"}]})
        report = self.merge_b_into_a()
        self.assertEqual((report["result"], report["base"]["records"], report["records"]["imported"]), ("merged", 2, 1))
        a = self.a.load()
        copy = a.transcript[-1]
        self.assertEqual((copy["id"], copy["imported"]["ref"], copy["text"]),
                         ("t000004", theirs[0], "USER: deploys now go out from main, not release branches"))
        self.assertEqual(a.claims["n000001"]["state"], "past")  # only they changed it: their update stands
        new = a.claims[a.claims["n000001"]["superseded_by"]]
        self.assertEqual((new["text"], new["status"], new["evidence"][0]["ref"]), ("Deploy from main.", "verified", "t000004"))
        self.assertEqual(a.claims["n000002"]["text"], "Staging's database is stg-main.")
        last = a.dreams[-1]
        self.assertEqual((last["author"], last["transcript_count"], last["merge"]["from"]), ("merge", 4, "b"))
        status = _handle_request(a, "dream_status", {}, writable=False, viewed=set())
        self.assertEqual((status["undreamed"], status["last_at"]), (0, a.dreams[-2]["at"]))  # a merge isn't a dream

        again = self.merge_b_into_a()  # nothing new over there
        self.assertEqual(again["result"], "up to date")
        more = say(self.b, "the on-call rota is in the team calendar")
        learn(self.b, add("On-call is in the team calendar.", more[0], "team calendar"))
        later = self.merge_b_into_a()
        self.assertEqual((later["records"]["imported"], later["claims"]), (1, {"add": 1}))
        texts = [c["text"] for c in self.a.load().claims.values() if c["state"] == "active"]
        self.assertEqual(sorted(texts), ["Deploy from main.", "On-call is in the team calendar.",
                                         "Staging's database is stg-main."])

    def test_a_claim_both_sides_changed_is_a_conflict_and_both_versions_stay_for_the_next_dream(self):
        mine = say(self.a, "we deploy from tags now")
        learn(self.a, {"op": "update", "claim_id": "n000001", "text": "Deploy from tags.",
                       "evidence": [{"ref": mine[0], "quote": "deploy from tags"}]})
        theirs = say(self.b, "we deploy from main now")
        learn(self.b, {"op": "update", "claim_id": "n000001", "text": "Deploy from main.",
                       "evidence": [{"ref": theirs[0], "quote": "deploy from main"}]})
        report = self.merge_b_into_a()
        conflict, = report["conflicts"]
        self.assertEqual((conflict["kind"], conflict["ours"], conflict["theirs"]), ("both_changed", "n000001", "n000001"))
        active = sorted(c["text"] for c in self.a.load().claims.values() if c["state"] == "active")
        self.assertEqual(active, ["Deploy from main.", "Deploy from tags."])
        self.assertEqual(self.a.dreams[-1]["merge"]["conflicts"][0]["imported_as"], "n000003")

    def test_a_conflict_waits_for_the_next_dream_until_one_side_is_settled(self):
        learn(self.a, {"op": "update", "claim_id": "n000001", "text": "Deploy from tags.",
                       "evidence": [{"ref": say(self.a, "we deploy from tags now")[0], "quote": "deploy from tags"}]})
        learn(self.b, {"op": "update", "claim_id": "n000001", "text": "Deploy from main.",
                       "evidence": [{"ref": say(self.b, "we deploy from main now")[0], "quote": "deploy from main"}]})
        self.merge_b_into_a()
        open_conflict, = self.a.load().merge_conflicts()
        self.assertEqual((open_conflict["ours"]["text"], open_conflict["theirs"]["text"], open_conflict["from"]),
                         ("Deploy from tags.", "Deploy from main.", "b"))
        learn(self.a, {"op": "retract", "claim_id": "n000003", "reason": "tags replaced main",
                       "evidence": [{"ref": "t000003"}]})
        self.assertEqual(self.a.load().merge_conflicts(), [])

    def test_their_retraction_of_a_shared_claim_is_taken_and_their_images_come_along(self):
        learn(self.b, {"op": "retract", "claim_id": "n000001", "reason": "we stopped using release branches",
                       "evidence": [{"ref": "t000001"}]})
        png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGMInhkAAAJ/AT1GZIqVAAAAAElFTkSuQmCC"
        self.b.set_phase("open")
        shot = self.b.record("tool_result", "screenshot of the dashboard", images=[{"mimeType": "image/png", "data": png}])
        self.b.set_phase("closed")
        learn(self.b, add("The dashboard is green.", shot, "dashboard"))
        say(self.a, "ours meanwhile")
        learn(self.a, add("Ours.", "t000003", "ours meanwhile"))
        report = self.merge_b_into_a()
        self.assertEqual(report["claims"], {"retract": 1, "add": 1})
        a = self.a.load()
        self.assertEqual((a.claims["n000001"]["state"], a.claims["n000001"]["retraction_reason"]),
                         ("retracted", "merged from b: we stopped using release branches"))
        copy = a.transcript[-1]
        self.assertEqual(a.image(copy["id"], copy["images"][0]["sha256"])["data"], png)

    def test_a_fork_merged_into_a_directory_with_no_store_becomes_that_store(self):
        say(self.b, "theirs")
        out = self.root / "b.tar.gz"
        N.snapshot(self.b.load(), out)
        report = M.merge(self.root / "fresh", "pi", out)
        self.assertEqual(report["result"], "fast-forward")
        self.assertEqual(SomniMemory(self.root / "fresh", "pi").load().state["transcript_count"], 3)

    def test_a_side_that_added_nothing_fast_forwards_to_the_other(self):
        theirs = say(self.b, "the cache lives in redis")
        learn(self.b, add("The cache is redis.", theirs[0], "redis"))
        report = self.merge_b_into_a()
        self.assertTrue(report["fast_forward"])
        a = self.a.load()
        self.assertEqual(a.state["transcript_head"], self.b.load().state["transcript_head"])
        self.assertTrue(pathlib.Path(report["kept_aside"]).is_dir())

    def test_stores_with_no_shared_history_merge_only_when_asked(self):
        (self.root / "c").mkdir()
        c = SomniMemory.create(self.root / "c", "pi", "l", "y")
        learn(c, add("Builds use bazel.", say(c, "we build with bazel")[0], "bazel"))
        say(self.a, "something new here")  # so it isn't a fast-forward
        with self.assertRaisesRegex(M.MergeError, "share no history"):
            M.merge(self.root / "a", "pi", self.root / "c")
        report = M.merge(self.root / "a", "pi", self.root / "c", unrelated=True)
        self.assertEqual((report["base"]["records"], report["records"]["imported"], report["claims"]), (0, 1, {"add": 1}))

    def test_a_recording_session_or_another_subject_stops_a_merge(self):
        say(self.a, "ours")
        say(self.b, "theirs")
        self.a.set_phase("open")
        with self.assertRaisesRegex(M.MergeError, "close Pi before merging"):
            self.merge_b_into_a()
        self.a.set_phase("closed")
        (self.root / "other").mkdir()
        SomniMemory.create(self.root / "other", "someone-else", "l", "z")
        with self.assertRaisesRegex(M.MergeError, "subject"):
            M.merge(self.root / "a", "pi", self.root / "other")

    def test_a_dry_run_reports_and_writes_nothing(self):
        say(self.a, "ours")
        say(self.b, "theirs")
        head = self.a.load().state["transcript_head"]
        report = self.merge_b_into_a(dry_run=True)
        self.assertEqual((report["result"], report["dry_run"], report["records"]["imported"]), ("merged", True, 1))
        self.assertEqual(self.a.load().state["transcript_head"], head)

    def test_undreamed_records_here_leave_the_imports_for_the_next_dream(self):
        say(self.a, "not dreamed yet")
        say(self.b, "theirs")
        learn(self.b, add("Theirs.", "t000003", "theirs"))
        report = self.merge_b_into_a()
        self.assertEqual(report["review"]["through"], 2)
        self.assertIn("dream before you merge", report["review"]["hint"])


class ForgettingTest(unittest.TestCase):
    """Forgetting wins by default; a setting makes it a conflict; keep takes the whole text back."""
    OLD, LATER = "2026-01-01T09:00:00+00:00", "2026-03-01T23:30:00+00:00"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        for name in ("a", "b"):
            (self.root / name).mkdir()
        self.a = SomniMemory.create(self.root / "a", "pi", "l", "x")
        self.long = "an old detail about the wiki migration. " + "Background that no claim needs. " * 12
        self.old = say(self.a, self.long, "another old detail about the build cache. " + "More filler. " * 20,
                       now=self.OLD)
        learn(self.a, add("The wiki migrated.", self.old[0], "wiki migration"), now=self.OLD)
        N.snapshot(self.a, self.root / "base.tar.gz")
        N.restore(self.root / "base.tar.gz", self.root / "b", SomniMemory)
        self.b = SomniMemory(self.root / "b", "pi").load()
        say(self.b, "recent work", "more recent work", now=self.LATER)
        learn(self.b, add("Recent.", "t000003", "recent work"), now=self.LATER, env={"SOMNI_DREAM_RECORDS": "2"})
        self.assertTrue(all("forgotten" in r for r in self.b.load().transcript[:2]))  # the other side forgot
        say(self.a, "ours meanwhile", now=self.LATER)
        learn(self.a, add("Ours.", "t000003", "ours meanwhile"), now=self.LATER)

    def snapshot_b(self):
        out = self.root / f"b{len(list(self.root.glob('b*.tar.gz')))}.tar.gz"
        N.snapshot(self.b.load(), out)
        return out

    def test_by_default_forgetting_wins(self):
        report = M.merge(self.root / "a", "pi", self.snapshot_b())
        self.assertEqual(report["forgetting_changes"], {"forget": 2, "restore": 0})
        a = self.a.load()
        self.assertTrue(all("forgotten" in r for r in a.transcript[:2]))
        self.assertIn("wiki migration", a.transcript[0]["text"])  # the quote a claim cites survives in the stub

    def test_as_a_conflict_it_stops_the_merge_and_says_how_to_go_on(self):
        with self.assertRaisesRegex(M.MergeError, "forgetting conflict: the other side forgot 2 .*--forgetting keep"):
            M.merge(self.root / "a", "pi", self.snapshot_b(), forgetting="conflict")

    def test_keep_takes_the_whole_text_back_from_the_side_that_has_it(self):
        N.snapshot(self.a.load(), self.root / "a-now.tar.gz")
        report = M.merge(self.root / "b", "pi", self.root / "a-now.tar.gz", forgetting="keep")
        self.assertEqual(report["forgetting_changes"], {"forget": 0, "restore": 2})
        b = self.b.load()
        self.assertEqual(b.transcript[0]["text"], f"USER: {self.long}")
        self.assertNotIn("forgotten", b.transcript[0])

    def test_the_cli_merges_and_reads_the_default_from_settings(self):
        env = {"PATH": "/usr/bin:/bin", "SOMNI_CONFIG": "none", "HOME": str(self.root),
               "SOMNI_MERGE_FORGETTING": "conflict"}
        cmd = [sys.executable, str(ROOT / "bin/somni"), "merge", str(self.snapshot_b()), "--store", str(self.root / "a"),
               "--cwd", str(self.root)]
        out = subprocess.run(cmd, capture_output=True, text=True, env=env)
        self.assertEqual(out.returncode, 2)
        self.assertIn("forgetting conflict", out.stderr)
        out = subprocess.run([*cmd, "--forgetting", "wins", "--dry-run"], capture_output=True, text=True, env=env)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout)["forgetting_changes"]["forget"], 2)


if __name__ == "__main__":
    unittest.main()
