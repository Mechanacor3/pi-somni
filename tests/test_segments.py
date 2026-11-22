"""Long logs are sealed into hash-checked segments; the chain, evidence and recovery span them."""
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from somni import store as S  # noqa: E402
from somni.store import SomniError, SomniMemory  # noqa: E402


class SegmentTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ws = pathlib.Path(tmp.name)
        patcher = mock.patch.object(S, "SEGMENT_BYTES", 4000)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.m = SomniMemory.create(self.ws, "s", "l", "a")

    def fill(self, n, start=0):
        self.m.set_phase("open")
        for i in range(start, start + n):
            self.m.record("tool_result", f'TOOL probe({{"i":{i}}}) -> {{"value":{i},"pad":"{"x" * 200}"}}', tool_ok=True)
        self.m.set_phase("closed")

    def test_records_seal_into_segments_and_reload_as_one_chain(self):
        self.fill(60)
        folder = self.ws / "somni-v1" / "segments"
        sealed = sorted(p.name for p in folder.glob("*.jsonl"))
        self.assertGreaterEqual(len(sealed), 2)
        self.assertTrue(all(name.startswith("transcript-") for name in sealed))
        self.assertLessEqual((self.ws / "somni-v1" / "transcript.jsonl").stat().st_size, 4000)
        m = SomniMemory(self.ws, "s").load()
        self.assertEqual([r["seq"] for r in m.transcript], list(range(1, 61)))
        status = m.status()
        self.assertEqual(status["transcript_count"], 60)
        self.assertEqual(status["segments"]["transcript"], len(sealed))
        self.assertEqual(status["transcript_live_records"],
                         60 - sum(seg["count"] for seg in m.state["segments"]["transcript.jsonl"]))
        # Evidence may cite a record that now lives in a sealed segment.
        m.dream({"schema": 1, "operations": [{"op": "add", "text": "Probes return their index.", "status": "verified",
                                              "salience": 3, "evidence": [{"ref": "t000001", "quote": '"value":0'}]}]})
        self.assertEqual(SomniMemory(self.ws, "s").load().claims["n000001"]["evidence"][0]["ref"], "t000001")

    def test_notes_and_dreams_seal_too(self):
        self.fill(4)
        for i in range(40):
            self.m.dream({"schema": 1, "operations": [{"op": "add", "text": f"Lesson {i}: " + "y" * 150,
                                                       "status": "uncertain", "salience": 2, "evidence": [{"ref": "t000001"}]}]})
        m = SomniMemory(self.ws, "s").load()
        self.assertGreaterEqual(m.status()["segments"]["notes"], 1)
        self.assertGreaterEqual(m.status()["segments"]["dreams"], 1)
        self.assertEqual(len(m.dreams), 40)
        self.assertEqual(len(m.claims), 40)

    def test_tampered_segment_is_detected(self):
        self.fill(60)
        segment = sorted((self.ws / "somni-v1" / "segments").glob("*.jsonl"))[0]
        text = segment.read_text()
        self.assertIn("xxxx", text)
        segment.write_text(text.replace("xxxx", "xxxy", 1))
        with self.assertRaisesRegex(SomniError, "does not match its recorded hash"):
            SomniMemory(self.ws, "s").load()

    def test_missing_segment_is_detected(self):
        self.fill(60)
        next((self.ws / "somni-v1" / "segments").glob("*.jsonl")).unlink()
        with self.assertRaisesRegex(SomniError, "missing sealed segment"):
            SomniMemory(self.ws, "s").load()

    def test_seal_interrupted_before_truncating_is_finished_on_load(self):
        self.fill(5)
        live = self.ws / "somni-v1" / "transcript.jsonl"
        before = live.read_bytes()
        real = os.ftruncate
        with mock.patch.object(S.os, "ftruncate", side_effect=OSError("crash")):
            with self.assertRaises(OSError):
                self.m._seal("transcript.jsonl")
        self.assertEqual(live.read_bytes(), before)  # state already lists the segment; the live copy remains
        m = SomniMemory(self.ws, "s").load()
        self.assertEqual(live.read_bytes(), b"")
        self.assertEqual(len(m.transcript), 5)
        self.assertIs(os.ftruncate, real)
        self.fill(3, start=5)
        self.assertEqual([r["seq"] for r in SomniMemory(self.ws, "s").load().transcript], list(range(1, 9)))

    def test_old_store_without_segments_loads_and_limits_are_configurable(self):
        self.fill(3)
        state = json.loads((self.ws / "somni-v1" / "state.json").read_text())
        self.assertNotIn("segments", state)
        m = SomniMemory(self.ws, "s").load()
        self.assertEqual(m.status()["segments"], {"transcript": 0, "notes": 0, "dreams": 0})
        self.assertEqual(m.status()["limits"]["max_dream_passes"], S.MAX_DREAM_PASSES)


if __name__ == "__main__":
    unittest.main()
