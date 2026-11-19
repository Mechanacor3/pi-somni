# pi-somni

**Somni** is a memory plugin for the [Pi coding agent](https://pi.dev). It keeps three memory layers
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
See [evaluation methods, results, and tests](EVALUATION.md) for the full comparison and replay.

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

During a dream, a `merge` operation can synthesize active claims into one new claim.
Somni sorts their IDs, inherits their evidence, and hashes the parent fingerprints together
with the combined text and status. Parent order and salience changes leave the digest
stable. The claim log and rendered vault show the parent IDs and the provenance digest.

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
pipes. The child verifies the store on open and reuses that verified snapshot for up to 128
store accesses or 60 seconds. It runs the next full check on access once either limit is
reached, when it detects an outside file change, during recovery, and after a dream. New
writes still extend the hash chain and use the transaction journal. A standalone store
CLI command loads and verifies the store when it opens it. The child exits when the Pi
session closes. Somni warns on recording errors, including a full store. To archive a full
store, close all Pi sessions using it, then move the directory named by `SOMNI_WORKSPACE` (by default
`.pi/somni`) to a private location. The next Pi session creates a clean store. Stale wiki pages
remain visible in the vault for repair while recall and the injected index skip them.

The store also has a CLI: `python3 somni/store.py --workspace W --subject-id S
{init,session-open,session-close,record,note,dream,search,inject,report,metrics}`.

## Dreaming internals

Somni's Python backend implements claim consolidation, wiki building, promotion, and
gap filling. The commands in [Use](#use) apply these operations in Pi. For trajectory
imports, build recipes, and benchmark details, see [Evaluation](EVALUATION.md).

## Safety notes

- The extension is a policy layer, not a sandbox. Run untrusted agents in a container whose only
  writable mount is the Somni workspace.
- Record only what the agent itself saw or produced. Hash chains detect accidental or
  out-of-band edits on load; they are not signatures against an agent that owns the files.
- `verified` means "the cited excerpts exist", not "the interpretation is true".

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

## License

MIT
