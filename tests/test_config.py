#!/usr/bin/env python3
"""somni.conf: the YAML subset, precedence, messages, the schema's coverage, and somni doctor."""
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from somni import config as C  # noqa: E402
from somni import doctor as D  # noqa: E402
from somni.store import SomniMemory  # noqa: E402


class Parser(unittest.TestCase):
    def test_the_supported_subset(self):
        text = """# comment
store:
  path: ~/mem   # trailing comment
  record: false
actions.enabled: yes
actions:
  tools:
    - read
    - "grep"
  max_calls: 8
recording: {image_bytes: 0, digest_tools: [stream_read, "a b"]}
wiki:
  page_guide: |
    Line one: here
      indented # kept

    after a blank line
daydreams:
  focus: >-
    folded
    text
  thinking: off
  model: 'it''s'
"""
        self.assertEqual(C.parse(text), {
            "store": {"path": "~/mem", "record": False},
            "actions.enabled": "yes",
            "actions": {"tools": ["read", "grep"], "max_calls": 8},
            "recording": {"image_bytes": 0, "digest_tools": ["stream_read", "a b"]},
            "wiki": {"page_guide": "Line one: here\n  indented # kept\n\nafter a blank line\n"},
            "daydreams": {"focus": "folded text", "thinking": "off", "model": "it's"},
        })

    def test_errors_name_the_line(self):
        cases = {
            "a:\n\tb: 1": (2, "spaces, not tabs"),
            "a: 1\na: 2": (2, "duplicate key a"),
            "a:\n  b: 1\n   c: 2": (3, "unexpected indentation"),
            "- x": (1, "must follow a key"),
            "a: [1, 2": (1, "must end on the same line"),
            "a:\n  - b: 1": (2, "lists of maps"),
            "  a: 1": (1, "must not be indented"),
            "a: 'x": (1, "unterminated"),
            "a b": (1, "expected `key: value`"),
            "a: *ref": (1, "unsupported YAML"),
        }
        for text, (line, message) in cases.items():
            with self.subTest(text=text), self.assertRaisesRegex(C.ConfigError, re.escape(message)) as caught:
                C.parse(text)
            self.assertEqual(caught.exception.line, line)


class Resolution(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.home, self.proj = self.root / "home", self.root / "proj"
        (self.home / ".config" / "somni").mkdir(parents=True)
        (self.proj / ".pi").mkdir(parents=True)

    def write(self, user=None, project=None):
        if user is not None:
            (self.home / ".config" / "somni" / "somni.conf").write_text(user)
        if project is not None:
            (self.proj / ".pi" / "somni.conf").write_text(project)

    def resolve(self, **env):
        return C.resolve(self.proj, {"HOME": str(self.home), **env})

    def test_environment_then_project_then_user_then_default(self):
        self.write(user="actions:\n  enabled: true\n  max_calls: 20\nindex.bytes: 1024\ndaydreams.model: openai/x\n",
                   project="index:\n  bytes: 2048\nstore.path: mem\nactions.tools: [read, grep]\n")
        out = self.resolve(SOMNI_ACTION_MAX_CALLS="8")
        values = {k: v["value"] for k, v in out["settings"].items()}
        self.assertEqual((values["index.bytes"], values["actions.enabled"], values["actions.max_calls"]), (2048, True, 8))
        self.assertEqual(values["store.path"], str(self.proj / "mem"))
        self.assertEqual(values["dreams.max_reads"], 16)
        self.assertEqual(out["settings"]["actions.max_calls"]["source"], "env SOMNI_ACTION_MAX_CALLS")
        self.assertTrue(out["settings"]["index.bytes"]["source"].endswith("somni.conf:2"))
        # Only file values whose variable is unset are exported, in the form the extension reads.
        self.assertEqual(out["env"], {"SOMNI_ACTIONS": "1", "SOMNI_INJECT_BYTES": "2048", "SOMNI_WORKSPACE": str(self.proj / "mem"),
                                      "SOMNI_ACTION_TOOLS": "read,grep", "SOMNI_DAYDREAM_MODEL": "openai/x"})
        self.assertEqual((out["errors"], out["warnings"]), ([], []))

    def test_somni_config_names_one_file_or_none(self):
        self.write(user="index.bytes: 1024\n", project="index.bytes: 2048\n")
        other = self.root / "other.conf"
        other.write_text("index.bytes: 512\n")
        self.assertEqual(self.resolve(SOMNI_CONFIG=str(other))["env"], {"SOMNI_INJECT_BYTES": "512"})
        self.assertEqual(self.resolve(SOMNI_CONFIG="none")["env"], {})
        missing = self.resolve(SOMNI_CONFIG=str(self.root / "nope.conf"))
        self.assertIn("does not exist", missing["errors"][0]["message"])
        xdg = self.root / "xdg"
        (xdg / "somni").mkdir(parents=True)
        (xdg / "somni" / "somni.conf").write_text("dreams.max_reads: 20\n")
        (self.proj / ".pi" / "somni.conf").unlink()
        self.assertEqual(self.resolve(XDG_CONFIG_HOME=str(xdg))["env"], {"SOMNI_DREAM_MAX_READS": "20"})

    def test_mistakes_get_a_line_and_a_suggestion_and_keep_the_previous_value(self):
        self.write(user="index.bytes: 1024\n",
                   project="actions:\n  enable: true\nwiki: wikilink\nlearning: true\nSOMNI_INJECT_BYTES: 5\n"
                           "dreams: {max_reads: lots}\nindex.bytes: 99999\nstore.record: maybe\n"
                           "daydreams:\n  thinking: off\n")
        out = self.resolve(SOMNI_ACTOINS="1")
        messages = [(e.get("line"), e["message"]) for e in out["errors"]]
        self.assertIn((4, "learning is a section, not a setting; did you mean learning.chel: true?"), messages)
        self.assertIn((2, "actions.enable is not a Somni setting; did you mean actions.enabled?"), messages)
        self.assertIn((3, 'wiki is a section, not a setting; did you mean wiki.link_style: "wikilink"?'), messages)
        self.assertIn((5, "SOMNI_INJECT_BYTES is not a Somni setting; did you mean index.bytes?"), messages)
        self.assertIn((6, "dreams.max_reads must be a number; got 'lots'"), messages)
        self.assertIn((8, "store.record must be true or false; got 'maybe'"), messages)
        warnings = [w["message"] for w in out["warnings"]]
        self.assertIn("index.bytes = 99999 is above 8192; Somni will use 8192", warnings)
        self.assertIn("environment variable SOMNI_ACTOINS is not a Somni setting; did you mean SOMNI_ACTIONS?", warnings)
        self.write(user="actions: true\n", project="")
        self.assertEqual(self.resolve()["errors"][0]["message"],
                         "actions is a section, not a setting; did you mean actions.enabled: true?")
        values = {k: v["value"] for k, v in out["settings"].items()}
        self.assertEqual(values["dreams.max_reads"], 16)  # the bad value fell back
        self.assertEqual(values["store.record"], True)
        self.assertEqual(values["daydreams.thinking"], "off")  # `off` stays text where text is expected
        self.assertEqual(out["env"]["SOMNI_INJECT_BYTES"], "99999")  # the extension clamps it, as for the variable

    def test_a_file_that_does_not_parse_is_skipped_whole(self):
        self.write(user="index.bytes: 1024\n", project="index:\n\tbytes: 2048\n")
        out = self.resolve()
        self.assertEqual(out["env"], {"SOMNI_INJECT_BYTES": "1024"})
        self.assertEqual(out["errors"][0]["line"], 2)


class SchemaCoverage(unittest.TestCase):
    def test_every_variable_the_code_reads_is_a_setting_or_env_only(self):
        used = set()
        for path in [ROOT / "extensions" / "somni.ts", *sorted((ROOT / "somni").glob("*.py"))]:
            used |= set(re.findall(r"SOMNI_[A-Z_]+", path.read_text()))
        used -= {"SOMNI_VERSION"}  # a Python constant, not a variable
        known = set(C.BY_ENV) | set(C.ENV_ONLY)
        self.assertEqual(sorted(used - known), [], "add new variables to SETTINGS in somni/config.py")
        self.assertEqual(sorted(set(C.BY_ENV) - used), [], "SETTINGS lists a variable nothing reads")

    def test_the_readme_table_is_generated_from_the_schema(self):
        self.assertIn(C.table(), (ROOT / "README.md").read_text(), "regenerate with `bin/somni config table`")

    def test_the_template_uncommented_is_a_valid_config(self):
        lines = []
        for line in C.template().splitlines():
            match = re.fullmatch(r"  # ([a-z_]+): (.*?)(\s+# default: .*)?", line)
            if match and match.group(2):
                lines.append(f"  {match.group(1)}: {match.group(2)}")
            elif re.fullmatch(r"[a-z]+:", line):
                lines.append(line)
        with tempfile.TemporaryDirectory() as tmp:
            conf = pathlib.Path(tmp) / "all.conf"
            conf.write_text("\n".join(lines) + "\n")
            out = C.resolve(tmp, {"SOMNI_CONFIG": str(conf)})
        self.assertEqual((out["errors"], out["warnings"]), ([], []))
        defaults = {s.key: s.default for s in C.SETTINGS if s.default not in (None, "")}
        self.assertEqual({k: out["settings"][k]["value"] for k in defaults}, defaults)


class Doctor(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.proj = self.root / "proj"
        (self.proj / ".pi").mkdir(parents=True)
        self.env = {"HOME": str(self.root / "home"), "PATH": os.environ["PATH"]}

    def run_doctor(self, conf=None, session=None):
        if conf is not None:
            (self.proj / ".pi" / "somni.conf").write_text(conf)
        return D.run(self.proj, self.env, session)

    def messages(self, result, level=None):
        return [i["message"] for i in result["items"] if level is None or i["level"] == level]

    def make_store(self, subject="pi"):
        (self.proj / ".pi" / "somni").mkdir(exist_ok=True)
        memory = SomniMemory.create(self.proj / ".pi" / "somni", subject, "l", "a")
        memory.set_phase("open")
        memory.record("tool_result", "bash ran", context={})
        memory.set_phase("closed")
        memory.record_action_tools([{"name": "bash", "description": "run a command", "parameters": {}},
                                    {"name": "read", "description": "read a file", "parameters": {}}])
        return memory

    def test_no_files_and_no_store_is_fine(self):
        result = self.run_doctor()
        self.assertEqual(result["errors"], 0)
        self.assertTrue(any("no store yet" in m for m in self.messages(result, "info")))
        self.assertTrue(any("no config file" in m for m in self.messages(result, "info")))

    def test_config_errors_fail_the_check(self):
        result = self.run_doctor("actions:\n  enable: true\n")
        self.assertEqual(result["errors"], 1)
        self.assertIn("did you mean actions.enabled?", self.messages(result, "error")[0])

    def test_feature_changes_come_with_what_they_need(self):
        self.make_store()
        on = self.run_doctor("actions.enabled: true\n")
        self.assertTrue(any("including bash" in m for m in self.messages(on, "warn")))
        typo = self.run_doctor("actions:\n  enabled: true\n  tools: [read, raed]\n")
        self.assertTrue(any("no recorded session offered: raed" in m for m in self.messages(typo, "warn")))
        empty = self.run_doctor("actions:\n  enabled: true\n  tools: []\n")
        self.assertTrue(any("can call no tool" in m for m in self.messages(empty, "warn")))
        day = self.run_doctor("store.record: false\ndaydreams.every_minutes: 30\n")
        self.assertTrue(any("only a recording session starts them" in m for m in self.messages(day, "warn")))
        words = self.proj / ".pi" / "somni" / "somni-v1" / "actions"
        words.mkdir(exist_ok=True)
        (words / "bad.py").write_text("import os\ndef bad(x: int):\n    \"\"\"x\"\"\"\n    return {}\n")
        broken = self.run_doctor("actions:\n  enabled: true\n  tools: [read]\n")
        self.assertTrue(any(m.startswith("actions/bad.py: the only import") for m in self.messages(broken, "error")))
        (words / "bad.py").unlink()
        live = self.run_doctor("actions:\n  enabled: true\n  tools: [grep]\n", session={"tools": ["bash", "grep"]})
        self.assertTrue(any("read tool is off" in m for m in self.messages(live, "info")))
        self.assertEqual(live["errors"], 0)

    def test_store_problems(self):
        self.make_store(subject="someone")
        result = self.run_doctor()
        self.assertTrue(any("belongs to subject 'someone'" in m for m in self.messages(result, "error")))
        fixed = self.run_doctor("store.subject: someone\n")
        self.assertEqual(fixed["errors"], 0)
        self.assertTrue(any("verified; 1 records" in m for m in self.messages(fixed, "ok")))
        moved = self.run_doctor("store:\n  subject: someone\n  path: elsewhere\n")
        self.assertTrue(any("also holds a store" in m for m in self.messages(moved, "warn")))

    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_a_store_git_would_commit_is_flagged(self):
        self.make_store()
        subprocess.run(["git", "init", "-q", str(self.proj)], check=True)
        self.assertTrue(any("not ignored" in m for m in self.messages(self.run_doctor(), "warn")))
        (self.proj / ".gitignore").write_text(".pi/\n")
        self.assertFalse(any("not ignored" in m for m in self.messages(self.run_doctor(), "warn")))

    def test_doctor_changes_nothing_in_the_store(self):
        self.make_store()
        before = {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in (self.proj / ".pi").rglob("*")}
        self.run_doctor("actions.enabled: true\n")
        after = {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in (self.proj / ".pi").rglob("*")}
        before.pop(self.proj / ".pi" / "somni.conf", None)
        after.pop(self.proj / ".pi" / "somni.conf", None)
        self.assertEqual(before, after)

    def test_the_cli_renders_text_and_json_and_sets_the_exit_status(self):
        (self.proj / ".pi" / "somni.conf").write_text("index.bytes: 2048\nactions.enable: true\n")
        cli = [sys.executable, str(ROOT / "bin" / "somni"), "doctor", "--cwd", str(self.proj)]
        text = subprocess.run(cli, capture_output=True, text=True, env=self.env)
        self.assertEqual(text.returncode, 1)
        self.assertIn("did you mean actions.enabled?", text.stdout)
        self.assertRegex(text.stdout, r"index\.bytes\s+2048\s+\(.*somni\.conf:1\)")
        data = json.loads(subprocess.run(cli + ["--json"], capture_output=True, text=True, env=self.env).stdout)
        self.assertEqual(data["errors"], 1)
        template = subprocess.run([sys.executable, str(ROOT / "bin" / "somni"), "config", "template"],
                                  capture_output=True, text=True, check=True).stdout
        self.assertIn("# max_calls: 16", template)


if __name__ == "__main__":
    unittest.main()
