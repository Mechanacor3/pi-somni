# Action words (experimental)

Set `SOMNI_ACTIONS=1` (or `actions.enabled: true` in [somni.conf](settings.md)) to let dreams turn a recurring
sequence of tool calls into an **action word**: one Python function that the agent then calls as a single tool. A word
extends Chel's verbs, using the same syntax policy, builtins, and sandboxed worker, with one capability: `rpc`.

```python
def cancel_if_refundable(booking_id: str):
    """Cancel a booking only when it is refundable, and report what happened."""
    booking = rpc.get_booking(booking_id=booking_id)
    if not booking["refundable"]:
        return {"cancelled": False, "reason": "not refundable"}
    result = rpc.cancel_booking({"booking_id": booking_id, "reason": "customer request"})
    return {"cancelled": result["status"] == "cancelled"}
```

An agent tends to take a word's return value as its outcome, so a word should return state it read back after acting
(a status, a count the tool reports, the changed record), not a count of its own steps. The dream prompt and the
`somni-action-words` skill both say so. The gate checks that a word reproduces its calls; it cannot check that those
calls had the intended effect. Timing-sensitive sequences, where the gap between calls matters, belong in a single
tool, because each call in a word is a separate round trip.

## Writing a word

- **Calls.** `rpc.TOOL(...)` and `rpc.pi.TOOL(...)` call the Pi tool named `TOOL`, which may be a built-in tool, an
  extension tool, or another action word. `rpc.mcp.SERVER.TOOL(...)` calls `mcp__SERVER__TOOL`. A call takes keyword
  arguments or one dict and returns the tool's result as parsed JSON, or as text if the result is not JSON.
- **Shape.** A word is one `def`, optionally followed by `test_*` self-tests in the same file. Its docstring is the
  tool description. Each parameter is annotated with `str`, `int`, `float`, `bool`, `list`, or `dict`. Imports (other
  than `from rpc import replay`), `while`, `try`, lambdas, classes, and I/O are rejected, as they are for Chel verbs.
  `date`, `datetime`, `timedelta`, `floor` and `ceil` are predefined, with no import.
- **Self-tests in the file.** After `from rpc import replay` (the one allowed import), up to eight `def test_*():`
  functions can call the word inside `with replay("t000012", "t000013"):` and `assert` on its results. Under replay,
  the word's calls must match those records and receive their recorded results. The gate runs self-tests alongside
  `define_action` tests, under the same sandbox and limits, and they count towards the coverage requirement.
- **Introspection.** In a word or a self-test, `dir(rpc.mcp.SERVER)` lists a server's tools, and
  `help(rpc.mcp.SERVER.TOOL)` returns a signature built from the recorded schema, for example
  `rpc.mcp.body.key(code: str, action?: "press"|"down"|"up", hold_ms?: int)`. The dream prompt lists tools in the
  same form.

## The dream gate

A dream submits `{"op":"define_action","source":"...","tests":[{"params":{...},"refs":["t000004","t000005"],"result":...}]}`.
Each test must reproduce the cited successful tool calls exactly, with the same tools and arguments in the same order,
and then return. Together the tests must exercise every tool the word calls. `revise_action` (`source`, optional
`tests`) repairs a word against the same gate, and `retire_action` (`name`, `reason`) removes one. A store holds up to
32 words.

The **dream workbench** in the dream prompt shows each word's source, tests, dependencies, and up to three recent runs
from the transcript, ok or failed, with the result or error. `somni_action_try` (dream-only) dry-runs the gate on a
draft and returns each test's returned value or the first error, or inspects an rpc path. Nothing is saved.

**Dependencies.** Word files, the index line, and the dream listing show which words use which. Retiring a word that
others use is refused. A call to another word with keyword arguments that don't match its parameters is rejected.
Revising a word's parameters reports the dependents it breaks as `broken_dependents` in the pass.

**Retiring.** A word's own result record is stamped `word: {source, somni}`: a hash of the word's source and the
Somni version that ran it. The workbench marks runs of an older source, an older Somni or with no stamp as stale. A
dream's `retire_action` may cite the failing runs it rests on (`"runs": ["t000123"]`). A retirement citing only stale
runs is rejected. So is an uncited retirement of a word whose failures are all stale, since those failures may already
be fixed. An uncited retirement resting on current failures records them. A word with no failures can be retired as
redundant, and a human deleting the file needs no evidence.

## How dreams find candidates

Each recording session stores the declarations of the tools it can call in `somni-v1/action-tools.json`, a cache
rather than evidence. The dream prompt lists those tools, any other tool named in the transcript, and the current
words. Dreams often run without the task's tools, for example with no MCP server attached, or in a one-shot `-p` dream
before MCP tools register, so this listing is what lets them write words for those tools. The gate checks words
against recorded calls, not live tools.

The prompt also lists up to five **recurring sequences** that Somni mines from the transcript: runs of 2–6 successful
calls between user prompts that occur at least twice. Each comes with its count, the argument fields that vary (likely
parameters), and the refs of a few occurrences, which serve as ready-made gate tests. The dream is asked to define a
word for each candidate seen three or more times, or to say why not. Models rarely discover words unprompted: in one
pilot, four dreams wrote none, although one sequence recurred six times with identical arguments.

Task-session tool names are reserved, so a word cannot shadow them. Dream sessions cannot call words, because offline
dreams block every tool except Somni's evidence tools.

## Running a word

Somni registers each word as a Pi tool and runs it one call at a time. The backend replays the word from the start
with the results so far and returns either its next call or its value. The extension dispatches each call through
`ctx.executeTool()`, so it passes the same argument validation and `tool_call` guards as a call from the agent. A
failed call stops the word, and its error names the calls that already ran.

- **Limits.** Words nest at most four deep and make at most 16 calls per run by default (`SOMNI_ACTION_MAX_CALLS`, up
  to 64).
- **Images.** Up to two images from the word's final call, such as a screenshot, are attached to its result. Images
  from earlier calls are not attached, because they may predate later calls. The result's text stays plain JSON, so a
  calling word can still parse it.
- **Recording.** The calls a word makes are recorded in the transcript like any other tool result, each with
  `via_word` naming the word. The word's own result record also stores the timing of each nested call, as
  `calls: [{tool, ok, start_ms, ms}]`, where `start_ms` is measured from the word's start, for failed runs too. The
  workbench shows it under each recent run, as in `timing: key +0ms 640ms, mouse_click +655ms 912ms`, so a dream can
  tell when a sequence is too slow for its environment. The timings are kept off the result text that a calling word
  parses.

## Words in the index

Pi lists a `--skill` skill only when the `read` tool is active. A session with words but without `read` gets the
skill's "Using a word" section in its injected index once, with the first index that lists a word.

Each turn's injected index lists up to five registered words, ranked against the prompt like claims, one line each
with its signature, description, its recent run record, the tools it calls, and the transcript records its tests
replay. A run record looks like `last 5 runs: 5 ok (3 confirmed, 2 unconfirmed), 0 failed`, where confirmed and
unconfirmed count runs that returned a top-level boolean `confirmed`, the convention for "a read-back proved the
effect". The run record helps an agent keep using a word that works after an unrelated failure.

The section opens with an instruction rather than a heading, for example: `You have 18 learned action words; most
reliable: turn_direct (10/10 confirmed), ... When one fits the next step, call it instead of making its tool calls one
by one.` In one benchmark, agents called words only when told to, even with the words listed. Agents tend to act on
the index rather than scan their tool list.

## Use statistics

`status --json` reports `word_use` for stores with words: `word_calls` (with `word_failed`, `confirmed` and
`unconfirmed`), `calls_made_by_words`, `direct_calls_to_wrapped_tools` (calls the agent made itself to tools some word
wraps) and `word_share`, the share of word calls among word calls plus those direct calls. Each is given in total and
per run label. For records made before `via_word` existed, the word's timing list marks its calls. Records older than
that are counted as direct calls.

## Files a human can edit

Somni writes each word to `somni-v1/actions/<name>.py`, with generated `# somni:` lines listing its evidence and
tests. Edit a file to revise the word, add a file to define one, or delete it to retire one. Files can also be placed
in `somni-v1/actions/` before the store's first session. The next recording session records these edits as a pass
authored by `human`. Human edits must pass the syntax policy. A stored test that an edit breaks is dropped and listed
in the pass instead of blocking the edit. The hash-chained dream log holds every version, and `status --json` lists
the current words.

## Which tools words may call

Without `SOMNI_ACTION_TOOLS`, any tool Pi enables in a session can be a word target, including a shell tool such as
`bash` or `powershell` if one is on. For a sandboxed deployment, list the exact tool names words may call. Names are
matched exactly, with no wildcards:

```bash
export SOMNI_ACTION_TOOLS=mcp__body__look,mcp__body__status,mcp__body__mouse_click,mcp__body__key
```

`SOMNI_ACTION_TOOLS` restricts which tools words may call; the dream prompt shows only those tools and the gate
rejects any others. Words can never call Somni's own tools. `somni doctor` warns when words may call a shell tool.
