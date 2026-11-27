#!/usr/bin/env python3
"""Snapshots: a store saved as one file, restored elsewhere with its chains checked; damaged or unsafe ones refused."""
import io
import json
import pathlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from somni import snapshot as N  # noqa: E402
from somni.store import SomniError, SomniMemory  # noqa: E402


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "a").mkdir()
        self.m = SomniMemory.create(self.root / "a", "pi", "l", "a")
        self.m.set_phase("open")
        said, = self.m.record_many([("ordinary_observation", "USER: deploys go out from release branches")])
        self.m.set_phase("closed")
        self.m.dream({"schema": 1, "operations": [{"op": "add", "text": "Deploy from release branches.",
                                                   "status": "verified", "salience": 3,
                                                   "evidence": [{"ref": said, "quote": "release branches"}]}]})
        self.m.set_phase("open", allow_same=True)  # a session is recording while the snapshot is taken
        self.file = self.root / "out" / "a.tar.gz"

    def test_a_store_moves_whole_and_its_chains_check_out_where_it_lands(self):
        manifest = N.snapshot(self.m, self.file)
        self.assertNotIn("memory.lock", manifest["files"])
        self.assertEqual(N.describe(self.file)["state"]["transcript_count"], 1)
        result = N.restore(self.file, self.root / "b", SomniMemory)
        copy = SomniMemory(self.root / "b", "pi").load()
        self.assertEqual(copy.claims["n000001"]["text"], "Deploy from release branches.")
        self.assertEqual((copy.state["transcript_head"], result["subject"]), (self.m.load().state["transcript_head"], "pi"))
        self.assertTrue((self.root / "b/somni-v1/memory.lock").is_file())
        with self.assertRaisesRegex(N.SnapshotError, "already exists"):
            N.snapshot(self.m, self.file)

    def test_restore_keeps_an_existing_memory_unless_told_and_then_sets_it_aside(self):
        N.snapshot(self.m, self.file)
        with self.assertRaisesRegex(N.SnapshotError, "already holds a memory store"):
            N.restore(self.file, self.root / "a", SomniMemory)
        result = N.restore(self.file, self.root / "a", SomniMemory, replace=True)
        self.assertTrue(pathlib.Path(result["kept_aside"]).is_dir())
        (self.root / "fresh").mkdir()
        SomniMemory.create(self.root / "fresh", "pi", "l", "a")  # a store a session only opened: no memory yet
        N.restore(self.file, self.root / "fresh", SomniMemory)

    def test_a_damaged_or_unsafe_snapshot_is_refused(self):
        N.snapshot(self.m, self.file)
        with tarfile.open(self.file) as tar:
            entries = [(m, tar.extractfile(m).read()) for m in tar.getmembers()]

        def rewrite(path, change):
            with tarfile.open(path, "w:gz") as tar:
                for member, data in entries:
                    member, data = change(member, data)
                    member.size = len(data)
                    tar.addfile(member, io.BytesIO(data))

        damaged = self.root / "damaged.tar.gz"
        rewrite(damaged, lambda m, d: (m, d.replace(b"release", b"hotfix")) if m.name.endswith("transcript.jsonl") else (m, d))
        with self.assertRaisesRegex(N.SnapshotError, "damaged"):
            N.restore(damaged, self.root / "c", SomniMemory)
        unsafe = self.root / "unsafe.tar.gz"

        def escape(member, data):
            if member.name.endswith("notes.jsonl"):
                member.name = "somni-v1/../../escaped"
            return member, data
        rewrite(unsafe, escape)
        with self.assertRaisesRegex(N.SnapshotError, "unsafe entry"):
            N.restore(unsafe, self.root / "c", SomniMemory)
        self.assertFalse((self.root / "escaped").exists())
        self.assertFalse((self.root / "c" / "somni-v1").exists())

    def test_a_store_copied_without_its_lock_file_says_how_to_move_it(self):
        shutil.copytree(self.root / "a", self.root / "copied", ignore=shutil.ignore_patterns("*.lock"))
        with self.assertRaisesRegex(SomniError, "somni snapshot"):
            SomniMemory(self.root / "copied", "pi").load()

    def test_the_cli_snapshots_and_restores_by_store_directory(self):
        out = subprocess.run([sys.executable, str(ROOT / "bin/somni"), "snapshot", "--store", str(self.root / "a"),
                              "--out", str(self.file), "--cwd", str(self.root)], capture_output=True, text=True,
                             env={"PATH": "/usr/bin:/bin", "SOMNI_CONFIG": "none", "HOME": str(self.root)})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout)["state"]["transcript_count"], 1)
        back = subprocess.run([sys.executable, str(ROOT / "bin/somni"), "restore", str(self.file), "--store",
                               str(self.root / "d"), "--cwd", str(self.root)], capture_output=True, text=True,
                              env={"PATH": "/usr/bin:/bin", "SOMNI_CONFIG": "none", "HOME": str(self.root)})
        self.assertEqual(back.returncode, 0, back.stderr)
        self.assertEqual(SomniMemory(self.root / "d", "pi").load().state["transcript_count"], 1)


if __name__ == "__main__":
    unittest.main()
