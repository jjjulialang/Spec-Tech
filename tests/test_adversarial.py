"""Adversarial tests for failure modes likely to occur in the grader sandbox."""

import io
import json
import os
import subprocess
import tempfile
import types
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

import agent


def finding(**overrides):
    value = {
        "document": "schedule.pdf",
        "category": "unit-error",
        "location": "PDF page 1, fixture L-1",
        "description": "Fixture L-1 says 5.0 gpm but section 22 40 00 requires 0.5 gpm.",
    }
    value.update(overrides)
    return value


class JsonAndResponseAdversarialTest(unittest.TestCase):
    def test_malformed_json_returns_empty_object(self):
        for malformed in ("", "not json", '{"errors":[}', "[1, 2, 3]", "null"):
            with self.subTest(malformed=malformed):
                self.assertEqual(agent.parse_json_object(malformed), {})

    def test_parser_finds_first_valid_object_after_broken_brace(self):
        text = 'analysis {broken first\nanswer: {"errors": []} trailing text'
        self.assertEqual(agent.parse_json_object(text), {"errors": []})

    def test_response_text_joins_text_parts_and_ignores_non_text_parts(self):
        response = {
            "choices": [
                {
                    "message": {
                        "content": [
                            {"type": "text", "text": '{"errors":'},
                            {"type": "image", "image_url": "ignored"},
                            None,
                            {"type": "text", "text": "[]}"},
                        ]
                    }
                }
            ]
        }
        self.assertEqual(agent.response_text(response), '{"errors":[]}')

    def test_response_text_rejects_missing_or_unsupported_content(self):
        bad_responses = [
            {},
            {"choices": []},
            {"choices": [{"message": {"content": 123}}]},
        ]
        for response in bad_responses:
            with self.subTest(response=response):
                with self.assertRaises(RuntimeError):
                    agent.response_text(response)
        # Missing content is treated as an empty generation, which downstream
        # parsing safely converts into zero findings.
        self.assertEqual(agent.response_text({"choices": [{}]}), "")


class FindingSanitizationAdversarialTest(unittest.TestCase):
    def setUp(self):
        self.files = [Path("/dataset/schedule.pdf"), Path("/dataset/spec.v2.PDF")]

    def test_weird_path_and_case_are_canonicalized_to_exact_filename(self):
        raw = finding(document=r"C:\uploaded\SCHEDULE.PDF")
        cleaned = agent.clean_errors({"errors": [raw]}, self.files)
        self.assertEqual(cleaned[0]["document"], "schedule.pdf")

    def test_missing_extension_is_rejected_for_multi_dot_filename(self):
        raw = finding(document="nested/spec.v2")
        cleaned = agent.clean_errors({"errors": [raw]}, self.files)
        self.assertEqual(cleaned, [])

    def test_ambiguous_case_insensitive_filename_is_rejected(self):
        files = [Path("/dataset/A.pdf"), Path("/dataset/a.PDF")]
        self.assertIsNone(agent.canonical_document("a.pdf", files))

    def test_unknown_categories_and_non_list_errors_are_discarded(self):
        unknown = finding(category=" prompt-injection ")
        self.assertEqual(agent.clean_errors({"errors": [unknown]}, self.files), [])
        for raw_errors in (None, {}, "errors", 7):
            with self.subTest(raw_errors=raw_errors):
                self.assertEqual(agent.clean_errors({"errors": raw_errors}, self.files), [])

    def test_exact_duplicates_are_removed(self):
        item = finding()
        cleaned = agent.clean_errors({"errors": [item, dict(item)]}, self.files)
        self.assertEqual(cleaned, [item])

    def test_near_duplicates_with_formatting_differences_are_removed(self):
        first = finding()
        second = finding(
            location="page 1 - L-1",
            description="L-1 is 5.0 GPM; Section 22 40 00 says it must be 0.5 GPM.",
        )
        cleaned = agent.clean_errors({"errors": [first, second]}, self.files)
        self.assertEqual(len(cleaned), 1)

    def test_at_most_one_hundred_model_findings_are_considered(self):
        errors = [
            finding(
                location=f"PDF page 1, fixture L-{index}",
                description=f"Fixture L-{index} says 5.0 gpm but the requirement is 0.5 gpm.",
            )
            for index in range(120)
        ]
        self.assertEqual(len(agent.clean_errors({"errors": errors}, self.files)), 100)

    def test_non_string_and_oversized_fields_are_rejected_or_capped(self):
        invalid = finding(description={"nested": "not allowed"})
        long = finding(location="L" * 2_000, description="D" * 5_000)
        cleaned = agent.clean_errors({"errors": [invalid, long]}, self.files)
        self.assertEqual(len(cleaned), 1)
        self.assertEqual(len(cleaned[0]["location"]), agent.MAX_LOCATION_CHARS)
        self.assertEqual(len(cleaned[0]["description"]), agent.MAX_DESCRIPTION_CHARS)


class FilesAndExtractionAdversarialTest(unittest.TestCase):
    def test_no_pdf_files_is_a_clear_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "manifest.json").write_text("{}", encoding="utf-8")
            with patch.object(agent, "DATASET_DIR", Path(directory)):
                with self.assertRaisesRegex(RuntimeError, "No PDF files"):
                    agent.pdf_files()

    def test_pdf_enumeration_is_case_insensitive_and_deterministic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("z.PdF", "A.pdf", "not-pdf.txt"):
                (root / name).write_bytes(b"x")
            with patch.object(agent, "DATASET_DIR", root):
                self.assertEqual([p.name for p in agent.pdf_files()], ["A.pdf", "z.PdF"])

    def test_corrupt_pdf_does_not_crash_text_extraction(self):
        with tempfile.TemporaryDirectory() as directory:
            corrupt = Path(directory, "corrupt.pdf")
            corrupt.write_bytes(b"this is not a PDF")
            self.assertEqual(agent.extracted_text([corrupt]), "")

    def test_extracted_text_honors_global_character_cap(self):
        class FakePage:
            def __init__(self, text):
                self._text = text

            def extract_text(self):
                return self._text

        fake_reader = Mock(return_value=Mock(pages=[FakePage("x" * 100), FakePage("y" * 100)]))
        fake_pypdf = types.ModuleType("pypdf")
        fake_pypdf.PdfReader = fake_reader
        with patch.object(agent, "MAX_TEXT_CHARS", 80), patch.dict(
            "sys.modules", {"pypdf": fake_pypdf}
        ):
            text = agent.extracted_text([Path("huge.pdf")])
        self.assertEqual(len(text), 80)
        self.assertIn("huge.pdf | PDF page 1", text)
        self.assertIn("PDF page 2", text)

    def test_extracted_text_reserves_space_for_every_file(self):
        class FakePage:
            def extract_text(self):
                return "x" * 1_000

        fake_reader = Mock(return_value=Mock(pages=[FakePage(), FakePage()]))
        fake_pypdf = types.ModuleType("pypdf")
        fake_pypdf.PdfReader = fake_reader
        with patch.object(agent, "MAX_TEXT_CHARS", 240), patch.dict(
            "sys.modules", {"pypdf": fake_pypdf}
        ):
            text = agent.extracted_text([Path("first.pdf"), Path("second.pdf")])
        self.assertLessEqual(len(text), 240)
        self.assertIn("first.pdf | PDF page 1", text)
        self.assertIn("second.pdf | PDF page 1", text)


class NetworkAndFallbackAdversarialTest(unittest.TestCase):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size=-1):
            return b'{"choices":[{"message":{"content":"{\\"errors\\":[]}"}}]}'

    def test_transient_network_failure_retries_without_changing_payload(self):
        calls = []

        def urlopen(request, **_kwargs):
            calls.append(request.data)
            if len(calls) == 1:
                raise urllib.error.URLError("temporary DNS failure")
            return self.Response()

        with (
            patch.object(agent, "API_KEY", "test-key"),
            patch.object(agent, "DEADLINE", 10_000),
            patch("agent.time.monotonic", return_value=0),
            patch("agent.time.sleep") as sleep,
            patch("agent.urllib.request.urlopen", side_effect=urlopen),
        ):
            result = agent.call_openrouter(
                model="test/model", content=[{"type": "text", "text": "hi"}], plugins=None
            )
        self.assertEqual(result, '{"errors":[]}')
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])
        sleep.assert_called_once_with(2)

    def test_deadline_prevents_network_call(self):
        with (
            patch.object(agent, "API_KEY", "test-key"),
            patch.object(agent, "DEADLINE", 100),
            patch("agent.time.monotonic", return_value=90),
            patch("agent.urllib.request.urlopen") as urlopen,
        ):
            with self.assertRaisesRegex(TimeoutError, "deadline"):
                agent.call_openrouter(
                    model="test/model",
                    content=[{"type": "text", "text": "hi"}],
                    plugins=None,
                )
        urlopen.assert_not_called()

    def test_rate_limit_retries_then_reports_provider_error(self):
        def rate_limited(*_args, **_kwargs):
            # HTTPError owns a consumable response stream, so each retry must
            # receive a fresh exception just as a real request would.
            raise urllib.error.HTTPError(
                "https://openrouter.ai", 429, "rate limited", {}, io.BytesIO(b"slow down")
            )
        with (
            patch.object(agent, "API_KEY", "test-key"),
            patch.object(agent, "DEADLINE", 10_000),
            patch("agent.time.monotonic", return_value=0),
            patch("agent.time.sleep") as sleep,
            patch("agent.urllib.request.urlopen", side_effect=rate_limited) as urlopen,
        ):
            with self.assertRaisesRegex(RuntimeError, "HTTP 429: slow down"):
                agent.call_openrouter(
                    model="test/model",
                    content=[{"type": "text", "text": "hi"}],
                    plugins=None,
                )
        self.assertEqual(urlopen.call_count, 2)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2])

    def test_model_fallback_is_used_after_primary_failure(self):
        models = []

        def fake_call(**kwargs):
            models.append(kwargs["model"])
            if len(models) == 1:
                raise RuntimeError("primary unavailable")
            return '{"errors":[]}'

        with patch.object(agent, "FALLBACK_MODEL", "fallback/model"), patch(
            "agent.call_openrouter", side_effect=fake_call
        ):
            result = agent.call_model(model="primary/model", content=[], plugins=None)
        self.assertEqual(result, '{"errors":[]}')
        self.assertEqual(models, ["primary/model", "fallback/model"])

    def test_oversized_provider_response_is_rejected(self):
        class OversizedResponse(self.Response):
            def read(self, _size=-1):
                return b"x" * 33

        with (
            patch.object(agent, "API_KEY", "test-key"),
            patch.object(agent, "MAX_RESPONSE_BYTES", 32),
            patch("agent.urllib.request.urlopen", return_value=OversizedResponse()),
        ):
            with self.assertRaisesRegex(RuntimeError, "safety limit"):
                agent.call_openrouter(
                    model="test/model", content=[{"type": "text", "text": "hi"}], plugins=None
                )

    def test_submission_endpoint_cannot_be_overridden_by_environment(self):
        self.assertEqual(agent.API_URL, "https://openrouter.ai/api/v1/chat/completions")

    def test_local_provider_call_cap_stays_below_sandbox_limit(self):
        with (
            patch.object(agent, "API_KEY", "test-key"),
            patch.object(agent, "PROVIDER_CALLS", agent.MAX_PROVIDER_CALLS),
            patch("agent.urllib.request.urlopen") as urlopen,
        ):
            with self.assertRaisesRegex(RuntimeError, "call safety limit"):
                agent.call_openrouter(
                    model="test/model", content=[{"type": "text", "text": "hi"}], plugins=None
                )
        urlopen.assert_not_called()
        self.assertLess(agent.MAX_PROVIDER_CALLS, 300)


class LargeSetAndEntrypointAdversarialTest(unittest.TestCase):
    def test_single_oversized_pdf_uses_text_without_native_encoding(self):
        """A single huge file must not bypass the native-request byte ceiling."""
        with tempfile.TemporaryDirectory() as directory:
            oversized = Path(directory) / "oversized.pdf"
            oversized.write_bytes(b"larger than the mocked cap")
            responses = [json.dumps({"errors": []}), json.dumps({"errors": []})]
            with (
                patch.object(agent, "MAX_NATIVE_PDF_BYTES", 1),
                patch("agent.pdf_content") as native_content,
                patch("agent.extracted_text", return_value="===== oversized.pdf | PDF page 1 =====\ntext"),
                patch("agent.call_model", side_effect=responses) as model,
            ):
                self.assertEqual(agent.audit([oversized]), [])

        native_content.assert_not_called()
        self.assertEqual(model.call_count, 2)
        self.assertTrue(
            all(call.kwargs["content"][0]["type"] == "text" for call in model.call_args_list)
        )

    def test_multiple_oversized_pdfs_never_bypass_native_cap(self):
        with tempfile.TemporaryDirectory() as directory:
            files = []
            for name in ("one.pdf", "two.pdf"):
                path = Path(directory, name)
                path.write_bytes(b"larger than cap")
                files.append(path)
            responses = [json.dumps({"errors": []})] * 3
            with (
                patch.object(agent, "MAX_NATIVE_PDF_BYTES", 1),
                patch("agent.pdf_content") as native_content,
                patch("agent.extracted_text", return_value="bounded text"),
                patch("agent.call_model", side_effect=responses) as model,
            ):
                self.assertEqual(agent.audit(files), [])

        native_content.assert_not_called()
        self.assertEqual(model.call_count, 3)

    def test_large_set_uses_native_batches_then_global_text_verification(self):
        files = [
            Path(__file__).resolve().parents[1] / "examples" / "practice-dataset" / "schedule.pdf",
            Path(__file__).resolve().parents[1] / "examples" / "practice-dataset" / "spec.pdf",
        ]
        unit = finding()
        conflict = finding(
            category="cross-document-conflict",
            location="PDF page 1, D-202",
            description="D-202 says 45 min while section 08 11 00 requires 90 minutes.",
        )
        responses = [
            json.dumps({"errors": [unit]}),
            json.dumps({"errors": []}),
            json.dumps({"errors": [unit, conflict]}),
        ]
        calls = []

        def fake_model(**kwargs):
            calls.append(kwargs)
            return responses[len(calls) - 1]

        with patch.object(agent, "MAX_NATIVE_PDF_BYTES", 1_800), patch(
            "agent.call_model", side_effect=fake_model
        ):
            errors = agent.audit(files)

        self.assertEqual(len(calls), 3)
        self.assertTrue(any(part.get("type") == "file" for part in calls[0]["content"]))
        self.assertTrue(any(part.get("type") == "file" for part in calls[1]["content"]))
        self.assertEqual(calls[2]["content"][0]["type"], "text")
        self.assertEqual(len(errors), 2)

    def test_run_sh_works_outside_repository_cwd(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output.json"
            env = os.environ.copy()
            env.update(
                {
                    "DATASET_DIR": str(root / "examples" / "practice-dataset"),
                    "OUTPUT_PATH": str(output),
                    "OPENROUTER_API_KEY": "",
                }
            )
            result = subprocess.run(
                ["bash", str(root / "run.sh")],
                cwd=directory,
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(output.read_text()), {"errors": []})


class OutputAtomicityAdversarialTest(unittest.TestCase):
    def test_successful_write_replaces_old_output_and_removes_temporary(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory, "nested", "output.json")
            with patch.object(agent, "OUTPUT_PATH", output):
                agent.write_output([finding()])
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["errors"], [finding()])
            self.assertFalse(output.with_name("output.json.tmp").exists())

    def test_failed_atomic_replace_preserves_previous_output(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory, "output.json")
            output.write_text('{"errors":["old"]}\n', encoding="utf-8")
            with patch.object(agent, "OUTPUT_PATH", output), patch.object(
                Path, "replace", side_effect=OSError("disk failure")
            ):
                with self.assertRaisesRegex(OSError, "disk failure"):
                    agent.write_output([finding()])
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), {"errors": ["old"]})


if __name__ == "__main__":
    unittest.main()
