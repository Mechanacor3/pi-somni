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

## Install

Requires Pi 1.0+ and `python3` (3.10 or newer, standard library only) on `PATH`.

```bash
pi install git:github.com/Mechanacor3/pi-somni
# or for one session:
pi -e git:github.com/Mechanacor3/pi-somni
```

## Use

In a session Somni records automatically, and adds these tools:

| Tool | Purpose |
|---|---|
| `somni_recall` | Wiki pages, then claims, most relevant to a query |
| `somni_search` | Exact phrase search over the transcript (to find evidence IDs) |
| `somni_note` | Add a cited claim while working |
| `somni_dream` | Submit a consolidation pass (only during `/somni-dream`) |

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

The store also has a CLI: `python3 somni/store.py --workspace W --subject-id S
{init,session-open,session-close,record,note,dream,search,inject,report,metrics}`.

## Offline dreaming over trajectories

`somni/dream.py` runs model-authored dream passes over any window of transcript. It takes any
`complete(system, prompt) -> json_text` function; `somni/llm.py` supplies one for the OpenAI
Responses API using only the standard library. Each pass records how many operations were
accepted and why the others were rejected, so `dreams.jsonl` doubles as the learning trace.

### STATE-Bench Agent Learning Track

`adapters/state_bench/somni_state_bench.py` records every
[STATE-Bench](https://github.com/microsoft/STATE-Bench) train trajectory verbatim (user turns,
assistant text, and one record per tool call). It then dreams over windows of three
trajectories, runs a consolidation pass, and exposes `SomniLearnings.retrieve_learnings` for a
`StateBenchAgent` subclass.

```bash
OPENAI_API_KEY=... python3 -m adapters.state_bench.somni_state_bench build \
  --train-dir STATE-Bench/datasets/train_task_trajectories/travel --workspace learnings/somni/travel
```

STATE-Bench redacts "Today's date" in its shipped train trajectories, although an agent at work
would see it. Add `--task-dir STATE-Bench/state_bench/domains/<domain>/tasks --split-file
STATE-Bench/state_bench/domains/<domain>/splits/train_test.json` to restore each **train**
episode's date (`now`) from its task file. Only ids in the official train split are read, and
only the `now` field; a trajectory without train metadata is refused rather than guessed.

```python
from state_bench.agents.state_bench import StateBenchAgent
from adapters.state_bench.somni_state_bench import SomniLearnings

class SomniAgent(SomniLearnings, StateBenchAgent):
    somni_root = "learnings/somni"        # <root>/<domain>/somni-v1
```

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
# Pi end-to-end test (real Pi 1.0.0 in Docker against a scripted fake model):
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

STATE-Bench pilot results will be added here.

## License

MIT
