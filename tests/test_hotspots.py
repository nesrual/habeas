import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import hotspots  # noqa: E402
import state  # noqa: E402


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
                    *args], check=True, capture_output=True)


class TestHotspots(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        git(self.repo, "init", "-q", "-b", "main")
        self.write_commit({"app/models/hot.rb": "x = 1\n" * 90,
                           "app/services/cold.rb": "y = 1\n" * 10,
                           "vendor/gem.rb": "z = 1\n" * 500}, "base")
        # churn on app/models across 3 more commits
        for i in range(3):
            self.write_commit({"app/models/hot.rb": "x = 1\n" * (90 + i + 1)}, f"c{i}")

    def tearDown(self):
        self._tmp.cleanup()

    def write_commit(self, files, msg):
        for rel, content in files.items():
            p = self.repo / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", msg)

    def queue(self, **kw):
        args = {"days": 180, "depth": 2, "top": 10}
        args.update(kw)
        return hotspots.build_queue(str(self.repo), **args)

    def test_scores_and_ordering(self):
        q = self.queue()
        units = [e["unit"] for e in q]
        self.assertEqual(units[0], "app/models")
        hot = q[0]
        self.assertEqual(hot["commits"], 4)  # base + 3 churn commits
        self.assertEqual(hot["files"], 1)
        self.assertAlmostEqual(hot["score"], 4 * 93, delta=1)
        self.assertIn("app/services", units)
        self.assertNotIn("vendor", units)  # scope.exclude honored

    def test_scanned_unchanged_units_skipped(self):
        state.init_repo(str(self.repo))
        state.run_start(str(self.repo), "app/services")
        state.record_hunks(str(self.repo), ["app/services/cold.rb"])
        units = [e["unit"] for e in self.queue()]
        self.assertNotIn("app/services", units)
        self.assertIn("app/models", units)
        # a change to the scanned file brings the unit back
        self.write_commit({"app/services/cold.rb": "y = 2\n" * 10}, "touch")
        self.assertIn("app/services", [e["unit"] for e in self.queue()])

    def test_depth_and_top(self):
        q = self.queue(depth=1, top=1)
        self.assertEqual(len(q), 1)
        self.assertEqual(q[0]["unit"], "app")

    def test_cli(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "hotspots.py"), "--repo", str(self.repo),
             "--top", "3"], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        q = json.loads(proc.stdout)
        self.assertEqual(q[0]["unit"], "app/models")


if __name__ == "__main__":
    unittest.main()
