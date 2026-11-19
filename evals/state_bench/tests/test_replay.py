"""Exercise the offline replay against a minimal STATE-Bench checkout layout."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class ReplayTest(unittest.TestCase):
    def test_persistent_replay_uses_ordered_training_events(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            train = root / "datasets/train_task_trajectories/travel"
            train.mkdir(parents=True)
            (train / "10-next.json").write_text(json.dumps({
                "conversation": [{"role": "user", "content": "second trajectory"}],
            }))
            (train / "2-first.json").write_text(json.dumps({
                "conversation": [
                    {"role": "system", "content": "system message"},
                    {"role": "user", "content": "first trajectory"},
                    {"role": "assistant", "content": "done"},
                ],
            }))
            holdout = root / "datasets/test_task_trajectories/travel"
            holdout.mkdir(parents=True)
            (holdout / "1-held-out.json").write_text("must not be opened")
            result = subprocess.run(
                [sys.executable, "-m", "evals.state_bench.replay", "--state-bench-root", str(root),
                 "--events", "3", "--persistent"],
                check=True, capture_output=True, text=True, timeout=60,
            )
        report = json.loads(result.stdout)
        self.assertEqual((report["split"], report["events"], report["persistent"]), ("train", 3, True))
        self.assertEqual([(step["episode"], step["source"]) for step in report["steps"]], [
            ("2-first", "ordinary_observation"),
            ("2-first", "agent_output"),
            ("10-next", "ordinary_observation"),
        ])
        self.assertEqual(report["summary"]["record_and_inject"]["calls"], 2)
