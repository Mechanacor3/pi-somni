# Memory over time: changes, fading and forgetting

Things change. The deploy branch moves, a teammate takes over a service, the pens in the supply closet turn red. Somni
treats a fact that changed differently from a fact that was wrong, lets memories you haven't used fade into the
background, and lets old raw transcript go once its lessons are kept. [Sleep and memory](sleep-and-memory.md#memory-over-time)
explains the research each part borrows from.

## Every record is dated

Each transcript record, claim and dream pass carries the time it was written (`at`, UTC). Dream prompts show a
`--- 2026-11-24 ---` line wherever the day changes, `somni_search` results say when a record was made, and the night
dream lists the current claims its records may confirm, update or contradict, with when each was first seen.

## Changed, or wrong?

When a record disagrees with a claim, the dream decides which happened:

| What happened | Operation | The old claim becomes | Recalled? |
|---|---|---|---|
| The world changed: the claim was true until then | `update` | **past**, true until the record that showed the change | Yes, marked `PAST` |
| The claim was wrong all along | `correct` | **superseded** | No |
| The claim was wrong, with nothing to replace it | `retract` | **retracted** | No |

```json
{"op": "update", "claim_id": "n000004", "text": "Pens are red since the supplier changed.", "status": "verified",
 "salience": 3, "evidence": [{"ref": "t000412", "quote": "pens are red now"}]}
```

An `update` takes the same fields as `correct` and must cite at least one record newer than every record the old
claim cites: a change has to show up in a later record. The dream prompt asks never to leave two active claims that
disagree.

The current claim keeps its history. The index shows it as
`n000009 [...] Pens are red. (since 2026-11-24; before that: Pens are blue.)`, and `somni_recall` returns past
versions below current ones, as `n000004 [PAST: true until 2026-11-24; now n000009; verified] Pens are blue.`, so
"what colour were the pens in March?" still has an answer. Updates can chain; each version records when it stopped
being true. The vault's `claims.md` shows the same lineage. A wiki page that cites a claim which has since been
updated is stale until the next wiki pass rewrites it on the current claim.

Somni's quote check can't settle this by itself: both quotes are real. It checks that something was said, not that
it is still true. Dating records is what lets the dream tell the difference.

## Strength: fresh, fading, faint

Every claim has a strength that fades as a power of time and returns with use, after ACT-R's base-level activation:

```
activation = ln( Σ age_j ^ -d ) + 0.25 × (salience − 3)
```

Each `age_j` is the age in days (at least an hour) of one use of the claim:

- a record it cites as evidence;
- a dream writing or promoting it;
- a later record that names it by ID, such as the agent recalling `n000004` and acting on it.

`d` is `memory.decay` (`SOMNI_MEMORY_DECAY`, default 0.5).

| Label | Activation | A salience-3 claim written the day its one record was seen, and not used since |
|---|---|---|
| fresh | ≥ −0.5 | for about 10 days |
| fading | −1.5 to −0.5 | from about 10 to 80 days |
| faint | below −1.5 | after about 80 days |

Repetition and use keep a claim fresh much longer. A faint claim is still verified if it was verified: strength
measures how recently memory was used, not whether it's true. The index ranks relevant claims a little lower as they
fade and shows the label on fading and faint ones (`faint, last used 2026-10-02`), so the agent knows to check before
relying on them. `status --json` counts active claims by strength (`claims_by_strength`) and past claims (`past_claims`).

## Forgetting old records

After each night dream (`/somni-dream`, not a daydream or deep sleep), Somni forgets transcript records older than
`memory.forget_after_days` (`SOMNI_FORGET_AFTER_DAYS`, default 30; `0` keeps everything). A forgotten record keeps:

- a stub of its text: `[forgotten 2026-12-24] ` and its first 160 characters, so `somni_search` can still find it;
- every quote a claim or retraction ever cited from it, so verified claims still check against their evidence;
- its images, its other fields, and the hash of the original record, so the hash chain still verifies and records
  stay in order.

These records are never forgotten:

- the newest 120 records, which make up the next dream's window;
- any record an action word's tests replay;
- any record whose stub wouldn't be shorter than the original.

The original text is gone. A forgotten record's stub is no longer checked against the original; its place in the
chain still is. `status --json` reports `forgotten` (records and bytes dropped).

A record a dream never got to is forgotten too, once it is old enough. In a long session, more than a dream's window
can pass between dreams, so dream often enough, or raise `memory.forget_after_days`, if old raw detail matters for
your work.
