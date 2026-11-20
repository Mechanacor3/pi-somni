#!/usr/bin/env python3
"""Chel open-world rule ledger: executable beliefs that must survive ALL history.

Memento (`memento_memory.py`) is Chel's closed-world model: one ψ/θ pair must
predict every field of every next observation.  That suits a small game state.
Tool-using agents see the opposite: wide, sparse observations where most fields
are unpredictable from what the agent knows.  The ledger keeps Memento's core
discipline -- a belief is only accepted if it replays the complete history H
without a single contradiction -- but lets each rule claim only what it states.

  H        append-only, hash-chained transitions {obs, action, next_obs}
           (flat scalar fields; numbers may be floats).
  rule     {id, text, when, then}.  `when` uses ψ's condition grammar over
           `obs.<field>` / `action.<field>`.  `then` maps next-observation
           fields to an effect: {"set": v}, {"copy": "obs.f"|"action.f"} or
           {"expr": E}, where E is a small terminating expression language
           (const, ref, add sub mul div min max round abs, eq ne lt le gt ge,
           and or not, if, and `call` of an agent-defined verb).  The text is
           the human-readable statement; the when/then clause is what gets checked.
  verb     an agent-written pure function (see verbs.py) that rules can call,
           e.g. date differences.  A verb is accepted only if it passes the
           static policy and reproduces its own worked examples in the sandbox;
           redefining a verb is refused if any active rule would then break.

A transition *supports* a rule when `when` holds, every `then` field is present
in next_obs, and every prediction matches (numbers within `TOLERANCE`).  It is a
*counterexample* when `when` holds, the field is present, and a prediction
differs.  Missing fields or undefined predictions abstain.

Acceptance gate (identical for add and revise): the rule is well-formed, makes
at least one claim, is not a duplicate, has at least `min_support` supporting
transitions and ZERO counterexamples across all of H.  Recording new history
re-checks every active rule; a rule that now has a counterexample becomes
`falsified` (kept, with its counterexample, never silently deleted).

All state lives in `<workspace>/chel-ledger/`.  No network, clock, environment
or paths outside the workspace are read.  Identical operation sequences give
byte-identical files.

CLI:
  ledger.py --workspace W init [--min-support 2]
  ledger.py --workspace W record  < transitions.jsonl
  ledger.py --workspace W propose < {"op": "add"|"revise"|"retract"|"define_verb", ...}
  ledger.py --workspace W status | rules | recall "query" [--top-k 3]
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import pathlib
import re
import sys
import tempfile

# Under `python3 -I` the script directory is not on sys.path; import siblings explicitly.
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from verbs import MAX_EXAMPLES, VerbError, VerbRuntime, check_source

SCHEMA = 1
LEDGER_DIR = "chel-ledger"
FIELD = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
RULE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
FORBIDDEN_FRAGMENTS = ("evaluator", "scorer", "credential", "secret", "password", "api_key", "access_token")
MAX_FIELDS = 256
MAX_STR = 256
MAX_TEXT = 2_000
MAX_RULES = 4_096
MAX_HISTORY = 200_000
MAX_CONDITIONS = 16
MAX_EFFECTS = 16
MAX_LIST = 64
MAX_NODES = 64
MAX_DEPTH = 8
MAX_REPORTED = 5
TOLERANCE = 0.01
NUM_LIMIT = 1e12
GENESIS = hashlib.sha256(b"chel-ledger-H-v1").hexdigest()
COMPARATORS = ("ne", "lt", "le", "gt", "ge")
ARITY = {"add": 2, "sub": 2, "mul": 2, "div": 2, "min": -2, "max": -2, "round": -1, "abs": 1,
         "eq": 2, "ne": 2, "lt": 2, "le": 2, "gt": 2, "ge": 2, "and": -1, "or": -1, "not": 1, "if": 3}
MISSING = object()


class LedgerError(ValueError):
    pass


def fail(message):
    raise LedgerError(message)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def is_num(value):
    return type(value) in (int, float) and math.isfinite(value) and abs(value) <= NUM_LIMIT


def scalar_ok(value):
    return value is None or type(value) is bool or is_num(value) or (type(value) is str and len(value) <= MAX_STR)


def same(a, b):
    if is_num(a) and is_num(b):
        return abs(a - b) <= TOLERANCE
    return type(a) is type(b) and a == b


def check_field(name, where):
    if not isinstance(name, str) or not FIELD.fullmatch(name):
        fail(f"{where}: invalid field name {name!r}")
    if any(fragment in name for fragment in FORBIDDEN_FRAGMENTS):
        fail(f"{where}: field name {name!r} looks like a privileged channel")


def check_ref(ref, where):
    if not isinstance(ref, str) or "." not in ref:
        fail(f"{where}: reference must be obs.<field> or action.<field>")
    scope, field = ref.split(".", 1)
    if scope not in ("obs", "action"):
        fail(f"{where}: reference scope must be obs or action")
    check_field(field, where)


def validate_state(state, where):
    if not isinstance(state, dict) or len(state) > MAX_FIELDS:
        fail(f"{where}: must be an object with at most {MAX_FIELDS} fields")
    for key, value in state.items():
        check_field(key, where)
        if not scalar_ok(value):
            fail(f"{where}.{key}: values must be null, bool, finite number, or short string")
    return dict(state)


def validate_transition(transition):
    if not isinstance(transition, dict) or set(transition) != {"obs", "action", "next_obs"}:
        fail("transition: expected exactly obs, action, next_obs")
    return {key: validate_state(transition[key], f"transition.{key}") for key in ("obs", "action", "next_obs")}


# ------------------------------------------------------------------ rule grammar

def _validate_expr(expr, depth, counter, where, verbs=None):
    counter[0] += 1
    if counter[0] > MAX_NODES or depth > MAX_DEPTH:
        fail(f"{where}: expression too large or deep (max {MAX_NODES} nodes, depth {MAX_DEPTH})")
    if not isinstance(expr, dict) or len(expr) not in (1, 2, 3):
        fail(f"{where}: expression must be {{const}}, {{ref}} or {{op, args}}")
    if set(expr) == {"const"}:
        if not scalar_ok(expr["const"]):
            fail(f"{where}: invalid constant")
        return
    if set(expr) == {"ref"}:
        check_ref(expr["ref"], where)
        return
    if set(expr) == {"op", "verb", "args"} and expr["op"] == "call":
        verbs = verbs or {}
        if expr["verb"] not in verbs:
            fail(f"{where}: unknown verb {expr['verb']!r}; define it first (known: {sorted(verbs)})")
        if not isinstance(expr["args"], list) or len(expr["args"]) != len(verbs[expr["verb"]]["params"]):
            fail(f"{where}: verb {expr['verb']} takes {len(verbs[expr['verb']]['params'])} arguments")
        for arg in expr["args"]:
            _validate_expr(arg, depth + 1, counter, where, verbs)
        return
    if set(expr) != {"op", "args"} or expr["op"] not in ARITY or not isinstance(expr["args"], list):
        fail(f"{where}: unknown expression {sorted(expr)} / op {expr.get('op')!r}")
    arity, count = ARITY[expr["op"]], len(expr["args"])
    if (arity >= 0 and count != arity) or (arity < 0 and not -arity <= count <= 8):
        fail(f"{where}: op {expr['op']} has wrong number of arguments")
    for arg in expr["args"]:
        _validate_expr(arg, depth + 1, counter, where, verbs)


def validate_rule(rule, where="rule", verbs=None):
    if not isinstance(rule, dict) or set(rule) != {"id", "text", "when", "then"}:
        fail(f"{where}: expected exactly keys id, text, when, then")
    if not isinstance(rule["id"], str) or not RULE_ID.fullmatch(rule["id"]):
        fail(f"{where}: invalid id {rule['id']!r}")
    if not isinstance(rule["text"], str) or not rule["text"].strip() or len(rule["text"]) > MAX_TEXT:
        fail(f"{where}: text must be a non-empty sentence")
    when, then = rule["when"], rule["then"]
    if not isinstance(when, dict) or len(when) > MAX_CONDITIONS:
        fail(f"{where}: when must be an object with at most {MAX_CONDITIONS} conditions")
    if not isinstance(then, dict) or not 1 <= len(then) <= MAX_EFFECTS:
        fail(f"{where}: then must claim 1..{MAX_EFFECTS} next-observation fields")
    for ref, cond in when.items():
        check_ref(ref, where)
        if isinstance(cond, list):
            if not cond or len(cond) > MAX_LIST or not all(scalar_ok(v) for v in cond):
                fail(f"{where}: membership list must hold 1..{MAX_LIST} scalars")
        elif isinstance(cond, dict):
            if len(cond) != 1 or next(iter(cond)) not in COMPARATORS:
                fail(f"{where}: comparator must be one of {COMPARATORS}")
            op, operand = next(iter(cond.items()))
            if not scalar_ok(operand) or (op != "ne" and not is_num(operand)):
                fail(f"{where}: invalid comparator operand")
        elif not scalar_ok(cond):
            fail(f"{where}: invalid condition literal")
    for field, effect in then.items():
        check_field(field, where)
        if not isinstance(effect, dict) or len(effect) != 1:
            fail(f"{where}: effect for {field} must be one of set/copy/expr")
        kind, arg = next(iter(effect.items()))
        if kind == "set" and scalar_ok(arg):
            continue
        if kind == "copy":
            check_ref(arg, where)
            continue
        if kind == "expr":
            _validate_expr(arg, 1, [0], f"{where}.then.{field}", verbs)
            continue
        fail(f"{where}: invalid effect for {field}")
    return rule


def lookup(ref, obs, action):
    scope, field = ref.split(".", 1)
    return (obs if scope == "obs" else action).get(field, MISSING)


def condition_holds(cond, value):
    if value is MISSING:
        return False
    if isinstance(cond, list):
        return any(same(value, option) for option in cond)
    if isinstance(cond, dict):
        op, operand = next(iter(cond.items()))
        if op == "ne":
            return not same(value, operand)
        if not (is_num(value) and is_num(operand)):
            return False
        return {"lt": value < operand, "le": value <= operand, "gt": value > operand, "ge": value >= operand}[op]
    return same(value, cond)


class _Undefined(Exception):
    pass


def evaluate(expr, obs, action, rt=None):
    if "const" in expr:
        return expr["const"]
    if "ref" in expr:
        value = lookup(expr["ref"], obs, action)
        if value is MISSING:
            raise _Undefined()
        return value
    op = expr["op"]
    if op == "if":
        return evaluate(expr["args"][1] if _truth(evaluate(expr["args"][0], obs, action, rt)) else expr["args"][2], obs, action, rt)
    if op == "and":
        return all(_truth(evaluate(a, obs, action, rt)) for a in expr["args"])
    if op == "or":
        return any(_truth(evaluate(a, obs, action, rt)) for a in expr["args"])
    args = [evaluate(a, obs, action, rt) for a in expr["args"]]
    if op == "call":
        if rt is None:
            raise _Undefined()
        try:
            value = rt.call(expr["verb"], args)
        except VerbError:
            raise _Undefined() from None
        if not scalar_ok(value):
            raise _Undefined()
        return value
    if op == "not":
        return not _truth(args[0])
    if op in ("eq", "ne"):
        return same(*args) if op == "eq" else not same(*args)
    if not all(is_num(a) for a in args):
        raise _Undefined()
    if op in ("lt", "le", "gt", "ge"):
        a, b = args
        return {"lt": a < b, "le": a <= b, "gt": a > b, "ge": a >= b}[op]
    if op == "add":
        result = args[0] + args[1]
    elif op == "sub":
        result = args[0] - args[1]
    elif op == "mul":
        result = args[0] * args[1]
    elif op == "div":
        if args[1] == 0:
            raise _Undefined()
        result = args[0] / args[1]
    elif op == "min":
        result = min(args)
    elif op == "max":
        result = max(args)
    elif op == "abs":
        result = abs(args[0])
    else:  # round(x) or round(x, digits)
        digits = int(args[1]) if len(args) > 1 else 0
        result = float(round(args[0], digits)) if digits else round(args[0])
    if not is_num(result):
        raise _Undefined()
    return result


def _truth(value):
    if type(value) is not bool:
        raise _Undefined()
    return value


def predict(rule, obs, action, rt=None):
    """Return {field: value} for fields the rule claims, MISSING when `when` fails, None if undefined."""
    if not all(condition_holds(c, lookup(r, obs, action)) for r, c in rule["when"].items()):
        return MISSING
    out = {}
    for field, effect in rule["then"].items():
        kind, arg = next(iter(effect.items()))
        try:
            if kind == "set":
                out[field] = arg
            elif kind == "copy":
                value = lookup(arg, obs, action)
                if value is MISSING:
                    return None
                out[field] = value
            else:
                out[field] = evaluate(arg, obs, action, rt)
        except _Undefined:
            return None
    return out


def check_rule(rule, history, rt=None):
    """Replay all of H.  Returns (support, counterexamples, abstentions)."""
    support, counter, abstain = 0, [], 0
    for index, t in enumerate(history):
        predicted = predict(rule, t["obs"], t["action"], rt)
        if predicted is MISSING:
            continue
        if predicted is None or any(field not in t["next_obs"] for field in predicted):
            abstain += 1
            continue
        wrong = {f: {"predicted": v, "actual": t["next_obs"][f]} for f, v in predicted.items() if not same(v, t["next_obs"][f])}
        if wrong:
            counter.append({"index": index, "mismatch": wrong, "context": _context(rule, t)})
        else:
            support += 1
    return support, counter, abstain


def _context(rule, t):
    refs = list(rule["when"])
    for effect in rule["then"].values():
        refs.extend(_refs(effect))
    ctx = {}
    for ref in refs:
        value = lookup(ref, t["obs"], t["action"])
        ctx[ref] = None if value is MISSING else value
    # Unreferenced fields are what a repair needs to find the missing condition.
    for field in sorted(t["obs"]):
        if len(ctx) >= 40:
            break
        ctx.setdefault(f"obs.{field}", t["obs"][field])
    for field in sorted(t["action"]):
        ctx.setdefault(f"action.{field}", t["action"][field])
    return ctx


def _refs(node):
    if isinstance(node, dict):
        if "ref" in node and isinstance(node["ref"], str):
            yield node["ref"]
        if "copy" in node and isinstance(node["copy"], str):
            yield node["copy"]
        for value in node.values():
            yield from _refs(value)
    elif isinstance(node, list):
        for value in node:
            yield from _refs(value)


def uses_verbs(node):
    if isinstance(node, dict):
        if node.get("op") == "call" and isinstance(node.get("verb"), str):
            yield node["verb"]
        for value in node.values():
            yield from uses_verbs(value)
    elif isinstance(node, list):
        for value in node:
            yield from uses_verbs(value)


def rule_shape(rule):
    """Identity of a rule's claim, ignoring id and prose."""
    return digest({"when": rule["when"], "then": rule["then"]})


def describe(rule):
    when = ", ".join(f"{r}={json.dumps(c)}" for r, c in sorted(rule["when"].items())) or "always"
    return f"when {when}"


# ------------------------------------------------------------------ store

class Ledger:
    def __init__(self, workspace):
        raw = pathlib.Path(workspace)
        if raw.is_symlink():
            fail("workspace must not be a symlink")
        self.workspace = raw.resolve(strict=True)
        self.dir = self.workspace / LEDGER_DIR
        if self.dir.is_symlink():
            fail("ledger directory must not be a symlink")

    # -- files
    def _path(self, name):
        path = self.dir / name
        if path.is_symlink():
            fail(f"{name} must not be a symlink")
        return path

    def _lines(self, name):
        path = self._path(name)
        if not path.exists():
            return []
        out = []
        with open(path, "rb") as handle:
            for number, raw in enumerate(handle, 1):
                value = json.loads(raw)
                if (canonical(value) + "\n").encode("utf-8") != raw:
                    fail(f"{name} line {number} is not canonical")
                out.append(value)
        return out

    def _append(self, name, values):
        data = "".join(canonical(v) + "\n" for v in values).encode("utf-8")
        fd = os.open(self._path(name), os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o644)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)

    def _write(self, name, text):
        fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=self.dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
            os.replace(tmp, self._path(name))
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def _lock(self):
        ledger = self

        class Lock:
            def __enter__(self):
                self.fd = os.open(ledger._path("ledger.lock"), os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o644)
                fcntl.flock(self.fd, fcntl.LOCK_EX)

            def __exit__(self, *_):
                fcntl.flock(self.fd, fcntl.LOCK_UN)
                os.close(self.fd)
        return Lock()

    # -- lifecycle
    @classmethod
    def create(cls, workspace, min_support=2):
        ledger = cls(workspace)
        if ledger.dir.exists():
            fail("ledger already exists")
        if type(min_support) is not int or not 1 <= min_support <= 1000:
            fail("min_support must be an integer 1..1000")
        ledger.dir.mkdir(mode=0o755)
        ledger._write("manifest.json", canonical({"schema": SCHEMA, "kind": "chel-ledger", "min_support": min_support}) + "\n")
        for name in ("history.jsonl", "revisions.jsonl"):
            ledger._path(name).touch()
        return ledger.load()

    def load(self):
        if not self.dir.is_dir():
            fail("ledger is not initialized")
        manifest = json.loads(self._path("manifest.json").read_text())
        if manifest.get("schema") != SCHEMA or manifest.get("kind") != "chel-ledger":
            fail("unsupported ledger manifest")
        self.min_support = manifest["min_support"]
        entries = self._lines("history.jsonl")
        head = GENESIS
        self.history = []
        for index, entry in enumerate(entries):
            if set(entry) != {"t", "prev"} or entry["prev"] != head:
                fail(f"history chain broken at {index}")
            self.history.append(validate_transition(entry["t"]))
            head = digest(entry)
        self.head = head
        self.revisions = self._lines("revisions.jsonl")
        prev = GENESIS
        self.rules = {}
        self.verbs = {}
        for index, rev in enumerate(self.revisions):
            if rev.get("prev") != prev:
                fail(f"revision chain broken at {index}")
            prev = digest(rev)
            self._apply(rev)
        self.revisions_head = prev
        return self

    def runtime(self):
        return VerbRuntime(self.verbs)

    def _apply(self, rev):
        kind = rev["kind"]
        if kind == "verb" and rev["accepted"]:
            self.verbs[rev["verb"]["name"]] = rev["verb"]
            return
        if kind in ("add", "revise") and rev["accepted"]:
            if kind == "revise":
                self.rules[rev["target"]]["state"] = "superseded"
                self.rules[rev["target"]]["superseded_by"] = rev["rule"]["id"]
            self.rules[rev["rule"]["id"]] = {"rule": rev["rule"], "state": "active", "support": rev["support"],
                                             "accepted_at": rev["history_len"]}
        elif kind == "retract" and rev["accepted"]:
            self.rules[rev["target"]]["state"] = "retracted"
            self.rules[rev["target"]]["reason"] = rev["reason"]
        elif kind == "falsified":
            entry = self.rules[rev["target"]]
            entry["state"] = "falsified"
            entry["counterexample"] = rev["counterexample"]
        elif kind == "support":
            for rule_id, support in rev["support"].items():
                self.rules[rule_id]["support"] = support

    def _log(self, rev):
        rev = {**rev, "prev": self.revisions_head}
        self._append("revisions.jsonl", [rev])
        self.revisions.append(rev)
        self.revisions_head = digest(rev)
        self._apply(rev)
        return rev

    # -- operations
    def active(self):
        return {rid: e for rid, e in self.rules.items() if e["state"] == "active"}

    def record(self, transitions):
        """Append transitions to H, then re-verify every active rule against the new entries."""
        transitions = [validate_transition(t) for t in transitions]
        with self._lock():
            self.load()
            if len(self.history) + len(transitions) > MAX_HISTORY:
                fail("history limit reached")
            entries, head = [], self.head
            for t in transitions:
                entry = {"prev": head, "t": t}
                entries.append(entry)
                head = digest(entry)
            self._append("history.jsonl", entries)
            start = len(self.history)
            self.history.extend(transitions)
            self.head = head
            falsified, support = [], {}
            with self.runtime() as rt:
                checks = {rid: check_rule(e["rule"], transitions, rt) for rid, e in sorted(self.active().items())}
            for rule_id, entry in sorted(self.active().items()):
                gained, counter, _ = checks[rule_id]
                if counter:
                    ce = {**counter[0], "index": start + counter[0]["index"]}
                    self._log({"kind": "falsified", "target": rule_id, "counterexample": ce, "history_len": len(self.history)})
                    falsified.append(rule_id)
                elif gained:
                    support[rule_id] = entry["support"] + gained
            if support:
                self._log({"kind": "support", "support": support, "history_len": len(self.history)})
            self._views()
        return {"recorded": len(transitions), "history_len": len(self.history), "falsified": falsified}

    def propose(self, proposal):
        """Submit {"op": "add", "rule": R, "rationale"?} | {"op": "revise", "target": id, "rule": R}
        | {"op": "retract", "target": id, "reason": str}.  Every proposal is logged."""
        if not isinstance(proposal, dict) or proposal.get("op") not in ("add", "revise", "retract", "define_verb"):
            fail("proposal op must be add, revise, retract or define_verb")
        if proposal["op"] == "define_verb":
            return self.define_verb(proposal.get("verb"), str(proposal.get("rationale", ""))[:MAX_TEXT])
        with self._lock():
            self.load()
            op = proposal["op"]
            rationale = str(proposal.get("rationale", ""))[:MAX_TEXT]
            if op == "retract":
                target, reason = proposal.get("target"), proposal.get("reason")
                ok = target in self.active() and isinstance(reason, str) and reason.strip()
                rev = self._log({"kind": "retract", "target": target, "reason": str(reason)[:MAX_TEXT],
                                 "accepted": bool(ok), "history_len": len(self.history)})
                self._views()
                return {"accepted": bool(ok), "reasons": [] if ok else ["target must be an active rule and reason non-empty"]}
            rule = proposal.get("rule")
            reasons, support, counter, abstain = [], 0, [], 0
            try:
                validate_rule(rule, verbs=self.verbs)
            except LedgerError as exc:
                reasons.append(str(exc))
            target = proposal.get("target") if op == "revise" else None
            if not reasons:
                if op == "revise" and target not in self.active():
                    reasons.append("revise target must be an active rule")
                if rule["id"] in self.rules:
                    reasons.append(f"rule id {rule['id']} is already used; pick a new id")
                if len(self.rules) >= MAX_RULES:
                    reasons.append("rule limit reached")
                shape = rule_shape(rule)
                if any(rule_shape(e["rule"]) == shape for rid, e in self.active().items() if rid != target):
                    reasons.append("an active rule already makes exactly this claim")
            if not reasons:
                with self.runtime() as rt:
                    support, counter, abstain = check_rule(rule, self.history, rt)
                if counter:
                    reasons.append(f"contradicted by {len(counter)} recorded transition(s)")
                if support < self.min_support:
                    reasons.append(f"supported by {support} transition(s); at least {self.min_support} required")
            accepted = not reasons
            record = {"kind": op, "rule": rule if isinstance(rule, dict) else None, "target": target,
                      "accepted": accepted, "support": support, "counterexamples": len(counter),
                      "abstain": abstain, "reasons": reasons, "rationale": rationale, "history_len": len(self.history)}
            if record["rule"] is not None:
                try:
                    canonical(record["rule"])
                except (TypeError, ValueError):
                    record["rule"] = None
            self._log(record)
            self._views()
            return {"accepted": accepted, "support": support, "abstain": abstain, "reasons": reasons,
                    "counterexamples": counter[:MAX_REPORTED]}

    def define_verb(self, verb, rationale=""):
        """Accept a verb only if it passes the static policy, reproduces its examples,
        and (when redefining) leaves every active rule that uses it unbroken."""
        with self._lock():
            self.load()
            reasons, record_verb = [], None
            try:
                if not isinstance(verb, dict) or set(verb) - {"name", "params", "doc", "source", "examples"}:
                    raise VerbError("verb must have name, params, doc, source, examples")
                name, params, source = verb.get("name"), verb.get("params"), verb.get("source")
                doc, examples = verb.get("doc", ""), verb.get("examples")
                if not isinstance(doc, str) or not doc.strip() or len(doc) > MAX_TEXT:
                    raise VerbError("doc must be a short description of what the verb computes")
                check_source(name, params, source)
                if not isinstance(examples, list) or not 2 <= len(examples) <= MAX_EXAMPLES:
                    raise VerbError(f"give 2..{MAX_EXAMPLES} worked examples [{{args: [...], result: v}}]")
                for ex in examples:
                    if (not isinstance(ex, dict) or set(ex) != {"args", "result"} or not isinstance(ex["args"], list)
                            or len(ex["args"]) != len(params) or not all(scalar_ok(a) for a in ex["args"])
                            or not scalar_ok(ex["result"])):
                        raise VerbError("each example is {args: [scalars, one per param], result: scalar}")
                record_verb = {"name": name, "params": params, "doc": doc.strip(), "source": source, "examples": examples}
                candidate = {**self.verbs, name: record_verb}
                with VerbRuntime(candidate) as rt:
                    for ex in examples:
                        try:
                            got = rt.call(name, ex["args"])
                        except VerbError as exc:
                            reasons.append(f"example {ex['args']} raised {exc}")
                            continue
                        if not same(got, ex["result"]):
                            reasons.append(f"example {ex['args']}: expected {ex['result']!r}, got {got!r}")
                    if not reasons and name in self.verbs:
                        for rid, entry in sorted(self.active().items()):
                            if name in set(uses_verbs(entry["rule"]["then"])):
                                _, counter, _ = check_rule(entry["rule"], self.history, rt)
                                if counter:
                                    reasons.append(f"redefinition would break active rule {rid}")
            except VerbError as exc:
                reasons.append(str(exc))
            accepted = not reasons
            stored = record_verb or (verb if isinstance(verb, dict) and len(canonical(verb)) < 20_000 else None)
            self._log({"kind": "verb", "verb": stored, "accepted": accepted, "reasons": reasons[:MAX_REPORTED],
                       "rationale": rationale, "history_len": len(self.history)})
            self._views()
            return {"accepted": accepted, "reasons": reasons[:MAX_REPORTED]}

    # -- views
    def _views(self):
        self._write("rules.json", json.dumps(self.rules_view(), indent=1, ensure_ascii=False, sort_keys=True) + "\n")
        lines = ["# Chel ledger", "", f"H: {len(self.history)} transitions. Minimum support: {self.min_support}.", ""]
        for rid, e in sorted(self.rules.items()):
            lines.append(f"- **{rid}** [{e['state']}; support {e['support']}] {e['rule']['text']}")
        if self.verbs:
            lines += ["", "## Verbs", ""] + [f"- `{n}({', '.join(v['params'])})`: {v['doc']}" for n, v in sorted(self.verbs.items())]
            vdir = self.dir / "verbs"
            vdir.mkdir(exist_ok=True)
            for n, v in self.verbs.items():
                fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=vdir)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(f"# {v['doc']}\n# Examples: {json.dumps(v['examples'], ensure_ascii=False)[:1500]}\n{v['source'].rstrip()}\n")
                os.replace(tmp, vdir / f"{n}.py")
        self._write("rules.md", "\n".join(lines) + "\n")

    def rules_view(self):
        return {rid: {"state": e["state"], "support": e["support"], "text": e["rule"]["text"],
                      "when": e["rule"]["when"], "then": e["rule"]["then"],
                      **({"counterexample": e["counterexample"]} if "counterexample" in e else {})}
                for rid, e in self.rules.items()}

    def status(self):
        states = {}
        for e in self.rules.values():
            states[e["state"]] = states.get(e["state"], 0) + 1
        proposals = [r for r in self.revisions if r["kind"] in ("add", "revise")]
        return {"history_len": len(self.history), "min_support": self.min_support, "rules": states,
                "verbs": sorted(self.verbs),
                "proposals": len(proposals), "accepted": sum(r["accepted"] for r in proposals)}

    def recall(self, query, top_k=3):
        """Rank active rules by lexical overlap of the query with text, tool names and fields."""
        terms = set(_terms(query))
        if not terms:
            return []
        active = self.active()
        docs = {rid: _terms(_rule_doc(e["rule"])) for rid, e in active.items()}
        if not docs:
            return []
        df = {}
        for d in docs.values():
            for term in set(d):
                df[term] = df.get(term, 0) + 1
        avg = sum(len(d) for d in docs.values()) / len(docs)
        scored = []
        for rid, d in docs.items():
            score = 0.0
            for term in terms:
                tf = d.count(term)
                if tf:
                    idf = math.log(1 + (len(docs) - df[term] + 0.5) / (df[term] + 0.5))
                    score += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * len(d) / avg))
            if score > 0:
                scored.append((score * (1 + 0.05 * math.log1p(active[rid]["support"])), rid))
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [{"id": rid, "score": round(s, 4), **self.rules_view()[rid]} for s, rid in scored[:top_k]]


_STOP = frozenset("a an and are as at be by for from has if in into is it its of on or that the then this to was when with".split())


def _terms(text):
    return [t for t in re.findall(r"[a-z0-9]+", text.casefold().replace("_", " ")) if t not in _STOP and len(t) > 1]


def _rule_doc(rule):
    parts = [rule["text"]]
    for ref, cond in rule["when"].items():
        parts.append(ref.split(".", 1)[1])
        if isinstance(cond, str):
            parts.append(cond)
    parts.extend(rule["then"])
    parts.extend(uses_verbs(rule["then"]))
    return " ".join(parts)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workspace", required=True)
    sub = ap.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--min-support", type=int, default=2)
    sub.add_parser("record")
    sub.add_parser("propose")
    sub.add_parser("status")
    sub.add_parser("rules")
    recall = sub.add_parser("recall")
    recall.add_argument("query")
    recall.add_argument("--top-k", type=int, default=3)
    args = ap.parse_args(argv)
    try:
        if args.command == "init":
            print(json.dumps(Ledger.create(args.workspace, args.min_support).status()))
            return 0
        ledger = Ledger(args.workspace).load()
        if args.command == "record":
            transitions = [json.loads(line) for line in sys.stdin if line.strip()]
            print(json.dumps(ledger.record(transitions)))
        elif args.command == "propose":
            result = ledger.propose(json.loads(sys.stdin.read()))
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result["accepted"] else 1
        elif args.command == "status":
            print(json.dumps(ledger.status()))
        elif args.command == "rules":
            print(json.dumps(ledger.rules_view(), indent=1, ensure_ascii=False, sort_keys=True))
        else:
            print(json.dumps(ledger.recall(args.query, args.top_k), indent=1, ensure_ascii=False))
        return 0
    except (LedgerError, OSError, json.JSONDecodeError) as exc:
        print(f"CHEL_LEDGER_ERROR {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
