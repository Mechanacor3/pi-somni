"""The daily journal: notes about one day, written from that day's whole transcript, each citing its records."""
import json
import os
import pathlib
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from somni import store as S  # noqa: E402


class Clock:
    """SOMNI_NOW and the local time zone, for one block."""

    def __init__(self, now, tz="UTC"):
        self.env = mock.patch.dict(os.environ, {"SOMNI_NOW": now, "TZ": tz})

    def __enter__(self):
        self.env.start()
        time.tzset()

    def __exit__(self, *exc):
        self.env.stop()
        time.tzset()


class JournalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = pathlib.Path(self.tmp.name)
        self.memory = S.SomniMemory.create(self.workspace, "subject", "lineage", "attempt")
        self.memory.set_phase("open")
        for now, text in (("2026-11-26T15:00:00Z", "USER: rename the deploy branch to release"),
                          ("2026-11-27T09:00:00Z", "USER: the docs live at https://docs.example.test/v2/start"),
                          ("2026-11-27T09:01:00Z", "AGENT: Pointed support at the v2 start page."),
                          ("2026-11-28T08:00:00Z", "USER: good morning")):
            with Clock(now):
                self.memory.record("ordinary_observation" if text.startswith("USER") else "agent_output", text)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, day, body, now="2026-11-28T12:00:00Z"):
        with Clock(now):
            return self.memory.write_journal(day, body)

    def test_an_entry_cites_its_days_records_and_renders_as_a_dated_note(self):
        body = "## Docs\n- Support now points at the v2 start page [t000002] [t000003]."
        out = self.write("2026-11-27", body)
        self.assertEqual((out["version"], out["records_that_day"], out["records_cited"], out["uncited_lines"]),
                         (1, 2, 2, 0))
        note = self.workspace / S.STORE_DIR / "vault" / "journal" / "2026" / "11" / "27.md"
        text = note.read_text()
        self.assertIn("# Friday, 2026-11-27", text)
        self.assertIn("records: [t000002, t000003]", text)
        index = (self.workspace / S.STORE_DIR / "vault" / "index.md").read_text()
        self.assertIn("## Journal", index)
        self.assertIn("(journal/2026/11/27.md)", index)
        fresh = S.SomniMemory(self.workspace, "subject").load()  # the chain verifies on a fresh load
        self.assertEqual(fresh.journal["2026-11-27"]["records"], ["t000002", "t000003"])
        self.assertEqual(fresh.status()["journal"], {"days": 1, "last": "2026-11-27"})
        self.assertEqual(fresh.metrics()["journal_days"], 1)

    def test_the_quality_checks_refuse_what_the_day_does_not_support(self):
        for body, error in (("Notes with no citations at all.", "must cite at least one record"),
                            ("Renamed the branch [t000001].", "t000001 is from 2026-11-26, not 2026-11-27"),
                            ("A record that isn't there [t000099].", "t000099 is not a record"),
                            ("A claim that isn't there [t000002] [n000042].", "n000042 is not a claim")):
            with self.subTest(body=body), self.assertRaisesRegex(S.SomniError, error):
                self.write("2026-11-27", body)
        with self.assertRaisesRegex(S.SomniError, "nothing was recorded on 2026-11-20"):
            self.write("2026-11-20", "x [t000002]")
        with self.assertRaisesRegex(S.SomniError, "hasn't happened yet"):
            self.write("2026-11-29", "x [t000004]")
        with self.assertRaisesRegex(S.SomniError, "not a calendar date"):
            self.write("2026-02-30", "x [t000002]")
        self.assertEqual(S.SomniMemory(self.workspace, "subject").load().journal, {})

    def test_uncited_lines_are_counted_not_refused(self):
        out = self.write("2026-11-27", "# The day\nA quiet one.\n- Docs moved to v2 [t000002].")
        self.assertEqual(out["uncited_lines"], 1)

    def test_a_rewrite_bumps_the_version_and_a_later_record_marks_the_day_stale(self):
        self.write("2026-11-27", "First pass [t000002].")
        with Clock("2026-11-28T12:00:00Z"):
            days = {d["date"]: d for d in self.memory.journal_days()}
        self.assertEqual(list(days), ["2026-11-27", "2026-11-26"])  # today (11-28) isn't over yet
        self.assertEqual((days["2026-11-27"]["journal"], days["2026-11-27"]["stale"]), (True, False))
        self.assertFalse(days["2026-11-26"]["journal"])
        out = self.write("2026-11-27", "Second pass [t000002] [t000003].")
        self.assertEqual(out["version"], 2)

    def test_a_day_is_the_machines_local_day(self):
        # 2026-11-27T05:00Z is still the evening of 11-26 in Los Angeles.
        with Clock("2026-11-27T05:00:00Z"):
            self.memory.record("ordinary_observation", "USER: late note")
        with Clock("2026-11-28T12:00:00Z", tz="America/Los_Angeles"):
            day = self.memory.journal_day("2026-11-26")
        self.assertEqual([r["id"] for r in day["records"]], ["t000001", "t000005"])

    def test_a_long_day_is_clipped_evenly_to_the_budget(self):
        with Clock("2026-11-28T12:00:00Z"):
            day = self.memory.journal_day("2026-11-27", budget=80)
        self.assertEqual(day["clipped_to"], S.JOURNAL_RECORD_MIN)
        with Clock("2026-11-28T12:00:00Z"):
            whole = self.memory.journal_day("2026-11-27")
        self.assertIsNone(whole["clipped_to"])
        self.assertIn("https://docs.example.test/v2/start", whole["records"][0]["text"])

    def test_the_days_claims_come_with_it(self):
        with Clock("2026-11-27T10:00:00Z"):
            self.memory.add_note("Docs live at the v2 start page.", "verified",
                                 [{"ref": "t000002", "quote": "https://docs.example.test/v2/start"}])
        with Clock("2026-11-28T12:00:00Z"):
            day = self.memory.journal_day("2026-11-27")
        self.assertEqual([c["id"] for c in day["claims"]], ["n000001"])
        out = self.write("2026-11-27", "Docs moved [t000002], kept as [n000001].")
        self.assertEqual(out["claims_cited"], 1)
        note = (self.workspace / S.STORE_DIR / "vault" / "journal" / "2026" / "11" / "27.md").read_text()
        self.assertIn("[n000001](../../../claims.md#n000001)", note)

    def test_wiki_pages_stay_out_of_the_journal_folder(self):
        with self.assertRaisesRegex(S.SomniError, "journal/ folder holds the daily journal"):
            S.SomniMemory._validate_page({"op": "page", "slug": "journal/notes", "title": "x", "body": "y",
                                          "claims": []}, {}, {})

    def test_an_edited_entry_breaks_the_chain(self):
        self.write("2026-11-27", "Docs moved [t000002].")
        path = self.workspace / S.STORE_DIR / "journal.jsonl"
        event = json.loads(path.read_text())
        event["entry"]["body"] = "Something else [t000002]."
        path.write_text(json.dumps(event) + "\n")
        with self.assertRaises(S.SomniError):
            S.SomniMemory(self.workspace, "subject").load()


if __name__ == "__main__":
    unittest.main()
