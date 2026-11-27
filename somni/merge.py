"""Merging: bring what another copy of a memory learned into this one.

Two stores that grew from one snapshot are forks. Their logs share a start (the base) and then diverge. A merge works
the way git's does, on Somni's own hash chains rather than with git:

- **The base** is the longest shared start of the two transcripts, compared by each record's chain hash, so a record
  one side forgot still matches. Claims have their own base: the shared start of the two note logs.
- **Up to date / fast-forward.** If the other side added nothing, there is nothing to do. If this side added nothing,
  the merge takes the other store as it is (keeping this one aside), unless forgetting says otherwise.
- **A true merge** appends the other side's new records as imported copies (each names the record and hash it came
  from), then compares every claim three ways: base, ours, theirs. A claim only one side changed takes that change. A
  claim both sides changed is a conflict: the other side's version comes in beside ours, and the next dream settles
  it with both sides' evidence. Claims only the other side has are added.
- **Forgetting.** By default forgetting wins: a shared record either side forgot is forgotten here. With
  ``forgetting="conflict"`` the merge stops and lists those records instead. ``forgetting="keep"`` takes the whole
  text back from the side that still has it, checked against the record's chain hash.
- **One pass.** Imported records, claim changes and a pass entry (author "merge") that says what came from where are
  one atomic commit. Merging the same fork again later brings only what is new since.

Dream before you merge, as git wants a clean working tree: records no dream has reviewed on either side are left for
the next dream here, which then reviews the imported ones again. Wiki pages and action words aren't merged yet; the
report counts them.
"""
from __future__ import annotations

import datetime as dt
import os
import pathlib
import shutil
import tempfile

FORGETTING = ("wins", "conflict", "keep")
ORIGIN = "merge"
MAX_LISTED = 12


class MergeError(ValueError):
    pass


def _S():
    from somni import store
    return store


def _prefix(ours, theirs, key):
    n = 0
    for a, b in zip(ours, theirs):
        if key(a) != key(b):
            break
        n += 1
    return n


def _successor(theirs, claim_id):
    """Follow a retired claim's replacements on the other side: (final claim id or None, changed?, retract event)."""
    claims, changed, seen = theirs.claims, False, set()
    current = claims[claim_id]
    while current["state"] in ("superseded", "past") and current.get("superseded_by") and current["id"] not in seen:
        seen.add(current["id"])
        changed = changed or current["state"] == "past"
        current = claims[current["superseded_by"]]
    if current["state"] == "active":
        return current["id"], changed, None
    if current["state"] == "retracted":
        event = next(e for e in reversed(theirs.note_log) if e.get("op") == "retract" and e.get("claim_id") == current["id"])
        return None, changed, event
    return None, changed, None


def plan(ours, theirs, *, forgetting="wins", unrelated=False, label="fork"):
    """What merging `theirs` into `ours` would do. Both are loaded SomniMemory objects; nothing is written."""
    S = _S()
    if forgetting not in FORGETTING:
        raise MergeError(f"forgetting must be one of {', '.join(FORGETTING)}; got {forgetting!r}")
    if ours.subject_id != theirs.subject_id:
        raise MergeError(f"the other store's subject is {theirs.subject_id!r}, this one's {ours.subject_id!r}: "
                         "one store holds one subject's memory")
    k = _prefix(ours.transcript, theirs.transcript, S._record_hash)
    nb = _prefix(ours.note_log, theirs.note_log, S.sha)
    if not k and not nb and ours.transcript and theirs.transcript and not unrelated:
        raise MergeError("the two stores share no history: they didn't grow from one snapshot. Pass --unrelated to "
                         "merge them anyway (every record and claim of the other comes in as new)")
    report = {"from": label, "base": {"records": k, "note_events": nb}, "forgetting": forgetting}

    # Forgetting in the shared records.
    theirs_forgot = [i for i in range(k) if "forgotten" in theirs.transcript[i] and "forgotten" not in ours.transcript[i]]
    ours_forgot = [i for i in range(k) if "forgotten" in ours.transcript[i] and "forgotten" not in theirs.transcript[i]]
    if forgetting == "conflict" and (theirs_forgot or ours_forgot):
        listed = [ours.transcript[i]["id"] for i in (theirs_forgot + ours_forgot)[:MAX_LISTED]]
        raise MergeError(f"forgetting conflict: the other side forgot {len(theirs_forgot)} shared record(s) this one "
                         f"keeps, and this side forgot {len(ours_forgot)} it keeps ({', '.join(listed)}"
                         f"{' …' if len(theirs_forgot) + len(ours_forgot) > MAX_LISTED else ''}). Merge again with "
                         "--forgetting wins (forget them here too) or --forgetting keep (take their whole text back)")
    to_forget = theirs_forgot if forgetting == "wins" else []
    to_restore = ours_forgot if forgetting == "keep" else []
    report["forgetting_changes"] = {"forget": len(to_forget), "restore": len(to_restore)}

    # Earlier merges of this fork: which of its records and claims are already here.
    fork = S._record_hash(theirs.transcript[k]) if len(theirs.transcript) > k else None
    known_records = {r["imported"]["sha256"]: r["id"] for r in ours.transcript
                     if (r.get("imported") or {}).get("from") == ORIGIN}
    claim_map, settled = {}, set()
    for dream in ours.dreams:
        earlier = dream.get("merge") or {}
        if earlier.get("fork") and earlier.get("fork") == fork:
            claim_map.update(earlier.get("claim_map", {}))
            settled.update(earlier.get("settled", []))

    ours_dreams_prefix = _prefix(ours.dreams, theirs.dreams, S.sha)
    ours_added_nothing = (len(ours.transcript) == k and len(ours.note_log) == nb and len(ours.dreams) == ours_dreams_prefix
                          and len(ours.wiki_log) == _prefix(ours.wiki_log, theirs.wiki_log, S.sha))
    # Taking the other store whole must not undo what this side forgot (wins), or what it kept (keep).
    if ours_added_nothing and not (ours_forgot if forgetting == "wins" else theirs_forgot):
        report["result"] = "fast-forward"
        return report, None
    report["result"] = "merged"

    # Records: the other side's new ones, as imported copies.
    seq, head = ours.state["transcript_count"], ours.state["transcript_head"]
    mapping, imported = {}, []
    their_reviewed = theirs.dreams[-1]["transcript_count"] if theirs.dreams else 0
    reviewed_imports = 0
    for record in theirs.transcript[k:]:
        digest = S._record_hash(record)
        if digest in known_records:
            mapping[record["id"]] = known_records[digest]
            continue
        seq += 1
        copy = {key: value for key, value in record.items()
                if key not in ("schema", "seq", "id", "prev_sha256", "forgotten", "imported")}
        copy = {"schema": S.SCHEMA, "seq": seq, "id": f"t{seq:06d}", **copy, "prev_sha256": head,
                "imported": {"from": ORIGIN, "fork": label, "ref": record["id"], "sha256": digest,
                             **({"forgotten": True} if "forgotten" in record else {})}}
        head = S.sha(copy)
        imported.append(copy)
        mapping[record["id"]] = copy["id"]
        known_records[digest] = copy["id"]
        if record["seq"] <= their_reviewed:
            reviewed_imports += 1
    report["records"] = {"imported": len(imported), "already_here": len(theirs.transcript) - k - len(imported)}

    def ref(value):
        number = S._ref_number(value)
        if number <= k:
            return value
        if value not in mapping:
            raise MergeError(f"the other store's {value} has no copy here")
        return mapping[value]

    def evidence(items, status=None):
        """Their citations, pointing at the copies here. A shared record this side forgot may have lost a quote they
        cite: the citation keeps its ref, and a verified claim left without proof becomes uncertain."""
        out = []
        for item in items:
            shared = S._ref_number(item["ref"]) <= k  # a copy made here keeps the text it was quoted from
            item = {**item, "ref": ref(item["ref"])}
            if shared and item.get("quote") and item["quote"] not in ours._records_by_id[item["ref"]]["text"]:
                item.pop("quote")
            out.append(item)
        if status == "verified" and not all(i.get("quote") or i.get("image_sha256") for i in out):
            status = "uncertain"
        return out, status

    # Claims: three ways.
    base = S.SomniMemory._replay_notes(ours.note_log[:nb], ours._records_by_id) if nb else {}
    known = {cid: cid for cid in base}
    known.update(claim_map)
    operations, conflicts, new_map, new_settled = [], [], {}, []
    standing = sum(1 for c in ours.claims.values() if c["state"] == "active" and c.get("standing"))
    taken = set()  # their claims already accounted for (a successor handled with its ancestor)
    next_id = ours.state["next_claim"]

    def claim_fields(claim, links_to):
        nonlocal standing
        cited, status = evidence(claim["evidence"], claim["status"])
        fields = {"text": claim["text"], "status": status, "salience": claim.get("salience", 3), "evidence": cited}
        if claim.get("gist"):
            fields["gist"] = claim["gist"]
        links = [links_to[link] for link in claim.get("links", []) if link in links_to]
        if links:
            fields["links"] = links
        if claim.get("standing"):
            if standing < S.MAX_STANDING:
                fields["standing"] = True
                standing += 1
            else:
                conflicts.append({"kind": "standing_limit", "theirs": claim["id"],
                                  "detail": "a standing preference came in without standing: the limit is reached"})
        return fields

    def add(op):
        nonlocal next_id
        operations.append(op)
        if op["op"] in ("add", "correct", "update"):
            new_id = f"n{next_id:06d}"
            next_id += 1
            return new_id
        return None

    links_to = dict(known)
    for their_id in sorted(theirs.claims):
        if their_id not in known or their_id in settled:
            continue
        our_id = known[their_id]
        mine, yours = ours.claims.get(our_id), theirs.claims[their_id]
        if mine is None:
            continue
        before = base[their_id]["state"] if their_id in base else "active"
        if yours["state"] == before:
            if yours["state"] == mine["state"] == "active" and yours.get("salience", 3) > max(
                    mine.get("salience", 3), base.get(their_id, {}).get("salience", 0)):
                add({"op": "promote", "claim_id": our_id, "salience": yours["salience"]})
            continue
        successor, changed, retraction = _successor(theirs, their_id)
        chain = [their_id]
        while theirs.claims[chain[-1]].get("superseded_by") and chain[-1] != successor:
            chain.append(theirs.claims[chain[-1]]["superseded_by"])
        taken.update(chain)
        new_settled += chain
        if mine["state"] == before:  # only they changed it: take their change
            if successor is not None:
                fields = claim_fields(theirs.claims[successor], links_to)
                newest = max(S._ref_number(item["ref"]) for item in mine["evidence"])
                newer = any(S._ref_number(item["ref"]) > newest for item in fields["evidence"])
                # An update keeps the old claim as true of its time, and needs newer evidence to say until when.
                new = add({"op": "update" if changed and newer else "correct", "claim_id": our_id, **fields})
                new_map[successor] = links_to[successor] = new
            elif retraction is not None:
                add({"op": "retract", "claim_id": our_id, "reason": f"merged from {label}: {retraction['reason']}",
                     "evidence": evidence(retraction["evidence"])[0]})
            continue
        # Both changed it.
        if successor is None and mine["state"] == "retracted":
            continue  # both dropped it
        entry = {"kind": "both_changed", "ours": our_id, "theirs": their_id,
                 "detail": f"this side {mine['state']} {our_id}; the other side "
                           + (f"replaced it with {successor}" if successor else "retracted it"
                              + (f" ({retraction['reason']})" if retraction else ""))}
        if successor is not None:
            new = add({"op": "add", **claim_fields(theirs.claims[successor], links_to)})
            new_map[successor] = links_to[successor] = entry["imported_as"] = new
        conflicts.append(entry)

    for their_id in sorted(theirs.claims):
        claim = theirs.claims[their_id]
        if their_id in known or their_id in taken or their_id in new_map or claim["state"] != "active":
            continue
        new = add({"op": "add", **claim_fields(claim, links_to)})
        new_map[their_id] = links_to[their_id] = new

    counts = {}
    for op in operations:
        counts[op["op"]] = counts.get(op["op"], 0) + 1
    report["claims"] = counts
    report["conflicts"] = conflicts
    their_pages = len(theirs.wiki_log) - _prefix(ours.wiki_log, theirs.wiki_log, S.sha)
    their_words = sum(len(d.get("actions", ())) for d in theirs.dreams[ours_dreams_prefix:])
    if their_pages or their_words:
        report["not_merged"] = {"page_operations": their_pages, "action_word_changes": their_words}

    # Where the merge pass is bound: what both sides' dreams have reviewed.
    ours_last = ours.dreams[-1]["transcript_count"] if ours.dreams else 0
    ours_clean = ours_last == ours.state["transcript_count"]
    bound = ours.state["transcript_count"] + reviewed_imports if ours_clean else ours_last
    if report["result"] == "merged" and not (imported or operations or to_forget or to_restore):
        report["result"] = "up to date"
    report["review"] = {"through": bound, "waiting": seq - bound}
    if seq - bound > len(imported) - reviewed_imports:
        report["review"]["hint"] = ("this store had records no dream reviewed, so the next dream reviews the imported "
                                    "ones too: dream before you merge")
    details = {"imported": imported, "count": seq, "head": head, "bound": bound,
               "operations": operations, "forget": to_forget, "restore": to_restore,
               "pass": {"from": label, "fork": fork, "base_records": k, "base_note_events": nb,
                        "their_heads": {key: theirs.state.get(key) for key in
                                        ("transcript_count", "transcript_head", "notes_head", "dreams_head")},
                        "claim_map": {**claim_map, **new_map}, "settled": sorted(settled | set(new_settled)),
                        **({"conflicts": conflicts} if conflicts else {}),
                        **({"not_merged": report["not_merged"]} if "not_merged" in report else {})}}
    return report, details


def apply(ours, theirs, details):
    """Commit a planned merge into `ours`, under its lock. Returns the merge pass."""
    S = _S()
    with ours._locked():
        ours._load_locked(force=True)
        if ours.state["phase"] != "closed":
            raise MergeError("a session is recording into this store; close Pi before merging")
        if ours.state["transcript_count"] != details["count"] - len(details["imported"]):
            raise MergeError("the store changed while the merge was planned; merge again")
        imported, encoded = [], []
        for copy in details["imported"]:
            copy = dict(copy)
            for descriptor in copy.get("images", ()):  # stored by content: the copy's descriptors stay as they were
                if ours._store_image(descriptor["mime_type"], theirs._read_image(descriptor)) is None:
                    raise MergeError("this store's image space is full; the merge needs room for the other side's images")
            imported.append(copy)
            encoded.append(S._encode_log_record(copy))
        real_transcript, real_records = ours.transcript, ours._records_by_id
        ours.transcript = real_transcript + imported
        ours._records_by_id = {**real_records, **{r["id"]: r for r in imported}}
        try:
            prepared, next_claim, rejected = ours._prepare_dream(details["operations"], False, None, None)
            forget = {}
            if details["forget"]:
                quotes = ours._cited_quotes(prepared)
                for index in details["forget"]:
                    record = ours.transcript[index]
                    their_stub = theirs.transcript[index]
                    when = S._parse_time(their_stub["forgotten"].get("at")) or S._now()
                    stub = ours._stub(record, quotes.get(record["id"], ()), when)
                    if stub is not None:
                        forget[record["seq"]] = stub
            bound = details["bound"]
            bound_head = S._record_hash(ours.transcript[bound - 1]) if bound else S.ZERO_HASH
        finally:
            ours.transcript, ours._records_by_id = real_transcript, real_records
        proposal = S.canonical({"merge": details["pass"], "operations": details["operations"]}).encode("utf-8")
        ours._commit_dream(proposal, prepared, next_claim, rejected, author="merge",
                           forgot={str(seq): S.sha(stub) for seq, stub in forget.items()},
                           merge={"records": encoded, "count": details["count"], "head": details["head"],
                                  "bound": bound, "bound_head": bound_head, "report": details["pass"]})
        ours._load_locked(force=True)
        if forget:
            ours._write_forgotten(forget)
        if details["restore"]:
            whole = {}
            for index in details["restore"]:
                record = theirs.transcript[index]
                if S.sha(record) != S._record_hash(ours.transcript[index]):
                    raise MergeError(f"{record['id']} doesn't match the record this store forgot")
                whole[record["seq"]] = record
            ours._rewrite_transcript(whole)
        if ours.pages or os.path.isdir(ours.store / S.VAULT_DIR):
            ours.write_vault()
        return ours.dreams[-1]


def open_other(source: pathlib.Path, subject: str | None, scratch: pathlib.Path):
    """The other side: a snapshot file (restored into `scratch`) or a store directory. Returns a loaded memory."""
    S = _S()
    source = pathlib.Path(source)
    if source.is_file():
        from somni import snapshot as N
        restored = N.restore(source, scratch, S.SomniMemory)
        return S.SomniMemory(scratch, restored["subject"]).load()
    if (source / S.STORE_DIR).is_dir():
        manifest = S._read_json(source / S.STORE_DIR / "manifest.json")
        return S.SomniMemory(source, subject or manifest["subject_id"]).load()
    raise MergeError(f"{source} is neither a snapshot file nor a directory holding {S.STORE_DIR}/")


def merge(workspace: pathlib.Path, subject: str, source: pathlib.Path, *, forgetting="wins", unrelated=False,
          dry_run=False, label=None):
    """Merge `source` (a snapshot or a store directory) into the store in `workspace`. Returns the report."""
    S = _S()
    workspace = pathlib.Path(workspace)
    label = label or pathlib.Path(source).name.split(".")[0] or "fork"
    scratch = pathlib.Path(tempfile.mkdtemp(prefix=".somni-merge-", dir=workspace if workspace.is_dir() else None))
    try:
        theirs = open_other(source, None, scratch)
        if not (workspace / S.STORE_DIR).is_dir():
            if dry_run:
                return {"from": label, "result": "fast-forward", "into": "an empty directory"}
            return {"from": label, "result": "fast-forward", **_fast_forward(workspace, theirs, scratch, label)}
        ours = S.SomniMemory(workspace, subject).load()
        report, details = plan(ours, theirs, forgetting=forgetting, unrelated=unrelated, label=label)
        if dry_run or report["result"] == "up to date":
            return {**report, **({"dry_run": True} if dry_run else {})}
        if details is None:
            return {**report, **_fast_forward(workspace, theirs, scratch, label)}
        merged = apply(ours, theirs, details)
        return {**report, "pass": f"d{merged['seq']:06d}"}
    except S.MemoryError as exc:
        raise MergeError(str(exc)) from None
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _fast_forward(workspace, theirs, scratch, label):
    """This side added nothing since the base: take the other store whole, keeping this one aside."""
    from somni import snapshot as N
    S = _S()
    if theirs.state["phase"] != "closed":
        raise MergeError("the other store has a session recording; take a snapshot of it instead")
    out = scratch / "fast-forward.tar.gz"
    N.snapshot(theirs, out)
    target = workspace / S.STORE_DIR
    if target.is_dir():
        ours = S.SomniMemory(workspace, theirs.subject_id).load()
        if ours.state["phase"] != "closed":
            raise MergeError("a session is recording into this store; close Pi before merging")
    result = N.restore(out, workspace, S.SomniMemory, replace=True)
    return {"fast_forward": True, **({"kept_aside": result["kept_aside"]} if "kept_aside" in result else {}),
            "at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
