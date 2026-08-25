"""Offline tests for a layout-heavy, precision-trap hidden-style dataset."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pypdf import PdfReader

import agent

sys.path.insert(0, str(Path(__file__).resolve().parent))
from extreme_dataset.build_fixture import FILENAMES, build_dataset


class ExtremeHiddenDatasetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.dataset = Path(cls.temporary.name)
        cls.files, cls.cases = build_dataset(cls.dataset)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_multiformat_fixture_and_pdf_only_discovery(self):
        with patch.object(agent, "DATASET_DIR", self.dataset):
            discovered = agent.pdf_files()
        self.assertEqual([path.name for path in discovered], sorted(FILENAMES, key=str.casefold))
        self.assertEqual([len(PdfReader(path).pages) for path in self.files], [3, 2, 3, 2])
        self.assertEqual(
            {path.name for path in self.dataset.iterdir() if path.is_file()} - set(FILENAMES),
            {"pages.csv", "detections.csv", "page_entities.json", "manifest.json"},
        )

    def test_fixture_generation_is_byte_deterministic(self):
        with tempfile.TemporaryDirectory() as directory:
            rebuilt, _ = build_dataset(Path(directory))
            self.assertEqual(
                [path.read_bytes() for path in rebuilt],
                [path.read_bytes() for path in self.files],
            )

    def test_landscape_and_image_only_scan_are_genuine_layout_complications(self):
        life_safety = PdfReader(self.dataset / "A-900_life-safety.pdf")
        self.assertGreater(float(life_safety.pages[1].mediabox.width), float(life_safety.pages[1].mediabox.height))
        plumbing = PdfReader(self.dataset / "P-900_plumbing.pdf")
        self.assertIn("L-17", plumbing.pages[0].extract_text())
        self.assertNotIn("CHW-17", plumbing.pages[1].extract_text() or "")
        page_stream = plumbing.pages[1].get_contents().get_data()
        self.assertIn(b"BI", page_stream)  # PDF inline-image operator

    def test_text_fallback_is_bounded_but_native_payload_keeps_scan(self):
        text = agent.extracted_text(self.files)
        self.assertIn("D-307", text)
        self.assertIn("AHU-12", text)
        self.assertNotIn("CHW-17  BRANCH DIAMETER = 6 mm", text)
        content, plugins = agent.pdf_content(self.files, "audit")
        self.assertEqual([part["type"] for part in content], ["text", "file", "file", "file", "file"])
        self.assertEqual(plugins[0]["pdf"]["engine"], "native")

    def test_expected_output_contains_all_errors_and_no_precision_traps(self):
        expected = self.cases["expected_output"]["errors"]
        self.assertEqual(len(expected), 7)
        serialized = json.dumps(expected)
        for trap in self.cases["precision_traps"]:
            self.assertNotIn(trap, serialized)
        self.assertEqual(
            {item["category"] for item in expected},
            {"cross-document-conflict", "code-violation", "unit-error", "missing-item"},
        )

    def test_cleanup_must_preserve_distinct_same_numbers_at_different_locations(self):
        expected = self.cases["expected_output"]["errors"]
        cleaned = agent.clean_errors({"errors": expected}, self.files)
        self.assertEqual(cleaned, expected)
        access = [item for item in cleaned if item["category"] == "code-violation"]
        self.assertEqual({item["location"].split(", ", 1)[1] for item in access}, {
            "North Alcove accessible route", "South Vestibule accessible route"
        })

    def test_mocked_two_pass_audit_filters_revision_and_tag_substring_traps(self):
        expected = self.cases["expected_output"]["errors"]
        traps = [
            {
                "document": "00_general-specifications.pdf",
                "category": "cross-document-conflict",
                "location": "PDF page 1, REV 1 SUPERSEDED",
                "description": "Superseded D-307 value says 30 min and should not be treated as current.",
            },
            {
                "document": "M-900_equipment.pdf",
                "category": "cross-document-conflict",
                "location": "PDF page 2, AHU-12A",
                "description": "AHU-12A says 208 V but is a different valid equipment tag.",
            },
        ]
        responses = [
            json.dumps({"errors": expected[:4] + traps}),
            json.dumps({"errors": expected}),
        ]
        with patch("agent.call_model", side_effect=responses) as model:
            result = agent.audit(self.files)
        self.assertEqual(model.call_count, 2)
        self.assertEqual(result, expected)
        self.assertFalse(any("SUPERSEDED" in item["location"] or "AHU-12A" in item["location"] for item in result))


if __name__ == "__main__":
    unittest.main()
