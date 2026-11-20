#!/usr/bin/env python3
"""Agent-authored verbs: small pure functions the rule language can call.

Chel's rule language is deliberately tiny.  When a belief needs something it
cannot express (date arithmetic, parsing a code out of a string, a lookup
table), the agent may define a *verb*: one pure Python function, written by the
model, that rules call as {"op": "call", "verb": NAME, "args": [E, ...]}.

This mirrors Memento 3's agent-written executable model, but bounded:

* Static policy (`check_source`): exactly one top-level `def NAME(...)`, a small
  whitelist of syntax, builtins (abs, min, max, round, int, float, str, len,
  bool, sorted, sum, any, all, range, divmod) and names (`date`, `datetime`,
  `timedelta`, `floor`, `ceil`), attribute access only to a short list of
  harmless names, and nothing starting with `_`.  No imports, no I/O, no
  globals, no lambdas, no classes, no try/with/while.
* Sandboxed execution (`VerbRuntime`): verbs run in a separate `python3 -I -S`
  process with an empty environment, `RLIMIT_CPU`, `RLIMIT_AS`, `RLIMIT_FSIZE=0`
  and `RLIMIT_NPROC=0` (no forking), and a per-call CPU timer.  Results must be
  JSON scalars.
* Verification happens elsewhere: a verb must reproduce its own worked
  examples, and every rule that uses it still goes through the ledger's
  replay-all-history gate.

The policy is defence in depth, not a proof of safety.  Run untrusted agents in
a container regardless.
"""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys

VERB_NAME = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
MAX_SOURCE = 4_000
MAX_PARAMS = 8
MAX_EXAMPLES = 32
CALL_CPU_SECONDS = 0.5
PROCESS_CPU_SECONDS = 60
PROCESS_MEMORY = 512 * 1024 * 1024
BUILTINS = ("abs", "min", "max", "round", "int", "float", "str", "len", "bool", "sorted", "sum", "any", "all",
            "range", "divmod")
NAMES = ("date", "datetime", "timedelta", "floor", "ceil")
ATTRIBUTES = frozenset((
    "days", "seconds", "year", "month", "day", "hour", "minute", "second", "weekday", "isoweekday",
    "fromisoformat", "date", "total_seconds", "lower", "upper", "strip", "split", "startswith", "endswith",
    "replace", "isdigit", "get", "keys", "values", "items", "count", "index", "find",
))
NODES = (
    ast.Module, ast.FunctionDef, ast.arguments, ast.arg, ast.Return, ast.If, ast.Assign, ast.AugAssign, ast.For,
    ast.Break, ast.Continue, ast.Pass, ast.Expr, ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.IfExp,
    ast.Call, ast.Name, ast.Constant, ast.Attribute, ast.Subscript, ast.Slice, ast.Tuple, ast.List, ast.Dict,
    ast.Load, ast.Store, ast.keyword, ast.ListComp, ast.GeneratorExp, ast.comprehension,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.USub, ast.UAdd, ast.Not, ast.And, ast.Or,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn, ast.Is, ast.IsNot,
)


class VerbError(ValueError):
    pass


def check_source(name, params, source):
    """Static policy check.  Returns nothing; raises VerbError with a reason."""
    if not isinstance(name, str) or not VERB_NAME.fullmatch(name):
        raise VerbError("verb name must match [a-z][a-z0-9_]{0,39}")
    if (not isinstance(params, list) or len(params) > MAX_PARAMS
            or not all(isinstance(p, str) and VERB_NAME.fullmatch(p) for p in params) or len(set(params)) != len(params)):
        raise VerbError(f"params must be up to {MAX_PARAMS} distinct lowercase identifiers")
    if not isinstance(source, str) or len(source) > MAX_SOURCE:
        raise VerbError(f"source must be text of at most {MAX_SOURCE} characters")
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise VerbError(f"syntax error: {exc.msg} (line {exc.lineno})") from None
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef) or tree.body[0].name != name:
        raise VerbError(f"source must be exactly one top-level `def {name}(...)`")
    fn = tree.body[0]
    a = fn.args
    if (fn.decorator_list or fn.returns or a.vararg or a.kwarg or a.kwonlyargs or a.posonlyargs or a.defaults
            or [x.arg for x in a.args] != params):
        raise VerbError(f"signature must be exactly def {name}({', '.join(params)}) with no defaults or annotations")
    allowed_names = set(BUILTINS) | set(NAMES) | set(params) | {"True", "False", "None"}
    local = {t.id for node in ast.walk(fn) for t in ([node.target] if isinstance(node, (ast.AugAssign, ast.For, ast.comprehension)) else getattr(node, "targets", []))
             if isinstance(t, ast.Name)}
    allowed_names |= local
    for node in ast.walk(tree):
        if not isinstance(node, NODES):
            raise VerbError(f"{type(node).__name__} is not allowed in a verb")
        if isinstance(node, ast.Name):
            if node.id.startswith("_") or node.id not in allowed_names:
                raise VerbError(f"name {node.id!r} is not available in a verb")
        if isinstance(node, ast.Attribute) and (node.attr.startswith("_") or node.attr not in ATTRIBUTES):
            raise VerbError(f"attribute .{node.attr} is not allowed in a verb")
        if isinstance(node, ast.Constant) and isinstance(node.value, (bytes, complex)):
            raise VerbError("bytes and complex constants are not allowed")
        if isinstance(node, (ast.Assign, ast.For, ast.comprehension)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                for n in ast.walk(t):
                    if isinstance(n, (ast.Attribute, ast.Subscript)):
                        raise VerbError("only plain local names can be assigned")


_RUNNER = r"""
import json, math, resource, signal, sys
from datetime import date, datetime, timedelta
_cpu = float(sys.argv[1])
class _Timeout(Exception):
    pass
def _alarm(*_):
    raise _Timeout()
signal.signal(signal.SIGPROF, _alarm)
_b = __builtins__ if isinstance(__builtins__, dict) else __builtins__.__dict__
_safe = {k: _b[k] for k in %(builtins)r}
_env = {"date": date, "datetime": datetime, "timedelta": timedelta, "floor": math.floor, "ceil": math.ceil}
_verbs = {}
def _scalar(v):
    if v is None or isinstance(v, bool) or isinstance(v, str):
        return v
    if isinstance(v, (int, float)):
        if isinstance(v, float) and v != v:
            raise ValueError("NaN result")
        return v
    raise TypeError("verb must return null, bool, number or string, got " + type(v).__name__)
for _line in sys.stdin:
    _req = json.loads(_line)
    try:
        if "define" in _req:
            _g = {"__builtins__": _safe, **_env}
            exec(compile(_req["define"]["source"], "<verb>", "exec"), _g)
            _verbs[_req["define"]["name"]] = _g[_req["define"]["name"]]
            _out = {"ok": True}
        else:
            signal.setitimer(signal.ITIMER_PROF, _cpu)
            try:
                _value = _scalar(_verbs[_req["call"]](*_req["args"]))
            finally:
                signal.setitimer(signal.ITIMER_PROF, 0)
            if isinstance(_value, str) and len(_value) > 256:
                raise ValueError("string result longer than 256 characters")
            _out = {"ok": True, "value": _value}
    except _Timeout:
        _out = {"ok": False, "error": "verb exceeded its CPU time limit"}
    except Exception as _exc:
        _out = {"ok": False, "error": (type(_exc).__name__ + ": " + str(_exc))[:300]}
    sys.stdout.write(json.dumps(_out, allow_nan=False) + "\n")
    sys.stdout.flush()
""" % {"builtins": BUILTINS}


def _limits():
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, (PROCESS_CPU_SECONDS, PROCESS_CPU_SECONDS))
    resource.setrlimit(resource.RLIMIT_AS, (PROCESS_MEMORY, PROCESS_MEMORY))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))
    os.setsid()


class VerbRuntime:
    """One sandbox process for a batch of verb calls.  Use as a context manager."""

    def __init__(self, verbs):
        self.verbs = dict(verbs)  # name -> {"params": [...], "source": "..."}
        self.proc = None
        self.cache = {}
        self.calls = 0

    def _start(self):
        self.proc = subprocess.Popen([sys.executable, "-I", "-S", "-c", _RUNNER, str(CALL_CPU_SECONDS)],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     env={}, cwd="/", text=True, preexec_fn=_limits)
        for name, verb in self.verbs.items():
            reply = self._send({"define": {"name": name, "source": verb["source"]}})
            if not reply.get("ok"):
                raise VerbError(f"verb {name} failed to load: {reply.get('error')}")

    def _send(self, request):
        try:
            self.proc.stdin.write(json.dumps(request) + "\n")
            self.proc.stdin.flush()
            line = self.proc.stdout.readline()
        except (BrokenPipeError, OSError):
            line = ""
        if not line:
            self.close()
            raise VerbError("verb sandbox stopped (resource limit reached)")
        return json.loads(line)

    def call(self, name, args):
        """Return the verb's value, or raise VerbError."""
        if name not in self.verbs:
            raise VerbError(f"unknown verb {name}")
        if len(args) != len(self.verbs[name]["params"]):
            raise VerbError(f"verb {name} takes {len(self.verbs[name]['params'])} arguments")
        key = (name, json.dumps(args, sort_keys=True))
        if key in self.cache:
            result = self.cache[key]
        else:
            if self.proc is None:
                self._start()
            self.calls += 1
            result = self._send({"call": name, "args": args})
            self.cache[key] = result
        if not result.get("ok"):
            raise VerbError(result.get("error", "verb failed"))
        return result["value"]

    def close(self):
        if self.proc is not None:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
            self.proc.stdout.close()
            self.proc = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
