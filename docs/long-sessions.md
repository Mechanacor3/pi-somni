# Long sessions, daydreams, and store ownership

## Daydreams (experimental)

A daydream is a dream that runs beside a working session instead of after it, so a long session can use what it
learned an hour ago after Pi has compacted it out of context. Turn them on with `daydreams.every_turns` or
`daydreams.every_minutes` in [somni.conf](settings.md) (`SOMNI_DAYDREAM_EVERY_TURNS`, `SOMNI_DAYDREAM_EVERY_MINUTES`).

**What happens.** When one is due, the session that owns recording starts a child Pi
(`pi --mode json -p "/somni-dream <focus>"`) and never waits for it. A daydream is skipped while another is running,
when nothing has been recorded since the last one, or once `SOMNI_DAYDREAM_PER_HOUR` (default 4) have started in the
past hour. A child that runs past `SOMNI_DAYDREAM_TIMEOUT_SECONDS` (default 300) is stopped. The default focus is
lessons from the newest records: what you told the agent, facts the work turned up, and steps that worked or failed.
It says that a lookup that found nothing is not a fact. Wiki pages are left to the night's `/somni-dream` and
`/somni-wiki`.
A daydream reviews only the records since the last pass, plus the eight before them for context (at most
`SOMNI_DREAM_RECORDS`), so frequent daydreams don't resend records that earlier passes already reviewed. An offline
`/somni-dream` still reviews the newest `SOMNI_DREAM_RECORDS` records.

A daydream is kept small so it doesn't hold up your session. Its record listing is capped at
`daydreams.prompt_bytes` (16 KiB, newest first), and each tool result in it is a one-line stub with the call and the
start of its output; `somni_search "t000123"` returns the whole record. A daydream gets `daydreams.max_reads`
(6) evidence reads, and it is stopped after `daydreams.timeout_seconds` (300).

**What you see.** When the pass commits, Pi shows a one-line notice, and the next turn's injected index starts with it,
for example `Somni daydream d000012: +2 claim(s), 0 merged, 1 action word change(s), 0 rejected (records up to 481,
$0.21, 48k tok, 52s).` The session registers any new or revised words at that turn. A daydream whose Pi fails to
authenticate gets a `Somni daydream skipped: auth` notice instead of the generic failure.

**What it costs.** A daydream is a second Pi process making its own model calls, by default with the session's model
and Pi's default thinking level. Set `daydreams.model` to a cheaper model and `daydreams.thinking` to a lower level to
spend less. With a model served locally, a daydream competes with your session for the same server, which can make
turns feel slower while it runs. The daydream's Pi totals the usage its provider reports (`input`, `output`,
`cacheRead`, `cacheWrite`, `totalTokens`, `cost`, model `calls`, `duration_ms`) and submits it with the pass. It is
stored in the hash-chained pass record as `usage`, covering spend up to the submission.

**Forked daydreams.** With `daydreams.fork: true`, a daydream starts as a fork of your saved session
(`pi --fork`), with your session's own Pi arguments, so it loads the same extensions, tools and system prompt. It
begins from your whole conversation, and its requests start with the same prefix as your session's, which the
provider has usually cached already. It keeps your session's prompt cache key. Somni blocks every tool in the fork
except its own evidence tools, and the fork's prompt tells it not to continue your task. The fork's session copy is
kept under the store's `forks/` folder only while it runs, so it never shows up in your session list. A fork needs a
saved session (not `--no-session`) and your session's model: with `daydreams.model` set, daydreams start cold. If a
fork fails to start, the session's later daydreams start cold, and the notice says so.

**Dream steps and the night's dream.** A forked daydream is a *dream step*: it reflects on the work since the last
step and writes the lessons down as unverified (`uncertain`) claims citing their records, without quoting them. The
records' text is already in its conversation, so its prompt lists only their IDs. When a step commits, the session's
next turn is shown what it added, so the agent can use those lessons right away, and the next step sees earlier
steps in its own conversation. The night's `/somni-dream` starts from the day's unverified lessons: it checks each
against the records, turns it into a verified claim with quotes, merges duplicates and retracts what the records don't
support, then adds what the steps missed.

**How it stays consistent.** The child's backend runs `store.py serve --daydream`, beside the session that owns
recording, without taking its lease. It pins the transcript head H it sees at start. `recent` stops at H, and the only
write it accepts is one dream pass. That pass is a *live* pass, marked `"live": true` and bound to H instead of the
current head, and it can cite only records up to H. The store lock serializes it with the owner's appends, and the
owner picks up the new claims and words at its next turn. The child reuses a verified snapshot for up to 128 store
accesses or 60 seconds. It runs the next full check on access once either limit is reached, when it detects an outside
file change, during recovery, and after a dream. The child exits when the Pi session closes.

## Long logs

The transcript, claim and dream logs are hash-chained across **segments**. Before an append would take a live log past
8 MiB, Somni seals it into `somni-v1/segments/<log>-NNNNNN.jsonl` and records the segment's count, head and SHA-256 in
`state.json`. Sealed segments are immutable and are re-checked against their hash when they change on disk. Routine
re-verification only re-parses the live tail. An interrupted seal is finished on the next load.

Default limits are 65,536 transcript records and 4,096 dream passes. A store's `manifest.json` `limits` can raise
`max_transcript_records` and `max_dream_passes` to 1,048,576. `status --json` reports `segments`,
`transcript_live_records`, and the effective `limits`.

Somni warns on recording errors, including a full store. To archive a full store, close all Pi sessions using it, then
move the directory named by `SOMNI_WORKSPACE` (by default `.pi/somni`) to a private location. The next Pi session
creates a clean store.

## One owner per store, and sub-agents

One Pi process owns recording in a workspace at a time: it opens and closes the session, and only it writes notes,
runs dreams and daydreams, and records the tool catalog. Any other Pi that records in the same store, such as a
sub-agent your session starts, or a second terminal, is a **contributor**: it appends its prompts, tool results and
replies to the same transcript, tagged `contributor:<its session ID>`, so dreams see what sub-agents found and can
tell whose work a record is. Contributors can read memory but can't change claims. Their records are skipped, with a
warning, while the owner runs an offline dream. Action-word mining keeps each session's calls in their own order.
Close a contributor and reopen it after the owner exits to take over ownership.

Pi starts one local Python child for the session and exchanges requests over private pipes. The child holds an OS lock
on `.somni-session.lock`. The file stays in the workspace; leave it in place so concurrent sessions lock the same file.
If Pi is killed, the pipe closes and the child releases its lock. If the child is killed too, the kernel releases it
directly. The next writer verifies the chain and recovers an interrupted transaction on startup. PID namespaces do not
affect ownership on the same mounted store. New writes extend the hash chain and use a transaction journal. A
standalone store CLI command loads and verifies the store when it opens it.

The extension can be mounted read-only with `SOMNI_WORKSPACE` on a separate writable mount.
