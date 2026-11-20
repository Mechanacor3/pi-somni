"""Chel's rule ledger over Somni's transcript and transactional dream log."""
import json
import re
from contextlib import nullcontext

from .chel import ledger as chel


def _field(name):
    name = re.sub("[^a-z0-9_]+", "_", name.lower()).strip("_")
    if not name or not name[0].isalpha():
        name = "f_" + name
    return name[:64].rstrip("_")


def _fields(obj):
    fields = {}

    def visit(value, path):
        if isinstance(value, dict):
            for key, child in value.items():
                visit(child, f"{path}_{key}" if path else key)
            return
        if isinstance(value, list):
            path, value = "n_" + path, len(value)
        if not chel.scalar_ok(value):
            raise ValueError("unsupported scalar")
        key = _field(path)
        if key in fields:
            raise ValueError("ambiguous field")
        fields[key] = value

    visit(obj, "")
    return fields


def _transition(record):
    if record["source"] != "tool_result" or record.get("tool_ok") is not True:
        return None
    match = re.match(r"TOOL ([\w.-]+)\(", record["text"])
    if not match:
        return None
    raw = record["text"][match.end():]
    try:
        args, end = json.JSONDecoder().raw_decode(raw)
        if not raw[end:].startswith(") -> "):
            return None
        result = json.loads(raw[end + 5:])
        if not isinstance(args, dict) or not isinstance(result, dict):
            return None
        return chel.validate_transition({
            "obs": {"arg_" + key: value for key, value in _fields(args).items()},
            "action": {"kind": _field(match[1])}, "next_obs": _fields(result),
        })
    except (ValueError, RecursionError, OverflowError):
        return None


class DreamLedger(chel.Ledger):
    """Use Chel's recorder, proposer, verb runner and recall. Somni owns disk I/O."""

    def __init__(self):
        self.rules, self.verbs, self.transitions_by_ref = {}, {}, {}
        self.history, self.revisions = [], []
        self.head = self.revisions_head = chel.GENESIS
        self.min_support = 2
        self.record_count = 0

    def _lock(self):
        return nullcontext()  # Each replay is private to its caller.

    def load(self):
        return self

    def _append(self, name, values):
        pass  # The enclosing Somni transaction persists the input records and proposals.

    def _views(self):
        pass  # The CLI renders the replayed ledger on demand.

    def ingest(self, records):
        batch = {}
        for record in records:
            transition = _transition(record)
            if transition is not None:
                batch[record["id"]] = transition
        if batch:
            self.record(batch.values())
            self.transitions_by_ref.update(batch)
        self.record_count += len(records)

    @classmethod
    def from_memory(cls, memory):
        ledger = cls()
        for dream in memory.dreams:
            if dream.get("learning"):
                ledger.ingest(memory.transcript[ledger.record_count:dream["transcript_count"]])
                for operation in dream["learning"]:
                    ledger.check_proposal(operation)
        ledger.ingest(memory.transcript[ledger.record_count:])
        return ledger

    def description(self):
        return {"history_len": len(self.history), "rules": self.rules_view(), "verbs": sorted(self.verbs)}

    def _check_cases(self, rule, tests):
        chel.validate_rule(rule, verbs=self.verbs)
        if not isinstance(tests, dict) or set(tests) != {"yes", "no"}:
            raise ValueError("rule needs yes and no transcript references")
        yes, no = (self.transitions_by_ref.get(tests[key]) for key in ("yes", "no"))
        if not yes or not no or yes is no or yes["action"] != no["action"]:
            raise ValueError("tests must cite distinct recorded transitions of the same action kind")
        with self.runtime() as rt:
            if chel.check_rule(rule, [yes], rt)[0] != 1:
                raise ValueError("yes case must support every prediction")
            if any(chel.lookup(ref, no["obs"], no["action"]) is chel.MISSING for ref in rule["when"]):
                raise ValueError("no case lacks an input checked by when")
            predicted = chel.predict(rule, no["obs"], no["action"], rt)
            if predicted is chel.MISSING:
                return
            if chel.check_rule(rule, [no], rt)[0] == 1 and any(
                yes["next_obs"][key] is True and no["next_obs"][key] is False for key in rule["then"]
            ):
                return
            raise ValueError("no case must fall outside when or correctly predict a true-to-false outcome")

    def check_proposal(self, operation):
        proposal = operation.get("proposal")
        if not isinstance(proposal, dict):
            raise ValueError("ledger_propose needs a Chel proposal")
        op = proposal.get("op")
        fields = {"op", "proposal"}
        if op in ("add", "revise"):
            fields.add("tests")
            self._check_cases(proposal.get("rule"), operation.get("tests"))
        if op == "define_verb":
            verb = proposal.get("verb")
            examples = verb.get("examples") if isinstance(verb, dict) else None
            if not isinstance(examples, list) or not all(isinstance(e, dict) for e in examples):
                raise ValueError("verb needs worked examples")
            if len({chel.canonical(e.get("args")) for e in examples}) < 2:
                raise ValueError("verb needs distinct worked inputs")
            outputs = [e.get("result") for e in examples]
            boolean_outputs = {v for v in outputs if type(v) is bool}
            if boolean_outputs and len(boolean_outputs) != 2:
                raise ValueError("Boolean verbs need both true and false worked examples")
        if set(operation) != fields:
            raise ValueError("invalid ledger_propose fields")
        result = self.propose(proposal)
        if not result["accepted"]:
            raise ValueError("; ".join(result["reasons"]))
        return operation
