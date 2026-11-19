#!/usr/bin/env python3
"""End-to-end: real Pi + extensions/somni.ts + scripted fake model."""
import base64
import hashlib
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "e2e"))
sys.path.insert(0, str(HERE.parent))
from harness import BINARY, REPO, FakeUpstream, PiSession, pi_ready, plan_script, text_of  # noqa: E402
from somni.store import SomniMemory  # noqa: E402
from tests.test_new_features import PNG  # noqa: E402

PLANS = {
    "What is the fee": [[("lookup_fee", {"cabin": "economy", "price": 300})]],
    "Remember that": [[("somni_search", {"query": "fee"})],
                      [("somni_note", {"text": "Economy cancellation fees have a $50 floor.", "status": "verified",
                                       "salience": 4, "evidence": [{"ref": "t000002", "quote": "\"fee\":50"}]})]],
    "Dream pass.": [[("somni_dream", {"proposal_json": json.dumps({"schema": 1, "operations": [
        {"op": "add", "text": "Look up the fee before quoting it.", "status": "uncertain", "salience": 3,
         "evidence": [{"ref": "t000002"}]},
        {"op": "add", "text": "Bad quote is rejected.", "status": "verified", "salience": 2,
         "evidence": [{"ref": "t000002", "quote": "not in the record"}]},
        {"op": "merge", "claim_ids": ["n000002", "n000001"], "text": "Look up the cancellation fee before quoting its floor."}]})})]],
    "Anything else": [],
    "Look around": [
        {"text": "I will look down.", "tool_calls": [("screenshot", {})]},
        {"text": "The frame is fresh.", "tool_calls": [("stream_read", {})]},
        [("large_result", {})],
    ],
    "Recall the rule": [[("somni_recall", {"query": "cancellation fee", "limit": 1})]],
    "Wiki pass.": [[("somni_dream", {"proposal_json": json.dumps({"schema": 1, "operations": [
        {"op": "page", "slug": "fees", "title": "Fees", "body": "Economy fees have a floor [n000001].", "claims": ["n000001"]}]})})]],
}


@unittest.skipUnless(pi_ready(), "set PI_PLUGIN_E2E_BINARY or build the Docker image in tests/e2e")
class SomniPiEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = pathlib.Path(self.tmp.name)
        self.upstream = FakeUpstream(plan_script(PLANS))
        self.pi = PiSession("extensions/somni.ts", self.work, self.upstream.port)

    def tearDown(self):
        self.pi.close()
        self.upstream.close()

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
        self.assertEqual(memory.dreams[0]["operation_count"], 2)
        self.assertEqual(memory.dreams[0]["rejected"][0]["index"], 1)
        self.assertEqual(memory.status()["last_pass"]["merged_count"], 1)
        self.assertEqual(memory.status()["last_pass"]["rejected_count"], 1)
        merged = memory.claims["n000003"]
        self.assertEqual(merged["merged_from"], ["n000001", "n000002"])
        self.assertEqual(memory.state["phase"], "open")
        # /somni-wiki gathers uncovered claims into pages; the vault becomes readable markdown.
        self.pi.prompt("/somni-wiki fees")
        self.pi.read(lambda r: r.get("type") == "agent_settled")
        memory = SomniMemory(store, "pi").load()
        self.assertEqual(sorted(memory.pages), ["fees"])
        vault = store / "somni-v1" / "vault"
        self.assertIn("[[claims#n000001|n000001]]", (vault / "fees.md").read_text())
        self.assertIn("[[fees|Fees]]", (vault / "index.md").read_text())
        self.assertIn(merged["provenance_sha256"], (vault / "claims.md").read_text())
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

    def test_wiki_pass_accepts_cycles_and_receives_custom_page_guide(self):
        self.pi.close()
        guide = "Describe terrain cues and where to aim."
        self.pi = PiSession("extensions/somni.ts", self.work, self.upstream.port, {"SOMNI_PAGE_GUIDE": guide})
        self.pi.prompt("What is the fee to cancel economy fare 300?")
        self.pi.prompt("Remember that for later.")
        self.upstream.script = plan_script({"Wiki pass.": [[("somni_dream", {"proposal_json": json.dumps({
            "schema": 1,
            "operations": [
                {"op": "page", "slug": "overview", "title": "Overview", "body": "See [[fees]].", "claims": []},
                {"op": "page", "slug": "fees", "title": "Fees", "body": "Return to [[overview]].", "claims": ["n000001"]},
            ],
        })})]]})
        self.pi.prompt("/somni-wiki")
        self.pi.read(lambda event: event.get("type") == "agent_settled")
        wiki_request = next(request for request in self.upstream.requests
                            if any(text_of(message.get("content")).startswith("Wiki pass.") for message in request["messages"]))
        prompt = text_of(next(message["content"] for message in wiki_request["messages"]
                              if text_of(message.get("content")).startswith("Wiki pass.")))
        self.assertIn(guide, prompt)
        self.assertIn("can form cycles", prompt)
        memory = SomniMemory(self.work / ".pi" / "somni", "pi").load()
        self.assertEqual(memory.pages["overview"]["links"], ["fees"])
        self.assertEqual(memory.pages["fees"]["links"], ["overview"])
        self.assertEqual(memory.dreams[-1]["page_operations"], 2)

    def test_vision_records_text_and_bounded_tool_results_then_dreams_in_json_mode(self):
        self.pi.close()
        task = PiSession("extensions/somni.ts", self.work, self.upstream.port, {
            "SOMNI_RUN_LABEL": "test 001", "SOMNI_TAGS": '["world A","dirt"]',
        }, mode="json", prompt="Look around.")
        code, _, err = task.finish()
        self.assertEqual(code, 0, err)
        store = self.work / ".pi" / "somni"
        memory = SomniMemory(store, "pi").load()
        self.assertEqual([r["source"] for r in memory.transcript],
                         ["ordinary_observation", "agent_output", "tool_result", "agent_output",
                          "tool_result", "tool_result", "agent_output"])
        for record in memory.transcript:
            self.assertEqual(record["run_label"], "test 001")
            self.assertEqual(record["tags"], ["dirt", "world A"])
        shot, stream, large = (memory.transcript[i] for i in (2, 4, 5))
        self.assertIn("frame_age_ms", shot["text"])
        self.assertNotIn("iVBOR", shot["text"])
        descriptor = shot["images"][0]
        image = memory.image(shot["id"], descriptor["sha256"])
        self.assertEqual(hashlib.sha256(base64.b64decode(image["data"])).hexdigest(), descriptor["sha256"])
        self.assertIn("body omitted: 2097152 UTF-8 bytes", stream["text"])
        self.assertIn(hashlib.sha256(b"ABCD" * 524288).hexdigest(), stream["text"])
        self.assertNotIn("ABCD", stream["text"])
        self.assertIn("body truncated: 8192 UTF-8 bytes", large["text"])
        self.assertLessEqual(len(large["text"].encode()), 4096)
        self.assertNotIn("\ufffd", large["text"])

        cite = {"ref": shot["id"], "image_sha256": descriptor["sha256"]}
        operation = {"op": "add", "text": "The screenshot showed a single pixel.", "status": "verified", "evidence": [cite]}
        plan = plan_script({"Dream pass.": [[("somni_dream", {
            "proposal_json": json.dumps({"schema": 1, "operations": [operation]}),
        })]]})
        seen = []

        def scripted(body, index):
            content = json.dumps(body["messages"])
            seen.append((image["data"] in content, descriptor["sha256"] in content, "world A" in content))
            return plan(body, index)

        self.upstream.script = scripted
        run = PiSession("extensions/somni.ts", self.work, self.upstream.port, {"SOMNI_RUN_LABEL": "dream run"},
                        mode="json", prompt="/somni-dream")
        code, out, err = run.finish()
        self.assertEqual(code, 0, err)
        events = [json.loads(line) for line in out.splitlines()]
        final = [e["message"] for e in events if e.get("type") == "message_end" and e["message"]["role"] == "assistant"][-1]
        self.assertEqual(final["content"][-1]["text"], "Somni dream d000001: 1 added, 0 rejected, 0 merged.")
        self.assertEqual(seen[0], (True, True, True))
        memory = SomniMemory(store, "pi").load()
        claim = memory.claims["n000001"]
        self.assertEqual(claim["evidence"], [cite])
        self.assertEqual(claim["run_labels"], ["test 001"])
        self.assertEqual(memory.recall("world A")[0]["id"], claim["id"])
        self.assertEqual(memory.state["transcript_count"], 7)
        result = subprocess.check_output([sys.executable, str(REPO / "somni/store.py"), "--workspace", str(store),
                                          "--subject-id", "pi", "status", "--json"])
        self.assertEqual(len(result.splitlines()), 1)
        status = json.loads(result)
        self.assertEqual(status["phase"], "closed")
        self.assertEqual(status["active_claims_by_status"], {"uncertain": 0, "verified": 1})
        self.assertEqual(status["last_pass"], {
            "id": "d000001", "transcript_head": status["transcript_head"], "operation_count": 1,
            "added_count": 1, "rejected_count": 0, "merged_count": 0,
        })

    def test_print_dream_reports_summary_and_fails_when_model_skips_the_pass(self):
        self.pi.close()
        self.upstream.script = plan_script({})
        run = PiSession("extensions/somni.ts", self.work, self.upstream.port, mode="text", prompt="/somni-dream")
        code, out, err = run.finish()
        self.assertNotEqual(code, 0)
        self.assertIn("without a committed pass", err)
        self.upstream.script = plan_script({"Dream pass.": [[("somni_dream", {
            "proposal_json": '{"schema":1,"operations":[]}',
        })]]})
        run = PiSession("extensions/somni.ts", self.work, self.upstream.port, mode="text", prompt="/somni-dream")
        code, out, err = run.finish()
        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines()[-1], "Somni dream d000001: 0 added, 0 rejected, 0 merged.")

    def test_dream_can_fetch_an_image_outside_the_attachment_limit(self):
        self.pi.prompt("Anything else?")
        self.pi.close()
        store = self.work / ".pi" / "somni"
        memory = SomniMemory(store, "pi").load()
        memory.set_phase("open")
        image = {"mimeType": "image/png", "data": PNG}
        refs = [memory.record("tool_result", f"frame {index}", images=[image]) for index in range(5)]
        digest = memory.transcript[-5]["images"][0]["sha256"]
        ref = refs[0]
        evidence = {"ref": ref, "image_sha256": digest}
        self.upstream.script = plan_script({"Dream pass.": [
            [("somni_image", {"ref": ref, "sha256": digest})],
            [("somni_dream", {"proposal_json": json.dumps({"schema": 1, "operations": [
                {"op": "add", "text": "The first frame shows green.", "status": "verified", "evidence": [evidence]},
            ]})})],
        ]})
        self.pi = PiSession("extensions/somni.ts", self.work, self.upstream.port)
        self.pi.prompt("/somni-dream")
        self.pi.read(lambda r: r.get("type") == "agent_settled")
        result = next(e for e in self.pi.tool_ends() if e["toolName"] == "somni_image")
        self.assertFalse(result["isError"], result)
        self.assertEqual(result["result"]["content"][1]["data"], PNG)
        self.assertEqual(SomniMemory(store, "pi").load().claims["n000001"]["evidence"], [evidence])

    def test_sigkill_releases_recording_lease_and_preserves_chain(self):
        self.pi.prompt("What is the fee to cancel economy fare 300?")
        store = self.work / ".pi" / "somni"
        before = SomniMemory(store, "pi").load()
        self.pi.kill()
        self.assertTrue((store / ".somni-session.lock").is_file())
        self.pi = PiSession("extensions/somni.ts", self.work, self.upstream.port)
        self.pi.prompt("Anything else?")
        after = SomniMemory(store, "pi").load()
        self.assertEqual(after.transcript[:len(before.transcript)], before.transcript)
        self.assertEqual(after.transcript[len(before.transcript)]["prev_sha256"], before.state["transcript_head"])

    @unittest.skipUnless(BINARY, "Docker already runs all Pi tests with /plugin mounted read-only")
    def test_read_only_install_and_image_size_cap(self):
        self.pi.close()
        install = self.work / "install"
        for name in ("extensions", "somni"):
            shutil.copytree(REPO / name, install / name, ignore=shutil.ignore_patterns("__pycache__"))
        files = {path.relative_to(install): path.read_bytes() for path in install.rglob("*") if path.is_file()}
        for path in reversed(sorted(install.rglob("*"))):
            path.chmod(0o555 if path.is_dir() else 0o444)
            self.addCleanup(path.chmod, 0o755 if path.is_dir() else 0o644)
        install.chmod(0o555)
        self.addCleanup(install.chmod, 0o755)
        self.pi = PiSession("extensions/somni.ts", self.work, self.upstream.port, {"SOMNI_IMAGE_BYTES": "32"}, package=install)
        self.upstream.script = plan_script({**PLANS, "Look around": [[("screenshot", {})]]})
        self.pi.prompt("Look around.")
        self.pi.prompt("What is the fee?")
        # Use a claim that cites the text on the screenshot record and render a vault from the read-only install.
        self.upstream.script = plan_script({**PLANS, "Remember that": [[("somni_note", {
            "text": "Screenshot tools report frame age.", "status": "verified",
            "evidence": [{"ref": "t000002", "quote": "frame_age_ms"}],
        })]]})
        self.pi.prompt("Remember that.")
        self.pi.prompt("/somni-wiki fees")
        self.pi.read(lambda r: r.get("type") == "agent_settled")
        store = self.work / ".pi" / "somni"
        memory = SomniMemory(store, "pi").load()
        self.assertEqual(memory.transcript[1]["omitted_images"], 1)
        self.assertNotIn("images", memory.transcript[1])
        self.assertTrue((memory.store / "vault" / "fees.md").is_file())
        self.assertEqual({p.relative_to(install): p.read_bytes() for p in install.rglob("*") if p.is_file()}, files)


if __name__ == "__main__":
    unittest.main()
