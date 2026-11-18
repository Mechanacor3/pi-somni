#!/usr/bin/env python3
"""Wiki layer: page validation, wikilinks, provenance, stale pages, and the Obsidian vault view."""
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from somni.store import SomniError, SomniMemory  # noqa: E402


class WikiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = pathlib.Path(self.tmp.name)
        m = SomniMemory.create(self.ws, "s", "l", "a")
        m.set_phase("open")
        m.record_many([("tool_result", "cancel_booking confirm=false returned status preview, fee 75"),
                       ("ordinary_observation", "USER: yes, please cancel it")])
        m.add_note("Unconfirmed cancels only preview the fee.", "verified", [{"ref": "t000001", "quote": "status preview"}], 5)
        m.add_note("Cancel only after the customer explicitly agrees.", "verified", [{"ref": "t000002", "quote": "yes, please cancel"}], 4)
        m.set_phase("closed")
        self.m = m.load()

    def tearDown(self):
        self.tmp.cleanup()

    def page(self, slug, body, claims, **extra):
        return {"op": "page", "slug": slug, "title": slug.replace("-", " ").title(), "body": body, "claims": claims, **extra}

    def test_pages_cite_active_claims_and_links_must_resolve(self):
        dangling = self.page("cancellations", "See [[refunds]]. Preview first [n000001].", ["n000001"])
        out = self.m.dream({"schema": 1, "operations": [dangling]}, partial=True) if False else None
        with self.assertRaisesRegex(SomniError, "does not exist"):
            self.m.dream({"schema": 1, "operations": [dangling]})
        with self.assertRaisesRegex(SomniError, "not an active claim"):
            self.m.dream({"schema": 1, "operations": [self.page("x", "body", ["n000099"])]})
        with self.assertRaisesRegex(SomniError, "at least one claim, or link"):
            self.m.dream({"schema": 1, "operations": [self.page("x", "no sources", [])]})
        record = self.m.dream({"schema": 1, "operations": [
            self.page("consent", "Get an explicit yes before any write [n000002].", ["n000002"]),
            self.page("cancellations", "Preview the fee first [n000001], then follow [[consent]].", ["n000001"])]})
        self.assertEqual(record["page_operations"], 2)
        m = SomniMemory(self.ws, "s").load()
        self.assertEqual(m.pages["cancellations"]["links"], ["consent"])
        m.dream({"schema": 1, "operations": [self.page("overview", "Start at [[cancellations]]; consent rules [n000002].", [])]})
        m = SomniMemory(self.ws, "s").load()
        self.assertEqual(m.pages["overview"]["claims"], ["n000002"])  # inline citation merged
        self.assertEqual(m.pages["overview"]["links"], ["cancellations"])
        self.assertEqual(m.recall_pages("how do I cancel", 1)[0]["slug"], "cancellations")
        with self.assertRaisesRegex(SomniError, "still link"):
            m.dream({"schema": 1, "operations": [{"op": "drop_page", "slug": "consent", "reason": "merge"}]})

    def test_vault_is_readable_and_tracks_retractions(self):
        self.m.dream({"schema": 1, "operations": [
            self.page("consent", "Get an explicit yes before any write [n000002].", ["n000002"]),
            self.page("cancellations", "Preview the fee first [n000001], then follow [[consent]].", ["n000001"])]})
        vault = self.ws / "somni-v1" / "vault"
        self.assertEqual(sorted(p.name for p in vault.glob("*.md")), ["cancellations.md", "claims.md", "consent.md", "index.md"])
        page = (vault / "cancellations.md").read_text()
        self.assertTrue(page.startswith("---\ntitle: \"Cancellations\""))
        self.assertIn("[[claims#n000001|n000001]]", page)
        self.assertIn("## See also", page)
        self.assertIn("## Backlinks", (vault / "consent.md").read_text())
        self.assertIn("> status preview", (vault / "claims.md").read_text())
        self.assertIn("[[consent|Consent]]", (vault / "index.md").read_text())
        m = SomniMemory(self.ws, "s").load()
        m.dream({"schema": 1, "operations": [{"op": "retract", "claim_id": "n000002", "reason": "test", "evidence": [{"ref": "t000002"}]}]})
        m = SomniMemory(self.ws, "s").load()
        self.assertEqual(m.stale_pages(), {"consent": ["n000002"]})
        self.assertIn("(**retracted**)", (vault / "consent.md").read_text())
        before = {p.name: p.read_text() for p in vault.glob("*.md")}
        m.write_vault()
        self.assertEqual(before, {p.name: p.read_text() for p in vault.glob("*.md")})

    def test_store_without_wiki_still_loads(self):
        self.assertFalse((self.ws / "somni-v1" / "wiki.jsonl").exists())
        self.assertEqual(SomniMemory(self.ws, "s").load().pages, {})


class WikiDreamingTest(unittest.TestCase):
    def test_build_wiki_fill_gaps_and_page_retrieval(self):
        sys.path.insert(0, str(ROOT))
        from adapters.state_bench import somni_state_bench as sb
        with tempfile.TemporaryDirectory() as tmp:
            src = pathlib.Path(tmp) / "src"
            src.mkdir()
            m = SomniMemory.create(src, sb.SUBJECT, "l", "a")
            m.set_phase("open")
            m.record_many([("tool_result", f"TOOL cancel_booking -> fee {i}") for i in range(4)])
            for i in range(4):
                m.add_note(f"Cancellation rule {i}: preview the fee before cancelling.", "uncertain", [{"ref": f"t00000{i + 1}"}], 3)
            m.set_phase("closed")
            calls = []

            def complete(system, prompt):
                calls.append(prompt)
                if "BUILD THE WIKI" in prompt:
                    return '{"operations": [{"op": "page", "slug": "cancellations", "title": "Cancellations", "body": "Always preview the fee first [n000001]. Then confirm [n000002].", "claims": ["n000001", "n000002", "n000003", "n000004"]}]}'
                return '{"operations": [{"op": "page", "slug": "overview", "title": "Overview", "body": "Start with [[cancellations]].", "claims": ["n000001"]}, {"op": "promote", "claim_id": "n000001", "salience": 5}]}'
            out = pathlib.Path(tmp) / "out" / "travel"
            summary = sb.build_wiki_from(src, out, complete, log=lambda *_: None)
            self.assertEqual(summary["metrics"]["wiki_pages"], 2)
            self.assertIn("GOAL for this memory", calls[0])
            self.assertTrue((out / "somni-v1" / "vault" / "overview.md").exists())
            m2 = SomniMemory(out, sb.SUBJECT).load()
            self.assertEqual(m2.stale_pages(), {})  # promotion keeps the claim id, so pages stay valid
            self.assertEqual(m2.claims["n000001"]["salience"], 5)
            self.assertTrue(m2.inject(4096).index("page `cancellations`") < m2.inject(4096).index("n000001"))
            self.assertFalse((src / "somni-v1" / "wiki.jsonl").exists())  # the source store is untouched

            class Agent(sb.SomniWikiLearnings):
                somni_root = str(pathlib.Path(tmp) / "out")

                class runtime_context:
                    domain = "travel"
            got = Agent().retrieve_learnings("how to cancel and what fee", 3)
            self.assertTrue(got[0].startswith("# Cancellations"))
            self.assertEqual(len(got), 3)


if __name__ == "__main__":
    unittest.main()
