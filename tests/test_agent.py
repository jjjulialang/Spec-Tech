import json
import tempfile
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

    def test_pdf_discovery_ignores_manifest_and_other_files(self):
        original_dataset = agent.DATASET_DIR
        try:
            agent.DATASET_DIR = PRACTICE
            self.assertEqual([path.name for path in agent.pdf_files()], ["schedule.pdf", "spec.pdf"])
        finally:
            agent.DATASET_DIR = original_dataset

    def test_text_fallback_preserves_file_and_page_labels(self):
        text = agent.extracted_text([PRACTICE / "schedule.pdf", PRACTICE / "spec.pdf"])
        self.assertIn("===== schedule.pdf | PDF page 1 =====", text)
        self.assertIn("D-202", text)
        self.assertIn("===== spec.pdf | PDF page 1 =====", text)
        self.assertIn("Section 08 11 00", text)

    def test_main_writes_valid_empty_output_after_input_failure(self):
        original_dataset, original_output = agent.DATASET_DIR, agent.OUTPUT_PATH
        with tempfile.TemporaryDirectory() as directory:
            try:
                agent.DATASET_DIR = Path(directory)
                agent.OUTPUT_PATH = Path(directory) / "result.json"
                self.assertEqual(agent.main(), 0)
                self.assertEqual(json.loads(agent.OUTPUT_PATH.read_text()), {"errors": []})
            finally:
                agent.DATASET_DIR, agent.OUTPUT_PATH = original_dataset, original_output


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
                "accepted_ids": [0],
                "rejected_ids": [],
                "corrected_errors": [],
                "added_errors": [
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

    def read(self, _size=-1):
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
        self.assertEqual(FakeResponse.calls[0]["messages"][0]["role"], "system")
        first_content = FakeResponse.calls[0]["messages"][1]["content"]
        self.assertEqual([part["type"] for part in first_content], ["text", "file", "file"])
        self.assertIn("UNTRUSTED EVIDENCE", FakeResponse.calls[0]["messages"][0]["content"])
        response_format = FakeResponse.calls[0]["response_format"]
        self.assertEqual(response_format, {"type": "json_object"})
        self.assertEqual(FakeResponse.calls[1]["response_format"], {"type": "json_object"})
        self.assertEqual(
            FakeResponse.calls[1]["plugins"][0]["pdf"]["engine"], "mistral-ocr"
        )
        self.assertEqual(len(errors), 2)
        self.assertEqual({error["category"] for error in errors}, {"unit-error", "cross-document-conflict"})

    def test_native_rejection_falls_back_to_document_ocr(self):
        files = [PRACTICE / "schedule.pdf", PRACTICE / "spec.pdf"]
        response = json.dumps(
            {
                "errors": [
                    {
                        "document": "schedule.pdf",
                        "category": "unit-error",
                        "location": "PDF page 1, L-1",
                        "description": "L-1 says 5.0 gpm while section 22 40 00 requires 0.5 gpm.",
                    }
                ]
            }
        )
        calls = []

        def fake_call_model(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise RuntimeError("provider rejected native PDF input")
            return response

        with patch("agent.call_model", side_effect=fake_call_model):
            errors = agent.audit(files)

        self.assertEqual(len(calls), 3)  # rejected native discovery, OCR discovery, OCR verification
        self.assertEqual(calls[0]["content"][1]["type"], "file")
        self.assertEqual(calls[1]["content"][1]["type"], "file")
        self.assertEqual(calls[1]["plugins"][0]["pdf"]["engine"], "mistral-ocr")
        self.assertEqual(errors[0]["document"], "schedule.pdf")

    def test_invalid_or_empty_verifier_cannot_erase_discovery(self):
        files = [PRACTICE / "schedule.pdf", PRACTICE / "spec.pdf"]
        discovery = json.dumps(
            {
                "errors": [
                    {
                        "document": "schedule.pdf",
                        "category": "unit-error",
                        "location": "PDF page 1, L-1",
                        "description": "L-1 says 5.0 gpm while the requirement is 0.5 gpm.",
                    }
                ]
            }
        )
        for verifier in ("not json", "{}", '{"errors":[]}'):
            with self.subTest(verifier=verifier), patch(
                "agent.call_model", side_effect=[discovery, verifier]
            ):
                errors = agent.audit(files)
            self.assertEqual(len(errors), 1)
            self.assertIn("L-1", errors[0]["description"])

    def test_partial_verifier_cannot_erase_most_discovery_results(self):
        files = [PRACTICE / "schedule.pdf", PRACTICE / "spec.pdf"]
        candidates = [
            {
                "document": "schedule.pdf",
                "category": "cross-document-conflict",
                "location": f"PDF page 1, item X-{index}",
                "description": f"Item X-{index} says {index} while the specification requires {index + 10}.",
            }
            for index in range(1, 5)
        ]
        discovery = json.dumps({"errors": candidates})
        partial = json.dumps({"errors": [candidates[0]]})
        with patch("agent.call_model", side_effect=[discovery, partial]):
            errors = agent.audit(files)
        self.assertEqual(errors, candidates)

    def test_explicit_verdicts_allow_safe_aggressive_pruning(self):
        files = [PRACTICE / "schedule.pdf", PRACTICE / "spec.pdf"]
        candidates = [
            {
                "document": "schedule.pdf",
                "category": "cross-document-conflict",
                "location": f"PDF page 1, item X-{index}",
                "description": f"Item X-{index} says {index} while the specification requires {index + 10}.",
            }
            for index in range(4)
        ]
        verdicts = {
            "accepted_ids": [0],
            "rejected_ids": [1, 2, 3],
            "corrected_errors": [],
            "added_errors": [],
        }
        with patch(
            "agent.call_model",
            side_effect=[json.dumps({"errors": candidates}), json.dumps(verdicts)],
        ):
            errors = agent.audit(files)
        self.assertEqual(errors, [candidates[0]])

    def test_unreviewed_candidate_survives_truncated_verdicts(self):
        files = [PRACTICE / "schedule.pdf", PRACTICE / "spec.pdf"]
        candidates = [
            {
                "document": "schedule.pdf",
                "category": "unit-error",
                "location": f"PDF page 1, fixture L-{index}",
                "description": f"Fixture L-{index} says {index}.0 gpm instead of 0.{index} gpm.",
            }
            for index in range(1, 3)
        ]
        partial_verdicts = {
            "accepted_ids": [0],
            "rejected_ids": [],
            "corrected_errors": [],
            "added_errors": [],
        }
        with patch(
            "agent.call_model",
            side_effect=[json.dumps({"errors": candidates}), json.dumps(partial_verdicts)],
        ):
            errors = agent.audit(files)
        self.assertEqual(errors, candidates)


if __name__ == "__main__":
    unittest.main()
