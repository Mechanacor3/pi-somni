"""Action words: learned Python functions that call the session's own tools.

A word is one plain, human-editable Python function.  Its docstring is the
description and its annotations are the parameter schema:

    def rebook_cheapest(booking_id: str, max_price: float):
        '''Move a booking to the cheapest flight on the same route under max_price.'''
        booking = rpc.get_booking(booking_id=booking_id)
        offers = rpc.search_flights(origin=booking["origin"], destination=booking["destination"])
        ...

Words follow Chel's verb policy (syntax, builtins, sandbox) plus one
capability, ``rpc``:

* ``rpc.TOOL(...)`` or ``rpc.pi.TOOL(...)`` calls the Pi tool named TOOL
  (built-in, extension or learned word, so words compose);
* ``rpc.mcp.SERVER.TOOL(...)`` calls the MCP tool ``mcp__SERVER__TOOL``.

A call takes keyword arguments or one dict and returns the tool's result
(parsed JSON, else text).

The word never performs I/O itself.  ``step()`` replays it from the start
with the results gathered so far and returns either the next tool call or the
final value.  Pi dispatches each call through ``ctx.executeTool()``, so every
call passes the same validation and ``tool_call`` guards as the agent's own.
Words are pure apart from ``rpc``, so replay is deterministic, and ``step()``
checks that it is.

The dream gate (``build``) mirrors Chel's: each test replays recorded,
successful tool calls from the transcript, with the same arguments in the
same order, and must then return.  Every tool a word uses must be exercised
by some test.  Human edits (``lenient=True``) must pass the syntax policy;
tests they break are dropped and reported instead of blocking the edit.  The
policy is defence in depth, not a proof of safety; run untrusted agents in a
container regardless.
"""
from __future__ import annotations

import ast
import json
import re
import subprocess
import sys

from .chel import verbs as chel

NAME = chel.VERB_NAME
SEGMENT = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
RECORD = re.compile(r"TOOL ([\w.-]+)\(")
TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "array": list, "object": dict}
ANNOTATIONS = {"str": "string", "int": "integer", "float": "number", "bool": "boolean", "list": "array", "dict": "object"}
HEADER = "# somni:"
MAX_WORDS = 32
MAX_USES = 8
MAX_CALLS = 16  # default per run; SOMNI_ACTION_MAX_CALLS raises it up to CALL_LIMIT
CALL_LIMIT = 64
MAX_TESTS = 8
MAX_DESCRIPTION = 300
VALUE_BYTES = 16_384
RESULT_BYTES = 65_536
HISTORY_BYTES = 262_144
RESERVED_PREFIX = "somni_"


class ActionError(ValueError):
    pass


def require(ok, message):
    if not ok:
        raise ActionError(message)


def size(value):
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode())


def tool_name(path):
    """`rpc.a` / `rpc.pi.a` -> a; `rpc.mcp.server.tool` -> mcp__server__tool."""
    if len(path) == 1:
        return path[0]
    if len(path) == 2 and path[0] == "pi":
        return path[1]
    if len(path) == 3 and path[0] == "mcp":
        return f"mcp__{path[1]}__{path[2]}"
    raise ActionError("call tools as rpc.TOOL(...), rpc.pi.TOOL(...) or rpc.mcp.SERVER.TOOL(...)")


def _rpc_path(node):
    """The attribute path of `rpc.x.y` (outermost node given), or None."""
    path = []
    while isinstance(node, ast.Attribute):
        path.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name) and node.id == "rpc" and path:
        return path[::-1]
    return None


def parse(source):
    """Check a word's source.  Returns its name, description, params, uses and sandbox source."""
    require(isinstance(source, str) and 0 < len(source) <= chel.MAX_SOURCE,
            f"source must be text of at most {chel.MAX_SOURCE} characters")
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ActionError(f"syntax error: {exc.msg} (line {exc.lineno})") from None
    require(len(tree.body) == 1 and isinstance(tree.body[0], ast.FunctionDef),
            "source must be exactly one top-level def NAME(param: type, ...)")
    fn = tree.body[0]
    name = fn.name
    require(NAME.fullmatch(name) and not name.startswith(RESERVED_PREFIX),
            "action name must match [a-z][a-z0-9_]{0,39} and not start with somni_")
    description = ast.get_docstring(fn)
    require(description and len(description.strip()) <= MAX_DESCRIPTION,
            f"start the body with a docstring of 1-{MAX_DESCRIPTION} characters describing the word")
    params = {}
    for arg in fn.args.args:
        kind = arg.annotation.id if isinstance(arg.annotation, ast.Name) else None
        require(kind in ANNOTATIONS, f"annotate parameter {arg.arg} with one of {', '.join(ANNOTATIONS)}")
        params[arg.arg] = ANNOTATIONS[kind]
        arg.annotation = None
    fn.returns = None
    uses, allowed = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and (path := _rpc_path(node.func)) is not None:
            require(all(SEGMENT.fullmatch(part) for part in path), "tool path parts must be plain identifiers")
            uses.add(tool_name(path))
            require(not (node.args and node.keywords) and len(node.args) <= 1
                    and all(k.arg is not None for k in node.keywords),
                    "call a tool with keyword arguments or a single dict")
            sub = node.func
            while isinstance(sub, ast.Attribute):
                allowed.add(id(sub))
                sub = sub.value
            allowed.add(id(sub))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Name) and node.id == "rpc"
                or isinstance(node, ast.Attribute) and _rpc_path(node) is not None):
            require(id(node) in allowed, "rpc may only be used to call a tool: rpc.TOOL(...)")
    require(1 <= len(uses) <= MAX_USES, f"an action word calls 1-{MAX_USES} distinct tools")
    require(name not in uses and not any(u.startswith(RESERVED_PREFIX) for u in uses),
            "an action word cannot call itself or Somni's tools")
    runtime = ast.unparse(tree)
    # Chel's verb policy, with each rpc call target masked as an allowed builtin.
    try:
        chel.check_source(name, list(params), ast.unparse(_Mask(allowed).visit(tree)))
    except chel.VerbError as exc:
        raise ActionError(str(exc)) from None
    return {"name": name, "description": " ".join(description.split()), "params": params,
            "uses": sorted(uses), "runtime": runtime}


class _Mask(ast.NodeTransformer):
    """Replace each rpc call target with a call to `len`, an allowed builtin."""

    def __init__(self, allowed):
        self.allowed = allowed

    def visit_Call(self, node):
        self.generic_visit(node)
        if id(node.func) in self.allowed:
            node.func = ast.Name(id="len", ctx=ast.Load())
        return node


_RUNNER = r"""
import json, math, signal, sys
from datetime import date, datetime, timedelta
_cpu = float(sys.argv[1])
class _Timeout(Exception):
    pass
class _Next(Exception):
    pass
def _alarm(*_):
    raise _Timeout()
signal.signal(signal.SIGPROF, _alarm)
_b = __builtins__ if isinstance(__builtins__, dict) else __builtins__.__dict__
_safe = {k: _b[k] for k in %(builtins)r}
_env = {"date": date, "datetime": datetime, "timedelta": timedelta, "floor": math.floor, "ceil": math.ceil}
_words = {}
def _plain(value):
    return json.loads(json.dumps(value, allow_nan=False))
class _Run:
    def __init__(self, results):
        self.results = results
        self.calls = []
    def call(self, path, args, kwargs):
        if len(path) == 1:
            tool = path[0]
        elif len(path) == 2 and path[0] == "pi":
            tool = path[1]
        elif len(path) == 3 and path[0] == "mcp":
            tool = "mcp__" + path[1] + "__" + path[2]
        else:
            raise AttributeError("call tools as rpc.TOOL(...), rpc.pi.TOOL(...) or rpc.mcp.SERVER.TOOL(...)")
        if (args and kwargs) or len(args) > 1:
            raise TypeError("call a tool with keyword arguments or a single dict")
        payload = args[0] if args else kwargs
        if not isinstance(payload, dict):
            raise TypeError("tool arguments must be an object")
        self.calls.append([tool, _plain(payload)])
        if len(self.calls) > len(self.results):
            raise _Next()
        return _plain(self.results[len(self.calls) - 1])
class _Rpc:
    __slots__ = ("_r", "_p")
    def __init__(self, run, path):
        self._r = run
        self._p = path
    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return _Rpc(self._r, self._p + (name,))
    def __call__(self, *args, **kwargs):
        return self._r.call(self._p, args, kwargs)
for _line in sys.stdin:
    _req = json.loads(_line)
    try:
        if "define" in _req:
            _g = {"__builtins__": _safe, **_env}
            exec(compile(_req["define"]["source"], "<action>", "exec"), _g)
            _words[_req["define"]["name"]] = (_g, _g[_req["define"]["name"]])
            _out = {"ok": True}
        else:
            _g, _fn = _words[_req["run"]]
            _run = _Run(_req["results"])
            _g["rpc"] = _Rpc(_run, ())
            _done, _value = True, None
            signal.setitimer(signal.ITIMER_PROF, _cpu)
            try:
                _value = _fn(**_req["args"])  # by name: stored params are key-sorted, not in declared order
            except _Next:
                _done = False
            finally:
                signal.setitimer(signal.ITIMER_PROF, 0)
                _g["rpc"] = None
            _out = {"ok": True, "done": _done, "calls": _run.calls, "value": _plain(_value) if _done else None}
    except _Timeout:
        _out = {"ok": False, "error": "action word exceeded its CPU time limit"}
    except Exception as _exc:
        _out = {"ok": False, "error": (type(_exc).__name__ + ": " + str(_exc))[:300]}
    sys.stdout.write(json.dumps(_out, allow_nan=False) + "\n")
    sys.stdout.flush()
""" % {"builtins": chel.BUILTINS}


class ActionRuntime:
    """One sandbox process (Chel's limits) for a set of words.  Use as a context manager."""

    def __init__(self, words):
        self.words = {name: {"source": w.get("runtime") or parse(w["source"])["runtime"]} for name, w in words.items()}
        self.proc = None

    def _start(self):
        self.proc = subprocess.Popen([sys.executable, "-I", "-S", "-c", _RUNNER, str(chel.CALL_CPU_SECONDS)],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     env={}, cwd="/", text=True, preexec_fn=chel._limits)
        for name, word in self.words.items():
            reply = self._send({"define": {"name": name, "source": word["source"]}})
            require(reply.get("ok"), f"action word {name} failed to load: {reply.get('error')}")

    def _send(self, request):
        try:
            self.proc.stdin.write(json.dumps(request) + "\n")
            self.proc.stdin.flush()
            line = self.proc.stdout.readline()
        except (BrokenPipeError, OSError):
            line = ""
        if not line:
            self.close()
            raise ActionError("action sandbox stopped (resource limit reached)")
        return json.loads(line)

    def run(self, name, args, results):
        require(name in self.words, f"unknown action word {name}")
        if self.proc is None:
            self._start()
        reply = self._send({"run": name, "args": args, "results": results})
        require(reply.get("ok"), reply.get("error", "action word failed"))
        return reply

    def close(self):
        if self.proc is not None:
            proc, self.proc = self.proc, None
            try:
                proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            proc.stdout.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


_shared = {"key": None, "runtime": None}


def shared_runtime(words):
    """A runtime reused across steps in a long-lived backend while the word set is unchanged."""
    key = tuple(sorted((name, w["source"]) for name, w in words.items()))
    if _shared["key"] != key:
        if _shared["runtime"] is not None:
            _shared["runtime"].close()
        _shared.update(key=key, runtime=ActionRuntime(words))
    return _shared["runtime"]


def check_params(word, params):
    require(isinstance(params, dict) and set(params) == set(word["params"]),
            f"{word['name']} takes parameters {', '.join(word['params']) or '(none)'}")
    for key, kind in word["params"].items():
        value = params[key]
        require(isinstance(value, TYPES[kind]) and (kind == "boolean" or type(value) is not bool),
                f"parameter {key} must be {kind}")
    require(size(params) <= VALUE_BYTES, "action parameters exceed 16 KiB")


def step(runtime, word, params, history, *, max_calls=MAX_CALLS):
    """Replay `word` over `history` ([{tool, args, result}]).  Returns {call: {tool, args}} or {result}."""
    require(type(max_calls) is int and 1 <= max_calls <= CALL_LIMIT, f"max_calls must be 1-{CALL_LIMIT}")
    check_params(word, params)
    require(isinstance(history, list) and len(history) <= max_calls, f"an action run makes at most {max_calls} calls")
    require(size(history) <= HISTORY_BYTES, "action history exceeds 256 KiB")
    for item in history:
        require(isinstance(item, dict) and {"tool", "args", "result"} <= set(item), "malformed action history")
        require(size(item["result"]) <= RESULT_BYTES, "a tool result in the action history exceeds 64 KiB")
    reply = runtime.run(word["name"], {key: params[key] for key in word["params"]}, [item["result"] for item in history])
    calls = reply["calls"]
    for index, item in enumerate(history):
        require(index < len(calls) and calls[index] == [item["tool"], item["args"]],
                f"action word did not replay call {index + 1} deterministically")
    if reply["done"]:
        require(len(calls) == len(history), "action word returned before replaying its history")
        require(size(reply["value"]) <= VALUE_BYTES, "action result exceeds 16 KiB")
        return {"result": reply["value"]}
    require(len(calls) == len(history) + 1, "action word made an unexpected call")
    require(len(history) < max_calls, f"action word exceeded {max_calls} tool calls (SOMNI_ACTION_MAX_CALLS)")
    tool, args = calls[-1]
    require(tool in word["uses"], f"action word called undeclared tool {tool}")
    require(size(args) <= VALUE_BYTES, "tool arguments exceed 16 KiB")
    return {"call": {"tool": tool, "args": args}}


def parse_record(record, ref="record"):
    """A transcript tool_result as (tool, args, result).  Raises if it cannot serve as evidence."""
    require(record is not None and record.get("source") == "tool_result", f"{ref} is not a tool_result record")
    require(record.get("tool_ok") is not False, f"{record['id']} records a failed tool call")
    text = record["text"]
    match = RECORD.match(text)
    require(match, f"{record['id']} is not a recorded tool call")
    try:
        args, end = json.JSONDecoder().raw_decode(text, match.end())
    except ValueError:
        raise ActionError(f"{record['id']} has truncated arguments") from None
    require(isinstance(args, dict) and text.startswith(") -> ", end), f"{record['id']} is not a recorded tool call")
    body = text[end + 5:]
    try:
        result = json.loads(body)
    except ValueError:
        require("[body " not in body and not body.endswith("[truncated]"), f"{record['id']} has a truncated result")
        result = body
    return match[1], args, result


def _reaches(name, uses, words, seen=()):
    for use in uses:
        if use == name or (use in words and use not in seen and _reaches(name, words[use]["uses"], words, (*seen, use))):
            return True
    return False


def build(source, tests, records, words, *, allow=None, reserved=(), replacing=None, lenient=False):
    """Validate a word against recorded history.  Returns (word, dropped_tests).

    `words` is the current lexicon; `replacing` names the word a revision replaces.
    With `lenient` (a human edit), a failing test is dropped and reported instead of
    rejecting the word, and the word needs no evidence.
    """
    info = parse(source)
    name = info["name"]
    others = {k: v for k, v in words.items() if k != replacing}
    if replacing is None:
        require(name not in words, f"{name} is already an action word; use revise_action")
        require(len(words) < MAX_WORDS, f"the store already has {MAX_WORDS} action words")
    else:
        require(name == replacing, f"a revision of {replacing} must keep its name")
    require(name not in reserved, f"{name} is already a session tool")
    require(not _reaches(name, info["uses"], others), f"{name} would call itself through another word")
    if allow is not None:
        outside = sorted(set(info["uses"]) - set(allow) - set(others))
        require(not outside, f"{', '.join(outside)} not in SOMNI_ACTION_TOOLS")
    word = {"name": name, "description": info["description"], "params": info["params"], "source": source,
            "uses": info["uses"]}
    require(isinstance(tests, list) and len(tests) <= MAX_TESTS
            and (lenient or tests), f"tests must hold {0 if lenient else 1}-{MAX_TESTS} cases")
    exercised, cited, kept, dropped = set(), [], [], []
    with ActionRuntime({name: {"runtime": info["runtime"]}}) as runtime:
        for number, case in enumerate(tests, 1):
            try:
                refs, used = _replay(runtime, word, case, records, number)
            except ActionError as exc:
                if not lenient:
                    raise
                dropped.append({"test": case, "reason": str(exc)[:300]})
                continue
            kept.append(case)
            exercised |= used
            cited.extend(refs)
    if not lenient:
        require(cited, "at least one test must replay recorded tool calls")
        missing = sorted(set(info["uses"]) - exercised)
        require(not missing, f"tests never exercise {', '.join(missing)}")
    word.update(tests=kept, evidence=sorted(set(cited)))
    return word, dropped


def _replay(runtime, word, case, records, number):
    require(isinstance(case, dict) and {"params", "refs"} <= set(case) <= {"params", "refs", "result"},
            f"test {number} needs params, refs and optionally result")
    refs = case["refs"]
    require(isinstance(refs, list) and len(refs) <= CALL_LIMIT and all(isinstance(r, str) for r in refs)
            and refs == sorted(set(refs)), f"test {number} refs must be distinct transcript IDs in order")
    history, used = [], set()
    for ref in refs:
        tool, args, result = parse_record(records.get(ref), ref)
        out = step(runtime, word, case["params"], history, max_calls=CALL_LIMIT)
        require(out.get("call") == {"tool": tool, "args": args},
                f"test {number}: at {ref} the word {_describe(out)}, but the record shows "
                f"{tool}({json.dumps(args)[:200]})")
        history.append({"tool": tool, "args": args, "result": result})
        used.add(tool)
    out = step(runtime, word, case["params"], history, max_calls=CALL_LIMIT)
    require("result" in out, f"test {number}: after {len(refs)} recorded call(s) the word {_describe(out)}")
    if "result" in case:
        require(out["result"] == case["result"],
                f"test {number}: returned {json.dumps(out['result'])[:200]}, expected {json.dumps(case['result'])[:200]}")
    return refs, used


def _describe(out):
    if "call" in out:
        return f"calls {out['call']['tool']}({json.dumps(out['call']['args'])[:200]})"
    return f"returns {json.dumps(out['result'])[:200]}"


MINE_SCAN = 4000      # most recent tool results scanned for recurring sequences
MINE_LENGTHS = range(2, 7)
MINE_LIMIT = 5
MINE_REFS = 3


def candidates(records, words=(), *, limit=MINE_LIMIT):
    """Recurring runs of successful tool calls: the raw material for action words.

    A run is the tool calls between two user prompts in one run label, cut at any call
    that failed, was truncated, or was a Somni tool or an existing word.  Returns the
    strongest shapes (tool sequences) of length 2-6 that occur at least twice without
    overlapping, each with the refs of a few occurrences (ready-made gate tests) and the
    argument fields that vary between occurrences (the word's parameters).
    """
    runs, run, label = [], [], object()
    for record in [r for r in records if r.get("source") in {"tool_result", "ordinary_observation"}][-MINE_SCAN:]:
        if record.get("source") != "tool_result" or record.get("run_label") != label:
            runs.append(run)
            run, label = [], record.get("run_label")
            if record.get("source") != "tool_result":
                continue
        try:
            tool, args, _ = parse_record(record, record.get("id"))
        except ActionError:
            runs.append(run)
            run = []
            continue
        if tool.startswith(RESERVED_PREFIX) or tool in words:
            runs.append(run)
            run = []
            continue
        run.append((record["id"], tool, args))
    runs.append(run)

    groups = {}
    for run in runs:
        for n in MINE_LENGTHS:
            free = {}
            for i in range(len(run) - n + 1):
                window = run[i:i + n]
                shape = tuple(tool for _, tool, _ in window)
                if i >= free.get(shape, 0):
                    groups.setdefault(shape, []).append(window)
                    free[shape] = i + n
    found = []
    for shape, windows in groups.items():
        if len(windows) < 2 or len(set(shape)) == 1 and len(shape) > 2:
            continue
        keyed = {}
        for window in windows:
            keyed.setdefault(json.dumps([args for _, _, args in window], sort_keys=True), []).append(window)
        same = max(keyed.values(), key=len)
        varying = sorted({f"{position + 1}.{key}" for position in range(len(shape))
                          for key in set().union(*(w[position][2] for w in windows))
                          if len({json.dumps(w[position][2].get(key), sort_keys=True) for w in windows}) > 1})
        shown = same if len(same) >= 2 else windows
        found.append({"tools": list(shape), "count": len(windows), "same_args": len(same),
                      "args": [args for _, _, args in shown[0]], "varying": varying[:12],
                      "refs": [[ref for ref, _, _ in w] for w in shown[:MINE_REFS]]})
    # Longer and more exactly repeated sequences save more turns and make easier first words.
    found.sort(key=lambda c: ((c["count"] + c["same_args"]) * (len(c["tools"]) - 1), len(c["tools"])), reverse=True)
    kept = []
    for cand in found:
        tools = cand["tools"]
        # Skip a shape already covered by a kept, longer shape that occurs about as often.
        if any(len(k["tools"]) > len(tools) and 4 * k["count"] >= 3 * cand["count"]
               and any(k["tools"][i:i + len(tools)] == tools for i in range(len(k["tools"]) - len(tools) + 1))
               for k in kept):
            continue
        kept.append(cand)
        if len(kept) == limit:
            break
    return kept


def check_retire(operation, words):
    require(isinstance(operation, dict) and set(operation) == {"op", "name", "reason"},
            "retire_action needs exactly name and reason")
    require(operation["name"] in words, "retire_action targets an unknown action word")
    require(isinstance(operation["reason"], str) and operation["reason"].strip(), "retire_action needs a reason")
    users = sorted(w["name"] for w in words.values() if operation["name"] in w["uses"])
    require(not users, f"retire {', '.join(users)} first; they call {operation['name']}")
    return {"name": operation["name"], "reason": operation["reason"].strip()}


def fold(events, words=None):
    """Apply stored define/retire events in order."""
    words = dict(words or {})
    for event in events:
        if event.get("op") == "retire":
            words.pop(event["name"], None)
        else:
            words[event["name"]] = {k: v for k, v in event.items() if k != "op"}
    return words


def render(word):
    """The human-readable file for a word: generated `# somni:` header lines, then its source."""
    lines = [f"{HEADER} action word. Edit freely; the next Pi session re-checks this file and records the change.",
             f"{HEADER} delete the file to retire the word. Lines starting with `{HEADER}` are regenerated.",
             f"{HEADER} calls {', '.join(word['uses'])}; evidence {', '.join(word['evidence']) or 'none (human-written)'}"]
    for case in word["tests"]:
        args = ", ".join(f"{key}={value!r}" for key, value in case["params"].items())
        result = f" -> {case['result']!r}" if "result" in case else ""
        lines.append(f"{HEADER} test {word['name']}({args}) replays {' '.join(case['refs']) or 'no calls'}{result}")
    return "\n".join(lines) + "\n\n" + word["source"] + "\n"


def source_of(text):
    """A word's source from its file: everything after the generated header."""
    lines = text.splitlines()
    while lines and (lines[0].startswith(HEADER) or not lines[0].strip()):
        lines.pop(0)
    return normalize("\n".join(lines))


def normalize(source):
    require(isinstance(source, str), "source must be text")
    return "\n".join(line.rstrip() for line in source.strip("\n").splitlines())
