# Using Somni day to day

Somni gives your Pi agent a memory that improves the more you work with it. It remembers what
you told it and what worked, and it learns your project's conventions. Over time it can turn
chores you repeat into one-step tools. You don't need to learn any of the internals. This page
covers the habits that make it pay off.

## What you get

- **Fewer repeats.** Tell the agent something once ("we deploy from the `release` branch",
  "I prefer short commit messages") and later sessions start out knowing it.
- **Fewer repeated mistakes.** When something fails and gets fixed, the fix becomes a note the
  agent sees the next time a similar situation comes up.
- **Notes you can check.** Every memory cites the moment in your sessions it came from. Nothing
  is remembered from thin air, and you can read all of it as markdown.
- **Chores in one step** (optional). A sequence of tool calls that keeps recurring can become an
  *action word*: a small, readable Python function the agent calls as a single tool.

## Set up once

```bash
pi install git:github.com/Mechanacor3/pi-somni
```

Somni is tested on Pi 1.5.2 and the latest Pi release; on other versions some features may fail. It needs
`python3` 3.10 or newer. By default each project gets its own memory in
`.pi/somni/` inside the directory where you start Pi. Add it to `.gitignore`: it holds what
the agent saw in your sessions, which can include file contents and command output.

```bash
echo ".pi/somni/" >> .gitignore
```

Then run `/somni-doctor` in Pi. It checks your settings, the Python backend and the memory store
without calling a model, and says what to fix.

### Your settings file

Somni works with no settings. When you want to change something, put it in a settings file:

- `~/.config/somni/somni.conf` holds your personal defaults, for every project.
- `.pi/somni.conf` in a project overrides them for that project.

It's YAML:

```yaml
actions:
  enabled: true          # let dreams write action words
daydreams:
  every_minutes: 30
```

`/somni-doctor` points out a misspelled setting or a bad value, with the file and line. The
The [settings table](settings.md) lists every setting. Every
setting also has an environment variable, which wins over the files, for scripts and one-off runs.

## The daily loop

1. **Work as usual.** Somni records each session automatically: your prompts, the agent's
   replies and the tool results it saw. Before each of your prompts it gives the agent a short
   index of the memories that match what you asked.
2. **Say what matters.** Corrections, preferences, project facts. If it's important, say so:
   "remember that the staging database is read-only". The agent can save a note right away with
   `somni_note`.
3. **Dream at the end of a working session.** Run `/somni-dream`. The agent reviews the recent
   session and turns it into short notes, each citing what it saw. It also corrects or retires
   older notes that today's work contradicted. A dream is one short run of your current model.
   You can steer it: `/somni-dream how we release` or `/somni-dream what went wrong with the
   migration`. Dreaming is up to you: nothing dreams on its own unless you turn on
   [daydreams](#long-sessions-optional). Pi's footer shows how long it has been, for example
   `somni: last dream 14 turns and 2 days ago`. Skip a few days and the next `/somni-dream`
   works through the backlog oldest first; see [a backlog](dreams.md#a-backlog).
   Each day also gets a [journal](journal.md) entry: notes about what happened, written from the whole day.
   Run `/somni-journal` for today; a day Pi closed on gets its entry in the background next time.
4. **Organize now and then.** Once a week, or after a big piece of work, run `/somni-wiki`.
   It gathers the notes into linked pages, one subject per page, like a small team wiki. Give it
   a goal if you like: `/somni-wiki onboarding a new teammate`.
5. **Look at it.** Open `.pi/somni/somni-v1/vault/` in any markdown viewer. Obsidian, VS Code
   and a GitHub or GitLab wiki all work. `index.md` is the map; `claims.md` lists every note
   with the evidence it cites.

## Tell it what to keep

Good memories are reusable. These are worth saying out loud:

- **Preferences:** "use pnpm, not npm", "explain before you change config files".
- **Project facts:** "tests need the local Redis running", "port 8042 is the sandbox".
- **Corrections:** "no, the API returns cents, not dollars". Somni prefers correcting an
  existing note over adding a near-duplicate.
- **What worked:** "that fixed it: clear the cache before rebuilding".

Not worth keeping: one-off details like today's ticket number. Dreams are told to leave those
out.

### Preferences that always apply

Most memories reach the index only when they match what you asked. A preference about how you
work ("I like 2-space indents", "always run the tests before you say it's done", "lead with the
answer") should apply to every task, whether or not the prompt mentions it. Say it once as a
standing rule ("from now on…", "I always…"), and Somni keeps it as a **standing** note. Standing
notes head every session's index, under "How the user wants work done".

- The agent can save one straight away with `somni_note` (`"standing": true`), or the next dream
  finds it. A dream also makes one when you correct the agent the same way more than once.
- A standing note has to cite something you said. The agent's own habits and a sub-agent's
  instructions don't count.
- Up to 12 can be active. Change one by saying so ("actually, 4 spaces now"): the dream updates
  it and it stays standing. Say it no longer applies, and the dream drops it.

## When a memory is wrong

The vault is a view, so editing it changes nothing. Tell the agent instead:

1. Say what is wrong in a session: "the note about deploying from `main` is out of date; we use
   `release` now".
2. Run `/somni-dream correct the deploy notes`. The dream corrects or retracts the old note and
   cites the session where you said it. The old version stays in the history, marked as replaced.

## Chores in one step (optional)

Set `actions.enabled: true` in your settings file to let dreams write action words. A dream looks for sequences of
tool calls that keep recurring, such as "check the service status, restart it, check again". It
can turn one into a function like `restart_and_check(service)`. The agent then sees the word
in its tool list and memory index, and calls it in one step.

- Words live as plain Python files in `.pi/somni/somni-v1/actions/`. Read them, edit them, or
  delete one to retire it. Somni records your edit in its history as written by `human`.
- You can write a word yourself. The [action-words skill](../skills/somni-action-words/SKILL.md)
  shows the format.
- A word can only do what the agent could already do with its tools, and each of its calls goes
  through Pi's normal checks. If the agent has a shell tool, limit words to safe tools.
  `/somni-doctor` warns when words could call a shell:

  ```yaml
  actions:
    enabled: true
    tools: [read, grep, mcp__tracker__get_issue, mcp__tracker__comment]
  ```

## Search by meaning (optional)

`somni_search` ranks the transcript by words: records holding the whole query first, then those sharing the most
of its words, newer first among equals. A query in double quotes finds that exact phrase only.

Words miss paraphrases ("milestone post" for "funding announcement"). Point `search.embeddings_url` at any
OpenAI-compatible embeddings endpoint and search also ranks by meaning, fusing the two. A small BERT model served
locally works; for example, with llama.cpp:

```sh
llama-server -m bge-small-en-v1.5-f16.gguf --embeddings --host 127.0.0.1 --port 8091
```

```yaml
search:
  embeddings_url: http://127.0.0.1:8091/v1/embeddings
  embeddings_query_prefix: "Represent this sentence for searching relevant passages: "
```

Vectors are cached in `embeddings.sqlite` beside the store, outside its hash chain. The first search embeds every
record; `somni embed` does that ahead of time and drops vectors of text that has since been forgotten. If the endpoint
is down, search ranks words only.

## Long sessions (optional)

If one Pi session runs for hours, Somni can dream in the background while you keep working:

```yaml
daydreams:
  every_minutes: 30
```

A daydream runs in a separate Pi process and never blocks your session. The next prompt's index
shows a one-line notice of what it added. If you quit before the next one is due, the next
session in that project dreams those last turns first. It is experimental. A night
`/somni-dream` and `/somni-wiki` are still worth running.

## Your personal memory, across projects

Each project has its own memory (`.pi/somni`). Beside it, interactive Pi keeps a **personal memory** in `~/.somni`:
what Somni learns about *you*, shared by every project. When the night dream finds a lesson about you ("likes 2-space
indents", "wants the diff before any change", "is in Berlin"), it files it there; lessons about the project ("deploys
from `release/*`") stay in the project. The agent can also save a note there straight away (`somni_note` with
`"scope": "personal"`).

The index shows both: after its header, a section "From the user's personal memory" with your
[standing preferences](#preferences-that-always-apply) first, then the personal notes that match the prompt, in up
to a third of the space. Personal IDs start with `p:` (`p:n000004`, `p:t000012`); `somni_recall` and `somni_search`
find them too.

A personal note still quotes what was said. Somni copies the project records it cites into the personal memory, each
naming the store and record it came from, so the quotes check out there. The rest of the project's transcript stays
in the project.

```yaml
# ~/.config/somni/somni.conf
personal:
  path: ~/.somni      # the default in interactive Pi; `none` turns it off
```

Scripted runs (print, JSON or RPC mode: benchmarks, sub-agents) don't use it unless `personal.path` names a store.
Daydreams leave it alone; the night dream files into it. To share one whole memory across projects instead, point
them all at the same `store.path`.

Several Pi sessions can use one store at once: each records into it, and what you say in any of
them counts. One of them owns the store and runs its dreams; see
[several sessions on one memory](long-sessions.md#several-sessions-on-one-memory).

## Sharing what you learned

After a couple of weeks on something new, `/somni-share <topic>` turns what Somni learned about it into a skill other
people's agents can load: a `SKILL.md` whose guidelines each say what backs them (how many records, over how many
days, what CI or a benchmark found) and an `EVIDENCE.md` listing those records by hash, without their text. It refuses
drafts with your name, paths or keys in them, and it only writes files, in `.pi/somni/shares/`. See
[Sharing](sharing.md).

## Privacy and pausing

- Everything stays on your machine, in the store directory and `~/.somni`. Somni sends memory to
  a model only as the agent's own context, and during the dreams and daydreams you run. A share
  is written to disk; it goes no further unless you send it.
- `SOMNI_RECORD=0 pi` runs one session that uses memory but records nothing. To stop recording
  in a project, set `store.record: false` in its `.pi/somni.conf`.
- To start fresh, close Pi and move the store directory (and `~/.somni`, for what Somni learned
  about you) somewhere private. The next session creates a new one.
- To take your memory to a new machine, use `somni snapshot` there and `somni restore` here
  (`--personal` for `~/.somni`). Copying the files one by one can leave the store unable to open;
  see [moving memory](containers.md#moving-memory-snapshots).
- If both machines went on learning, `somni merge` brings one's lessons into the other: see
  [merging forks](containers.md#merging-forks).

## Checking that it helps

Your own sense is the real test: does the agent ask you fewer repeat questions and repeat fewer
mistakes? For numbers, `/somni-doctor` shows how many records, notes, pages, dreams and
action words the store holds, and how many dreams failed. For how often the agent used its words,
run this from the pi-somni package directory and look at `word_use`:

```bash
python3 somni/store.py --workspace /path/to/project/.pi/somni --subject-id pi status --json
```

We also measure it on a public benchmark of multi-step customer-service tasks, as a stand-in for
"does memory make the agent better at real work". See [EVALUATION.md](../EVALUATION.md).
