import json
import unittest
from pathlib import Path
from unittest.mock import patch

import agent


PRACTICE = Path(__file__).resolve().parents[1] / "examples" / "practice-dataset"


class AgentHelpersTest(unittest.TestCase):
    def test_parse_json_surrounded_by_model_text(self):
        parsed = agent.parse_json_object('result follows\n```json\n{"errors": []}\n```')
        self.assertEqual(parsed, {"errors": []})

    def test_clean_errors_enforces_contract(self):
        files = [PRACTICE / "schedule.pdf", PRACTICE / "spec.pdf"]
        data = {
            "errors": [
                {
                    "document": "folder/SCHEDULE.PDF",
                    "category": "unit-error",
                    "location": "PDF page 1, L-1",
                    "description": "L-1 is listed at 5.0 gpm instead of the required 0.5 gpm.",
                },
                {
                    "document": "invented.pdf",
                    "category": "unit-error",
                    "description": "This hallucinated document must be discarded.",
                },
                {
                    "document": "spec.pdf",
                    "category": "not-a-category",
                    "description": "This invalid category must be discarded.",
                },
            ]
        }
        self.assertEqual(
            agent.clean_errors(data, files),
            [
                {
                    "document": "schedule.pdf",
                    "category": "unit-error",
                    "location": "PDF page 1, L-1",
                    "description": "L-1 is listed at 5.0 gpm instead of the required 0.5 gpm.",
                }
            ],
        )


class FakeResponse:
    calls = []

    def __init__(self, request):
        body = json.loads(request.data)
        type(self).calls.append(body)
        if len(type(self).calls) == 1:
            result = {
                "errors": [
                    {
                        "document": "schedule.pdf",
                        "category": "unit-error",
                        "location": "PDF page 1, L-1",
                        "description": "L-1 says 5.0 gpm while section 22 40 00 requires 0.5 gpm.",
                    }
                ]
            }
        else:
            result = {
                "errors": [
                    {
                        "document": "schedule.pdf",
                        "category": "unit-error",
                        "location": "PDF page 1, L-1",
                        "description": "L-1 says 5.0 gpm while section 22 40 00 requires 0.5 gpm.",
                    },
                    {
                        "document": "schedule.pdf",
                        "category": "cross-document-conflict",
                        "location": "PDF page 1, D-202",
                        "description": "D-202 says 45 min while section 08 11 00 requires 90 minutes.",
                    },
                ]
            }
        self.payload = json.dumps(
            {"choices": [{"message": {"content": json.dumps(result)}}]}
        ).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload


class AgentIntegrationTest(unittest.TestCase):
    def test_two_pass_native_pdf_audit(self):
        FakeResponse.calls = []
        original_key = agent.API_KEY
        try:
            agent.API_KEY = "test-key"
            files = [PRACTICE / "schedule.pdf", PRACTICE / "spec.pdf"]
            with patch("agent.urllib.request.urlopen", side_effect=lambda request, **_: FakeResponse(request)):
                errors = agent.audit(files)
        finally:
            agent.API_KEY = original_key

        self.assertEqual(len(FakeResponse.calls), 2)
        first_content = FakeResponse.calls[0]["messages"][0]["content"]
        self.assertEqual([part["type"] for part in first_content], ["text", "file", "file"])
        self.assertEqual(len(errors), 2)
        self.assertEqual({error["category"] for error in errors}, {"unit-error", "cross-document-conflict"})


if __name__ == "__main__":
    unittest.main()
