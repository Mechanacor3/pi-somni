# pi-somni

**Somni** gives the [Pi coding agent](https://pi.dev) a memory that gets better the more you work with it.
It remembers what you told it, what worked and what went wrong, and your project's conventions.
With action words on, it can also turn chores you repeat into one-step tools. Every memory cites the
session it came from, and you can read all of it as markdown.

**New here? Start with [Using Somni day to day](docs/everyday-use.md)**: setup, the daily loop,
correcting a memory, and privacy.

## Install

Requires `python3` (3.10 or newer) on `PATH`. Optional image downscaling uses Pillow.

```bash
pi install git:github.com/Mechanacor3/pi-somni
# or for one session:
pi -e git:github.com/Mechanacor3/pi-somni
```

**Pi versions.** Somni is tested on Pi 1.5.2 and on the latest Pi release. Other versions may work, but some features may
fail on them; daydreams, for example, fail on Pi 1.0.0.

## Try it

1. Start Pi in a project directory and give it a harmless example: “Our sandbox smoke test uses port 8042. Remember
   this using evidence from this session.”
2. Run `/somni-dream smoke test convention`, then `/somni-wiki project conventions`. These make model calls. The
   resulting notes are in `.pi/somni/somni-v1/vault/`.
3. Start a fresh Pi session in the same directory and ask which port the smoke test uses.

## How it works

Somni records each session and consolidates it offline ("dreaming") into layers:

1. **Transcript**: a hash-chained, append-only record of what the agent saw (prompts, bounded tool results,
   screenshots) and said.
2. **Claims**: short notes, each citing transcript records. A `verified` claim quotes each record exactly, or cites a
   stored image, and the store checks it.
3. **Wiki**: pages that gather one subject, cite claims inline and link each other. Every link must resolve. Somni
   renders the wiki as a markdown vault that works in Obsidian or as a GitLab or GitHub wiki.
4. **Dreams**: passes that correct, add, retract, merge or promote claims and write pages. History is never deleted.

Before each turn, Somni injects a small memory index (4 KiB by default) ranked against your prompt, so the agent starts
with what is most relevant. The design follows Anthropic's essay
[*The components of agent memory*](https://www.anthropic.com/research/components-of-agent-memory); see
[Background and credits](#background-and-credits).

## Commands and tools

| Command | What it does |
|---|---|
| `/somni-dream [focus]` | Consolidate recent sessions into claims |
| `/somni-wiki [goal]` | Gather claims into linked wiki pages |
| `/somni-doctor [all]` | Check settings, the store and features offline, without a model call |

| Tool | Purpose |
|---|---|
| `somni_recall` | Wiki pages, then claims, most relevant to a query |
| `somni_search` | Exact phrase search over the transcript (to find evidence IDs) |
| `somni_image` | Load image evidence by transcript ID and SHA-256 |
| `somni_note` | Add a cited claim while working |
| `somni_dream` | Submit a consolidation pass during `/somni-dream` or `/somni-wiki` |

Two skills come with the package. `somni-memory` tells the agent how to use the index, when to recall, and what is
worth a note. `somni-action-words` covers action words. Pi lists a skill only when the `read` tool is active.

## Features

- **[Settings and `somni doctor`](docs/settings.md).** One YAML-style `somni.conf` for you
  (`~/.config/somni/somni.conf`) and one per project (`.pi/somni.conf`). Environment variables override both.
  `/somni-doctor` reports problems with their file and line.
- **[Dreams and the wiki](docs/dreams.md).** Budgets, gists, image evidence, merges, failed and interrupted dreams,
  and how the index is ranked.
- **[Memory over time](docs/memory-over-time.md).** Dated records; a fact that changed is kept as history (`update`)
  rather than treated as a mistake; claims fade from fresh to faint unless they are used; and after a month, old raw
  transcript keeps only a stub with its cited quotes.
- **[Outcomes](docs/outcomes.md).** `somni outcome` records what happened after the agent was done (a CI failure, a
  review, a grader's verdict, the user's "that didn't work"). Dreams see it beside the agent's own report and learn
  most from the times it said "done" and wasn't.
- **[Action words](docs/action-words.md)** (experimental). Dreams turn a recurring sequence of tool calls into one
  checked Python function that the agent calls as a single tool.
- **[Daydreams](docs/long-sessions.md#daydreams-experimental)** (experimental). Dreams that run beside a long session,
  so it can use what it learned after Pi compacts its context.
- **[Deep sleep](docs/action-words.md#deep-sleep-building-words-with-real-tools)** (experimental). `/somni-deepsleep`
  builds and rehearses one action word (also called a habit, trick or reflex), trying it with real tools you allow.
- **[Sleep and memory](docs/sleep-and-memory.md).** How Somni's recording, daydreams, dreams and deep sleep relate to
  human sleep, and where the analogy breaks.
- **[Headless runs](docs/headless.md).** Scripts and harnesses: one-shot dreams, run labels and tags, the store CLI.

**Does it help?** The real test is your own work: fewer repeat questions and fewer repeated mistakes.
To measure it, we use the public STATE-Bench benchmark of multi-step service tasks as a stand-in for
"better at day-to-day work". With the Python adapter, the wiki + search condition scored an estimated
+4.7 percentage points in pooled task completion (95% interval −0.4 to +9.8). See
[evaluation methods, results, and tests](EVALUATION.md).

## Safety notes

- The extension is a policy layer, not a sandbox. Run untrusted agents in a container whose only
  writable mount is the Somni workspace.
- Record only what the agent itself saw or produced. Hash chains detect accidental or
  out-of-band edits on load; they are not signatures against an agent that owns the files.
- `verified` means Somni checked the cited excerpt or image against the transcript.
- An action word can do nothing the agent could not do with the same tools. Each of its calls goes
  through Pi's guards, but an earlier call's effects stay in place when a later call fails or is blocked.
  Set `SOMNI_ACTION_TOOLS` to keep words away from command tools the agent has.
- Keep the store (`.pi/somni` by default) out of version control. `somni doctor` warns when Git doesn't ignore it.
- One Pi session owns a store; others in the same directory, such as sub-agents, add their records beside it. See
  [store ownership](docs/long-sessions.md#one-owner-per-store-and-sub-agents).

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
