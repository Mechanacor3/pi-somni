#!/usr/bin/env python3
"""Recall, partial dreams, batch recording, limits, the dream loop and the STATE-Bench adapter."""
import base64
import io
import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from somni import dream as dm  # noqa: E402
from somni.store import SomniError, SomniMemory, _recent  # noqa: E402

PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGMInhkAAAJ/AT1GZIqVAAAAAElFTkSuQmCC"


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

    def test_exact_run_tag_wins_over_shared_terms(self):
        self.memory.set_phase("open")
        for world, salience in (("world A", 5), ("world B", 1)):
            ref = self.memory.record("ordinary_observation", "look around", context={"tags": [world]})
            self.memory.add_note("look for landmarks", "uncertain", [{"ref": ref}], salience)
        self.assertEqual(self.memory.recall("world B", 1)[0]["tags"], ["world B"])

    def test_injection_skips_an_oversized_claim_and_uses_available_space(self):
        self.memory.set_phase("open")
        ref = self.memory.record("tool_result", "Evidence for all three claims.")
        evidence = [{"ref": ref}]
        self.memory.add_note("長" * 300, "uncertain", evidence, 5)
        self.memory.add_note("Keep the receipt.", "uncertain", evidence, 4)
        self.memory.add_note("Check the date.", "uncertain", evidence, 3)
        index = self.memory.inject(256)
        self.assertLessEqual(len(index.encode("utf-8")), 256)
        self.assertNotIn("n000001", index)
        self.assertIn("Keep the receipt.", index)
        self.assertIn("Check the date.", index)

    def test_injection_breaks_lexical_ties_with_recent_evidence(self):
        self.memory.set_phase("open")
        older, newer = self.memory.record_many([("tool_result", "Receipt"), ("tool_result", "New receipt")])
        new_claim = self.memory.add_note("Keep the receipt.", "uncertain", [{"ref": newer}])
        old_claim = self.memory.add_note("Keep the receipt.", "uncertain", [{"ref": older}])
        index = self.memory.inject(query="How should I handle the receipt?")
        self.assertLess(index.index(new_claim), index.index(old_claim))

    def test_dream_gist_is_bounded_and_full_text_survives_reload(self):
        self.memory.set_phase("open")
        ref = self.memory.record("tool_result", "The original payment method received the refund.")
        full = "Refund to the original payment method and check the settlement record."
        self.memory.set_phase("closed")
        add = {"op": "add", "text": full, "status": "uncertain", "evidence": [{"ref": ref}]}
        gist = "é" * 80
        dream = self.memory.dream({"schema": 1, "operations": [
            {**add, "gist": gist + "x"}, {**add, "gist": "Two\nlines"}, {**add, "gist": gist},
        ]}, partial=True)
        self.assertEqual([op["index"] for op in dream["rejected"]], [0, 1])
        memory = SomniMemory(self.ws, "s").load()
        self.assertIn(gist, memory.inject())
        self.assertNotIn(full, memory.inject())
        self.assertEqual(memory.recall("settlement")[0]["text"], full)

    def test_images_are_deduplicated_and_bound_to_cited_records(self):
        memory = self.memory
        memory.set_phase("open")
        image = {"mimeType": "image/png", "data": PNG}
        first = memory.record("tool_result", "screenshot", images=[image],
                              context={"run_label": "task A", "tags": ["forest"]})
        # The budget is full after one image, but the same content can still be referenced.
        with patch("somni.store.MAX_IMAGE_STORE_BYTES", len(base64.b64decode(PNG))):
            second = memory.record("tool_result", "again", images=[image],
                                   context={"run_label": "task B", "tags": ["desert"]})
            missing = memory.record("tool_result", "oversized or disabled", images=[image], image_bytes=0)
        self.assertEqual(len(list((memory.store / "images").iterdir())), 1)
        self.assertEqual(memory.transcript[2]["omitted_images"], 1)
        digest = memory.transcript[0]["images"][0]["sha256"]
        self.assertEqual(memory.transcript[1]["images"][0]["sha256"], digest)
        memory.set_phase("closed")
        evidence = [{"ref": first, "image_sha256": digest}, {"ref": second, "image_sha256": digest}]
        operation = {"op": "add", "text": "The two screenshots match.", "status": "verified"}
        proposal = {"schema": 1, "operations": [{**operation, "evidence": evidence}]}
        with self.assertRaisesRegex(SomniError, "view the cited image"):
            memory.dream(proposal, viewed_images=set())
        with self.assertRaisesRegex(SomniError, "match an image"):
            memory.dream({"schema": 1, "operations": [{**operation, "evidence": [{"ref": missing, "image_sha256": digest}]}]})
        viewed = set()
        attachments = _recent(memory, True, viewed)["attachments"]
        self.assertEqual({item["ref"] for item in attachments}, {first, second})
        memory.dream(proposal, viewed_images=viewed)
        self.assertEqual(memory.claims["n000001"]["run_labels"], ["task A", "task B"])
        self.assertEqual(memory.claims["n000001"]["tags"], ["desert", "forest"])
        blob = next((memory.store / "images").iterdir())
        blob.write_bytes(b"!" * blob.stat().st_size)
        with self.assertRaisesRegex(SomniError, "does not match its transcript digest"):
            memory.image(first, digest)

    def test_optional_downscale_stores_a_bounded_thumbnail(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("optional Pillow dependency is unavailable")
        original = io.BytesIO()
        Image.new("RGB", (120, 80), "green").save(original, "PNG")
        data = base64.b64encode(original.getvalue()).decode()
        self.memory.set_phase("open")
        ref = self.memory.record("tool_result", "frame", images=[{"mimeType": "image/png", "data": data}],
                                 image_bytes=1024, resize=(Image, 24))
        descriptor = self.memory.transcript[0]["images"][0]
        image = self.memory.image(ref, descriptor["sha256"])
        with Image.open(io.BytesIO(base64.b64decode(image["data"]))) as thumbnail:
            self.assertEqual(thumbnail.size, (24, 16))
            self.assertEqual(thumbnail.format, "JPEG")

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
