# Sharing what you learned as a skill

Say you spent two weeks on something new: a test setup, a deploy pipeline, a way of working with a tool. Somni kept
notes the whole time, from what you said, what the tools returned and what CI or a reviewer found afterwards.
`/somni-share` turns the notes on one topic into a skill other people's agents can load. It comes with the evidence
behind each line.

```
/somni-share running flaky test suites
```

The agent picks the notes on that topic and rewrites them as guidelines for someone else's projects. Then it calls
`somni_share`, which writes a folder in `.pi/somni/shares/<name>/`:

- **`SKILL.md`** uses the [Agent Skills](https://agentskills.io) format: a `name` and a `description` up top, then the
  guidelines. Pi, Claude Code, Codex and other agents that read skills can load it. Under each guideline is a line on
  what backs it, for example *4 record(s) over 9 day(s), 2026-11-03 to 2026-11-12; the author said it once; 2 tool
  result(s); outcomes 1 pass (ci)*. An agent can then lean on a well-tested guideline and treat a one-off with care.
- **`EVIDENCE.md`** goes guideline by guideline. It names the notes each one rests on and the records behind those
  notes: what kind each is, the day, the result (a tool's ok or failure, an outcome's verdict and who gave it) and the
  record's hash. It holds none of the records' text.

Nothing is published. Read both files, edit them as you like, then put the folder wherever you share things: a repo,
a gist, your team's skills folder.

## What counts as evidence

The guidelines rest on your own work, so the evidence is your experience, counted:

- **What you said.** A *user message* is something you typed: a preference, a correction, a rule you gave.
- **What the tools showed.** A *tool result* is what a command or tool returned, ok or failed.
- **What was checked from outside.** An *outcome* is a verdict recorded afterwards by CI, a grader, a reviewer or a
  benchmark ([Outcomes](outcomes.md)). These are the strongest evidence a share can carry, since the agent didn't
  write them. To back a share with measurements, record them as outcomes while you work, for example
  `somni outcome --by benchmark --verdict pass --text "grid 9: 22/24 with the change, 18/24 without"`.
- **How the note held up.** *Verified* notes quote their records exactly. *Revised* counts the times a note was
  updated because things changed.

The dream is told to prefer verified notes, notes an outcome confirmed and notes learned over many days. It leaves out
notes an outcome contradicted, and it says so when a guideline rests on a single case.

**Checking a record later.** Records are hash-chained, and EVIDENCE.md gives each one's SHA-256 (over its canonical
JSON: sorted keys, no spaces), plus the chain's head when the share was made. If someone asks what backs a guideline,
you can show them the record. They can hash it and find it in the table. You choose which records to show; the share
gives nothing away by itself.

## Keeping personal details out

`somni_share` refuses a draft whose text has any of these, and lists each one so the agent can rewrite it:

- home paths, the project's path and the store's path
- your username, the machine's name, and your git `user.name` (and each part of it) and `user.email`
- email addresses and IP addresses
- anything shaped like a key or token (`sk-…`, `ghp_…`, `AKIA…`, private-key headers, long random strings)
- names you list in `personal.never_share` (`SOMNI_NEVER_SHARE`), comma-separated, such as a client's or a teammate's

The check is a backstop and can miss things, such as a customer named in passing. Read the files before you share them.

## Notes from your personal memory

A share can cite notes from your [personal memory](everyday-use.md#your-personal-memory-across-projects) (IDs that
start with `p:`) next to the project's own. EVIDENCE.md then gives both chains' heads. Lessons about how you like
to work rarely belong in a public skill, though. The dream is told to pick what would help someone else.
