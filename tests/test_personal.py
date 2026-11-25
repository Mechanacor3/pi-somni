#!/usr/bin/env python3
"""The personal store: one memory of the user, shared by every project, next to each project's own store."""
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from somni.store import SomniError, SomniMemory, _handle_request, _layered_index, _open_personal  # noqa: E402

SECTION = "From the user's personal memory"


class PersonalTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.personal = _open_personal(self.root / "home" / ".somni")
        self.a, self.b = self.project("a"), self.project("b")

    def project(self, name):
        workspace = self.root / name
        workspace.mkdir()
        return SomniMemory.create(workspace, "pi", "pi", "session")

    def session(self, memory, *items):
        memory.set_phase("open", allow_same=True)
        ids = memory.record_many(list(items))
        memory.set_phase("closed")
        return ids

    def dream(self, memory, *operations):
        return _handle_request(memory, "dream", {"proposal_json": json.dumps({"schema": 1, "operations": list(operations)})},
                               writable=True, personal=self.personal)

    def test_a_lesson_about_the_user_learned_in_one_project_heads_another_projects_index(self):
        said, fact = self.session(self.a, ("ordinary_observation", "USER: I like 2-space indents, everywhere"),
                                  ("tool_result", "TOOL read -> this repo deploys from release branches"))
        result = self.dream(self.a,
                            {"op": "add", "text": "Indent with 2 spaces.", "status": "verified", "salience": 3,
                             "standing": True, "scope": "personal",
                             "evidence": [{"ref": said, "quote": "I like 2-space indents"}]},
                            {"op": "add", "text": "Deploy from release branches.", "status": "verified", "salience": 3,
                             "evidence": [{"ref": fact, "quote": "deploys from release branches"}]})
        self.assertEqual((result["added_count"], result["personal"]["added_ids"], result["rejected"]), (2, ["p:n000001"], []))
        personal = SomniMemory(self.personal.workspace, "personal").load()  # the copied record's chain verifies
        copy = personal.transcript[0]
        self.assertEqual((copy["source"], copy["text"], copy["imported"]["ref"]),
                         ("ordinary_observation", "USER: I like 2-space indents, everywhere", said))
        self.assertTrue(personal.claims["n000001"]["standing"])
        self.assertEqual(list(self.a.load().claims), ["n000001"])  # the project keeps only its own lesson
        self.assertEqual(self.a.dreams[-1]["operation_count"], 1)
        index = _layered_index(self.b.load(), self.personal, 4000, query="what's on today?")  # a different project
        self.assertTrue(index.startswith("# Subject memory index\nClaims are interpretations."), index)
        self.assertIn(SECTION, index)
        self.assertIn("- p:n000001 [standing; evidence=p:t000001] Indent with 2 spaces.", index)
        self.assertNotIn("Deploy from release branches.", index)

    def test_an_update_to_a_personal_claim_routes_by_its_p_id_and_cites_newer_records_from_any_project(self):
        first, = self.session(self.a, ("ordinary_observation", "USER: I like 2-space indents"))
        self.dream(self.a, {"op": "add", "text": "Indent with 2 spaces.", "status": "verified", "salience": 3,
                            "standing": True, "scope": "personal", "evidence": [{"ref": first, "quote": "2-space"}]})
        later, = self.session(self.b, ("ordinary_observation", "USER: I've switched to 4-space indents"))
        result = self.dream(self.b, {"op": "update", "claim_id": "p:n000001", "text": "Indent with 4 spaces.",
                                     "status": "verified", "salience": 3,
                                     "evidence": [{"ref": later, "quote": "4-space indents"}]})
        self.assertEqual(result["personal"]["added_ids"], ["p:n000002"])
        personal = self.personal.load()
        self.assertEqual(personal.claims["n000001"]["state"], "past")
        self.assertTrue(personal.claims["n000002"]["standing"])  # an update keeps standing
        self.assertEqual({r["imported"]["from"] for r in personal.transcript}, {str(self.a.store), str(self.b.store)})

    def test_a_record_cited_twice_is_copied_once_and_rejections_keep_their_place(self):
        said, = self.session(self.a, ("ordinary_observation", "USER: keep replies short and lead with the answer"))
        result = self.dream(self.a,
                            {"op": "add", "text": "Keep replies short.", "status": "verified", "salience": 3,
                             "scope": "personal", "evidence": [{"ref": said, "quote": "keep replies short"}]},
                            {"op": "add", "text": "Bad quote.", "status": "verified", "salience": 3,
                             "evidence": [{"ref": said, "quote": "not in the record"}]},
                            {"op": "add", "text": "Lead with the answer.", "status": "verified", "salience": 3,
                             "scope": "personal", "evidence": [{"ref": said, "quote": "lead with the answer"}]},
                            {"op": "add", "text": "Bad scope.", "status": "uncertain", "salience": 3,
                             "scope": "personal", "evidence": [{"ref": "t000099"}]})
        self.assertEqual([r["index"] for r in result["rejected"]], [1, 3])
        self.assertTrue(result["rejected"][1]["reason"].startswith("personal: "))
        self.assertEqual(len(self.personal.load().transcript), 1)
        self.assertEqual(sorted(self.personal.claims), ["n000001", "n000002"])

    def test_a_note_recall_and_search_reach_the_personal_store_with_p_ids(self):
        self.a.set_phase("open")
        said, = self.a.record_many([("ordinary_observation", "USER: from now on, always show me the diff first")])
        claim = _handle_request(self.a, "note", {"text": "Show the diff before applying changes.", "status": "verified",
                                                 "salience": 3, "standing": True, "scope": "personal",
                                                 "evidence": [{"ref": said, "quote": "always show me the diff first"}]},
                                writable=True, personal=self.personal)
        self.assertEqual(claim, "p:n000001")
        hits = _handle_request(self.b, "recall", {"query": "diff before changes", "limit": 3}, writable=True,
                               personal=self.personal)
        mine = [h for h in hits if h.get("personal")]
        self.assertEqual((mine[0]["id"], mine[0]["evidence"][0]["ref"]), ("p:n000001", "p:t000001"))
        found = _handle_request(self.b, "search", {"query": "p:t000001", "limit": 5}, writable=True,
                                personal=self.personal)
        self.assertEqual([r["id"] for r in found], ["p:t000001"])

    def test_without_a_personal_store_scope_personal_is_refused(self):
        said, = self.session(self.a, ("ordinary_observation", "USER: I like tabs"))
        with self.assertRaisesRegex(SomniError, "personal memory is off"):
            _handle_request(self.a, "dream", {"proposal_json": json.dumps({"schema": 1, "operations": [
                {"op": "add", "text": "Use tabs.", "status": "uncertain", "salience": 3, "scope": "personal",
                 "evidence": [{"ref": said}]}]})}, writable=True)


if __name__ == "__main__":
    unittest.main()
