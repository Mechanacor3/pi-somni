#!/usr/bin/env python3
"""Somni: bounded, agent-owned layered memory with offline "dreaming".

Follows the hierarchy in "The components of agent memory" (L. Martin, Anthropic
Engineering, 2026-11-06, https://www.anthropic.com/research/components-of-agent-memory).

Layers (all under `<workspace>/somni-v1/`):

  transcript.jsonl  canonical, hash-chained, append-only record of what the agent
                    saw (observations, tool results) and said (its own output).
  notes.jsonl       agent-authored claims.  Every claim cites transcript records;
                    `verified` claims quote an exact excerpt of each cited record.
  wiki.jsonl        pages that gather what is known about one subject (the essay's
                    "representations"): markdown written by dreaming, each citing the
                    active claims it rests on and linking (with [[slug]] wikilinks)
                    only to pages that exist.
  vault/            a readable Obsidian-style rendering of the wiki, regenerated after
                    every dream: one note per page with frontmatter and backlinks,
                    index.md, and claims.md with each claim's exact evidence quotes.
                    It is a view; edits there are not read back.
  dreams.jsonl      offline consolidation passes that add, correct (supersede) or
                    retract claims and write or drop pages, each bound to the
                    transcript head it reviewed.

`inject` renders a small index for the start of a session (page titles, then
claims by salience); `recall` ranks claims and `recall_pages` ranks pages for a query.  The store is stdlib-only, makes no
network calls, and reads nothing outside its workspace.  A trusted adapter must
append only text the agent itself already saw or produced.
"""

import argparse
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
import fcntl
import hashlib
import json
import math
import os
import pathlib
import re
import stat
import sys
import tempfile
from time import monotonic


STORE_DIR = "somni-v1"
SCHEMA = 1
ZERO_HASH = "0" * 64
MAX_RECORD_BYTES = 8_192
MAX_NOTE_BYTES = 4_096
MAX_INPUT_BYTES = 65_536
MAX_CLAIMS = 512
MAX_TRANSCRIPT_RECORDS = 4_096
MAX_DREAM_PASSES = 512
MAX_LOG_BYTES = 16 * 1024 * 1024
MAX_INJECTION_BYTES = 8_192
# Per-store overrides (set once at create time, recorded in the manifest).
LIMIT_KEYS = {"max_transcript_records": 65_536, "max_claims": 4_096, "max_record_bytes": 32_768}
DEFAULT_INJECTION_BYTES = 4_096
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
CLAIM_ID = re.compile(r"^n[0-9]{6}$")
PAGE_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAX_PAGES = 512
MAX_PAGE_BYTES = 6_144
WIKILINK = re.compile(r"\[\[([a-z0-9][a-z0-9-]{0,63})(?:\|[^\]\n]{1,160})?\]\]")
CLAIM_CITE = re.compile(r"\[(n[0-9]{6})\]")
VAULT_DIR = "vault"
EVIDENCE_ID = re.compile(r"^t[0-9]{6}$")
SOURCES = {"ordinary_observation", "tool_result", "agent_output"}
STATUSES = {"verified", "uncertain"}
FILES = ("manifest.json", "state.json", "transcript.jsonl", "notes.jsonl", "dreams.jsonl", "memory.lock")
VERIFIED_FILES = (*FILES, "wiki.jsonl", ".transaction.json")
VERIFY_AFTER_ACCESSES = 128
VERIFY_AFTER_SECONDS = 60
LOG_STATE = {
    "transcript.jsonl": ("transcript_count", "transcript_head"),
    "notes.jsonl": ("note_events", "notes_head"),
    "dreams.jsonl": ("dream_count", "dreams_head"),
    "wiki.jsonl": ("wiki_events", "wiki_head"),
}


class SomniError(ValueError):
    pass


@dataclass
class _VerifiedStore:
    files: dict
    checked_at: float
    accesses: int = 0


MemoryError = SomniError  # backwards-compatible name


def canonical(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def sha(value):
    data = value if isinstance(value, bytes) else canonical(value).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def token_estimate(value):
    size = len(value.encode("utf-8") if isinstance(value, str) else value)
    return (size + 3) // 4


def _require_claim_capacity(existing_count, limit=None):
    if existing_count >= (MAX_CLAIMS if limit is None else limit):
        raise MemoryError("claim limit reached; seal the bounded attempt")


def _validate_limits(limits):
    if not isinstance(limits, dict) or set(limits) - set(LIMIT_KEYS):
        raise MemoryError(f"limits may only set {sorted(LIMIT_KEYS)}")
    for key, value in limits.items():
        if type(value) is not int or not 1 <= value <= LIMIT_KEYS[key]:
            raise MemoryError(f"limit {key} must be an integer in 1..{LIMIT_KEYS[key]}")
    return dict(sorted(limits.items()))


def _safe_id(value, label):
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise MemoryError(f"invalid {label}")
    return value


def _no_symlink(path):
    current = pathlib.Path(path)
    while True:
        if os.path.lexists(current) and current.is_symlink():
            raise MemoryError(f"symlink is not allowed: {current}")
        if current.parent == current:
            return
        current = current.parent


def _read_regular(path, limit=MAX_INPUT_BYTES):
    _no_symlink(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size > limit:
            raise MemoryError(f"input must be a regular file no larger than {limit} bytes")
        data = os.read(fd, limit + 1)
        if len(data) > limit:
            raise MemoryError("input exceeds byte limit")
        return data
    finally:
        os.close(fd)


def _atomic_json(path, value, *, create=False):
    encoded = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if create:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        return
    fd, temporary = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _sync_directory(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _encode_log_record(value):
    return (canonical(value) + "\n").encode("utf-8")


def _preflight_append(path, encoded_records):
    encoded_size = sum(len(encoded) for encoded in encoded_records)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0))
    try:
        size = os.fstat(fd).st_size
        if size + encoded_size > MAX_LOG_BYTES:
            raise MemoryError(f"{path.name} reached its {MAX_LOG_BYTES}-byte bound")
        return size
    finally:
        os.close(fd)


def _append_encoded(path, encoded):
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0))
    try:
        view = memoryview(encoded)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short append to memory log")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _append(path, value):
    encoded = _encode_log_record(value)
    _preflight_append(path, [encoded])
    _append_encoded(path, encoded)


def _read_json(path):
    try:
        return json.loads(_read_regular(path).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, OSError) as exc:
        raise MemoryError(f"invalid JSON file {path.name}: {exc}") from exc


def _read_log(path, committed_size=None):
    data = _read_regular(path, limit=MAX_LOG_BYTES)
    if committed_size is not None:
        if committed_size > len(data) or (committed_size and data[committed_size - 1] != 10):
            raise MemoryError(f"invalid transaction offset for {path.name}")
        data = data[:committed_size]
    records = []
    for number, raw in enumerate(data.splitlines(), 1):
        try:
            text = raw.decode("utf-8")
            value = json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MemoryError(f"invalid {path.name} line {number}") from exc
        if canonical(value).encode("utf-8") != raw:
            raise MemoryError(f"non-canonical {path.name} line {number}")
        records.append(value)
    return records


class SomniMemory:
    def __init__(self, workspace, subject_id, *, scheduled_verification=False):
        raw = pathlib.Path(workspace)
        _no_symlink(raw)
        self.workspace = raw.resolve(strict=True)
        self.store = self.workspace / STORE_DIR
        _no_symlink(self.store)
        self.subject_id = _safe_id(subject_id, "subject id")
        self._scheduled_verification = scheduled_verification
        self._verified = None

    @classmethod
    def create(cls, workspace, subject_id, lineage_id, attempt_id, limits=None):
        memory = cls(workspace, subject_id)
        limits = _validate_limits(limits) if limits else None
        if memory.store.exists() or os.path.lexists(memory.store):
            raise MemoryError(f"create-only store already exists: {memory.store}")
        memory.store.mkdir(mode=0o700)
        manifest = {
            "schema": SCHEMA,
            "condition": "somni-v1",
            "subject_id": _safe_id(subject_id, "subject id"),
            "lineage_id": _safe_id(lineage_id, "lineage id"),
            "attempt_id": _safe_id(attempt_id, "attempt id"),
        }
        if limits:
            manifest["limits"] = limits
        state = {
            "schema": SCHEMA, "phase": "closed", "transcript_count": 0,
            "transcript_head": ZERO_HASH, "note_events": 0, "notes_head": ZERO_HASH,
            "next_claim": 1, "dream_count": 0, "dreams_head": ZERO_HASH,
        }
        _atomic_json(memory.store / "manifest.json", manifest, create=True)
        _atomic_json(memory.store / "state.json", state, create=True)
        for name in ("transcript.jsonl", "notes.jsonl", "dreams.jsonl", "memory.lock"):
            fd = os.open(memory.store / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
        memory.load()
        return memory

    def _locked(self):
        class Lock:
            def __enter__(inner):
                path = self.store / "memory.lock"
                if path.is_symlink() or not path.is_file():
                    raise MemoryError("missing or unsafe store file: memory.lock")
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
                try:
                    if not stat.S_ISREG(os.fstat(fd).st_mode):
                        raise MemoryError("unsafe store file: memory.lock")
                    fcntl.flock(fd, fcntl.LOCK_EX)
                except (OSError, MemoryError):
                    os.close(fd)
                    raise
                inner.fd = fd
                return inner

            def __exit__(inner, *_):
                fcntl.flock(inner.fd, fcntl.LOCK_UN)
                os.close(inner.fd)
        return Lock()

    def _limit(self, key):
        limits = (getattr(self, "manifest", None) or {}).get("limits") or {}
        if key in limits:
            return limits[key]
        return {"max_transcript_records": MAX_TRANSCRIPT_RECORDS, "max_claims": MAX_CLAIMS,
                "max_record_bytes": MAX_RECORD_BYTES}[key]

    def load(self):
        if not self.store.is_dir():
            raise MemoryError("somni memory is not initialized")
        _no_symlink(self.store)
        with self._locked():
            return self._load_locked()

    def _fingerprints(self, names):
        _no_symlink(self.store)
        files = {}
        for name in names:
            try:
                info = (self.store / name).lstat()
            except FileNotFoundError:
                files[name] = None
            else:
                files[name] = (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        return files

    def _load_locked(self, *, force=False):
        if not self._scheduled_verification:
            return self._verify_locked()
        current = self._fingerprints(VERIFIED_FILES)
        snapshot = self._verified
        if (not force and snapshot is not None and current == snapshot.files
                and snapshot.accesses < VERIFY_AFTER_ACCESSES
                and monotonic() - snapshot.checked_at < VERIFY_AFTER_SECONDS):
            snapshot.accesses += 1
            return self

        self._verified = None
        self._verify_locked()
        # Recovery can truncate logs and clear the journal. The next access takes
        # a fresh snapshot of the recovered files.
        if current[".transaction.json"] is None:
            if self._fingerprints(VERIFIED_FILES) != current:
                raise MemoryError("store changed during verification")
            self._verified = _VerifiedStore(current, monotonic())
        return self

    def _verify_locked(self):
        for name in FILES:
            path = self.store / name
            if not os.path.lexists(path) or path.is_symlink() or not path.is_file():
                raise MemoryError(f"missing or unsafe store file: {name}")
        manifest = _read_json(self.store / "manifest.json")
        if manifest.get("schema") != SCHEMA or manifest.get("subject_id") != self.subject_id:
            raise MemoryError("subject identity does not match this memory store")
        _validate_limits(manifest.get("limits") or {})
        self.manifest = manifest
        state = _read_json(self.store / "state.json")
        journal = self.store / ".transaction.json"
        if os.path.lexists(journal):
            self._recover_transaction(journal, state)
        transcript = _read_log(self.store / "transcript.jsonl")
        notes = _read_log(self.store / "notes.jsonl")
        dreams = _read_log(self.store / "dreams.jsonl")
        self._check_chain(transcript, "transcript", state["transcript_count"], state["transcript_head"])
        self._check_chain(notes, "notes", state["note_events"], state["notes_head"])
        self._check_chain(dreams, "dreams", state["dream_count"], state["dreams_head"])
        wiki_path = self.store / "wiki.jsonl"
        wiki = _read_log(wiki_path) if os.path.lexists(wiki_path) else []
        self._check_chain(wiki, "wiki", state.get("wiki_events", 0), state.get("wiki_head", ZERO_HASH))
        claims = self._replay_notes(notes, transcript, self._limit("max_claims"))
        expected_next = max([int(key[1:]) for key in claims] + [0]) + 1
        if state.get("next_claim") != expected_next or state.get("phase") not in {"open", "closed"}:
            raise MemoryError("state counters are inconsistent")
        self.manifest, self.state = manifest, state
        self.transcript, self.note_log, self.dreams, self.claims = transcript, notes, dreams, claims
        self.wiki_log = wiki
        self.pages = self._replay_wiki(wiki, claims)
        if os.path.lexists(journal):
            self._clear_transaction(journal)
        return self

    def _recover_transaction(self, journal, state):
        pending = _read_json(journal)
        if not isinstance(pending, dict) or set(pending) != {"before", "after", "offsets"}:
            raise MemoryError("invalid memory transaction")
        offsets = pending["offsets"]
        if not isinstance(offsets, dict) or not offsets or any(
            name not in LOG_STATE or type(size) is not int or not 0 <= size <= MAX_LOG_BYTES
            for name, size in offsets.items()
        ):
            raise MemoryError("invalid memory transaction offsets")
        if state == pending["after"]:
            return  # state commit succeeded; load verifies the new logs before cleanup
        if state != pending["before"]:
            raise MemoryError("memory transaction does not match state")
        # Verify all committed prefixes before removing any interrupted append.
        for name, size in offsets.items():
            count, head = LOG_STATE[name]
            prefix = _read_log(self.store / name, size)
            self._check_chain(prefix, name, state.get(count, 0), state.get(head, ZERO_HASH))
        for name, size in offsets.items():
            fd = os.open(self.store / name, os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                os.ftruncate(fd, size)
                os.fsync(fd)
            finally:
                os.close(fd)

    def _clear_transaction(self, journal):
        journal.unlink()
        _sync_directory(self.store)

    @staticmethod
    def _check_chain(records, label, count, head):
        if len(records) != count:
            raise MemoryError(f"{label} length does not match state")
        previous = ZERO_HASH
        for index, record in enumerate(records, 1):
            if record.get("seq") != index or record.get("prev_sha256") != previous:
                raise MemoryError(f"broken {label} chain at record {index}")
            previous = sha(record)
        if previous != head:
            raise MemoryError(f"{label} head does not match state")

    @staticmethod
    def _evidence_map(transcript):
        return {record["id"]: record for record in transcript}

    @classmethod
    def _validate_evidence(cls, evidence, transcript, status=None):
        if not isinstance(evidence, list) or not evidence or len(evidence) > 16:
            raise MemoryError("every claim needs 1-16 evidence references")
        known = cls._evidence_map(transcript)
        normalized = []
        seen = set()
        for item in evidence:
            if not isinstance(item, dict) or set(item) - {"ref", "quote"}:
                raise MemoryError("evidence must contain only ref and optional quote")
            ref, quote = item.get("ref"), item.get("quote")
            if not isinstance(ref, str) or not EVIDENCE_ID.fullmatch(ref) or ref not in known or ref in seen:
                raise MemoryError("evidence reference is missing, duplicated, or unknown")
            if quote is not None and (not isinstance(quote, str) or not quote or quote not in known[ref]["text"]):
                raise MemoryError("evidence quote must occur exactly in the referenced transcript")
            if status == "verified" and quote is None:
                raise MemoryError("verified claims require an exact quote for every evidence reference")
            normalized.append({"ref": ref, **({"quote": quote} if quote is not None else {})})
            seen.add(ref)
        return normalized

    @classmethod
    def _replay_notes(cls, events, transcript, max_claims=None):
        claims = {}
        for event in events:
            op = event.get("op")
            if op == "add":
                _require_claim_capacity(len(claims), max_claims)
                claim = event.get("claim")
                expected_id = f"n{len(claims) + 1:06d}"
                if not isinstance(claim, dict) or claim.get("id") != expected_id:
                    raise MemoryError("invalid or duplicate claim event")
                cls._validate_claim(claim, transcript, claims)
                claims[claim["id"]] = {**claim, "state": "active"}
                replaced = claim.get("replaces")
                if replaced:
                    if replaced not in claims or claims[replaced]["state"] != "active":
                        raise MemoryError("correction replaces a non-active claim")
                    claims[replaced]["state"] = "superseded"
                    claims[replaced]["superseded_by"] = claim["id"]
            elif op == "promote":
                claim_id, salience = event.get("claim_id"), event.get("salience")
                if claim_id not in claims or claims[claim_id]["state"] != "active" or type(salience) is not int or not 1 <= salience <= 5:
                    raise MemoryError("promotion targets an invalid claim or salience")
                claims[claim_id]["salience"] = salience
            elif op == "retract":
                claim_id = event.get("claim_id")
                if (not isinstance(claim_id, str) or not CLAIM_ID.fullmatch(claim_id)
                        or claim_id not in claims or claims[claim_id]["state"] != "active"):
                    raise MemoryError("retraction targets an invalid or non-active claim")
                if not isinstance(event.get("reason"), str) or not event["reason"].strip():
                    raise MemoryError("retraction needs a reason")
                cls._validate_evidence(event.get("evidence"), transcript)
                claims[claim_id]["state"] = "retracted"
                claims[claim_id]["retraction_reason"] = event["reason"]
            else:
                raise MemoryError("unknown note event operation")
        return claims

    @staticmethod
    def _replay_wiki(events, claims):
        pages = {}
        for event in events:
            if event.get("op") == "page":
                page = event.get("page") or {}
                if not isinstance(page, dict) or not PAGE_SLUG.fullmatch(str(page.get("slug"))):
                    raise MemoryError("invalid wiki page event")
                for ref in page.get("claims", []):
                    if ref not in claims:
                        raise MemoryError("wiki page cites an unknown claim")
                pages[page["slug"]] = page
            elif event.get("op") == "drop_page":
                if event.get("slug") not in pages:
                    raise MemoryError("wiki drop targets an unknown page")
                del pages[event["slug"]]
            else:
                raise MemoryError("unknown wiki event")
        return pages

    @staticmethod
    def _validate_page(op, simulated_claims, simulated_pages):
        allowed = {"op", "slug", "title", "body", "claims", "links"}
        if set(op) - allowed or not {"slug", "title", "body", "claims"} <= set(op):
            raise MemoryError("page operation needs slug, title, body, claims (and optional links)")
        slug, title, body = op["slug"], op["title"], op["body"]
        if not isinstance(slug, str) or not PAGE_SLUG.fullmatch(slug):
            raise MemoryError("page slug must match [a-z0-9][a-z0-9-]{0,63}")
        if not isinstance(title, str) or not title.strip() or len(title) > 160:
            raise MemoryError("page title must be 1-160 characters")
        if not isinstance(body, str) or not body.strip() or len(body.encode("utf-8")) > MAX_PAGE_BYTES:
            raise MemoryError(f"page body must be 1-{MAX_PAGE_BYTES} bytes")
        refs = op["claims"]
        if not isinstance(refs, list) or not all(isinstance(r, str) for r in refs):
            raise MemoryError("page claims must be a list of claim ids")
        refs = list(dict.fromkeys(refs + CLAIM_CITE.findall(body)))  # inline [nXXXXXX] citations count too
        if len(refs) > 64:
            raise MemoryError("a page may cite at most 64 claims")
        for ref in refs:
            if not isinstance(ref, str) or ref not in simulated_claims or simulated_claims[ref]["state"] != "active":
                raise MemoryError(f"page cites {ref!r}, which is not an active claim")
        links = op.get("links", [])
        if not isinstance(links, list) or not all(isinstance(x, str) for x in links):
            raise MemoryError("page links must be a list of slugs")
        links = sorted(set(links) | set(WIKILINK.findall(body)))
        if len(links) > 32:
            raise MemoryError("a page may link to at most 32 other pages")
        if not refs and not links:
            raise MemoryError("a page must cite at least one claim, or link other pages (a navigation page)")
        for link in links:
            if link == slug or link not in simulated_pages:
                raise MemoryError(f"page links to {link!r}, which does not exist (write linked pages first)")
        if slug not in simulated_pages and len(simulated_pages) >= MAX_PAGES:
            raise MemoryError("page limit reached")
        version = simulated_pages[slug]["version"] + 1 if slug in simulated_pages else 1
        return {"slug": slug, "title": title.strip(), "body": body.strip(), "claims": refs, "links": links, "version": version}

    def write_vault(self):
        """Render the wiki as an Obsidian-style vault (deterministic; a view, never read back)."""
        vault = self.store / VAULT_DIR
        _no_symlink(vault)
        vault.mkdir(mode=0o700, exist_ok=True)
        claims = self.claims
        backlinks = {}
        for page in self.pages.values():
            for link in page["links"]:
                backlinks.setdefault(link, []).append(page["slug"])

        def body_md(text, refs):
            text = CLAIM_CITE.sub(lambda m: f"[[claims#{m.group(1)}|{m.group(1)}]]" if m.group(1) in refs else m.group(0), text)
            return text

        files = {}
        for slug, page in sorted(self.pages.items()):
            fm = ["---", f"title: {json.dumps(page['title'], ensure_ascii=False)}", f"version: {page['version']}",
                  "claims: [" + ", ".join(page["claims"]) + "]", "tags: [somni]", "---", ""]
            lines = fm + [f"# {page['title']}", "", body_md(page["body"], set(page["claims"])), ""]
            if page["links"]:
                lines += ["## See also", ""] + [f"- [[{l}|{self.pages[l]['title']}]]" for l in page["links"] if l in self.pages] + [""]
            if backlinks.get(slug):
                lines += ["## Backlinks", ""] + [f"- [[{b}|{self.pages[b]['title']}]]" for b in sorted(backlinks[slug])] + [""]
            lines += ["## Evidence", ""]
            for ref in page["claims"]:
                c = claims.get(ref)
                state = "" if c and c["state"] == "active" else f" (**{c['state'] if c else 'missing'}**)"
                lines.append(f"- [[claims#{ref}|{ref}]]{state}: {c['text'] if c else ''}")
            files[f"{slug}.md"] = "\n".join(lines) + "\n"
        index = ["---", "title: Index", "tags: [somni, index]", "---", "", "# Index", "",
                 f"{len(self.pages)} pages, {sum(c['state'] == 'active' for c in claims.values())} active claims. "
                 "Pages are written by dreaming; every statement traces to [[claims]] and from there to exact transcript quotes.", ""]
        index += [f"- [[{slug}|{page['title']}]]" for slug, page in sorted(self.pages.items(), key=lambda kv: kv[1]["title"].casefold())]
        files["index.md"] = "\n".join(index) + "\n"
        transcript = {r["id"]: r for r in self.transcript}
        cl = ["---", "title: Claims", "tags: [somni, claims]", "---", "", "# Claims", "",
              "Each claim cites transcript records; `verified` means every cited record contains the quoted excerpt.", ""]
        cited_by = {}
        for slug, page in self.pages.items():
            for ref in page["claims"]:
                cited_by.setdefault(ref, []).append(slug)
        for cid, c in sorted(claims.items()):
            cl += [f"## {cid}", "", f"{c['text']}", "",
                   f"- state: {c['state']}; status: {c['status']}; salience: {c['salience']}"]
            if c.get("replaces"):
                cl.append(f"- supersedes [[claims#{c['replaces']}|{c['replaces']}]]")
            if c.get("merged_from"):
                parents = ", ".join(f"[[claims#{ref}|{ref}]]" for ref in c["merged_from"])
                cl.extend([f"- merged from {parents}", f"- provenance SHA-256: `{c['provenance_sha256']}`"])
            if cited_by.get(cid):
                cl.append("- used by: " + ", ".join(f"[[{s}]]" for s in sorted(cited_by[cid])))
            for ev in c["evidence"]:
                rec = transcript.get(ev["ref"], {})
                quote = ev.get("quote")
                cl.append(f"- {ev['ref']} ({rec.get('source', '?')}): " + (f"> {quote}" if quote else f"{rec.get('text', '')[:200]}"))
            cl.append("")
        files["claims.md"] = "\n".join(cl) + "\n"
        for existing in vault.glob("*.md"):
            if existing.name not in files:
                existing.unlink()
        for name, text in files.items():
            target = vault / name
            if target.exists() and target.read_text(encoding="utf-8") == text:
                continue
            fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=vault)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
            os.replace(tmp, target)
        return sorted(files)

    def stale_pages(self):
        """Pages citing claims that are no longer active (input for gap-filling)."""
        return {slug: [c for c in page["claims"] if self.claims[c]["state"] != "active"]
                for slug, page in self.pages.items() if any(self.claims[c]["state"] != "active" for c in page["claims"])}

    @classmethod
    def _validate_claim(cls, claim, transcript, existing):
        required = {"id", "text", "status", "salience", "evidence", "links"}
        merge_fields = {"merged_from", "provenance_sha256"}
        if (not isinstance(claim, dict) or not required.issubset(claim)
                or set(claim) - (required | {"replaces"} | merge_fields)):
            raise MemoryError("claim has invalid fields")
        text = claim["text"]
        if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > MAX_NOTE_BYTES:
            raise MemoryError("claim text is empty or too large")
        if (not isinstance(claim["status"], str) or claim["status"] not in STATUSES
                or type(claim["salience"]) is not int or not 1 <= claim["salience"] <= 5):
            raise MemoryError("claim status or salience is invalid")
        cls._validate_evidence(claim["evidence"], transcript, claim["status"])
        if not isinstance(claim["links"], list) or len(claim["links"]) > 16:
            raise MemoryError("claim links are invalid")
        for link in claim["links"]:
            if not isinstance(link, str) or not CLAIM_ID.fullmatch(link) or link not in existing:
                raise MemoryError("claim link is invalid")
        if "replaces" in claim:
            replaced = claim["replaces"]
            if (not isinstance(replaced, str) or not CLAIM_ID.fullmatch(replaced)
                    or replaced not in existing or existing[replaced]["state"] != "active"):
                raise MemoryError("replacement target is invalid or non-active")
        if claim.keys() & merge_fields:
            parents = cls._merge_parents(claim.get("merged_from"), existing)
            if ("replaces" in claim or claim["merged_from"] != parents or claim["links"] != parents
                    or claim["evidence"] != cls._merge_evidence(parents, existing)):
                raise MemoryError("merged claim must use its canonical parents and evidence")
            if claim.get("provenance_sha256") != cls._merge_digest(claim, transcript, existing):
                raise MemoryError("merged claim provenance hash does not match its parents and content")

    @staticmethod
    def _merge_parents(claim_ids, existing):
        if (not isinstance(claim_ids, list) or not 2 <= len(claim_ids) <= 16
                or any(not isinstance(ref, str) or not CLAIM_ID.fullmatch(ref) for ref in claim_ids)
                or len(set(claim_ids)) != len(claim_ids)):
            raise MemoryError("merge requires 2-16 distinct claim IDs")
        if any(ref not in existing or existing[ref]["state"] != "active" for ref in claim_ids):
            raise MemoryError("merge requires active parent claims")
        return sorted(claim_ids)

    @staticmethod
    def _merge_evidence(parents, existing):
        evidence = {}
        for ref in parents:
            for citation in existing[ref]["evidence"]:
                previous = evidence.get(citation["ref"])
                if previous is None or (citation.get("quote") and not previous.get("quote")):
                    evidence[citation["ref"]] = citation
        return [evidence[ref] for ref in sorted(evidence)]

    @classmethod
    def _merge_digest(cls, claim, transcript, existing):
        known = cls._evidence_map(transcript)

        def parent_hash(parent):
            if "provenance_sha256" in parent:
                return parent["provenance_sha256"]
            evidence = [
                {**ref, "record_sha256": sha(known[ref["ref"]])}
                for ref in sorted(parent["evidence"], key=lambda ref: ref["ref"])
            ]
            return sha({
                "schema": SCHEMA, "kind": "claim", "text": parent["text"], "status": parent["status"],
                "evidence": evidence, "links": sorted(parent["links"]), "replaces": parent.get("replaces"),
            })

        return sha({
            "schema": SCHEMA, "kind": "merge", "text": claim["text"], "status": claim["status"],
            "parents": [[ref, parent_hash(existing[ref])] for ref in claim["merged_from"]],
        })

    def _save_state(self):
        _atomic_json(self.store / "state.json", self.state)

    def _accept_local_write(self, snapshot, names):
        if snapshot is not None:
            snapshot.files.update(self._fingerprints(names))
            self._verified = snapshot

    def _commit_logs(self, entries, next_state):
        journal = self.store / ".transaction.json"
        offsets = {name: _preflight_append(self.store / name, values) for name, values in entries.items()}
        snapshot, self._verified = self._verified, None
        _atomic_json(journal, {"before": self.state, "after": next_state, "offsets": offsets})
        for name, values in entries.items():
            _append_encoded(self.store / name, b"".join(values))
        self.state = next_state
        self._save_state()
        self._clear_transaction(journal)
        self._accept_local_write(snapshot, ("state.json", *entries))

    def set_phase(self, phase, *, allow_same=False):
        if not isinstance(phase, str) or phase not in {"open", "closed"}:
            raise MemoryError("phase must be open or closed")
        with self._locked():
            self._load_locked()
            if self.state["phase"] == phase:
                if allow_same:
                    return
                raise MemoryError(f"session is already {phase}")
            snapshot, self._verified = self._verified, None
            self.state["phase"] = phase
            self._save_state()
            self._accept_local_write(snapshot, ("state.json",))

    def record(self, source, text):
        return self.record_many([(source, text)])[0]

    def record_many(self, items):
        """Append several (source, text) records under one lock; all or nothing."""
        items = list(items)
        if not items:
            return []
        for source, text in items:
            if not isinstance(source, str) or source not in SOURCES:
                raise MemoryError("invalid transcript source")
        with self._locked():
            self._load_locked()
            max_bytes = self._limit("max_record_bytes")
            if any(not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > max_bytes
                   for _, text in items):
                raise MemoryError("transcript text is empty or too large")
            if self.state["phase"] != "open":
                raise MemoryError("recording requires an open subject session")
            if self.state["transcript_count"] + len(items) > self._limit("max_transcript_records"):
                raise MemoryError("transcript record limit reached; seal the bounded attempt")
            seq, head, encoded, ids, records = self.state["transcript_count"], self.state["transcript_head"], [], [], []
            for source, text in items:
                seq += 1
                record = {"schema": SCHEMA, "seq": seq, "id": f"t{seq:06d}", "source": source,
                          "text": text, "prev_sha256": head}
                head = sha(record)
                encoded.append(_encode_log_record(record))
                ids.append(record["id"])
                records.append(record)
            self._commit_logs({"transcript.jsonl": encoded}, {
                **self.state, "transcript_count": seq, "transcript_head": head,
            })
            self.transcript.extend(records)
        return ids

    def add_note(self, text, status, evidence, salience=3, links=None):
        with self._locked():
            self._load_locked()
            if self.state["phase"] != "open":
                raise MemoryError("agent notes require an open subject session")
            return self._append_claim(text, status, evidence, salience, links or [], None)

    def _append_claim(self, text, status, evidence, salience, links, replaces):
        _require_claim_capacity(len(self.claims), self._limit("max_claims"))
        claim_id = f"n{self.state['next_claim']:06d}"
        claim = {"id": claim_id, "text": text, "status": status, "salience": salience,
                 "evidence": evidence, "links": links}
        if replaces is not None:
            claim["replaces"] = replaces
        self._validate_claim(claim, self.transcript, self.claims)
        event = {"schema": SCHEMA, "seq": self.state["note_events"] + 1, "op": "add", "claim": claim,
                 "prev_sha256": self.state["notes_head"]}
        self._commit_logs({"notes.jsonl": [_encode_log_record(event)]}, {
            **self.state, "note_events": event["seq"], "notes_head": sha(event),
            "next_claim": self.state["next_claim"] + 1,
        })
        self.note_log.append(event)
        self.claims[claim_id] = {**claim, "state": "active"}
        if replaces is not None:
            self.claims[replaces]["state"] = "superseded"
            self.claims[replaces]["superseded_by"] = claim_id
        return claim_id

    def dream(self, proposal, partial=False):
        """Apply one offline consolidation pass.

        By default the pass is all-or-nothing.  With ``partial=True`` an operation
        that fails validation is skipped and listed in the pass record's
        ``rejected`` field (index and reason), so a model-authored pass keeps its
        valid operations and the rejection itself becomes part of the trace.  At
        least one operation must still be accepted.
        """
        if not isinstance(proposal, dict) or proposal.get("schema") != SCHEMA or set(proposal) != {"schema", "operations"}:
            raise MemoryError("dream proposal must contain schema and operations")
        operations = proposal["operations"]
        if not isinstance(operations, list) or not 1 <= len(operations) <= 32:
            raise MemoryError("dream proposal needs 1-32 operations")
        proposal_bytes = canonical(proposal).encode("utf-8")
        if len(proposal_bytes) > MAX_INPUT_BYTES:
            raise MemoryError("dream proposal exceeds byte limit")
        with self._locked():
            self._load_locked()
            if self.state["phase"] != "closed":
                raise MemoryError("dreaming is offline-only; close the subject session first")
            if self.state["dream_count"] >= MAX_DREAM_PASSES:
                raise MemoryError("dream pass limit reached; seal the bounded attempt")
            # Validate all operations against a simulation before appending any event.
            simulated = {key: dict(value) for key, value in self.claims.items()}
            self._sim_pages = {key: dict(value) for key, value in self.pages.items()}
            prepared = []
            rejected = []
            next_claim = self.state["next_claim"]
            for index, operation in enumerate(operations):
                try:
                    next_claim = self._prepare_dream_op(operation, simulated, prepared, next_claim)
                except MemoryError as exc:
                    if not partial or "claim limit reached" in str(exc):
                        raise
                    rejected.append({"index": index, "reason": str(exc)[:300]})
            if not prepared:
                raise MemoryError("dream pass has no valid operations: " + "; ".join(r["reason"] for r in rejected)[:600])
            self._commit_dream(proposal_bytes, prepared, next_claim, rejected)
            self._load_locked(force=True)
            if self.pages or os.path.isdir(self.store / VAULT_DIR):
                self.write_vault()
            return self.dreams[-1]

    def _prepare_dream_op(self, operation, simulated, prepared, next_claim):
        if not isinstance(operation, dict):
            raise MemoryError("dream operation must be an object")
        op = operation.get("op")
        if not isinstance(op, str):
            raise MemoryError("dream operation type is invalid")
        if op == "page":
            page = self._validate_page(operation, simulated, self._sim_pages)
            prepared.append(("page", page))
            self._sim_pages[page["slug"]] = page
            return next_claim
        if op == "promote":
            if set(operation) != {"op", "claim_id", "salience"}:
                raise MemoryError("promote needs exactly claim_id and salience")
            claim_id, salience = operation["claim_id"], operation["salience"]
            if claim_id not in simulated or simulated[claim_id]["state"] != "active":
                raise MemoryError("promote targets an invalid or non-active claim")
            if type(salience) is not int or not simulated[claim_id]["salience"] < salience <= 5:
                raise MemoryError("promote must raise salience (up to 5)")
            prepared.append(("promote", {"claim_id": claim_id, "salience": salience}))
            simulated[claim_id]["salience"] = salience
            return next_claim
        if op == "drop_page":
            if set(operation) != {"op", "slug", "reason"} or operation.get("slug") not in self._sim_pages:
                raise MemoryError("drop_page needs an existing slug and a reason")
            if not isinstance(operation["reason"], str) or not operation["reason"].strip():
                raise MemoryError("drop_page needs a reason")
            if any(operation["slug"] in p["links"] for k, p in self._sim_pages.items() if k != operation["slug"]):
                raise MemoryError("other pages still link to this page; relink them first")
            prepared.append(("drop_page", {"slug": operation["slug"], "reason": operation["reason"]}))
            del self._sim_pages[operation["slug"]]
            return next_claim
        if op in {"add", "correct", "merge"}:
            _require_claim_capacity(len(simulated), self._limit("max_claims"))
            allowed = {"op", "text", "status", "salience"}
            allowed |= {"claim_ids"} if op == "merge" else {"evidence", "links"}
            if op == "correct":
                allowed.add("claim_id")
            if set(operation) - allowed or (op == "correct" and "claim_id" not in operation):
                raise MemoryError("dream claim operation has unknown fields")
            replaces = operation.get("claim_id") if op == "correct" else None
            if op == "correct" and (not isinstance(replaces, str) or not CLAIM_ID.fullmatch(replaces)
                    or replaces not in simulated or simulated[replaces]["state"] != "active"):
                raise MemoryError("correction targets an invalid or non-active claim")
            claim_id = f"n{next_claim:06d}"
            claim = {"id": claim_id, "text": operation.get("text"), "status": operation.get("status"),
                     "salience": operation.get("salience", 3), "evidence": operation.get("evidence"),
                     "links": operation.get("links", [])}
            if replaces is not None:
                claim["replaces"] = replaces
            if op == "merge":
                parents = self._merge_parents(operation.get("claim_ids"), simulated)
                claim.update(merged_from=parents, links=parents, evidence=self._merge_evidence(parents, simulated))
                claim["status"] = operation.get("status", "uncertain")
                claim["provenance_sha256"] = self._merge_digest(claim, self.transcript, simulated)
            self._validate_claim(claim, self.transcript, simulated)
            prepared.append(("add", claim))
            simulated[claim_id] = {**claim, "state": "active"}
            if replaces:
                simulated[replaces]["state"] = "superseded"
            next_claim += 1
        elif op == "retract":
            if set(operation) != {"op", "claim_id", "reason", "evidence"}:
                raise MemoryError("retraction has invalid fields")
            claim_id = operation["claim_id"]
            if (not isinstance(claim_id, str) or not CLAIM_ID.fullmatch(claim_id)
                    or claim_id not in simulated or simulated[claim_id]["state"] != "active"):
                raise MemoryError("retraction targets an invalid or non-active claim")
            if not isinstance(operation["reason"], str) or not operation["reason"].strip():
                raise MemoryError("retraction needs a reason")
            evidence = self._validate_evidence(operation["evidence"], self.transcript)
            prepared.append(("retract", {"claim_id": claim_id, "reason": operation["reason"], "evidence": evidence}))
            simulated[claim_id]["state"] = "retracted"
        else:
            raise MemoryError("unknown dream operation")
        return next_claim

    def _commit_dream(self, proposal_bytes, prepared, next_claim, rejected):
        wiki_events = []
        wiki_seq, wiki_head = self.state.get("wiki_events", 0), self.state.get("wiki_head", ZERO_HASH)
        for op, body in prepared:
            if op in ("page", "drop_page"):
                wiki_seq += 1
                event = {"schema": SCHEMA, "seq": wiki_seq, "op": op, "prev_sha256": wiki_head,
                         **({"page": body} if op == "page" else body)}
                wiki_events.append(event)
                wiki_head = sha(event)
        page_ops = len(prepared) - len([1 for op, _ in prepared if op not in ("page", "drop_page")])
        prepared = [(op, body) for op, body in prepared if op not in ("page", "drop_page")]
        note_events = []
        note_seq = self.state["note_events"]
        notes_head = self.state["notes_head"]
        for op, body in prepared:
            note_seq += 1
            event = {"schema": SCHEMA, "seq": note_seq, "op": op,
                     "prev_sha256": notes_head, **({"claim": body} if op == "add" else body)}
            note_events.append(event)
            notes_head = sha(event)
        pass_number = self.state["dream_count"] + 1
        dream = {"schema": SCHEMA, "seq": pass_number, "transcript_count": self.state["transcript_count"],
                 "transcript_head": self.state["transcript_head"], "operation_count": len(prepared),
                 "proposal_sha256": sha(proposal_bytes), "input_bytes": len(proposal_bytes),
                 "input_tokens_estimate": token_estimate(proposal_bytes), "prev_sha256": self.state["dreams_head"]}
        if wiki_events:
            dream["page_operations"] = len(wiki_events)
            dream["operation_count"] += page_ops
        if rejected:
            dream["rejected"] = rejected
        note_bytes = [_encode_log_record(event) for event in note_events]
        wiki_bytes = [_encode_log_record(event) for event in wiki_events]
        entries = {}
        if note_bytes:
            entries["notes.jsonl"] = note_bytes
        if wiki_bytes:
            wiki_path = self.store / "wiki.jsonl"
            if not os.path.lexists(wiki_path):
                os.close(os.open(wiki_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
            entries["wiki.jsonl"] = wiki_bytes
        entries["dreams.jsonl"] = [_encode_log_record(dream)]
        self._commit_logs(entries, {
            **self.state, "note_events": note_seq, "notes_head": notes_head, "next_claim": next_claim,
            "dream_count": pass_number, "dreams_head": sha(dream),
            **({"wiki_events": wiki_seq, "wiki_head": wiki_head} if wiki_bytes else {}),
        })

    def search(self, query, limit=20):
        if not isinstance(query, str) or not query.strip() or len(query) > 256 or not 1 <= limit <= 50:
            raise MemoryError("invalid search query or limit")
        needle = query.casefold()
        return [record for record in self.transcript if needle in record["text"].casefold()][:limit]

    def recall(self, query, limit=3):
        """Rank active claims for ``query``: lexical BM25 over claim text, then salience.

        Deterministic and local.  Returns claim dicts (with ``score``), best first.
        """
        if not isinstance(query, str) or not query.strip() or len(query) > 2_000 or not 1 <= limit <= 50:
            raise MemoryError("invalid recall query or limit")
        active = [claim for claim in self.claims.values() if claim["state"] == "active"]
        if not active:
            return []
        docs = [_terms(claim["text"]) for claim in active]
        scored = [(score * (1 + 0.1 * claim["salience"]), claim) for score, claim in _bm25(docs, active, query)]
        scored.sort(key=lambda item: (-item[0], -item[1]["salience"], item[1]["id"]))
        return [{**claim, "score": round(score, 4)} for score, claim in scored[:limit]]

    def recall_pages(self, query, limit=3):
        """Rank wiki pages for ``query`` with BM25 over title and body (title counted twice)."""
        if not isinstance(query, str) or not query.strip() or not 1 <= limit <= 50:
            raise MemoryError("invalid recall query or limit")
        stale = self.stale_pages()
        pages = [page for slug, page in self.pages.items() if slug not in stale]
        if not pages:
            return []
        docs = [_terms(f"{p['title']} {p['title']} {p['body']}") for p in pages]
        return [{**page, "score": round(score, 4)} for score, page in _bm25(docs, pages, query)[:limit]]

    def inject(self, max_bytes=DEFAULT_INJECTION_BYTES):
        if type(max_bytes) is not int or not 256 <= max_bytes <= MAX_INJECTION_BYTES:
            raise MemoryError(f"injection bound must be 256-{MAX_INJECTION_BYTES} bytes")
        header = ("# Subject memory index\n"
                  "Claims are subject-authored interpretations, not guaranteed truths. "
                  "Use evidence IDs to inspect the immutable subject-visible transcript.\n")
        lines = [header]
        stale = self.stale_pages()
        for slug, page in sorted(self.pages.items()):
            if slug in stale:
                continue
            line = f"- page `{page['slug']}`: {page['title']}\n"
            if len(("".join(lines) + line).encode("utf-8")) > max_bytes // 2:
                break
            lines.append(line)
        page_lines = len(lines) - 1
        active = [claim for claim in self.claims.values() if claim["state"] == "active"]
        active.sort(key=lambda claim: (-claim["salience"], claim["id"]))
        for claim in active:
            refs = ",".join(item["ref"] for item in claim["evidence"])
            line = f"- {claim['id']} [{claim['status']}; salience={claim['salience']}; evidence={refs}] {claim['text']}\n"
            if len(("".join(lines) + line).encode("utf-8")) > max_bytes:
                break
            lines.append(line)
        omitted = len(active) - (len(lines) - 1 - page_lines)
        if omitted:
            suffix = f"- … {omitted} additional active claims omitted; search/report on demand.\n"
            while len(lines) > 1 + page_lines and len(("".join(lines) + suffix).encode("utf-8")) > max_bytes:
                lines.pop()
                omitted += 1
                suffix = f"- … {omitted} additional active claims omitted; search/report on demand.\n"
            if len(("".join(lines) + suffix).encode("utf-8")) <= max_bytes:
                lines.append(suffix)
        result = "".join(lines)
        if len(result.encode("utf-8")) > max_bytes:
            raise MemoryError("fixed injection header exceeds requested bound")
        return result

    def metrics(self):
        transcript_bytes = sum(len((canonical(item) + "\n").encode("utf-8")) for item in self.transcript)
        note_bytes = sum(len((canonical(item) + "\n").encode("utf-8")) for item in self.note_log)
        dream_input = sum(item["input_bytes"] for item in self.dreams)
        return {
            "transcript_records": len(self.transcript), "transcript_bytes": transcript_bytes,
            "transcript_tokens_estimate": token_estimate(b"x" * transcript_bytes),
            "note_events": len(self.note_log), "active_claims": sum(c["state"] == "active" for c in self.claims.values()),
            "note_bytes": note_bytes, "note_tokens_estimate": token_estimate(b"x" * note_bytes),
            "dream_passes": len(self.dreams), "dream_input_bytes": dream_input,
            "wiki_pages": len(self.pages), "wiki_links": sum(len(p["links"]) for p in self.pages.values()),
            "dream_input_tokens_estimate": sum(item["input_tokens_estimate"] for item in self.dreams),
            "local_memory_processing_usd": 0,
        }

    def report(self):
        metrics = self.metrics()
        lines = [
            "# Somni memory report", "",
            f"- Subject: `{self.manifest['subject_id']}`",
            f"- Lineage / attempt: `{self.manifest['lineage_id']}` / `{self.manifest['attempt_id']}`",
            f"- Transcript: {metrics['transcript_records']} records, {metrics['transcript_bytes']} bytes, ~{metrics['transcript_tokens_estimate']} tokens",
            f"- Notes: {metrics['active_claims']} active claims / {metrics['note_events']} events, {metrics['note_bytes']} bytes, ~{metrics['note_tokens_estimate']} tokens",
            f"- Dreaming: {metrics['dream_passes']} passes, {metrics['dream_input_bytes']} input bytes, ~{metrics['dream_input_tokens_estimate']} input tokens",
            "- Local memory processing cost: $0 (external model-review cost is not known to this store)", "",
            "## Current claims", "",
        ]
        for claim in sorted(self.claims.values(), key=lambda item: item["id"]):
            refs = ", ".join(item["ref"] for item in claim["evidence"])
            lines.append(f"- **{claim['id']}** [{claim['state']}; {claim['status']}; evidence: {refs}] {claim['text']}")
            if claim.get("retraction_reason"):
                lines.append(f"  Retraction: {claim['retraction_reason']}")
        if not self.claims:
            lines.append("- No claims.")
        if self.pages:
            lines.extend(["", "## Wiki pages", ""])
            for page in sorted(self.pages.values(), key=lambda p: p["slug"]):
                lines.append(f"### {page['title']} (`{page['slug']}`, v{page['version']}; claims: {', '.join(page['claims'])})")
                lines.extend(["", page["body"], ""])
        lines.extend(["", "## Evidence transcript", ""])
        for record in self.transcript:
            lines.append(f"- **{record['id']}** `{record['source']}` — {record['text']}")
        if not self.transcript:
            lines.append("- No transcript records.")
        lines.extend(["", "Verified means exact cited excerpts were present in subject-visible records; it is not semantic proof.", ""])
        return "\n".join(lines)


_STOP = frozenset("""a an and are as at be by can do does for from has have if in into is it its of on or
that the their then this to was were when which while will with you your i me my we our they them""".split())


def _bm25(docs, items, query):
    if not docs:
        return []
    q_terms = set(_terms(query))
    avg = (sum(len(d) for d in docs) / len(docs)) or 1.0
    df = {}
    for d in docs:
        for term in set(d):
            df[term] = df.get(term, 0) + 1
    scored = []
    for item, d in zip(items, docs):
        score = 0.0
        for term in q_terms:
            tf = d.count(term)
            if tf:
                idf = math.log(1 + (len(docs) - df[term] + 0.5) / (df[term] + 0.5))
                score += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * len(d) / avg))
        if score > 0:
            scored.append((score, item))
    scored.sort(key=lambda x: -x[0])
    return scored


_SUFFIXES = ("ations", "ation", "ings", "ing", "edly", "ed", "ies", "es", "s")


def _stem(term):
    """Tiny suffix stripper so cancel/cancelled/cancellation(s) and refund/refunds/refunded match."""
    for suffix in _SUFFIXES:
        if term.endswith(suffix) and len(term) - len(suffix) >= 3:
            term = term[: -len(suffix)] + ("y" if suffix == "ies" else "")
            break
    if len(term) > 3 and term[-1] == term[-2] and term[-1] not in "aeiouslz":
        term = term[:-1]
    if len(term) > 3 and term.endswith("ll"):
        term = term[:-1]
    return term


def _terms(text):
    return [_stem(t) for t in re.findall(r"[a-z0-9]+", text.casefold()) if t not in _STOP and len(t) > 1]


LayeredMemory = SomniMemory  # backwards-compatible name


@contextmanager
def _recording_lease(memory):
    """The child keeps the writer lock until Pi closes its input pipe."""
    lease = memory.workspace / ".somni-session.lock"
    fd = os.open(lease, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise MemoryError("the Somni session lock must be a regular file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)


def _handle_request(memory, method, args, *, writable):
    if method in {"phase", "record", "note", "dream", "recent", "wiki"} and not writable:
        raise MemoryError("Somni memory changes require the session that owns recording")
    if method == "phase":
        memory.set_phase(args["phase"], allow_same=True)
    elif method == "record":
        index_bytes = args.get("index_bytes")
        _validate_index_bound(index_bytes)
        ref = memory.record(args["source"], args["text"])
        return ref if index_bytes is None else memory.inject(index_bytes)
    elif method == "note":
        if len(canonical(args).encode("utf-8")) > MAX_INPUT_BYTES:
            raise MemoryError("note input exceeds byte limit")
        return memory.add_note(args["text"], args["status"], args["evidence"], args["salience"])
    elif method == "dream":
        return memory.dream(json.loads(args["proposal_json"]), partial=True)
    elif method == "inject":
        return memory.load().inject(args["index_bytes"])
    elif method == "search":
        return memory.load().search(args["query"], args["limit"])
    elif method == "recall":
        memory.load()
        query, limit = args["query"], args["limit"]
        return memory.recall_pages(query, limit) + memory.recall(query, limit)
    elif method == "recent":
        return [{"id": r["id"], "source": r["source"], "text": r["text"][:1500]}
                for r in memory.load().transcript[-120:]]
    elif method == "wiki":
        # With python -I, explicitly add this package's root for the formatter.
        root = str(pathlib.Path(__file__).resolve().parent.parent)
        if root not in sys.path:
            sys.path.insert(0, root)
        from somni.dream import PAGE_GUIDE, _uncovered, render_pages

        memory.load()
        uncovered = _uncovered(memory)[:120]
        return {
            "pages": render_pages(memory), "guide": PAGE_GUIDE, "n": len(uncovered),
            "claims": "\n".join(f"[{claim['id']}] {claim['text']}" for claim in uncovered),
        }
    else:
        raise MemoryError(f"unknown Somni backend method: {method}")


def _serve(memory, record):
    def reply(request_id, **fields):
        print(json.dumps({"id": request_id, **fields}), flush=True)

    with _recording_lease(memory) if record else nullcontext(None) as owner:
        if owner is not False and not os.path.lexists(memory.store):
            SomniMemory.create(memory.workspace, memory.subject_id, "pi", "session")
        memory = SomniMemory(memory.workspace, memory.subject_id, scheduled_verification=True)
        if owner:
            memory.set_phase("open", allow_same=True)
        else:
            memory.load()
        try:
            reply(0, result={"writable": owner is not False})
            while line := sys.stdin.buffer.readline(512 * 1024 + 1):
                if len(line) > 512 * 1024:
                    raise MemoryError("Somni backend request exceeds byte limit")
                request_id = None
                try:
                    request = json.loads(line)
                    request_id = request["id"]
                    result = _handle_request(memory, request["method"], request["args"], writable=owner is not False)
                    reply(request_id, result=result)
                except (MemoryError, OSError, KeyError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                    reply(request_id, error=str(exc))
        finally:
            if owner:
                memory.set_phase("closed", allow_same=True)


def _validate_index_bound(bound):
    if bound is not None and (type(bound) is not int or not 256 <= bound <= MAX_INJECTION_BYTES):
        raise MemoryError(f"injection bound must be 256-{MAX_INJECTION_BYTES} bytes")


def _input_json(memory, value):
    if value == "-":
        data = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(data) > MAX_INPUT_BYTES:
            raise MemoryError("stdin exceeds byte limit")
    else:
        relative = pathlib.PurePath(value)
        if relative.is_absolute() or ".." in relative.parts:
            raise MemoryError("input path must be relative and confined to the attempt workspace")
        path = memory.workspace.joinpath(*relative.parts)
        _no_symlink(path)
        try:
            path.relative_to(memory.workspace)
        except ValueError as exc:
            raise MemoryError("input path escapes attempt workspace") from exc
        data = _read_regular(path)
    try:
        return json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MemoryError(f"invalid JSON input: {exc}") from exc


def parser():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--subject-id", required=True)
    commands = ap.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init"); init.add_argument("--lineage-id", required=True); init.add_argument("--attempt-id", required=True)
    commands.add_parser("session-open"); commands.add_parser("session-close")
    serve = commands.add_parser("serve", help=argparse.SUPPRESS)
    serve.add_argument("--record", action="store_true")
    record = commands.add_parser("record"); record.add_argument("--source", choices=sorted(SOURCES), required=True); record.add_argument("--text", required=True)
    record.add_argument("--inject-max-bytes", type=int, help="emit the updated index instead of the record ID")
    note = commands.add_parser("note"); note.add_argument("--input", required=True, help="claim JSON relative to workspace, or - for stdin")
    dream = commands.add_parser("dream"); dream.add_argument("--input", required=True, help="proposal JSON relative to workspace, or - for stdin")
    search = commands.add_parser("search"); search.add_argument("query"); search.add_argument("--limit", type=int, default=20)
    inject = commands.add_parser("inject"); inject.add_argument("--max-bytes", type=int, default=DEFAULT_INJECTION_BYTES)
    commands.add_parser("report"); commands.add_parser("metrics")
    return ap


def main():
    args = parser().parse_args()
    try:
        if args.command == "init":
            memory = SomniMemory.create(args.workspace, args.subject_id, args.lineage_id, args.attempt_id)
            print(f"LAYERED_MEMORY_INITIALIZED store={memory.store}")
            return 0
        memory = SomniMemory(args.workspace, args.subject_id)
        if args.command == "serve":
            _serve(memory, args.record)
        elif args.command == "session-open":
            memory.set_phase("open"); print("LAYERED_MEMORY_SESSION_OPEN")
        elif args.command == "session-close":
            memory.set_phase("closed"); print("LAYERED_MEMORY_SESSION_CLOSED")
        elif args.command == "record":
            bound = args.inject_max_bytes
            _validate_index_bound(bound)
            record = memory.record(args.source, args.text)
            if bound is None:
                print(record)
            else:
                sys.stdout.write(memory.inject(bound))
        elif args.command == "note":
            claim = _input_json(memory, args.input)
            if not isinstance(claim, dict) or set(claim) - {"text", "status", "evidence", "salience", "links"}:
                raise MemoryError("note input has invalid fields")
            print(memory.add_note(claim.get("text"), claim.get("status"), claim.get("evidence"), claim.get("salience", 3), claim.get("links", [])))
        elif args.command == "dream":
            print(canonical(memory.dream(_input_json(memory, args.input))))
        elif args.command == "search":
            print(json.dumps(memory.load().search(args.query, args.limit), ensure_ascii=False, indent=2, sort_keys=True))
        elif args.command == "inject":
            sys.stdout.write(memory.load().inject(args.max_bytes))
        elif args.command == "report":
            sys.stdout.write(memory.load().report())
        elif args.command == "metrics":
            print(json.dumps(memory.load().metrics(), indent=2, sort_keys=True))
        return 0
    except (MemoryError, OSError, KeyError) as exc:
        print(f"LAYERED_MEMORY_ERROR {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
