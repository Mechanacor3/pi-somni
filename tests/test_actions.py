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
            "exactly one top-level def": "import os\n" + CANCEL,
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


if __name__ == "__main__":
    unittest.main()
