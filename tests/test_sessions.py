#!/usr/bin/env python3
"""Several Pi sessions on one memory: the owner, peers (the user's other sessions) and contributors (sub-agents)."""
import json
import pathlib
import select
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from somni.store import SomniMemory  # noqa: E402


class Backend:
    def __init__(self, workspace, label, *extra):
        cmd = [sys.executable, "-I", str(ROOT / "somni/store.py"), "--workspace", str(workspace), "--subject-id", "s",
               "serve", "--record", f"--session-label={label}", *extra]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.next = 0
        self.ready = self.read()

    def read(self):
        assert select.select([self.proc.stdout], [], [], 10)[0], "backend did not answer"
        reply = json.loads(self.proc.stdout.readline())
        if "error" in reply:
            raise RuntimeError(reply["error"])
        return reply["result"]

    def __call__(self, method, **args):
        self.next += 1
        self.proc.stdin.write(json.dumps({"id": self.next, "method": method, "args": args}) + "\n")
        self.proc.stdin.flush()
        return self.read()

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=10)
        self.proc.stdout.close()

    def stop(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=10)
        for stream in (self.proc.stdin, self.proc.stdout):
            if not stream.closed:
                stream.close()


class SessionsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ws = pathlib.Path(tmp.name)

    def start(self, label, *extra):
        backend = Backend(self.ws, label, *extra)
        self.addCleanup(backend.stop)
        return backend

    def memory(self):
        return SomniMemory(self.ws, "s").load()

    def test_a_peer_records_as_the_user_and_may_save_a_standing_note_a_contributor_only_records(self):
        owner = self.start("owner")
        peer = self.start("peer-1", "--peer")
        sub = self.start("sub-1")
        self.assertEqual((owner.ready["writable"], owner.ready["others"]), (True, 0))
        self.assertEqual(peer.ready, {"writable": False, "contributor": True, "peer": True, "others": 1})
        self.assertEqual(sub.ready, {"writable": False, "contributor": True, "others": 2})
        said = peer("record", source="ordinary_observation", text="USER: I like 2-space indents")
        told = sub("record", source="ordinary_observation", text="USER: use tabs")
        records = {r["id"]: r for r in self.memory().transcript}
        self.assertEqual(records[said]["tags"], ["session:peer-1"])
        self.assertEqual(records[told]["tags"], ["contributor:sub-1"])
        note = {"text": "Indent with 2 spaces.", "status": "verified", "salience": 3, "standing": True}
        claim = peer("note", **note, evidence=[{"ref": said, "quote": "I like 2-space indents"}])
        self.assertTrue(self.memory().claims[claim]["standing"])
        with self.assertRaisesRegex(RuntimeError, "owns recording"):
            sub("note", **note, evidence=[{"ref": told, "quote": "use tabs"}])
        with self.assertRaisesRegex(RuntimeError, "owns recording"):
            peer("phase", phase="closed")

    def test_the_owner_leaves_the_phase_open_for_others_and_the_last_one_out_closes_it(self):
        owner = self.start("owner")
        peer = self.start("peer-1", "--peer")
        self.assertEqual(peer("role")["writable"], False)
        owner.close()
        self.assertEqual(self.memory().state["phase"], "open")  # the peer is still recording
        peer("record", source="agent_output", text="AGENT: still here")
        role = peer("role")  # its next turn takes the lease over
        self.assertEqual(role, {"writable": True, "others": 0})
        record = peer("record", source="agent_output", text="AGENT: now the owner")
        self.assertNotIn("tags", self.memory()._records_by_id[record])
        peer.close()
        self.assertEqual(self.memory().state["phase"], "closed")
        self.assertEqual(list((self.ws / ".somni-sessions").glob("*.lock")), [])

    def test_a_peer_that_never_took_over_still_closes_the_phase_when_it_is_last(self):
        owner = self.start("owner")
        peer = self.start("peer-1", "--peer")
        owner.close()
        peer.close()
        self.assertEqual(self.memory().state["phase"], "closed")

    def test_a_killed_session_does_not_count_as_recording(self):
        owner = self.start("owner")
        crashed = self.start("peer-1", "--peer")
        crashed.proc.kill()
        crashed.proc.wait(timeout=10)
        self.assertEqual(owner("role")["others"], 0)  # its slot is stale and is removed
        owner.close()
        self.assertEqual(self.memory().state["phase"], "closed")

    def test_the_owners_dream_with_a_peer_recording_is_a_live_pass(self):
        owner = self.start("owner")
        ref = owner("record", source="ordinary_observation", text="USER: deploys go out from release branches")
        peer = self.start("peer-1", "--peer")
        proposal = {"schema": 1, "operations": [{"op": "add", "text": "Deploy from release branches.",
                                                 "status": "verified", "salience": 3,
                                                 "evidence": [{"ref": ref, "quote": "deploys go out from release"}]}]}
        self.assertEqual(owner("role")["others"], 1)
        dream = owner("dream", proposal_json=json.dumps(proposal), live=True)
        self.assertTrue(dream["live"])
        peer("record", source="agent_output", text="AGENT: recorded while the owner dreamed")
        self.assertEqual(self.memory().state["phase"], "open")


if __name__ == "__main__":
    unittest.main()
