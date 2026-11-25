---
name: somni-memory
description: Somni is this session's long-term memory of the user and their work. Use when a "Subject memory index" appears in your context, when the user asks you to remember, recall or forget something, when you are about to repeat a question or a mistake, or before saving a note with somni_note.
---

# Somni memory

Somni remembers your earlier sessions with this user. Use it to get better at their day-to-day
work: don't ask again what they already told you, don't repeat a mistake that was already
fixed, and work the way they and their project prefer.

## The index

Before each user prompt you get a short index, headed "Subject memory index". It holds the notes
(claims) and wiki pages that best match the prompt, plus any learned action words.

- "How the user wants work done" lists their lasting preferences (`standing`): indentation, commit style, how
  to report back. Follow them in every task without being reminded, unless the user now says otherwise.
- Treat the rest as advice from your past self, not as fact. Each line cites evidence IDs (`t000123`).
  When an entry decides what you do and the stakes are real, check its evidence with
  `somni_search` or `somni_recall`.
- Prefer what the user says now. If they contradict a memory, follow them and note the change.
- Memories about Somni itself (dreams, action words, `rpc.TOOL` calls, settings) describe the memory tool, not
  your work. Call tools only by the names in your tool list; `rpc.*` works only inside an action word's source.
- `uncertain` entries are interpretations. `verified` ones quote what was actually seen.
- `as of 2026-10-02` (and `faint`) means a memory hasn't been seen or used for a while. Unless something newer
  says otherwise, it is still the latest you know: use it and give its date.
- `since 2026-11-20; before that: ...` means the fact changed. Use the current one. `somni_recall` also returns
  past versions, marked `PAST: true until <date>`, for questions about how things were.
- "Outcomes since the last dream" are reports from outside you: CI, a reviewer, a grader, the user afterwards. On
  whether earlier work succeeded, they outrank what you reported at the time. `outcome-backed` claims cite one.

## Looking things up

- `somni_recall {"query": "..."}` returns the wiki pages and notes most relevant to a question.
  Use it before asking the user something they may already have told you: "which branch do
  we deploy from?", "how do they like commit messages?"
- `somni_search {"query": "..."}` finds an exact phrase in past sessions and returns the
  transcript IDs. Use it to check what was actually said or shown.
- When something you need is gone or has moved (a rotated log, a page moved elsewhere, a file
  someone deleted), or only a sub-agent saw it, search before saying you can't find it. The
  transcript keeps what this session, earlier sessions and sub-agents read.

## Saving a note

Save a note with `somni_note` when you learn something that will help in a later session:

- **A preference:** "The user wants a plan before any change to CI config."
- **A project fact or convention:** "Integration tests need the local Redis on port 6380."
- **A correction:** "Prices from the billing API are in cents, not dollars."
- **A fix that worked:** "Clearing `.cache/` before a rebuild fixes the stale-asset error."

How to write one:

- One idea per note, phrased as guidance you can act on: "Before X, do Y, because Z."
- Leave out one-off details (today's ticket number, a temporary path) unless they will matter again.
- Cite the transcript records it rests on. `verified` needs an exact quote from each cited
  record; otherwise use `uncertain`.
- When the user says how they want work done from now on ("I like 2-space indents", "always show me the diff
  first"), add `"standing": true` and cite their message. A standing note heads every later index, whatever the
  task. Don't use it for a one-off request ("4 spaces in this file") or a project fact.
- Salience 3 is an ordinary lesson. Use 4 for something that comes up often, and 1–2 for
  narrow cases.
- Never save secrets, passwords, tokens or keys, even if the user shows you one.

When the user says "remember this", save the note and tell them briefly what you saved. When
they say a memory is wrong, save the correction as a note. Then suggest
`/somni-dream correct <topic>`, so the next dream corrects or retracts the old note. When a
memory was right but things have changed since ("we moved to the new branch"), note the new fact;
the next dream updates the old note and keeps it as history.

## Dreams

The user runs `/somni-dream` to turn recent sessions into notes, and `/somni-wiki` to organize
the notes into pages. During those commands, follow the dream prompt. Keep what will help with
this user's work later: preferences, corrections, project facts and conventions, and steps and
checks that worked or failed.

## Settings and problems

Somni's settings live in `~/.config/somni/somni.conf` and the project's `.pi/somni.conf`. When the
user asks to change how memory works, or memory seems not to work, suggest `/somni-doctor`. It
checks the settings and the store without a model call and says what to fix.

## Action words

If the index lists learned action words, see the `somni-action-words` skill. A word does a
recurring chore in one call. When one fits your next step, use it instead of making its tool
calls one by one, then check that it had the effect you needed.
