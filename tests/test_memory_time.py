#!/usr/bin/env python3
"""Memory over time: dated records, claims that change (update), fading strength, and forgetting old records."""
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from somni import config as C  # noqa: E402
from somni import dream as dm  # noqa: E402
from somni import store as S  # noqa: E402
from somni.store import SomniError, SomniMemory, _recent  # noqa: E402
from tests.test_actions import CANCEL, ON, TESTS  # noqa: E402

DAY0 = "2026-01-01T09:00:00Z"
DAY1 = "2026-01-02T09:00:00Z"
DAY40 = "2026-02-10T09:00:00Z"
DAY90 = "2026-04-01T09:00:00Z"


def at(moment):
    return patch.dict(os.environ, {"SOMNI_NOW": moment})


class TimeTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ws = pathlib.Path(tmp.name)
        self.m = SomniMemory.create(self.ws, "s", "l", "a")

    def dream(self, *ops, **kwargs):
        return self.m.dream({"schema": 1, "operations": list(ops)}, **kwargs)

    def record(self, moment, *texts, source="tool_result"):
        with at(moment):
            self.m.set_phase("open", allow_same=True)
            ids = self.m.record_many([(source, text) for text in texts])
            self.m.set_phase("closed")
        return ids

    def reload(self):
        return SomniMemory(self.ws, "s").load()

    # --- dated records ---

    def test_records_notes_and_passes_carry_the_time_and_somni_now_fixes_it(self):
        [ref] = self.record(DAY0, "Pens in the supply closet are blue.")
        self.assertEqual(self.m.transcript[-1]["at"], DAY0)
        with at(DAY1):
            self.dream({"op": "add", "text": "Pens are blue.", "status": "verified", "salience": 3,
                        "evidence": [{"ref": ref, "quote": "Pens in the supply closet are blue."}]})
        claim = self.reload().claims["n000001"]
        self.assertEqual(claim["created_at"], DAY1)
        self.assertEqual(self.m.dreams[-1]["at"], DAY1)
        with patch.dict(os.environ, {"SOMNI_NOW": "yesterday"}), self.assertRaisesRegex(SomniError, "ISO 8601"):
            self.m.set_phase("open")
            self.m.record("tool_result", "x")

    def test_dream_prompts_show_day_headers(self):
        self.record(DAY0, "first")
        self.record(DAY1, "second")
        listing = dm.render_records(self.m.load().transcript)
        self.assertEqual(listing.splitlines(), ["--- 2026-01-01 ---", "[t000001] (tool_result) first",
                                                "--- 2026-01-02 ---", "[t000002] (tool_result) second"])
        self.assertIn("use update", dm.SYSTEM.replace("{time_guide}", dm.TIME_GUIDE))

    # --- conflicting claims: changed versus wrong ---

    def blue_then_red(self):
        [blue] = self.record(DAY0, "Inventory: pens are blue.")
        with at(DAY0):
            self.dream({"op": "add", "text": "Pens are blue.", "gist": "Pens are blue.", "status": "verified",
                        "salience": 3, "evidence": [{"ref": blue, "quote": "pens are blue"}]})
        [red] = self.record(DAY1, "Inventory: pens are red now; the supplier changed.")
        return blue, red

    def test_update_keeps_the_old_claim_as_true_until_the_change(self):
        blue, red = self.blue_then_red()
        with at(DAY1):
            self.dream({"op": "update", "claim_id": "n000001", "text": "Pens are red (since the supplier changed).",
                        "status": "verified", "salience": 3, "evidence": [{"ref": red, "quote": "pens are red now"}]})
        memory = self.reload()
        old, new = memory.claims["n000001"], memory.claims["n000002"]
        self.assertEqual((old["state"], old["until_ref"], old["superseded_by"]), ("past", red, "n000002"))
        self.assertEqual((new["state"], new["change"], new["replaces"]), ("active", "changed", "n000001"))
        self.assertEqual(memory.history(new), [{"id": "n000001", "text": "Pens are blue.", "until_ref": red, "until": DAY1}])
        with at(DAY1):
            index = memory.inject(query="what colour are the pens?")
            self.assertIn("since 2026-01-02; before that: Pens are blue.", index)
            self.assertIn("Dates say when a memory was last seen or used, or since when it holds.", index)
            self.assertNotIn("- n000001", index)  # history, not a current claim
            hits = memory.recall("pens colour blue red", 5)
        self.assertEqual([h["id"] for h in hits], ["n000002", "n000001"])  # past claims rank below current ones
        self.assertEqual((hits[1]["state"], hits[1]["until"]), ("past", DAY1))
        self.assertEqual(hits[0]["history"][0]["id"], "n000001")
        self.assertEqual(memory.status()["past_claims"], 1)

    def test_update_needs_newer_evidence_and_correct_still_means_wrong(self):
        blue, red = self.blue_then_red()
        with self.assertRaisesRegex(SomniError, "newer than n000001's newest record.*use correct"):
            self.dream({"op": "update", "claim_id": "n000001", "text": "Pens are red.", "status": "uncertain",
                        "salience": 3, "evidence": [{"ref": blue}]})
        self.dream({"op": "correct", "claim_id": "n000001", "text": "Pens are red.", "status": "uncertain",
                    "salience": 3, "evidence": [{"ref": red}]})
        memory = self.reload()
        self.assertEqual(memory.claims["n000001"]["state"], "superseded")
        self.assertEqual(memory.history(memory.claims["n000002"]), [])
        self.assertEqual([h["id"] for h in memory.recall("pens", 5)], ["n000002"])  # a wrong claim isn't recalled

    def test_a_chain_of_updates_is_history_newest_first(self):
        blue, red = self.blue_then_red()
        self.dream({"op": "update", "claim_id": "n000001", "text": "Pens are red.", "status": "uncertain",
                    "salience": 3, "evidence": [{"ref": red}]})
        [green] = self.record(DAY40, "Inventory: pens are green.")
        self.dream({"op": "update", "claim_id": "n000002", "text": "Pens are green.", "status": "uncertain",
                    "salience": 3, "evidence": [{"ref": green}]})
        memory = self.reload()
        self.assertEqual([v["id"] for v in memory.history(memory.claims["n000003"])], ["n000002", "n000001"])

    def test_the_night_dream_sees_claims_related_to_its_records(self):
        self.blue_then_red()
        with at(DAY1):
            window = _recent(self.m.load(), False, set(), related=True)
        self.assertEqual([c["id"] for c in window["claims"]], ["n000001"])
        self.assertEqual(window["claims"][0]["strength"]["first_seen"], DAY0)
        self.assertNotIn("claims", _recent(self.m, False, set()))

    # --- strength ---

    def test_strength_fades_with_age_and_returns_with_use(self):
        self.blue_then_red()
        memory = self.reload()
        claim = memory.claims["n000001"]
        with at(DAY0):
            self.assertEqual(memory.strength(claim)["level"], "fresh")
        with at(DAY40):
            self.assertEqual(memory.strength(claim)["level"], "fading")
        with at(DAY90):
            faint = memory.strength(claim)
            self.assertEqual(faint["level"], "faint")
            index = memory.inject(query="pens")
            self.assertIn("Pens are blue. (as of 2026-01-01; faint)", index)
            self.assertIn("It is still the latest you know unless something newer says otherwise", index)
        with at(DAY0):
            self.assertNotIn("Dates say", memory.inject(query="pens"))  # nothing dated to explain
        with at(DAY40):
            self.assertIn("Pens are blue. (as of 2026-01-01)\n", memory.inject(query="pens"))
            self.assertIn("as of 2026-01-01) Pens are blue.", dm.render_claims(memory))
        self.assertIn("Age is not doubt", dm.TIME_GUIDE)
        # Using the claim (a later record names it) is retrieval practice: it is fresh again.
        self.record("2026-03-31T09:00:00Z", "Checked n000001 against the closet.", source="agent_output")
        memory = self.reload()
        with at(DAY90):
            self.assertEqual(memory.strength(memory.claims["n000001"])["level"], "fresh")
            self.assertGreater(memory.strength(memory.claims["n000001"])["activation"], faint["activation"])

    def test_salience_and_decay_shape_strength(self):
        self.assertGreater(S._activation([10], 0.5, salience=5), S._activation([10], 0.5, salience=3))
        self.assertGreater(S._activation([10, 20, 30], 0.5), S._activation([10], 0.5))  # repetition
        self.assertGreater(S._activation([10], 0.3), S._activation([10], 0.8))  # slower decay keeps more
        self.assertEqual(S._strength_level(S._activation([1], 0.5)), "fresh")

    def test_a_fresh_claim_outranks_an_equally_relevant_faint_one(self):
        [old] = self.record(DAY0, "Receipts go in the blue folder.")
        [new] = self.record(DAY90, "Receipts go in the red folder.")
        with at(DAY90):
            self.dream({"op": "add", "text": "File receipts.", "status": "uncertain", "salience": 3, "evidence": [{"ref": old}]},
                       {"op": "add", "text": "File receipts.", "status": "uncertain", "salience": 3, "evidence": [{"ref": new}]})
            memory = self.reload()
            self.assertGreater(memory.strength(memory.claims["n000002"])["activation"],
                               memory.strength(memory.claims["n000001"])["activation"])
            index = memory.inject(query="where do receipts go?")
        self.assertLess(index.index("n000002"), index.index("n000001"))

    # --- forgetting ---

    def old_day(self, count=6):
        texts = [f"Day-zero log line {i}: " + "detail " * 40 for i in range(count)]
        texts[1] = "The deploy key lives in the vault under ops/deploy. " + "padding " * 40
        refs = self.record(DAY0, *texts)
        with at(DAY0):
            self.dream({"op": "add", "text": "The deploy key is in the vault.", "status": "verified", "salience": 4,
                        "evidence": [{"ref": refs[1], "quote": "under ops/deploy"}]},
                       {"op": "add", "text": "Log lines repeat.", "status": "verified", "salience": 2,
                        "evidence": [{"ref": refs[2], "quote": "log line 2: detail"}]})
            self.dream({"op": "merge", "claim_ids": ["n000001", "n000002"], "text": "Ops notes."})
        return refs

    def night(self, moment):
        with at(moment):
            return self.dream({"op": "add", "text": "A night passed.", "status": "uncertain", "salience": 1,
                               "evidence": [{"ref": self.m.load().transcript[-1]["id"]}]})

    def test_a_night_dream_forgets_old_reviewed_records_but_keeps_their_quotes_and_chain(self):
        refs = self.old_day()
        with patch.object(S, "FORGET_KEEP_RECORDS", 2):
            self.record(DAY40, "today 1", "today 2", "today 3")
            self.night(DAY1)  # too soon: nothing is 30 days old yet
            self.assertEqual(self.reload().forgotten_counts()["records"], 0)
            self.night(DAY40)
        memory = self.reload()  # the chain, every claim's quotes and the merge provenance still verify
        forgotten = [r for r in memory.transcript if "forgotten" in r]
        self.assertEqual([r["id"] for r in forgotten], refs)
        stub = memory._records_by_id[refs[1]]["text"]
        self.assertTrue(stub.startswith("[forgotten 2026-02-10] The deploy key lives in the vault under ops/deploy."))
        self.assertLess(len(stub), 220)
        self.assertEqual(memory.search("ops/deploy", 5)[0]["id"], refs[1])
        self.assertEqual(memory.claims["n000003"]["state"], "active")  # the merge's provenance hash still checks
        self.assertTrue(all(not r["text"].startswith("[forgotten") for r in memory.transcript[len(refs):]))
        self.assertGreater(memory.status()["forgotten"]["bytes_dropped"], 600)
        # A quote that isn't in the stub's opening is kept after it.
        long = "x " * 120 + "the rare quoted words"
        self.assertIn("quotes kept: the rare quoted words", SomniMemory._stub({"text": long}, {"the rare quoted words"},
                                                                              S._parse_time(DAY40))["text"])

    def test_forgetting_respects_its_setting_live_passes_and_the_newest_records(self):
        self.old_day()
        with patch.object(S, "FORGET_KEEP_RECORDS", 1):
            with patch.dict(os.environ, {"SOMNI_FORGET_AFTER_DAYS": "0"}):
                self.night(DAY90)
            self.assertEqual(self.reload().forgotten_counts()["records"], 0)
            # A live pass (a daydream or deep sleep) never forgets.
            with at(DAY90):
                self.dream({"op": "add", "text": "live", "status": "uncertain", "salience": 1,
                            "evidence": [{"ref": "t000001"}]}, snapshot=6)
            self.assertEqual(self.reload().forgotten_counts()["records"], 0)
            # The newest records (the next dream's window) stay, however old they are.
            self.record(DAY0, "the newest record, stamped long ago " + "z " * 100)
            self.night(DAY90)
        memory = self.reload()
        self.assertEqual(memory.forgotten_counts()["records"], 6)
        self.assertNotIn("forgotten", memory.transcript[-1])

    def test_the_dream_window_sets_how_many_newest_records_are_kept(self):
        refs = self.old_day()
        self.record(DAY0, *[f"more old detail {i} " + "w " * 120 for i in range(4)])
        with patch.dict(os.environ, {"SOMNI_DREAM_RECORDS": "3"}):
            self.night(DAY40)
        memory = self.reload()
        self.assertEqual([r["id"] for r in memory.transcript if "forgotten" in r], refs + ["t000007"])

    def test_records_an_action_word_replays_are_never_forgotten(self):
        with at(DAY0):
            self.m.set_phase("open")
            note = "n" * 400  # long results, so forgetting them would save space
            for name, args, result in [("get_booking", {"booking_id": "BK-1"}, {"booking_id": "BK-1", "refundable": True}),
                                       ("cancel_booking", {"booking_id": "BK-1", "reason": "customer request"},
                                        {"status": "cancelled"}),
                                       ("get_booking", {"booking_id": "BK-2"}, {"booking_id": "BK-2", "refundable": False})]:
                self.m.record("tool_result", f"TOOL {name}({json.dumps(args)}) -> {json.dumps({**result, 'note': note})}",
                              tool_ok=True)
            self.m.record("tool_result", "unrelated " * 60)
            self.m.set_phase("closed")
            self.m.dream({"schema": 1, "operations": [{"op": "define_action", "source": CANCEL, "tests": TESTS}]},
                         actions=ON)
        with patch.object(S, "FORGET_KEEP_RECORDS", 0):
            self.night(DAY90)
        memory = self.reload()
        self.assertEqual([r["id"] for r in memory.transcript if "forgotten" in r], ["t000004"])
        self.assertIn("cancel_if_refundable", memory.action_words())

    def test_forgetting_rewrites_sealed_segments(self):
        refs = self.old_day()
        self.m.load()
        with self.m._locked():
            self.m._load_locked()
            self.m._seal("transcript.jsonl")
        self.record(DAY40, "after the seal")
        before = self.m.load().state["segments"]["transcript.jsonl"][0]["file"]
        with patch.object(S, "FORGET_KEEP_RECORDS", 1):
            self.night(DAY40)
        memory = self.reload()
        [segment] = memory.state["segments"]["transcript.jsonl"]
        self.assertNotEqual(segment["file"], before)
        self.assertFalse((self.ws / "somni-v1/segments" / before).exists())
        self.assertEqual([r["id"] for r in memory.transcript if "forgotten" in r], refs)
        with self.m._locked():  # the next seal picks a fresh file name
            self.m._load_locked()
            self.assertNotEqual(self.m._next_segment_file("transcript.jsonl"), segment["file"])

    def test_an_unusable_forgetting_value_keeps_every_record(self):
        # Forgetting can't be undone: "off" or a typo must not fall back to the 30-day default.
        self.old_day()
        with patch.object(S, "FORGET_KEEP_RECORDS", 0):
            for value in ("off", "never", "false", "30 days", "nan", "-1"):
                with patch.dict(os.environ, {"SOMNI_FORGET_AFTER_DAYS": value}):
                    self.night(DAY90)
                    memory = self.reload()
                    self.assertEqual(memory.forgotten_counts()["records"], 0, value)
                    self.assertEqual(memory.status()["forgetting"]["after_days"], 0)
                    self.assertIn("keeps every record", memory.status()["forgetting"]["problem"])
            with patch.dict(os.environ, {"SOMNI_FORGET_AFTER_DAYS": ""}):
                self.assertEqual(self.reload().status()["forgetting"], {"after_days": 30})

    def test_a_bad_forgetting_value_in_a_config_file_means_keep_everything(self):
        conf = self.ws / "somni.conf"
        for text in ("memory:\n  forget_after_days: off\n", "memory:\n  forget_after_days: [30]\n"):
            conf.write_text(text)
            out = C.resolve(self.ws, {"SOMNI_CONFIG": str(conf)})
            self.assertEqual(out["settings"]["memory.forget_after_days"]["value"], 0)
            self.assertEqual(out["env"]["SOMNI_FORGET_AFTER_DAYS"], "0")
            [error] = out["errors"]
            self.assertIn("Somni uses 0, keeping every record", error["message"])
        out = C.resolve(self.ws, {"SOMNI_CONFIG": "none", "SOMNI_FORGET_AFTER_DAYS": "off"})
        self.assertEqual(out["settings"]["memory.forget_after_days"]["value"], 0)
        self.assertIn("Somni uses 0, keeping every record", out["warnings"][0]["message"])

    def test_the_night_pass_lists_each_stub_so_an_edited_stub_is_caught(self):
        refs = self.old_day()
        with patch.object(S, "FORGET_KEEP_RECORDS", 0):
            self.night(DAY40)
        memory = self.reload()
        listed = memory.dreams[-1]["forgot"]
        self.assertEqual(sorted(listed, key=int), [str(n) for n in range(1, len(refs) + 1)])
        self.assertEqual(listed["2"], S.sha(memory.transcript[1]))
        path = self.ws / "somni-v1/transcript.jsonl"
        lines = path.read_text().splitlines()
        record = json.loads(lines[1])
        record["text"] += " … fabricated evidence"
        lines[1] = S.canonical(record)
        path.write_text("\n".join(lines) + "\n")
        with self.assertRaisesRegex(SomniError, "t000002 does not match the night pass that forgot it"):
            self.reload()

    def test_one_night_forgets_at_most_a_bounded_number_of_records(self):
        self.old_day()
        with patch.object(S, "FORGET_KEEP_RECORDS", 0), patch.object(S, "FORGET_PER_PASS", 2):
            self.night(DAY40)
            self.assertEqual([r["id"] for r in self.reload().transcript if "forgotten" in r], ["t000001", "t000002"])
            self.night(DAY40)
            self.assertEqual(self.reload().forgotten_counts()["records"], 4)

    def test_decay_zero_ranks_by_uses_alone(self):
        self.assertEqual(S._activation([1, 1000], 0.0), S._activation([0.1, 5], 0.0))
        self.blue_then_red()
        memory = self.reload()
        with at(DAY90), patch.dict(os.environ, {"SOMNI_MEMORY_DECAY": "0"}):
            self.assertEqual(memory.strength(memory.claims["n000001"])["level"], "fresh")

    def test_a_tampered_stub_fails_verification(self):
        self.old_day()
        with patch.object(S, "FORGET_KEEP_RECORDS", 0):
            self.night(DAY40)
        path = self.ws / "somni-v1/transcript.jsonl"
        lines = path.read_text().splitlines()
        record = json.loads(lines[2])
        record["text"] = "rewritten history"
        lines[2] = S.canonical(record)
        path.write_text("\n".join(lines) + "\n")
        with self.assertRaisesRegex(SomniError, "invalid forgotten record"):
            self.reload()


if __name__ == "__main__":
    unittest.main()
