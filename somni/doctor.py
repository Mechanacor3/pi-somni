#!/usr/bin/env python3
"""somni doctor: check Somni's settings, runtime and memory store, offline.

It reads the config files and the environment, checks the Python backend and the store, and
compares the features you turned on with what the store holds, so a change such as enabling
action words comes with what else it needs.  It makes no model calls and changes nothing: a
store with an interrupted write is reported, not repaired (the next Pi session recovers it).

    python3 somni/doctor.py [--cwd DIR] [--all] [--json]

Exit status: 0 when there are no errors, 1 otherwise.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from somni import config as C  # noqa: E402

SHELL_TOOLS = {"bash", "sh", "shell", "zsh", "powershell", "pwsh", "cmd", "exec", "run_command", "terminal"}
LEVELS = ("error", "warn", "info", "ok")


class Report:
    def __init__(self):
        self.items = []

    def add(self, level, area, message, fix=None):
        self.items.append({"level": level, "area": area, "message": message, **({"fix": fix} if fix else {})})

    def count(self, level):
        return sum(1 for item in self.items if item["level"] == level)


def _short(path):
    home = os.path.expanduser("~")
    text = str(path)
    return "~" + text[len(home):] if home and home != "/" and text.startswith(home + os.sep) else text


def check_config(report, resolved):
    found = [f for f in resolved["files"] if f["exists"]]
    for f in resolved["files"]:
        if f["exists"]:
            report.add("ok", "config", f"{_short(f['path'])} ({f['label']} file, {f['settings']} setting(s))")
    if not found and not any(f["label"] == "SOMNI_CONFIG" for f in resolved["files"]):
        user = next((f["path"] for f in resolved["files"] if f["label"] == "user"), None)
        report.add("info", "config", "no config file; every setting comes from the environment or its default",
                   f"`{_short(ROOT / 'bin' / 'somni')} config template` prints one listing every setting; save it as "
                   f"{_short(user)} or .pi/somni.conf" if user else None)
    for problem in resolved["errors"]:
        where = f"{_short(problem['file'])}:{problem['line']}" if problem.get("line") else _short(problem.get("file") or "env")
        report.add("error", "config", f"{where}: {problem['message']}", "this setting keeps its previous value until fixed")
    for problem in resolved["warnings"]:
        where = (f"{_short(problem['file'])}:{problem['line']}: " if problem.get("line")
                 else f"{_short(problem['file'])}: " if problem.get("file") else "")
        report.add("warn", "config", where + problem["message"])


def check_runtime(report, values, live):
    python = values["store.python"]
    exe = shutil.which(python) if not os.path.isabs(python) else (python if os.path.exists(python) else None)
    if not exe:
        report.add("error", "runtime", f"backend Python {python!r} was not found", "set store.python to a Python 3.10+ interpreter")
        return
    try:
        out = subprocess.run([exe, "-I", "-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])"],
                             capture_output=True, text=True, timeout=20)
        version = out.stdout.strip()
        major, minor = (int(x) for x in version.split(".")[:2])
    except (OSError, ValueError, subprocess.SubprocessError):
        report.add("error", "runtime", f"could not run backend Python {exe}")
        return
    if (major, minor) < (3, 10):
        report.add("error", "runtime", f"backend Python {exe} is {version}; Somni needs 3.10 or newer",
                   "set store.python to a newer interpreter")
    else:
        report.add("ok", "runtime", f"backend Python {version} ({exe})")
    if values["recording.image_max_edge"]:
        pil = subprocess.run([exe, "-I", "-c", "import PIL; print(PIL.__version__)"], capture_output=True, text=True, timeout=20)
        if pil.returncode:
            report.add("error", "runtime", "recording.image_max_edge needs Pillow, which the backend Python lacks",
                       f"`{exe} -m pip install pillow`, or set recording.image_max_edge: 0")
        else:
            report.add("ok", "runtime", f"Pillow {pil.stdout.strip()} for thumbnails")
    if not live:
        pi = shutil.which("pi")
        report.add("ok" if pi else "info", "runtime", f"pi on PATH ({pi})" if pi else "pi is not on PATH; fine if you start it another way")


def check_store(report, values, cwd, session=None):
    """Returns (memory or None, store dir)."""
    workspace = pathlib.Path(values["store.path"])
    store = workspace / "somni-v1"
    default = pathlib.Path(cwd) / ".pi" / "somni"
    if workspace.resolve() != default.resolve() and (default / "somni-v1").is_dir():
        report.add("warn", "store", f"store.path points at {_short(workspace)}, but {_short(default)} also holds a store; "
                   "sessions here no longer use it", "move it there, or set store.path back")
    if not store.is_dir():
        report.add("info", "store", f"no store yet at {_short(workspace)}; the first Pi session creates it")
        return None, store
    _git_check(report, workspace)
    if (store / ".transaction.json").exists():
        report.add("warn", "store", "an interrupted write is pending; the next Pi session recovers it",
                   "start and close one Pi session, then run doctor again")
        return None, store
    try:
        manifest = json.loads((store / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        report.add("error", "store", f"cannot read the store manifest: {exc}")
        return None, store
    subject = values["store.subject"]
    if manifest.get("subject_id") != subject:
        report.add("error", "store", f"the store belongs to subject {manifest.get('subject_id')!r}, but store.subject is {subject!r}",
                   f"set store.subject: {manifest.get('subject_id')}, or point store.path at another store")
        return None, store
    from somni.store import SomniError, SomniMemory
    try:
        memory = SomniMemory(workspace, subject).load()
        status = memory.status()
    except (SomniError, OSError, ValueError) as exc:
        report.add("error", "store", f"the store failed verification: {exc}", "keep a copy before changing anything in it")
        return None, store
    claims = sum(status["active_claims_by_status"].values())
    report.add("ok", "store", f"{_short(workspace)}: verified; {status['transcript_count']} records, {claims} active claims, "
               f"{len(memory.pages)} pages, {status['dream_count']} dreams, {len(status['action_words'])} action words")
    limit = status["limits"].get("max_transcript_records")
    if limit and status["transcript_count"] > 0.9 * limit:
        report.add("warn", "store", f"the transcript holds {status['transcript_count']} of {limit} records",
                   "raise max_transcript_records in manifest.json limits, or archive the store")
    failures = status.get("dream_failures") or {}
    if failures.get("total"):
        causes = ", ".join(f"{k} {v}" for k, v in sorted(failures["by_cause"].items()))
        report.add("info", "store", f"{failures['total']} dream attempt(s) committed nothing ({causes}); "
                   "see somni-v1/dream-failures.jsonl")
    if status["phase"] == "open" and not (session or {}).get("writable"):
        report.add("info", "store", "a session is open on this store (recording now, or a session ended without closing)")
    return memory, store


def _git_check(report, workspace):
    git = shutil.which("git")
    if not git:
        return
    inside = subprocess.run([git, "-C", str(workspace), "rev-parse", "--is-inside-work-tree"],
                            capture_output=True, text=True, timeout=20)
    if inside.returncode or inside.stdout.strip() != "true":
        return
    ignored = subprocess.run([git, "-C", str(workspace), "check-ignore", "-q", "somni-v1"], capture_output=True, timeout=20)
    if ignored.returncode == 1:
        report.add("warn", "store", f"{_short(workspace)} is inside a Git repository and not ignored; it holds what the "
                   "agent saw, which can include file contents and command output",
                   f"add {_short(workspace)}/ to .gitignore")


def check_features(report, values, memory, store, session):
    from somni import actions as A
    words = memory.action_words() if memory else {}
    try:
        catalog = set(memory.action_tool_catalog()) if memory else set()
    except Exception:  # an older or damaged cache must not stop the report
        catalog = set(memory._read_action_tools()) if memory else set()
    live_tools = set(session.get("tools") or [])
    seen = catalog | live_tools
    allow = values["actions.tools"]
    if not values["store.record"]:
        report.add("info", "features", "store.record is false: sessions use memory but record nothing")
    if values["actions.enabled"]:
        if allow is None:
            shells = sorted(SHELL_TOOLS & seen)
            if shells:
                report.add("warn", "features", f"action words may call any tool, including {', '.join(shells)}",
                           "set actions.tools to the tools words may call, such as [read, grep]")
            else:
                report.add("ok", "features", "action words on; words may call any non-Somni tool")
        elif not allow:
            report.add("warn", "features", "actions.tools is an empty list, so words can call no tool",
                       "list the tools words may call, or remove actions.tools to allow all")
        else:
            unknown = sorted(set(allow) - seen) if seen else []
            if unknown:
                report.add("warn", "features", f"actions.tools names tools no recorded session offered: {', '.join(unknown)}",
                           "check the spelling; MCP tools are named mcp__SERVER__TOOL")
            else:
                report.add("ok", "features", f"action words on, limited to {len(allow)} tool(s)")
        if memory and not catalog:
            report.add("info", "features", "no tool calls recorded yet; dreams write words from recorded sessions")
        if session and "read" not in live_tools and live_tools:
            report.add("info", "features", "the read tool is off, so Pi doesn't list Somni's skills; the index carries the "
                       "short word guide instead")
    elif words:
        report.add("info", "features", f"the store has {len(words)} action word(s), not loaded because actions.enabled is false",
                   "set actions.enabled: true to use them")
    folder = store / "actions"
    if folder.is_dir():
        for path in sorted(folder.glob("*.py")):
            try:
                info = A.parse(path.read_text(encoding="utf-8"))
                if info["name"] != path.stem:
                    report.add("error", "features", f"actions/{path.name} defines {info['name']}; the file and function "
                               "names must match", f"rename the file to {info['name']}.py")
            except (A.ActionError, OSError, UnicodeDecodeError) as exc:
                report.add("error", "features", f"actions/{path.name}: {exc}",
                           "fix the file; until then Pi warns and keeps the previous version")
    turns, minutes = values["daydreams.every_turns"], values["daydreams.every_minutes"]
    if turns or minutes:
        if not values["store.record"]:
            report.add("warn", "features", "daydreams are on, but store.record is false; only a recording session starts them",
                       "set store.record: true, or turn daydreams off")
        command = values["daydreams.command"]
        if command and not shutil.which(command) and not os.path.exists(command):
            report.add("error", "features", f"daydreams.command {command!r} was not found")
        when = f"every {turns} turns" if turns else f"every {minutes} minutes"
        model = values["daydreams.model"] or "the session's model"
        report.add("ok", "features", f"daydreams {when}, at most {values['daydreams.per_hour']} per hour, with {model}")
    index = store / "vault" / "index.md"
    if index.is_file():
        text = index.read_text(encoding="utf-8", errors="replace")
        rendered = "wikilink" if "[[" in text else "markdown" if "](" in text else None
        if rendered and rendered != values["wiki.link_style"]:
            report.add("info", "features", f"the vault was rendered with {rendered} links; it switches to "
                       f"{values['wiki.link_style']} at the next dream or wiki pass")


def run(cwd, env=None, session=None):
    env = dict(os.environ if env is None else env)
    report = Report()
    resolved = C.resolve(cwd, env)
    values = {key: item["value"] for key, item in resolved["settings"].items()}
    check_config(report, resolved)
    check_runtime(report, values, live=bool(session))
    memory, store = check_store(report, values, cwd, session)
    check_features(report, values, memory, store, session or {})
    return {"version": _version(), "cwd": str(cwd), "settings": resolved["settings"], "files": resolved["files"],
            "items": report.items, "errors": report.count("error"), "warnings": report.count("warn")}


def _version():
    try:
        return json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["version"]
    except (OSError, ValueError, KeyError):
        return "unknown"


def render(result, *, show_all=False):
    marks = {"error": "✗", "warn": "!", "info": "·", "ok": "✓"}
    out = [f"Somni doctor (pi-somni {result['version']})", ""]
    for area in ("config", "runtime", "store", "features"):
        items = [i for i in result["items"] if i["area"] == area]
        if not items:
            continue
        out.append(area.capitalize())
        for item in items:
            out.append(f"  {marks[item['level']]} {item['message']}")
            if item.get("fix"):
                out.append(f"      fix: {item['fix']}")
        out.append("")
    rows = [(key, item) for key, item in result["settings"].items() if show_all or item["source"] != "default"]
    out.append("Settings" + ("" if show_all else " (changed from the default; --all shows every one)"))
    if not rows:
        out.append("  (all at their defaults)")
    width = max((len(k) for k, _ in rows), default=0)
    for key, item in rows:
        value = item["value"]
        if key == "store.path":
            shown = _short(value)
        elif isinstance(value, str) and value and "\n" not in value:
            shown = value
        else:
            shown = json.dumps(value, ensure_ascii=False)
        if len(shown) > 60:
            shown = "..." + shown[-57:] if key == "store.path" else shown[:57] + "..."
        source = item["source"]
        source = _short(source) if not source.startswith(("env ", "default")) else source
        out.append(f"  {key.ljust(width)}  {shown}  ({source})")
    out.append("")
    summary = f"{result['errors']} error(s), {result['warnings']} warning(s)"
    out.append(summary if result["errors"] or result["warnings"] else "No problems found.")
    return "\n".join(out) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cwd", default=os.getcwd(), help="the directory Pi starts in (default: here)")
    ap.add_argument("--all", action="store_true", help="list every setting, not only changed ones")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--session-json", help=argparse.SUPPRESS)  # live facts from a running Pi session
    args = ap.parse_args(argv)
    session = json.loads(args.session_json) if args.session_json else None
    result = run(pathlib.Path(args.cwd).resolve(), session=session)
    sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n" if args.json else render(result, show_all=args.all))
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
