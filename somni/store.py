#!/usr/bin/env python3
"""Somni: bounded, agent-owned layered memory with offline "dreaming".

Follows the hierarchy in "The components of agent memory" (L. Martin, Anthropic
Engineering, 2026-11-06, https://www.anthropic.com/research/components-of-agent-memory).

Layers (all under `<workspace>/somni-v1/`):

  transcript.jsonl  canonical, hash-chained, append-only record of what the agent
                    saw (observations, tool results) and said (its own output).
  segments/         sealed, immutable older parts of the transcript, notes and dream
                    logs; each chain runs on across its segments into the live file.
  notes.jsonl       agent-authored claims.  Every claim cites transcript records;
                    `verified` claims cite exact excerpts or stored images.
  images/           bounded, content-addressed attachments for tool results.
  wiki.jsonl        pages that gather what is known about one subject (the essay's
                    "representations"): markdown written by dreaming, each citing the
                    active claims it rests on and linking (with [[slug]] wikilinks)
                    only to pages that exist at the end of the pass.
  vault/            a readable Obsidian-style rendering of the wiki, regenerated after
                    every dream: one note per page with frontmatter and backlinks,
                    index.md, and claims.md with each claim's exact evidence quotes.
                    It is a view; edits there are not read back.
  dreams.jsonl      offline consolidation passes that add, correct (supersede) or
                    retract claims and write or drop pages, each bound to the
                    transcript head it reviewed.

`inject` renders a small index of pages and claims ranked for the current prompt;
`recall` and `recall_pages` retrieve full claims and pages. The store is stdlib-only, makes no
network calls, and reads nothing outside its workspace.  A trusted adapter must
append only text the agent itself already saw or produced.
"""

import argparse
import base64
import binascii
import datetime as _dt
from collections import Counter
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
import fcntl
import hashlib
import io
import json
import math
import os
import pathlib
import posixpath
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
MAX_GIST_BYTES = 160
MAX_INPUT_BYTES = 65_536
MAX_CLAIMS = 512
MAX_TRANSCRIPT_RECORDS = 65_536
MAX_DREAM_PASSES = 4_096
MAX_LOG_BYTES = 16 * 1024 * 1024  # per file; long logs are sealed into segments well before this
SEGMENT_DIR = "segments"
SEGMENT_BYTES = 8 * 1024 * 1024  # seal a live log before an append takes it past this
SEGMENTED_LOGS = ("transcript.jsonl", "notes.jsonl", "dreams.jsonl")
MAX_INJECTION_BYTES = 8_192
DEFAULT_DREAM_RECORD_BYTES = 1_500
DEFAULT_DREAM_RECORDS = 120
DEFAULT_IMAGE_BYTES = 256 * 1024
MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_IMAGE_INPUT = 4 * 1024 * 1024  # raw before optional downscaling
MAX_RECORD_IMAGES = 4
MAX_RECORD_CALLS = 64  # an action word's nested-call timings on its result record
MAX_CALL_MS = 86_400_000
MAX_IMAGE_STORE_BYTES = 64 * 1024 * 1024
MAX_BACKEND_REQUEST = 512 * 1024 + MAX_RECORD_IMAGES * ((MAX_IMAGE_INPUT + 2) // 3 * 4)
IMAGE_SUFFIX = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}
SHA256 = re.compile(r"^[0-9a-f]{64}$")
# Per-store overrides (set once at create time, recorded in the manifest).
LIMIT_KEYS = {"max_transcript_records": 1_048_576, "max_claims": 4_096, "max_record_bytes": 32_768,
              "max_dream_passes": 1_048_576}
DEFAULT_INJECTION_BYTES = 4_096
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
CLAIM_ID = re.compile(r"^n[0-9]{6}$")
SLUG_SEGMENT = r"[a-z0-9][a-z0-9-]{0,63}"
MAX_PAGE_DEPTH = 3  # a slug may be a path: folder/subfolder/page
SLUG_PATH = rf"{SLUG_SEGMENT}(?:/{SLUG_SEGMENT}){{0,{MAX_PAGE_DEPTH - 1}}}"
PAGE_SLUG = re.compile(rf"^{SLUG_PATH}$")
RESERVED_PAGES = frozenset({"index", "claims"})  # vault files Somni renders itself
LINK_STYLES = ("markdown", "wikilink")
MAX_PAGES = 512
MAX_PAGE_BYTES = 6_144
WIKILINK = re.compile(rf"\[\[({SLUG_PATH})(?:\|([^\]\n]{{1,160}}))?\]\]")
CLAIM_CITE = re.compile(r"\[(n[0-9]{6})\]")
VAULT_DIR = "vault"
ACTIONS_DIR = "actions"
ACTION_OPS = frozenset({"define_action", "revise_action", "retire_action"})
MAX_ACTION_FILE_BYTES = 16384
ACTION_TOOLS_FILE = "action-tools.json"  # tool declarations seen in recording sessions; a cache, not evidence
FAILURES_FILE = "dream-failures.jsonl"  # dream attempts that committed nothing; diagnostics, not evidence
FAILURE_CAUSES = ("provider_error", "no_submission", "all_rejected", "invalid_submission", "budget_exhausted",
                  "timeout", "other")
FAILURE_KINDS = ("dream", "wiki", "daydream", "deepsleep")
MAX_FAILURES_BYTES = 1 << 20  # past this the oldest half is dropped; the counts say how many were dropped
def _somni_version():
    try:
        return json.loads((pathlib.Path(__file__).resolve().parent.parent / "package.json").read_text())["version"]
    except (OSError, ValueError, KeyError, TypeError):
        return "unknown"


SOMNI_VERSION = _somni_version()  # stamped on word runs, so a dream can tell runs of older code
CACHE_FILE = "dream-cache.json"  # evidence reads of an unfinished dream, so a retry on the same records needn't repeat them
MAX_CACHE_READS = 32
MAX_CACHE_TEXT = 8_192
MAX_ACTION_TOOLS = 256
MAX_ACTION_TOOL_SCHEMA_BYTES = 4096
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


# --- Time, strength and forgetting (see docs/memory-over-time.md) ---

DEFAULT_FORGET_DAYS = 30  # SOMNI_FORGET_AFTER_DAYS: reviewed records older than this keep only a stub
FORGET_KEEP_RECORDS = 120  # the newest records are never forgotten: the next dream's window (SOMNI_DREAM_RECORDS)
FORGET_GIST_CHARS = 160
FORGET_PER_PASS = 2000  # records one night may forget; the pass lists each stub's hash, so this bounds its size
FORGOTTEN_PREFIX = "[forgotten "
DEFAULT_DECAY = 0.5  # SOMNI_MEMORY_DECAY: ACT-R's base-level decay d (Anderson & Schooler, 1991)
FADING_BELOW = -0.5  # activation thresholds for the fuzzy strength labels
FAINT_BELOW = -1.5
CLAIM_MENTION = re.compile(r"(?<![A-Za-z0-9])n[0-9]{6}(?![0-9])")


def _parse_time(text):
    """An ISO 8601 time as an aware UTC datetime, or None."""
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        moment = _dt.datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment.replace(tzinfo=_dt.timezone.utc) if moment.tzinfo is None else moment.astimezone(_dt.timezone.utc)


def _now():
    """The current time. SOMNI_NOW fixes it, for tests and for benchmarks that simulate days."""
    value = os.environ.get("SOMNI_NOW", "")
    if value.strip():
        moment = _parse_time(value)
        if moment is None:
            raise SomniError("SOMNI_NOW must be an ISO 8601 time such as 2026-11-24T09:00:00Z")
        return moment
    return _dt.datetime.now(_dt.timezone.utc)


def _stamp(moment):
    return moment.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _env_number(name, default, low, high):
    try:
        value = float(os.environ.get(name, "") or default)
    except ValueError:
        return default
    return min(max(value, low), high) if math.isfinite(value) else default


def _forget_setting():
    """(days, problem) from SOMNI_FORGET_AFTER_DAYS. Forgetting can't be undone, so a value that isn't a number of
    days keeps every record (0) instead of falling back to the default, and says why."""
    text = os.environ.get("SOMNI_FORGET_AFTER_DAYS")
    if text is None or not text.strip():
        return DEFAULT_FORGET_DAYS, None
    try:
        days = float(text)
    except ValueError:
        days = math.nan
    if not math.isfinite(days) or days < 0:
        return 0, f"SOMNI_FORGET_AFTER_DAYS={text!r} is not a number of days; Somni keeps every record"
    return min(days, 36500), None


def _ref_number(ref):
    return int(ref[1:])


def _record_hash(record):
    """A record's link in its chain. A forgotten record keeps the hash of the record it replaced."""
    forgotten = record.get("forgotten")
    if forgotten is None:
        return sha(record)
    if (not isinstance(forgotten, dict) or not SHA256.fullmatch(str(forgotten.get("sha256")))
            or not str(record.get("text", "")).startswith(FORGOTTEN_PREFIX)):
        raise SomniError(f"invalid forgotten record {record.get('id')!r}")
    return forgotten["sha256"]


def _activation(ages_days, decay, salience=3):
    """ACT-R base-level activation: ln(sum of t^-d) over the ages of a memory's uses, plus a salience term.

    Each use (an observation, a dream writing the claim, a later mention of it) adds to the sum and fades as a
    power of its age. A claim used often and lately is strong; one seen once, long ago, is faint."""
    total = sum(max(age, 1 / 24) ** -decay for age in ages_days)
    return math.log(total) + 0.25 * (salience - 3) if total > 0 else FAINT_BELOW - 1


def _strength_level(activation):
    return "fresh" if activation >= FADING_BELOW else "fading" if activation >= FAINT_BELOW else "faint"


MIN_DIGEST_PREFIX = 6
DEFAULT_DREAM_IMAGES = 16
MAX_DREAM_IMAGES = 64


def _word_stamp(memory, args):
    """{source, somni} for a word's own result record: which version of the word, on which Somni, made this run."""
    A = _action_module()
    match = A.RECORD.match(args.get("text", "")) if isinstance(args.get("text"), str) else None
    word = memory.load().action_words().get(match[1]) if match else None
    return {"source": A.source_hash(word["source"]), "somni": SOMNI_VERSION} if word else None


def _word_result(text, match):
    """(args, result text, parsed result or None) of a recorded `TOOL name(args) -> result` line."""
    try:
        args, end = json.JSONDecoder().raw_decode(text, match.end())
        body = text[end + 5:] if text.startswith(") -> ", end) else text[end:]
    except ValueError:
        args, body = None, text[match.end():]
    try:
        value = json.loads(body)
    except ValueError:
        value = None
    return args, body, value


def _op_name(operation):
    return operation.get("op", "?") if isinstance(operation, dict) else type(operation).__name__


def _resolve_digest(record, ref, digest):
    """The full SHA-256 of the image on `record` that `digest` names: the digest itself or a unique prefix of it.

    Otherwise the error lists the digests the record actually has, so the caller can fix the citation in one step.
    """
    images = [image["sha256"] for image in record.get("images", [])]
    text = digest.strip().lower() if isinstance(digest, str) else ""
    if text in images:
        return text
    matches = [full for full in images if len(text) >= MIN_DIGEST_PREFIX and re.fullmatch(r"[0-9a-f]+", text)
               and full.startswith(text)]
    if len(matches) == 1:
        return matches[0]
    raw = digest if isinstance(digest, str) else repr(digest)
    shown = repr(raw if len(raw) <= 16 else raw[:16] + "...")
    if not images:
        raise MemoryError(f"{ref} has no stored images (image_sha256 {shown} cannot be cited there)")
    if len(matches) > 1:
        raise MemoryError(f"image_sha256 prefix {shown} matches several images on {ref}: {', '.join(matches)}")
    raise MemoryError(f"{ref} has no image {shown}; its image_sha256 values are: {', '.join(images)}")


def _call_timings(calls):
    """Validate an action word's nested-call timings: [{tool, ok, start_ms, ms}], in call order."""
    if not isinstance(calls, list) or len(calls) > MAX_RECORD_CALLS:
        raise MemoryError(f"call timings must be a list of at most {MAX_RECORD_CALLS}")
    out = []
    for call in calls:
        if (not isinstance(call, dict) or set(call) != {"tool", "ok", "start_ms", "ms"}
                or not isinstance(call["tool"], str) or not 0 < len(call["tool"]) <= 128 or type(call["ok"]) is not bool
                or any(type(call[k]) is not int or not 0 <= call[k] <= MAX_CALL_MS for k in ("start_ms", "ms"))):
            raise MemoryError("each call timing is {tool, ok, start_ms, ms} with non-negative integer milliseconds")
        out.append({"tool": call["tool"], "ok": call["ok"], "start_ms": call["start_ms"], "ms": call["ms"]})
    return out


def _record_context(run_label=None, tags=None):
    def label(value):
        return (isinstance(value, str) and value.strip() and value.isprintable()
                and len(value.encode("utf-8")) <= 96)

    if run_label is not None and not label(run_label):
        raise MemoryError("run label must be printable text up to 96 bytes")
    if tags is not None and (not isinstance(tags, list) or len(tags) > 8 or not all(map(label, tags))):
        raise MemoryError("tags must be an array of up to 8 printable strings, each up to 96 bytes")
    return {**({"run_label": run_label} if run_label else {}), **({"tags": sorted(set(tags))} if tags else {})}


def _clip_record(text, max_bytes, marker=b" [truncated]"):
    data = text.encode("utf-8")
    if len(data) <= max_bytes:
        return text
    if max_bytes <= len(marker):
        return "!" * max_bytes
    return data[:max_bytes - len(marker)].decode("utf-8", errors="ignore") + marker.decode()


def _image_type(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _image_bytes(image, max_bytes, resize=None):
    if not isinstance(image, dict):
        return None
    mime, encoded = image.get("mimeType"), image.get("data")
    if mime == "image/jpg":
        mime = "image/jpeg"
    if (not isinstance(mime, str) or mime not in IMAGE_SUFFIX or not isinstance(encoded, str)
            or len(encoded) > (MAX_IMAGE_INPUT + 2) // 3 * 4):
        return None
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        return None
    if len(data) > MAX_IMAGE_INPUT or _image_type(data) != mime:
        return None
    if resize:
        pillow, max_edge = resize
        try:
            with pillow.open(io.BytesIO(data)) as original:
                if max(original.size) > max_edge or len(data) > max_bytes:
                    if original.width * original.height > 24_000_000:
                        return None
                    preview = original.convert("RGB")
                    preview.thumbnail((max_edge, max_edge))
                    output = io.BytesIO()
                    preview.save(output, "JPEG", quality=80)
                    mime, data = "image/jpeg", output.getvalue()
        except (OSError, ValueError, SyntaxError, pillow.DecompressionBombError):
            return None
    return (mime, data) if len(data) <= max_bytes else None


def _require_viewed_images(evidence, viewed):
    if viewed is not None and any("image_sha256" in item and (item["ref"], item["image_sha256"]) not in viewed
                                  for item in evidence):
        raise MemoryError("view the cited image in this dream first with somni_image")


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


def _seeded_only(store):
    """True when the store folder holds nothing but a human-seeded actions/ folder."""
    store = pathlib.Path(store)
    if store.is_symlink() or not store.is_dir():
        return False
    entries = list(store.iterdir())
    return (len(entries) == 1 and entries[0].name == ACTIONS_DIR
            and entries[0].is_dir() and not entries[0].is_symlink())


def _write_text_atomic(path, text):
    _no_symlink(path)
    fd, temporary = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


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


def _write_bytes_durable(path, data):
    _no_symlink(path)
    fd, temporary = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
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


USAGE_KEYS = ("input", "output", "cacheRead", "cacheWrite", "totalTokens", "cost", "calls", "duration_ms")


def _usage(value):
    """Model usage reported with a dream pass: known non-negative numbers only."""
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - set(USAGE_KEYS):
        raise MemoryError(f"usage may only report {', '.join(USAGE_KEYS)}")
    out = {}
    for key, number in value.items():
        if isinstance(number, bool) or not isinstance(number, (int, float)) or number != number or not 0 <= number < 1e12:
            raise MemoryError(f"usage {key} must be a non-negative number")
        out[key] = round(number, 6) if key == "cost" else int(number)
    return out


class _Horizon(dict):
    """Records visible to a daydream, with the last ID it may cite."""
    horizon = None


class SomniMemory:
    _image_downgrades = None  # a list while a lenient-image dream pass is being prepared

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
        if _seeded_only(memory.store):
            pass  # a human seeded action word files before the first session
        elif memory.store.exists() or os.path.lexists(memory.store):
            raise MemoryError(f"create-only store already exists: {memory.store}")
        else:
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
                "max_record_bytes": MAX_RECORD_BYTES, "max_dream_passes": MAX_DREAM_PASSES}[key]

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
        transcript = self._read_chain("transcript.jsonl", state)
        notes = self._read_chain("notes.jsonl", state)
        dreams = self._read_chain("dreams.jsonl", state)
        self._check_chain(transcript, "transcript", state["transcript_count"], state["transcript_head"])
        self._check_chain(notes, "notes", state["note_events"], state["notes_head"])
        self._check_chain(dreams, "dreams", state["dream_count"], state["dreams_head"])
        self._check_forgotten(transcript, dreams)
        wiki_path = self.store / "wiki.jsonl"
        wiki = _read_log(wiki_path) if os.path.lexists(wiki_path) else []
        self._check_chain(wiki, "wiki", state.get("wiki_events", 0), state.get("wiki_head", ZERO_HASH))
        records = {record["id"]: record for record in transcript}
        claims = self._replay_notes(notes, records, self._limit("max_claims"))
        expected_next = max([int(key[1:]) for key in claims] + [0]) + 1
        if state.get("next_claim") != expected_next or state.get("phase") not in {"open", "closed"}:
            raise MemoryError("state counters are inconsistent")
        self.manifest, self.state = manifest, state
        self.transcript, self.note_log, self.dreams, self.claims = transcript, notes, dreams, claims
        self._records_by_id = records
        self._image_store_bytes = None
        self.wiki_log = wiki
        self.pages = self._replay_wiki(wiki, claims)
        if os.path.lexists(journal):
            self._clear_transaction(journal)
        return self

    # --- Segments: long logs are sealed into immutable, hash-checked files; the chain runs across them. ---

    def _segments(self, name, state=None):
        segments = ((state or self.state).get("segments") or {}).get(name, [])
        if not isinstance(segments, list):
            raise MemoryError("invalid segment list in state")
        return segments

    def _sealed_records(self, name, state):
        cache = self.__dict__.setdefault("_segment_cache", {})
        records = []
        for segment in self._segments(name, state):
            if not (isinstance(segment, dict) and isinstance(segment.get("file"), str)
                    and re.fullmatch(r"[a-z]+-[0-9]{6}\.jsonl", segment["file"])):
                raise MemoryError("invalid segment entry in state")
            path = self.store / SEGMENT_DIR / segment["file"]
            _no_symlink(path)
            try:
                info = path.lstat()
            except FileNotFoundError:
                raise MemoryError(f"missing sealed segment {segment['file']}") from None
            key = (segment["file"], info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, segment["sha256"])
            if cache.get(segment["file"], (None,))[0] != key:
                data = _read_regular(path, MAX_LOG_BYTES)
                if sha(data) != segment["sha256"]:
                    raise MemoryError(f"sealed segment {segment['file']} does not match its recorded hash")
                part = _read_log(path)
                if len(part) != segment["count"] or (part and _record_hash(part[-1]) != segment["head"]):
                    raise MemoryError(f"sealed segment {segment['file']} does not match state")
                cache[segment["file"]] = (key, part)
            records.extend(cache[segment["file"]][1])
        return records

    def _read_chain(self, name, state):
        sealed = self._sealed_records(name, state)
        live_path = self.store / name
        live = _read_log(live_path)
        segments = self._segments(name, state)
        count_key, _ = LOG_STATE[name]
        if segments and len(sealed) == state[count_key] and live and len(live) == segments[-1]["count"] \
                and sha(_read_regular(live_path, MAX_LOG_BYTES)) == segments[-1]["sha256"]:
            # A seal was interrupted after state recorded the segment: finish truncating the live log.
            fd = os.open(live_path, os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                os.ftruncate(fd, 0)
                os.fsync(fd)
            finally:
                os.close(fd)
            live = []
        return sealed + live

    def _seal(self, name):
        """Move the live log into segments/ and start an empty live log, without breaking the chain."""
        live_path = self.store / name
        data = _read_regular(live_path, MAX_LOG_BYTES)
        if not data:
            return
        folder = self.store / SEGMENT_DIR
        _no_symlink(folder)
        folder.mkdir(mode=0o700, exist_ok=True)
        segments = self._segments(name)
        count_key, head_key = LOG_STATE[name]
        file = self._next_segment_file(name)
        fd, temporary = tempfile.mkstemp(prefix=".tmp-", dir=folder)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, folder / file)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        _sync_directory(folder)
        snapshot, self._verified = self._verified, None
        live_count = self.state[count_key] - sum(segment["count"] for segment in segments)
        state = deepcopy(self.state)
        state.setdefault("segments", {}).setdefault(name, []).append(
            {"file": file, "count": live_count, "head": self.state[head_key], "sha256": sha(data)})
        self.state = state
        self._save_state()
        fd = os.open(live_path, os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.ftruncate(fd, 0)
            os.fsync(fd)
        finally:
            os.close(fd)
        self._accept_local_write(snapshot, ("state.json", name))

    def _next_segment_file(self, name, taken=()):
        prefix = name.split(".")[0]
        folder = self.store / SEGMENT_DIR
        names = [*(segment["file"] for segment in self._segments(name)), *taken,
                 *(os.listdir(folder) if folder.is_dir() else [])]
        numbers = [int(match[1]) for file in names if (match := re.fullmatch(rf"{prefix}-([0-9]{{6}})\.jsonl", file))]
        return f"{prefix}-{max(numbers, default=0) + 1:06d}.jsonl"

    # --- Forgetting: reviewed, old transcript records keep a stub (gist and cited quotes) and their chain hash. ---

    def _forget_protected(self, tonight=()):
        """Records that keep their full text: those an action word's tests or self-tests replay, including words
        tonight's pass defines (``tonight``: its prepared operations)."""
        events = [event for dream in self.dreams for event in dream.get("actions", ())]
        events += [body for op, body in tonight if op == "action"]
        return {ref for event in events for ref in re.findall(r"t[0-9]{6}", canonical(event))}

    def _cited_quotes(self, tonight=()):
        """{record id: quotes any claim or retraction cites}, including tonight's prepared operations, which a dream
        may cite from any record it read: a stub keeps them all, so replaying the notes still checks them."""
        quotes = {}
        evidence = [((e.get("claim") or {}).get("evidence") if e.get("op") == "add" else e.get("evidence")) or []
                    for e in self.note_log]
        evidence += [body.get("evidence") or [] for op, body in tonight if op in ("add", "retract")]
        for items in evidence:
            for item in items:
                if item.get("quote"):
                    quotes.setdefault(item["ref"], set()).add(item["quote"])
        return quotes

    def _forgettable(self, after_days, now, tonight=()):
        """Records tonight's pass may forget, oldest first. Called before the pass commits: it counts as the night."""
        if after_days <= 0:
            return []
        keep = int(_env_number("SOMNI_DREAM_RECORDS", FORGET_KEEP_RECORDS, 1, FORGET_KEEP_RECORDS))
        keep = min(keep, FORGET_KEEP_RECORDS)
        end = len(self.transcript) - keep
        cutoff = now - _dt.timedelta(days=after_days)
        protected = self._forget_protected(tonight)
        out = []
        for record in self.transcript[:max(0, end)]:
            moment = _parse_time(record.get("at"))
            if "forgotten" not in record and record["id"] not in protected and moment is not None and moment <= cutoff:
                out.append(record)
                if len(out) == FORGET_PER_PASS:
                    break
        return out

    @staticmethod
    def _stub(record, quotes, now):
        gist = " ".join(record["text"].split())
        if len(gist) > FORGET_GIST_CHARS:
            gist = gist[:FORGET_GIST_CHARS].rstrip() + " …"
        text = f"{FORGOTTEN_PREFIX}{_stamp(now)[:10]}] {gist}"
        kept = [quote for quote in sorted(quotes) if quote not in text]
        if kept:
            text += " | quotes kept: " + " … ".join(kept)
        if len(text.encode("utf-8")) >= len(record["text"].encode("utf-8")):
            return None  # nothing to gain
        return {**record, "text": text, "forgotten": {"sha256": sha(record), "at": _stamp(now),
                                                     "bytes": len(record["text"].encode("utf-8"))}}

    def _plan_forgetting(self, tonight=()):
        """{seq: stub} for tonight's night pass, which lists each stub's hash before any record is rewritten.

        A record is forgotten when it is older than SOMNI_FORGET_AFTER_DAYS (default 30; 0, or a value that isn't a
        number of days, keeps everything), it is outside the newest SOMNI_DREAM_RECORDS (the next dream's window), and
        no action word's tests replay it. The stub keeps the start of the text and every quote a claim cites from it;
        the chain keeps the original record's hash. At most FORGET_PER_PASS records go per night."""
        after_days, _ = _forget_setting()
        now = _now()
        quotes = self._cited_quotes(tonight)
        planned = {}
        for record in self._forgettable(after_days, now, tonight):
            stub = self._stub(record, quotes.get(record["id"], ()), now)
            if stub is not None:
                planned[record["seq"]] = stub
        return planned

    @staticmethod
    def _check_forgotten(transcript, dreams):
        """Every stub must be one a committed night pass listed, so editing a stub's text is caught like any edit."""
        listed = {}
        for dream in dreams:
            for seq, digest in (dream.get("forgot") or {}).items():
                listed.setdefault(seq, set()).add(digest)
        for record in transcript:
            if "forgotten" in record and sha(record) not in listed.get(str(record["seq"]), ()):
                raise MemoryError(f"forgotten record {record['id']} does not match the night pass that forgot it")

    def _write_forgotten(self, replaced):
        """Rewrite the transcript with tonight's stubs. Called with the lock held, after the pass that lists them.

        A last check first: a stub is written only if it still holds every quote now cited from its record and no
        word's tests replay it. A stub the pass listed but that isn't written leaves the record whole."""
        quotes, protected = self._cited_quotes(), self._forget_protected()
        replaced = {seq: stub for seq, stub in replaced.items()
                    if stub["id"] not in protected and all(q in stub["text"] for q in quotes.get(stub["id"], ()))}
        if not replaced:
            return 0
        originals = {seq: self.transcript[seq - 1] for seq in replaced}
        try:
            self._rewrite_transcript(replaced)
        except (MemoryError, OSError):
            # Never leave a store that won't load: put the original records back. They match the chain as they did.
            self._rewrite_transcript(originals)
            return 0
        return len(replaced)

    def _rewrite_transcript(self, replaced):
        """Replace records ({seq: record}) in the live log and sealed segments, then reload and verify the store."""
        name = "transcript.jsonl"
        folder = self.store / SEGMENT_DIR
        segments, written, retired, offset = [], [], [], 0
        for segment in self._segments(name):
            first, offset = offset, offset + segment["count"]
            if not any(first < seq <= offset for seq in replaced):
                segments.append(segment)
                continue
            data = b"".join(_encode_log_record(replaced.get(r["seq"], r)) for r in self.transcript[first:offset])
            file = self._next_segment_file(name, written)
            _write_bytes_durable(folder / file, data)
            written.append(file)
            retired.append(segment["file"])
            segments.append({**segment, "file": file, "sha256": sha(data)})
        self._verified = None
        if any(seq > offset for seq in replaced):
            # The live log holds no file hash, and every record keeps its chain hash, so replacing it is one atomic step.
            data = b"".join(_encode_log_record(replaced.get(r["seq"], r)) for r in self.transcript[offset:])
            _write_bytes_durable(self.store / name, data)
        if retired:
            state = deepcopy(self.state)
            state["segments"][name] = segments
            self.state = state
            self._save_state()
            for file in retired:
                (folder / file).unlink(missing_ok=True)
            _sync_directory(folder)
        self.__dict__.pop("_segment_cache", None)
        self._load_locked(force=True)

    def forgotten_counts(self):
        forgotten = [record for record in self.transcript if "forgotten" in record]
        return {"records": len(forgotten), "bytes_dropped": sum(record["forgotten"]["bytes"]
                                                                - len(record["text"].encode("utf-8")) for record in forgotten)}

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
            prefix = self._sealed_records(name, state) + _read_log(self.store / name, size)
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
            previous = _record_hash(record)
        if previous != head:
            raise MemoryError(f"{label} head does not match state")

    @staticmethod
    def _validate_evidence(evidence, records, status=None):
        if not isinstance(evidence, list) or not evidence or len(evidence) > 16:
            raise MemoryError("every claim needs 1-16 evidence references")
        normalized = []
        seen = set()
        for item in evidence:
            if not isinstance(item, dict) or set(item) - {"ref", "quote", "image_sha256"}:
                raise MemoryError("evidence must contain ref and optional quote or image_sha256")
            ref, quote = item.get("ref"), item.get("quote")
            if not isinstance(ref, str) or not EVIDENCE_ID.fullmatch(ref) or ref not in records or ref in seen:
                horizon = getattr(records, "horizon", None)
                beyond = horizon and isinstance(ref, str) and EVIDENCE_ID.fullmatch(ref) and ref > horizon
                raise MemoryError(f"evidence reference {ref!r} is after this daydream's snapshot ({horizon})" if beyond
                                  else f"evidence reference {ref!r} is missing, duplicated, or unknown")
            if quote is not None and (not isinstance(quote, str) or not quote or quote not in records[ref]["text"]):
                raise MemoryError("evidence quote must occur exactly in the referenced transcript")
            digest = item.get("image_sha256")
            if "image_sha256" in item:
                digest = _resolve_digest(records[ref], ref, digest)
            if status == "verified" and quote is None and digest is None:
                raise MemoryError("verified claims require an exact quote or stored image for every evidence reference")
            normalized.append({"ref": ref, **({"quote": quote} if quote is not None else {}),
                               **({"image_sha256": digest} if digest is not None else {})})
            seen.add(ref)
        return normalized

    @staticmethod
    def _claim_context(evidence, records):
        cited = [records[item["ref"]] for item in evidence]
        labels = sorted({r["run_label"] for r in cited if "run_label" in r})
        tags = sorted({tag for r in cited for tag in r.get("tags", [])})
        return {**({"run_labels": labels} if labels else {}), **({"tags": tags} if tags else {})}

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
                claims[claim["id"]] = {**claim, "state": "active", **({"created_at": event["at"]} if "at" in event else {})}
                replaced = claim.get("replaces")
                if replaced:
                    if replaced not in claims or claims[replaced]["state"] != "active":
                        raise MemoryError("correction replaces a non-active claim")
                    cls._supersede(claims, claim)
            elif op == "promote":
                claim_id, salience = event.get("claim_id"), event.get("salience")
                if claim_id not in claims or claims[claim_id]["state"] != "active" or type(salience) is not int or not 1 <= salience <= 5:
                    raise MemoryError("promotion targets an invalid claim or salience")
                claims[claim_id]["salience"] = salience
                if "at" in event:  # a dream that promotes a claim has used it: a rehearsal
                    claims[claim_id]["rehearsed_at"] = [*claims[claim_id].get("rehearsed_at", []), event["at"]]
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
    def _supersede(claims, claim):
        """Retire the claim that ``claim`` replaces. A correction says it was wrong (superseded); an update says the
        world changed, so the old claim stays true of its time (past), until the first newer record that shows it."""
        old = claims[claim["replaces"]]
        old["superseded_by"] = claim["id"]
        if claim.get("change") != "changed":
            old["state"] = "superseded"
            return
        newest_old = max(_ref_number(item["ref"]) for item in old["evidence"])
        old["state"] = "past"
        old["until_ref"] = min((item["ref"] for item in claim["evidence"] if _ref_number(item["ref"]) > newest_old),
                               key=_ref_number)

    @classmethod
    def _replay_wiki(cls, events, claims):
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
        broken = next(cls._broken_wiki_links(pages), None)
        if broken:
            source, target = broken
            raise MemoryError(f"wiki page {source!r} links to {target!r}, which does not exist")
        return pages

    @staticmethod
    def _broken_wiki_links(pages):
        for slug, page in pages.items():
            for target in page["links"]:
                if target not in pages:
                    yield slug, target

    @staticmethod
    def _validate_page(op, simulated_claims, simulated_pages):
        allowed = {"op", "slug", "title", "body", "claims", "links"}
        if set(op) - allowed or not {"slug", "title", "body", "claims"} <= set(op):
            raise MemoryError("page operation needs slug, title, body, claims (and optional links)")
        slug, title, body = op["slug"], op["title"], op["body"]
        if not isinstance(slug, str) or not PAGE_SLUG.fullmatch(slug):
            raise MemoryError(f"page slug must be lowercase-hyphenated segments ([a-z0-9][a-z0-9-]{{0,63}}), "
                              f"optionally a folder path such as topic/page, at most {MAX_PAGE_DEPTH} deep")
        if slug in RESERVED_PAGES:
            raise MemoryError(f"page slug {slug!r} is reserved for the vault's own {slug}.md")
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
        if not isinstance(links, list) or not all(isinstance(x, str) and PAGE_SLUG.fullmatch(x) for x in links):
            raise MemoryError("page links must be a list of slugs")
        links = sorted(set(links) | {match[1] for match in WIKILINK.finditer(body)})
        if len(links) > 32:
            raise MemoryError("a page may link to at most 32 other pages")
        if not refs and not links:
            raise MemoryError("a page must cite at least one claim, or link other pages (a navigation page)")
        if slug not in simulated_pages and len(simulated_pages) >= MAX_PAGES:
            raise MemoryError("page limit reached")
        version = simulated_pages[slug]["version"] + 1 if slug in simulated_pages else 1
        return {"slug": slug, "title": title.strip(), "body": body.strip(), "claims": refs, "links": links, "version": version}

    def write_vault(self):
        """Render the wiki as a vault of markdown notes (deterministic; a view, never read back).

        A slug like ``topic/page`` becomes ``topic/page.md``.  Links render as relative markdown
        links (``[Title](../topic/page.md)``), which GitLab wikis and Obsidian both follow, or as
        Obsidian ``[[wikilinks]]`` with SOMNI_LINK_STYLE=wikilink.
        """
        vault = self.store / VAULT_DIR
        _no_symlink(vault)
        vault.mkdir(mode=0o700, exist_ok=True)
        markdown = os.environ.get("SOMNI_LINK_STYLE", "markdown") != "wikilink"
        claims = self.claims
        backlinks = {}
        for page in self.pages.values():
            for link in page["links"]:
                backlinks.setdefault(link, []).append(page["slug"])

        def link(source, target, label, anchor=None):
            """A link written in note `source` (a slug, or index/claims) to note `target`."""
            if not markdown:
                return f"[[{target}{'#' + anchor if anchor else ''}|{label}]]"
            path = posixpath.relpath(f"{target}.md", posixpath.dirname(f"{source}.md") or ".")
            text = label.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
            return f"[{text}]({path}{'#' + anchor if anchor else ''})"

        def title(slug):
            return self.pages[slug]["title"] if slug in self.pages else slug

        def body_md(source, text, refs):
            text = CLAIM_CITE.sub(lambda m: link(source, "claims", m.group(1), m.group(1))
                                  if m.group(1) in refs else m.group(0), text)
            if markdown:
                text = WIKILINK.sub(lambda m: link(source, m.group(1), m.group(2) or title(m.group(1))), text)
            return text

        files = {}
        for slug, page in sorted(self.pages.items()):
            fm = ["---", f"title: {json.dumps(page['title'], ensure_ascii=False)}", f"version: {page['version']}",
                  "claims: [" + ", ".join(page["claims"]) + "]", "tags: [somni]", "---", ""]
            lines = fm + [f"# {page['title']}", "", body_md(slug, page["body"], set(page["claims"])), ""]
            if page["links"]:
                lines += ["## See also", ""] + [f"- {link(slug, l, title(l))}" for l in page["links"] if l in self.pages] + [""]
            if backlinks.get(slug):
                lines += ["## Backlinks", ""] + [f"- {link(slug, b, title(b))}" for b in sorted(backlinks[slug])] + [""]
            lines += ["## Evidence", ""]
            for ref in page["claims"]:
                c = claims.get(ref)
                state = "" if c and c["state"] == "active" else f" (**{c['state'] if c else 'missing'}**)"
                lines.append(f"- {link(slug, 'claims', ref, ref)}{state}: {c['text'] if c else ''}")
            files[f"{slug}.md"] = "\n".join(lines) + "\n"

        tree = {"pages": [], "folders": {}}
        for slug, page in self.pages.items():
            node = tree
            for part in slug.split("/")[:-1]:
                node = node["folders"].setdefault(part, {"pages": [], "folders": {}})
            node["pages"].append(slug)

        def tree_lines(node, depth=0):
            out = [f"{'  ' * depth}- {link('index', slug, title(slug))}"
                   for slug in sorted(node["pages"], key=lambda slug: (title(slug).casefold(), slug))]
            for name, sub in sorted(node["folders"].items()):
                out += [f"{'  ' * depth}- {name}/", *tree_lines(sub, depth + 1)]
            return out

        index = ["---", "title: Index", "tags: [somni, index]", "---", "", "# Index", "",
                 f"{len(self.pages)} pages, {sum(c['state'] == 'active' for c in claims.values())} active claims. "
                 f"Pages trace to {link('index', 'claims', 'claims')} and their transcript quotes and images.", ""]
        index += tree_lines(tree)
        files["index.md"] = "\n".join(index) + "\n"
        transcript = {r["id"]: r for r in self.transcript}
        cl = ["---", "title: Claims", "tags: [somni, claims]", "---", "", "# Claims", "",
              "Each `verified` claim cites an exact excerpt or a stored image for each record.", ""]
        cited_by = {}
        for slug, page in self.pages.items():
            for ref in page["claims"]:
                cited_by.setdefault(ref, []).append(slug)
        for cid, c in sorted(claims.items()):
            cl += [f"## {cid}", "", f"{c['text']}", "",
                   f"- state: {c['state']}; status: {c['status']}; salience: {c['salience']}"]
            if c.get("run_labels"):
                cl.append("- runs: " + ", ".join(c["run_labels"]))
            if c.get("tags"):
                cl.append("- tags: " + ", ".join(c["tags"]))
            if c.get("replaces"):
                verb = "updates (the world changed)" if c.get("change") == "changed" else "supersedes"
                cl.append(f"- {verb} {link('claims', 'claims', c['replaces'], c['replaces'])}")
            if c["state"] == "past":
                until = self.record_time(c.get("until_ref"))
                cl.append(f"- true until {until[:10] if until else c.get('until_ref')} "
                          f"({link('claims', 'claims', c['superseded_by'], c['superseded_by'])} is current)")
            if c.get("merged_from"):
                parents = ", ".join(link("claims", "claims", ref, ref) for ref in c["merged_from"])
                cl.extend([f"- merged from {parents}", f"- provenance SHA-256: `{c['provenance_sha256']}`"])
            if cited_by.get(cid):
                cl.append("- used by: " + ", ".join(link("claims", s, title(s)) for s in sorted(cited_by[cid])))
            for ev in c["evidence"]:
                rec = transcript.get(ev["ref"], {})
                quote = ev.get("quote")
                content = f"> {quote}" if quote else rec.get("text", "")[:200]
                if "image_sha256" in ev:
                    name = self._image_path(self._image_descriptor(ev["ref"], ev["image_sha256"])).name
                    content = f"[image](../images/{name})" + (f" {content}" if quote else "")
                cl.append(f"- {ev['ref']} ({rec.get('source', '?')}): {content}")
            cl.append("")
        files["claims.md"] = "\n".join(cl) + "\n"

        for folder, dirs, names in os.walk(vault, topdown=False):
            for name in names:
                path = pathlib.Path(folder) / name
                if name.endswith(".md") and path.relative_to(vault).as_posix() not in files:
                    path.unlink()
            if pathlib.Path(folder) != vault and not os.listdir(folder):
                os.rmdir(folder)
        for name, text in files.items():
            target = vault / name
            folder = vault
            for part in pathlib.PurePosixPath(name).parts[:-1]:
                folder = folder / part
                _no_symlink(folder)
                folder.mkdir(mode=0o700, exist_ok=True)
            if target.exists() and target.read_text(encoding="utf-8") == text:
                continue
            _write_text_atomic(target, text)
        return sorted(files)

    def stale_pages(self):
        """Pages citing claims that are no longer active (input for gap-filling)."""
        return {slug: [c for c in page["claims"] if self.claims[c]["state"] != "active"]
                for slug, page in self.pages.items() if any(self.claims[c]["state"] != "active" for c in page["claims"])}

    @classmethod
    def _validate_claim(cls, claim, records, existing):
        required = {"id", "text", "status", "salience", "evidence", "links"}
        merge_fields = {"merged_from", "provenance_sha256"}
        if (not isinstance(claim, dict) or not required.issubset(claim)
                or set(claim) - (required | {"gist", "replaces", "change", "run_labels", "tags"} | merge_fields)):
            raise MemoryError("claim has invalid fields")
        text = claim["text"]
        if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > MAX_NOTE_BYTES:
            raise MemoryError("claim text is empty or too large")
        if "gist" in claim:
            gist = claim["gist"]
            if (not isinstance(gist, str) or not gist.strip() or not gist.isprintable()
                    or len(gist.encode("utf-8")) > MAX_GIST_BYTES):
                raise MemoryError(f"claim gist must be one printable line of up to {MAX_GIST_BYTES} UTF-8 bytes")
        if not isinstance(claim["status"], str) or claim["status"] not in STATUSES:
            raise MemoryError(f"claim status must be one of {', '.join(sorted(STATUSES))}; got {claim['status']!r}")
        if type(claim["salience"]) is not int or not 1 <= claim["salience"] <= 5:
            raise MemoryError(f"claim salience must be an integer from 1 to 5; got {claim['salience']!r}")
        cls._validate_evidence(claim["evidence"], records, claim["status"])
        context = {key: claim[key] for key in ("run_labels", "tags") if key in claim}
        if context != cls._claim_context(claim["evidence"], records):
            raise MemoryError("claim run labels or tags do not match its evidence")
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
        if "change" in claim:
            if claim["change"] != "changed" or "replaces" not in claim:
                raise MemoryError('change must be "changed" and needs a replaced claim')
            newest_old = max(_ref_number(item["ref"]) for item in existing[claim["replaces"]]["evidence"])
            if not any(_ref_number(item["ref"]) > newest_old for item in claim["evidence"]):
                raise MemoryError(f"an update needs evidence newer than {claim['replaces']}'s newest record "
                                  f"(t{newest_old:06d}): the change has to show in a later record. If the old claim "
                                  f"was wrong rather than out of date, use correct")
        if claim.keys() & merge_fields:
            parents = cls._merge_parents(claim.get("merged_from"), existing)
            if ("replaces" in claim or claim["merged_from"] != parents or claim["links"] != parents
                    or claim["evidence"] != cls._merge_evidence(parents, existing)):
                raise MemoryError("merged claim must use its canonical parents and evidence")
            if claim.get("provenance_sha256") != cls._merge_digest(claim, records, existing):
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
                rid = citation["ref"]
                previous = evidence.get(rid, {})
                best = previous if previous and (previous.get("quote") or not citation.get("quote")) else citation
                image = previous.get("image_sha256") or citation.get("image_sha256")
                evidence[rid] = {**best, **({"image_sha256": image} if image else {})}
        return [evidence[ref] for ref in sorted(evidence)]

    @classmethod
    def _merge_digest(cls, claim, records, existing):
        def parent_hash(parent):
            if "provenance_sha256" in parent:
                return parent["provenance_sha256"]
            evidence = [
                {**ref, "record_sha256": _record_hash(records[ref["ref"]])}
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
        for name, values in entries.items():
            if name in SEGMENTED_LOGS and (self.store / name).stat().st_size + sum(map(len, values)) > SEGMENT_BYTES:
                self._seal(name)
                next_state = {**next_state, "segments": self.state["segments"]}
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

    def _image_path(self, descriptor):
        digest = descriptor["sha256"]
        if not isinstance(digest, str) or not SHA256.fullmatch(digest) or descriptor["mime_type"] not in IMAGE_SUFFIX:
            raise MemoryError("invalid stored image descriptor")
        return self.store / "images" / f"{digest}.{IMAGE_SUFFIX[descriptor['mime_type']]}"

    def _read_image(self, descriptor):
        data = _read_regular(self._image_path(descriptor), MAX_IMAGE_BYTES)
        if len(data) != descriptor["bytes"] or sha(data) != descriptor["sha256"]:
            raise MemoryError("stored image does not match its transcript digest")
        return data

    def _store_image(self, mime, data):
        descriptor = {"sha256": sha(data), "mime_type": mime, "bytes": len(data)}
        path = self._image_path(descriptor)
        _no_symlink(path)
        if path.exists():
            self._read_image(descriptor)
            return descriptor
        if not path.parent.exists():
            path.parent.mkdir(mode=0o700)
            _sync_directory(self.store)
        if self._image_store_bytes is None:
            self._image_store_bytes = sum(entry.lstat().st_size for entry in path.parent.iterdir())
        if self._image_store_bytes + len(data) > MAX_IMAGE_STORE_BYTES:
            return None
        fd, tmp = tempfile.mkstemp(prefix=".image-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
            self._image_store_bytes += len(data)
            _sync_directory(path.parent)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return descriptor

    def _image_descriptor(self, ref, digest):
        record = self._records_by_id.get(ref)
        if record is None:
            raise MemoryError(f"transcript record {ref!r} does not exist")
        full = _resolve_digest(record, ref, digest)
        return next(descriptor for descriptor in record["images"] if descriptor["sha256"] == full)

    def image(self, ref, digest):
        descriptor = self._image_descriptor(ref, digest)
        data = self._read_image(descriptor)
        return {"type": "image", "mimeType": descriptor["mime_type"], "data": base64.b64encode(data).decode("ascii")}

    def _check_cited_images(self, evidence):
        for item in evidence:
            if "image_sha256" in item:
                self._read_image(self._image_descriptor(item["ref"], item["image_sha256"]))

    def record(self, source, text, **kwargs):
        return self.record_many([(source, text)], **kwargs)[0]

    def record_many(self, items, *, context=None, images=(), omitted_images=0, clip=False,
                    image_bytes=DEFAULT_IMAGE_BYTES, resize=None, tool_ok=None, calls=None, via_word=None,
                    word_stamp=None):
        """Append several (source, text) records under one lock.

        `calls` ([{tool, ok, start_ms, ms}]) times an action word's nested calls on the word's own result.
        """
        items = list(items)
        if not items:
            return []
        if calls is not None:
            calls = _call_timings(calls)
            if len(items) != 1 or items[0][0] != "tool_result":
                raise MemoryError("call timings require one tool result")
        if via_word is not None and (not isinstance(via_word, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", via_word)
                                     or len(items) != 1 or items[0][0] != "tool_result"):
            raise MemoryError("via_word names the action word that made one tool call")
        context = _record_context(**(context or {}))
        if tool_ok is not None and (type(tool_ok) is not bool or len(items) != 1 or items[0][0] != "tool_result"):
            raise MemoryError("tool status requires one tool result")
        if len(images) > MAX_RECORD_IMAGES or type(omitted_images) is not int or omitted_images < 0:
            raise MemoryError("invalid tool result image list or omission count")
        if type(image_bytes) is not int or not 0 <= image_bytes <= MAX_IMAGE_BYTES:
            raise MemoryError(f"image byte cap must be 0..{MAX_IMAGE_BYTES}")
        for source, text in items:
            if not isinstance(source, str) or source not in SOURCES:
                raise MemoryError("invalid transcript source")
        with self._locked():
            self._load_locked()
            max_bytes = self._limit("max_record_bytes")
            if any(not isinstance(text, str) or not text.strip() or (not clip and len(text.encode("utf-8")) > max_bytes)
                   for _, text in items):
                raise MemoryError("transcript text is empty or too large")
            if self.state["phase"] != "open":
                raise MemoryError("recording requires an open subject session")
            if self.state["transcript_count"] + len(items) > self._limit("max_transcript_records"):
                raise MemoryError("transcript record limit reached; seal the bounded attempt")
            if (images or omitted_images) and (len(items) != 1 or items[0][0] != "tool_result"):
                raise MemoryError("images can only attach to one tool result at a time")
            attachments = []
            for image in images:
                decoded = _image_bytes(image, image_bytes, resize)
                stored = self._store_image(*decoded) if decoded else None
                if stored:
                    attachments.append(stored)
                else:
                    omitted_images += 1
            seq, head, encoded, ids, records = self.state["transcript_count"], self.state["transcript_head"], [], [], []
            at = _stamp(_now())
            for source, text in items:
                seq += 1
                record = {"schema": SCHEMA, "seq": seq, "id": f"t{seq:06d}", "source": source, "at": at,
                          "text": _clip_record(text, max_bytes) if clip else text, "prev_sha256": head, **context}
                if clip and record["text"] != text:
                    record["original_text_bytes"] = len(text.encode("utf-8"))
                if tool_ok is not None:
                    record["tool_ok"] = tool_ok
                if calls:
                    record["calls"] = calls
                if via_word:
                    record["via_word"] = via_word
                if word_stamp:
                    record["word"] = word_stamp
                if attachments:
                    record["images"] = attachments
                if omitted_images:
                    record["omitted_images"] = omitted_images
                head = sha(record)
                encoded.append(_encode_log_record(record))
                ids.append(record["id"])
                records.append(record)
            self._commit_logs({"transcript.jsonl": encoded}, {
                **self.state, "transcript_count": seq, "transcript_head": head,
            })
            self.transcript.extend(records)
            self._records_by_id.update(zip(ids, records))
        return ids

    def add_note(self, text, status, evidence, salience=3, links=None, *, gist=None):
        with self._locked():
            self._load_locked()
            if self.state["phase"] != "open":
                raise MemoryError("agent notes require an open subject session")
            return self._append_claim(text, status, evidence, salience, links or [], None, gist=gist)

    def _append_claim(self, text, status, evidence, salience, links, replaces, *, gist=None):
        _require_claim_capacity(len(self.claims), self._limit("max_claims"))
        claim_id = f"n{self.state['next_claim']:06d}"
        claim = {"id": claim_id, "text": text, "status": status, "salience": salience,
                 "evidence": evidence, "links": links}
        if gist is not None:
            claim["gist"] = gist
        if replaces is not None:
            claim["replaces"] = replaces
        claim["evidence"] = self._validate_evidence(evidence, self._records_by_id, status)
        claim.update(self._claim_context(claim["evidence"], self._records_by_id))
        self._validate_claim(claim, self._records_by_id, self.claims)
        self._check_cited_images(claim["evidence"])
        event = {"schema": SCHEMA, "seq": self.state["note_events"] + 1, "op": "add", "claim": claim,
                 "at": _stamp(_now()), "prev_sha256": self.state["notes_head"]}
        self._commit_logs({"notes.jsonl": [_encode_log_record(event)]}, {
            **self.state, "note_events": event["seq"], "notes_head": sha(event),
            "next_claim": self.state["next_claim"] + 1,
        })
        self.note_log.append(event)
        self.claims[claim_id] = {**claim, "state": "active", "created_at": event["at"]}
        if replaces is not None:
            self._supersede(self.claims, claim)
        return claim_id

    def dream(self, proposal, partial=False, *, allow_empty=False, viewed_images=None, actions=None,
              snapshot=None, usage=None, lenient_images=False):
        """Apply one offline consolidation pass.

        By default the pass is all-or-nothing.  With ``partial=True`` an operation
        that fails validation is skipped and listed in the pass record's
        ``rejected`` field (index and reason), so a model-authored pass keeps its
        valid operations and the rejection itself becomes part of the trace.  At
        least one operation must still be accepted.

        ``actions`` enables action-word operations: ``{"allow": names or None,
        "reserved": session tool names, "author": "dream" or "human"}``.

        ``snapshot`` (a transcript count H) makes this a live pass, a daydream: it may run
        while a session keeps recording, may cite only records 1..H, and is bound to the
        transcript head at H rather than the current head.

        ``lenient_images`` keeps a citation's {ref} when only its image digest is wrong or unviewed,
        downgrading a verified claim that is left without proof; the pass lists each downgrade.
        """
        if not isinstance(proposal, dict) or proposal.get("schema") != SCHEMA or set(proposal) != {"schema", "operations"}:
            raise MemoryError(f'a dream proposal is exactly {{"schema": {SCHEMA}, "operations": [...]}}; '
                              f"got {'keys ' + ', '.join(sorted(map(str, proposal))) if isinstance(proposal, dict) else type(proposal).__name__}")
        operations = proposal["operations"]
        minimum = 0 if allow_empty else 1
        if not isinstance(operations, list) or not minimum <= len(operations) <= 32:
            raise MemoryError(f"dream proposal needs {minimum}-32 operations")
        usage = _usage(usage)
        proposal_bytes = canonical(proposal).encode("utf-8")
        if len(proposal_bytes) > MAX_INPUT_BYTES:
            raise MemoryError("dream proposal exceeds byte limit")
        with self._locked():
            self._load_locked()
            if snapshot is None and self.state["phase"] != "closed":
                raise MemoryError("dreaming is offline-only; close the subject session first")
            if snapshot is not None:
                if type(snapshot) is not int or not 0 < snapshot <= self.state["transcript_count"]:
                    raise MemoryError(f"snapshot must be a transcript count in 1..{self.state['transcript_count']}")
            if self.state["dream_count"] >= self._limit("max_dream_passes"):
                raise MemoryError("dream pass limit reached; seal the bounded attempt")
            records = self._records_by_id
            if snapshot is not None:
                # Evidence is limited to the snapshot: records after H do not exist for this pass.
                self._records_by_id = _Horizon({r["id"]: r for r in self.transcript[:snapshot]})
                self._records_by_id.horizon = self.transcript[snapshot - 1]["id"]
            self._image_downgrades = [] if lenient_images else None
            try:
                prepared, next_claim, rejected = self._prepare_dream(operations, partial, viewed_images, actions)
                downgrades = self._image_downgrades or []
            finally:
                self._records_by_id = records
                self._image_downgrades = None
            if not prepared and operations:
                listed = "; ".join(f"op {r['index']} ({_op_name(operations[r['index']])}): {r['reason']}" for r in rejected)
                raise MemoryError(f"nothing committed: all {len(operations)} operation(s) were rejected. {listed[:1500]}")
            # Sleep prunes: a night pass forgets old raw detail. It lists each stub's hash, then the stubs are written.
            night = snapshot is None and (actions or {}).get("author", "dream") == "dream"
            forget = self._plan_forgetting(prepared) if night else {}
            self._commit_dream(proposal_bytes, prepared, next_claim, rejected,
                               author=(actions or {}).get("author", "dream"), snapshot=snapshot, usage=usage,
                               downgrades=downgrades, forgot={str(seq): sha(stub) for seq, stub in forget.items()})
            self._load_locked(force=True)
            if forget:
                self._write_forgotten(forget)
            if self.dreams[-1].get("actions"):
                self.write_action_files(self.dreams[-1]["actions"])
            if self.pages or os.path.isdir(self.store / VAULT_DIR):
                self.write_vault()
            return self.dreams[-1]

    def _prepare_dream(self, operations, partial, viewed_images, actions):
        # Removing a broken wiki operation can expose another broken link or an
        # earlier page version. Rebuild the simulation before checking again.
        link_rejections = {}
        while True:
            simulated = {key: dict(value) for key, value in self.claims.items()}
            words = None
            pages = self.pages.copy()
            page_changes = {}
            prepared = []
            rejected = link_rejections.copy()
            next_claim = self.state["next_claim"]
            if self._image_downgrades is not None:
                self._image_downgrades = []
            for index, operation in enumerate(operations):
                if index in rejected:
                    continue
                try:
                    if isinstance(operation, dict) and isinstance(operation.get("op"), str) and operation["op"] in ACTION_OPS:
                        if actions is None:
                            raise MemoryError("action words are disabled (SOMNI_ACTIONS=1 enables them)")
                        if words is None:
                            words = self.action_words()
                        prepared.append(("action", self._prepare_action(operation, words, actions)))
                        continue
                    if self._image_downgrades is not None:
                        operation = self._lenient_image_evidence(operation, viewed_images, index)
                    next_claim = self._prepare_dream_op(operation, simulated, pages, prepared, next_claim, viewed_images)
                except (MemoryError, OSError) as exc:
                    if not partial or "claim limit reached" in str(exc):
                        raise
                    rejected[index] = str(exc)[:300]
                    continue
                if operation["op"] in {"page", "drop_page"}:
                    page_changes[operation["slug"]] = (index, operation["op"])

            conflict = self._wiki_link_conflict(pages, page_changes)
            if conflict is None:
                return prepared, next_claim, [{"index": index, "reason": rejected[index]} for index in sorted(rejected)]
            index, reason = conflict
            if not partial:
                raise MemoryError(reason)
            link_rejections[index] = reason

    def _prepare_action(self, operation, words, actions):
        A = _action_module()
        kind = operation["op"]
        try:
            if kind == "retire_action":
                body = A.check_retire(operation, words)
                if actions.get("author") != "human":  # a human deleting the file needs no evidence
                    self._check_retire_evidence(body, words[body["name"]])
                del words[body["name"]]
                return {"op": "retire", **body}
            if kind == "define_action":
                if set(operation) != {"op", "source", "tests"}:
                    raise MemoryError("define_action needs exactly source and tests")
                source, tests, replacing = A.normalize(operation["source"]), operation["tests"], None
            else:
                if not {"op", "source"} <= set(operation) <= {"op", "source", "tests"}:
                    raise MemoryError("revise_action needs source and optionally tests")
                source = A.normalize(operation["source"])
                replacing = A.parse(source)["name"]
                if replacing not in words:
                    raise MemoryError(f"revise_action targets unknown action word {replacing}")
                tests = operation.get("tests", words[replacing]["tests"])
            word, dropped = A.build(source, tests, self._records_by_id, words, allow=actions.get("allow"),
                                    reserved=set(actions.get("reserved", ())), replacing=replacing,
                                    lenient=actions.get("author") == "human",
                                    catalog=A.namespace(self.action_tool_catalog(), words))
        except (ValueError, TypeError, KeyError, RecursionError) as exc:
            raise MemoryError(str(exc)[:600]) from exc
        words[word["name"]] = word
        return {"op": "define" if replacing is None else "revise", **word,
                **({"dropped_tests": dropped} if dropped else {})}

    def _wiki_link_conflict(self, pages, page_changes):
        blocked_drop = None
        for source, target in self._broken_wiki_links(pages):
            drop_index, op = page_changes.get(target, (None, None))
            if op != "drop_page":
                return page_changes[source][0], f"page {source!r} links to {target!r}, which does not exist at the end of the pass"
            # Resolve broken writes first. Removing one can make the drop valid.
            if blocked_drop is None:
                blocked_drop = drop_index, f"cannot drop {target!r}: page {source!r} would still link to it"
        return blocked_drop

    def _lenient_image_evidence(self, operation, viewed_images, index):
        """With lenient image evidence, keep a citation's {ref} when only its image is wrong or unviewed.

        A verified claim whose item is left with neither quote nor image becomes uncertain.  Each change
        is recorded so the pass shows what was downgraded.
        """
        if not isinstance(operation, dict) or not isinstance(operation.get("evidence"), list):
            return operation
        evidence, changes = [], []
        for item in operation["evidence"]:
            if not isinstance(item, dict) or "image_sha256" not in item or not isinstance(item.get("ref"), str):
                evidence.append(item)
                continue
            record = self._records_by_id.get(item["ref"])
            try:
                full = _resolve_digest(record or {}, item["ref"], item["image_sha256"])
                problem = None if viewed_images is None or (item["ref"], full) in viewed_images else "image not viewed in this dream"
            except MemoryError as exc:
                problem = str(exc)
            if problem is None:
                evidence.append(item)
                continue
            evidence.append({key: value for key, value in item.items() if key != "image_sha256"})
            changes.append({"index": index, "ref": item["ref"], "reason": problem[:200]})
        if not changes:
            return operation
        operation = {**operation, "evidence": evidence}
        if operation.get("status") == "verified" and any("quote" not in item and "image_sha256" not in item
                                                         for item in evidence if isinstance(item, dict)):
            operation["status"] = "uncertain"
            changes[-1]["status"] = "verified -> uncertain"
        self._image_downgrades.extend(changes)
        return operation

    def _prepare_dream_op(self, operation, simulated, pages, prepared, next_claim, viewed_images):
        if not isinstance(operation, dict):
            raise MemoryError("dream operation must be an object")
        op = operation.get("op")
        if not isinstance(op, str):
            raise MemoryError("dream operation type is invalid")
        if op == "page":
            page = self._validate_page(operation, simulated, pages)
            prepared.append(("page", page))
            pages[page["slug"]] = page
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
            slug = operation.get("slug")
            if set(operation) != {"op", "slug", "reason"} or not isinstance(slug, str) or slug not in pages:
                raise MemoryError("drop_page needs an existing slug and a reason")
            if not isinstance(operation["reason"], str) or not operation["reason"].strip():
                raise MemoryError("drop_page needs a reason")
            prepared.append(("drop_page", {"slug": slug, "reason": operation["reason"]}))
            del pages[slug]
            return next_claim
        if op in {"add", "correct", "update", "merge"}:
            _require_claim_capacity(len(simulated), self._limit("max_claims"))
            allowed = {"op", "text", "gist", "status", "salience"}
            allowed |= {"claim_ids"} if op == "merge" else {"evidence", "links"}
            replacing = op in {"correct", "update"}
            if replacing:
                allowed.add("claim_id")
            if set(operation) - allowed or (replacing and "claim_id" not in operation):
                raise MemoryError("dream claim operation has unknown fields")
            replaces = operation.get("claim_id") if replacing else None
            if replacing and (not isinstance(replaces, str) or not CLAIM_ID.fullmatch(replaces)
                    or replaces not in simulated or simulated[replaces]["state"] != "active"):
                raise MemoryError(f"{'correction' if op == 'correct' else 'update'} targets an invalid or non-active claim")
            claim_id = f"n{next_claim:06d}"
            claim = {"id": claim_id, "text": operation.get("text"), "status": operation.get("status"),
                     "salience": operation.get("salience", 3), "evidence": operation.get("evidence"),
                     "links": operation.get("links", [])}
            if "gist" in operation:
                claim["gist"] = operation["gist"]
            if replaces is not None:
                claim["replaces"] = replaces
            if op == "update":
                claim["change"] = "changed"
            if op == "merge":
                parents = self._merge_parents(operation.get("claim_ids"), simulated)
                claim.update(merged_from=parents, links=parents, evidence=self._merge_evidence(parents, simulated))
                claim["status"] = operation.get("status", "uncertain")
                claim["provenance_sha256"] = self._merge_digest(claim, self._records_by_id, simulated)
            evidence = self._validate_evidence(claim["evidence"], self._records_by_id, claim["status"])
            if op != "merge":
                claim["evidence"] = evidence
            claim.update(self._claim_context(evidence, self._records_by_id))
            self._validate_claim(claim, self._records_by_id, simulated)
            self._check_cited_images(evidence)
            if op != "merge":
                _require_viewed_images(evidence, viewed_images)
            prepared.append(("add", claim))
            simulated[claim_id] = {**claim, "state": "active"}
            if replaces:
                self._supersede(simulated, claim)
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
            evidence = self._validate_evidence(operation["evidence"], self._records_by_id)
            self._check_cited_images(evidence)
            _require_viewed_images(evidence, viewed_images)
            prepared.append(("retract", {"claim_id": claim_id, "reason": operation["reason"], "evidence": evidence}))
            simulated[claim_id]["state"] = "retracted"
        else:
            raise MemoryError("unknown dream operation")
        return next_claim

    def _commit_dream(self, proposal_bytes, prepared, next_claim, rejected, *, author="dream", snapshot=None, usage=None,
                      downgrades=(), forgot=None):
        words = [body for op, body in prepared if op == "action"]
        prepared = [(op, body) for op, body in prepared if op != "action"]
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
        at = _stamp(_now())
        for op, body in prepared:
            note_seq += 1
            event = {"schema": SCHEMA, "seq": note_seq, "op": op, "at": at,
                     "prev_sha256": notes_head, **({"claim": body} if op == "add" else body)}
            note_events.append(event)
            notes_head = sha(event)
        pass_number = self.state["dream_count"] + 1
        count = self.state["transcript_count"] if snapshot is None else snapshot
        head = self.state["transcript_head"] if snapshot is None else _record_hash(self.transcript[snapshot - 1])
        dream = {"schema": SCHEMA, "seq": pass_number, "transcript_count": count, "at": at,
                 "transcript_head": head, "operation_count": len(prepared),
                 "proposal_sha256": sha(proposal_bytes), "input_bytes": len(proposal_bytes),
                 "input_tokens_estimate": token_estimate(proposal_bytes), "prev_sha256": self.state["dreams_head"]}
        merged = sum(op == "add" and "merged_from" in body for op, body in prepared)
        dream["added_count"] = sum(op == "add" for op, _ in prepared) - merged
        dream["merged_count"] = merged
        if added := [body["id"] for op, body in prepared if op == "add"]:
            dream["added_ids"] = added  # the claims this pass created, so a session can show them
        if words:
            dream["actions"] = words
            dream["operation_count"] += len(words)
        if author != "dream":
            dream["author"] = author
        if snapshot is not None:
            dream["live"] = True  # a daydream: committed while recording continued past its snapshot
        if downgrades:
            dream["image_downgrades"] = list(downgrades)
        if forgot:
            dream["forgot"] = forgot  # {seq: sha256 of the stub} for the records this night forgets
        if usage:
            dream["usage"] = usage  # what the dreaming model reported spending, up to this submission
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

    # --- Time and strength ---

    def _mentions(self):
        """{claim id: [times a later record named it]}: an agent's use of a memory (retrieval practice)."""
        cached = self.__dict__.get("_mention_cache")
        if cached is None or cached[0] is not self.transcript or cached[1] > len(self.transcript):
            cached = [self.transcript, 0, {}]
        for record in self.transcript[cached[1]:]:
            if "n0" in record["text"] and record.get("at"):
                for claim_id in set(CLAIM_MENTION.findall(record["text"])):
                    cached[2].setdefault(claim_id, []).append(record["at"])
        cached[1] = len(self.transcript)
        self._mention_cache = cached
        return cached[2]

    def _uses(self, claim):
        """The times a claim was used: its evidence records, its writing and promotions, later mentions of it."""
        records = self._records_by_id
        times = [records[item["ref"]].get("at") for item in claim["evidence"] if item["ref"] in records]
        times += [claim.get("created_at"), *claim.get("rehearsed_at", []), *self._mentions().get(claim["id"], [])]
        return [moment for moment in map(_parse_time, times) if moment is not None]

    def strength(self, claim, now=None):
        """How available a claim is now: ACT-R activation, a fuzzy label and when it was last used."""
        uses = self._uses(claim)
        if not uses:
            return {"activation": 0.0, "level": "fresh"}
        now = now or _now()
        decay = _env_number("SOMNI_MEMORY_DECAY", DEFAULT_DECAY, 0.0, 1.0)  # 0: by number of uses, no clock
        activation = _activation([(now - moment).total_seconds() / 86400 for moment in uses], decay, claim["salience"])
        return {"activation": round(activation, 3), "level": _strength_level(activation), "last_used": _stamp(max(uses)),
                "first_seen": _stamp(min(uses))}

    def record_time(self, ref):
        record = self._records_by_id.get(ref)
        return record.get("at") if record else None

    def history(self, claim):
        """The versions a claim updated, newest first: each was true until the record that showed the change."""
        versions = []
        current = claim
        while current.get("change") == "changed" and current.get("replaces") in self.claims and len(versions) < 8:
            old = self.claims[current["replaces"]]
            versions.append({"id": old["id"], "text": old.get("gist", old["text"]), "until_ref": old.get("until_ref"),
                             "until": self.record_time(old.get("until_ref"))})
            current = old
        return versions

    def describe_time(self, claim, now=None):
        """A short clause about a claim's age and history: empty for a fresh claim with no history.

        A faded claim gets a neutral "as of <date>", and "faint" only once it is faint. In a benchmark, labelling every
        week-old claim "fading" sent the agent to the files instead of using it, and made dreams write "verify before
        relying" into claims."""
        parts = []
        strength = self.strength(claim, now)
        if strength["level"] != "fresh":
            parts.append(f"as of {strength['last_used'][:10]}" + ("; faint" if strength["level"] == "faint" else ""))
        history = self.history(claim)
        if history:
            since = history[0]["until"]
            parts.append(f"since {since[:10] if since else history[0]['until_ref']}; before that: {history[0]['text'][:120]}")
        return "; ".join(parts)

    def related_claims(self, records, limit=12):
        """Active claims that share terms with ``records``: what a dream should check those records against."""
        active = [claim for claim in self.claims.values() if claim["state"] == "active"]
        if not active or not records:
            return []
        text = " ".join(record["text"] for record in records[-60:])[:20000]
        docs = [_terms(" ".join([claim["text"], *_claim_labels(claim)])) for claim in active]
        now = _now()
        hits = _bm25(docs, active, text)[:limit]
        return [{"id": claim["id"], "text": claim["text"], "status": claim["status"],
                 "refs": [item["ref"] for item in claim["evidence"]], "strength": self.strength(claim, now),
                 **({"history": history} if (history := self.history(claim)) else {})}
                for _, claim in hits]

    def claims_by_id(self, ids):
        """The active claims among ``ids``, in that order."""
        if not isinstance(ids, list) or len(ids) > 64 or not all(isinstance(i, str) for i in ids):
            raise MemoryError("claim ids must be a list of up to 64 strings")
        return [{"id": i, "text": c["text"], "status": c["status"], "refs": [e["ref"] for e in c["evidence"]]}
                for i in ids if (c := self.claims.get(i)) and c["state"] == "active"]

    def day_lessons(self, limit=64):
        """Unverified claims that daydreams added since the last offline dream: tonight's dream checks them."""
        ids = []
        for dream in reversed(self.dreams):
            if not dream.get("live"):
                break
            ids = dream.get("added_ids", []) + ids
        return [c for c in self.claims_by_id(ids[-limit:]) if c["status"] == "uncertain"]

    def search(self, query, limit=20):
        if not isinstance(query, str) or not query.strip() or len(query) > 256 or not 1 <= limit <= 50:
            raise MemoryError("invalid search query or limit")
        if EVIDENCE_ID.fullmatch(query.strip()):
            record = self._records_by_id.get(query.strip())
            if record is not None:
                return [record]
        needle = query.casefold()
        return [record for record in self.transcript
                if any(needle in value.casefold() for value in (record["text"], record.get("run_label", ""), *record.get("tags", [])))][:limit]

    def recall(self, query, limit=3):
        """Rank active claims for ``query``: lexical BM25 over claim text, then salience.

        Deterministic and local.  Returns claim dicts (with ``score``), best first.
        """
        if not isinstance(query, str) or not query.strip() or len(query) > 2_000 or not 1 <= limit <= 50:
            raise MemoryError("invalid recall query or limit")
        # Past claims (true until the world changed) are recalled too, ranked below current ones, so a question
        # about how things were can be answered. Corrected and retracted claims are not: they were wrong.
        candidates = [claim for claim in self.claims.values() if claim["state"] in ("active", "past")]
        if not candidates:
            return []
        contexts = {claim["id"]: _claim_labels(claim) for claim in candidates}
        docs = [_terms(" ".join([claim["text"], *contexts[claim["id"]]])) for claim in candidates]
        scores = {claim["id"]: score for score, claim in _bm25(docs, candidates, query)}
        phrase = query.strip().casefold()
        hits = []
        for claim in candidates:
            score = scores.get(claim["id"], 0) * (1 + 0.1 * claim["salience"]) * (1 if claim["state"] == "active" else 0.5)
            exact = any(phrase == label.casefold() for label in contexts[claim["id"]])
            if exact or score:
                hits.append((exact, score, claim))
        hits.sort(key=lambda item: (-item[0], -item[1], -item[2]["salience"], item[2]["id"]))
        now = _now()
        out = []
        for _, score, claim in hits[:limit]:
            extra = {"strength": self.strength(claim, now)}
            if claim["state"] == "past":
                extra["until"] = self.record_time(claim.get("until_ref"))
            elif history := self.history(claim):
                extra["history"] = history
            out.append({**claim, **extra, "score": round(score, 4)})
        return out

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

    def _index_ranks(self, claims, pages, query):
        prompt = " ".join(query.casefold().split())
        labels = {claim["id"]: _claim_labels(claim) for claim in claims}
        matches = {}
        for label in {label for values in labels.values() for label in values}:
            phrase = " ".join(label.casefold().split())
            # Match a whole context label inside a longer prompt. Give more specific phrases precedence.
            if phrase in prompt and re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", prompt):
                matches[label] = len(phrase.split())
        claim_docs = [_terms(" ".join([c["text"], *labels[c["id"]]])) for c in claims]
        claim_scores = {c["id"]: score for score, c in _bm25(claim_docs, claims, prompt)}
        now = _now()
        activation = {c["id"]: self.strength(c, now)["activation"] for c in claims}
        # A relevant memory that has faded ranks a little lower: ACT-R's retrieval probability scales its score.
        available = {key: 1 / (1 + math.exp(-(value - FAINT_BELOW) / 0.4)) for key, value in activation.items()}
        claim_ranks = {
            c["id"]: (max((matches.get(label, 0) for label in labels[c["id"]]), default=0),
                      claim_scores.get(c["id"], 0) * (1 + 0.1 * c["salience"]) * (0.6 + 0.4 * available[c["id"]]),
                      activation[c["id"]], max(_ref_number(item["ref"]) for item in c["evidence"]), c["salience"], c["id"])
            for c in claims
        }
        page_docs = [_terms(f"{p['title']} {p['title']} {p['slug']} {p['body']}") for p in pages]
        page_scores = {p["slug"]: score for score, p in _bm25(page_docs, pages, prompt)}
        page_ranks = {}
        for page in pages:
            cited = [claim_ranks[ref] for ref in page["claims"]]
            salience = sum(rank[4] for rank in cited) / (len(cited) or 1)
            page_ranks[page["slug"]] = (max((rank[0] for rank in cited), default=0),
                                       page_scores.get(page["slug"], 0) * (1 + 0.1 * salience),
                                       max((rank[2] for rank in cited), default=FAINT_BELOW - 1), salience)
        return claim_ranks, page_ranks

    def action_words(self):
        """The current lexicon: define/revise/retire events from every committed pass, in order."""
        return _action_module().fold(event for dream in self.dreams for event in dream.get("actions", ()))

    def _cache_key(self, kind, head=None):
        if kind not in FAILURE_KINDS:
            raise MemoryError(f"dream kind must be one of {', '.join(FAILURE_KINDS)}")
        transcript_head = self.state["transcript_head"] if head is None else _record_hash(self.transcript[head - 1])
        # A committed pass changes dream_count, so a cache never outlives the attempt it belongs to.
        return {"kind": kind, "transcript_head": transcript_head, "dream_count": self.state["dream_count"]}

    def cache_dream_read(self, read, head=None):
        """Keep one evidence read of the current dream: {kind, tool, args, text, images?: [{ref, sha256}]}."""
        if not isinstance(read, dict) or set(read) - {"kind", "tool", "args", "text", "images"}:
            raise MemoryError("a cached dream read is {kind, tool, args, text, images?}")
        tool, text, images = read.get("tool"), read.get("text"), read.get("images", [])
        if not isinstance(tool, str) or not re.fullmatch(r"somni_[a-z_]{1,40}", tool) or not isinstance(text, str):
            raise MemoryError("a cached dream read needs a Somni tool name and its result text")
        if not isinstance(images, list) or len(images) > MAX_RECORD_IMAGES or not all(
                isinstance(i, dict) and set(i) == {"ref", "sha256"} and isinstance(i["ref"], str)
                and EVIDENCE_ID.fullmatch(i["ref"]) and isinstance(i["sha256"], str) and SHA256.fullmatch(i["sha256"])
                for i in images):
            raise MemoryError("cached dream images are [{ref, sha256}]")
        args = read.get("args")
        if len(canonical(args).encode("utf-8")) > 2_000:
            args = {"omitted": "arguments over 2000 bytes"}
        with self._locked():
            self._load_locked()
            key = self._cache_key(read.get("kind"), head)
            path = self.store / CACHE_FILE
            _no_symlink(path)
            cache = self._read_cache()
            if cache.get("key") != key:
                cache = {"key": key, "started": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"), "reads": []}
            entry = {"tool": tool, "args": args, "text": _clip_record(text, MAX_CACHE_TEXT, b" [cached excerpt truncated]"),
                     **({"images": images} if images else {})}
            if entry not in cache["reads"]:
                cache["reads"] = (cache["reads"] + [entry])[-MAX_CACHE_READS:]
            cache["updated"] = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
            _write_text_atomic(path, json.dumps(cache, ensure_ascii=False) + "\n")
        return len(cache["reads"])

    def _read_cache(self):
        try:
            cache = json.loads((self.store / CACHE_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return cache if isinstance(cache, dict) and isinstance(cache.get("reads"), list) else {}

    def cached_dream_reads(self, kind, max_age_seconds=3600, head=None):
        """The reads an interrupted attempt made on these same records, if it was recent enough; else []."""
        if type(max_age_seconds) is not int or not 0 <= max_age_seconds <= 7 * 86400:
            raise MemoryError("max_age_seconds must be 0-604800")
        self.load()
        cache = self._read_cache()
        if not max_age_seconds or cache.get("key") != self._cache_key(kind, head):
            return []
        try:  # age from the latest read, so an attempt that keeps reading stays fresh
            updated = _dt.datetime.fromisoformat(cache.get("updated") or cache["started"])
        except (KeyError, TypeError, ValueError):
            return []
        if (_dt.datetime.now(_dt.timezone.utc) - updated).total_seconds() > max_age_seconds:
            return []
        return cache["reads"]

    def clear_dream_cache(self):
        try:
            (self.store / CACHE_FILE).unlink()
        except FileNotFoundError:
            pass

    def record_dream_failure(self, failure, context=None):
        """Log a dream attempt that committed nothing: its cause and what it had used.

        The log sits beside the hash-chained logs, not in them: it describes the dreamer, not the subject.
        """
        if not isinstance(failure, dict) or set(failure) - {"cause", "kind", "detail", "reads", "submissions", "images"}:
            raise MemoryError("a dream failure is {cause, kind, detail?, reads?, submissions?, images?}")
        if failure.get("cause") not in FAILURE_CAUSES or failure.get("kind") not in FAILURE_KINDS:
            raise MemoryError(f"dream failure cause must be one of {', '.join(FAILURE_CAUSES)} "
                              f"and kind one of {', '.join(FAILURE_KINDS)}")
        detail = failure.get("detail", "")
        if not isinstance(detail, str):
            raise MemoryError("dream failure detail must be text")
        entry = {"at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"), "cause": failure["cause"],
                 "kind": failure["kind"], "detail": " ".join(detail.split())[:500]}
        for key in ("reads", "submissions", "images"):
            value = failure.get(key, 0)
            if type(value) is not int or not 0 <= value <= 10_000:
                raise MemoryError(f"dream failure {key} must be a count")
            entry[key] = value
        with self._locked():
            self._load_locked()
            entry["transcript_count"] = self.state["transcript_count"]
            if (context or {}).get("run_label") is not None:
                entry["run_label"] = str(context["run_label"])[:200]
            path = self.store / FAILURES_FILE
            _no_symlink(path)
            line = json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n"
            if path.exists() and path.stat().st_size + len(line) > MAX_FAILURES_BYTES:
                # Keep the newest half; a header line carries the count of every entry dropped so far.
                old = self._failure_rows(path.read_text(encoding="utf-8").splitlines())
                kept = old["entries"][len(old["entries"]) // 2:]
                header = {"dropped": old["dropped"] + len(old["entries"]) - len(kept)}
                _write_text_atomic(path, "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
                                                 for row in [header, *kept]))
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
        return entry

    @staticmethod
    def _failure_rows(rows):
        out = {"dropped": 0, "entries": []}
        for row in rows:
            try:
                value = json.loads(row)
            except ValueError:
                continue
            if isinstance(value, dict) and set(value) == {"dropped"} and type(value["dropped"]) is int:
                out["dropped"] += value["dropped"]
            elif isinstance(value, dict) and value.get("cause") in FAILURE_CAUSES:
                out["entries"].append(value)
        return out

    def dream_failures(self):
        """Counts of failed dream attempts by cause (and kind), with the latest one."""
        path = self.store / FAILURES_FILE
        if not path.is_file():
            return {"total": 0, "by_cause": {}, "by_kind": {}}
        rows = self._failure_rows(path.read_text(encoding="utf-8").splitlines())
        by_cause, by_kind = {}, {}
        for entry in rows["entries"]:
            by_cause[entry["cause"]] = by_cause.get(entry["cause"], 0) + 1
            by_kind[entry.get("kind", "dream")] = by_kind.get(entry.get("kind", "dream"), 0) + 1
        return {"total": len(rows["entries"]) + rows["dropped"], "by_cause": dict(sorted(by_cause.items())),
                "by_kind": dict(sorted(by_kind.items())), **({"dropped": rows["dropped"]} if rows["dropped"] else {}),
                **({"last": rows["entries"][-1]} if rows["entries"] else {})}

    def record_action_tools(self, tools):
        """Remember the tools a recording session can call, so later dreams (which may run
        without those tools, e.g. without an MCP server) can write words for them."""
        A = _action_module()
        A.require(isinstance(tools, list) and len(tools) <= MAX_ACTION_TOOLS, f"at most {MAX_ACTION_TOOLS} tools")
        catalog = self._read_action_tools()
        seen = {}
        for tool in tools:
            A.require(isinstance(tool, dict) and isinstance(tool.get("name"), str)
                      and re.fullmatch(r"[A-Za-z_][\w.-]{0,127}", tool["name"]), "each tool needs a valid name")
            if tool["name"].startswith(A.RESERVED_PREFIX):
                continue
            schema = tool.get("parameters") if isinstance(tool.get("parameters"), dict) else {}
            if A.size(schema) > MAX_ACTION_TOOL_SCHEMA_BYTES:
                schema = {"omitted": f"schema over {MAX_ACTION_TOOL_SCHEMA_BYTES} bytes"}
            description = tool.get("description") if isinstance(tool.get("description"), str) else ""
            seen[tool["name"]] = {"description": " ".join(description.split())[:A.MAX_DESCRIPTION],
                                  "parameters": schema}
        if all(catalog.get(name) == entry for name, entry in seen.items()):
            return len(catalog)
        # The latest declaration wins; the least recently declared tools fall off first.
        merged = {name: entry for name, entry in catalog.items() if name not in seen} | seen
        merged = dict(list(merged.items())[-MAX_ACTION_TOOLS:])
        _write_text_atomic(self.store / ACTION_TOOLS_FILE, json.dumps(merged, ensure_ascii=False, indent=1) + "\n")
        return len(merged)

    def _read_action_tools(self):
        path = self.store / ACTION_TOOLS_FILE
        try:
            _no_symlink(path)
            data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def action_tool_catalog(self):
        """Tools that task sessions could call: recorded declarations, plus any other tool
        named in the transcript's tool results (without a schema).  Words are excluded."""
        A = _action_module()
        words = self.action_words()
        catalog = {name: {**entry, "declared": True} for name, entry in self._read_action_tools().items()
                   if isinstance(entry, dict) and name not in words}
        for record in self.transcript:
            match = record.get("source") == "tool_result" and A.RECORD.match(record.get("text", ""))
            if match and match[1] not in catalog and match[1] not in words \
                    and not match[1].startswith(A.RESERVED_PREFIX):
                catalog[match[1]] = {"description": "", "parameters": {}, "declared": False}
        return catalog

    def word_runs(self, names, *, per_word=3, scan=4000):
        """The most recent runs of each named word in the transcript, newest first.

        A run of an older version of the word, or on an older Somni, is marked `stale` with the reason.
        """
        A = _action_module()
        words = self.action_words()
        recent = {name: [] for name in names}
        for record in reversed(self.transcript[-scan:]):
            match = record.get("source") == "tool_result" and A.RECORD.match(record.get("text", ""))
            if not match or match[1] not in recent or len(recent[match[1]]) >= per_word:
                continue
            args, body, value = _word_result(record["text"], match)
            # Convention: a word returns a top-level boolean `confirmed` when a read-back proved its effect (or not).
            confirmed = value.get("confirmed") if isinstance(value, dict) else None
            stale = self._run_staleness(record, words.get(match[1]))
            recent[match[1]].append({"ref": record["id"], "ok": record.get("tool_ok") is not False, "args": args,
                                     "result": body[:300], "confirmed": confirmed if isinstance(confirmed, bool) else None,
                                     **({"calls": record["calls"]} if record.get("calls") else {}),
                                     **({"stale": stale} if stale else {})})
        return recent

    @staticmethod
    def _run_staleness(record, word):
        """Why a word run no longer describes the current word on the current Somni, or None."""
        stamp = record.get("word")
        if not isinstance(stamp, dict):
            return "unstamped run (recorded before runs carried versions)"
        if word is not None and stamp.get("source") != _action_module().source_hash(word["source"]):
            return "older source of this word"
        if stamp.get("somni") != SOMNI_VERSION:
            return f"older Somni ({stamp.get('somni')})"
        return None

    def _check_retire_evidence(self, body, word):
        """A dream may retire a word for failing only on failures of its current source on the current Somni."""
        runs = {run["ref"]: run for run in self.word_runs([body["name"]], per_word=64, scan=len(self.transcript))[body["name"]]}
        failing = {ref: run for ref, run in runs.items() if not run["ok"] or run["confirmed"] is False}
        current = {ref for ref, run in failing.items() if "stale" not in run}
        cited = body.get("runs", [])
        for ref in cited:
            if ref not in runs:
                raise MemoryError(f"{ref} is not a recorded run of {body['name']}")
        if cited:
            if not current & set(cited):
                why = "; ".join(f"{ref}: {runs[ref].get('stale') or 'it succeeded'}" for ref in cited)
                raise MemoryError(f"retire_action for {body['name']} cites no failing run of its current source on the current "
                                  f"Somni ({why}). Those failures may be fixed already: revise the word, or keep it")
        elif failing and not current:
            raise MemoryError(f"{body['name']}'s failed runs ({', '.join(sorted(failing))}) are all from older code "
                              f"({runs[min(failing)].get('stale')}). Those failures may be fixed already: revise the word, "
                              "keep it, or cite a failing run of the current version")
        if not cited and current:
            body["runs"] = sorted(current)[-8:]

    def word_use(self, *, labels=50):
        """How often the agent used its words: word calls (ok, failed, confirmed, unconfirmed), calls a word made,
        and direct calls to the tools words wrap, in total and by run label (the most recent `labels`)."""
        A = _action_module()
        words = self.action_words()
        names = set(words) | {event["name"] for dream in self.dreams for event in dream.get("actions", ())
                              if isinstance(event.get("name"), str)}
        if not names:
            return None
        wrapped = {tool for word in words.values() for tool in word["uses"]} - names
        blank = {"word_calls": 0, "word_failed": 0, "confirmed": 0, "unconfirmed": 0, "calls_made_by_words": 0,
                 "direct_calls_to_wrapped_tools": 0}
        total, by_label = dict(blank), {}
        results = [r for r in self.transcript if r.get("source") == "tool_result"]
        nested = set()
        for i, record in enumerate(results):
            # Older records lack via_word; a word's timing list says how many of the records before it were its calls.
            if record.get("calls") and not any("via_word" in r for r in results[max(0, i - len(record["calls"])):i]):
                nested.update(range(max(0, i - len(record["calls"])), i))
        for i, record in enumerate(results):
            match = A.RECORD.match(record.get("text", ""))
            if not match:
                continue
            name = match[1]
            counts = [total, by_label.setdefault(record["run_label"], dict(blank))] if record.get("run_label") else [total]
            if record.get("via_word") or i in nested:
                key = "calls_made_by_words"
            elif name in names:
                key = "word_calls"
            elif name in wrapped:
                key = "direct_calls_to_wrapped_tools"
            else:
                continue
            for count in counts:
                count[key] += 1
            if key != "word_calls":
                continue
            ok = record.get("tool_ok") is not False
            _, _, value = _word_result(record["text"], match)
            confirmed = value.get("confirmed") if isinstance(value, dict) else None
            for count in counts:
                count["word_failed"] += not ok
                if ok and confirmed is True:
                    count["confirmed"] += 1
                elif ok and confirmed is False:
                    count["unconfirmed"] += 1
        calls = total["word_calls"] + total["direct_calls_to_wrapped_tools"]
        return {**total, "word_share": round(total["word_calls"] / calls, 3) if calls else None,
                "by_run_label": dict(list(by_label.items())[-labels:])}

    def action_workbench(self, *, runs=3, scan=4000):
        """What a dream needs to write or repair words: each word's tests, links, and recent runs."""
        A = _action_module()
        words = self.action_words()
        recent = self.word_runs(words, per_word=runs, scan=scan)
        out = {}
        for name, word in words.items():
            uses, used_by = A.dependencies(name, words)
            out[name] = {"tests": word["tests"], "selftests": word.get("selftests", []), "uses_words": uses,
                         "used_by": used_by, "runs": recent[name]}
        return out

    def write_action_files(self, events=None):
        """Render words as editable files in somni-v1/actions/.

        Words a pass changed are rewritten (all words when the folder is new).  Other files
        get fresh generated headers (e.g. a new "used by") only when their source still
        matches the stored word, so a human edit that has not been synced is never overwritten.
        """
        A = _action_module()
        folder = self.store / ACTIONS_DIR
        _no_symlink(folder)
        words = self.action_words()
        if not os.path.isdir(folder):
            if not words:
                return
            os.mkdir(folder, 0o700)
            events = [{"op": "define", "name": name} for name in words]
        changed = set()
        for event in events or ():
            path = folder / f"{event['name']}.py"
            if event["op"] == "retire":
                if os.path.isfile(path):
                    os.unlink(path)
            elif event["name"] in words:
                _write_text_atomic(path, A.render(words[event["name"]], words))
                changed.add(event["name"])
        for name, word in words.items():
            path = folder / f"{name}.py"
            if name in changed or path.is_symlink() or not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            fresh = A.render(word, words)
            if text != fresh and A.source_of(text) == word["source"]:
                _write_text_atomic(path, fresh)

    def sync_action_files(self, *, reserved=(), allow=None):
        """Record human edits to somni-v1/actions/ as a pass authored by "human".

        A changed file revises its word, a new file defines one, and a deleted file
        retires one.  Edits must pass the syntax policy; tests they break are dropped
        and listed in the pass.  Returns the pass, or None when nothing changed.
        """
        A = _action_module()
        self.load()
        folder = self.store / ACTIONS_DIR
        if not os.path.isdir(folder):
            self.write_action_files()
            return None
        words = self.action_words()
        operations = []
        _no_symlink(folder)
        for path in sorted(folder.glob("*.py")):
            if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_ACTION_FILE_BYTES:
                raise MemoryError(f"{path.name} must be a regular file of at most {MAX_ACTION_FILE_BYTES} bytes")
            source = A.source_of(path.read_text(encoding="utf-8"))
            if path.stem not in words:
                operations.append({"op": "define_action", "source": source, "tests": []})
            elif source != words[path.stem]["source"]:
                operations.append({"op": "revise_action", "source": source})
        for name in sorted(words):
            if not os.path.exists(folder / f"{name}.py"):
                operations.append({"op": "retire_action", "name": name, "reason": "file deleted by a human"})
        if not operations:
            return None
        return self.dream({"schema": SCHEMA, "operations": operations[:32]}, partial=True,
                          actions={"author": "human", "reserved": reserved, "allow": allow})

    def _index_words(self, names, query, budget):
        A = _action_module()
        python = {kind: name for name, kind in A.ANNOTATIONS.items()}
        lexicon = self.action_words()
        words = [lexicon[name] for name in dict.fromkeys(names) if name in lexicon]
        # Most relevant to the prompt first, then the words with the most recent evidence.
        words.sort(key=lambda w: max(w.get("evidence") or [""]), reverse=True)
        if query.strip():
            scores = {id(w): score for score, w in
                      _bm25([_terms(f"{w['name']} {w['name']} {w['description']}") for w in words], words, query)}
            words.sort(key=lambda w: scores.get(id(w), 0), reverse=True)
        lines = []
        runs = self.word_runs([w["name"] for w in words], per_word=10)
        # Agents tend to use words only when told to, so the section opens with an instruction, and names the
        # words whose recent runs all succeeded (confirmed effects first).
        reliable = []
        for word in words:
            done = runs[word["name"]]
            if len(done) >= 2 and all(run["ok"] for run in done):
                confirmed = sum(run["confirmed"] is True for run in done)
                reliable.append((confirmed, len(done), word["name"],
                                 f"{confirmed}/{len(done)} confirmed" if confirmed else f"{len(done)}/{len(done)} ok"))
        reliable.sort(reverse=True)
        best = "; most reliable: " + ", ".join(f"{name} ({score})" for _, _, name, score in reliable[:3]) if reliable else ""
        intro = (f"You have {len(words)} learned action word{'s' if len(words) != 1 else ''}{best}. When one fits the "
                 "next step, call it instead of making its tool calls one by one: it runs the whole sequence in one turn.\n")
        for word in words[:5]:
            declared = A.parse(word["source"])["params"]  # stored params are key-sorted; show the declared order
            params = ", ".join(f"{key}: {python.get(kind, kind)}" for key, kind in declared.items())
            evidence = word.get("evidence") or []
            proof = f"; replays {evidence[0]}..{evidence[-1]}" if len(evidence) > 1 else \
                f"; replays {evidence[0]}" if evidence else "; edited by a human, no tests"
            used_by = A.dependencies(word["name"], lexicon)[1]
            also = f"; used by {', '.join(used_by)}" if used_by else ""
            # The run record keeps a word that works in use after an unrelated failure.
            done = runs[word["name"]]
            failed = sum(not run["ok"] for run in done)
            yes = sum(run["ok"] and run["confirmed"] is True for run in done)
            no = sum(run["ok"] and run["confirmed"] is False for run in done)
            checked = f" ({yes} confirmed, {no} unconfirmed)" if yes or no else ""
            record = (f"last {len(done)} run{'s' if len(done) != 1 else ''}: {len(done) - failed} ok{checked}, {failed} failed"
                      + ("" if not failed else f" (latest {'ok' if done[0]['ok'] else 'failed'})")) if done else "not run yet"
            line = (f"- word `{word['name']}({params})`: {word['description'][:200]} "
                    f"({record}; a tool; calls {', '.join(word['uses'])}{proof}{also})\n")
            size = len(line.encode("utf-8"))
            if size > budget:
                break
            if not lines:
                if size + len(intro.encode("utf-8")) > budget:
                    break
                lines.append(intro)
                budget -= len(intro.encode("utf-8"))
            lines.append(line)
            budget -= size
        return lines

    def inject(self, max_bytes=DEFAULT_INJECTION_BYTES, *, query="", words=None):
        """The bounded index injected before each agent turn.  ``words`` names the action
        words registered as tools in this session; up to five are listed, ranked by the query."""
        _validate_index_request(max_bytes, query)
        header = "# Subject memory index\nClaims are interpretations. Use evidence IDs to inspect the transcript.\n"
        active = [claim for claim in self.claims.values() if claim["state"] == "active"]
        now = _now()
        timing = {claim["id"]: self.describe_time(claim, now) for claim in active}
        if any(timing.values()):
            header += ("Dates say when a memory was last seen or used, or since when it holds. It is still the latest "
                       "you know unless something newer says otherwise: answer with it and give its date.\n")
        lines = [header]
        remaining = max_bytes - len(header.encode("utf-8"))
        if words:
            word_lines = self._index_words(words, query, min(remaining // 4, 1536))
            lines += word_lines
            remaining -= sum(len(line.encode("utf-8")) for line in word_lines)
        page_budget = remaining // 2
        stale = self.stale_pages()
        pages = [page for slug, page in sorted(self.pages.items()) if slug not in stale]
        page_ranks = {}
        if query.strip():
            claim_ranks, page_ranks = self._index_ranks(active, pages, query)
            pages.sort(key=lambda page: page_ranks[page["slug"]], reverse=True)
            active.sort(key=lambda claim: claim_ranks[claim["id"]], reverse=True)
        else:
            now = _now()
            strength = {claim["id"]: self.strength(claim, now)["activation"] for claim in active}
            active.sort(key=lambda claim: (-claim["salience"], -strength[claim["id"]], claim["id"]))

        included_body = False
        for page in pages:
            line = f"- page `{page['slug']}`: {page['title']}\n"
            size = len(line.encode("utf-8"))
            if size > page_budget:
                continue
            rank = page_ranks.get(page["slug"], (0, 0))
            if not included_body and (rank[0] or rank[1]):
                excerpt = _page_excerpt(page["body"], page_budget - size - 3)
                if excerpt:
                    line += f"\n{excerpt}\n\n"
                    size = len(line.encode("utf-8"))
                    included_body = True
            lines.append(line)
            page_budget -= size
            remaining -= size

        omitted = 0
        for claim in active:
            refs = ",".join(item["ref"] for item in claim["evidence"])
            labels = ", ".join(_claim_labels(claim))
            context = f" (context: {labels[:160]})" if labels else ""
            text = claim.get("gist", claim["text"])
            when = f" ({timing[claim['id']]})" if timing[claim["id"]] else ""
            line = f"- {claim['id']} [{claim['status']}; salience={claim['salience']}; evidence={refs}] {text}{when}{context}\n"
            size = len(line.encode("utf-8"))
            if size > remaining:
                omitted += 1
                continue
            lines.append(line)
            remaining -= size
        if omitted:
            suffix = f"- … {omitted} additional active claims omitted; use somni_recall for more.\n"
            if len(suffix.encode("utf-8")) <= remaining:
                lines.append(suffix)
        return "".join(lines)

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

    def status(self):
        active = {status: 0 for status in sorted(STATUSES)}
        for claim in self.claims.values():
            if claim["state"] == "active":
                active[claim["status"]] += 1
        return {
            "phase": self.state["phase"], "transcript_count": self.state["transcript_count"],
            "transcript_head": self.state["transcript_head"], "active_claims_by_status": active,
            "claim_count": len(self.claims), "dream_count": self.state["dream_count"],
            "last_pass": _dream_summary(self.dreams[-1]) if self.dreams else None,
            "action_words": sorted(self.action_words()),
            "segments": {name.split(".")[0]: len(self._segments(name)) for name in SEGMENTED_LOGS},
            "transcript_live_records": self.state["transcript_count"]
            - sum(segment["count"] for segment in self._segments("transcript.jsonl")),
            "limits": {key: self._limit(key) for key in LIMIT_KEYS},
            "dream_failures": self.dream_failures(),
            "forgotten": self.forgotten_counts(),
            "forgetting": {"after_days": (forget := _forget_setting())[0], **({"problem": forget[1]} if forget[1] else {})},
            "claims_by_strength": dict(Counter(self.strength(claim)["level"] for claim in self.claims.values()
                                               if claim["state"] == "active")),
            "past_claims": sum(claim["state"] == "past" for claim in self.claims.values()),
            **({"word_use": use} if (use := self.word_use()) else {}),
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
        lines.extend(["", "Verified describes the evidence check. Assess semantic proof from the cited excerpts and images.", ""])
        return "\n".join(lines)


_STOP = frozenset("""a an and are as at be by can do does for from has have if in into is it its of on or
that the their then this to was were when which while will with you your i me my we our they them""".split())


def _claim_labels(claim):
    return claim.get("run_labels", []) + claim.get("tags", [])


def _page_excerpt(body, max_bytes):
    if len(body.encode("utf-8")) <= max_bytes:
        return body
    marker = "\n…"
    remaining = max_bytes - len(marker.encode("utf-8"))
    if remaining < 24:
        return ""
    headings = list(re.finditer(r"(?m)^ {0,3}(#{1,6})[ \t]+([^\n]+)", body))
    for number, heading in enumerate(headings):
        if re.match(r"(?:procedure|steps|workflow)\b", heading[2], re.IGNORECASE):
            end = next((h.start() for h in headings[number + 1:] if len(h[1]) <= len(heading[1])), len(body))
            body = body[heading.start():end]
            break
    sections = re.split(r"(?m)(?=^ {0,3}#{1,6}[ \t]+)", body)
    sections = [section.strip() for section in sections if section.strip()]
    excerpt = []
    for section in sections:
        size = len(section.encode("utf-8")) + (2 if excerpt else 0)
        if size > remaining:
            break
        excerpt.append(section)
        remaining -= size
    if not excerpt:
        prefix = sections[0].encode("utf-8")[:remaining].decode("utf-8", errors="ignore")
        complete_lines, _, _ = prefix.rpartition("\n")
        if complete_lines and not re.fullmatch(r"#{1,6}[ \t]+[^\n]*", complete_lines.strip()):
            prefix = complete_lines
        excerpt.append(prefix.rstrip())
    return "\n\n".join(excerpt) + marker


def _bm25(docs, items, query):
    q_terms = set(_terms(query))
    if not docs or not q_terms:
        return []
    avg = (sum(len(d) for d in docs) / len(docs)) or 1.0
    counts = [Counter(d) for d in docs]
    df = Counter(term for doc in counts for term in doc.keys() & q_terms)
    scored = []
    for item, doc, frequencies in zip(items, docs, counts):
        score = 0.0
        norm = 1.2 * (0.25 + 0.75 * len(doc) / avg)
        for term in sorted(frequencies.keys() & q_terms):
            tf = frequencies[term]
            idf = math.log(1 + (len(docs) - df[term] + 0.5) / (df[term] + 0.5))
            score += idf * tf * 2.2 / (tf + norm)
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


def _dream_summary(dream):
    # Legacy passes didn't record which note events belonged to them.
    default = 0 if dream["operation_count"] == dream.get("page_operations", 0) else None
    return {"id": f"d{dream['seq']:06d}", "added_count": dream.get("added_count", default),
            **({"action_count": len(dream["actions"])} if dream.get("actions") else {}),
            **({"broken_dependents": broken} if (broken := {k: v for e in dream.get("actions", ())
                                                           for k, v in e.get("broken_dependents", {}).items()}) else {}),
            **({"rejected": dream["rejected"]} if dream.get("rejected") else {}),
            **({"image_downgrades": dream["image_downgrades"]} if dream.get("image_downgrades") else {}),
            **({"live": True} if dream.get("live") else {}),
            **({"usage": dream["usage"]} if dream.get("usage") else {}),
            **({"added_ids": dream["added_ids"]} if dream.get("added_ids") else {}),
            "merged_count": dream.get("merged_count", default), "rejected_count": len(dream.get("rejected", [])),
            "operation_count": dream["operation_count"], "transcript_count": dream["transcript_count"],
            "transcript_head": dream["transcript_head"]}


DAYDREAM_OVERLAP = 8  # records before the last pass's head that a daydream sees again, for context


def _recent(memory, include_images, viewed, *, record_limit=DEFAULT_DREAM_RECORDS,
            record_bytes=DEFAULT_DREAM_RECORD_BYTES, head=None, related=False):
    """The records a dream reviews: the newest `record_limit` up to the head. A daydream (`head` set) starts a few
    records before the last pass's head instead, so it doesn't resend records earlier passes already reviewed."""
    if type(record_limit) is not int or not 1 <= record_limit <= DEFAULT_DREAM_RECORDS:
        raise MemoryError(f"dream record limit must be 1-{DEFAULT_DREAM_RECORDS}")
    if type(record_bytes) is not int or not 64 <= record_bytes <= MAX_RECORD_BYTES:
        raise MemoryError(f"dream record excerpt must be 64-{MAX_RECORD_BYTES} bytes")
    records = memory.transcript if head is None else memory.transcript[:head]
    reviewed = None
    if head is not None and memory.dreams:
        start = min(max(0, memory.dreams[-1]["transcript_count"] - DAYDREAM_OVERLAP), max(0, len(records) - DAYDREAM_OVERLAP))
        if start:
            reviewed = records[start - 1]["id"]  # this record and those before it were reviewed by earlier passes
        records = records[start:]
    records = records[-record_limit:]
    attachments = []
    budget = 1024 * 1024
    viewed.clear()
    if include_images:
        for record in reversed(records):
            for descriptor in record.get("images", []):
                ref, digest = record["id"], descriptor["sha256"]
                if (ref, digest) in viewed or descriptor["bytes"] > budget:
                    continue
                try:
                    image = memory.image(ref, digest)
                except (MemoryError, OSError):
                    continue  # Keep the text and descriptor available for inspection.
                attachments.append({"ref": ref, "sha256": digest, "image": image})
                viewed.add((ref, digest))
                budget -= descriptor["bytes"]
                if len(attachments) == MAX_RECORD_IMAGES:
                    break
            if len(attachments) == MAX_RECORD_IMAGES:
                break
    excerpts = [{**r, "text": _clip_record(r["text"], record_bytes, b" [excerpt truncated]")} for r in records]
    claims = memory.related_claims(records) if related else []
    return {"records": excerpts, "attachments": attachments, **({"reviewed_through": reviewed} if reviewed else {}),
            **({"claims": claims} if claims else {})}


def _action_module():
    root = str(pathlib.Path(__file__).resolve().parent.parent)
    if root not in sys.path:
        sys.path.insert(0, root)
    from somni import actions
    return actions


def _handle_request(memory, method, args, *, writable, context=None, image_bytes=DEFAULT_IMAGE_BYTES,
                    resize=None, viewed=None, actions=False, daydream=None, contributor=False):
    if daydream is not None and method in {"phase", "record", "note", "wiki", "action_tools"}:
        raise MemoryError("a daydream may only read memory and submit its pass")
    # A contributor (a second Pi on the store, such as a sub-agent) may append records; the owner keeps the rest.
    if method in {"phase", "record", "note", "dream", "recent", "wiki"} and not writable \
            and not (contributor and method == "record"):
        raise MemoryError("Somni memory changes require the session that owns recording")
    if method == "phase":
        memory.set_phase(args["phase"], allow_same=True)
    elif method == "record":
        index_bytes = args.get("index_bytes")
        query = args.get("query", "")
        if index_bytes is not None:
            _validate_index_request(index_bytes, query)
        images, omitted = args.get("images", []), args.get("omitted_images", 0)
        if not isinstance(images, list):
            raise MemoryError("invalid tool result image list")
        ref = memory.record(args["source"], args["text"], context=context, images=images, omitted_images=omitted,
                            clip=True, image_bytes=image_bytes, resize=resize, tool_ok=args.get("tool_ok"),
                            calls=args.get("calls"), via_word=args.get("via_word"),
                            word_stamp=_word_stamp(memory, args) if actions and args.get("word") is True else None)
        return ref if index_bytes is None else memory.inject(index_bytes, query=query,
                                                            words=_index_word_names(args) if actions else None)
    elif method == "note":
        if len(canonical(args).encode("utf-8")) > MAX_INPUT_BYTES:
            raise MemoryError("note input exceeds byte limit")
        return memory.add_note(args["text"], args["status"], args["evidence"], args["salience"], gist=args.get("gist"))
    elif method == "dream":
        proposal = args["proposal_json"]
        if not isinstance(proposal, str) or len(proposal.encode()) > MAX_INPUT_BYTES:
            raise MemoryError("dream proposal exceeds byte limit")
        options = None
        if actions:
            options = _dream_actions(args)
            options["reserved"] = sorted(set(options["reserved"]) | set(memory.load().action_tool_catalog()))
        try:
            parsed = json.loads(proposal)
        except ValueError as exc:
            raise MemoryError(f"proposal_json is not valid JSON ({exc}); nothing committed") from None
        # A deep-sleep run commits while its session keeps recording: a live pass bound to the current head.
        live = memory.load().state["transcript_count"] if args.get("live") is True and writable and daydream is None else None
        dream = memory.dream(parsed, partial=True, allow_empty=True, viewed_images=viewed,
                             actions=options, snapshot=daydream if daydream is not None else live, usage=args.get("usage"),
                             lenient_images=args.get("lenient_images") is True)
        memory.clear_dream_cache()
        return {**dream, **_dream_summary(dream)}
    elif method == "inject":
        return memory.load().inject(args["index_bytes"], query=args.get("query", ""),
                                    words=_index_word_names(args) if actions else None)
    elif method == "search":
        return memory.load().search(args["query"], args["limit"])
    elif method == "claims":
        return memory.load().claims_by_id(args.get("ids", []))
    elif method == "day_lessons":
        return memory.load().day_lessons()
    elif method == "recall":
        memory.load()
        query, limit = args["query"], args["limit"]
        return memory.recall_pages(query, limit) + memory.recall(query, limit)
    elif method == "action_list" and actions:
        return [{key: word[key] for key in ("name", "description", "params", "uses", "source")}
                for word in memory.load().action_words().values()]
    elif method == "action_tools" and actions:
        if not writable:
            raise MemoryError("Somni memory changes require the session that owns recording")
        return memory.load().record_action_tools(args.get("tools"))
    elif method == "status":
        return memory.load().status()
    elif method == "action_catalog" and actions:
        A = _action_module()
        catalog = memory.load().action_tool_catalog()
        live = args.get("live") or []
        if not (isinstance(live, list) and len(live) <= MAX_ACTION_TOOLS and all(isinstance(t, dict) for t in live)):
            raise MemoryError("live must be a list of tool declarations")
        for tool in live:
            if isinstance(tool.get("name"), str):
                catalog[tool["name"]] = {"description": str(tool.get("description") or "")[:A.MAX_DESCRIPTION],
                                         "parameters": tool.get("parameters") if isinstance(tool.get("parameters"), dict)
                                         else {}, "declared": True, "live": True}
        for tool, entry in catalog.items():
            entry["signature"] = A.signature(tool, entry["parameters"] if entry["declared"] else None,
                                             entry["description"] or ("" if entry["declared"] else
                                                                      "seen in the transcript; see its recorded calls"))
        return catalog
    elif method == "action_workbench" and actions:
        return memory.load().action_workbench()
    elif method == "action_try" and actions:
        return _action_try(memory.load(), args)
    elif method == "action_candidates" and actions:
        memory.load()
        return _action_module().candidates(memory.transcript, memory.action_words())
    elif method == "action_step" and actions:
        words = memory.load().action_words()
        if args.get("name") not in words:
            raise MemoryError(f"unknown action word {args.get('name')}")
        A = _action_module()
        # Declared tools only: transcript-derived names change while a word runs, and dir() must replay identically.
        return A.step(A.shared_runtime(words), words[args["name"]], args["params"], args["history"],
                      max_calls=args.get("max_calls", A.MAX_CALLS),
                      catalog=A.namespace(memory._read_action_tools(), words))
    elif method == "action_draft_step" and actions:
        # Deep sleep: step a draft word (not yet stored) against the session's real tools, as action_step does a word.
        A = _action_module()
        words = memory.load().action_words()
        source = A.normalize(args.get("source"))
        draft = {**A.parse(source), "source": source}
        merged = {**words, draft["name"]: draft}
        return A.step(A.shared_runtime(merged), draft, args["params"], args["history"],
                      max_calls=args.get("max_calls", A.MAX_CALLS), catalog=A.namespace(memory._read_action_tools(), merged))
    elif method == "records_since":
        memory.load()
        start = args.get("count")
        if type(start) is not int or not 0 <= start <= memory.state["transcript_count"]:
            raise MemoryError("count must be a transcript count")
        return memory.transcript[start:start + 64]
    elif method == "recent":
        return _recent(memory.load(), args.get("include_images", False), viewed,
                       record_limit=args.get("record_limit", DEFAULT_DREAM_RECORDS),
                       record_bytes=args.get("record_bytes", DEFAULT_DREAM_RECORD_BYTES), head=daydream,
                       related=args.get("related") is True)
    elif method == "image":
        ref = args["ref"]
        memory.load()
        digest = memory._image_descriptor(ref, args["sha256"])["sha256"]
        if args.get("during_dream"):
            cap = args.get("max_images", DEFAULT_DREAM_IMAGES)
            if type(cap) is not int or not 0 <= cap <= MAX_DREAM_IMAGES:
                raise MemoryError(f"max_images must be 0-{MAX_DREAM_IMAGES}")
            if len(viewed) >= cap and (ref, digest) not in viewed:
                raise MemoryError(f"this dream reached its image limit ({cap} images). Cite the images you have "
                                  "already viewed, use text quotes, or submit the pass now")
            viewed.add((ref, digest))
        image = memory.image(ref, digest)
        return {"sha256": digest, "image": image} if args.get("resolve") else image
    elif method == "dream_images":
        return len(viewed)
    elif method == "dream_failure":
        return memory.record_dream_failure(args, context)
    elif method == "dream_cache_put":
        return memory.cache_dream_read(args, head=daydream)
    elif method == "dream_cache_take":
        return memory.cached_dream_reads(args.get("kind"), args.get("max_age_seconds", 3600), head=daydream)
    elif method == "wiki":
        # With python -I, explicitly add this package's root for the formatter.
        root = str(pathlib.Path(__file__).resolve().parent.parent)
        if root not in sys.path:
            sys.path.insert(0, root)
        from somni.dream import WIKI_LINK_GUIDE, _uncovered, page_guide, render_pages

        viewed.clear()
        memory.load()
        uncovered = _uncovered(memory)[:120]
        return {
            "pages": render_pages(memory), "guide": f"{page_guide(args.get('page_guide'))} {WIKI_LINK_GUIDE}", "n": len(uncovered),
            "claims": "\n".join(f"[{claim['id']}] {claim['text']}" for claim in uncovered),
        }
    else:
        raise MemoryError(f"unknown Somni backend method: {method}")


def _action_try(memory, args):
    """Dry-run the action gate (or inspect rpc) for a dream; nothing is committed."""
    A = _action_module()
    words = memory.action_words()
    space = A.namespace(memory.action_tool_catalog(), words)
    if args.get("inspect"):
        return {"inspect": A.inspect(args["inspect"], space)}
    options = _dream_actions(args)
    try:
        source = A.normalize(args.get("source"))
        name = A.parse(source)["name"]
        replacing = name if name in words else None
        tests = args.get("tests")
        if tests is None:
            tests = words[name]["tests"] if replacing else []
        report = []
        word, _ = A.build(source, tests, memory._records_by_id, words, allow=options["allow"],
                          reserved=set(options["reserved"]) | set(memory.action_tool_catalog()),
                          replacing=replacing, catalog=space, report=report)
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        return {"ok": False, "error": str(exc)[:800]}
    return {"ok": True, "would": "revise" if replacing else "define", "name": word["name"],
            "params": word["params"], "uses": word["uses"], "evidence": word["evidence"], "results": report,
            **({"broken_dependents": word["broken_dependents"]} if word.get("broken_dependents") else {})}


def _index_word_names(args):
    names = args.get("action_words") or []
    if not (isinstance(names, list) and len(names) <= 64 and all(isinstance(n, str) for n in names)):
        raise MemoryError("action_words must be a list of at most 64 tool names")
    return names


def _dream_actions(args):
    allow = args.get("action_allow")
    if allow is not None and not (isinstance(allow, list) and all(isinstance(n, str) for n in allow)):
        raise MemoryError("action_allow must be a list of tool names")
    reserved = args.get("tools", [])
    if not (isinstance(reserved, list) and all(isinstance(n, str) for n in reserved)):
        raise MemoryError("tools must be a list of tool names")
    return {"author": "dream", "allow": allow, "reserved": reserved}


def _serve(memory, record, *, context, image_bytes, image_max_edge, actions=False, daydream=False,
           session_label=None):
    def reply(request_id, **fields):
        print(json.dumps({"id": request_id, **fields}), flush=True)

    resize = None
    if not 0 <= image_bytes <= MAX_IMAGE_BYTES or not 0 <= image_max_edge <= 4096:
        raise MemoryError(f"image byte cap must be 0..{MAX_IMAGE_BYTES} and max edge must be 0..4096")
    if image_bytes and image_max_edge:
        try:
            from PIL import Image
        except ImportError as exc:
            raise MemoryError("SOMNI_IMAGE_MAX_EDGE requires Pillow in SOMNI_PYTHON") from exc
        resize = (Image, image_max_edge)
    viewed = set()
    if daydream:
        # A daydream backend takes no lease: the live session keeps recording. It reads up to the head
        # it saw at start and may write only its dream pass, under the store lock, bound to that head.
        memory = SomniMemory(memory.workspace, memory.subject_id, scheduled_verification=True)
        head = memory.load().state["transcript_count"]
        if not head:
            raise MemoryError("nothing to daydream about: the transcript is empty")
        reply(0, result={"writable": True, "daydream_head": head})
        while line := sys.stdin.buffer.readline(MAX_BACKEND_REQUEST + 1):
            if len(line) > MAX_BACKEND_REQUEST:
                raise MemoryError("Somni backend request exceeds byte limit")
            request_id = None
            try:
                request = json.loads(line)
                request_id = request["id"]
                reply(request_id, result=_handle_request(memory, request["method"], request["args"], writable=True,
                                                         context=context, image_bytes=image_bytes, resize=resize,
                                                         viewed=viewed, actions=actions, daydream=head))
            except (ValueError, OSError, KeyError, TypeError, RecursionError, OverflowError) as exc:
                reply(request_id, error=str(exc))
        return
    with _recording_lease(memory) as owner:
        if owner is not False and (not os.path.lexists(memory.store) or _seeded_only(memory.store)):
            SomniMemory.create(memory.workspace, memory.subject_id, "pi", "session")
        memory = SomniMemory(memory.workspace, memory.subject_id, scheduled_verification=True)
        synced = None
        if owner and record and actions:
            memory.load()
            if memory.state["phase"] == "closed":
                try:
                    synced = memory.sync_action_files()
                    synced = synced and {**_dream_summary(synced), "author": "human"}
                except (ValueError, OSError) as exc:
                    synced = {"error": str(exc)[:600]}
        if owner and record:
            memory.set_phase("open", allow_same=True)
        else:
            memory.load()
        # Another session owns recording: this one contributes its records, tagged so dreams can tell them apart.
        contributor = bool(record and owner is False)
        if contributor:
            tags = [*context.get("tags", []), f"contributor:{session_label}" if session_label else "contributor"]
            context = _record_context(context.get("run_label"), tags[-8:])
        try:
            reply(0, result={"writable": owner is not False, **({"contributor": True} if contributor else {}),
                             **({"action_sync": synced} if synced else {})})
            while line := sys.stdin.buffer.readline(MAX_BACKEND_REQUEST + 1):
                if len(line) > MAX_BACKEND_REQUEST:
                    raise MemoryError("Somni backend request exceeds byte limit")
                request_id = None
                try:
                    request = json.loads(line)
                    request_id = request["id"]
                    result = _handle_request(memory, request["method"], request["args"], writable=owner, context=context,
                                             image_bytes=image_bytes, resize=resize, viewed=viewed,
                                             actions=actions, contributor=contributor)
                    reply(request_id, result=result)
                except (ValueError, OSError, KeyError, TypeError, RecursionError, OverflowError) as exc:
                    reply(request_id, error=str(exc))
        finally:
            if owner and record:
                memory.set_phase("closed", allow_same=True)


def _validate_index_request(bound, query=""):
    if type(bound) is not int or not 256 <= bound <= MAX_INJECTION_BYTES:
        raise MemoryError(f"injection bound must be 256-{MAX_INJECTION_BYTES} bytes")
    if not isinstance(query, str):
        raise MemoryError("injection query must be text")


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
    serve.add_argument("--actions", action="store_true")
    serve.add_argument("--daydream", action="store_true", help="read-only except for one live dream pass; no lease")
    serve.add_argument("--record", action="store_true")
    serve.add_argument("--run-label")
    serve.add_argument("--tags", default="[]")
    serve.add_argument("--session-label", help="tag for this session's records when another session owns recording")
    serve.add_argument("--image-bytes", type=int, default=DEFAULT_IMAGE_BYTES)
    serve.add_argument("--image-max-edge", type=int, default=0)
    record = commands.add_parser("record"); record.add_argument("--source", choices=sorted(SOURCES), required=True); record.add_argument("--text", required=True)
    record.add_argument("--inject-max-bytes", type=int, help="emit the updated index instead of the record ID")
    note = commands.add_parser("note"); note.add_argument("--input", required=True, help="claim JSON relative to workspace, or - for stdin")
    dream = commands.add_parser("dream"); dream.add_argument("--input", required=True, help="proposal JSON relative to workspace, or - for stdin")
    search = commands.add_parser("search"); search.add_argument("query"); search.add_argument("--limit", type=int, default=20)
    inject = commands.add_parser("inject"); inject.add_argument("--max-bytes", type=int, default=DEFAULT_INJECTION_BYTES)
    inject.add_argument("--query", default="", help="rank the index for a prompt")
    commands.add_parser("report"); commands.add_parser("metrics")
    commands.add_parser("status").add_argument("--json", action="store_true", help="emit the machine-readable status")
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
            _serve(memory, args.record, context=_record_context(args.run_label, json.loads(args.tags)),
                   image_bytes=args.image_bytes, image_max_edge=args.image_max_edge,
                   actions=args.actions, daydream=args.daydream, session_label=args.session_label)
        elif args.command == "session-open":
            memory.set_phase("open"); print("LAYERED_MEMORY_SESSION_OPEN")
        elif args.command == "session-close":
            memory.set_phase("closed"); print("LAYERED_MEMORY_SESSION_CLOSED")
        elif args.command == "record":
            bound = args.inject_max_bytes
            if bound is not None:
                _validate_index_request(bound)
            record = memory.record(args.source, args.text)
            if bound is None:
                print(record)
            else:
                sys.stdout.write(memory.inject(bound))
        elif args.command == "note":
            claim = _input_json(memory, args.input)
            if not isinstance(claim, dict) or set(claim) - {"text", "gist", "status", "evidence", "salience", "links"}:
                raise MemoryError("note input has invalid fields")
            print(memory.add_note(claim.get("text"), claim.get("status"), claim.get("evidence"), claim.get("salience", 3),
                                  claim.get("links", []), gist=claim.get("gist")))
        elif args.command == "dream":
            print(canonical(memory.dream(_input_json(memory, args.input))))
        elif args.command == "search":
            print(json.dumps(memory.load().search(args.query, args.limit), ensure_ascii=False, indent=2, sort_keys=True))
        elif args.command == "inject":
            sys.stdout.write(memory.load().inject(args.max_bytes, query=args.query))
        elif args.command == "report":
            sys.stdout.write(memory.load().report())
        elif args.command == "metrics":
            print(json.dumps(memory.load().metrics(), indent=2, sort_keys=True))
        elif args.command == "status":
            print(json.dumps(memory.load().status(), ensure_ascii=False, sort_keys=True, indent=None if args.json else 2))
        return 0
    except (MemoryError, OSError, KeyError, json.JSONDecodeError) as exc:
        print(f"LAYERED_MEMORY_ERROR {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
