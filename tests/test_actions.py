"""Action words: learned Python that calls session tools through rpc.TOOL(...)."""
import json
import tempfile
import unittest

from somni import actions as A
from somni.store import SomniError, SomniMemory

CANCEL = '''
def cancel_if_refundable(booking_id: str):
    """Cancel a booking only when it is refundable, and report what happened."""
    booking = rpc.get_booking(booking_id=booking_id)
    if not booking["refundable"]:
        return {"cancelled": False, "reason": "not refundable"}
    result = rpc.pi.cancel_booking({"booking_id": booking_id, "reason": "customer request"})
    return {"cancelled": result["status"] == "cancelled"}
'''
TESTS = [{"params": {"booking_id": "BK-1"}, "refs": ["t000001", "t000002"], "result": {"cancelled": True}},
         {"params": {"booking_id": "BK-2"}, "refs": ["t000003"]}]
ON = {"author": "dream", "reserved": ["get_booking", "cancel_booking"], "allow": None}


class ActionWordTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.m = SomniMemory.create(tmp.name, "s", "l", "a")
        self.m.set_phase("open")
        self.tool("get_booking", {"booking_id": "BK-1"}, {"booking_id": "BK-1", "refundable": True})
        self.tool("cancel_booking", {"booking_id": "BK-1", "reason": "customer request"}, {"status": "cancelled"})
        self.tool("get_booking", {"booking_id": "BK-2"}, {"booking_id": "BK-2", "refundable": False})
        self.m.set_phase("closed")

    def tool(self, name, args, result, ok=True):
        return self.m.record("tool_result", f"TOOL {name}({json.dumps(args)}) -> {json.dumps(result)}", tool_ok=ok)

    def dream(self, *ops, actions=ON, partial=False):
        return self.m.dream({"schema": 1, "operations": list(ops)}, actions=actions, partial=partial)

    def define(self, source=CANCEL, tests=TESTS):
        return self.dream({"op": "define_action", "source": source, "tests": tests})

    def run_word(self, name, params, results):
        """Drive a word the way the Pi extension does, with canned tool results."""
        words = self.m.load().action_words()
        history = []
        with A.ActionRuntime(words) as runtime:
            while True:
                out = A.step(runtime, words[name], params, history)
                if "result" in out:
                    return out["result"], [h["tool"] for h in history]
                history.append({**out["call"], "result": results[out["call"]["tool"]]})

    def test_define_replays_history_and_runs_step_by_step(self):
        dream = self.define()
        self.assertEqual(dream["actions"][0]["op"], "define")
        word = self.m.load().action_words()["cancel_if_refundable"]
        self.assertEqual(word["params"], {"booking_id": "string"})
        self.assertEqual(word["uses"], ["cancel_booking", "get_booking"])
        self.assertEqual(word["evidence"], ["t000001", "t000002", "t000003"])
        self.assertEqual(self.m.status()["action_words"], ["cancel_if_refundable"])
        result, calls = self.run_word("cancel_if_refundable", {"booking_id": "BK-9"},
                                      {"get_booking": {"refundable": True}, "cancel_booking": {"status": "cancelled"}})
        self.assertEqual((result, calls), ({"cancelled": True}, ["get_booking", "cancel_booking"]))
        result, calls = self.run_word("cancel_if_refundable", {"booking_id": "BK-9"},
                                      {"get_booking": {"refundable": False}})
        self.assertEqual(calls, ["get_booking"])
        self.assertFalse(result["cancelled"])

    def test_human_readable_file_is_rendered(self):
        self.define()
        text = (self.m.store / "actions" / "cancel_if_refundable.py").read_text()
        self.assertIn("# somni: calls cancel_booking, get_booking; evidence t000001, t000002, t000003", text)
        self.assertIn("# somni: test cancel_if_refundable(booking_id='BK-1') replays t000001 t000002", text)
        self.assertIn(A.normalize(CANCEL), text)
        self.assertEqual(A.source_of(text), A.normalize(CANCEL))

    def test_gate_rejects_unsupported_words(self):
        cases = {
            "the record shows": [{"params": {"booking_id": "BK-1"}, "refs": ["t000001", "t000003"]}],
            "never exercise cancel_booking": [TESTS[1]],
            "returned": [{**TESTS[0], "result": {"cancelled": False}}],
            "not a tool_result": [{"params": {"booking_id": "BK-1"}, "refs": ["t000009"]}],
            "parameter booking_id must be string": [{"params": {"booking_id": 1}, "refs": ["t000003"]}],
        }
        for reason, tests in cases.items():
            with self.subTest(reason), self.assertRaisesRegex(SomniError, reason):
                self.define(tests=tests)
        with self.assertRaisesRegex(SomniError, "disabled"):
            self.dream({"op": "define_action", "source": CANCEL, "tests": TESTS}, actions=None)
        with self.assertRaisesRegex(SomniError, "SOMNI_ACTION_TOOLS"):
            self.dream({"op": "define_action", "source": CANCEL, "tests": TESTS}, actions={**ON, "allow": ["get_booking"]})
        with self.assertRaisesRegex(SomniError, "session tool"):
            self.dream({"op": "define_action", "source": CANCEL, "tests": TESTS},
                       actions={**ON, "reserved": ["cancel_if_refundable"]})
        self.assertFalse(self.m.load().dreams)

    def test_syntax_policy(self):
        body = '    """Doc."""\n    x = rpc.get_booking(booking_id=b)\n'
        bad = {
            "a word file holds one def": "import os\n" + CANCEL,
            "only import a word file may have": "from os import path\n" + CANCEL,
            "defines exactly one word": CANCEL + "\ndef helper(x: int):\n    \"\"\"x\"\"\"\n    return rpc.get_booking(booking_id=x)\n",
            "need `from rpc import replay`": CANCEL + '\ndef test_a():\n    with replay("t000001"):\n        cancel_if_refundable(booking_id="BK-1")\n',
            "with dir\\(\\)/help\\(\\)": 'def w(b: str):\n' + body + '    return len(rpc.mcp)\n',
            "Import is not allowed": 'def w(b: str):\n' + body + '    import os\n',
            "docstring": "def w(b: str):\n    return rpc.get_booking(booking_id=b)\n",
            "annotate": 'def w(b):\n' + body,
            "not available": 'def w(b: str):\n' + body + '    return open("/etc/passwd")\n',
            "only be used to call": 'def w(b: str):\n' + body + '    f = rpc.get_booking\n',
            "plain identifiers": 'def w(b: str):\n' + body + '    rpc._secret(b=1)\n',
            "keyword arguments or a single dict": 'def w(b: str):\n' + body + '    rpc.get_booking(b, x=1)\n',
            "Somni": 'def w(b: str):\n' + body + '    rpc.somni_note(text=b)\n',
            "rpc.mcp.SERVER.TOOL": 'def w(b: str):\n' + body + '    rpc.a.b(x=1)\n',
        }
        for reason, source in bad.items():
            with self.subTest(reason), self.assertRaisesRegex(A.ActionError, reason):
                A.parse(source)
        info = A.parse('def w(q: str, n: int):\n    """Search docs."""\n    return rpc.mcp.docs.search(query=q, limit=n)\n')
        self.assertEqual((info["uses"], info["params"]), (["mcp__docs__search"], {"q": "string", "n": "integer"}))

    def test_step_guards(self):
        self.define()
        word = self.m.load().action_words()["cancel_if_refundable"]
        with A.ActionRuntime({"cancel_if_refundable": word}) as runtime:
            forged = [{"tool": "get_booking", "args": {"booking_id": "OTHER"}, "result": {"refundable": True}}]
            with self.assertRaisesRegex(A.ActionError, "deterministically"):
                A.step(runtime, word, {"booking_id": "BK-1"}, forged)
            with self.assertRaisesRegex(A.ActionError, "takes parameters"):
                A.step(runtime, word, {}, [])
        spin = 'def spin(n: int):\n    """Spin."""\n    for i in range(n):\n        pass\n    return rpc.get_booking(booking_id="x")\n'
        with A.ActionRuntime({"spin": {"source": spin}}) as runtime:
            with self.assertRaisesRegex(A.ActionError, "CPU time"):
                A.step(runtime, {"name": "spin", "params": {"n": "integer"}, "uses": ["get_booking"]}, {"n": 10**10}, [])

    def test_words_compose_and_cycles_and_dependents_are_refused(self):
        self.define()
        self.m.set_phase("open")
        self.tool("cancel_if_refundable", {"booking_id": "BK-3"}, {"cancelled": True})
        self.m.set_phase("closed")
        outer = ('def cancel_all(first: str):\n    """Cancel one booking through the learned word."""\n'
                 '    return rpc.cancel_if_refundable(booking_id=first)\n')
        self.define(outer, [{"params": {"first": "BK-3"}, "refs": ["t000004"], "result": {"cancelled": True}}])
        cyclic = CANCEL.replace('return {"cancelled": result["status"] == "cancelled"}',
                                'return rpc.cancel_all(first=booking_id)')
        with self.assertRaisesRegex(SomniError, "call itself"):
            self.dream({"op": "revise_action", "source": cyclic})
        with self.assertRaisesRegex(SomniError, "retire cancel_all first"):
            self.dream({"op": "retire_action", "name": "cancel_if_refundable", "reason": "obsolete"})
        self.dream({"op": "retire_action", "name": "cancel_all", "reason": "obsolete"})
        self.assertEqual(sorted(self.m.load().action_words()), ["cancel_if_refundable"])
        self.assertFalse((self.m.store / "actions" / "cancel_all.py").exists())

    def test_human_edits_are_synced_as_a_human_pass(self):
        self.define()
        folder = self.m.store / "actions"
        path = folder / "cancel_if_refundable.py"
        path.write_text(path.read_text().replace('"customer request"', '"requested by customer"'))
        (folder / "lookup.py").write_text('def lookup(b: str):\n    """Fetch a booking."""\n'
                                          '    return rpc.get_booking(booking_id=b)\n')
        synced = self.m.sync_action_files()
        self.assertEqual(synced["author"], "human")
        events = {e["name"]: e for e in synced["actions"]}
        self.assertEqual(events["cancel_if_refundable"]["op"], "revise")
        self.assertEqual(len(events["cancel_if_refundable"]["dropped_tests"]), 1)  # its cancel call changed
        self.assertEqual(events["lookup"]["op"], "define")
        words = self.m.load().action_words()
        self.assertIn('"requested by customer"', words["cancel_if_refundable"]["source"])
        self.assertIsNone(self.m.sync_action_files())  # nothing changed since
        path.unlink()
        self.assertEqual(self.m.sync_action_files()["actions"][0]["op"], "retire")
        (folder / "lookup.py").write_text("def lookup(b: str):\n    return 1\n")  # no docstring, no rpc
        with self.assertRaisesRegex(SomniError, "docstring"):
            self.m.sync_action_files()
        self.assertEqual(sorted(self.m.load().action_words()), ["lookup"])

    def test_call_cap_is_configurable(self):
        source = ('def lookups(n: int):\n    """Look up n bookings."""\n'
                  '    return [rpc.get_booking(booking_id=str(i)) for i in range(n)]')
        word = {"name": "lookups", "params": {"n": "integer"}, "uses": ["get_booking"], "source": source}
        history = [{"tool": "get_booking", "args": {"booking_id": str(i)}, "result": {}} for i in range(16)]
        with A.ActionRuntime({"lookups": word}) as runtime:
            with self.assertRaisesRegex(A.ActionError, "exceeded 16 tool calls"):
                A.step(runtime, word, {"n": 20}, history[:16])
            self.assertEqual(A.step(runtime, word, {"n": 20}, history, max_calls=20)["call"]["args"],
                             {"booking_id": "16"})
            with self.assertRaisesRegex(A.ActionError, "max_calls"):
                A.step(runtime, word, {"n": 1}, [], max_calls=65)

    def test_words_seeded_before_the_first_session_are_adopted(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        folder = SomniMemory(tmp.name, "s").store / "actions"
        folder.mkdir(parents=True)
        (folder / "lookup.py").write_text('def lookup(b: str):\n    """Fetch a booking."""\n'
                                          '    return rpc.get_booking(booking_id=b)\n')
        memory = SomniMemory.create(tmp.name, "s", "l", "a")
        self.assertEqual(memory.sync_action_files()["author"], "human")
        self.assertEqual(sorted(memory.load().action_words()), ["lookup"])
        (folder / "other.py").write_text("x = 1\n")
        with self.assertRaisesRegex(SomniError, "already exists"):
            SomniMemory.create(tmp.name, "s", "l", "a")

    def test_truncated_or_failed_records_are_not_evidence(self):
        self.m.set_phase("open")
        self.m.record("tool_result", 'TOOL get_booking({"booking_id": "BK-4"}) -> {"x": 1 [body truncated: 9 UTF-8 bytes '
                      'sha256=00]', tool_ok=True)
        self.tool("get_booking", {"booking_id": "BK-5"}, {"refundable": False}, ok=False)
        self.m.set_phase("closed")
        for ref, reason in (("t000004", "truncated result"), ("t000005", "failed tool call")):
            with self.subTest(ref), self.assertRaisesRegex(SomniError, reason):
                self.define(tests=[TESTS[0], {"params": {"booking_id": "BK-4" if ref == "t000004" else "BK-5"},
                                              "refs": [ref]}])

    def test_recurring_sequences_are_mined_with_refs_that_pass_the_gate(self):
        self.m.set_phase("open")
        for booking in ("BK-3", "BK-4", "BK-5"):
            self.m.record("ordinary_observation", f"USER: cancel {booking}")
            self.tool("get_booking", {"booking_id": booking}, {"booking_id": booking, "refundable": True})
            self.m.record("agent_output", "It is refundable; cancelling.")
            self.tool("cancel_booking", {"booking_id": booking, "reason": "customer request"}, {"status": "cancelled"})
        self.m.record("ordinary_observation", "USER: cancel BK-6")
        self.tool("get_booking", {"booking_id": "BK-6"}, {"booking_id": "BK-6", "refundable": True})
        self.tool("cancel_booking", {"booking_id": "BK-6", "reason": "customer request"}, {"error": "down"}, ok=False)
        self.m.record("ordinary_observation", "USER: and BK-7")
        self.tool("get_booking", {"booking_id": "BK-7"}, {"booking_id": "BK-7", "refundable": True})
        self.tool("somni_recall", {"query": "x"}, [])
        self.tool("cancel_booking", {"booking_id": "BK-7", "reason": "customer request"}, {"status": "cancelled"})
        self.m.set_phase("closed")
        found = A.candidates(self.m.load().transcript)
        top = found[0]
        # t000001-2 (setup) plus three prompts; the failed and somni-interrupted runs do not count.
        self.assertEqual((top["tools"], top["count"]), (["get_booking", "cancel_booking"], 4))
        self.assertEqual(top["varying"], ["1.booking_id", "2.booking_id"])
        word = 'def cancel(booking_id: str):\n    """Cancel a refundable booking."""\n    rpc.get_booking(booking_id=booking_id)\n' \
               '    return rpc.cancel_booking(booking_id=booking_id, reason="customer request")'
        tests = [{"params": {"booking_id": self.m._records_by_id[refs[0]]["text"].split('"')[3]}, "refs": refs}
                 for refs in top["refs"]]
        self.assertEqual(self.define(word, tests)["actions"][0]["name"], "cancel")

    def test_index_lists_registered_words_ranked_by_the_prompt(self):
        self.define()
        self.dream({"op": "define_action", "tests": [{"params": {"booking_id": "BK-2"}, "refs": ["t000003"]}],
                    "source": 'def check_booking(booking_id: str):\n    """Look up whether a booking is refundable."""\n'
                              '    return rpc.get_booking(booking_id=booking_id)["refundable"]'})
        memory = self.m.load()
        both = ["cancel_if_refundable", "check_booking"]
        index = memory.inject(4096, query="please cancel my booking", words=both)
        lines = [line for line in index.splitlines() if line.startswith("- word ")]
        self.assertEqual(lines[0], "- word `cancel_if_refundable(booking_id: str)`: Cancel a booking only when it is "
                                   "refundable, and report what happened. (not run yet; a tool; calls cancel_booking, "
                                   "get_booking; replays t000001..t000003)")
        self.assertTrue(lines[1].startswith("- word `check_booking(booking_id: str)`"))
        self.assertIn("Learned action words: call one as a tool", index)
        # Only words this session registered are listed; none at all without the list.
        self.assertEqual(len([l for l in memory.inject(4096, words=["check_booking", "nope"]).splitlines()
                              if l.startswith("- word ")]), 1)
        self.assertNotIn("- word ", memory.inject(4096))
        self.assertNotIn("- word ", memory.inject(512, words=both))  # a quarter of a small index cannot fit one
        # Each line carries the word's recent run record, so the agent sees which words have worked.
        self.m.set_phase("open")
        self.tool("cancel_if_refundable", {"booking_id": "BK-1"}, {"cancelled": True})
        self.tool("cancel_if_refundable", {"booking_id": "BK-9"}, "stopped at call 1", ok=False)
        self.m.set_phase("closed")
        self.assertIn("(last 2 runs: 1 ok, 1 failed (latest failed); a tool;",
                      self.m.load().inject(4096, words=["cancel_if_refundable"]))
        self.m.set_phase("open")
        self.tool("cancel_if_refundable", {"booking_id": "BK-1"}, {"cancelled": True, "confirmed": True})
        self.tool("cancel_if_refundable", {"booking_id": "BK-1"}, {"cancelled": False, "confirmed": False})
        self.m.set_phase("closed")
        self.assertIn("(last 4 runs: 3 ok (1 confirmed, 1 unconfirmed), 1 failed (latest ok); a tool;",
                      self.m.load().inject(4096, words=["cancel_if_refundable"]))
        runs = self.m.action_workbench()["cancel_if_refundable"]["runs"]
        self.assertEqual([run["confirmed"] for run in runs], [False, True, None])

    def test_parameters_bind_by_name_after_the_word_is_stored(self):
        source = ('def zeta_first(zeta: str, alpha: int):\n    """Look up a booking and echo a number."""\n'
                  '    rpc.get_booking(booking_id=zeta)\n    return alpha')
        self.dream({"op": "define_action", "source": source,
                    "tests": [{"params": {"zeta": "BK-1", "alpha": 3}, "refs": ["t000001"], "result": 3}]})
        words = SomniMemory(self.m.workspace, "s").load().action_words()  # from disk: params come back key-sorted
        self.assertEqual(list(words["zeta_first"]["params"]), ["alpha", "zeta"])
        result = {"booking_id": "BK-1", "refundable": True}
        with A.ActionRuntime(words) as runtime:
            for params in ({"zeta": "BK-9", "alpha": 7}, {"alpha": 7, "zeta": "BK-9"}):
                call = A.step(runtime, words["zeta_first"], params, [])["call"]
                self.assertEqual(call, {"tool": "get_booking", "args": {"booking_id": "BK-9"}})
                done = A.step(runtime, words["zeta_first"], params, [{**call, "result": result}])
                self.assertEqual(done, {"result": 7})
        # The rendered file and the index keep the declared order.
        self.assertIn("def zeta_first(zeta: str, alpha: int):", (self.m.store / "actions" / "zeta_first.py").read_text())
        self.assertIn("- word `zeta_first(zeta: str, alpha: int)`", self.m.load().inject(4096, words=["zeta_first"]))

    def test_self_tests_in_the_file_replay_records_and_assert_on_results(self):
        source = ("from rpc import replay\n\n" + CANCEL.strip() + """

def test_refundable_is_cancelled():
    with replay("t000001", "t000002"):
        result = cancel_if_refundable(booking_id="BK-1")
        assert result["cancelled"]

def test_other_booking_is_kept():
    with replay("t000003"):
        assert cancel_if_refundable(booking_id="BK-2") == {"cancelled": False, "reason": "not refundable"}
""")
        dream = self.dream({"op": "define_action", "source": source, "tests": []})
        word = self.m.load().action_words()["cancel_if_refundable"]
        self.assertEqual(word["selftests"], ["test_refundable_is_cancelled", "test_other_booking_is_kept"])
        self.assertEqual(word["evidence"], ["t000001", "t000002", "t000003"])
        self.assertIn("self-tests test_refundable_is_cancelled, test_other_booking_is_kept ran and passed",
                      (self.m.store / "actions" / "cancel_if_refundable.py").read_text())
        # The word still runs normally; the tests are not part of its runtime.
        self.assertEqual(self.run_word("cancel_if_refundable", {"booking_id": "BK-1"},
                                       {"get_booking": {"refundable": True}, "cancel_booking": {"status": "cancelled"}})[0],
                         {"cancelled": True})
        failing = {
            "assert result\\[.cancelled.\\] == False": ('assert result["cancelled"]', 'assert result["cancelled"] == False'),
            "at t000002 the word called cancel_booking": ('"t000001", "t000002"', '"t000001", "t000002"'),
            "more calls than replay\\(t000001\\)": ('with replay("t000001", "t000002"):', 'with replay("t000001"):'),
            "stopped after 1 call": ('with replay("t000003"):', 'with replay("t000003", "t000004"):'),
        }
        self.m.set_phase("open")
        self.tool("cancel_booking", {"booking_id": "BK-2", "reason": "other"}, {"status": "cancelled"})  # t000004
        self.m.set_phase("closed")
        for reason, (old, new) in failing.items():
            bad = source.replace(old, new).replace('"customer request"', '"customer request"')
            if "cancel_booking" in reason:
                bad = source.replace('"reason": "customer request"', '"reason": "changed"')
            with self.subTest(reason), self.assertRaisesRegex(SomniError, reason):
                self.dream({"op": "revise_action", "source": bad, "tests": []})

    def test_dir_and_help_list_tools_with_signatures(self):
        schema = {"type": "object", "properties": {"code": {"type": "string"},
                                                   "action": {"enum": ["press", "down", "up"]}, "hold_ms": {"type": "integer"}},
                  "required": ["code"]}
        self.assertEqual(A.signature("mcp__body__key", schema, "Press a key."),
                         'rpc.mcp.body.key(code: str, action?: "press"|"down"|"up", hold_ms?: int): Press a key.')
        space = A.namespace({"mcp__body__key": {"description": "Press a key.", "parameters": schema},
                             "mcp__body__look": {"description": "Turn.", "parameters": {}},
                             "get_booking": {"description": "Fetch.", "parameters": {}}})
        self.assertEqual(space["names"][""], ["get_booking", "mcp", "pi"])
        self.assertEqual(space["names"]["mcp.body"], ["key", "look"])
        self.assertIn("- rpc.mcp.body.key(code: str", A.inspect("rpc.mcp.body", space))
        source = ('def peek(b: str):\n    """List body tools, then fetch."""\n    names = dir(rpc.mcp.body)\n'
                  '    rpc.get_booking(booking_id=b)\n    return {"names": names, "help": help(rpc.mcp.body.key)}')
        word = A.parse(source)
        self.assertEqual(word["uses"], ["get_booking"])  # dir/help are not calls
        with A.ActionRuntime({"peek": word}) as runtime:
            out = A.step(runtime, {**word, "name": "peek"}, {"b": "BK-1"}, [], catalog=space)
            self.assertEqual(out["call"]["tool"], "get_booking")
            out = A.step(runtime, {**word, "name": "peek"}, {"b": "BK-1"},
                         [{**out["call"], "result": {}}], catalog=space)
        self.assertEqual(out["result"]["names"], ["key", "look"])
        self.assertTrue(out["result"]["help"].startswith("rpc.mcp.body.key(code: str"))

    def test_dependencies_are_shown_and_checked(self):
        self.define()
        caller = ('def cancel_twice(first: str, second: str):\n    """Cancel two bookings."""\n'
                  '    a = rpc.cancel_if_refundable(booking_id=first)\n    b = rpc.cancel_if_refundable(booking_id=second)\n'
                  '    return [a, b]')
        self.m.set_phase("open")
        self.tool("cancel_if_refundable", {"booking_id": "BK-1"}, {"cancelled": True})   # t000004
        self.tool("cancel_if_refundable", {"booking_id": "BK-2"}, {"cancelled": False})  # t000005
        self.m.set_phase("closed")
        self.dream({"op": "define_action", "source": caller,
                    "tests": [{"params": {"first": "BK-1", "second": "BK-2"}, "refs": ["t000004", "t000005"]}]})
        folder = self.m.store / "actions"
        self.assertIn("# somni: used by cancel_twice", (folder / "cancel_if_refundable.py").read_text().replace("uses words none; ", ""))
        self.assertIn("uses words cancel_if_refundable; used by none", (folder / "cancel_twice.py").read_text())
        self.assertIn("used by cancel_twice", self.m.load().inject(4096, words=["cancel_if_refundable"]))
        # A new caller with the wrong keyword arguments is rejected.
        with self.assertRaisesRegex(SomniError, r"calls cancel_if_refundable with \['id'\]"):
            self.dream({"op": "define_action", "tests": [{"params": {"x": "BK-1"}, "refs": ["t000004"]}],
                        "source": 'def bad(x: str):\n    """x"""\n    return rpc.cancel_if_refundable(id=x)'})
        # Changing the signature of a used word is allowed but reports the dependents it breaks.
        renamed = CANCEL.replace("booking_id: str", "ref: str").replace("booking_id=booking_id", "booking_id=ref") \
            .replace('{"booking_id": booking_id,', '{"booking_id": ref,')
        tests = [{"params": {"ref": "BK-1"}, "refs": ["t000001", "t000002"]}, {"params": {"ref": "BK-2"}, "refs": ["t000003"]}]
        dream = self.dream({"op": "revise_action", "source": renamed, "tests": tests})
        from somni.store import _dream_summary
        self.assertIn("cancel_twice", _dream_summary(dream)["broken_dependents"])
        self.assertNotIn("broken_dependents", self.m.load().action_words()["cancel_if_refundable"])

    def test_try_dry_runs_the_gate_and_workbench_shows_runs(self):
        from somni.store import _action_try
        out = _action_try(self.m.load(), {"source": CANCEL, "tests": TESTS})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["results"][0]["returned"], {"cancelled": True})
        self.assertFalse(self.m.load().action_words())  # nothing saved
        bad = _action_try(self.m, {"source": CANCEL, "tests": [{"params": {"booking_id": "BK-1"}, "refs": ["t000003"]}]})
        self.assertFalse(bad["ok"])
        self.assertIn("at t000003", bad["error"])
        self.assertIn("holds:", _action_try(self.m, {"inspect": "rpc"})["inspect"])
        self.define()
        self.m.set_phase("open")
        self.m.record("tool_result", 'TOOL cancel_if_refundable({"booking_id":"BK-9"}) -> cancel_if_refundable stopped at call 1', tool_ok=False)
        self.m.set_phase("closed")
        runs = self.m.load().action_workbench()["cancel_if_refundable"]["runs"]
        self.assertEqual((runs[0]["ok"], runs[0]["args"]), (False, {"booking_id": "BK-9"}))
        self.assertIn("stopped at call 1", runs[0]["result"])


if __name__ == "__main__":
    unittest.main()
