# pi-somni

**Somni** gives the [Pi coding agent](https://pi.dev) a memory that gets better the more you work with it.
It remembers what you told it, what worked and what went wrong, and your project's conventions.
With action words on, it can also turn chores you repeat into one-step tools. Every memory cites the
session it came from, and you can read all of it as markdown.

**New here? Start with [Using Somni day to day](docs/everyday-use.md)**: setup, the daily loop,
correcting a memory, and privacy. The rest of this README is the reference.

## How it works

Somni keeps three memory layers and consolidates them offline ("dreaming"):

1. **Transcript**: a canonical, hash-chained, append-only record of what the agent saw
   (prompts, bounded tool results, and screenshots) and said.
2. **Claims**: short notes the agent writes. Every claim cites transcript records. A
   `verified` claim cites an exact excerpt or a stored image from each record. The store checks
   the quote character for character and checks image bytes against their SHA-256 digest.
3. **Wiki**: pages that gather what is known about one subject. Pages are markdown that cite
   claims inline (`[n000012]`) and link other pages with `[[slug]]` wikilinks. Every link must
   resolve, and every cited claim must be active.
4. **Dreams**: offline passes that correct, add, retract or promote claims and write or merge
   pages, each bound to the transcript head it reviewed. History is never deleted: corrections
   and retractions keep their lineage.

After every dream Somni renders the wiki as a **vault of markdown notes** in
`somni-v1/vault/`. Each page is one note with YAML frontmatter, *See also* and *Backlinks*
sections, and an *Evidence* list. `index.md` is the map of content, showing folders as a tree, and `claims.md` shows
every claim with its transcript quotes and image links. A page slug may be a folder path such as `fares/refunds`
(at most three levels), rendered as `fares/refunds.md`. Dreams are asked to group pages into a folder once a topic has several.
Links render as relative markdown links (`[Refunds](fares/refunds.md)`, `[n000004](../claims.md#n000004)`), so the
folder works as a GitLab or GitHub wiki and in Obsidian. `SOMNI_LINK_STYLE=wikilink` renders Obsidian `[[wikilinks]]`
instead. Dreams write `[[slug]]` links either way. The vault is a view; edits there are not read back.

Before each Pi turn, Somni injects a small memory index ranked against the current user prompt.
It prioritizes exact run labels and tags, then uses lexical similarity (BM25), salience, and
the recency of the cited evidence. Pages get up to half the available space, with an excerpt
from the best matching page. A long page can contribute its procedure section. Claim lines
include evidence IDs and use a short gist when one is available. The index skips entries that
exceed the remaining space and is capped at 8 KiB.

The design follows Anthropic's engineering essay
[*The components of agent memory*](https://www.anthropic.com/research/components-of-agent-memory)
(see [Background and credits](#background-and-credits)). Somni is a small, independent
interpretation of it, not Anthropic's implementation.

**Does it help?** The real test is your own work: fewer repeat questions and fewer repeated mistakes.
To measure it, we use the public STATE-Bench benchmark of multi-step service tasks as a stand-in for
"better at day-to-day work". With the Python adapter, the wiki + search condition scored an estimated
+4.7 percentage points in pooled task completion (95% interval −0.4 to +9.8). See
[evaluation methods, results, and tests](EVALUATION.md) for the full comparison and replay.

## Install

Requires Pi 1.0+ and `python3` (3.10 or newer) on `PATH`. Optional image downscaling uses Pillow.

```bash
pi install git:github.com/Mechanacor3/pi-somni
# or for one session:
pi -e git:github.com/Mechanacor3/pi-somni
```

## Use

### Record, dream, and recall

For everyday habits, see [Using Somni day to day](docs/everyday-use.md). A quick check that it works:

1. Start Pi in a project directory. For a first run, give it a harmless example such as “Our
   sandbox smoke test uses port 8042. Remember this using evidence from this session.”
2. Run `/somni-dream smoke test convention`, then `/somni-wiki project conventions`.
   These commands make model calls. Review the resulting notes in `.pi/somni/somni-v1/vault/`.
3. Start a fresh Pi session in the same directory and ask it to use `somni_recall` to find the
   smoke test port. The index and matching claim or page come from the same on-disk store.

In a session Somni records automatically, and adds these tools:

| Tool | Purpose |
|---|---|
| `somni_recall` | Wiki pages, then claims, most relevant to a query |
| `somni_search` | Exact phrase search over the transcript (to find evidence IDs) |
| `somni_image` | Load image evidence by transcript ID and SHA-256 |
| `somni_note` | Add a cited claim while working |
| `somni_dream` | Submit a consolidation pass during `/somni-dream` or `/somni-wiki` |

Two skills come with the package. `somni-memory` tells the agent how to use the index, when to
recall, and what is worth a note. `somni-action-words` covers action words. Pi lists a skill only
when the `read` tool is active.

Run `/somni-dream [focus]` to consolidate recent transcript into claims, and
`/somni-wiki [goal]` to gather claims that are not yet on a page into linked wiki pages.
Wiki links can point forward to pages created in the same pass and can form cycles.
Every link must resolve when the pass ends, including after any page drops.
Set `SOMNI_PAGE_GUIDE` to replace the default wiki-writing guidance for your domain.
`/somni-dream` works as follows. Somni closes the
recording session, asks the model for one pass, and reopens recording when the pass settles.
Invalid operations (for example, a quote that does not match) are rejected one by one and
listed in the pass record.

Dreams can supply an optional one-line `gist` of up to 160 UTF-8 bytes when they add, correct,
or merge a claim. `somni_note` also accepts a gist. `somni_recall` returns the full claim text.
All readers of a store that contains gists need a Somni version that supports them.
Dream guidance reserves salience 5 for up to two broadly useful claims per pass and uses 3
for an ordinary reusable lesson.

With a vision-capable model, Somni attaches up to four recent images within a 1 MiB total
budget. The dream can open older images with `somni_image` and cite one using
`{"ref":"t000042","image_sha256":"<digest>"}`. The dream can cite images after receiving them
as attachments or through the tool. Use `uncertain` for interpretations the evidence does not settle.

During a dream, a `merge` operation can synthesize active claims into one new claim.
Somni sorts their IDs, inherits their evidence, and hashes the parent fingerprints together
with the combined text and status. Parent order and salience changes leave the digest
stable. The claim log and rendered vault show the parent IDs and the provenance digest.

### Chel rule learning in dreams

Set `SOMNI_CHEL_LEARNING=1` for the task and dream sessions to learn executable predictions. Somni uses the rule ledger and pure-Python verb runner from [pi-chel at ba2dee4](https://github.com/Mechanacor3/pi-chel/tree/ba2dee4186e2b4c019d8fd5d7eb25f8d39f414e9/chel). The package includes both modules under the MIT license.

A **transition** comes from a successful tool call whose complete arguments and JSON object result fit in the transcript. Its `obs` contains the arguments as `arg_*` fields, `action.kind` names the tool, and `next_obs` contains the observed result fields. Older records without a recorded tool status stay available as ordinary evidence.

During `/somni-dream`, a `ledger_propose` operation contains a Chel `proposal`. An `add` or `revise` also includes two transcript references in `tests`. For example, a dream could submit this operation when the cited evidence exists.

```json
{
  "op": "ledger_propose",
  "proposal": {
    "op": "add",
    "rule": {
      "id": "confirmed_request",
      "text": "Confirmed requests commit the change.",
      "when": {"action.kind": "apply", "obs.arg_confirm": true},
      "then": {"committed": {"set": true}}
    }
  },
  "tests": {"yes": "t000004", "no": "t000010"}
}
```

The yes case must match the rule and support every prediction. The no case must cite a different transition of the same action kind. It must fall outside the rule's conditions on observed inputs, or demonstrate a correctly predicted Boolean changing from true to false. Chel then replays the rule over all eligible transitions and requires at least two supporting transitions and zero counterexamples. A later contradiction falsifies the rule and removes it from recall and the injected index.

The ledger also accepts Chel's `retract` and `define_verb` proposals. A **verb** is a pure function used by a rule. Chel checks its syntax and runs its worked examples in an isolated Python process with CPU, address-space, file-size, and process limits. Somni requires distinct worked inputs and both outcomes for Boolean verbs. Chel tests redefinitions against active rules that use the verb. Running a verb gives it no Pi tool capabilities.

Accepted proposals and their test references are committed in the Somni dream chain. Use `python3 somni/store.py --workspace W --subject-id S ledger` to replay the ledger and inspect rule states, support counts, and verb names. The CLI accepts `--learning` on `dream` and `inject` to apply proposals and preview matching rules. Replay can run recorded verbs in Chel's worker. Offline dream and wiki turns allow 16 Somni evidence reads, 16 viewed images and three submission attempts by default (`SOMNI_DREAM_MAX_READS`, `SOMNI_DREAM_MAX_IMAGES`, `SOMNI_DREAM_MAX_SUBMISSIONS`), with one committed pass. Every Somni tool result in a dream ends with what is left, for example `[dream budget left: images 9/16, reads 11/16, submissions 3/3]`. When a budget runs out, the message says what to do next. Arguments are forgiving: a `somni_search` or `somni_recall` `limit` outside its range is clamped and the result says so. An `image_sha256` may be a unique prefix of at least six hex characters, and a digest that doesn't belong to the cited record gets back the digests that record does have. A submission whose operations are all rejected commits nothing, lists each operation's reason, and says how many attempts are left. A submission with some valid operations commits those and lists the rest. With `SOMNI_LENIENT_IMAGE_EVIDENCE=1`, a citation whose image is wrong or wasn't viewed keeps its `{ref}`. A verified claim left without a quote or image becomes uncertain, and the pass records each change in `image_downgrades`. A dream or wiki turn that commits nothing is logged in `somni-v1/dream-failures.jsonl` with its cause (`provider_error`, `no_submission`, `all_rejected`, `invalid_submission`, `budget_exhausted`, `timeout` for a daydream stopped by its time limit, or `other`), its kind (`dream`, `wiki` or `daydream`), the run label and the reads, submissions and images it used. `status --json` reports `dream_failures` as counts by cause and kind, plus the latest entry. The log sits beside the hash-chained logs and is bounded at 1 MiB: past that, the oldest half is dropped and the counts keep the total. A dream's evidence reads (Somni tool results other than `somni_dream`) are also kept in `somni-v1/dream-cache.json` until a pass commits. The cache is keyed by the kind of turn, the transcript head and the pass count. If an attempt is cut off, say by a provider error, a retry on the same records within `SOMNI_DREAM_CACHE_MINUTES` (default 60, measured from the latest read; `0` disables) gets those results in its prompt under "Evidence from an interrupted attempt". The images it viewed are attached again, so the retry needn't spend turns or read budget repeating them. A daydream's cache is keyed to its snapshot.

### Action words (experimental)

Set `SOMNI_ACTIONS=1` to let dreams turn a recurring sequence of tool calls into an **action word**: one Python function that the agent then calls as a single tool. A word extends Chel's verbs, using the same syntax policy, builtins, and sandboxed worker, with one capability: `rpc`.

```python
def cancel_if_refundable(booking_id: str):
    """Cancel a booking only when it is refundable, and report what happened."""
    booking = rpc.get_booking(booking_id=booking_id)
    if not booking["refundable"]:
        return {"cancelled": False, "reason": "not refundable"}
    result = rpc.cancel_booking({"booking_id": booking_id, "reason": "customer request"})
    return {"cancelled": result["status"] == "cancelled"}
```

- **Calls.** `rpc.TOOL(...)` and `rpc.pi.TOOL(...)` call the Pi tool named `TOOL`, which may be a built-in tool, an extension tool, or another action word. `rpc.mcp.SERVER.TOOL(...)` calls `mcp__SERVER__TOOL`. A call takes keyword arguments or one dict and returns the tool's result as parsed JSON, or as text if the result is not JSON.
- **Shape.** A word is one `def`, optionally followed by `test_*` self-tests in the same file. Its docstring is the tool description. Each parameter is annotated with `str`, `int`, `float`, `bool`, `list`, or `dict`. Imports (other than `from rpc import replay`), `while`, `try`, lambdas, classes, and I/O are rejected, as they are for Chel verbs.
- **Execution.** Somni registers each word as a Pi tool and runs it one call at a time. Pi lists a `--skill` skill only when the `read` tool is active. A session with words but without `read` gets the skill's "Using a word" section in its injected index once, with the first index that lists a word. The backend replays the word from the start with the results so far and returns either its next call or its value. The extension dispatches each call through `ctx.executeTool()`, so it passes the same argument validation and `tool_call` guards as a call from the agent. A failed call stops the word, and its error names the calls that already ran. Each turn's injected memory index also lists up to five registered words, ranked against the prompt like claims, one line each with its signature, description, its recent run record (for example `last 5 runs: 5 ok (3 confirmed, 2 unconfirmed), 0 failed`, where confirmed and unconfirmed count runs that returned a top-level boolean `confirmed`, the convention for "a read-back proved the effect"), the tools it calls, and the transcript records its tests replay. The run record helps an agent keep using a word that works after an unrelated failure. The section opens with an instruction rather than a heading, for example: `You have 18 learned action words; most reliable: turn_direct (10/10 confirmed), ... When one fits the next step, call it instead of making its tool calls one by one.` In one benchmark, agents called words only when told to, even with the words listed. Agents tend to act on the index rather than scan their tool list. Up to two images from the word's final call, such as a screenshot, are attached to its result. Images from earlier calls are not attached, because they may predate later calls. The result's text stays plain JSON, so a calling word can still parse it. Words nest at most four deep and make at most 16 calls per run by default (`SOMNI_ACTION_MAX_CALLS`, up to 64). The calls a word makes are recorded in the transcript like any other tool result. The word's own result record also stores the timing of each nested call, as `calls: [{tool, ok, start_ms, ms}]`, where `start_ms` is measured from the word's start. This is recorded for failed runs too. The dream workbench shows it under each recent run, as in `timing: key +0ms 640ms, mouse_click +655ms 912ms`, so a dream can tell when a sequence is too slow for its environment. The timings are kept off the result text that a calling word parses. Each call a word makes is recorded with `via_word` naming the word. `status --json` reports `word_use` for stores with words: `word_calls` (with `word_failed`, `confirmed` and `unconfirmed`), `calls_made_by_words`, `direct_calls_to_wrapped_tools` (calls the agent made itself to tools some word wraps) and `word_share`, the share of word calls among word calls plus those direct calls. Each is given in total and per run label. For records made before `via_word` existed, the word's timing list marks its calls. Records older than that are counted as direct calls. A word's own result record is stamped `word: {source, somni}`: a hash of the word's source and the Somni version that ran it. The dream workbench marks runs of an older source, an older Somni or with no stamp as stale. A dream's `retire_action` may cite the failing runs it rests on (`"runs": ["t000123"]`). A retirement citing only stale runs is rejected. So is an uncited retirement of a word whose failures are all stale, since those failures may already be fixed. An uncited retirement resting on current failures records them. A word with no failures can be retired as redundant, and a human deleting the file needs no evidence.
- **The dream gate.** A dream submits `{"op":"define_action","source":"...","tests":[{"params":{...},"refs":["t000004","t000005"],"result":...}]}`. Each test must reproduce the cited successful tool calls exactly, with the same tools and arguments in the same order, and then return. Together the tests must exercise every tool the word calls. `revise_action` (`source`, optional `tests`) repairs a word against the same gate, and `retire_action` (`name`, `reason`) removes one. A store holds up to 32 words.
- **Self-tests in the file.** After `from rpc import replay` (the one allowed import), up to eight `def test_*():` functions can call the word inside `with replay("t000012", "t000013"):` and `assert` on its results. Under replay, the word's calls must match those records and receive their recorded results. The gate runs self-tests alongside `define_action` tests, under the same sandbox and limits, and they count towards the coverage requirement.
- **Introspection.** In a word or a self-test, `dir(rpc.mcp.SERVER)` lists a server's tools, and `help(rpc.mcp.SERVER.TOOL)` returns a signature built from the recorded schema, for example `rpc.mcp.body.key(code: str, action?: "press"|"down"|"up", hold_ms?: int)`. The dream prompt lists tools in the same form.
- **Dream workbench.** The dream prompt shows each word's source, tests, dependencies, and up to three recent runs from the transcript, ok or failed, with the result or error. `somni_action_try` (dream-only) dry-runs the gate on a draft and returns each test's returned value or the first error, or inspects an rpc path. Nothing is saved.
- **Dependencies.** Word files, the index line, and the dream listing show which words use which. Retiring a word that others use is refused. A call to another word with keyword arguments that don't match its parameters is rejected. Revising a word's parameters reports the dependents it breaks as `broken_dependents` in the pass.
- **Files a human can edit.** Somni writes each word to `somni-v1/actions/<name>.py`, with generated `# somni:` lines listing its evidence and tests. Edit a file to revise the word, add a file to define one, or delete it to retire one. Files can also be placed in `somni-v1/actions/` before the store's first session. The next recording session records these edits as a pass authored by `human`. Human edits must pass the syntax policy. A stored test that an edit breaks is dropped and listed in the pass instead of blocking the edit. The hash-chained dream log holds every version, and `status --json` lists the current words.

An agent tends to take a word's return value as its outcome, so a word should return state it read back after acting (a status, a count the tool reports, the changed record), not a count of its own steps. The dream prompt and the `somni-action-words` skill both say so. The gate checks that a word reproduces its calls; it cannot check that those calls had the intended effect. Timing-sensitive sequences, where the gap between calls matters, belong in a single tool, because each call in a word is a separate round trip.

Without `SOMNI_ACTION_TOOLS`, any tool Pi enables in a session can be a word target, including a shell tool such as `bash` or `powershell` if one is on. For a sandboxed deployment, list the exact tool names words may call. Names are matched exactly, with no wildcards:

```bash
export SOMNI_ACTION_TOOLS=mcp__body__look,mcp__body__status,mcp__body__mouse_click,mcp__body__key
```

`SOMNI_ACTION_TOOLS` restricts which tools words may call; the dream prompt shows only those tools and the gate rejects any others. Words can never call Somni's own tools. Each recording session stores the declarations of the tools it can call in `somni-v1/action-tools.json`, a cache rather than evidence. The dream prompt lists those tools, any other tool named in the transcript, and the current words. It also lists up to five **recurring sequences** that Somni mines from the transcript: runs of 2–6 successful calls between user prompts that occur at least twice. Each comes with its count, the argument fields that vary (likely parameters), and the refs of a few occurrences, which serve as ready-made gate tests. The dream is asked to define a word for each candidate seen three or more times, or to say why not. Models rarely discover words unprompted: in one pilot, four dreams wrote none, although one sequence recurred six times with identical arguments. Dreams often run without the task's tools, for example with no MCP server attached, or in a one-shot `-p` dream before MCP tools register, so this listing is what lets them write words for those tools. The gate checks words against recorded calls, not live tools. Task-session tool names are reserved, so a word cannot shadow them. Dream sessions cannot call words, because offline dreams block every tool except Somni's evidence tools.

### Headless runs

Use Pi's normal model and login configuration. Point each task and its dream at the same store.

```bash
export SOMNI_WORKSPACE=/path/to/memory
SOMNI_RUN_LABEL='test 001' SOMNI_TAGS='["world A", "harvest"]' \
  pi --mode json -p 'Complete the task.'
pi -p '/somni-dream what should carry over to the next world?'
python3 somni/store.py --workspace "$SOMNI_WORKSPACE" --subject-id "${SOMNI_SUBJECT:-pi}" status --json
```

The one-shot dream waits for the pass and prints a line such as
`Somni dream d000001: 1 added, 0 rejected, 0 merged.` It exits nonzero with a diagnostic if
there is no committed pass. Use `--mode json -p` for the same dream with Pi's JSON events.
In RPC mode, send `/somni-dream` as a `prompt` and wait for `agent_settled` before reading
the store status. The status JSON reports the transcript head and count, active claims by
status, and the last pass ID and counts. `added_count` includes corrections that create a
replacement claim. For older passes that didn't record added and merged counts, those counts
can be `null`. Claims inherit the union of run labels and tags from the records they cite.

| Variable | Default | Meaning |
|---|---|---|
| `SOMNI_WORKSPACE` | `<cwd>/.pi/somni` | Store location |
| `SOMNI_RECORD` | on | `0` disables automatic recording (a testbed driver records instead) |
| `SOMNI_INJECT_BYTES` | `4096` | Size of the injected index (256–8192) |
| `SOMNI_SUBJECT` | `pi` | Identity bound into the store's manifest |
| `SOMNI_PYTHON` | `python3` | Interpreter for the backend |
| `SOMNI_RUN_LABEL` | unset | Optional label on each recorded event, up to 96 UTF-8 bytes |
| `SOMNI_TAGS` | `[]` | JSON array of up to eight printable tags, each up to 96 UTF-8 bytes |
| `SOMNI_PAGE_GUIDE` | domain-neutral playbook guidance | Optional replacement for the wiki-writing guide |
| `SOMNI_CHEL_LEARNING` | off | `1` enables Chel rule and verb proposals in dreams and rule retrieval in task sessions |
| `SOMNI_ACTIONS` | off | `1` enables [action words](#action-words-experimental): dreamed Python functions that call session tools, registered as Pi tools |
| `SOMNI_ACTION_TOOLS` | all non-Somni tools | Comma-separated tools that action words may call |
| `SOMNI_ACTION_MAX_CALLS` | `16` | Tool calls per action word run, clamped to 1–64 |
| `SOMNI_DAYDREAM_EVERY_TURNS` | off | Start a daydream (see *Long sessions* below) after this many model turns (each `turn_end`) |
| `SOMNI_DAYDREAM_EVERY_MINUTES` | off | Or start one on this interval |
| `SOMNI_DAYDREAM_PER_HOUR` | `4` | At most this many daydreams start per hour, clamped to 1–60 |
| `SOMNI_DAYDREAM_MODEL` | the session's model | Model for daydreams, as `provider/id` |
| `SOMNI_DAYDREAM_THINKING` | Pi's default | Thinking level for daydreams |
| `SOMNI_DAYDREAM_TIMEOUT_SECONDS` | `600` | Stop a daydream that runs longer, clamped to 10–7200 |
| `SOMNI_DAYDREAM_FOCUS` | claims and words from the newest records | Focus text passed to the daydream's `/somni-dream` |
| `SOMNI_DAYDREAM_COMMAND` | the running Pi | Command used to start the daydream's Pi |
| `SOMNI_DREAM_MAX_READS` | `16` | Somni evidence reads per dream or wiki turn, clamped to 1–64 |
| `SOMNI_DREAM_MAX_IMAGES` | `16` | Distinct images a dream may view, including those attached to its prompt, clamped to 0–64 |
| `SOMNI_DREAM_MAX_SUBMISSIONS` | `3` | `somni_dream` attempts per dream, clamped to 1–10 |
| `SOMNI_DREAM_CACHE_MINUTES` | `60` | How long an interrupted dream's evidence reads are offered to a retry on the same records; `0` disables |
| `SOMNI_LENIENT_IMAGE_EVIDENCE` | unset | `1` keeps a citation's `{ref}` when only its image is wrong, downgrading the claim if needed |
| `SOMNI_LINK_STYLE` | `markdown` | Vault link style: `markdown` (relative `[Title](path.md)` links) or `wikilink` (Obsidian `[[slug\|Title]]`) |
| `SOMNI_DREAM_RECORDS` | `120` | Number of recent transcript records sent to `/somni-dream`, clamped to 1–120 |
| `SOMNI_DREAM_RECORD_BYTES` | `1500` | UTF-8 text excerpt per dream record, including the truncation marker, clamped to 64–8192. IDs, metadata, and attachments have separate bounds |
| `SOMNI_TOOL_RESULT_BYTES` | `4096` | Tool result text budget in UTF-8 bytes, clamped to 512–8000 |
| `SOMNI_DIGEST_TOOLS` | `stream_read` | Comma-separated tool names whose text output is recorded as a byte count and SHA-256 alongside their arguments. An empty value disables this policy |
| `SOMNI_IMAGE_BYTES` | `262144` | Stored bytes per image, up to 2097152. Set to `0` to disable image capture |
| `SOMNI_IMAGE_MAX_EDGE` | `0` | Optional thumbnail edge in pixels, up to 4096. Requires Pillow in `SOMNI_PYTHON` |

Somni records finalized assistant text blocks before, between, and after tool calls. Large tool
results get a bounded preview with the original byte count and SHA-256. These policies apply to
the Somni transcript. Pi itself receives the original tool result. The store keeps up to four
PNG, JPEG, GIF, or WebP images per result in `somni-v1/images/` and deduplicates them by content.
The image budget is 64 MiB per store. Records count images omitted by the size, format, or
storage limits. If configured, Pillow converts downscaled images to JPEG before hashing,
with a 4 MiB input limit. Keep the store out of version control. Set `SOMNI_RECORD=0` before
launching Pi to pause automatic recording while retaining memory retrieval.

One Pi process owns recording in a workspace at a time. A second Pi can use existing memory while
the first owns the store. Close the second session and reopen it after the first exits to take over
recording. Pi starts one local Python child for the session and exchanges requests over private
pipes. The child holds an OS lock on `.somni-session.lock`. The file stays in the workspace.
If Pi is killed, the pipe closes and the child releases its lock. If the child is killed too,
the kernel releases it directly. The next writer verifies the chain and recovers an interrupted
transaction on startup. PID namespaces do not affect ownership on the same mounted store.
Leave the lock file in place so concurrent sessions lock the same file.
The extension can be mounted read-only with `SOMNI_WORKSPACE` on a separate writable mount.

**Long sessions.** The transcript, claim and dream logs are hash-chained across **segments**. Before an append would take a live log past 8 MiB, Somni seals it into `somni-v1/segments/<log>-NNNNNN.jsonl` and records the segment's count, head and SHA-256 in `state.json`. Sealed segments are immutable and are re-checked against their hash when they change on disk. Routine re-verification only re-parses the live tail. An interrupted seal is finished on the next load. Default limits are 65,536 transcript records and 4,096 dream passes. A store's `manifest.json` `limits` can raise `max_transcript_records` and `max_dream_passes` to 1,048,576. `status --json` reports `segments`, `transcript_live_records`, and the effective `limits`.

**Daydreams (backend, experimental).** `store.py serve --daydream` opens a backend beside the session that owns recording, without taking its lease. It pins the transcript head H it sees at start. `recent` stops at H, and the only write it accepts is one dream pass. That pass is a *live* pass, marked `"live": true` and bound to H instead of the current head, and it can cite only records up to H. The store lock serializes it with the owner's appends, and the owner picks up the new claims and words at its next turn. To have Pi schedule them, set `SOMNI_DAYDREAM_EVERY_TURNS` or `SOMNI_DAYDREAM_EVERY_MINUTES`. When one is due, the session that owns recording starts a child Pi (`pi --mode json -p "/somni-dream <focus>"`) whose Somni backend runs in `--daydream` mode, and it never waits for it. A daydream is skipped while another is running, when nothing has been recorded since the last one, or once `SOMNI_DAYDREAM_PER_HOUR` have started in the past hour. A child that runs past `SOMNI_DAYDREAM_TIMEOUT_SECONDS` is stopped. The default focus is claims and action words from the newest records, and wiki pages are left to the night dream. At the next turn, the session registers any new or revised words and prepends a one-line notice to the injected index, for example `Somni daydream d000012: +2 claim(s), 0 merged, 1 action word change(s), 0 rejected (records up to 481, $0.21, 48k tok, 52s).` The daydream's Pi totals the usage its provider reports (`input`, `output`, `cacheRead`, `cacheWrite`, `totalTokens`, `cost`, model `calls`, `duration_ms`) and submits it with the pass. It is stored in the hash-chained pass record as `usage`, covering spend up to the submission. A daydream whose Pi fails to authenticate gets a `Somni daydream skipped: auth` notice instead of the generic failure.
The child reuses a verified snapshot for up to 128
store accesses or 60 seconds. It runs the next full check on access once either limit is
reached, when it detects an outside file change, during recovery, and after a dream. New
writes still extend the hash chain and use the transaction journal. A standalone store
CLI command loads and verifies the store when it opens it. The child exits when the Pi
session closes. Somni warns on recording errors, including a full store. To archive a full
store, close all Pi sessions using it, then move the directory named by `SOMNI_WORKSPACE` (by default
`.pi/somni`) to a private location. The next Pi session creates a clean store. Stale wiki pages
remain visible in the vault for repair while recall and the injected index skip them.

The store also has a CLI: `python3 somni/store.py --workspace W --subject-id S
{init,session-open,session-close,record,note,dream,search,inject,report,metrics,status}`.
Use `inject --query 'your prompt'` to preview a prompt-ranked index. Pi uses up to the
first 64 KiB of the prompt for ranking.

## Dreaming internals

Somni's Python backend implements claim consolidation, wiki building, promotion, and
gap filling. The commands in [Use](#use) apply these operations in Pi. For trajectory
imports, build recipes, and benchmark details, see [Evaluation](EVALUATION.md).

## Safety notes

- The extension is a policy layer, not a sandbox. Run untrusted agents in a container whose only
  writable mount is the Somni workspace.
- Record only what the agent itself saw or produced. Hash chains detect accidental or
  out-of-band edits on load; they are not signatures against an agent that owns the files.
- `verified` means Somni checked the cited excerpt or image against the transcript.
- An action word can do nothing the agent could not do with the same tools. Each of its calls goes
  through Pi's guards, but an earlier call's effects stay in place when a later call fails or is blocked.
  Set `SOMNI_ACTION_TOOLS` to keep words away from command tools the agent has.

## Background and credits

Somni implements the memory hierarchy described by Lance Martin in
[*The components of agent memory*](https://www.anthropic.com/research/components-of-agent-memory)
(Anthropic Engineering, 6 November 2026), with Somni's equivalent of each layer:

| Essay | Somni |
|---|---|
| Transcripts as a searchable event log at the base | `transcript.jsonl` and `somni_search` |
| Memory notes written by the agent, which judges salience | Claims with salience 1–5 (`somni_note`) |
| Representations: pages that gather one subject and link to notes and each other (wikis) | `wiki.jsonl` pages with `[[slug]]` links and `[nXXXXXX]` citations, rendered as a markdown vault (folders, relative links; GitLab wiki and Obsidian) |
| Small injected context loaded every session (an index) | The bounded index from `inject`, with prompt-ranked pages and claims |
| Dreaming corrects memory against the transcript | `correct` / `retract` operations (`/somni-dream`, `somni/dream.py dream_pass`) |
| Dreaming builds new representations | `build_wiki` / `/somni-wiki`: claims gathered into pages, iterated until nearly all are covered |
| Dreaming promotes notes up the hierarchy | `promote` raises a claim's salience into the injected index without changing its id |
| Dreaming focuses memory on a goal | Every wiki pass is steered by an explicit `goal` |
| Dreaming fills gaps | `fill_gaps`: merge duplicate pages, repair pages whose claims went stale, add an overview page |

The essay reports that a wiki combined with search beat either alone. Somni's retrieval follows
that: pages first, then claims.

What Somni adds:
- **No dangling links.** Every link must resolve at the end of its pass. The essay lists links
  to not-yet-created pages as a failure mode.
- **Checked evidence.** Every claim must cite transcript records, and `verified` claims must quote
  them exactly or cite a stored image, which the store checks. History is hash-chained and append-only.
- **Hard size bounds** on every layer.
- **Partial dream passes** that log their rejected operations.
- **Deterministic recall.**

These are our design choices, not claims about Anthropic's system. Nothing here is Anthropic
code. Any errors are ours.

## License

MIT
