"""Verify Somni's STATE-Bench adapter with synthetic trajectories and scripted completions."""

import json
import pathlib
import tempfile
import unittest

from evals.state_bench import somni_state_bench as sb
from somni.store import SomniMemory


class StateBenchAdapterTest(unittest.TestCase):
    CONVERSATION = [
        {"role": "system", "content": "You are a travel agent."},
        {"role": "user", "content": "Cancel BK-1."},
        {"role": "assistant", "content": "Here is the preview.", "tool_calls": [
            {"name": "cancel_booking", "arguments": {"booking_id": "BK-1", "confirm": False},
             "result": {"status": "preview", "cancellation_fee": 50}}]},
        {"role": "user", "content": "Yes, go ahead."},
    ]

    def test_legacy_import_forwards_to_evaluation_adapter(self):
        from adapters.state_bench import somni_state_bench as legacy

        self.assertIs(legacy.SomniWikiSearchLearnings, sb.SomniWikiSearchLearnings)
        self.assertIs(legacy.trajectory_records, sb.trajectory_records)

    def test_trajectory_records_and_build_and_retrieve(self):
        items = sb.trajectory_records("t1", self.CONVERSATION)
        self.assertEqual([s for s, _ in items], ["ordinary_observation", "ordinary_observation", "ordinary_observation",
                                                   "tool_result", "agent_output", "ordinary_observation", "ordinary_observation"])
        self.assertIn('TOOL cancel_booking({"booking_id": "BK-1", "confirm": false})', items[3][1])
        with tempfile.TemporaryDirectory() as tmp:
            train = pathlib.Path(tmp) / "travel"
            train.mkdir()
            for i in range(2):
                (train / f"{i + 1}-task.json").write_text(json.dumps({"conversation": self.CONVERSATION}))

            def complete(system, prompt):
                ref = prompt.split("[t", 1)[1][:6]
                return json.dumps({"operations": [{"op": "add", "status": "uncertain", "salience": 5,
                                                   "text": "Show the cancellation preview before confirming.",
                                                   "evidence": [{"ref": "t" + ref}]}]})
            summary = sb.build(train, pathlib.Path(tmp) / "out" / "travel", complete, per_pass=1, log=lambda *_: None)
            self.assertEqual(summary["metrics"]["active_claims"], 2)

            class Agent(sb.SomniLearnings):
                somni_root = str(pathlib.Path(tmp) / "out")

                class runtime_context:
                    domain = "travel"
            learnings = Agent().retrieve_learnings("cancellation preview", 3)
            self.assertEqual(len(learnings), 2)
            self.assertTrue(all(isinstance(x, str) and "preview" in x for x in learnings))
            self.assertEqual(Agent().retrieve_learnings("unrelated words", 3), [])


class EpisodeMetaTest(unittest.TestCase):
    def make(self, tmp):
        tasks = pathlib.Path(tmp) / "tasks"
        tasks.mkdir()
        for task_id, now in (("1-train", "2026-06-15T10:00:00"), ("2-test", "2026-07-01T09:00:00")):
            (tasks / f"{task_id}.json").write_text(json.dumps({"task_id": task_id, "now": now, "task_summary": "secret"}))
        split = pathlib.Path(tmp) / "split.json"
        split.write_text(json.dumps({"splits": {"train": ["1-train"], "test": ["2-test"]}}))
        return tasks, split

    def test_only_train_ids_and_only_requested_fields_are_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            tasks, split = self.make(tmp)
            meta = sb.train_episode_meta(tasks, split)
            self.assertEqual(meta, {"1-train": {"now": "2026-06-15T10:00:00"}})
            self.assertEqual(sb._episode_for("1-train", meta), {"now": "2026-06-15T10:00:00"})
            with self.assertRaisesRegex(ValueError, "refusing"):
                sb._episode_for("2-test", meta)
            self.assertIsNone(sb._episode_for("anything", None))

    def test_now_is_recorded_as_an_observation(self):
        items = sb.trajectory_records("t1", [{"role": "user", "content": "hi"}], {"now": "2026-06-15T10:00:00"})
        self.assertEqual(items[1], ("ordinary_observation", "[episode t1] Today's date: 2026-06-15T10:00:00"))


class EpisodeSearchTest(unittest.TestCase):
    def test_episode_digest_and_ranking(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = SomniMemory.create(pathlib.Path(tmp), sb.SUBJECT, "l", "a")
            m.set_phase("open")
            for tid, req, tool in (("1-cancel", "Cancel my flight BK-1", "cancel_booking"), ("2-bag", "Add a checked bag", "add_bag")):
                m.record_many(sb.trajectory_records(tid, [{"role": "user", "content": req},
                    {"role": "assistant", "content": "Done.", "tool_calls": [{"name": tool, "arguments": {"id": 1}, "result": {"ok": True}}]}]))
            m.set_phase("closed")
            m = m.load()
            eps = sb.episodes(m)
            self.assertEqual(sorted(eps), ["1-cancel", "2-bag"])
            digest = sb.episode_digest("1-cancel", eps["1-cancel"])
            self.assertIn("USER: Cancel my flight", digest)
            self.assertIn('- cancel_booking({"id": 1})', digest)
            self.assertIn("Final answer: Done.", digest)
            docs = [sb._terms_for(" ".join(r["text"] for r in eps[k])) for k in eps]
            self.assertEqual(sb._rank(list(eps), docs, "checked bag")[0], "2-bag")


class FeedbackTest(unittest.TestCase):
    def test_feedback_text_lists_missed_requirements(self):
        text = sb.feedback_text("7-x", {"task_completion_pass": 0, "state_requirements_met": 0, "state_requirements_reasoning": "fee wrong",
                                        "task_requirements_details": [{"kind": "must", "requirement": "Preview first", "passed": False, "reasoning": "skipped"},
                                                                      {"kind": "must", "requirement": "Be polite", "passed": True}]})
        self.assertIn("outcome FAIL", text)
        self.assertIn("missed [must] Preview first -- reviewer: skipped", text)
        self.assertNotIn("Be polite", text)
        self.assertIn("final database state was wrong: fee wrong", text)
        self.assertIn("15-turn limit", sb.feedback_text("8-y", {"task_completion_pass": 0, "turns": 15}))


class WikiDreamingTest(unittest.TestCase):
    def test_build_wiki_fill_gaps_and_page_retrieval(self):
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
                return '{"operations": [{"op": "page", "slug": "overview", "title": "Overview", "body": "Start with [[cancellations]].", "claims": []}, {"op": "promote", "claim_id": "n000001", "salience": 5}]}'
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

            class SearchAgent(sb.SomniWikiSearchLearnings):
                somni_root = str(pathlib.Path(tmp) / "out")

                class runtime_context:
                    domain = "travel"
            class Layered(sb.SomniLayeredLearnings):
                somni_root = str(pathlib.Path(tmp) / "out")

                class runtime_context:
                    domain = "travel"
            layered = Layered().retrieve_learnings("how to cancel and what fee", 3)
            self.assertTrue(layered[0].startswith("# Always-loaded memory"))
            self.assertIn("page `cancellations`", layered[0])
            self.assertTrue(layered[1].startswith("# Cancellations"))
            self.assertFalse(any(x.startswith("# Overview") for x in layered))  # navigation pages are skipped
            class Briefed(sb.SomniBriefingLearnings):
                somni_root = str(pathlib.Path(tmp) / "out")

                class runtime_context:
                    domain = "travel"
            seen = []
            def fake(system, prompt):
                seen.append(prompt)
                return '{"briefing": "Preview the fee first, then confirm (cancellations)."}'
            fake.usage = type("U", (), {"as_dict": lambda self: {"calls": len(seen), "input_tokens": 10 * len(seen), "output_tokens": 5 * len(seen)}})()
            sb.SomniBriefingLearnings._completer = fake
            briefed = Briefed().retrieve_learnings("how to cancel and what fee", 3)
            self.assertEqual(briefed[0], "# Memory briefing for this situation\nPreview the fee first, then confirm (cancellations).")
            self.assertTrue(briefed[1].startswith("# Cancellations"))
            self.assertIn("SITUATION: how to cancel", seen[0])
            sb.SomniBriefingLearnings._completer = None
            m3 = SomniMemory(out, sb.SUBJECT).load()
            self.assertEqual(sb.episodes(m3), {})  # this toy store has no episode markers
            hits = SearchAgent().retrieve_learnings("how to cancel and what fee", 3)
            self.assertTrue(hits[0].startswith("# Cancellations"))
            self.assertLessEqual(len(hits), 3)
            self.assertTrue(got[0].startswith("# Cancellations"))
            self.assertEqual(len(got), 3)
