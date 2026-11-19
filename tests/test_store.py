#!/usr/bin/env python3

import sys
import contextlib
import io
import json
import pathlib
import select
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from somni import store as layered  # noqa: E402


class SomniStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = pathlib.Path(self.temp.name) / "attempt"
        self.workspace.mkdir()
        self.memory = layered.LayeredMemory.create(self.workspace, "subject-a", "lineage-a", "task001-a")

    def tearDown(self):
        self.temp.cleanup()

    def open_with_evidence(self, text="Inventory dirt changed from 0 to 1 after harvest."):
        self.memory.set_phase("open")
        ref = self.memory.record("ordinary_observation", text)
        return ref

    @staticmethod
    def evidence(ref, quote=None):
        value = {"ref": ref}
        if quote is not None:
            value["quote"] = quote
        return [value]

    def lineage_bytes(self):
        return tuple(
            (self.memory.store / name).read_bytes()
            for name in ("state.json", "notes.jsonl", "dreams.jsonl")
        )

    def test_scheduled_verification_checks_after_access_count_and_time(self):
        ref = self.open_with_evidence()
        session = layered.SomniMemory(self.workspace, "subject-a", scheduled_verification=True)
        with mock.patch.object(layered, "VERIFY_AFTER_ACCESSES", 2), mock.patch.object(
            layered, "monotonic", return_value=100
        ) as now, mock.patch.object(session, "_verify_locked", wraps=session._verify_locked) as audit:
            session.load()
            session.record("agent_output", "Recorded through the verified snapshot.")
            claim = session.add_note("Inventory changed.", "verified", self.evidence(ref, "changed"))
            self.assertEqual(audit.call_count, 1)
            self.assertEqual(session.note_log[-1]["claim"]["id"], claim)
            self.assertIn(claim, session.inject())
            session.load()
            self.assertEqual(audit.call_count, 2)
            now.return_value += layered.VERIFY_AFTER_SECONDS
            session.load()
            self.assertEqual(audit.call_count, 3)

    def test_scheduled_verification_detects_external_writes_and_tampering(self):
        self.open_with_evidence("Original evidence.")
        session = layered.SomniMemory(self.workspace, "subject-a", scheduled_verification=True).load()
        self.memory.record("agent_output", "Written by another store client.")
        self.assertEqual(session.record("agent_output", "Next session record."), "t000003")
        log = self.memory.store / "transcript.jsonl"
        log.write_text(log.read_text().replace("Original evidence.", "Tampered evidence."))
        with self.assertRaisesRegex(layered.SomniError, "broken transcript chain"):
            session.load()

    def test_scheduled_verification_recovers_from_interrupted_write(self):
        first = self.open_with_evidence()
        session = layered.SomniMemory(self.workspace, "subject-a", scheduled_verification=True).load()
        with mock.patch.object(session, "_save_state", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                session.record("agent_output", "Uncommitted cached output.")
        self.assertEqual([record["id"] for record in session.load().transcript], [first])
        self.assertEqual(session.record("agent_output", "Committed after recovery."), "t000002")
        self.assertEqual(session.load().transcript[-1]["text"], "Committed after recovery.")

    def merge_fixture(self):
        first = self.open_with_evidence("The preview lists the fee.")
        second = self.memory.record("tool_result", "Confirmation is required for a write.")
        left = self.memory.add_note("Look up the fee.", "verified", self.evidence(first, "lists the fee"))
        right = self.memory.add_note("Ask for confirmation.", "uncertain", self.evidence(second))
        self.memory.set_phase("closed")
        return left, right

    def test_merge_digest_is_parent_order_independent_and_tracks_content(self):
        left, right = self.merge_fixture()
        merge = {"op": "merge", "claim_ids": [right, left], "text": "Quote the fee, then confirm."}
        self.memory.dream({"schema": 1, "operations": [
            merge,
            {"op": "promote", "claim_id": left, "salience": 5},
            {**merge, "claim_ids": [left, right]},
            {**merge, "text": "Explain the fee, then confirm."},
            {**merge, "claim_ids": [left, "n000003"]},
        ]})
        claims = self.memory.load().claims
        merged = claims["n000003"]
        self.assertEqual(merged["merged_from"], [left, right])
        self.assertEqual(merged["evidence"], self.evidence("t000001", "lists the fee") + self.evidence("t000002"))
        self.assertEqual(merged["status"], "uncertain")
        self.assertEqual(merged["provenance_sha256"], claims["n000004"]["provenance_sha256"])
        self.assertNotEqual(merged["provenance_sha256"], claims["n000005"]["provenance_sha256"])
        self.assertNotEqual(merged["provenance_sha256"], claims["n000006"]["provenance_sha256"])
        self.assertTrue(all(claims[ref]["state"] == "active" for ref in (left, right)))

    def test_merge_replay_rejects_tampered_provenance_even_with_valid_chain_head(self):
        left, right = self.merge_fixture()
        self.memory.dream({"schema": 1, "operations": [
            {"op": "merge", "claim_ids": [left, right], "text": "Quote the fee, then confirm."},
        ]})
        log = self.memory.store / "notes.jsonl"
        notes = [json.loads(line) for line in log.read_text().splitlines()]
        notes[-1]["claim"]["provenance_sha256"] = "f" * 64
        log.write_bytes(b"".join(layered._encode_log_record(item) for item in notes))
        state = {**self.memory.state, "notes_head": layered.sha(notes[-1])}
        layered._atomic_json(self.memory.store / "state.json", state)
        with self.assertRaisesRegex(layered.SomniError, "provenance hash"):
            self.memory.load()

    def test_interrupted_append_recovers_the_last_committed_transcript(self):
        first = self.open_with_evidence()
        with mock.patch.object(self.memory, "_save_state", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                self.memory.record("agent_output", "Uncommitted output.")
        reloaded = layered.SomniMemory(self.workspace, "subject-a").load()
        self.assertEqual([entry["id"] for entry in reloaded.transcript], [first])
        self.assertEqual(reloaded.record("agent_output", "Retried output."), "t000002")
        self.assertFalse((reloaded.store / ".transaction.json").exists())

    def test_record_cli_can_return_the_index_without_recording_twice(self):
        ref = self.open_with_evidence()
        self.memory.add_note("Remember the harvest.", "uncertain", self.evidence(ref))
        argv = ["store.py", "--workspace", str(self.workspace), "--subject-id", "subject-a",
                "record", "--source", "ordinary_observation", "--text", "New event.",
                "--inject-max-bytes", "4096"]
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(layered.main(), 0)
        self.assertEqual(output.getvalue(), self.memory.load().inject(4096))
        self.assertEqual(self.memory.state["transcript_count"], 2)

    def test_committed_state_survives_interrupted_transaction_cleanup(self):
        self.open_with_evidence()
        with mock.patch.object(self.memory, "_clear_transaction", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                self.memory.record("agent_output", "Committed output.")
        reloaded = layered.SomniMemory(self.workspace, "subject-a").load()
        self.assertEqual(reloaded.transcript[-1]["text"], "Committed output.")
        self.assertFalse((reloaded.store / ".transaction.json").exists())

    def test_backend_reacquires_recording_after_owner_is_killed(self):
        cmd = [sys.executable, "-I", str(ROOT / "somni/store.py"), "--workspace", str(self.workspace),
               "--subject-id", "subject-a", "serve", "--record"]
        owner = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertTrue(select.select([owner.stdout], [], [], 10)[0], "backend did not report readiness")
            self.assertTrue(json.loads(owner.stdout.readline())["result"]["writable"])
        finally:
            owner.kill()
            owner.communicate(timeout=10)
        self.assertEqual(self.memory.load().state["phase"], "open")
        with subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True) as replacement:
            stdout, _ = replacement.communicate(input="", timeout=10)
            self.assertEqual(replacement.returncode, 0)
        self.assertTrue(json.loads(stdout)["result"]["writable"])
        self.assertEqual(self.memory.load().state["phase"], "closed")

    def test_interrupted_dream_rolls_back_multiple_logs_and_a_partial_line(self):
        ref = self.open_with_evidence()
        self.memory.set_phase("closed")
        proposal = {"schema": 1, "operations": [
            {"op": "add", "text": "Check inventory.", "status": "uncertain",
             "evidence": self.evidence(ref), "salience": 3},
            {"op": "page", "slug": "inventory", "title": "Inventory", "body": "Check inventory [n000001].",
             "claims": ["n000001"]},
        ]}
        before = self.lineage_bytes()
        append = layered._append_encoded

        def interrupt_wiki(path, encoded):
            if path.name == "wiki.jsonl":
                append(path, encoded[:10])
                raise OSError("interrupted")
            append(path, encoded)

        with mock.patch.object(layered, "_append_encoded", side_effect=interrupt_wiki):
            with self.assertRaises(OSError):
                self.memory.dream(proposal)
        reloaded = layered.SomniMemory(self.workspace, "subject-a").load()
        self.assertEqual(self.lineage_bytes(), before)
        self.assertEqual((reloaded.store / "wiki.jsonl").read_bytes(), b"")
        self.assertEqual(reloaded.dream(proposal)["operation_count"], 2)

    def test_identity_scope_isolation_and_create_only(self):
        with self.assertRaises(layered.MemoryError):
            layered.LayeredMemory(self.workspace, "sibling-subject").load()
        with self.assertRaises(layered.MemoryError):
            layered.LayeredMemory.create(self.workspace, "subject-a", "lineage-a", "other")
        sibling = pathlib.Path(self.temp.name) / "sibling"
        sibling.mkdir()
        other = layered.LayeredMemory.create(sibling, "subject-b", "lineage-b", "task001-b")
        self.assertEqual(other.manifest["subject_id"], "subject-b")
        self.assertEqual(self.memory.load().manifest["attempt_id"], "task001-a")

    def test_path_traversal_workspace_and_input_symlinks_are_rejected(self):
        alias = pathlib.Path(self.temp.name) / "alias"
        alias.symlink_to(self.workspace, target_is_directory=True)
        with self.assertRaises(layered.MemoryError):
            layered.LayeredMemory(alias, "subject-a")
        outside = pathlib.Path(self.temp.name) / "outside.json"
        outside.write_text("{}")
        link = self.workspace / "proposal.json"
        link.symlink_to(outside)
        with self.assertRaises(layered.MemoryError):
            layered._input_json(self.memory.load(), "proposal.json")
        with self.assertRaises(layered.MemoryError):
            layered._input_json(self.memory, "../outside.json")

    def test_store_file_symlink_is_rejected(self):
        transcript = self.memory.store / "transcript.jsonl"
        transcript.unlink()
        transcript.symlink_to(self.memory.store / "notes.jsonl")
        with self.assertRaises(layered.MemoryError):
            self.memory.load()

    def test_transcript_is_append_only_and_tampering_is_detected(self):
        self.open_with_evidence()
        self.memory.set_phase("closed")
        path = self.memory.store / "transcript.jsonl"
        original = json.loads(path.read_text())
        original["text"] = "rewritten evidence"
        path.write_text(layered.canonical(original) + "\n")
        with self.assertRaises(layered.MemoryError):
            self.memory.load()

    def test_session_phase_separates_subject_writes_from_offline_dreaming(self):
        ref = self.open_with_evidence()
        proposal = {"schema": 1, "operations": [{
            "op": "add", "text": "Harvest increased dirt.", "status": "uncertain",
            "evidence": self.evidence(ref), "salience": 3,
        }]}
        with self.assertRaises(layered.MemoryError):
            self.memory.dream(proposal)
        self.memory.set_phase("closed")
        self.memory.dream(proposal)
        with self.assertRaises(layered.MemoryError):
            self.memory.record("ordinary_observation", "concurrent write")

    def test_evidence_references_and_verified_quotes_are_enforced(self):
        ref = self.open_with_evidence()
        with self.assertRaises(layered.MemoryError):
            self.memory.add_note("Unsupported", "uncertain", [{"ref": "t999999"}])
        with self.assertRaises(layered.MemoryError):
            self.memory.add_note("Bad quote", "verified", self.evidence(ref, "not present"))
        with self.assertRaises(layered.MemoryError):
            self.memory.add_note("No quote", "verified", self.evidence(ref))
        claim = self.memory.add_note(
            "A harvest can increase dirt inventory.", "verified",
            self.evidence(ref, "dirt changed from 0 to 1"), salience=5,
        )
        self.assertEqual(claim, "n000001")

    def test_dream_correction_and_retraction_preserve_provenance(self):
        ref = self.open_with_evidence()
        first = self.memory.add_note("Harvest always succeeds.", "uncertain", self.evidence(ref))
        self.memory.set_phase("closed")
        dream = self.memory.dream({"schema": 1, "operations": [{
            "op": "correct", "claim_id": first,
            "text": "This observed harvest increased dirt; other attempts may differ.",
            "status": "verified", "evidence": self.evidence(ref, "changed from 0 to 1"),
            "salience": 5, "links": [first],
        }]})
        self.assertEqual(dream["operation_count"], 1)
        self.assertEqual(self.memory.claims[first]["state"], "superseded")
        self.assertEqual(self.memory.claims["n000002"]["replaces"], first)
        self.memory.dream({"schema": 1, "operations": [{
            "op": "retract", "claim_id": "n000002", "reason": "Too broad for the evidence.",
            "evidence": self.evidence(ref, "after harvest"),
        }]})
        self.assertEqual(self.memory.claims["n000002"]["state"], "retracted")
        self.assertEqual(self.memory.metrics()["dream_passes"], 2)

    def test_claim_cap_applies_to_direct_notes_and_existing_logs(self):
        self.assertEqual(layered.MAX_CLAIMS, 512)
        ref = self.open_with_evidence()
        with mock.patch.object(layered, "MAX_CLAIMS", 2):
            self.memory.add_note("First claim.", "uncertain", self.evidence(ref))
            self.memory.add_note("Second claim.", "uncertain", self.evidence(ref))
            before = self.lineage_bytes()
            with self.assertRaisesRegex(layered.MemoryError, "claim limit reached"):
                self.memory.add_note("Overflow claim.", "uncertain", self.evidence(ref))
            self.assertEqual(self.lineage_bytes(), before)
            self.assertEqual(len(self.memory.load().claims), 2)
            with mock.patch.object(layered, "MAX_CLAIMS", 1):
                with self.assertRaisesRegex(layered.MemoryError, "claim limit reached"):
                    layered.LayeredMemory(self.workspace, "subject-a").load()

    def test_multi_operation_dream_overflow_is_atomic(self):
        ref = self.open_with_evidence()
        with mock.patch.object(layered, "MAX_CLAIMS", 3):
            self.memory.add_note("First claim.", "uncertain", self.evidence(ref))
            self.memory.add_note("Second claim.", "uncertain", self.evidence(ref))
            self.memory.set_phase("closed")
            before = self.lineage_bytes()
            proposal = {"schema": 1, "operations": [
                {"op": "add", "text": "Fits alone.", "status": "uncertain",
                 "evidence": self.evidence(ref), "salience": 3},
                {"op": "add", "text": "Would overflow.", "status": "uncertain",
                 "evidence": self.evidence(ref), "salience": 3},
            ]}
            with self.assertRaisesRegex(layered.MemoryError, "claim limit reached"):
                self.memory.dream(proposal)
            self.assertEqual(self.lineage_bytes(), before)
            reloaded = self.memory.load()
            self.assertEqual(len(reloaded.claims), 2)
            self.assertEqual(reloaded.state["next_claim"], 3)
            self.assertEqual(reloaded.state["dream_count"], 0)

    def test_multi_operation_dream_log_byte_overflow_is_atomic(self):
        quote = "e" * 1_024
        ref = self.open_with_evidence("Large exact evidence: " + quote)
        self.memory.set_phase("closed")
        proposal = {"schema": 1, "operations": [
            {"op": "add", "text": "First bounded claim.", "status": "verified",
             "evidence": self.evidence(ref, quote), "salience": 3},
            {"op": "add", "text": "Second bounded claim.", "status": "verified",
             "evidence": self.evidence(ref, quote), "salience": 3},
        ]}
        first_claim = {"id": "n000001", "text": "First bounded claim.", "status": "verified",
                       "salience": 3, "evidence": self.evidence(ref, quote), "links": []}
        first_event = {"schema": 1, "seq": 1, "op": "add", "claim": first_claim,
                       "prev_sha256": layered.ZERO_HASH}
        second_claim = {"id": "n000002", "text": "Second bounded claim.", "status": "verified",
                        "salience": 3, "evidence": self.evidence(ref, quote), "links": []}
        second_event = {"schema": 1, "seq": 2, "op": "add", "claim": second_claim,
                        "prev_sha256": layered.sha(first_event)}
        boundary = len(layered._encode_log_record(first_event)) + len(layered._encode_log_record(second_event)) - 1
        self.assertLess(len(layered._encode_log_record(first_event)), boundary)
        self.assertLess((self.memory.store / "transcript.jsonl").stat().st_size, boundary)
        before = self.lineage_bytes()
        with mock.patch.object(layered, "MAX_LOG_BYTES", boundary):
            with self.assertRaisesRegex(layered.MemoryError, "notes.jsonl reached"):
                self.memory.dream(proposal)
        self.assertEqual(self.lineage_bytes(), before)
        reloaded = self.memory.load()
        self.assertEqual(reloaded.claims, {})
        self.assertEqual(reloaded.state["note_events"], 0)
        self.assertEqual(reloaded.state["dream_count"], 0)

    def test_retraction_at_cap_is_allowed_but_correction_does_not_free_capacity(self):
        ref = self.open_with_evidence()
        claim = self.memory.add_note("Overbroad claim.", "uncertain", self.evidence(ref))
        self.memory.set_phase("closed")
        with mock.patch.object(layered, "MAX_CLAIMS", 1):
            before = self.lineage_bytes()
            with self.assertRaisesRegex(layered.MemoryError, "claim limit reached"):
                self.memory.dream({"schema": 1, "operations": [{
                    "op": "correct", "claim_id": claim, "text": "Narrower claim.",
                    "status": "uncertain", "evidence": self.evidence(ref), "salience": 3,
                }]})
            self.assertEqual(self.lineage_bytes(), before)
            self.assertEqual(self.memory.load().claims[claim]["state"], "active")
            self.memory.dream({"schema": 1, "operations": [{
                "op": "retract", "claim_id": claim, "reason": "Overbroad.",
                "evidence": self.evidence(ref),
            }]})
            self.assertEqual(self.memory.claims[claim]["state"], "retracted")
            self.assertEqual(self.memory.load().claims[claim]["state"], "retracted")
            before = self.lineage_bytes()
            with self.assertRaisesRegex(layered.MemoryError, "claim limit reached"):
                self.memory.dream({"schema": 1, "operations": [{
                    "op": "add", "text": "Retraction did not free this slot.",
                    "status": "uncertain", "evidence": self.evidence(ref), "salience": 3,
                }]})
            self.assertEqual(self.lineage_bytes(), before)

    def test_exact_512th_claim_can_be_created_by_dream_but_no_more(self):
        ref = self.open_with_evidence()
        for number in range(511):
            self.memory.add_note(f"Immutable claim {number + 1}.", "uncertain", self.evidence(ref))
        self.memory.set_phase("closed")
        dream = self.memory.dream({"schema": 1, "operations": [{
            "op": "add", "text": "Exact final immutable claim.", "status": "uncertain",
            "evidence": self.evidence(ref), "salience": 3,
        }]})
        self.assertEqual(dream["operation_count"], 1)
        self.assertEqual(len(self.memory.claims), 512)
        self.assertIn("n000512", self.memory.claims)
        before = self.lineage_bytes()
        with self.assertRaisesRegex(layered.MemoryError, "claim limit reached"):
            self.memory.dream({"schema": 1, "operations": [{
                "op": "add", "text": "Claim 513 must fail.", "status": "uncertain",
                "evidence": self.evidence(ref), "salience": 3,
            }]})
        self.assertEqual(self.lineage_bytes(), before)

    def test_malformed_dream_types_and_null_correction_target_are_memory_errors(self):
        ref = self.open_with_evidence()
        claim = self.memory.add_note("Existing claim.", "uncertain", self.evidence(ref))
        self.memory.set_phase("closed")
        proposals = [
            {"schema": 1, "operations": [{"op": {"unhashable": True}}]},
            {"schema": 1, "operations": [{
                "op": "correct", "claim_id": None, "text": "Must not become an add.",
                "status": "uncertain", "evidence": self.evidence(ref), "salience": 3,
            }]},
            {"schema": 1, "operations": [{
                "op": "retract", "claim_id": [], "reason": "Invalid target.",
                "evidence": self.evidence(ref),
            }]},
            {"schema": 1, "operations": [{
                "op": "add", "text": "Invalid status.", "status": [],
                "evidence": self.evidence(ref), "salience": 3,
            }]},
        ]
        before = self.lineage_bytes()
        for proposal in proposals:
            with self.subTest(proposal=proposal):
                with self.assertRaises(layered.MemoryError):
                    self.memory.dream(proposal)
                self.assertEqual(self.lineage_bytes(), before)
        self.assertEqual(self.memory.load().claims[claim]["state"], "active")

    def test_replay_rejects_self_replacement(self):
        ref = self.open_with_evidence()
        claim = {"id": "n000001", "text": "Self replacement.", "status": "uncertain",
                 "salience": 3, "evidence": self.evidence(ref), "links": [], "replaces": "n000001"}
        event = {"schema": 1, "seq": 1, "op": "add", "claim": claim,
                 "prev_sha256": layered.ZERO_HASH}
        layered._append(self.memory.store / "notes.jsonl", event)
        state = dict(self.memory.state)
        state.update({"note_events": 1, "notes_head": layered.sha(event), "next_claim": 2})
        layered._atomic_json(self.memory.store / "state.json", state)
        with self.assertRaisesRegex(layered.MemoryError, "replacement target"):
            self.memory.load()

    def test_reload_and_views_are_deterministic(self):
        ref = self.open_with_evidence("Tool result: harvest accepted; dirt is now 1.")
        self.memory.add_note("One harvest was accepted.", "verified", self.evidence(ref, "harvest accepted"))
        self.memory.set_phase("closed")
        before = (self.memory.inject(), self.memory.report(), self.memory.metrics())
        reloaded = layered.LayeredMemory(self.workspace, "subject-a").load()
        after = (reloaded.inject(), reloaded.report(), reloaded.metrics())
        self.assertEqual(before, after)

    def test_injection_is_bounded_and_overgrown_linked_wiki_stays_on_demand(self):
        ref = self.open_with_evidence("A compact piece of evidence for many interpretations.")
        previous = None
        for number in range(80):
            previous = self.memory.add_note(
                f"Interpretation {number}: " + ("x" * 100), "uncertain", self.evidence(ref),
                salience=(number % 5) + 1, links=[] if previous is None else [previous],
            )
        index = self.memory.inject(512)
        self.assertLessEqual(len(index.encode("utf-8")), 512)
        self.assertIn("additional active claims omitted", index)
        self.assertNotIn("Interpretation 0:", index)  # low-salience material stays on demand
        self.assertEqual(len(self.memory.claims), 80)

    def test_search_and_operator_report_use_only_subject_records(self):
        first = self.open_with_evidence("I can see dirt inventory 0.")
        self.memory.record("tool_result", "Harvest was accepted and inventory is now 1.")
        self.memory.add_note("Harvest changed inventory.", "uncertain", self.evidence(first))
        found = self.memory.search("inventory")
        self.assertEqual([item["id"] for item in found], ["t000001", "t000002"])
        report = self.memory.report()
        self.assertIn("# Somni memory report", report)
        self.assertIn("Evidence transcript", report)
        self.assertIn("semantic proof", report)

    def test_condition_is_separate_from_chel_and_memento_files(self):
        memento = self.workspace / "memento"
        memento.mkdir()
        sentinel = memento / "sentinel"
        sentinel.write_text("unchanged")
        ref = self.open_with_evidence()
        self.memory.add_note("Separate condition.", "uncertain", self.evidence(ref))
        self.assertEqual(sentinel.read_text(), "unchanged")
        self.assertFalse((self.memory.store / "psi.json").exists())
        source = (ROOT / "somni" / "store.py").read_text()
        self.assertNotIn("import memento", source)


if __name__ == "__main__":
    unittest.main()
