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
import time
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


def failures(store, total, timeout=20):
    """dream_failures once `total` attempts are logged: the log is written just after the agent settles."""
    deadline = time.monotonic() + timeout
    while True:
        status = SomniMemory(store, "pi").load().status()["dream_failures"]
        if status["total"] >= total or time.monotonic() > deadline:
            return status
        time.sleep(0.1)


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
        self.assertIn("[n000001](claims.md#n000001)", (vault / "fees.md").read_text())
        self.assertIn("- [Fees](fees.md)", (vault / "index.md").read_text())
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

    def test_a_dream_cannot_use_live_tools(self):
        plans = {"economy": [[("lookup_fee", {"cabin": "economy", "price": 300})]]}
        self.upstream.script = plan_script(plans)
        self.pi.prompt("economy")
        memory = SomniMemory(self.work / ".pi" / "somni", "pi").load()
        ref = next(r["id"] for r in memory.transcript if r.get("tool_ok"))
        claim = {"op": "add", "text": "The economy cancellation fee is 50 for a fare of 300.", "status": "verified",
                 "salience": 3, "evidence": [{"ref": ref, "quote": '"fee":50'}]}
        plans["Dream pass."] = [[("write", {"path": "dream-marker", "content": "unexpected"})],
                                [("somni_dream", {"proposal_json": json.dumps({"schema": 1, "operations": [claim]})})]]
        self.pi.prompt("/somni-dream cancellation fees")
        self.pi.read(lambda r: r.get("type") == "agent_settled")
        self.assertTrue(next(e for e in self.pi.tool_ends() if e["toolName"] == "write")["isError"])
        self.assertFalse((self.work / "dream-marker").exists())
        self.assertEqual([c["text"] for c in memory.load().claims.values()], [claim["text"]])

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
        self.assertIn("- rpc.lookup_fee(cabin: str, price: float): Return the cancellation fee for a fare.\n", dream_prompt)
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
        # The word's own record times its nested calls: when each began after the word started, and how long it took.
        timed = [r for r in memory.transcript if r["source"] == "tool_result"][-1]["calls"]
        self.assertEqual([(c["tool"], c["ok"]) for c in timed], [("lookup_fee", True), ("lookup_fee", True)])
        self.assertTrue(all(type(c["start_ms"]) is int and type(c["ms"]) is int and c["ms"] >= 0 for c in timed))
        self.assertGreaterEqual(timed[1]["start_ms"], timed[0]["start_ms"] + timed[0]["ms"])
        self.assertNotIn("start_ms", texts[-1])  # not in the result text a calling word parses
        results = [r for r in memory.transcript if r["source"] == "tool_result"]
        self.assertEqual([r.get("via_word") for r in results[-3:]], ["quote_fees", "quote_fees", None])
        version = json.loads((REPO / "package.json").read_text())["version"]
        self.assertEqual(results[-1]["word"]["somni"], version)  # which word version, on which Somni, made the run
        self.assertNotIn("word", results[-2])
        use = memory.status()["word_use"]
        self.assertEqual((use["word_calls"], use["calls_made_by_words"], use["direct_calls_to_wrapped_tools"]), (1, 2, 2))
        # The next dream's workbench shows that timeline beside the run.
        plans["Dream pass."] = [[("somni_action_try", {"inspect": "rpc"})]]
        self.pi.prompt("/somni-dream word timing")
        self.pi.read(lambda r: r.get("type") == "agent_settled")
        dream_prompt = next(text_of(m.get("content")) for m in reversed(self.upstream.requests[-1]["messages"])
                            if text_of(m.get("content")).startswith("Dream pass."))
        self.assertRegex(dream_prompt, r"\n  timing: lookup_fee \+\d+ms \d+ms, lookup_fee \+\d+ms \d+ms\n")

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
        plans["Dream pass."] = [[("somni_action_try", {"source": source, "tests": [{"params": {"price": 300}, "refs": refs}]})],
                                [("somni_action_try", {"inspect": "rpc"})],
                                [("somni_dream", {"proposal_json": json.dumps({"schema": 1, "operations": [
            {"op": "define_action", "source": source, "tests": [{"params": {"price": 300}, "refs": refs}]},
            {"op": "define_action", "tests": [],
             "source": 'def lookup_fee(cabin: str):\n    """Shadow a task tool."""\n    return rpc.screenshot()'}]})})]]
        self.pi = PiSession("extensions/somni.ts", self.work, self.upstream.port, env={**env, "FIXTURE_NO_FEE_TOOL": "1"})
        self.pi.prompt("/somni-dream recurring tool sequences")
        self.pi.read(lambda r: r.get("type") == "agent_settled")
        dream_prompt = next(text_of(m.get("content")) for m in self.upstream.requests[-2]["messages"]
                            if text_of(m.get("content")).startswith("Dream pass."))
        self.assertIn("- rpc.lookup_fee(cabin: str, price: float): Return the cancellation fee for a fare. (task sessions)", dream_prompt)
        self.assertIn('- 2x: rpc.lookup_fee({"cabin":"economy","price":100}) -> rpc.lookup_fee({"cabin":"business","price":100}); '
                      "arguments that vary: 1.price, 2.price. Refs of occurrences: ", dream_prompt)
        tries = [e for e in self.pi.tool_ends() if e["toolName"] == "somni_action_try"]
        self.assertEqual(tries[0]["result"]["details"]["results"][0]["returned"], [50, 24])
        self.assertIn("rpc.lookup_fee(cabin: str, price: float)", text_of(tries[1]["result"]["content"]))
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
        # The actor's injected index names the word, not only its tool declaration.
        index = "\n".join(text_of(m.get("content")) for m in self.upstream.requests[-1]["messages"])
        self.assertIn(f"- word `quote_fees(price: float)`: Quote economy and business cancellation fees for one fare. "
                      f"(not run yet; a tool; calls lookup_fee; replays {refs[0]}..{refs[1]})", index)
        self.assertNotIn("## Using a learned action word", index)  # read is active: the skill is listed instead
        self.pi.close()

        # Without the read tool, Pi lists no --skill skill, so the index carries the word guidance, once per session.
        self.pi = PiSession("extensions/somni.ts", self.work, self.upstream.port, env=env,
                            extra_args=["--no-builtin-tools"])
        hidden = []
        for _ in range(2):
            self.pi.prompt("Quote both fees for 200")
            hidden.append("\n".join(text_of(m.get("content")) for m in self.upstream.requests[-1]["messages"]))
        self.assertEqual(hidden[0].count("## Using a learned action word\nCall it like any tool."), 1)
        self.assertIn("Check the effect with a tool before you say the work is done.", hidden[0])
        self.assertEqual(hidden[1].count("## Using a learned action word"), 1)  # the first turn's, not a second copy

    def test_tools_that_register_late_are_still_declared_for_dreams(self):
        self.pi.close()
        self.upstream.script = plan_script({"Use the late tool": [[("late_tool", {"n": 3})]]})
        self.pi = PiSession("extensions/somni.ts", self.work, self.upstream.port,
                            env={"SOMNI_ACTIONS": "1", "FIXTURE_LATE_TOOL": "1"})
        self.pi.prompt("Use the late tool")
        end = next(e for e in self.pi.tool_ends() if e["toolName"] == "late_tool")
        self.assertFalse(end["isError"], text_of(end["result"]["content"]))
        catalog = json.loads((self.work / ".pi" / "somni" / "somni-v1" / "action-tools.json").read_text())
        self.assertEqual(catalog["late_tool"]["parameters"]["required"], ["n"])

    @unittest.skipUnless(BINARY, "the daydream worker starts the host Pi binary")
    def test_daydream_commits_a_live_pass_while_the_session_keeps_recording(self):
        self.pi.close()
        proposal = {"schema": 1, "operations": [{"op": "add", "text": "Economy cancellation fees have a $50 floor.",
                                                 "status": "verified", "salience": 3,
                                                 "evidence": [{"ref": "t000002", "quote": "\"fee\":50"}]}]}
        self.upstream.script = plan_script({
            "What is the fee": [[("lookup_fee", {"cabin": "economy", "price": 300})]],
            "Dream pass.": [[("somni_dream", {"proposal_json": json.dumps(proposal)})]],
            "Anything else": [],
        })
        # The worker lingers after its pass commits, so the next turn starts while it is still running.
        linger = self.work / "linger-pi"
        linger.write_text(f'#!/bin/sh\n"{shutil.which(BINARY)}" "$@"\nstatus=$?\nsleep 4\nexit $status\n')
        linger.chmod(0o755)
        env = {"SOMNI_DAYDREAM_EVERY_TURNS": "1", "SOMNI_DAYDREAM_COMMAND": str(linger)}
        self.pi = PiSession("extensions/somni.ts", self.work, self.upstream.port, env=env)
        self.pi.prompt("What is the fee to cancel economy fare 300?")
        store = self.work / ".pi" / "somni"
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline and not SomniMemory(store, "pi").load().dreams:
            time.sleep(0.5)
        memory = SomniMemory(store, "pi").load()
        self.assertEqual(len(memory.dreams), 1, "the daydream did not commit a pass")
        live = memory.dreams[0]
        self.assertTrue(live["live"])
        self.assertGreaterEqual(live["usage"]["calls"], 1)  # the worker reports what it spent
        self.assertEqual(memory.state["phase"], "open")  # the owning session kept recording throughout
        self.assertIn("n000001", memory.claims)
        # The next turn carries the notice and the new claim.
        self.pi.prompt("Anything else?")
        injected = "\n".join(text_of(m.get("content")) for m in self.upstream.requests[-1]["messages"])
        self.assertRegex(injected, r"Somni daydream d000001: \+1 claim\(s\), 0 merged, 0 rejected "
                                   r"\(records up to \d+, \$\d+\.\d\d, \d+k tok, \d+s\)")
        self.assertIn("Economy cancellation fees have a $50 floor.", injected)
        self.assertGreater(len(SomniMemory(store, "pi").load().transcript), live["transcript_count"])
        # That turn announced the pass to the user too, and the worker's exit doesn't announce it again.
        shown = lambda: [e.get("message", "") for e in self.pi.events if e.get("method") == "notify"]
        self.assertEqual(sum("Somni daydream d000001" in message for message in shown()), 1, shown())
        time.sleep(5)
        self.pi.prompt("Anything else?")
        self.assertEqual(sum("Somni daydream d000001" in message for message in shown()), 1, shown())

    @unittest.skipUnless(BINARY, "the daydream worker starts the host Pi binary")
    def test_a_forked_daydream_starts_from_the_session_and_keeps_its_prefix(self):
        self.pi.close()
        proposal = {"schema": 1, "operations": [{"op": "add", "text": "Economy cancellation fees have a $50 floor.",
                                                 "status": "verified", "salience": 3,
                                                 "evidence": [{"ref": "t000002", "quote": "\"fee\":50"}]}]}
        self.upstream.script = plan_script({
            "What is the fee": [[("lookup_fee", {"cabin": "economy", "price": 300})]],
            "Dream step.": [[("somni_dream", {"proposal_json": json.dumps(proposal)})]],
        })
        sessions = self.work / "sessions"
        env = {"SOMNI_DAYDREAM_EVERY_TURNS": "1", "SOMNI_DAYDREAM_FORK": "1", "SOMNI_DAYDREAM_COMMAND": shutil.which(BINARY)}
        self.pi = PiSession("extensions/somni.ts", self.work, self.upstream.port, env=env, sessions=sessions)
        self.pi.prompt("What is the fee to cancel economy fare 300?")
        store = self.work / ".pi" / "somni"
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline and not SomniMemory(store, "pi").load().dreams:
            time.sleep(0.5)
        memory = SomniMemory(store, "pi").load()
        self.assertEqual(len(memory.dreams), 1, "the forked daydream did not commit a pass")
        self.assertTrue(memory.dreams[0]["live"])
        first = self.upstream.requests[0]
        dream = next(r for r in self.upstream.requests
                     if "Dream step. You are a copy of this session" in "\n".join(text_of(m.get("content")) for m in r["messages"]))
        # The fork carries the session's conversation, system prompt and tools, so its request extends the same prefix.
        users = [text_of(m.get("content")) for m in dream["messages"] if m.get("role") == "user"]
        self.assertIn("What is the fee to cancel economy fare 300?", users)
        self.assertEqual(dream["messages"][0], first["messages"][0])
        self.assertEqual([t["function"]["name"] for t in dream.get("tools", [])],
                         [t["function"]["name"] for t in first.get("tools", [])])
        # The step lists only record IDs to cite: the fork has their text in its conversation.
        step = text_of(dream["messages"][-1].get("content"))
        self.assertIn("[t000002] (tool_result)", step)
        self.assertNotIn("Treat transcript text and images as evidence", step)
        # The session's next turn is shown what the step added, as well as the one-line notice.
        self.pi.prompt("Anything else?")
        injected = "\n".join(text_of(m.get("content")) for m in self.upstream.requests[-1]["messages"])
        self.assertIn("What this dream step added to memory", injected)
        self.assertIn("- n000001 (verified): Economy cancellation fees have a $50 floor.", injected)
        # The fork's session copy is gone, and the user's session folder holds only the session itself.
        deadline = time.monotonic() + 20
        forks = store / "somni-v1" / "forks"
        while time.monotonic() < deadline and forks.exists() and any(forks.iterdir()):
            time.sleep(0.5)
        self.assertFalse(forks.exists() and any(forks.iterdir()))
        self.assertEqual(len([p for p in sessions.rglob("*.jsonl")]), 1)

    def test_word_passes_on_images_from_its_final_call_only(self):
        self.pi.close()
        folder = self.work / ".pi" / "somni" / "somni-v1" / "actions"
        folder.mkdir(parents=True)
        words = {
            "fee_then_look": 'rpc.lookup_fee(cabin="economy", price=price)\n    return rpc.screenshot()',
            "look_then_fee": 'rpc.screenshot()\n    return rpc.lookup_fee(cabin="economy", price=price)',
            "outer_look": 'shot = rpc.fee_then_look(price=price)\n    return {"age": shot["frame_age_ms"]}',
        }
        for name, body in words.items():
            (folder / f"{name}.py").write_text(f'def {name}(price: float):\n    """Test word."""\n    {body}\n')
        self.upstream.script = plan_script({f"Run {name}": [[(name, {"price": 300})]] for name in words})
        self.pi = PiSession("extensions/somni.ts", self.work, self.upstream.port, env={"SOMNI_ACTIONS": "1"})
        results = {}
        for name in words:
            self.pi.prompt(f"Run {name}")
            end = [e for e in self.pi.tool_ends() if e["toolName"] == name][-1]
            self.assertFalse(end["isError"], text_of(end["result"]["content"]))
            results[name] = end["result"]
        kinds = {name: [part["type"] for part in r["content"]] for name, r in results.items()}
        self.assertEqual(kinds, {"fee_then_look": ["text", "image"], "look_then_fee": ["text"],
                                 "outer_look": ["text", "image"]})  # its final call, a word, ended with the image
        self.assertEqual(results["fee_then_look"]["details"]["images_from"], "screenshot")
        self.assertEqual(results["outer_look"]["details"]["images_from"], "fee_then_look")
        self.assertEqual(json.loads(text_of(results["fee_then_look"]["content"])), {"frame_time": 100, "frame_age_ms": 25})
        # The image does not disturb a calling word, which still parses the text as JSON.
        self.assertEqual(json.loads(text_of(results["outer_look"]["content"])), {"age": 25})

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

    def test_a_second_pi_session_contributes_records_but_cannot_change_the_store(self):
        self.pi.prompt("What is the fee to cancel economy fare 300?")
        store = self.work / ".pi" / "somni"
        before = SomniMemory(store, "pi").load().state["transcript_count"]
        other = PiSession("extensions/somni.ts", self.work, self.upstream.port)  # a sub-agent in the same directory
        try:
            other.prompt("Remember that for later.")
            warnings = [e.get("message", "") for e in other.events if e.get("method") == "notify"]
            self.assertFalse(any("opened without recording" in msg for msg in warnings), warnings)
            note = next(e for e in other.tool_ends() if e["toolName"] == "somni_note")
            self.assertTrue(note["isError"])  # claims, phases and dreams stay with the owner
            self.assertIn("owns recording", text_of(note["result"]["content"]))
        finally:
            other.close()
        memory = SomniMemory(store, "pi").load()
        added = memory.transcript[before:]
        self.assertTrue(added, "the second session's work was not recorded")
        self.assertTrue(all(any(t.startswith("contributor:") for t in r.get("tags", [])) for r in added))
        self.assertIn("USER: Remember that for later.", [r["text"] for r in added])
        self.assertEqual(memory.state["phase"], "open")  # its exit didn't close the owner's session
        self.pi.prompt("Anything else?")  # and the owner still records
        self.assertGreater(SomniMemory(store, "pi").load().state["transcript_count"], memory.state["transcript_count"])

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
            "transcript_count": status["transcript_count"], "added_count": 1, "rejected_count": 0, "merged_count": 0,
            "added_ids": ["n000001"],
        })

    def test_dream_tools_show_the_budget_left_and_a_rejected_pass_can_be_resubmitted(self):
        self.pi.close()
        env = {"SOMNI_DREAM_MAX_READS": "3", "SOMNI_DREAM_MAX_SUBMISSIONS": "2", "SOMNI_DREAM_MAX_IMAGES": "5"}
        self.pi = PiSession("extensions/somni.ts", self.work, self.upstream.port, env=env)
        self.pi.prompt("What is the fee")
        bad = {"op": "add", "text": "Bad quote.", "status": "verified", "evidence": [{"ref": "t000002", "quote": "nope"}]}
        good = {"op": "add", "text": "Look up the fee first.", "status": "uncertain", "evidence": [{"ref": "t000002"}]}
        plans = dict(PLANS)
        plans["Dream pass."] = [[("somni_search", {"query": "fee", "limit": 80})],
                                [("somni_dream", {"proposal_json": json.dumps({"schema": 1, "operations": [bad]})})],
                                [("somni_recall", {"query": "fee"})],
                                [("somni_search", {"query": "economy"})],
                                [("somni_search", {"query": "again"})],
                                [("somni_dream", {"proposal_json": json.dumps({"schema": 1, "operations": [good]})})]]
        self.upstream.script = plan_script(plans)
        self.pi.prompt("/somni-dream fees")
        self.pi.read(lambda r: r.get("type") == "agent_settled")
        ends = [e for e in self.pi.tool_ends() if e["toolName"].startswith("somni_")]
        texts = [text_of(e["result"]["content"]) for e in ends]
        self.assertEqual([e["toolName"] for e in ends], ["somni_search", "somni_dream", "somni_recall", "somni_search",
                                                         "somni_search", "somni_dream"])
        self.assertTrue(texts[0].startswith("(limit 80 clamped to 50)\n"))
        self.assertTrue(texts[0].endswith("[dream budget left: images 5/5, reads 2/3, submissions 2/2]"))
        self.assertTrue(ends[1]["isError"])
        self.assertIn("nothing committed: all 1 operation(s) were rejected. op 0 (add): evidence quote", texts[1])
        self.assertIn("submissions 1/2] Nothing was committed. Fix or drop the rejected operations and resubmit; "
                      "1 attempt left.", texts[1])
        self.assertTrue(texts[3].endswith("reads 0/3, submissions 1/2] No reads left: submit the pass now."))
        self.assertIn("Dream evidence read limit reached (3 reads). Submit the pass now", texts[4])
        self.assertFalse(ends[5]["isError"], texts[5])
        memory = SomniMemory(self.work / ".pi" / "somni", "pi").load()
        self.assertEqual([c["text"] for c in memory.claims.values()], ["Look up the fee first."])

    def test_failed_dreams_are_logged_by_cause(self):
        self.pi.prompt("What is the fee")
        bad = {"op": "add", "text": "Bad quote.", "status": "verified", "evidence": [{"ref": "t000002", "quote": "nope"}]}
        plans = dict(PLANS)
        self.upstream.script = plan_script(plans)
        store = self.work / ".pi" / "somni"
        for logged, (kind, plan) in enumerate((("no_submission", [[("somni_search", {"query": "fee"})]]),
                           ("all_rejected", [[("somni_dream", {"proposal_json": json.dumps({"schema": 1, "operations": [bad]})})]]),
                           ("provider_error", [{"http_error": 400, "text": "quota exhausted for this test"}])), 1):
            plans["Dream pass."] = plan
            self.pi.prompt(f"/somni-dream {kind}")
            self.pi.read(lambda r: r.get("type") == "agent_settled")
            failures(store, logged)
        status = failures(store, 3)
        self.assertEqual(status["by_cause"], {"all_rejected": 1, "no_submission": 1, "provider_error": 1})
        self.assertEqual(status["by_kind"], {"dream": 3})
        self.assertIn("quota exhausted for this test", status["last"]["detail"])
        log = [json.loads(line) for line in (store / "somni-v1" / "dream-failures.jsonl").read_text().splitlines()]
        self.assertEqual([(e["cause"], e["reads"], e["submissions"]) for e in log],
                         [("no_submission", 1, 0), ("all_rejected", 0, 1), ("provider_error", 0, 0)])

    def test_a_dream_cut_off_by_the_provider_keeps_its_reads_for_the_retry(self):
        self.pi.prompt("What is the fee")
        store = self.work / ".pi" / "somni"
        good = {"op": "add", "text": "Economy fees have a floor.", "status": "verified",
                "evidence": [{"ref": "t000002", "quote": "\"fee\":50"}]}
        plans = dict(PLANS)
        plans["Dream pass."] = [[("somni_search", {"query": q})] for q in ("fee", "economy", "cabin", "price")] + [
            [("somni_recall", {"query": "fee"})], {"http_error": 500, "text": "upstream overloaded"}]
        self.upstream.script = plan_script(plans)
        self.pi.prompt("/somni-dream fees")
        self.pi.read(lambda r: r.get("type") == "agent_settled")
        self.assertEqual(len(json.loads((store / "somni-v1" / "dream-cache.json").read_text())["reads"]), 5)
        self.assertEqual(failures(store, 1)["by_cause"], {"provider_error": 1})

        # The retry on the same records gets the five reads in its prompt and can submit at once.
        plans["Dream pass."] = [[("somni_dream", {"proposal_json": json.dumps({"schema": 1, "operations": [good]})})]]
        before = len(self.pi.tool_ends())
        self.pi.prompt("/somni-dream fees")
        self.pi.read(lambda r: r.get("type") == "agent_settled")
        prompt = next(text_of(m.get("content")) for m in reversed(self.upstream.requests[-1]["messages"])
                      if text_of(m.get("content")).startswith("Dream pass."))
        self.assertIn("## Evidence from an interrupted attempt", prompt)
        self.assertIn("These are the 5 read(s) it made", prompt)
        self.assertIn('somni_search({"query":"cabin"}) ->', prompt)
        self.assertIn('somni_recall({"query":"fee"}) ->', prompt)
        self.assertNotIn("[dream budget left", prompt)  # cached without the old budget line
        tools = [e["toolName"] for e in self.pi.tool_ends()[before:]]
        self.assertEqual(tools, ["somni_dream"])  # no repeated reads
        memory = SomniMemory(store, "pi").load()  # loading re-verifies the chains
        self.assertEqual([c["text"] for c in memory.claims.values()], ["Economy fees have a floor."])
        self.assertFalse((store / "somni-v1" / "dream-cache.json").exists())  # a committed pass clears it

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

    def test_a_config_file_sets_options_and_somni_doctor_checks_it_offline(self):
        self.pi.close()
        (self.work / ".pi" / "somni.conf").write_text("store:\n  path: memory-here\nindex.bytes: 300\nactions.enable: true\n")
        code, out, err = PiSession("extensions/somni.ts", self.work, self.upstream.port, mode="text",
                                   prompt="Anything new?").finish()
        self.assertEqual(code, 0, err)
        self.assertIn("Somni config: 1 problem(s)", err)
        self.assertIn("somni.conf:4: actions.enable is not a Somni setting; did you mean actions.enabled?", err)
        self.assertTrue((self.work / "memory-here" / "somni-v1").is_dir())  # store.path from the file
        requests = len(self.upstream.requests)
        code, _, out = PiSession("extensions/somni.ts", self.work, self.upstream.port, mode="text",
                                 prompt="/somni-doctor").finish()  # headless Pi reports on stderr
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.upstream.requests), requests)  # no model call
        self.assertIn("Somni doctor (pi-somni", out)
        self.assertIn("did you mean actions.enabled?", out)
        self.assertRegex(out, r"index\.bytes\s+300\s+\(.*somni\.conf:3\)")  # sourced from the file, not the environment
        self.assertIn("also holds a store", out)  # the session before the file used .pi/somni
        self.assertIn("bin/somni doctor", out)

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
