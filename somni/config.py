#!/usr/bin/env python3
"""Somni settings: one schema, a YAML-flavoured config file, and the environment.

Every setting has a config key (``actions.enabled``) and an environment variable
(``SOMNI_ACTIONS``).  The value in effect is the first of:

1. the environment variable, when it is set;
2. ``<cwd>/.pi/somni.conf`` (the project file);
3. ``$XDG_CONFIG_HOME/somni/somni.conf``, by default ``~/.config/somni/somni.conf`` (the user file);
4. the default.

``SOMNI_CONFIG=/path/file`` reads that one file instead of both, and ``SOMNI_CONFIG=none`` reads
no file.  The Pi extension resolves the files once at load and sets each unset variable from
them, so the backend and daydream workers see the same values.

The file format is a strict subset of YAML: nested ``key: value`` maps (spaces, not tabs),
``# comments``, ``[a, b]`` and ``- item`` lists, ``{a: 1}`` maps, quoted strings, and ``|`` or
``>`` blocks for long text.  ``a.b: value`` is the same as ``a:`` then ``  b: value``.

Stdlib only.  CLI: ``config.py resolve --cwd DIR --json`` (used by the extension),
``config.py template`` (a commented file listing every setting), ``config.py table``
(the settings table in docs/settings.md).
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import pathlib
import re
import sys
from dataclasses import dataclass

PROJECT_FILE = pathlib.Path(".pi") / "somni.conf"
MAX_FILE_BYTES = 256 * 1024


@dataclass(frozen=True)
class Setting:
    key: str
    env: str
    kind: str  # bool, int, number, str, path, enum, list, text
    default: object
    help: str
    low: float | None = None
    high: float | None = None
    choices: tuple = ()
    default_text: str = ""  # how the default is described when it isn't a plain value


SETTINGS = (
    Setting("store.path", "SOMNI_WORKSPACE", "path", None,
            "Where the memory store lives. Relative paths are relative to the directory Pi starts in",
            default_text="`.pi/somni`"),
    Setting("store.subject", "SOMNI_SUBJECT", "str", "pi", "Identity bound into the store's manifest"),
    Setting("store.record", "SOMNI_RECORD", "bool", True,
            "Record sessions automatically. `false` keeps memory retrieval but records nothing "
            "(a testbed driver may record instead)"),
    Setting("store.python", "SOMNI_PYTHON", "str", "python3",
            "Interpreter for the backend. The config files themselves are read with `SOMNI_PYTHON` from the "
            "environment, or `python3`"),
    Setting("index.bytes", "SOMNI_INJECT_BYTES", "int", 4096, "Size of the memory index injected before each prompt",
            256, 8192),
    Setting("recording.tool_result_bytes", "SOMNI_TOOL_RESULT_BYTES", "int", 4096,
            "Tool result text kept per record, in UTF-8 bytes", 512, 8000),
    Setting("recording.digest_tools", "SOMNI_DIGEST_TOOLS", "list", ["stream_read"],
            "Tools whose text output is recorded as a byte count and SHA-256 beside their arguments. "
            "An empty list disables this"),
    Setting("recording.image_bytes", "SOMNI_IMAGE_BYTES", "int", 262144,
            "Stored bytes per image. `0` disables image capture", 0, 2097152),
    Setting("recording.image_max_edge", "SOMNI_IMAGE_MAX_EDGE", "int", 0,
            "Optional thumbnail edge in pixels. Needs Pillow in the backend's Python", 0, 4096),
    Setting("dreams.records", "SOMNI_DREAM_RECORDS", "int", 120,
            "Recent transcript records sent to `/somni-dream`", 1, 120),
    Setting("dreams.record_bytes", "SOMNI_DREAM_RECORD_BYTES", "int", 1500,
            "Text excerpt per dream record, in UTF-8 bytes, including the truncation marker", 64, 8192),
    Setting("dreams.max_reads", "SOMNI_DREAM_MAX_READS", "int", 16, "Somni evidence reads per dream or wiki turn",
            1, 64),
    Setting("dreams.max_images", "SOMNI_DREAM_MAX_IMAGES", "int", 16,
            "Distinct images a dream may view, including those attached to its prompt", 0, 64),
    Setting("dreams.max_submissions", "SOMNI_DREAM_MAX_SUBMISSIONS", "int", 3, "`somni_dream` attempts per dream", 1, 10),
    Setting("dreams.cache_minutes", "SOMNI_DREAM_CACHE_MINUTES", "number", 60,
            "How long an interrupted dream's evidence reads are offered to a retry on the same records. `0` disables",
            0, 10080),
    Setting("dreams.lenient_image_evidence", "SOMNI_LENIENT_IMAGE_EVIDENCE", "bool", False,
            "Keep a citation's `{ref}` when only its image is wrong, downgrading the claim to uncertain if needed"),
    Setting("wiki.link_style", "SOMNI_LINK_STYLE", "enum", "markdown",
            "Vault links: `markdown` (relative `[Title](path.md)`) or `wikilink` (Obsidian `[[slug|Title]]`)",
            choices=("markdown", "wikilink")),
    Setting("wiki.page_guide", "SOMNI_PAGE_GUIDE", "text", "", "Replaces the default wiki-writing guidance",
            default_text="practical playbook guidance"),
    Setting("learning.chel", "SOMNI_CHEL_LEARNING", "bool", False,
            "Chel rule and verb proposals in dreams, and rule retrieval in sessions"),
    Setting("actions.enabled", "SOMNI_ACTIONS", "bool", False,
            "[Action words](action-words.md): dreamed Python functions that call session tools, "
            "registered as Pi tools"),
    Setting("actions.tools", "SOMNI_ACTION_TOOLS", "list", None, "The tools action words may call",
            default_text="all non-Somni tools"),
    Setting("actions.max_calls", "SOMNI_ACTION_MAX_CALLS", "int", 16, "Tool calls per action word run", 1, 64),
    Setting("daydreams.every_turns", "SOMNI_DAYDREAM_EVERY_TURNS", "int", 0,
            "Start a daydream (see [Daydreams](long-sessions.md#daydreams-experimental)) after this many model turns. `0` is off", 0, 10000),
    Setting("daydreams.every_minutes", "SOMNI_DAYDREAM_EVERY_MINUTES", "number", 0,
            "Or start one on this interval. `0` is off", 0, 1440),
    Setting("daydreams.per_hour", "SOMNI_DAYDREAM_PER_HOUR", "int", 4, "At most this many daydreams start per hour",
            1, 60),
    Setting("daydreams.model", "SOMNI_DAYDREAM_MODEL", "str", "", "Model for daydreams, as `provider/id`",
            default_text="the session's model"),
    Setting("daydreams.thinking", "SOMNI_DAYDREAM_THINKING", "str", "", "Thinking level for daydreams",
            default_text="Pi's default"),
    Setting("daydreams.timeout_seconds", "SOMNI_DAYDREAM_TIMEOUT_SECONDS", "number", 600,
            "Stop a daydream that runs longer", 10, 7200),
    Setting("daydreams.focus", "SOMNI_DAYDREAM_FOCUS", "text", "", "Focus text passed to the daydream's `/somni-dream`",
            default_text="claims and words from the newest records"),
    Setting("daydreams.command", "SOMNI_DAYDREAM_COMMAND", "str", "", "Command used to start the daydream's Pi",
            default_text="the running Pi"),
)
BY_KEY = {s.key: s for s in SETTINGS}
BY_ENV = {s.env: s for s in SETTINGS}
SECTIONS = sorted({s.key.split(".")[0] for s in SETTINGS})
# Set per run or by Somni itself; never read from a config file.
ENV_ONLY = {
    "SOMNI_CONFIG": "Config file to read instead of the user and project files; `none` reads none",
    "SOMNI_RUN_LABEL": "Label on each recorded event, up to 96 UTF-8 bytes",
    "SOMNI_TAGS": "JSON array of up to eight printable tags, each up to 96 UTF-8 bytes",
    "SOMNI_DAYDREAM": "Set by Somni in a daydream worker",
    "SOMNI_STORE": "Path to an alternative `store.py` (development)",
}
TRUE_WORDS, FALSE_WORDS = {"true", "yes", "on", "1"}, {"false", "no", "off", "0"}


class ConfigError(ValueError):
    def __init__(self, message, line=None):
        super().__init__(message)
        self.line = line


# ---------------------------------------------------------------- the YAML subset

_NUMBER = re.compile(r"[-+]?(\d+\.\d*|\.\d+|\d+)([eE][-+]?\d+)?")
_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*")


def _strip_comment(text):
    """Drop a trailing `# comment` that is outside quotes."""
    quote = None
    for i, ch in enumerate(text):
        if quote:
            if ch == quote and not (quote == '"' and text[i - 1] == "\\"):
                quote = None
        elif ch in "\"'" and (i == 0 or text[i - 1] in " \t[{,:"):
            quote = ch
        elif ch == "#" and (i == 0 or text[i - 1] in " \t"):
            return text[:i].rstrip()
    return text.rstrip()


def _split_flow(body, line):
    """Split the inside of [..] or {..} on top-level commas."""
    items, depth, quote, start = [], 0, None, 0
    for i, ch in enumerate(body):
        if quote:
            if ch == quote and not (quote == '"' and body[i - 1] == "\\"):
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == "," and depth == 0:
            items.append(body[start:i].strip())
            start = i + 1
    if quote or depth:
        raise ConfigError("unbalanced quotes or brackets", line)
    last = body[start:].strip()
    if last:
        items.append(last)
    elif items:
        raise ConfigError("empty item in a list or map", line)
    return items


def _scalar(text, line):
    text = text.strip()
    if text == "":
        return None
    if text[0] == '"':
        if len(text) < 2 or text[-1] != '"':
            raise ConfigError("unterminated double-quoted string", line)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            raise ConfigError(f"bad escape in {text}", line) from None
    if text[0] == "'":
        if len(text) < 2 or text[-1] != "'":
            raise ConfigError("unterminated single-quoted string", line)
        return text[1:-1].replace("''", "'")
    if text[0] == "[":
        if text[-1] != "]":
            raise ConfigError("a [list] must end on the same line", line)
        return [_scalar(item, line) for item in _split_flow(text[1:-1], line)]
    if text[0] == "{":
        if text[-1] != "}":
            raise ConfigError("a {map} must end on the same line", line)
        out = {}
        for item in _split_flow(text[1:-1], line):
            key, sep, value = item.partition(":")
            key = key.strip()
            if not sep or not _KEY.fullmatch(key):
                raise ConfigError(f"expected key: value in {{...}}, got {item!r}", line)
            if key in out:
                raise ConfigError(f"duplicate key {key}", line)
            out[key] = _scalar(value, line)
        return out
    if text[0] in "|>&*!%@`":
        raise ConfigError(f"unsupported YAML syntax {text[0]!r} here", line)
    low = text.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "~"):
        return None
    if _NUMBER.fullmatch(text):
        return int(text) if re.fullmatch(r"[-+]?\d+", text) else float(text)
    return text


_ENTRY = re.compile(r"([A-Za-z_][A-Za-z0-9_.-]*|\"[^\"]+\"|'[^']+')\s*:(?:\s+(.*)|$)")
_BLOCK = ("|", "|-", "|+", ">", ">-", ">+")


def parse(text):
    """Parse the YAML subset into nested dicts.  Raises ConfigError with a 1-based line."""
    raw = text.split("\n")
    if raw and raw[0].startswith("\ufeff"):
        raw[0] = raw[0][1:]

    def meaningful(i):
        """The next line index at or after i that holds content, or len(raw)."""
        while i < len(raw):
            line = raw[i]
            if line.strip() and not line.lstrip(" ").startswith("#") and line.strip() not in ("---", "..."):
                indent = len(line) - len(line.lstrip(" "))
                if line[indent:indent + 1] == "\t":
                    raise ConfigError("indent with spaces, not tabs", i + 1)
                return i
            if line.strip() == "---" and i > 0 and any(r.strip() and not r.lstrip().startswith("#") for r in raw[:i]):
                raise ConfigError("only one document per file", i + 1)
            i += 1
        return i

    def indent_of(i):
        return len(raw[i]) - len(raw[i].lstrip(" "))

    def block_scalar(i, owner_indent, style):
        """Lines after a `key: |` that are indented past the key."""
        body = []
        while i < len(raw) and (not raw[i].strip() or indent_of(i) > owner_indent):
            body.append(raw[i])
            i += 1
        while body and not body[-1].strip():
            body.pop()
        if not body:
            return "", i
        margin = min(len(b) - len(b.lstrip(" ")) for b in body if b.strip())
        lines = [b[margin:] if b.strip() else "" for b in body]
        if style.startswith(">"):
            paragraphs, current = [], []
            for line in lines:
                if line:
                    current.append(line.strip())
                else:
                    paragraphs.append(" ".join(current))
                    current = []
            paragraphs.append(" ".join(current))
            joined = "\n".join(paragraphs)
        else:
            joined = "\n".join(lines)
        return (joined if style.endswith("-") else joined + "\n"), i

    def is_item(i):
        content = _strip_comment(raw[i].lstrip(" "))
        return content == "-" or content.startswith("- ")

    def sequence(i, indent):
        items = []
        while (i := meaningful(i)) < len(raw) and indent_of(i) == indent:
            if not is_item(i):
                break
            item = _strip_comment(raw[i].lstrip(" "))[1:].strip()
            if item.startswith("- ") or item == "-":
                raise ConfigError("nested lists are not supported", i + 1)
            if _ENTRY.fullmatch(item):
                raise ConfigError("lists of maps are not supported", i + 1)
            items.append(_scalar(item, i + 1))
            i += 1
        return items, i

    def mapping(i, indent):
        out = {}
        while (i := meaningful(i)) < len(raw):
            here = indent_of(i)
            if here < indent:
                break
            if here > indent:
                raise ConfigError("unexpected indentation", i + 1)
            content = _strip_comment(raw[i][here:])
            if is_item(i):
                raise ConfigError("a - list item must follow a key with no value", i + 1)
            match = _ENTRY.fullmatch(content)
            if not match:
                raise ConfigError(f"expected `key: value`, got {content!r}", i + 1)
            key, value = match.group(1).strip("\"'"), (match.group(2) or "").strip()
            if key in out:
                raise ConfigError(f"duplicate key {key}", i + 1)
            line = i + 1
            i += 1
            if value in _BLOCK:
                out[key], i = block_scalar(i, indent, value)
            elif value:
                out[key] = _scalar(value, line)
            else:
                nxt = meaningful(i)
                if nxt < len(raw) and indent_of(nxt) >= indent and is_item(nxt):
                    out[key], i = sequence(nxt, indent_of(nxt))
                elif nxt < len(raw) and indent_of(nxt) > indent:
                    out[key], i = mapping(nxt, indent_of(nxt))
                else:
                    out[key] = None
        return out, i

    first = meaningful(0)
    if first < len(raw) and indent_of(first) != 0:
        raise ConfigError("the first setting must not be indented", first + 1)
    tree, end = mapping(first, 0)
    if meaningful(end) < len(raw):
        raise ConfigError("unexpected indentation", meaningful(end) + 1)
    return tree


# ---------------------------------------------------------------- resolution


def flatten(tree, prefix="", lines=None):
    """{'actions': {'enabled': True}, 'index.bytes': 1} -> {'actions.enabled': True, 'index.bytes': 1}."""
    out = {}
    for key, value in tree.items():
        full = f"{prefix}{key}"
        if isinstance(value, dict) and full not in BY_KEY:
            for k, v in flatten(value, full + ".").items():
                if k in out:
                    raise ConfigError(f"{k} is set twice")
                out[k] = v
        else:
            if full in out:
                raise ConfigError(f"{full} is set twice")
            out[full] = value
    return out


def _key_lines(text):
    """Best-effort line number for each dotted key, for messages."""
    found, stack = {}, []
    for number, raw in enumerate(text.split("\n"), 1):
        content = _strip_comment(raw)
        match = re.match(r"( *)([A-Za-z_][A-Za-z0-9_.-]*)\s*:", content)
        if not match or content.lstrip().startswith("-"):
            continue
        indent = len(match.group(1))
        while stack and stack[-1][0] >= indent:
            stack.pop()
        full = ".".join([k for _, k in stack] + [match.group(2)])
        found.setdefault(full, number)
        stack.append((indent, match.group(2)))
    return found


def suggest(key):
    """A likely intended key for an unknown one."""
    if key in BY_ENV:
        return BY_ENV[key].key
    if key.upper().startswith("SOMNI_") and key.upper() in BY_ENV:
        return BY_ENV[key.upper()].key
    if key in SECTIONS and f"{key}.enabled" in BY_KEY:
        return f"{key}.enabled"
    close = difflib.get_close_matches(key, list(BY_KEY), n=1, cutoff=0.6)
    if close:
        return close[0]
    leaf = key.rsplit(".", 1)[-1]
    by_leaf = [k for k in BY_KEY if k.rsplit(".", 1)[-1] == leaf]
    return by_leaf[0] if len(by_leaf) == 1 else None


def _num(x):
    return str(int(x)) if float(x) == int(x) else str(x)


def check_value(setting, value):
    """Return (normalized value, warning or None).  Raises ConfigError for a value that can't be used."""
    kind, key = setting.kind, setting.key
    if value is None:
        raise ConfigError(f"{key} has no value; remove the line to use the default")
    if kind == "bool":
        if isinstance(value, bool):
            return value, None
        if isinstance(value, (str, int)) and str(value).lower() in TRUE_WORDS | FALSE_WORDS:
            return str(value).lower() in TRUE_WORDS, None
        raise ConfigError(f"{key} must be true or false; got {value!r}")
    if kind in ("int", "number"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{key} must be a number; got {value!r}")
        if kind == "int" and value != int(value):
            raise ConfigError(f"{key} must be a whole number; got {value!r}")
        value = int(value) if kind == "int" else value
        if setting.low is not None and value < setting.low:
            return value, f"{key} = {value} is below {_num(setting.low)}; Somni will use {_num(setting.low)}"
        if setting.high is not None and value > setting.high:
            return value, f"{key} = {value} is above {_num(setting.high)}; Somni will use {_num(setting.high)}"
        return value, None
    if kind == "list":
        if isinstance(value, str):
            value = [part.strip() for part in value.split(",") if part.strip()]
        if not isinstance(value, list) or not all(isinstance(v, str) and v and "," not in v for v in value):
            raise ConfigError(f"{key} must be a list of names, such as [read, grep]; got {value!r}")
        return value, None
    if kind == "enum":
        if value not in setting.choices:
            raise ConfigError(f"{key} must be one of {', '.join(setting.choices)}; got {value!r}")
        return value, None
    if isinstance(value, (int, float)) and not isinstance(value, bool) and kind == "str":
        value = str(value)
    if not isinstance(value, str):
        raise ConfigError(f"{key} must be text; got {value!r}")
    if kind in ("str", "path") and "\n" in value.strip():
        raise ConfigError(f"{key} must be one line")
    return (value.strip() if kind != "text" else value), None


def to_env(setting, value, cwd):
    """The environment string the extension reads for a checked value."""
    if setting.kind == "bool":
        return "1" if value else "0"
    if setting.kind == "list":
        return ",".join(value)
    if setting.kind == "path":
        path = pathlib.Path(os.path.expanduser(value))
        return str(path if path.is_absolute() else pathlib.Path(cwd) / path)
    if isinstance(value, float) and value == int(value):
        value = int(value)
    return str(value)


def config_files(cwd, env):
    """[(label, path)] in increasing precedence."""
    chosen = env.get("SOMNI_CONFIG")
    if chosen is not None:
        if chosen.strip().lower() in ("", "none"):
            return []
        return [("SOMNI_CONFIG", pathlib.Path(os.path.expanduser(chosen)))]
    base = env.get("XDG_CONFIG_HOME") or os.path.join(env.get("HOME") or os.path.expanduser("~"), ".config")
    return [("user", pathlib.Path(base) / "somni" / "somni.conf"), ("project", pathlib.Path(cwd) / PROJECT_FILE)]


def resolve(cwd=None, env=None):
    """Read the config files and merge them with the environment.

    Returns {"files": [...], "settings": {key: {value, env, source, line}}, "env": {VAR: text}
    for file values whose variable is unset, "errors": [...], "warnings": [...]}.  A setting with
    an error keeps its lower-precedence value."""
    cwd = str(cwd or os.getcwd())
    env = dict(os.environ if env is None else env)
    files, errors, warnings = [], [], []
    layers = []
    for label, path in config_files(cwd, env):
        entry = {"label": label, "path": str(path), "exists": path.is_file(), "settings": 0}
        files.append(entry)
        if not entry["exists"]:
            if label == "SOMNI_CONFIG":
                errors.append({"file": str(path), "line": None, "message": "SOMNI_CONFIG names a file that does not exist"})
            continue
        try:
            raw = path.read_bytes()
            if len(raw) > MAX_FILE_BYTES:
                raise ConfigError(f"larger than {MAX_FILE_BYTES // 1024} KiB")
            text = raw.decode("utf-8")
            tree = parse(text)
            values = flatten(tree)
        except (ConfigError, UnicodeDecodeError, OSError) as exc:
            errors.append({"file": str(path), "line": getattr(exc, "line", None), "message": str(exc)})
            continue
        lines = _key_lines(text)
        layer = {}
        for key, value in values.items():
            parts = key.split(".")
            line = next((lines[".".join(parts[:n])] for n in range(len(parts), 0, -1) if ".".join(parts[:n]) in lines), None)
            if key not in BY_KEY:
                if key in SECTIONS or any(k.startswith(key + ".") for k in BY_KEY):
                    hint = suggest(key) or next((k for k, st in BY_KEY.items() if k.startswith(key + ".")
                                                 and value in st.choices), None)
                    message = f"{key} is a section, not a setting" + (f"; did you mean {hint}: {json.dumps(value)}?" if hint else "")
                else:
                    hint = suggest(key)
                    message = f"{key} is not a Somni setting" + (f"; did you mean {hint}?" if hint else "")
                errors.append({"file": str(path), "line": line, "message": message})
                continue
            setting = BY_KEY[key]
            try:
                checked, warning = check_value(setting, value)
            except ConfigError as exc:
                errors.append({"file": str(path), "line": line, "message": str(exc)})
                continue
            if warning:
                warnings.append({"file": str(path), "line": line, "message": warning})
            layer[key] = (checked, f"{path}" + (f":{line}" if line else ""))
        entry["settings"] = len(layer)
        layers.append(layer)
    settings, exported = {}, {}
    for setting in SETTINGS:
        value, source, from_file = setting.default, "default", False
        for layer in layers:
            if setting.key in layer:
                value, source = layer[setting.key]
                from_file = True
        if setting.env in env:
            text = env[setting.env]
            source = f"env {setting.env}"
            from_file = False
            value = _from_env(setting, text, cwd, warnings)
        elif from_file:
            exported[setting.env] = to_env(setting, value, cwd)
        if setting.kind == "path" and value is not None and from_file:
            value = exported[setting.env]
        settings[setting.key] = {"value": value, "env": setting.env, "source": source}
    if settings["store.path"]["value"] is None:
        settings["store.path"]["value"] = str(pathlib.Path(cwd) / ".pi" / "somni")
    for name in sorted(env):
        if name.startswith("SOMNI_") and name not in BY_ENV and name not in ENV_ONLY:
            close = difflib.get_close_matches(name, list(BY_ENV) + list(ENV_ONLY), n=1, cutoff=0.7)
            warnings.append({"file": None, "line": None, "message": f"environment variable {name} is not a Somni setting"
                             + (f"; did you mean {close[0]}?" if close else "")})
    return {"files": files, "settings": settings, "env": exported, "errors": errors, "warnings": warnings}


def _from_env(setting, text, cwd, warnings):
    """An environment value, read the way the extension reads it."""
    kind = setting.kind
    if kind == "bool":
        return text != "0" if setting.env == "SOMNI_RECORD" else text == "1"
    if kind == "list":
        return [part.strip() for part in text.split(",") if part.strip()]
    if kind in ("int", "number"):
        try:
            number = float(text)
        except ValueError:
            warnings.append({"file": None, "line": None,
                             "message": f"{setting.env}={text!r} is not a number; Somni uses {setting.default}"})
            return setting.default
        if kind == "int" and number != int(number):
            warnings.append({"file": None, "line": None,
                             "message": f"{setting.env}={text!r} is not a whole number; Somni uses {setting.default}"})
            return setting.default
        number = int(number) if number == int(number) else number
        if (setting.low is not None and number < setting.low) or (setting.high is not None and number > setting.high):
            warnings.append({"file": None, "line": None, "message": f"{setting.env}={text} is outside "
                             f"{_num(setting.low)}-{_num(setting.high)}; Somni clamps it"})
        return number
    if kind == "path":
        path = pathlib.Path(os.path.expanduser(text))
        return str(path if path.is_absolute() else pathlib.Path(cwd) / path)
    return text


def apply(env=None, cwd=None):
    """Set unset variables in ``env`` (default os.environ) from the config files.  For CLIs."""
    target = os.environ if env is None else env
    resolved = resolve(cwd, target)
    for name, value in resolved["env"].items():
        target.setdefault(name, value)
    return resolved


# ---------------------------------------------------------------- docs


def _yaml_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return "[" + ", ".join(value) + "]"
    if isinstance(value, str):
        return json.dumps(value) if value == "" or re.search(r"[:#\[\]{},'\"]|^\s|\s$", value) else value
    return str(value)


def template():
    """A config file listing every setting, commented out at its default."""
    out = ["# Somni settings. Uncomment a line to change it. The environment variable named on each",
           "# line overrides this file. Check this file with /somni-doctor or `somni doctor`.",
           "# Put personal defaults in ~/.config/somni/somni.conf and project settings in .pi/somni.conf."]
    section = None
    for s in SETTINGS:
        head, leaf = s.key.split(".", 1)
        if head != section:
            out += ["", f"{head}:"]
            section = head
        default = s.default_text.strip("`") if s.default is None or s.default_text else _yaml_value(s.default)
        rng = f" ({_num(s.low)}-{_num(s.high)})" if s.low is not None else ""
        help_text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s.help).replace("`", "")
        out.append(f"  # {help_text}{rng}. {s.env}")
        value = default if s.default not in (None, "") or s.kind in ("int", "number", "bool") else ""
        out.append(f"  # {leaf}: {value}".rstrip() + ("" if value else f"    # default: {default}"))
    return "\n".join(out) + "\n"


def table():
    """The settings table in docs/settings.md."""
    rows = ["| Config key | Environment variable | Default | Meaning |", "|---|---|---|---|"]
    for s in SETTINGS:
        if s.default_text:
            default = s.default_text
        elif s.kind == "bool":
            default = "on" if s.default else "off"
        elif s.kind == "list":
            default = f"`{','.join(s.default)}`"
        else:
            default = f"`{s.default}`"
        rng = f" ({_num(s.low)}–{_num(s.high)})" if s.low is not None else ""
        rows.append(f"| `{s.key}` | `{s.env}` | {default} | {s.help.replace('|', chr(92) + '|')}{rng} |")
    for name, meaning in ENV_ONLY.items():
        rows.append(f"| | `{name}` | unset | {meaning} |")
    return "\n".join(rows) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Somni settings")
    sub = ap.add_subparsers(dest="command", required=True)
    res = sub.add_parser("resolve", help="merge the config files with the environment")
    res.add_argument("--cwd", default=os.getcwd())
    res.add_argument("--json", action="store_true")
    sub.add_parser("template", help="print a commented config file listing every setting")
    sub.add_parser("table", help="print the settings table (docs/settings.md)")
    args = ap.parse_args(argv)
    if args.command == "template":
        sys.stdout.write(template())
    elif args.command == "table":
        sys.stdout.write(table())
    else:
        resolved = resolve(args.cwd)
        if args.json:
            print(json.dumps(resolved, ensure_ascii=False))
        else:
            for key, item in resolved["settings"].items():
                print(f"{key} = {json.dumps(item['value'])}  ({item['source']})")
            for problem in resolved["errors"] + resolved["warnings"]:
                print(f"{problem.get('file') or 'env'}:{problem.get('line') or ''} {problem['message']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
