#!/usr/bin/env python3
"""Outcomes: evidence from outside the agent, recorded afterwards, paired with the agent report it judges."""
import base64
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from somni import dream as dm  # noqa: E402
from somni import store as S  # noqa: E402
from somni.store import SomniError, SomniMemory, _handle_request, _recent  # noqa: E402

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGD4DwABBAEAwS2OUAAAAABJRU5ErkJggg==")


class OutcomeTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ws = pathlib.Path(tmp.name)
        self.m = SomniMemory.create(self.ws, "s", "l", "a")

    def session(self, *items, run_label=None):
        self.m.set_phase("open", allow_same=True)
        ids = self.m.record_many(list(items), context={"run_label": run_label} if run_label else None)
        self.m.set_phase("closed")
        return ids

    def reload(self):
        return SomniMemory(self.ws, "s").load()

    def test_an_outcome_is_recorded_after_the_session_dated_chained_and_labelled(self):
        self.session(("agent_output", "AGENT: DONE — placed four dirt blocks"), run_label="try-1")
        with patch.dict(os.environ, {"SOMNI_NOW": "2026-03-01T10:00:00Z"}):
            ref = self.m.record_outcome("no line of four", by="grader", verdict="fail", run_label="try-1")
        record = self.reload().transcript[-1]  # reloading verifies the hash chain
        self.assertEqual((record["id"], record["source"], record["at"], record["run_label"]),
                         (ref, "outcome", "2026-03-01T10:00:00Z", "try-1"))
        self.assertEqual(record["outcome"], {"by": "grader", "verdict": "fail"})
        self.assertEqual(self.m.state["phase"], "closed")
        self.m.set_phase("open")  # an outcome can also arrive while a session is open
        self.m.record_outcome("CI is green again")
        self.assertEqual(self.reload().transcript[-1]["outcome"], {})

    def test_outcome_fields_are_checked_and_only_record_outcome_writes_one(self):
        for kwargs, message in (({"verdict": "maybe"}, "verdict must be one of"), ({"by": "x" * 41}, "up to 40 bytes"),
                                ({"run_label": "\n"}, "run label")):
            with self.assertRaisesRegex(SomniError, message):
                self.m.record_outcome("text", **kwargs)
        self.m.set_phase("open")
        with self.assertRaisesRegex(SomniError, "record_outcome"):
            self.m.record("outcome", "forged")
        with self.assertRaisesRegex(SomniError, "outcomes come from outside the agent"):
            _handle_request(self.m, "record", {"source": "outcome", "text": "forged"}, writable=True)
        with self.assertRaisesRegex(SomniError, "empty"):
            self.m.record_outcome("   ")

    def test_an_outcome_pairs_with_the_report_it_judges(self):
        self.session(("agent_output", "AGENT: Placing blocks now."), ("tool_result", "placed 1"),
                     ("agent_output", "AGENT: DONE — placed four dirt blocks in a straight line"),
                     ("agent_output", "AGENT: Anything else?"), run_label="try-1")
        self.session(("agent_output", "AGENT: DONE with something else"), run_label="try-2")
        ref = self.m.record_outcome("no line of four", by="grader", verdict="fail", run_label="try-1")
        report = self.m.outcome_report(self.m._records_by_id[ref])
        self.assertEqual(report["ref"], "t000003")  # the success claim in its own run, not the last line or try-2
        self.assertTrue(report["claimed_success"] and report["overclaim"])
        # A second outcome on the same run judges the same report; a pass is not an overclaim.
        second = self.m.record_outcome("reviewer: gap at x=3", by="reviewer", run_label="try-1")
        self.assertEqual(self.m.outcome_report(self.m._records_by_id[second])["ref"], "t000003")
        self.session(("agent_output", "AGENT: I could not finish."), run_label="try-1")
        third = self.m.record_outcome("not done", verdict="fail", run_label="try-1")
        report = self.m.outcome_report(self.m._records_by_id[third])
        self.assertEqual((report["ref"], report["claimed_success"], report["overclaim"]), ("t000008", False, False))
        lone = self.m.record_outcome("an outcome with no run")
        self.assertIsNone(self.m.outcome_report(self.m._records_by_id[lone]))

    def test_dream_listings_mark_outcomes_and_put_an_overclaim_beside_its_report(self):
        self.session(("agent_output", "AGENT: DONE — placed four"), run_label="try-1")
        self.m.record_outcome("no line of four", by="grader", verdict="fail", run_label="try-1")
        window = _recent(self.reload(), False, set())["records"]
        self.assertEqual(window[-1]["report"]["ref"], "t000001")
        listing = dm.render_records(window)
        self.assertIn("[t000002] (OUTCOME, from outside the agent; by grader; verdict fail) [run: try-1] no line of four\n"
                      "    judges the agent's report [t000001]: AGENT: DONE — placed four\n"
                      "    The agent reported success; this outcome says it failed.", listing)
        prompts = []
        dm.dream_pass(self.m, window, lambda system, prompt: prompts.append((system, prompt)) or '{"operations": []}')
        self.assertIn("OUTCOME records come from outside the agent", prompts[0][0])

    def test_a_claim_citing_an_outcome_is_verified_counted_and_marked_in_the_index_and_recall(self):
        self.session(("agent_output", "AGENT: DONE — placed four"), run_label="try-1")
        ref = self.m.record_outcome("no line of four", by="grader", verdict="fail", run_label="try-1")
        index = self.m.inject(4096)
        self.assertIn("Outcomes since the last dream", index)
        self.assertIn(f"- {ref} [grader; fail; run: try-1] no line of four (the agent had reported success in t000001)", index)
        self.m.dream({"schema": 1, "operations": [{
            "op": "add", "text": "Before saying a line is done, look at the whole line: the counter only counts placements.",
            "status": "verified", "salience": 4,
            "evidence": [{"ref": "t000001", "quote": "DONE — placed four"}, {"ref": ref, "quote": "no line of four"}]}]})
        memory = self.reload()
        index = memory.inject(4096)
        self.assertNotIn("Outcomes since the last dream", index)
        self.assertIn("- n000001 [verified, outcome-backed; salience=4;", index)
        [hit] = memory.recall("line counter placements")
        self.assertEqual(hit["outcome_evidence"], [ref])
        self.assertEqual(memory.status()["outcomes"], {"records": 1, "by_verdict": {"fail": 1}, "overclaims": 1,
                                                       "cited_by_active_claims": 1})
        (self.ws / "empty").mkdir()
        self.assertEqual(SomniMemory.create(self.ws / "empty", "s", "l", "a").status()["outcomes"], {"records": 0})

    def test_the_command_lines_record_an_outcome_with_an_image(self):
        self.session(("agent_output", "AGENT: DONE"))
        image = self.ws / "end.png"
        image.write_bytes(PNG)
        out = io.StringIO()
        argv = ["store", "--workspace", str(self.ws), "--subject-id", "s", "outcome", "--text", "end state",
                "--verdict", "fail", "--by", "grader", "--image", str(image), "--tag", "lab"]
        with patch.object(sys, "argv", argv), redirect_stdout(out):
            self.assertEqual(S.main(), 0)
        record = self.reload().transcript[-1]
        self.assertEqual(out.getvalue().strip(), record["id"])
        self.assertEqual((record["tags"], len(record["images"])), (["lab"], 1))
        (self.ws / "detail.txt").write_text("CI: 2 tests failed\n", encoding="utf-8")
        env = {**os.environ, "SOMNI_WORKSPACE": str(self.ws), "SOMNI_SUBJECT": "s", "SOMNI_CONFIG": "none"}
        done = subprocess.run([sys.executable, str(ROOT / "bin" / "somni"), "outcome", "--text-file",
                               str(self.ws / "detail.txt"), "--by", "ci", "--verdict", "fail"],
                              capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        record = self.reload().transcript[-1]
        self.assertEqual((done.stdout.strip(), record["text"], record["outcome"]),
                         (record["id"], "CI: 2 tests failed", {"by": "ci", "verdict": "fail"}))
        missing = subprocess.run([sys.executable, str(ROOT / "bin" / "somni"), "outcome", "--text", "x"],
                                 capture_output=True, text=True, timeout=60,
                                 env={**env, "SOMNI_WORKSPACE": str(self.ws / "nowhere")})
        self.assertEqual(missing.returncode, 2)
        self.assertIn("no store", missing.stderr)

    def test_the_store_cli_records_a_harness_record_with_a_run_label_for_pairing(self):
        self.m.set_phase("open")
        base = ["store", "--workspace", str(self.ws), "--subject-id", "s", "record", "--source", "agent_output"]
        with patch.object(sys, "argv", [*base, "--text", "AGENT: DONE", "--run-label", "t-3", "--tag", "lab"]), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(S.main(), 0)
        with patch.dict(os.environ, {"SOMNI_RUN_LABEL": "t-4"}), patch.object(sys, "argv", [*base, "--text", "AGENT: done too"]), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(S.main(), 0)
        self.m.set_phase("closed")
        records = self.reload().transcript
        self.assertEqual([(r.get("run_label"), r.get("tags")) for r in records], [("t-3", ["lab"]), ("t-4", None)])
        ref = self.m.record_outcome("no line of four", verdict="fail", run_label="t-3")
        self.assertEqual(self.m.outcome_report(self.m._records_by_id[ref])["ref"], "t000001")

    def test_forgetting_keeps_an_outcome_quote_a_claim_cites(self):
        with patch.dict(os.environ, {"SOMNI_NOW": "2026-01-01T09:00:00Z"}):
            self.session(("agent_output", "AGENT: DONE"))
            ref = self.m.record_outcome("no line of four; " + "detail " * 60, verdict="fail")
            self.m.dream({"schema": 1, "operations": [{"op": "add", "text": "Check the line.", "status": "verified",
                                                       "salience": 3, "evidence": [{"ref": ref, "quote": "no line of four"}]}]})
        with patch.dict(os.environ, {"SOMNI_NOW": "2026-03-01T09:00:00Z", "SOMNI_DREAM_RECORDS": "1"}):
            self.session(("tool_result", "later"))
            self.m.dream({"schema": 1, "operations": []}, allow_empty=True)
        record = self.reload().transcript[1]
        self.assertTrue(record["text"].startswith(S.FORGOTTEN_PREFIX))
        self.assertIn("no line of four", record["text"])
        self.assertEqual(record["outcome"], {"verdict": "fail"})


if __name__ == "__main__":
    unittest.main()
