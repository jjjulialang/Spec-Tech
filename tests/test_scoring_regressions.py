"""Run scoring regressions against the official TypeScript grader implementation."""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class OfficialGraderRegressionTest(unittest.TestCase):
    def test_official_typescript_grader_regressions(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node 22 is required by the official sandbox contract")
        result = subprocess.run(
            [
                node,
                "--no-warnings",
                "--experimental-strip-types",
                "--test",
                str(ROOT / "tests" / "scoring_regressions.mjs"),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
