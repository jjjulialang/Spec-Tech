"""Opt-in real-provider smoke test; skipped during ordinary offline test runs."""

import os
import time
import unittest
from pathlib import Path

import agent


ROOT = Path(__file__).resolve().parents[1]
PRACTICE = ROOT / "examples" / "practice-dataset"


@unittest.skipUnless(
    os.environ.get("RUN_LIVE_OPENROUTER_TEST") == "1"
    and bool(os.environ.get("OPENROUTER_API_KEY")),
    "set RUN_LIVE_OPENROUTER_TEST=1 and OPENROUTER_API_KEY to run",
)
class LiveOpenRouterPracticeTest(unittest.TestCase):
    def test_gemini_finds_exactly_the_two_practice_errors(self):
        original_key, original_deadline = agent.API_KEY, agent.DEADLINE
        try:
            agent.API_KEY = os.environ["OPENROUTER_API_KEY"]
            agent.DEADLINE = time.monotonic() + agent.RUN_BUDGET_SECONDS
            files = [PRACTICE / "schedule.pdf", PRACTICE / "spec.pdf"]
            errors = agent.audit(files)
        finally:
            agent.API_KEY, agent.DEADLINE = original_key, original_deadline

        self.assertEqual(len(errors), 2, errors)
        by_category = {error["category"]: error for error in errors}
        self.assertIn("D-202", by_category["cross-document-conflict"]["description"])
        self.assertIn("L-1", by_category["unit-error"]["description"])
        self.assertEqual({error["document"] for error in errors}, {"schedule.pdf"})


if __name__ == "__main__":
    unittest.main()
