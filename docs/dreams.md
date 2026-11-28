# Dreams and the wiki

## Dreams

Run `/somni-dream [focus]` to consolidate recent transcript into claims. Somni closes the recording session, asks the
model for one pass, and reopens recording when the pass settles. Invalid operations (for example, a quote that does
not match) are rejected one by one and listed in the pass record. A pass adds, corrects, updates, retracts, merges or
promotes claims, and is bound to the transcript head it reviewed. A correction says a claim was wrong; an update says
the world changed since it was true, and keeps it as history (see [Memory over time](memory-over-time.md)). Claim
history is never deleted: corrections, updates and retractions keep their lineage. After a night dream, transcript
records older than `memory.forget_after_days` keep only a stub.

- **Gists.** Dreams can supply an optional one-line `gist` of up to 160 UTF-8 bytes when they add, correct, or merge a
  claim. `somni_note` also accepts a gist. The index uses the gist; `somni_recall` returns the full claim text. All
  readers of a store that contains gists need a Somni version that supports them.
- **Salience.** Dream guidance reserves salience 5 for up to two broadly useful claims per pass and uses 3 for an
  ordinary reusable lesson.
- **Images.** With a vision-capable model, Somni attaches up to four recent images within a 1 MiB total budget. The
  dream can open older images with `somni_image` and cite one using `{"ref":"t000042","image_sha256":"<digest>"}`,
  after receiving it as an attachment or through the tool. Use `uncertain` for interpretations the evidence does not
  settle.
- **Merges.** A `merge` operation synthesizes active claims into one new claim. Somni sorts their IDs, inherits their
  evidence, and hashes the parent fingerprints together with the combined text and status. Parent order and salience
  changes leave the digest stable. The claim log and rendered vault show the parent IDs and the provenance digest.

### A backlog

A pass reviews up to `dreams.records` (120) transcript records. When more than that have piled up since the last
pass (a long day, a week without dreaming), `/somni-dream` works through them oldest first. Each pass reviews the
next part, with a few earlier records for context, and is bound to the end of that part, so the next pass starts
there. The first passes are told that newer records wait, so they learn what held at the time and leave changes to
later passes. One `/somni-dream` runs up to `dreams.max_passes` (4) passes. If records still wait after that, it says
how many, and the next `/somni-dream` continues. A daydream facing a backlog likewise reviews its oldest part, and a
catch-up daydream starts the next one when it is done. `status --json` reports `undreamed`, the records no pass has
reviewed.

### Budgets

Offline dream and wiki turns allow 16 Somni evidence reads, 16 viewed images and three submission attempts by default
(`SOMNI_DREAM_MAX_READS`, `SOMNI_DREAM_MAX_IMAGES`, `SOMNI_DREAM_MAX_SUBMISSIONS`), with one committed pass. Every
Somni tool result in a dream ends with what is left, for example
`[dream budget left: images 9/16, reads 11/16, submissions 3/3]`. When a budget runs out, the message says what to do
next.

Arguments are forgiving. A `somni_search` or `somni_recall` `limit` outside its range is clamped, and the result says
so. An `image_sha256` may be a unique prefix of at least six hex characters, and a digest that doesn't belong to the
cited record gets back the digests that record does have.

A submission whose operations are all rejected commits nothing, lists each operation's reason, and says how many
attempts are left. A submission with some valid operations commits those and lists the rest. With
`SOMNI_LENIENT_IMAGE_EVIDENCE=1`, a citation whose image is wrong or wasn't viewed keeps its `{ref}`. A verified claim
left without a quote or image becomes uncertain, and the pass records each change in `image_downgrades`.

### Failed and interrupted dreams

A dream or wiki turn that commits nothing is logged in `somni-v1/dream-failures.jsonl` with its cause
(`provider_error`, `no_submission`, `all_rejected`, `invalid_submission`, `budget_exhausted`, `timeout` for a daydream
stopped by its time limit, or `other`), its kind (`dream`, `wiki` or `daydream`), the run label and the reads,
submissions and images it used. `status --json` reports `dream_failures` as counts by cause and kind, plus the latest
entry. The log sits beside the hash-chained logs and is bounded at 1 MiB: past that, the oldest half is dropped and
the counts keep the total.

A dream's evidence reads (Somni tool results other than `somni_dream`) are kept in `somni-v1/dream-cache.json` until a
pass commits. The cache is keyed by the kind of turn, the transcript head and the pass count. If an attempt is cut off,
say by a provider error, a retry on the same records within `SOMNI_DREAM_CACHE_MINUTES` (default 60, measured from the
latest read; `0` disables) gets those results in its prompt under "Evidence from an interrupted attempt". The images it
viewed are attached again, so the retry needn't spend turns or read budget repeating them. A daydream's cache is keyed
to its snapshot.

## The wiki

Run `/somni-wiki [goal]` to gather claims that are not yet on a page into linked wiki pages. Every wiki pass is
steered by its goal. Pages cite claims inline (`[n000012]`) and link other pages with `[[slug]]`. Links can point
forward to pages created in the same pass and can form cycles, but every link must resolve when the pass ends,
including after any page drops. Set `SOMNI_PAGE_GUIDE` to replace the default wiki-writing guidance for your domain.

After every dream Somni renders the wiki as a **vault of markdown notes** in `somni-v1/vault/`. Each page is one note
with YAML frontmatter, *See also* and *Backlinks* sections, and an *Evidence* list. `index.md` is the map of content,
showing folders as a tree, and `claims.md` shows every claim with its transcript quotes and image links. A page slug may
be a folder path such as `fares/refunds` (at most three levels), rendered as `fares/refunds.md`. Dreams are asked to
group pages into a folder once a topic has several. Links render as relative markdown links
(`[Refunds](fares/refunds.md)`, `[n000004](../claims.md#n000004)`), so the folder works as a GitLab or GitHub wiki and
in Obsidian. `SOMNI_LINK_STYLE=wikilink` renders Obsidian `[[wikilinks]]` instead. Dreams write `[[slug]]` links either
way. The vault is a view; edits there are not read back. Stale pages remain visible in the vault for repair while
recall and the injected index skip them.

Days get their own notes too: see [the daily journal](journal.md).

## The injected index

Before each Pi turn, Somni injects a small memory index ranked against the current user prompt. Standing claims
(the user's lasting preferences; see [everyday use](everyday-use.md#preferences-that-always-apply)) come first, whatever
the prompt, in up to a third of the space; one that doesn't fit is ranked with the rest. It prioritizes exact
run labels and tags, then uses lexical similarity (BM25), salience, and the recency of the cited evidence. Pages get up
to half the available space, with an excerpt from the best matching page. A long page can contribute its procedure
section. Claim lines include evidence IDs and use a short gist when one is available. The index skips entries that
exceed the remaining space and is capped at 8 KiB. Use `store.py ... inject --query 'your prompt'` to preview it; Pi
uses up to the first 64 KiB of the prompt for ranking.

## Internals

Somni's Python backend (`somni/dream.py`, `somni/store.py`) implements claim consolidation, wiki building, promotion,
and gap filling. For trajectory imports, build recipes, and benchmark details, see [Evaluation](../EVALUATION.md).
