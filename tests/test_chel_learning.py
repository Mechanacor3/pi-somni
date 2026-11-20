"""Chel learning through Somni's transcript, dream transaction, and index."""
import json
import tempfile
import unittest

from somni.store import SomniError, SomniMemory


class LearningTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.m = SomniMemory.create(tmp.name, "s", "l", "a")
        self.m.set_phase("open")
        for confirm in (True, False, True):
            self.record(confirm, confirm)
        self.m.set_phase("closed")
        self.rule = {"id": "consent", "text": "A confirmed request commits the change.",
                     "when": {"action.kind": "apply", "obs.arg_confirm": True}, "then": {"committed": {"set": True}}}
        self.op = {"op": "ledger_propose", "proposal": {"op": "add", "rule": self.rule},
                   "tests": {"yes": "t000001", "no": "t000002"}}

    def record(self, confirm, committed):
        args = json.dumps({"confirm": confirm})
        result = json.dumps({"committed": committed})
        return self.m.record("tool_result", f"TOOL apply({args}) -> {result}", tool_ok=True)

    def dream(self, *ops, **opts):
        return self.m.dream({"schema": 1, "operations": list(ops)}, learning=True, **opts)

    def test_accept_reload_inject_and_falsify(self):
        self.dream(self.op)
        self.assertIn("- rule consent (2 supporting", self.m.load().inject(query="confirmed", learning=True))
        self.m.set_phase("open")
        self.record(True, False)
        self.assertNotIn("- rule consent", self.m.inject(query="confirmed", learning=True))
        self.assertEqual(self.m.learning_ledger().rules_view()["consent"]["state"], "falsified")

    def test_bad_tests_and_full_history_contradiction(self):
        with self.assertRaisesRegex(SomniError, "no case"):
            self.dream({**self.op, "tests": {"yes": "t000001", "no": "t000003"}})
        with self.assertRaisesRegex(SomniError, "disabled"):
            self.m.dream({"schema": 1, "operations": [self.op]})
        self.m.set_phase("open")
        self.record(True, False)
        self.m.set_phase("closed")
        with self.assertRaisesRegex(SomniError, "contradicted"):
            self.dream(self.op)
        self.assertFalse(self.m.load().dreams)

    def test_aborted_or_partially_accepted_dream_keeps_ledger_consistent(self):
        bad = {"op": "ledger_propose", "proposal": {"op": "retract", "target": "missing", "reason": "obsolete"}}
        with self.assertRaises(SomniError):
            self.dream(self.op, bad)
        self.assertEqual(self.m.learning_ledger().rules_view(), {})
        committed = self.dream(self.op, bad, partial=True)
        self.assertEqual(committed["operation_count"], 1)
        self.assertEqual(committed["rejected"][0]["index"], 1)
        self.assertEqual(list(self.m.load().learning_ledger().active()), ["consent"])

    def test_checked_verb_and_boolean_rule(self):
        verb = {"name": "consented", "params": ["x"], "doc": "Reports consent",
                "source": "def consented(x):\n    return x", "examples": [
                    {"args": [True], "result": True}, {"args": [False], "result": False}]}
        define = {"op": "ledger_propose", "proposal": {"op": "define_verb", "verb": verb}}
        rule = {**self.rule, "when": {"action.kind": "apply"}, "then": {"committed": {
            "expr": {"op": "call", "verb": "consented", "args": [{"ref": "obs.arg_confirm"}]}}}}
        self.dream(define, {**self.op, "proposal": {"op": "add", "rule": rule}})
        self.assertEqual(self.m.learning_ledger().rules_view()["consent"]["support"], 3)
        for changed in ({"source": "import os\ndef consented(x):\n    return x"},
                        {"examples": [{"args": [True], "result": True}, {"args": [1], "result": True}]},
                        {"source": "def consented(x):\n    return not x",
                         "examples": [{"args": [True], "result": False}, {"args": [False], "result": True}]}):
            with self.assertRaises(SomniError):
                self.dream({"op": "ledger_propose", "proposal": {"op": "define_verb", "verb": {**verb, **changed}}})
        self.assertEqual(self.m.learning_ledger().description()["verbs"], ["consented"])
