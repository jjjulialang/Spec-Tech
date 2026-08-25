"""Build tiny, deterministic text PDFs for hidden-set-style agent tests."""

from __future__ import annotations

import json
from pathlib import Path


CASE_FILE = Path(__file__).with_name("cases.json")


def load_cases() -> dict:
    return json.loads(CASE_FILE.read_text(encoding="utf-8"))


def _pdf_literal(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def write_text_pdf(path: Path, lines: list[str]) -> None:
    """Write a valid one-page PDF without adding a test-only dependency."""
    commands = ["BT", "/F1 9 Tf", "36 756 Td", "11 TL"]
    for line in lines:
        # Fixture text is deliberately ASCII so a basic Type1 font is sufficient.
        commands.extend((f"({_pdf_literal(line)}) Tj", "T*"))
    commands.append("ET")
    stream = "\n".join(commands).encode("ascii")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [4 0 R] /Count 1 >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 3 0 R >> >> /Contents 5 0 R >>",
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n"
        + stream
        + b"\nendstream",
    ]

    result = bytearray(b"%PDF-1.4\n% synthetic AEC fixture\n")
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(result))
        result.extend(f"{number} 0 obj\n".encode("ascii"))
        result.extend(obj)
        result.extend(b"\nendobj\n")
    xref = len(result)
    result.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    result.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        result.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    result.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n".encode("ascii")
    )
    path.write_bytes(result)


def build_dataset(destination: Path) -> tuple[list[Path], dict]:
    cases = load_cases()
    destination.mkdir(parents=True, exist_ok=True)
    for filename, lines in cases["documents"].items():
        write_text_pdf(destination / filename, lines)
    pdf_names = sorted(cases["documents"], key=str.casefold)
    (destination / "files.json").write_text(
        json.dumps(pdf_names, indent=2), encoding="utf-8"
    )
    # These are traps: production discovery must enumerate only PDFs.
    manifest = json.loads(json.dumps(cases["manifest"]))
    expected = cases["expected_output"]["errors"]
    for answer, report in zip(manifest["errors"], expected, strict=True):
        answer["description"] = report["description"]
        answer["page"] = 1
    (destination / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    (destination / "README.txt").write_text(
        "This non-PDF is not an input document.\n", encoding="utf-8"
    )
    return sorted(destination.glob("*.pdf"), key=lambda p: p.name.casefold()), cases
