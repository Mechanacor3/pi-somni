# Outcomes: what happened after the agent was done

An agent's memory is built from its own transcript: what it was told, what its tools returned, what it said. Many of
the most useful facts arrive later, from somewhere else:

- CI fails after the agent pushes.
- A reviewer comments on the pull request.
- The user says "that fix didn't work" the next morning.
- A grader says the task wasn't done.

An **outcome** is a record of something like that. The agent never saw it, so the next dream is the first to learn from it.

## Recording an outcome

```sh
somni outcome --text "2 tests failed: test_parse_dates, test_empty_input" --verdict fail --by ci
somni outcome --text-file review.txt --by reviewer
somni outcome --text "no line of four" --verdict fail --by grader --run-label try-3 --image end.png
```

| Option | Meaning |
|---|---|
| `--text TEXT`, `--text-file FILE` | What happened, in the reporter's own words (`--text -` reads stdin). Up to 8 KB, the record size limit; trim long logs to the part that matters. |
| `--verdict pass\|fail\|mixed` | Optional. Whether the work succeeded. |
| `--by WHO` | Optional. Who reported it: `ci`, `grader`, `reviewer`, `user`, anything up to 40 bytes. |
| `--run-label LABEL` | Optional. The run it concerns: that run's `SOMNI_RUN_LABEL`. |
| `--tag TAG`, `--image FILE` | Optional and repeatable. Tags, and up to four images of the end state. |
| `--cwd DIR` | The directory Pi starts in, which decides which store to use (default: here). |

`somni outcome` finds the store the same way Pi does (`store.path`, `store.subject`). It works whether or not a
session is open, and prints the new record's ID. An outcome joins the same hash chain as every other record and is
dated like them. A harness can call the store CLI directly instead:
`python3 somni/store.py --workspace W --subject-id S outcome --text ...`.

A Pi session can't write an outcome: the backend refuses one from the agent. That keeps the agent's own claims and
outside reports apart. It isn't a security boundary, though. An agent with a shell can run `somni outcome` itself, so
outcomes are as trustworthy as whatever records them.

## Which report an outcome judges

Each outcome is paired with the agent report it judges. Somni looks back through records from the same run (or,
without `--run-label`, records with no run label), stopping at an earlier outcome on that run, for:

1. the last agent message that claims success ("done", "fixed", "tests pass", "✓"), or else
2. the last agent message.

When the verdict is `fail` and the agent had claimed success, the pair is an **overclaim**: the agent said it was done,
and it wasn't.

## What dreams do with them

A dream sees an outcome marked as coming from outside the agent, with the report it judges right under it:

```
[t000412] (OUTCOME, from outside the agent; by grader; verdict fail) [run: try-3] no line of four
    judges the agent's report [t000398]: AGENT: DONE — placed four dirt blocks in a straight line
    The agent reported success; this outcome says it failed.
```

The dream is told that on whether something worked, an outcome outranks the agent's own report and its counters.
It is also told that an overclaim pair is the most useful thing to learn from: it should write what the agent should
have checked before reporting success, and cite both records. An exact quote from an outcome can back a `verified`
claim, like any other record.

## Where they show up

- **The index.** Up to three outcomes that no dream has reviewed yet are listed at the top, so the next session sees
  "no line of four" even before tonight's dream. A claim that cites an outcome is marked `outcome-backed`.
- **Recall.** `somni_recall` says which of a claim's evidence came from outcomes:
  `n000012 [verified; 2 cited; outcome-backed (t000412)] ...`.
- **Status.** `status --json` counts `outcomes`: `records`, `by_verdict`, `overclaims`, and how many are
  `cited_by_active_claims`.

Outcomes are records like any other, so [forgetting](memory-over-time.md#forgetting-old-records) treats them the same:
after the forgetting window, an outcome keeps a stub and every quote a claim cites from it.

## Why the pairing matters

People learn the most from feedback on errors they were confident about. Errors made with high confidence are
corrected more often than low-confidence ones once the right answer is shown: the *hypercorrection effect*
(Butterfield & Metcalfe, 2001). Telling where a memory came from is called *source monitoring* (Johnson, Hashtroudi &
Lindsay, 1993). Outcomes make that distinction explicit: what the agent concluded is kept apart from what the world
said afterwards.
