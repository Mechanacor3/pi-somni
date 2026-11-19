#!/usr/bin/env python3
"""End-to-end: real Pi + extensions/somni.ts + scripted fake model."""
import json
import pathlib
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "e2e"))
sys.path.insert(0, str(HERE.parent))
from harness import FakeUpstream, PiSession, pi_ready, plan_script, text_of  # noqa: E402
from somni.store import SomniMemory  # noqa: E402

PLANS = {
    "What is the fee": [[("lookup_fee", {"cabin": "economy", "price": 300})]],
    "Remember that": [[("somni_search", {"query": "fee"})],
                      [("somni_note", {"text": "Economy cancellation fees have a $50 floor.", "status": "verified",
                                       "salience": 4, "evidence": [{"ref": "t000002", "quote": "\"fee\":50"}]})]],
    "Dream pass.": [[("somni_dream", {"proposal_json": json.dumps({"schema": 1, "operations": [
        {"op": "add", "text": "Look up the fee before quoting it.", "status": "uncertain", "salience": 3,
         "evidence": [{"ref": "t000002"}]},
        {"op": "add", "text": "Bad quote is rejected.", "status": "verified", "salience": 2,
         "evidence": [{"ref": "t000002", "quote": "not in the record"}]}]})})]],
    "Anything else": [],
    "Recall the rule": [[("somni_recall", {"query": "cancellation fee", "limit": 1})]],
    "Wiki pass.": [[("somni_dream", {"proposal_json": json.dumps({"schema": 1, "operations": [
        {"op": "page", "slug": "fees", "title": "Fees", "body": "Economy fees have a floor [n000001].", "claims": ["n000001"]}]})})]],
}


@unittest.skipUnless(pi_ready(), "set PI_PLUGIN_E2E_BINARY or build the Docker image in tests/e2e")
class SomniPiEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = pathlib.Path(self.tmp.name)
        self.upstream = FakeUpstream(plan_script(PLANS))
        self.pi = PiSession("extensions/somni.ts", self.work, self.upstream.port)

    def tearDown(self):
        self.pi.close()
        self.upstream.close()
        self.tmp.cleanup()

    def test_record_note_inject_and_dream(self):
        self.pi.prompt("What is the fee to cancel economy fare 300?")
        store = self.work / ".pi" / "somni"
        memory = SomniMemory(store, "pi").load()
        sources = [r["source"] for r in memory.transcript]
        self.assertEqual(sources[:2], ["ordinary_observation", "tool_result"])
        self.assertIn('"fee":50', memory.transcript[1]["text"])
        self.assertIn("agent_output", sources)

        self.pi.prompt("Remember that for later.")
        ends = {e["toolName"]: e for e in self.pi.tool_ends()}
        self.assertFalse(ends["somni_note"].get("isError"), ends["somni_note"])
        self.assertEqual(SomniMemory(store, "pi").load().claims["n000001"]["status"], "verified")

        # The next run sees the claim index as an injected message.
        self.pi.prompt("Anything else I should know?")
        last = json.dumps(self.upstream.requests[-1]["messages"])
        self.assertIn("n000001", last)
        self.assertIn("$50 floor", last)

        # /somni-dream closes the session, the model submits a pass, recording reopens afterwards.
        self.pi.prompt("/somni-dream fees")  # handled by the extension; the dream turn follows
        self.pi.read(lambda r: r.get("type") == "agent_settled")
        dream_end = [e for e in self.pi.tool_ends() if e["toolName"] == "somni_dream"][-1]
        self.assertFalse(dream_end.get("isError"), dream_end)
        memory = SomniMemory(store, "pi").load()
        self.assertEqual(len(memory.dreams), 1)
        self.assertEqual(memory.dreams[0]["operation_count"], 1)
        self.assertEqual(memory.dreams[0]["rejected"][0]["index"], 1)
        self.assertEqual(memory.state["phase"], "open")
        # /somni-wiki gathers uncovered claims into pages; the vault becomes readable markdown.
        self.pi.prompt("/somni-wiki fees")
        self.pi.read(lambda r: r.get("type") == "agent_settled")
        memory = SomniMemory(store, "pi").load()
        self.assertEqual(sorted(memory.pages), ["fees"])
        vault = store / "somni-v1" / "vault"
        self.assertIn("[[claims#n000001|n000001]]", (vault / "fees.md").read_text())
        self.assertIn("[[fees|Fees]]", (vault / "index.md").read_text())
        self.pi.prompt("Anything else I should know?")
        self.assertIn("page `fees`", json.dumps(self.upstream.requests[-1]["messages"][-1]))
        tools = {t["function"]["name"] for t in self.upstream.requests[0]["tools"]}
        self.assertTrue({"somni_recall", "somni_search", "somni_note", "somni_dream", "lookup_fee"} <= tools)
        self.assertTrue(all("not-a-real-key" not in text_of(m.get("content")) for r in self.upstream.requests for m in r["messages"]))

    def test_second_pi_session_cannot_write_or_close_the_first(self):
        self.pi.prompt("What is the fee to cancel economy fare 300?")
        store = self.work / ".pi" / "somni"
        before = SomniMemory(store, "pi").load().state["transcript_count"]
        other = PiSession("extensions/somni.ts", self.work, self.upstream.port)
        try:
            other.prompt("Remember that for later.")
            warnings = [e.get("message", "") for e in other.events if e.get("method") == "notify"]
            self.assertTrue(any("another Pi session is recording" in msg for msg in warnings), warnings)
            self.assertTrue(next(e for e in other.tool_ends() if e["toolName"] == "somni_note")["isError"])
        finally:
            other.close()
        self.assertEqual(SomniMemory(store, "pi").load().state["transcript_count"], before)
        self.pi.prompt("Anything else I should know?")
        self.assertGreater(SomniMemory(store, "pi").load().state["transcript_count"], before)

    def test_recall_after_restart(self):
        self.pi.prompt("What is the fee to cancel economy fare 300?")
        self.pi.prompt("Remember that for later.")
        self.pi.close()
        store = self.work / ".pi" / "somni"
        self.assertEqual(SomniMemory(store, "pi").load().state["phase"], "closed")
        self.pi = PiSession("extensions/somni.ts", self.work, self.upstream.port)
        self.pi.prompt("Recall the rule we saved.")
        recall = next(e for e in self.pi.tool_ends() if e["toolName"] == "somni_recall")
        self.assertFalse(recall.get("isError"), recall)
        self.assertIn("$50 floor", json.dumps(recall["result"]))


if __name__ == "__main__":
    unittest.main()
