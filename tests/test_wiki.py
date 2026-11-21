#!/usr/bin/env python3
"""Wiki layer: page validation, wikilinks, provenance, stale pages, and the Obsidian vault view."""
import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

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
        return {"op": "page", "slug": slug, "title": slug.split("/")[-1].replace("-", " ").title(), "body": body, "claims": claims, **extra}

    def test_pages_cite_active_claims_and_links_must_resolve(self):
        dangling = self.page("cancellations", "See [[refunds]]. Preview first [n000001].", ["n000001"])
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

    def test_forward_links_and_cycles_survive_reload_and_render_backlinks(self):
        record = self.m.dream({"schema": 1, "operations": [
            self.page("overview", "Start at [[consent]].", []),
            self.page("consent", "See [[cancellations]] and [[overview]].", ["n000002"]),
            self.page("cancellations", "Preview the fee [n000001], then follow [[consent]].", ["n000001"]),
        ]})
        self.assertEqual(record["page_operations"], 3)
        pages = SomniMemory(self.ws, "s").load().pages
        self.assertEqual(pages["overview"]["links"], ["consent"])
        self.assertEqual(pages["consent"]["links"], ["cancellations", "overview"])
        self.assertEqual(pages["cancellations"]["links"], ["consent"])
        vault = self.ws / "somni-v1" / "vault"
        self.assertIn("## Backlinks\n\n- [Consent](consent.md)", (vault / "overview.md").read_text())
        self.assertIn("## See also\n\n- [Consent](consent.md)", (vault / "overview.md").read_text())

    def test_drops_and_rewrites_use_final_links(self):
        self.m.dream({"schema": 1, "operations": [
            self.page("overview", "See [[consent]].", []),
            self.page("consent", "See [[overview]].", ["n000002"]),
        ]})
        self.m.dream({"schema": 1, "operations": [
            {"op": "drop_page", "slug": "consent", "reason": "incorporated into overview"},
            self.page("overview", "Get an explicit yes [n000002].", ["n000002"]),
        ]})
        pages = SomniMemory(self.ws, "s").load().pages
        self.assertEqual(list(pages), ["overview"])
        self.assertEqual(pages["overview"]["version"], 2)
        self.assertEqual(pages["overview"]["links"], [])

    def test_partial_pass_rejects_broken_link_dependencies_and_keeps_valid_operations(self):
        self.m.dream({"schema": 1, "operations": [
            self.page("overview", "[[consent]]", []),
            self.page("consent", "Get an explicit yes [n000002].", ["n000002"]),
        ]})
        record = self.m.dream({"schema": 1, "operations": [
            {"op": "drop_page", "slug": "consent", "reason": "retire"},
            self.page("start", "See [[next]].", []),
            self.page("next", "See [[missing]].", []),
            self.page("cancellations", "Preview costs [n000001].", ["n000001"]),
        ]}, partial=True)
        self.assertEqual(record["page_operations"], 1)
        self.assertEqual([item["index"] for item in record["rejected"]], [0, 1, 2])
        self.assertIn("would still link", record["rejected"][0]["reason"])
        self.assertIn("'next'", record["rejected"][1]["reason"])
        self.assertIn("'missing'", record["rejected"][2]["reason"])
        self.assertEqual(set(SomniMemory(self.ws, "s").load().pages), {"overview", "consent", "cancellations"})

    def test_vault_is_readable_and_tracks_retractions(self):
        self.m.dream({"schema": 1, "operations": [
            self.page("consent", "Get an explicit yes before any write [n000002].", ["n000002"]),
            self.page("cancellations", "Preview the fee first [n000001], then follow [[consent]].", ["n000001"])]})
        vault = self.ws / "somni-v1" / "vault"
        self.assertEqual(sorted(p.name for p in vault.glob("*.md")), ["cancellations.md", "claims.md", "consent.md", "index.md"])
        page = (vault / "cancellations.md").read_text()
        self.assertTrue(page.startswith("---\ntitle: \"Cancellations\""))
        self.assertIn("[n000001](claims.md#n000001)", page)
        self.assertIn("## See also", page)
        self.assertIn("## Backlinks", (vault / "consent.md").read_text())
        self.assertIn("> status preview", (vault / "claims.md").read_text())
        self.assertIn("- [Consent](consent.md)", (vault / "index.md").read_text())
        m = SomniMemory(self.ws, "s").load()
        m.dream({"schema": 1, "operations": [{"op": "retract", "claim_id": "n000002", "reason": "test", "evidence": [{"ref": "t000002"}]}]})
        m = SomniMemory(self.ws, "s").load()
        self.assertEqual(m.stale_pages(), {"consent": ["n000002"]})
        self.assertNotIn("consent", [p["slug"] for p in m.recall_pages("consent")])
        self.assertNotIn("page `consent`", m.inject())
        self.assertIn("(**retracted**)", (vault / "consent.md").read_text())
        before = {p.name: p.read_text() for p in vault.glob("*.md")}
        m.write_vault()
        self.assertEqual(before, {p.name: p.read_text() for p in vault.glob("*.md")})

    def test_nested_pages_render_as_folders_with_relative_markdown_links(self):
        self.m.dream({"schema": 1, "operations": [
            self.page("overview", "Start with [[fares/economy|economy fares]] and [[fares/rules/refunds]].", []),
            self.page("fares/economy", "Preview the fee [n000001]; see [[fares/rules/refunds]].", ["n000001"]),
            self.page("fares/rules/refunds", "Ask first [n000002]. Back to [[overview]].", ["n000002"])]})
        vault = self.ws / "somni-v1" / "vault"
        files = sorted(p.relative_to(vault).as_posix() for p in vault.rglob("*.md"))
        self.assertEqual(files, ["claims.md", "fares/economy.md", "fares/rules/refunds.md", "index.md", "overview.md"])
        economy = (vault / "fares" / "economy.md").read_text()
        self.assertIn("Preview the fee [n000001](../claims.md#n000001); see [Refunds](rules/refunds.md).", economy)
        refunds = (vault / "fares" / "rules" / "refunds.md").read_text()
        self.assertIn("Back to [Overview](../../overview.md).", refunds)
        self.assertIn("## Backlinks\n\n- [Economy](../economy.md)\n- [Overview](../../overview.md)", refunds)
        self.assertIn("Start with [economy fares](fares/economy.md)", (vault / "overview.md").read_text())
        index = (vault / "index.md").read_text()
        self.assertIn("- [Overview](overview.md)\n- fares/\n  - [Economy](fares/economy.md)\n  - rules/\n"
                      "    - [Refunds](fares/rules/refunds.md)\n", index)
        self.assertIn("used by: [Economy](fares/economy.md)", (vault / "claims.md").read_text())
        # Links are validated over full paths; depth and reserved names are bounded.
        with self.assertRaisesRegex(SomniError, "at most 3 deep.*reserved for the vault.*'fares/missing', which does not exist"):
            self.m.dream({"schema": 1, "operations": [
                self.page("a/b/c/d", "Too deep [n000001].", ["n000001"]),
                self.page("claims", "Clashes with claims.md [n000001].", ["n000001"]),
                self.page("fares/oops", "Links [[fares/missing]] [n000001].", ["n000001"])]}, partial=True)
        # Moving a page to a folder removes the old note and any emptied folder.
        m = SomniMemory(self.ws, "s").load()
        m.dream({"schema": 1, "operations": [
            {"op": "drop_page", "slug": "fares/rules/refunds", "reason": "moved"},
            self.page("refunds", "Ask first [n000002].", ["n000002"]),
            self.page("fares/economy", "Preview the fee [n000001]; see [[refunds]].", ["n000001"]),
            self.page("overview", "Start with [[fares/economy]].", [])]})
        self.assertFalse((vault / "fares" / "rules").exists())
        self.assertTrue((vault / "refunds.md").exists())

    def test_wikilink_style_keeps_obsidian_links(self):
        with patch.dict(os.environ, {"SOMNI_LINK_STYLE": "wikilink"}):
            self.m.dream({"schema": 1, "operations": [
                self.page("fares/economy", "Preview the fee [n000001], see [[overview]].", ["n000001"]),
                self.page("overview", "Start at [[fares/economy]].", [])]})
        vault = self.ws / "somni-v1" / "vault"
        self.assertIn("Preview the fee [[claims#n000001|n000001]], see [[overview]].",
                      (vault / "fares" / "economy.md").read_text())
        self.assertIn("- [[fares/economy|Economy]]", (vault / "index.md").read_text())

    def test_corrected_page_can_be_recalled_after_repair(self):
        self.m.dream({"schema": 1, "operations": [
            self.page("cancellations", "Preview the fee first [n000001].", ["n000001"]),
        ]})
        self.m.dream({"schema": 1, "operations": [{
            "op": "correct", "claim_id": "n000001", "text": "Check the actual fare rules.",
            "status": "uncertain", "evidence": [{"ref": "t000001"}], "salience": 5,
        }]})
        self.assertEqual(self.m.recall_pages("cancellations"), [])
        self.m.dream({"schema": 1, "operations": [
            self.page("cancellations", "Check the actual fare rules [n000003].", ["n000003"]),
        ]})
        self.assertIn("n000003", self.m.recall_pages("cancellations")[0]["body"])

    def test_store_without_wiki_still_loads(self):
        self.assertFalse((self.ws / "somni-v1" / "wiki.jsonl").exists())
        self.assertEqual(SomniMemory(self.ws, "s").load().pages, {})


class WikiFeedbackTest(unittest.TestCase):
    def test_page_guide_uses_neutral_default_and_can_be_overridden(self):
        from somni import dream as dm
        with tempfile.TemporaryDirectory() as tmp:
            m = SomniMemory.create(pathlib.Path(tmp), "s", "l", "a")
            calls = []

            def complete(system, prompt):
                calls.append((system, prompt))
                return '{"schema":1,"operations":[]}'

            with patch.dict(os.environ, {"SOMNI_PAGE_GUIDE": " "}):
                dm.fill_gaps(m, complete, goal="g", log=lambda *_: None)
            with patch.dict(os.environ, {"SOMNI_PAGE_GUIDE": "Describe terrain cues."}):
                dm.fill_gaps(m, complete, goal="g", log=lambda *_: None)
        self.assertIn("can form cycles", calls[0][0])
        self.assertIn("steps and checks", calls[0][1])
        self.assertNotIn("confirmed with the person", calls[0][1])
        self.assertIn("Describe terrain cues.", calls[-1][1])

    def test_refocus_returns_instructions_and_survives_refusal(self):
        from somni import dream as dm
        with tempfile.TemporaryDirectory() as tmp:
            m = SomniMemory.create(pathlib.Path(tmp), "s", "l", "a")
            ins, trace = dm.refocus(m, lambda s, p: '{"diagnosis": "d", "instructions": "write a cheaper-path page"}', goal="g", failures="x", log=lambda *_: None)
            self.assertEqual(ins, "write a cheaper-path page")
            def refuse(s, p):
                raise RuntimeError("flagged")
            ins, trace = dm.refocus(m, refuse, goal="g", failures="x", log=lambda *_: None)
            self.assertEqual(ins, "")
            self.assertIn("flagged", trace["error"])


if __name__ == "__main__":
    unittest.main()
