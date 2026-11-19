#!/usr/bin/env python3
"""Recall, partial dreams, batch recording, limits, the dream loop and the STATE-Bench adapter."""
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from somni import dream as dm  # noqa: E402
from somni.store import SomniError, SomniMemory  # noqa: E402


class FeatureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = pathlib.Path(self.tmp.name)
        self.memory = SomniMemory.create(self.ws, "s", "l", "a", limits={"max_transcript_records": 5})

    def tearDown(self):
        self.tmp.cleanup()

    def test_record_many_is_atomic_and_respects_store_limits(self):
        self.memory.set_phase("open")
        ids = self.memory.record_many([("ordinary_observation", "one"), ("tool_result", "two")])
        self.assertEqual(ids, ["t000001", "t000002"])
        with self.assertRaisesRegex(SomniError, "record limit"):
            self.memory.record_many([("agent_output", str(i)) for i in range(4)])
        self.assertEqual(len(SomniMemory(self.ws, "s").load().transcript), 2)
        (self.ws / "x").mkdir()
        with self.assertRaises(SomniError):
            SomniMemory.create(self.ws / "x", "s", "l", "a", limits={"max_claims": 10**9})
        self.assertFalse((self.ws / "x" / "somni-v1").exists())  # nothing half-created

    def test_partial_dream_keeps_valid_ops_and_logs_rejections(self):
        self.memory.set_phase("open")
        self.memory.record("tool_result", "cancel_booking with confirm=false returned status preview")
        self.memory.set_phase("closed")
        good = {"op": "add", "text": "Unconfirmed cancels only preview.", "status": "verified", "salience": 5,
                "evidence": [{"ref": "t000001", "quote": "returned status preview"}]}
        bad = {**good, "evidence": [{"ref": "t000001", "quote": "paraphrased"}]}
        with self.assertRaises(SomniError):
            self.memory.dream({"schema": 1, "operations": [good, bad]})
        record = self.memory.dream({"schema": 1, "operations": [good, bad]}, partial=True)
        self.assertEqual(record["operation_count"], 1)
        self.assertEqual(record["rejected"][0]["index"], 1)
        with self.assertRaisesRegex(SomniError, "no valid operations"):
            self.memory.dream({"schema": 1, "operations": [bad]}, partial=True)
        self.assertEqual(SomniMemory(self.ws, "s").load().dreams[0]["rejected"], record["rejected"])

    def test_recall_ranks_by_query_then_salience(self):
        self.memory.set_phase("open")
        ref = self.memory.record("ordinary_observation", "evidence")
        for text, salience in (("Refunds go to the original payment method.", 2),
                               ("Quote the cancellation fee and wait for confirmation.", 5),
                               ("Cancellation fees for basic economy are not refundable.", 1)):
            self.memory.add_note(text, "uncertain", [{"ref": ref}], salience)
        top = self.memory.recall("what cancellation fee applies", 2)
        self.assertEqual([c["id"] for c in top], ["n000002", "n000003"])
        self.assertEqual(self.memory.recall("zebra", 3), [])

    def test_dream_pass_retries_with_reasons_and_never_raises(self):
        self.memory.set_phase("open")
        self.memory.record("tool_result", "fee is 50")
        self.memory.set_phase("closed")
        calls = []

        def complete(system, prompt):
            calls.append(prompt)
            if len(calls) == 1:
                return json.dumps({"operations": [{"op": "add", "text": "x", "status": "verified",
                                                   "evidence": [{"ref": "t000001", "quote": "nope"}]}]})
            return json.dumps({"operations": [{"op": "add", "text": "Fees are quoted.", "status": "verified",
                                               "salience": 4, "evidence": [{"ref": "t000001", "quote": "fee is 50"}]}]})
        trace = dm.dream_pass(self.memory, self.memory.load().transcript, complete)
        self.assertEqual([a["accepted"] for a in trace["attempts"]], [0, 1])
        self.assertIn("rejected", calls[1])
        def refuse(system, prompt):
            raise RuntimeError("HTTP 400: prompt flagged")
        self.assertIn("model_error", dm.dream_pass(self.memory.load(), self.memory.transcript, refuse)["attempts"][0])
        trace = dm.dream_pass(self.memory.load(), self.memory.transcript, lambda s, p: "not json")
        self.assertEqual(trace["attempts"][-1]["accepted"], 0)


if __name__ == "__main__":
    unittest.main()
