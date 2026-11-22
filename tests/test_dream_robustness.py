#!/usr/bin/env python3
"""Dreams that don't waste turns: digest prefixes, mismatch hints, the image cap, lenient image evidence,
and rejections that say what to resubmit."""
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from somni.store import SomniError, SomniMemory, _handle_request, _recent  # noqa: E402

PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGMInhkAAAJ/AT1GZIqVAAAAAElFTkSuQmCC"


class DreamRobustness(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ws = pathlib.Path(tmp.name)
        self.m = SomniMemory.create(self.ws, "s", "l", "a")
        self.m.set_phase("open")
        image = {"mimeType": "image/png", "data": PNG}
        self.shot = self.m.record("tool_result", "screenshot of the forest", images=[image])
        self.again = self.m.record("tool_result", "screenshot of the desert", images=[image])
        self.plain = self.m.record("tool_result", "no image here")
        self.m.set_phase("closed")
        self.digest = self.m.load().transcript[0]["images"][0]["sha256"]

    def request(self, method, args, viewed):
        return _handle_request(self.m, method, args, writable=True, viewed=viewed)

    def claim(self, evidence, status="verified"):
        return {"op": "add", "text": "The screenshot shows a forest.", "status": status, "evidence": evidence}

    def test_a_unique_digest_prefix_is_accepted_and_stored_in_full(self):
        viewed = set()
        _recent(self.m, True, viewed)
        self.m.dream({"schema": 1, "operations": [self.claim([{"ref": self.shot, "image_sha256": self.digest[:10]}])]},
                     viewed_images=viewed)
        self.assertEqual(self.m.claims["n000001"]["evidence"][0]["image_sha256"], self.digest)
        out = self.request("image", {"ref": self.shot, "sha256": self.digest[:8].upper(), "resolve": True}, set())
        self.assertEqual(out["sha256"], self.digest)
        with self.assertRaisesRegex(SomniError, "has no image '1234'"):  # too short to be a prefix
            self.request("image", {"ref": self.shot, "sha256": "1234"}, set())

    def test_a_wrong_digest_lists_the_digests_the_record_has(self):
        with self.assertRaisesRegex(SomniError, f"{self.shot} has no image 'f{{16}}\\.\\.\\.'; its image_sha256 values are: {self.digest}"):
            self.m.dream({"schema": 1, "operations": [self.claim([{"ref": self.shot, "image_sha256": "f" * 64}])]})
        with self.assertRaisesRegex(SomniError, f"{self.plain} has no stored images"):
            self.m.dream({"schema": 1, "operations": [self.claim([{"ref": self.plain, "image_sha256": self.digest}])]})

    def test_image_cap_is_configurable_and_counted(self):
        viewed = set()
        self.request("image", {"ref": self.shot, "sha256": self.digest, "during_dream": True, "max_images": 1}, viewed)
        self.assertEqual(self.request("dream_images", {}, viewed), 1)
        # The same image again is free; a new one is past the cap, and the message says what to do next.
        self.request("image", {"ref": self.shot, "sha256": self.digest, "during_dream": True, "max_images": 1}, viewed)
        with self.assertRaisesRegex(SomniError, r"image limit \(1 images\)\. Cite the images you have already viewed"):
            self.request("image", {"ref": self.again, "sha256": self.digest, "during_dream": True, "max_images": 1}, viewed)
        with self.assertRaisesRegex(SomniError, "max_images must be 0-64"):
            self.request("image", {"ref": self.again, "sha256": self.digest, "during_dream": True, "max_images": 99}, viewed)

    def test_lenient_image_evidence_keeps_the_ref_and_records_the_downgrade(self):
        viewed = set()  # nothing viewed: the image half of each citation is unusable
        operations = [self.claim([{"ref": self.shot, "image_sha256": self.digest}]),
                      self.claim([{"ref": self.again, "image_sha256": "e" * 64, "quote": "desert"}])]
        with self.assertRaisesRegex(SomniError, "nothing committed"):
            self.m.dream({"schema": 1, "operations": operations}, partial=True, viewed_images=viewed)
        dream = self.m.dream({"schema": 1, "operations": operations}, partial=True, viewed_images=viewed,
                             lenient_images=True)
        first, second = self.m.claims["n000001"], self.m.claims["n000002"]
        self.assertEqual((first["status"], first["evidence"]), ("uncertain", [{"ref": self.shot}]))
        self.assertEqual((second["status"], second["evidence"]), ("verified", [{"ref": self.again, "quote": "desert"}]))
        downgrades = SomniMemory(self.ws, "s").load().dreams[-1]["image_downgrades"]
        self.assertEqual([(d["index"], d["ref"]) for d in downgrades], [(0, self.shot), (1, self.again)])
        self.assertEqual(downgrades[0]["status"], "verified -> uncertain")
        self.assertIn("not viewed", downgrades[0]["reason"])
        self.assertIn("has no image", downgrades[1]["reason"])
        self.assertEqual(dream["image_downgrades"], downgrades)

    def test_an_all_rejected_submission_says_why_per_operation_and_a_fixed_one_commits(self):
        viewed = set()
        _recent(self.m, True, viewed)
        bad = [self.claim([{"ref": self.shot, "image_sha256": "a" * 64}]),
               self.claim([{"ref": self.again, "image_sha256": "b" * 64}])]
        with self.assertRaises(SomniError) as caught:
            self.request("dream", {"proposal_json": json.dumps({"schema": 1, "operations": bad})}, viewed)
        message = str(caught.exception)
        self.assertIn("nothing committed: all 2 operation(s) were rejected.", message)
        self.assertIn(f"op 0 (add): {self.shot} has no image", message)
        self.assertIn(f"op 1 (add): {self.again} has no image", message)
        self.assertIn(self.digest, message)  # the fix is in the error
        fixed = [self.claim([{"ref": self.shot, "image_sha256": self.digest}])]
        out = self.request("dream", {"proposal_json": json.dumps({"schema": 1, "operations": fixed})}, viewed)
        self.assertEqual((out["added_count"], out["rejected_count"]), (1, 0))

    def test_malformed_proposals_say_what_shape_is_expected(self):
        with self.assertRaisesRegex(SomniError, r"not valid JSON .*nothing committed"):
            self.request("dream", {"proposal_json": '{"schema": 1, "operations": ['}, set())
        with self.assertRaisesRegex(SomniError, r'exactly \{"schema": 1, "operations": \[\.\.\.\]\}; got keys ops, schema'):
            self.request("dream", {"proposal_json": '{"schema": 1, "ops": []}'}, set())


if __name__ == "__main__":
    unittest.main()
