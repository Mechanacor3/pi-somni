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

    def test_failed_dreams_are_logged_and_counted_by_cause(self):
        self.assertEqual(self.m.status()["dream_failures"], {"total": 0, "by_cause": {}, "by_kind": {}})
        for cause, kind in (("provider_error", "dream"), ("no_submission", "dream"), ("all_rejected", "wiki"),
                            ("budget_exhausted", "daydream"), ("provider_error", "daydream"), ("timeout", "daydream")):
            self.request("dream_failure", {"cause": cause, "kind": kind, "detail": f"{cause}  happened\n", "reads": 5}, set())
        status = SomniMemory(self.ws, "s").load().status()["dream_failures"]
        self.assertEqual(status["total"], 6)
        self.assertEqual(status["by_cause"], {"all_rejected": 1, "budget_exhausted": 1, "no_submission": 1,
                                              "provider_error": 2, "timeout": 1})
        self.assertEqual(status["by_kind"], {"daydream": 3, "dream": 2, "wiki": 1})
        self.assertEqual({k: status["last"][k] for k in ("cause", "kind", "detail", "reads", "transcript_count")},
                         {"cause": "timeout", "kind": "daydream", "detail": "timeout happened", "reads": 5,
                          "transcript_count": 3})
        logged = _handle_request(self.m, "dream_failure", {"cause": "other", "kind": "dream"}, writable=True,
                                 viewed=set(), context={"run_label": "task 7"})
        self.assertEqual(logged["run_label"], "task 7")
        for bad in ({"cause": "bored", "kind": "dream"}, {"cause": "other", "kind": "nap"},
                    {"cause": "other", "kind": "dream", "reads": -1}, {"cause": "other", "kind": "dream", "extra": 1}):
            with self.subTest(bad=bad), self.assertRaisesRegex(SomniError, "dream failure"):
                self.request("dream_failure", bad, set())
        # The chained logs are untouched: the failure log is diagnostics beside them.
        self.assertEqual(SomniMemory(self.ws, "s").load().state["dream_count"], 0)

    def test_the_failure_log_is_bounded_and_keeps_counting(self):
        from unittest.mock import patch
        with patch("somni.store.MAX_FAILURES_BYTES", 2000):
            for _ in range(40):
                self.request("dream_failure", {"cause": "provider_error", "kind": "dream", "detail": "x" * 40}, set())
        status = self.m.status()["dream_failures"]
        self.assertEqual(status["total"], 40)
        self.assertGreater(status["dropped"], 0)
        self.assertLessEqual((self.m.store / "dream-failures.jsonl").stat().st_size, 2000)

    def test_cached_reads_belong_to_one_attempt_on_one_set_of_records(self):
        put = lambda **kw: self.request("dream_cache_put", {"kind": "dream", "tool": "somni_search",
                                                             "args": {"query": "fee"}, "text": "t000001 ...", **kw}, set())
        take = lambda kind="dream", age=3600: self.request("dream_cache_take", {"kind": kind, "max_age_seconds": age}, set())
        self.assertEqual(put(), 1)
        self.assertEqual(put(), 1)  # the same read again is not stored twice
        put(tool="somni_image", text="[t000001] image", images=[{"ref": self.shot, "sha256": self.digest}])
        self.assertEqual([r["tool"] for r in take()], ["somni_search", "somni_image"])
        self.assertEqual(take("wiki"), [])  # another kind of turn
        self.assertEqual(take(age=0), [])  # caching disabled
        cache = json.loads((self.m.store / "dream-cache.json").read_text())
        cache["started"] = cache["updated"] = "2000-01-01T00:00:00+00:00"
        (self.m.store / "dream-cache.json").write_text(json.dumps(cache))
        self.assertEqual(take(), [])  # too old
        put(text="x" * 20_000)  # a new read refreshes it; long text is clipped
        self.assertEqual(len(take()), 3)
        self.assertLess(len(take()[-1]["text"]), 9_000)
        # New records change the head, so an old attempt's reads no longer apply.
        self.m.set_phase("open")
        self.m.record("ordinary_observation", "something new")
        self.m.set_phase("closed")
        self.assertEqual(take(), [])
        put()
        self.request("dream", {"proposal_json": json.dumps({"schema": 1, "operations": [
            self.claim([{"ref": self.plain, "quote": "no image"}])]})}, set())
        self.assertFalse((self.m.store / "dream-cache.json").exists())
        for bad in ({"kind": "dream", "tool": "bash", "text": ""}, {"kind": "nap", "tool": "somni_search", "text": ""},
                    {"kind": "dream", "tool": "somni_image", "text": "", "images": [{"ref": "x", "sha256": "y"}]}):
            with self.subTest(bad=bad), self.assertRaises(SomniError):
                self.request("dream_cache_put", bad, set())

    def test_a_daydream_cache_is_keyed_to_its_snapshot(self):
        self.m.set_phase("open")
        put = {"kind": "daydream", "tool": "somni_search", "args": {}, "text": "hit"}
        _handle_request(self.m, "dream_cache_put", put, writable=False, viewed=set(), daydream=2)
        self.m.record("ordinary_observation", "recorded after the snapshot")
        take = {"kind": "daydream", "max_age_seconds": 60}
        self.assertEqual(len(_handle_request(self.m, "dream_cache_take", take, writable=False, viewed=set(), daydream=2)), 1)
        self.assertEqual(_handle_request(self.m, "dream_cache_take", take, writable=False, viewed=set(), daydream=4), [])


if __name__ == "__main__":
    unittest.main()
