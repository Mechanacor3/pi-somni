# Containers, harnesses and moving memory

Somni runs the same inside a container as on a laptop: a Pi with the extension, a Python 3 for the backend, and a
directory that holds the store. This page covers what to mount and what to set, how to dream when nobody is at the
keyboard, how to carry memory from one container to another, and how to merge what copies learned apart. For
scripted runs in general, see [Headless runs](headless.md).

## What a container needs

- **Pi and Python 3.** Somni's backend uses only the standard library. Pillow is optional, for image thumbnails.
  [`tests/e2e/Dockerfile`](../tests/e2e/Dockerfile) is a minimal image: Node, a pinned Pi, Python 3 and git.
- **Pi's login.** Give the container its own Pi agent directory (`PI_CODING_AGENT_DIR`) with the provider login
  it should use, mounted read-only where you can. Somni never reads it.
- **A volume for the store.** Everything Somni keeps for a project is in one directory: `.pi/somni` under the
  directory Pi starts in, or wherever `SOMNI_WORKSPACE` points. Put it on a volume so it outlives the container.
  The personal store (`~/.somni`) is off in scripted runs; mount it and set `SOMNI_PERSONAL` only if the container
  should learn about its user across projects.

```sh
docker run --rm \
  -v somni-memory:/memory \
  -v "$HOME/.pi-container:/pi-agent:ro" \
  -e PI_CODING_AGENT_DIR=/pi-agent \
  -e SOMNI_CONFIG=none -e SOMNI_WORKSPACE=/memory -e SOMNI_SUBJECT=assistant \
  my-pi-image pi -e /opt/pi-somni/extensions/somni.ts -p "Plan today's work"
```

## Settings a harness usually sets

| Variable | Why |
|---|---|
| `SOMNI_CONFIG=none` | Ignore config files, so the run depends only on its environment. |
| `SOMNI_WORKSPACE`, `SOMNI_SUBJECT` | Which store, and whose memory it is. One subject per store. |
| `SOMNI_RECORD=0` | Use memory without recording: an evaluation, or a reviewer looking around. |
| `SOMNI_PERSONAL=none` or a path | Off by default in scripted runs; a path shares one personal store. |
| `SOMNI_SESSION_ROLE` | `peer` when a second scripted session is really the user's own; sub-agents stay `contributor`. |
| `SOMNI_NOW` | A fixed current time, for replaying dated history: records and dreams take that date. |
| `SOMNI_RUN_LABEL`, `SOMNI_TAGS` | Label a task's records, so outcomes and dreams can tell tasks apart. |

Every setting is in [Settings](settings.md).

## One store, one machine at a time

A store's locks are ordinary file locks (`flock`). Several Pi sessions on the same machine can share a store, and
Somni sorts out who owns it ([several sessions on one memory](long-sessions.md#several-sessions-on-one-memory)).
Two containers on the same Docker volume and host are fine for the same reason. Don't share one store between hosts
over a network file system, where file locks are unreliable. Give each host its own store and move memory with
snapshots.

## Dreaming in a container

Nothing dreams on its own unless daydreams are on. A container that works through a queue of tasks should dream
on a schedule: after every few tasks, or at the end of a batch.

```sh
pi -e /opt/pi-somni/extensions/somni.ts -p "/somni-dream"
```

A dream reviews up to 120 records per pass. When more are waiting, one `/somni-dream` works through them oldest
first, up to `dreams.max_passes` (4) passes, and says how many records still wait. A busy container should dream
before that backlog builds up: `status --json` reports `undreamed`. To replay dated history, set `SOMNI_NOW` on each
turn and on each dream to the time it happened. See [a backlog](dreams.md#a-backlog).

## Moving memory: snapshots

A store is several files that must stay consistent with each other and with an empty lock file. Copying them one by
one (rsync with filters, sync folders, `docker cp` of a live store) can drop the lock file or catch a write halfway.
Use a snapshot instead:

```sh
# in the container that learned it (safe while Pi is recording)
somni snapshot --store /memory --out /backup/assistant-2026-11-27.tar.gz

# inspect one
somni snapshot --show /backup/assistant-2026-11-27.tar.gz

# in the container that should start from it
somni restore /backup/assistant-2026-11-27.tar.gz --store /memory
```

- **What's in it.** The store's records, claims, dreams, wiki pages, action words and images, plus a manifest with
  every file's SHA-256 and the hash chains' heads. Lock files and caches stay behind.
- **Taking one.** `somni snapshot` holds the store's lock while it copies, so a recording session just waits a
  moment, and it checks the chains first.
- **Restoring.** `somni restore` checks every file against the manifest, refuses links and paths outside the store,
  recreates the lock file, and verifies the chains before it moves the store into place. It won't overwrite a store
  that holds memory unless you pass `--replace`, and then it keeps the old one beside it.
- **The subject.** A snapshot keeps its subject. Restore says so if it differs from the target's `store.subject`, so
  you can set `SOMNI_SUBJECT` to match.
- **Personal memory.** `--personal` snapshots or restores the personal store instead of the project's.

Without `--store`, both commands use the store that Pi would use in `--cwd` (default: the current directory).

## Merging forks

A store restored from a snapshot is a fork: from then on, each copy learns on its own. `somni merge` brings what
another copy learned back into this one, the way git merges branches. It needs both copies to come from the same
snapshot, and it doesn't use git.

```sh
# the container's copy went on learning; bring it home
somni snapshot --store /memory --out /backup/worker-1.tar.gz        # in the worker
somni merge /backup/worker-1.tar.gz --store /memory --dry-run       # here: what would happen
somni merge /backup/worker-1.tar.gz --store /memory
```

The other side can be a snapshot file or a store directory. What the merge does:

- **It finds the base.** The two transcripts share a start, the records they had when they forked, and then
  diverge. Records are compared by their place in the hash chain, so a record one side has since forgotten still
  counts as shared.
- **Nothing new over there:** "up to date". **Nothing new here:** a fast-forward. This store becomes a copy of the
  other, and the old one is kept beside it.
- **Otherwise the merge brings in the other side's new records.** Each one becomes a copy here that names the
  record it came from. Then every claim is compared three ways: as it was at the base, here, and over there.
  - A claim only one side changed takes that change: an update, a correction or a retraction.
  - A claim only the other side has comes in, with its citations pointing at the copies.
  - A claim both sides changed is a **conflict**. The other side's version comes in beside this one's, and the
    next `/somni-dream` is shown both and settles them with the evidence. Nothing is lost while they wait.
- **It is one pass.** The copied records, the claim changes and a pass entry (author `merge`) are one atomic commit.
  The entry says where they came from and lists the conflicts. Merging the same fork again later brings only what's
  new since.

**Forgetting** has three modes (`--forgetting`, or the `merge.forgetting` setting):

| Mode | A shared record one side forgot |
|---|---|
| `wins` (default) | is forgotten here too. Forgetting is how a store stays small and drops detail nobody cited. |
| `conflict` | stops the merge with a list of those records, so you choose `wins` or `keep` for this merge. |
| `keep` | gets its whole text back from the side that still has it, checked against the record's chain hash. A store that forgets will forget it again at a later night unless forgetting is off there (`SOMNI_FORGET_AFTER_DAYS=0`). |

**Dream before you merge.** It's like git asking for a clean working tree first. When both sides have dreamed, the
copied records count as reviewed: their lessons came along as claims. If this store has records no dream has
reviewed yet, the next dream reviews those and every copied record, which takes more passes. The report says so.

Not merged yet: wiki pages and action words. The report counts the other side's changes to them. Both stores must
have the same subject, and no session may be recording into this store while it merges. `--unrelated` merges a
store that shares no history with this one: everything in it comes in as new.

## Freezing memory for an evaluation

To test what an agent learned without letting the tests change it:

1. Let the agent work, and dream, with recording on.
2. Take a snapshot. That snapshot is the frozen memory.
3. For each test, restore the snapshot into a fresh store (a temporary directory) and run with `SOMNI_RECORD=0`.
   Throw the store away afterwards, so no test can affect the next one.

Reading memory (the index, `somni_recall`, `somni_search`) doesn't change a store. A fresh copy per test still keeps
tests independent, whatever the agent tries.
