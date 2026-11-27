"""Snapshots: one memory store as a single file, to move between machines and containers.

A snapshot is a gzip tar holding `somni-snapshot.json` and the store's `somni-v1/` files. It is taken under the
store's own lock, so a session may keep recording meanwhile, and only after the hash chains verify. The manifest
lists every file's SHA-256 and the chains' heads. Lock files and caches are left out: they belong to the machine.

Restore checks every file against the manifest, refuses links and paths outside `somni-v1/`, recreates the lock
file, verifies the chains in their new place and only then moves the store into position. It will not overwrite a
store that holds memory unless told to, and then it keeps the old one beside it.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import os
import pathlib
import shutil
import tarfile
import tempfile

FORMAT = "somni-snapshot/1"
MANIFEST = "somni-snapshot.json"
STORE_DIR = "somni-v1"
SKIP_FILES = {"memory.lock", "dream-cache.json"}
SKIP_DIRS = {"forks"}
MAX_FILES = 100_000


class SnapshotError(ValueError):
    pass


def _version():
    try:
        return json.loads((pathlib.Path(__file__).resolve().parent.parent / "package.json").read_text())["version"]
    except (OSError, ValueError, KeyError):
        return "unknown"


def _heads(memory):
    state = memory.state
    return {key: state.get(key) for key in ("transcript_count", "transcript_head", "note_events", "notes_head",
                                              "dream_count", "dreams_head") if key in state}


def _files(store: pathlib.Path):
    for path in sorted(store.rglob("*")):
        rel = path.relative_to(store)
        if path.is_symlink():
            raise SnapshotError(f"the store holds a link, which a snapshot won't follow: {rel}")
        if path.is_dir() or rel.parts[0] in SKIP_DIRS or path.name in SKIP_FILES or path.name.endswith(".tmp"):
            continue
        yield rel, path


def snapshot(memory, out: pathlib.Path) -> dict:
    """Write `memory`'s store to `out` (a new .tar.gz). Returns the manifest."""
    out = pathlib.Path(out)
    if out.exists():
        raise SnapshotError(f"{out} already exists; snapshot won't overwrite it")
    store = memory.store
    with memory._locked():  # writers commit under this lock: the copy is one consistent state
        memory._load_locked(force=True)
        contents = {str(rel): path.read_bytes() for rel, path in _files(store)}
        manifest = {"format": FORMAT, "somni": _version(), "subject": memory.subject_id,
                    "created": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "state": _heads(memory),
                    "files": {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()}}
    out.parent.mkdir(parents=True, exist_ok=True)
    partial = out.with_name(out.name + ".partial")
    try:
        with tarfile.open(partial, "w:gz") as tar:
            def add(name, data):
                info = tarfile.TarInfo(name)
                info.size, info.mode, info.mtime = len(data), 0o600, 0
                tar.addfile(info, io.BytesIO(data))
            add(MANIFEST, json.dumps(manifest, indent=2, sort_keys=True).encode())
            for name, data in contents.items():
                add(f"{STORE_DIR}/{name}", data)
        os.replace(partial, out)
    finally:
        partial.unlink(missing_ok=True)
    return manifest


def _read(path: pathlib.Path):
    """The manifest and the store files of a snapshot, checked member by member."""
    try:
        tar = tarfile.open(path, "r:gz")
    except (OSError, tarfile.TarError) as exc:
        raise SnapshotError(f"{path} is not a Somni snapshot ({exc})") from None
    with tar:
        members = tar.getmembers()
        if len(members) > MAX_FILES:
            raise SnapshotError("the snapshot holds too many files")
        files, manifest = {}, None
        for member in members:
            name = member.name
            parts = pathlib.PurePosixPath(name).parts
            if not member.isfile() or name.startswith("/") or ".." in parts or "\\" in name:
                raise SnapshotError(f"unsafe entry in the snapshot: {name}")
            data = tar.extractfile(member).read()
            if name == MANIFEST:
                manifest = json.loads(data)
            elif parts[0] == STORE_DIR and len(parts) > 1:
                files["/".join(parts[1:])] = data
            else:
                raise SnapshotError(f"unexpected entry in the snapshot: {name}")
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise SnapshotError(f"{path} has no {FORMAT} manifest")
    listed = manifest.get("files") or {}
    if set(listed) != set(files):
        missing, extra = sorted(set(listed) - set(files)), sorted(set(files) - set(listed))
        raise SnapshotError(f"the snapshot's files don't match its manifest (missing {missing[:3]}, extra {extra[:3]})")
    for name, data in files.items():
        if hashlib.sha256(data).hexdigest() != listed[name]:
            raise SnapshotError(f"{name} doesn't match its hash in the manifest: the snapshot is damaged")
    return manifest, files


def describe(path: pathlib.Path) -> dict:
    manifest, files = _read(pathlib.Path(path))
    return {**{k: manifest[k] for k in ("format", "somni", "subject", "created", "state")}, "files": len(files)}


def restore(path: pathlib.Path, workspace: pathlib.Path, open_memory, *, replace=False) -> dict:
    """Restore a snapshot into `workspace` (the directory that holds somni-v1/). `open_memory(workspace, subject)`
    returns a SomniMemory. Returns a summary."""
    manifest, files = _read(pathlib.Path(path))
    workspace = pathlib.Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    target = workspace / STORE_DIR
    kept = None
    if target.exists():
        if _holds_memory(target) and not replace:
            raise SnapshotError(f"{workspace} already holds a memory store; pass --replace to keep it aside and "
                                "restore over it")
    staging = pathlib.Path(tempfile.mkdtemp(prefix=".somni-restore-", dir=workspace))
    try:
        store = staging / STORE_DIR
        for name, data in files.items():
            dest = store / name
            dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
        os.chmod(store, 0o700)
        (store / "memory.lock").touch(mode=0o600)
        memory = open_memory(staging, manifest["subject"])
        memory.load()  # the chains verify in their new place before anything moves
        if _heads(memory) != manifest["state"]:
            raise SnapshotError("the restored store's chains don't match the snapshot's manifest")
        if target.exists():
            kept = workspace / f"{STORE_DIR}.before-restore-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}"
            os.replace(target, kept)
        os.replace(store, target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return {"restored": str(target), "subject": manifest["subject"], "state": manifest["state"],
            "files": len(files), "from_somni": manifest["somni"], **({"kept_aside": str(kept)} if kept else {})}


def _holds_memory(store: pathlib.Path) -> bool:
    """A store a session merely opened (no records, no claims) can be restored over."""
    try:
        state = json.loads((store / "state.json").read_text())
    except (OSError, ValueError):
        return any(store.iterdir())
    return bool(state.get("transcript_count") or state.get("note_events") or state.get("dream_count"))
