"""Daydreams: live passes bound to a snapshot head while the owning session keeps recording."""
import json
import pathlib
import select
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from somni.store import SomniError, SomniMemory, sha  # noqa: E402


def add(ref, quote, text="Probes echo their input."):
    return {"op": "add", "text": text, "status": "verified", "salience": 3, "evidence": [{"ref": ref, "quote": quote}]}


class Backend:
    def __init__(self, workspace, *flags):
        self.proc = subprocess.Popen([sys.executable, "-I", str(ROOT / "somni/store.py"), "--workspace", str(workspace),
                                      "--subject-id", "s", "serve", *flags],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.n = 0
        self.ready = self.read()

    def read(self):
        assert select.select([self.proc.stdout], [], [], 20)[0], "backend did not answer"
        return json.loads(self.proc.stdout.readline())

    def __call__(self, method, **args):
        self.n += 1
        self.proc.stdin.write(json.dumps({"id": self.n, "method": method, "args": args}) + "\n")
        self.proc.stdin.flush()
        return self.read()

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=20)


class DaydreamTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ws = pathlib.Path(tmp.name)
        self.m = SomniMemory.create(self.ws, "s", "l", "a")
        self.m.set_phase("open")
        for i in range(1, 4):
            self.m.record("tool_result", f'TOOL probe({{"i":{i}}}) -> {{"echo":{i}}}', tool_ok=True)

    def test_snapshot_pass_commits_while_recording_and_cites_only_up_to_its_head(self):
        daydream = SomniMemory(self.ws, "s").load()  # a second writer, as a daydream backend is
        self.m.record("tool_result", 'TOOL probe({"i":4}) -> {"echo":4}', tool_ok=True)  # t000004, after H=3
        with self.assertRaisesRegex(SomniError, "'t000004' is after this daydream's snapshot \\(t000003\\)"):
            daydream.dream({"schema": 1, "operations": [add("t000004", '"echo":4')]}, snapshot=3)
        with self.assertRaisesRegex(SomniError, "offline-only"):
            daydream.dream({"schema": 1, "operations": [add("t000001", '"echo":1')]})  # no snapshot: still closed-only
        dream = daydream.dream({"schema": 1, "operations": [add("t000001", '"echo":1')]}, snapshot=3)
        self.assertEqual((dream["transcript_count"], dream["live"]), (3, True))
        self.assertEqual(dream["transcript_head"], sha(daydream.transcript[2]))
        # The owner keeps recording after the daydream's commit; both chains verify.
        self.m.record("tool_result", 'TOOL probe({"i":5}) -> {"echo":5}', tool_ok=True)
        self.assertIn("n000001", self.m.inject(4096))
        reloaded = SomniMemory(self.ws, "s").load()
        self.assertEqual((len(reloaded.transcript), len(reloaded.dreams), reloaded.state["phase"]), (5, 1, "open"))

    def test_daydream_backend_runs_beside_the_recording_owner(self):
        self.m.set_phase("closed")
        owner = Backend(self.ws, "--record")
        self.addCleanup(owner.proc.kill)
        self.assertTrue(owner.ready["result"]["writable"])
        owner("record", source="tool_result", text='TOOL probe({"i":4}) -> {"echo":4}', tool_ok=True)
        day = Backend(self.ws, "--daydream", "--actions")
        self.addCleanup(day.proc.kill)
        self.assertEqual(day.ready["result"], {"writable": True, "daydream_head": 4})
        owner("record", source="tool_result", text='TOOL probe({"i":5}) -> {"echo":5}', tool_ok=True)  # after H
        recent = day("recent", record_limit=120)["result"]["records"]
        self.assertEqual(recent[-1]["id"], "t000004")
        self.assertIn("may only read memory", day("record", source="agent_output", text="no")["error"])
        out = day("dream", proposal_json=json.dumps({"schema": 1, "operations": [
            add("t000004", '"echo":4'), add("t000005", '"echo":5', "After the snapshot.")]}))["result"]
        self.assertEqual((out["live"], out["transcript_count"], out["rejected_count"]), (True, 4, 1))
        self.assertIn("t000005", out["rejected"][0]["reason"])
        # The owner sees the daydream's claim at its next turn and keeps recording.
        self.assertIn("n000001", owner("inject", index_bytes=4096)["result"])
        owner("record", source="agent_output", text="Still recording.")
        day.close()
        owner.close()
        final = SomniMemory(self.ws, "s").load()
        self.assertEqual((len(final.transcript), final.state["phase"], len(final.dreams)), (6, "closed", 1))


if __name__ == "__main__":
    unittest.main()
