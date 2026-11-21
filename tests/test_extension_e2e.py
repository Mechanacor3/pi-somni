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

    def test_prompt_ranked_index_in_recording_and_read_only_sessions(self):
        self.pi.close()
        store = self.work / "seeded-memory"
        store.mkdir()
        memory = SomniMemory.create(store, "pi", "test", "index")
        memory.set_phase("open")
        for number in range(10):
            ref = memory.record("tool_result", "Earlier attempt", context={"run_label": "test 0040"})
            memory.add_note(f"Old lesson {number} for test 004. " + "Prior observations. " * 22,
                            "uncertain", [{"ref": ref}], 5)
        ref = memory.record("tool_result", "The retry failed", context={"tags": ["test 004"]})
        claim = memory.add_note("Long guidance for the next retry. " * 12, "uncertain", [{"ref": ref}], 5,
                                gist="Verify the staging mark before retrying.")
        memory.set_phase("closed")
        memory.dream({"schema": 1, "operations": [
            {"op": "page", "slug": "archive", "title": "Archive", "body": "Earlier attempts [n000001].",
             "claims": ["n000001"]},
            {"op": "page", "slug": "retry", "title": "Retry", "body":
             f"Overview [{claim}].\n\n## Procedure\n\n### Before retrying\nCheck the staging mark.\n\n## Appendix\n" + "背景" * 500,
             "claims": [claim]},
        ]})
        self.assertNotIn("- n000010 ", memory.inject())
        for recording in ("1", "0"):
            with self.subTest(recording=recording):
                self.pi = PiSession("extensions/somni.ts", self.work, self.upstream.port,
                                    env={"SOMNI_WORKSPACE": store.name, "SOMNI_RECORD": recording})
                try:
                    self.pi.prompt("Compare the prior observations for Test 004 before proceeding.")
                    messages = [text_of(m.get("content")) for m in self.upstream.requests[-1]["messages"]]
                    index = next(m for m in messages if m.startswith("# Subject memory index"))
                    self.assertLessEqual(len(index.encode("utf-8")), 4096)
                    self.assertLess(index.index(f"- {claim} "), index.index("- n000010 "))
                    self.assertLess(index.index("page `retry`"), index.index("page `archive`"))
                    self.assertIn("## Procedure\n\n### Before retrying\nCheck the staging mark.", index)
                    self.assertNotIn("## Appendix", index)
                    self.assertIn("Verify the staging mark before retrying.", index)
                    self.assertNotIn("Long guidance for the next retry", index)
                finally:
                    self.pi.close()

    def test_dream_checks_chel_rule_and_blocks_live_tools(self):
        self.pi.close()
        self.pi = PiSession("extensions/somni.ts", self.work, self.upstream.port,
                            env={"SOMNI_CHEL_LEARNING": "1"})
        plans = {cabin: [[("lookup_fee", {"cabin": cabin, "price": 300})]] for cabin in ("economy", "business")}
        self.upstream.script = plan_script(plans)
        for cabin in ("economy", "business", "economy"):
            self.pi.prompt(cabin)
        memory = SomniMemory(self.work / ".pi" / "somni", "pi").load()
        refs = [r["id"] for r in memory.transcript if r.get("tool_ok")]
        self.assertEqual(len(refs), 3)
        rule = {"id": "economy_fee", "text": "The economy cancellation fee is 50 for a fare of 300.",
                "when": {"action.kind": "lookup_fee", "obs.arg_cabin": "economy", "obs.arg_price": 300},
                "then": {"fee": {"set": 50}}}
        op = {"op": "ledger_propose", "proposal": {"op": "add", "rule": rule}, "tests": {"yes": refs[0], "no": refs[1]}}
        plans["Dream pass."] = [[("write", {"path": "dream-marker", "content": "unexpected"})],
                                [("somni_dream", {"proposal_json": json.dumps({"schema": 1, "operations": [op]})})]]
        self.pi.prompt("/somni-dream cancellation fees")
        self.pi.read(lambda r: r.get("type") == "agent_settled")
        self.assertTrue(next(e for e in self.pi.tool_ends() if e["toolName"] == "write")["isError"])
        self.assertFalse((self.work / "dream-marker").exists())
        self.assertEqual(memory.load().learning_ledger().rules_view()["economy_fee"]["support"], 2)
        plans["Recall the economy"] = [[("somni_recall", {"query": "economy fee"})]]
        self.pi.prompt("Recall the economy cancellation fee")
        self.assertIn("- rule economy_fee", json.dumps(self.upstream.requests[-1]["messages"]))
        recall = next(e for e in self.pi.tool_ends() if e["toolName"] == "somni_recall")
        self.assertIn("Rule economy_fee", text_of(recall["result"]["content"]))

    def test_action_word_is_dreamed_registered_run_through_pi_and_human_edited(self):
        self.pi.close()
        env = {"SOMNI_ACTIONS": "1"}
        self.pi = PiSession("extensions/somni.ts", self.work, self.upstream.port, env=env)
        plans = {cabin: [[("lookup_fee", {"cabin": cabin, "price": 300})]] for cabin in ("economy", "business")}
        self.upstream.script = plan_script(plans)
        for cabin in ("economy", "business"):
            self.pi.prompt(cabin)
        store = self.work / ".pi" / "somni"
        memory = SomniMemory(store, "pi").load()
        refs = [r["id"] for r in memory.transcript if r["source"] == "tool_result"]
        source = ('def quote_fees(price: float):\n    """Quote economy and business cancellation fees for one fare."""\n'
                  '    economy = rpc.lookup_fee(cabin="economy", price=price)\n'
                  '    business = rpc.pi.lookup_fee({"cabin": "business", "price": price})\n'
                  '    return {"economy": economy["fee"], "business": business["fee"]}')
        op = {"op": "define_action", "source": source,
              "tests": [{"params": {"price": 300}, "refs": refs, "result": {"economy": 50, "business": 24}}]}
        plans["Dream pass."] = [[("somni_dream", {"proposal_json": json.dumps({"schema": 1, "operations": [op]})})]]
        self.pi.prompt("/somni-dream recurring tool sequences")
        self.pi.read(lambda r: r.get("type") == "agent_settled")
        dream_prompt = next(text_of(m.get("content")) for m in self.upstream.requests[-2]["messages"]
                            if text_of(m.get("content")).startswith("Dream pass."))
        self.assertIn("- lookup_fee: Return the cancellation fee", dream_prompt)
        self.assertNotIn("- somni_note", dream_prompt)
        self.assertEqual(sorted(memory.load().action_words()), ["quote_fees"])
        self.assertIn("rpc.pi.lookup_fee", (store / "somni-v1" / "actions" / "quote_fees.py").read_text())

        # Registered in the same session after the dream; its calls go through Pi.
        plans["Quote both"] = [[("quote_fees", {"price": 200})]]
        self.pi.prompt("Quote both fees for 200")
        declared = [t["function"]["name"] for t in self.upstream.requests[-1].get("tools", [])]
        self.assertIn("quote_fees", declared)
        ends = self.pi.tool_ends()
        word = next(e for e in ends if e["toolName"] == "quote_fees")
        self.assertFalse(word["isError"], text_of(word["result"]["content"]))
        self.assertEqual(json.loads(text_of(word["result"]["content"])), {"economy": 50, "business": 16})
        nested = [e for e in ends if e.get("parentToolCallId") == word["toolCallId"]]
        self.assertEqual([e["toolName"] for e in nested], ["lookup_fee", "lookup_fee"])
        texts = [r["text"] for r in memory.load().transcript if r["source"] == "tool_result"]
        self.assertTrue(texts[-3].startswith('TOOL lookup_fee({"cabin":"economy","price":200})'))
        self.assertTrue(texts[-1].startswith('TOOL quote_fees({"price":200}) -> {"economy":50,"business":16}'))

        # A human edits the file; the next session records the edit and runs the new version.
        self.pi.close()
        path = store / "somni-v1" / "actions" / "quote_fees.py"
        path.write_text(path.read_text().replace('return {"economy": economy["fee"], "business": business["fee"]}',
                                                 'return economy["fee"] + business["fee"]'))
        self.pi = PiSession("extensions/somni.ts", self.work, self.upstream.port, env=env)
        self.pi.prompt("Quote both fees for 200")
        word = [e for e in self.pi.tool_ends() if e["toolName"] == "quote_fees"][-1]
        self.assertEqual(json.loads(text_of(word["result"]["content"])), 66)
        last = memory.load().dreams[-1]
        self.assertEqual((last["author"], last["actions"][0]["op"]), ("human", "revise"))
        self.assertEqual(len(last["actions"][0]["dropped_tests"]), 1)

    def test_dream_without_the_task_tools_can_still_define_a_word_for_them(self):
        self.pi.close()
        env = {"SOMNI_ACTIONS": "1"}
        self.pi = PiSession("extensions/somni.ts", self.work, self.upstream.port, env=env)
        plans = {cabin: [[("lookup_fee", {"cabin": cabin, "price": 300})]] for cabin in ("economy", "business")}
        self.upstream.script = plan_script(plans)
        for cabin in ("economy", "business"):
            self.pi.prompt(cabin)
        for price in (100, 400):  # the same two-call sequence twice: a mined candidate
            plans[f"Both {price}"] = [[("lookup_fee", {"cabin": "economy", "price": price})],
                                      [("lookup_fee", {"cabin": "business", "price": price})]]
            self.pi.prompt(f"Both {price}")
        self.pi.close()
        store = self.work / ".pi" / "somni"
        catalog = json.loads((store / "somni-v1" / "action-tools.json").read_text())
        self.assertEqual(catalog["lookup_fee"]["description"], "Return the cancellation fee for a fare.")
        self.assertNotIn("somni_recall", catalog)

        # The dream session lacks lookup_fee (as a dream lacks a task's MCP server) but still sees and may call it.
        memory = SomniMemory(store, "pi").load()
        refs = [r["id"] for r in memory.transcript if r["source"] == "tool_result"][:2]
        source = ('def quote_fees(price: float):\n    """Quote economy and business cancellation fees for one fare."""\n'
                  '    return [rpc.lookup_fee(cabin=c, price=price)["fee"] for c in ["economy", "business"]]')
        plans["Dream pass."] = [[("somni_dream", {"proposal_json": json.dumps({"schema": 1, "operations": [
            {"op": "define_action", "source": source, "tests": [{"params": {"price": 300}, "refs": refs}]},
            {"op": "define_action", "tests": [],
             "source": 'def lookup_fee(cabin: str):\n    """Shadow a task tool."""\n    return rpc.screenshot()'}]})})]]
        self.pi = PiSession("extensions/somni.ts", self.work, self.upstream.port, env={**env, "FIXTURE_NO_FEE_TOOL": "1"})
        self.pi.prompt("/somni-dream recurring tool sequences")
        self.pi.read(lambda r: r.get("type") == "agent_settled")
        dream_prompt = next(text_of(m.get("content")) for m in self.upstream.requests[-2]["messages"]
                            if text_of(m.get("content")).startswith("Dream pass."))
        self.assertIn("- lookup_fee (task sessions): Return the cancellation fee", dream_prompt)
        self.assertIn('- 2x: lookup_fee({"cabin":"economy","price":100}) -> lookup_fee({"cabin":"business","price":100}); '
                      "arguments that vary: 1.price, 2.price. Refs of occurrences: ", dream_prompt)
        memory.load()
        self.assertEqual(sorted(memory.action_words()), ["quote_fees"])
        self.assertIn("lookup_fee is already a session tool", json.dumps(memory.dreams[-1]["rejected"]))
        self.pi.close()

        # Back in a task session with the tool, the word registers and runs.
        plans["Quote both"] = [[("quote_fees", {"price": 200})]]
        self.pi = PiSession("extensions/somni.ts", self.work, self.upstream.port, env=env)
        self.pi.prompt("Quote both fees for 200")
        word = next(e for e in self.pi.tool_ends() if e["toolName"] == "quote_fees")
        self.assertEqual(json.loads(text_of(word["result"]["content"])), [50, 16])

    def test_dream_record_settings_bound_the_prompt(self):
        self.pi.close()
        store = self.work / "dream-memory"
        store.mkdir()
        memory = SomniMemory.create(store, "pi", "test", "dream")
        memory.set_phase("open")
        memory.record("ordinary_observation", "Earlier record outside the dream window")
        memory.record("tool_result", "Recent evidence " + "雪" * 80 + " hidden tail")
        memory.set_phase("closed")
        self.pi = PiSession("extensions/somni.ts", self.work, self.upstream.port,
                            env={"SOMNI_WORKSPACE": store.name, "SOMNI_DREAM_RECORDS": "1",
                                 "SOMNI_DREAM_RECORD_BYTES": "80"})
        self.pi.prompt("/somni-dream fees")
        self.pi.read(lambda r: r.get("type") == "agent_settled")
        messages = [text_of(m.get("content")) for m in self.upstream.requests[-1]["messages"]]
        prompt = next(m for m in messages if m.startswith("Dream pass."))
        self.assertNotIn("Earlier record outside the dream window", prompt)
        excerpt = prompt.split("[t000002] (tool_result) ")[1]
        self.assertTrue(excerpt.startswith("Recent evidence 雪"))
        self.assertTrue(excerpt.endswith("[excerpt truncated]"))
        self.assertLessEqual(len(excerpt.encode("utf-8")), 80)
        self.assertIn("hidden tail", memory.load().transcript[-1]["text"])

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
