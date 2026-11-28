# The daily journal

Somni keeps a journal: one note per day, written from everything recorded that day. Claims and wiki pages hold what
lasts (rules, preferences, how things work). The journal holds what happened: what you worked on and why, what was
decided, what failed, and what was left open, in the day's own words and numbers.

Each entry is a markdown note in the vault at `somni-v1/vault/journal/yyyy/mm/dd.md`, and `index.md` lists the newest
days.

## Writing an entry

```
/somni-journal              today so far
/somni-journal yesterday
/somni-journal 2026-11-27
```

A journal pass is one model call. It reads the day's whole transcript: every record from midnight to midnight in the
machine's time zone (`TZ`), up to about 384 KB (roughly 100k tokens). A longer day is clipped evenly, record by record,
and the model can read any record whole with `somni_search "t000123"`. It also sees the claims dreamed from that day,
and the day's existing entry, if any. It saves the entry with the `somni_journal` tool.

Running it again for the same day rewrites the entry whole, as a new version. Recording goes on during the pass: unlike
a dream, a journal pass doesn't close the session.

## What makes an entry acceptable

Like a claim, every note in the journal points at what it comes from:

- Notes end with the records they come from, as `[t000123]`. An entry must cite at least one record.
- Every cited record must be from that day. Citing a record from another day, or one that isn't in this memory, is
  refused with the reason, and the model fixes the entry and submits again (up to `dreams.max_submissions`).
- A note may cite a claim as `[n000045]`; the claim must exist. In the vault it links to `claims.md`.
- Lines with no citation are allowed and counted. The tool reports them as `uncited_lines`.

Entries live in `somni-v1/journal.jsonl`, hash-chained like the rest of the store, so an edited entry is caught when
the store loads. The vault notes are a view; edits there are not read back.

## Catching up in the background

If Pi closes before a day's journal is written, the next session writes it. When a session that owns recording
starts, Somni looks at the most recent past days that have records (`journal.catch_up_days`, 3 by default). It writes
the oldest one with no entry in the background, then the next, one at a time. A session that runs past midnight checks
again after its next turn. Each catch-up is a cold Pi worker, like a [daydream](long-sessions.md#daydreams-experimental):
it uses `daydreams.model` and `daydreams.thinking` when set, and the session's model otherwise. A notice says when an
entry was written, or why it wasn't.

`journal.auto` (`SOMNI_JOURNAL`) controls this:

| Value | Background catch-up |
|---|---|
| `auto` (default) | In interactive Pi only |
| `on` | In every mode, including scripts and harnesses |
| `off` | Never; only `/somni-journal` writes entries |

Today is never caught up, since it isn't over. Run `/somni-journal` for that.

## How it fits with the rest

- **Forgetting.** After 30 days, raw records keep only a stub ([memory over time](memory-over-time.md)). Journal
  entries are never forgotten, so the day's notes outlast its raw transcript. The citations stay valid, since a
  forgotten record keeps its ID, its date and the hash of what it said.
- **Recall.** The journal is for reading. `somni_recall` and the injected index don't search it yet.
- **Snapshots** carry `journal.jsonl` with the rest of the store. `somni merge` doesn't merge journal entries.
- **Older versions.** Somni 0.9 and earlier ignore `journal.jsonl` and open the store as before.
