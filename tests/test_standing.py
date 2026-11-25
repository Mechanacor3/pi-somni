#!/usr/bin/env python3
"""Standing preferences: how the user wants work done, listed first in every index whatever the query."""
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from somni import dream as dm  # noqa: E402
from somni.store import MAX_STANDING, SomniError, SomniMemory, _handle_request  # noqa: E402

HEADER = "How the user wants work done (standing"


class StandingTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ws = pathlib.Path(tmp.name)
        self.m = SomniMemory.create(self.ws, "s", "l", "a")

    def session(self, *items, tags=None):
        self.m.set_phase("open", allow_same=True)
        ids = self.m.record_many(list(items), context={"tags": tags} if tags else None)
        self.m.set_phase("closed")
        return ids

    def add(self, ref, quote, text, **extra):
        return {"op": "add", "text": text, "status": "verified", "salience": 3,
                "evidence": [{"ref": ref, "quote": quote}], **extra}

    def reload(self):
        return SomniMemory(self.ws, "s").load()

    def test_a_standing_preference_heads_the_index_whatever_the_query(self):
        said, _ = self.session(("ordinary_observation", "USER: I like 2-space indents, always."),
                               ("agent_output", "AGENT: Noted."))
        deploys, = self.session(("tool_result", "TOOL read -> deploys go out from release branches"))
        self.m.dream({"schema": 1, "operations": [
            self.add(said, "I like 2-space indents", "Indent code with 2 spaces.", standing=True),
            self.add(deploys, "deploys go out from release branches", "Deploy from release branches.", salience=5)]})
        memory = self.reload()  # replaying the notes checks the standing claim again
        index = memory.inject(4000, query="how do deploys work")
        self.assertIn(HEADER, index)
        self.assertLess(index.index("Indent code with 2 spaces."), index.index("Deploy from release branches."))
        self.assertEqual(index.count("Indent code with 2 spaces."), 1)  # not listed again among the ranked claims
        self.assertIn("[standing; evidence=", index)
        self.assertIn(", standing,", dm.render_claims(memory))

    def test_a_standing_claim_needs_the_users_own_words(self):
        reply, = self.session(("agent_output", "AGENT: I always use tabs."))
        sub, = self.session(("ordinary_observation", "USER: use tabs everywhere"), tags=["contributor:helper"])
        for ref, quote in ((reply, "I always use tabs"), (sub, "use tabs everywhere")):
            with self.assertRaisesRegex(SomniError, "user's own words"):
                self.m.dream({"schema": 1, "operations": [self.add(ref, quote, "Use tabs.", standing=True)]})
        self.m.dream({"schema": 1, "operations": [self.add(reply, "I always use tabs", "The agent used tabs.")]})
        with self.assertRaisesRegex(SomniError, "standing must be true or false"):
            self.m.dream({"schema": 1, "operations": [self.add(reply, "I always use tabs", "x", standing="yes")]})

    def test_update_and_correct_keep_standing_unless_told_and_merge_inherits_it(self):
        first, = self.session(("ordinary_observation", "USER: I like 2-space indents"))
        self.m.dream({"schema": 1, "operations": [self.add(first, "2-space indents", "Indent with 2 spaces.",
                                                           standing=True)]})
        later, = self.session(("ordinary_observation", "USER: actually I've switched to 4-space indents"))
        self.m.dream({"schema": 1, "operations": [{**self.add(later, "4-space indents", "Indent with 4 spaces."),
                                                   "op": "update", "claim_id": "n000001"}]})
        self.assertTrue(self.m.claims["n000002"].get("standing"))
        done, = self.session(("ordinary_observation", "USER: indent however the project's formatter says"))
        self.m.dream({"schema": 1, "operations": [{**self.add(done, "however the project's formatter says",
                                                              "Follow the project's formatter.", standing=False),
                                                   "op": "update", "claim_id": "n000002"}]})
        self.assertNotIn("standing", self.m.claims["n000003"])
        self.assertNotIn(HEADER, self.reload().inject(4000))
        a, b = self.session(("ordinary_observation", "USER: keep replies short"),
                            ("ordinary_observation", "USER: lead with the answer"))
        self.m.dream({"schema": 1, "operations": [self.add(a, "keep replies short", "Keep replies short.", standing=True),
                                                  self.add(b, "lead with the answer", "Lead with the answer.")]})
        self.m.dream({"schema": 1, "operations": [{"op": "merge", "claim_ids": ["n000004", "n000005"],
                                                   "text": "Reply briefly, answer first.", "status": "verified"}]})
        self.assertTrue(self.m.claims["n000006"].get("standing"))

    def test_only_a_few_standing_claims_can_be_active(self):
        refs = self.session(*[("ordinary_observation", f"USER: preference number {i}") for i in range(MAX_STANDING + 1)])
        ops = [self.add(ref, f"preference number {i}", f"Follow preference {i}.", standing=True)
               for i, ref in enumerate(refs)]
        self.m.dream({"schema": 1, "operations": ops[:MAX_STANDING]})
        with self.assertRaisesRegex(SomniError, "standing limit reached"):
            self.m.dream({"schema": 1, "operations": ops[MAX_STANDING:]})
        self.m.dream({"schema": 1, "operations": [{"op": "retract", "claim_id": "n000001", "reason": "no longer wanted",
                                                   "evidence": [{"ref": refs[0]}]}]})
        self.m.dream({"schema": 1, "operations": ops[MAX_STANDING:]})  # room again

    def test_standing_claims_fit_a_third_of_the_index_and_the_rest_stay_ranked(self):
        refs = self.session(*[("ordinary_observation", f"USER: rule {i}: " + "x" * 150) for i in range(MAX_STANDING)])
        self.m.dream({"schema": 1, "operations": [self.add(ref, f"rule {i}", f"Rule {i}: " + "y" * 150, standing=True)
                                                  for i, ref in enumerate(refs)]})
        index = self.reload().inject(1500)
        head = index[index.index(HEADER):]
        shown = [line for line in head.splitlines() if line.startswith("- n") and "[standing;" in line]
        self.assertTrue(0 < len(shown) < MAX_STANDING)
        self.assertLessEqual(sum(len(line.encode()) + 1 for line in shown), 1500 // 3)
        self.assertLessEqual(len(index.encode()), 1500)

    def test_a_daytime_note_can_be_standing_and_the_night_dream_sees_it(self):
        self.m.set_phase("open")
        said, = self.m.record_many([("ordinary_observation", "USER: from now on, run the tests before saying done")])
        claim_id = _handle_request(self.m, "note", {"text": "Run the tests before reporting done.", "status": "verified",
                                                    "salience": 3, "standing": True,
                                                    "evidence": [{"ref": said, "quote": "run the tests before saying done"}]},
                                   writable=True)
        self.assertTrue(self.m.claims[claim_id]["standing"])
        self.assertIn(HEADER, self.m.inject(4000, query="unrelated"))
        other, = self.m.record_many([("tool_result", "TOOL ls -> README.md")])
        related = self.m.related_claims([self.m._records_by_id[other]])
        self.assertEqual([(c["id"], c.get("standing")) for c in related], [(claim_id, True)])


if __name__ == "__main__":
    unittest.main()
