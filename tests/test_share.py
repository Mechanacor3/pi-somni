#!/usr/bin/env python3
"""Sharing: a skill drafted from memory, with the evidence behind each guideline and no personal details."""
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from somni.store import SomniError, SomniMemory, _handle_request, _open_personal, _record_hash  # noqa: E402


class ShareTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "proj").mkdir()
        self.m = SomniMemory.create(self.root / "proj", "pi", "pi", "session")
        self.personal = _open_personal(self.root / "home" / ".somni")
        self.m.set_phase("open")
        self.said, self.ran = self.m.record_many([
            ("ordinary_observation", "USER: always run the slow tests in a container, the host one flakes"),
            ("tool_result", "TOOL run_tests -> 212 passed in the container")])
        self.m.set_phase("closed")
        self.checked = self.m.record_outcome("CI green on the container run", by="ci", verdict="pass")
        self.request("dream", proposal_json=json.dumps({"schema": 1, "operations": [
            {"op": "add", "text": "Run the slow tests in a container; on the host they flake.", "status": "verified",
             "salience": 4, "evidence": [{"ref": self.said, "quote": "run the slow tests in a container"},
                                         {"ref": self.ran, "quote": "212 passed in the container"},
                                         {"ref": self.checked, "quote": "CI green"}]},
            {"op": "add", "text": "Lead replies with the answer.", "status": "verified", "salience": 3,
             "scope": "personal", "evidence": [{"ref": self.said, "quote": "always"}]}]}))
        deny = mock.patch.dict(os.environ, {"SOMNI_NEVER_SHARE": "Quentin Marsh"})
        deny.start()
        self.addCleanup(deny.stop)

    def request(self, method, **args):
        return _handle_request(self.m, method, args, writable=True, personal=self.personal)

    def share(self, **proposal):
        draft = {"name": "container-tests", "description": "How to run flaky slow test suites so they pass.",
                 "guidelines": [{"text": "Run slow test suites in a container rather than on the host.",
                                 "claims": ["n000001"]}], **proposal}
        return self.request("share", proposal=draft, cwd=str(self.root / "proj"))

    def test_the_material_shows_what_backs_each_claim_project_and_personal(self):
        material = self.request("share_material", topic="slow tests container")
        self.assertIn("Share pass.", material["guide"])
        self.assertIn("- n000001 [verified; 3 record(s)", material["claims"])
        self.assertIn("outcomes 1 pass (ci)", material["claims"])
        everything = self.request("share_material", topic="")
        self.assertIn("- p:n000001 [verified;", everything["claims"])

    def test_a_share_writes_a_skill_and_its_evidence_with_record_hashes(self):
        result = self.share(intro="Lessons from two weeks of CI work.", title="Container-first test runs",
                            guidelines=[{"text": "Run slow test suites in a container rather than on the host.",
                                         "claims": ["n000001"]},
                                        {"text": "Lead each reply with the answer.", "claims": ["p:n000001"]}])
        folder = pathlib.Path(result["path"])
        self.assertEqual((folder.name, result["guidelines"], result["replaced"]), ("container-tests", 2, False))
        skill = (folder / "SKILL.md").read_text()
        self.assertTrue(skill.startswith("---\nname: container-tests\ndescription: \"How to run flaky"), skill)
        self.assertIn("# Container-first test runs\n\nLessons from two weeks of CI work.", skill)
        self.assertIn("1. Run slow test suites in a container rather than on the host.\n   *Evidence: 3 record(s)", skill)
        self.assertIn("the author said it once; 1 tool result(s); outcomes 1 pass (ci). [E1](EVIDENCE.md#e1)*", skill)
        evidence = (folder / "EVIDENCE.md").read_text()
        record = self.m.load()._records_by_id[self.checked]
        self.assertIn(f"| {self.checked} | outcome | {record['at'][:10]} | pass (ci) | `{_record_hash(record)}` |", evidence)
        self.assertIn("| p:t000001 | user message |", evidence)
        self.assertIn("Personal memory: transcript head", evidence)
        self.assertNotIn("host one flakes", skill + evidence)  # no record text leaves the store
        self.assertTrue(self.share()["replaced"])

    def test_a_draft_with_personal_details_is_refused_with_what_to_fix(self):
        cases = {"/home/quinn/ci/run.sh": "a home path", "mail ops@example.com": "an email address",
                 "ssh 10.0.0.12": "an IP address", "key sk-abcdefghijklmnop1234": "a key or token",
                 "ask Quentin first": "a name that identifies the user", str(self.root / "proj"): "a local path"}
        for text, why in cases.items():
            with self.assertRaisesRegex(SomniError, f"personal details.*guideline 1: {why}"):
                self.share(guidelines=[{"text": f"Run tests in a container: {text}", "claims": ["n000001"]}])
        self.assertFalse((self.m.workspace / "shares").exists())

    def test_a_share_cites_current_claims_and_keeps_to_the_skill_format(self):
        for proposal, error in (({"name": "Container Tests"}, "lowercase words"),
                                ({"guidelines": [{"text": "x", "claims": ["n000099"]}]}, "no such claim"),
                                ({"guidelines": [{"text": "x", "claims": []}]}, "cite 1 to 8"),
                                ({"description": "x" * 1025}, "up to 1024")):
            with self.assertRaisesRegex(SomniError, error):
                self.share(**proposal)
        self.request("dream", proposal_json=json.dumps({"schema": 1, "operations": [
            {"op": "retract", "claim_id": "n000001", "reason": "wrong", "evidence": [{"ref": self.said}]}]}))
        with self.assertRaisesRegex(SomniError, "only current claims"):
            self.share()


if __name__ == "__main__":
    unittest.main()
