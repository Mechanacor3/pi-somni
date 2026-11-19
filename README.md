# pi-somni

**Somni** is a memory plugin for the [Pi coding agent](https://pi.dev). It keeps three layers
and consolidates them offline ("dreaming"):

1. **Transcript**: a canonical, hash-chained, append-only record of what the agent saw
   (prompts, tool results) and said.
2. **Claims**: short notes the agent writes. Every claim cites transcript records. A
   `verified` claim must quote an exact excerpt of each record it cites, and the store checks
   the quote character for character. `uncertain` is the honest default for interpretations.
3. **Wiki**: pages that gather what is known about one subject. Pages are markdown that cite
   claims inline (`[n000012]`) and link other pages with `[[slug]]` wikilinks. Every link must
   resolve, and every cited claim must be active.
4. **Dreams**: offline passes that correct, add, retract or promote claims and write or merge
   pages, each bound to the transcript head it reviewed. History is never deleted: corrections
   and retractions keep their lineage.

After every dream Somni renders the wiki as an **Obsidian-style vault** in
`somni-v1/vault/`. Each page is one note with YAML frontmatter, *See also* and *Backlinks*
sections, and an *Evidence* list. `index.md` is the map of content, and `claims.md` shows
every claim with its exact transcript quotes. Open the folder in Obsidian, or read it on GitHub.
The vault is a view; edits there are not read back.

At the start of each run Somni injects a small index: page titles first, then claims by salience
(at most 8 KiB, never the whole transcript). Retrieval ranks pages and claims with a
deterministic lexical (BM25, lightly stemmed) score.

The design follows Anthropic's engineering essay
[*The components of agent memory*](https://www.anthropic.com/research/components-of-agent-memory)
(see [Background and credits](#background-and-credits)). Somni is a small, independent
interpretation of it, not Anthropic's implementation.

**Measured with the Python STATE-Bench adapter:** the wiki + search condition scored an estimated
+4.7 percentage points in pooled task completion (95% interval −0.4 to +9.8) and +9.3 in travel.
The native Pi client has a separate test suite. See [Results](#results).

## Install

Requires Pi 1.0+ and `python3` (3.10 or newer, standard library only) on `PATH`.

```bash
pi install git:github.com/Mechanacor3/pi-somni
# or for one session:
pi -e git:github.com/Mechanacor3/pi-somni
```

## Use

### Record, dream, and recall

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
| `somni_note` | Add a cited claim while working |
| `somni_dream` | Submit a consolidation pass during `/somni-dream` or `/somni-wiki` |

Run `/somni-dream [focus]` to consolidate recent transcript into claims, and
`/somni-wiki [goal]` to gather claims that are not yet on a page into linked wiki pages.
`/somni-dream` works as follows. Somni closes the
recording session, asks the model for one pass, and reopens recording when the pass settles.
Invalid operations (for example, a quote that does not match) are rejected one by one and
listed in the pass record.

| Variable | Default | Meaning |
|---|---|---|
| `SOMNI_WORKSPACE` | `<cwd>/.pi/somni` | Store location |
| `SOMNI_RECORD` | on | `0` disables automatic recording (a testbed driver records instead) |
| `SOMNI_INJECT_BYTES` | `4096` | Size of the injected index (256–8192) |
| `SOMNI_SUBJECT` | `pi` | Identity bound into the store's manifest |
| `SOMNI_PYTHON` | `python3` | Interpreter for the backend |

The store holds user prompt text, text from non-Somni tool results, assistant text, and derived
memory. The configured model can receive the bounded index, recalled content, and excerpts selected
for dreaming. Keep the store out of version control. Set `SOMNI_RECORD=0` before launching Pi
to pause automatic recording. It can still inject and retrieve existing memory.

One Pi process owns recording in a workspace at a time. A second Pi can use existing memory while
the first owns the store. Close the second session and reopen it after the first exits to take over
recording. Pi starts one local Python child for the session and exchanges requests over private
pipes. The child exits when the session closes. Somni warns on recording errors, including a full
store. To archive a full store, close
all Pi sessions using it, then move the directory named by `SOMNI_WORKSPACE` (by default
`.pi/somni`) to a private location. The next Pi session creates a clean store. Stale wiki pages
remain visible in the vault for repair while recall and the injected index skip them.

The store also has a CLI: `python3 somni/store.py --workspace W --subject-id S
{init,session-open,session-close,record,note,dream,search,inject,report,metrics}`.

## Offline dreaming over trajectories

`somni/dream.py` holds the model-authored dreaming operations. Each takes any
`complete(system, prompt) -> json_text` function; `somni/llm.py` supplies one for the OpenAI
Responses API using only the standard library. Every pass records what it accepted and why the
rest was rejected, so `dreams.jsonl` doubles as the learning trace.

| Function | Essay operation | What it does |
|---|---|---|
| `dream_pass` | correct memory | Turns a transcript window into cited claims; can correct, retract or promote |
| `build_wiki` | build new representations | Gathers claims not yet on a page into linked subject pages, until nearly all are covered |
| `fill_gaps` | fill gaps, promote | Merges duplicate pages, repairs stale ones, writes an overview page, promotes key claims |
| `refocus` + `rewrite_wiki` | focus on a goal | Reflects on recorded feedback, revises the wiki instructions, and rewrites pages under them |
| `brief` | (reading the wiki) | At retrieval time, writes a short situation-specific briefing from the index and best pages |

### STATE-Bench Agent Learning Track

`adapters/state_bench/somni_state_bench.py` has three subcommands:
- **`build`** records every [STATE-Bench](https://github.com/microsoft/STATE-Bench) train
  trajectory verbatim (user turns, assistant text, one record per tool call), then dreams over
  windows of three trajectories.
- **`build-wiki`** dreams a wiki on top of a finished claim store.
- **`build-feedback`** refocuses the wiki on feedback from the agent's own scored **train**
  episodes. Add `--balanced` to learn from successes and failures together; learning only from
  failures made the agent over-cautious.

The retrieval mixins for a `StateBenchAgent` subclass are:

| Mixin | `retrieve_learnings` returns |
|---|---|
| `SomniLearnings` | top-k claims |
| `SomniWikiLearnings` | top-k pages, then claims |
| `SomniWikiSearchLearnings` | top pages plus a digest of the most similar past episode (wiki + search) |
| `SomniLayeredLearnings` | the promoted, always-loaded index first, then content pages |
| `SomniBriefingLearnings` | a model-written briefing for the situation, then the two best pages |

```bash
OPENAI_API_KEY=... python3 -m adapters.state_bench.somni_state_bench build \
  --train-dir STATE-Bench/datasets/train_task_trajectories/travel --workspace learnings/somni/travel
python3 -m adapters.state_bench.somni_state_bench build-wiki --from learnings/somni/travel --workspace learnings/wiki/travel
```

```python
from state_bench.agents.state_bench import StateBenchAgent
from adapters.state_bench.somni_state_bench import SomniWikiSearchLearnings

class SomniAgent(SomniWikiSearchLearnings, StateBenchAgent):
    somni_root = "learnings/wiki"          # <root>/<domain>/somni-v1
```

STATE-Bench redacts "Today's date" in its shipped train trajectories. `build --task-dir
STATE-Bench/state_bench/domains/<domain>/tasks --split-file .../splits/train_test.json` restores
each **train** episode's date from its task file. It reads only ids in the train split, and only
the `now` field.

Results are in [Results](#results).

## Safety notes

- The extension is a policy layer, not a sandbox. Run untrusted agents in a container whose only
  writable mount is the Somni workspace.
- Record only what the agent itself saw or produced. Hash chains detect accidental or
  out-of-band edits on load; they are not signatures against an agent that owns the files.
- `verified` means "the cited excerpts exist", not "the interpretation is true".

## Tests

```bash
python3 -m unittest discover -s tests -t .
# Run the Pi client test using the local Pi executable and a scripted model:
PI_PLUGIN_E2E_BINARY="$(command -v pi)" python3 -m unittest tests.test_extension_e2e -v
# Or use the test image:
docker build -t pi-plugin-e2e:pi-1.0.0 tests/e2e && python3 -m unittest tests.test_extension_e2e
```

## Background and credits

Somni implements the memory hierarchy described by Lance Martin in
[*The components of agent memory*](https://www.anthropic.com/research/components-of-agent-memory)
(Anthropic Engineering, 6 November 2026), with Somni's equivalent of each layer:

| Essay | Somni |
|---|---|
| Transcripts as a searchable event log at the base | `transcript.jsonl` and `somni_search` |
| Memory notes written by the agent, which judges salience | Claims with salience 1–5 (`somni_note`) |
| Representations: pages that gather one subject and link to notes and each other (wikis) | `wiki.jsonl` pages with `[[slug]]` links and `[nXXXXXX]` citations, rendered as an Obsidian vault |
| Small injected context loaded every session (an index) | The bounded index from `inject`: page titles, then claims by salience |
| Dreaming corrects memory against the transcript | `correct` / `retract` operations (`/somni-dream`, `somni/dream.py dream_pass`) |
| Dreaming builds new representations | `build_wiki` / `/somni-wiki`: claims gathered into pages, iterated until nearly all are covered |
| Dreaming promotes notes up the hierarchy | `promote` raises a claim's salience into the injected index without changing its id |
| Dreaming focuses memory on a goal | Every wiki pass is steered by an explicit `goal` |
| Dreaming fills gaps | `fill_gaps`: merge duplicate pages, repair pages whose claims went stale, add an overview page |

The essay reports that a wiki combined with search beat either alone. Somni's retrieval follows
that: pages first, then claims.

What Somni adds:
- **No dangling links.** A link must point to a page that already exists. The essay lists links
  to not-yet-created pages as a failure mode.
- **Checked evidence.** Every claim must cite transcript records, and `verified` claims must quote
  them exactly, which the store checks. History is hash-chained and append-only.
- **Hard size bounds** on every layer.
- **Partial dream passes** that log their rejected operations.
- **Deterministic recall.**

These are our design choices, not claims about Anthropic's system. Nothing here is Anthropic
code. Any errors are ours.

Evaluation uses [STATE-Bench](https://github.com/microsoft/STATE-Bench) (MIT) and runs on
[Pi](https://pi.dev).

## Results

The Python adapter was evaluated on the [STATE-Bench](https://github.com/microsoft/STATE-Bench) Agent Learning Track
(v0.8.1, 50 held-out test tasks per domain, three runs per condition). Learnings were built from
the shipped train trajectories only.

`gpt-6-luna` played agent, user simulator and judge. That is **unofficial**: the protocol locks
the simulator and judge to GPT-5.4, so compare these numbers with each other, not with the
leaderboard. Δ is percentage points of pass@1 against the same agent without memory, paired by
task, with 95% bootstrap intervals.

| Configuration | Travel | Customer support | Shopping | Pooled |
|---|---:|---:|---:|---:|
| No memory (pass@1) | 56.7% | 62.7% | 53.3% | 57.6% |
| **Somni wiki + search** (recommended) | **+9.3** [+1.3, +17.3] | +1.3 [−9.3, +12.0] | +3.3 [−4.0, +10.7] | **+4.7** [−0.4, +9.8] |
| Somni claims only | +8.0 [−0.7, +16.7] | +1.3 [−8.0, +10.0] | +4.7 [−0.7, +10.7] | +4.7 [+0.2, +9.1] |
| Somni wiki (pages only) | +6.7 | +1.3 | +3.3 | +3.8 |
| Somni wiki refocused on train feedback | +4.0 | +3.3 | +2.7 | +3.3 |

- **Completion:** the recommended setup raises pooled pass@1 from 57.6% to 62.2%. That's about
  8% relative, and it fixes 11% of the baseline's failures. The gain is largest in travel, small
  in shopping, and not detectable in customer support.
- **Conversation quality:** the judge's 1–5 UX score improves in travel (+0.23 [+0.04, +0.45])
  and customer support (+0.68 [+0.37, +0.99]). Claims-only memory gets about half of that.
- **Cost:** about 2× the baseline's agent tokens per task, because whole pages are retrieved.
  Claims only costs about 1.4×. The one-time build used about 130 model calls (≈1.5M tokens)
  for all three domains.
- **Not detected:** in customer support, memory made the agent better at the conversational
  requirements (asking, confirming, explaining) but sometimes over-cautious. Its final-state
  accuracy dropped slightly, so completion stayed flat.
- **What didn't help:** recording the episode date. Dreaming only over failures also made the
  agent over-cautious, consistent with the essay's warning about a "record only failures" rule.

Full methodology, every condition tried, and per-run numbers:
[`state-bench-eval/results`](https://github.com/Mechanacor3/state-bench-eval) (private).

### Recommended setup

1. **Build claims:** `build` over your trajectories, dreaming in windows of about three episodes.
2. **Build the wiki:** `build-wiki` gathers the claims into linked pages, then `fill_gaps` merges
   them, adds an overview and promotes key claims. Open `somni-v1/vault/` in Obsidian to review
   what was learned.
3. **Retrieve:** use `SomniWikiSearchLearnings`, which returns the two best pages plus the most
   similar past case.
4. **In Pi:** recording, `/somni-dream` and `/somni-wiki` do the same online. `somni_recall`
   returns pages, then claims.

`SomniLayeredLearnings`, `SomniBriefingLearnings` and `build-feedback --balanced` are
implemented and tested, but were not fully evaluated: the evaluation budget ran out.

## License

MIT
