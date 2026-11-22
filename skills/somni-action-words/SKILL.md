---
name: somni-action-words
description: Somni action words are learned Python functions that call this session's tools as rpc.TOOL(...) and appear as ordinary tools. Use when a tool says "Learned action word", when writing or repairing one in a Somni dream (define_action, revise_action, retire_action), or when reading or editing files in Somni's actions folder.
---

# Somni action words

An action word is a short Python function that Somni learned from your own transcript. It runs a sequence of tool calls that recurred, so you can make the whole sequence with one tool call instead of one turn per call. Each word appears in your tool list under its own name, with a description ending in "Learned action word: calls ...". The memory index at the start of a turn lists the words most relevant to your task as `- word `name(params)`: ...` lines.

## Using a word

Call it like any tool. It returns JSON: whatever the function returns. If the word's final call returned images, such as a screenshot, up to two of them come back with the result. Images from earlier calls do not, because they may show the state before the word's later calls.

- Each call it makes goes through Pi like your own calls do: the same argument checks, the same permission hooks, and the same recording.
- If one of its calls fails, the word stops and the error names the calls that already ran. Those calls' effects stay in place. Check the state before you retry.
- A word encodes what worked before. When your situation differs from its description, make the calls yourself.
- A word whose index line shows many unconfirmed runs often runs without effect. Check the result before you rely on it.
- The return value is whatever the word's author chose to report. If it reports only the word's own steps, such as how many calls it made, it does not show that anything changed. Check the effect with a tool before you say the work is done.

## Reading a word

Words live as files in `<somni workspace>/somni-v1/actions/<name>.py`:

```python
# somni: action word. Edit freely; the next Pi session re-checks this file and records the change.
# somni: delete the file to retire the word. Lines starting with `# somni:` are regenerated.
# somni: calls cancel_booking, get_booking; evidence t000001, t000002, t000003
# somni: uses words none; used by cancel_all
# somni: self-tests test_refundable_is_cancelled, test_other_is_kept ran and passed when this version was recorded

from rpc import replay

def cancel_if_refundable(booking_id: str):
    """Cancel a booking only when it is refundable, and report what happened."""
    booking = rpc.get_booking(booking_id=booking_id)
    if not booking["refundable"]:
        return {"cancelled": False, "reason": "not refundable"}
    result = rpc.cancel_booking({"booking_id": booking_id, "reason": "customer request"})
    return {"cancelled": result["status"] == "cancelled"}

def test_refundable_is_cancelled():
    with replay("t000001", "t000002"):
        assert cancel_if_refundable(booking_id="BK-1")["cancelled"]

def test_other_is_kept():
    with replay("t000003"):
        assert cancel_if_refundable(booking_id="BK-2")["reason"] == "not refundable"
```

One file is one skill: the word, then its self-tests.

- The docstring becomes the tool description. The parameter annotations, which must be `str`, `int`, `float`, `bool`, `list`, or `dict`, become its schema.
- `rpc.TOOL(...)` and `rpc.pi.TOOL(...)` call a Pi tool, which may be another word. `rpc.mcp.SERVER.TOOL(...)` calls the MCP tool `mcp__SERVER__TOOL`; prefer this form for MCP tools (`rpc.mcp__SERVER__TOOL(...)` also works). Pass keyword arguments or one dict. The call returns the tool's result as parsed JSON, or as text if it is not JSON.
- `dir(rpc.mcp.SERVER)` lists a server's tools, `dir(rpc)` lists the namespaces and tools, and `help(rpc.mcp.SERVER.TOOL)` returns a signature such as `rpc.mcp.body.key(code: str, hold_ms?: int): ...`. They read the tools Somni recorded from task sessions.
- **Self-tests.** After `from rpc import replay` at the top, up to eight `def test_*():` functions call the word inside `with replay("t000012", "t000013"):` and `assert` on its results. Inside the block, the word's tool calls must match those transcript records exactly and in order, and they receive the recorded results. Self-tests run whenever the word is defined or edited. They never call live tools.
- The `# somni: uses words ...; used by ...` line shows which words call which.
- Allowed: assignments, `if`, `for` over lists or `range`, comprehensions, arithmetic, indexing, and string and dict methods such as `.get`, `.items`, `.lower`, and `.split`. The allowed builtins are `abs min max round int float str len bool sorted sum any all range divmod`. `date`, `datetime`, `timedelta`, `floor`, and `ceil` are also available.
- Rejected: imports other than `from rpc import replay`, `while`, `try`, `lambda`, classes, names starting with `_`, and any I/O except `rpc`.

## Editing a word (humans)

Edit the file and save it. The next Pi session that records to this store checks the edit and records it in the store's hash-chained history as a pass authored by `human`.

- To add a word, create a new `.py` file containing one word `def`, plus any self-tests. To retire one, delete its file.
- If a self-test you wrote fails, the edit is rejected, Pi warns with the failing assertion, and the previous version stays active.
- The edit must pass the syntax rules above. If it does not, Pi warns at session start, and the previous version stays active until you fix the file.
- A stored test that your edit breaks is dropped and listed in the pass. It does not block the edit.
- Keep the file name and the function name the same.

## Writing a word in a dream

Only during `/somni-dream`, when `SOMNI_ACTIONS=1`. The dream prompt lists the callable tools as signatures, plus each current word with its source, tests, dependencies and recent runs (ok or failed, with the result or error). That is the "fails here, works there" evidence to read before writing or repairing a word. Tools marked "(task sessions)" or "seen in the transcript" are absent from the dream itself, but words run in task sessions, so a word may call them.

1. Find the same sequence of tool calls in two or more places in the transcript. The dream prompt's "Recurring sequences" list does this for you: each candidate has refs you can use directly as tests.
2. Write one function that makes that sequence. Turn the values that vary between occurrences into parameters, and branch with `if` where the occurrences differ.
3. Try it. `somni_action_try {"source": ..., "tests": [...]}` runs the gate without saving and returns what each test's call returned, or the first error. `somni_action_try {"inspect": "rpc.mcp.body"}` lists tools with signatures.
4. Add tests. Each test gives `params` and `refs`: the transcript IDs of the successful tool calls the word must reproduce, in order, with exactly the recorded arguments. After the last ref, the word must return. An optional `result` checks what it returns. Together the tests must exercise every tool the word calls. A branch that stops early needs its own test with fewer refs.

```json
{"op": "define_action",
 "source": "def cancel_if_refundable(booking_id: str):\n    \"\"\"Cancel a booking only when it is refundable.\"\"\"\n    ...",
 "tests": [{"params": {"booking_id": "BK-1"}, "refs": ["t000001", "t000002"], "result": {"cancelled": true}},
           {"params": {"booking_id": "BK-2"}, "refs": ["t000003"]}]}
```

A good word does two things beyond replaying calls:

- **It checks preconditions it can read cheaply.** If a status or lookup tool returns structured state, branch on it before acting. For example, `if not rpc.status()["ready"]: rpc.prepare()`.
- **It ends with the observation, if one is visual.** Only the final call's images reach the agent, so make the looking call last.
- **It returns evidence of its effect.** Return state read back after the change, such as the new status, a count the tool reports, or the changed record. Do not return a count of the word's own steps. An agent will take the return value as the outcome.
- **It says whether the effect was confirmed.** A word that changes something returns a top-level `"confirmed": true` only when a read-back proves the effect, and `false` when it does not. The memory index counts them, as in `last 5 runs: 5 ok (3 confirmed, 2 unconfirmed), 0 failed`, and the dream sees each run marked confirmed or UNCONFIRMED.
- **It doesn't depend on fast timing.** A word is a script of round trips, not a real-time loop: each call is a full trip to the tool and back, and can take anywhere from milliseconds to seconds. Steps that must happen within a fraction of a second of each other, such as jumping and then clicking while still in the air, go into one tool call that does its own timing, if the environment has one. Each run is timed in the dream workbench, as in `timing: key +0ms 640ms, mouse_click +655ms 912ms`: when each call began after the word started, and how long it took. If a run failed or did nothing, check the gaps before changing the plan.

```python
def restock(item: str, amount: int):
    """Order stock for one item and report the level the inventory tool shows afterwards."""
    before = rpc.inventory(item=item)["level"]
    rpc.place_order(item=item, amount=amount)
    after = rpc.inventory(item=item)["level"]
    return {"before": before, "after": after, "confirmed": after - before == amount}
```

To repair a word, use `{"op":"revise_action","source":...,"tests":...}`. Omit `tests` to keep the stored ones. To remove a word, use `{"op":"retire_action","name":...,"reason":...,"runs":["t000123"]}`, citing the failing runs it is retired for. Runs the workbench marks as older source, older Somni or unstamped failed on code that has changed since. They are not grounds to retire: a retirement citing only them is refused, and so is one that cites nothing while every failure is stale. A word with no failures can be retired as redundant without citing runs. A rejected operation comes back with a reason. Fix it and resubmit while attempts remain.

Dependencies: a word that other words use cannot be retired until they are revised or retired. Revising a word's parameters is allowed, but the pass reports the words whose calls no longer match (`broken_dependents`); revise them in the same pass. A word whose call to another word uses the wrong keyword arguments is rejected.

Common rejections:

- **Arguments differ from the record.** Key order does not matter, but values and types do: `300` and `"300"` differ.
- **The record is truncated or failed.** Cite only complete, successful results.
- **A tool is never exercised.** Add a test that reaches that call.
- **Name clash.** A word cannot share a name with an existing tool, and cannot call itself, directly or through another word.

When the transcript shows a word ran without the effect it promised, revise it or retire it. Its calls are recorded under its own tool call, so you can see what it actually did.
