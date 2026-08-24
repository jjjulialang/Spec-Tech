#!/usr/bin/env python3
"""Audit a construction document set for deliberately injected errors.

The grader provides PDF files in DATASET_DIR and an OpenRouter credential.
This agent gives a vision-capable model the original PDFs (not just flattened
text), then asks for a second, conservative verification pass.  It always
writes a schema-compatible result to OUTPUT_PATH.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


DATASET_DIR = Path(os.environ.get("DATASET_DIR", "./dataset"))
OUTPUT_PATH = Path(os.environ.get("OUTPUT_PATH", "./output.json"))
API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
API_URL = os.environ.get(
    "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1/chat/completions"
)
MODEL = os.environ.get("AEC_MODEL", "google/gemini-3.1-pro-preview")
VERIFY_MODEL = os.environ.get("AEC_VERIFY_MODEL", MODEL)
FALLBACK_MODEL = os.environ.get("AEC_FALLBACK_MODEL", "google/gemini-2.5-pro")

ALLOWED_CATEGORIES = {
    "cross-document-conflict",
    "code-violation",
    "unit-error",
    "missing-item",
}
MAX_NATIVE_PDF_BYTES = 18 * 1024 * 1024
MAX_TEXT_CHARS = 500_000
DEADLINE = time.monotonic() + 540  # leave time to serialize before the 10-minute limit


DISCOVERY_PROMPT = r"""
You are the lead QA/QC reviewer for an AEC construction document set. The set
contains a small number of DELIBERATELY INJECTED errors. Inspect every page,
drawing, keynote, specification, product schedule, equipment schedule, and
table, including visually positioned text.

Find only errors in these four categories:
1. cross-document-conflict: the same mark, room, product, material, fixture,
   circuit, dimension, rating, or requirement disagrees between documents.
2. code-violation: a stated design value clearly violates a well-established
   building, accessibility, fire, energy, electrical, or plumbing requirement.
3. unit-error: a value has an implausible unit, decimal shift, scale, or unit
   conversion (for example 5.0 gpm where 0.5 gpm is required).
4. missing-item: an item explicitly required or referenced in one place is
   absent from the schedule/drawing where it must appear.

Work carefully before answering:
- Join records by exact identifiers such as room numbers, door/finish/fixture/
  equipment tags, keynote numbers, and specification sections.
- Treat project drawings/specifications as the evidence. Do not flag ordinary
  design choices, incomplete-looking excerpts, cosmetic inconsistencies, or
  uncertain code trivia.
- Determine which document contains the INCORRECT information. A controlling
  requirement or repeated consensus is normally correct; a lone conflicting
  schedule/drawing entry is normally wrong.
- Precision matters. Include a finding only when you can name the wrong fact
  and the correct fact (or the specifically required missing item).
- Use the exact supplied PDF filename and 1-based PDF page number. Include the
  distinctive mark, room, section, and numeric values in location/description.
- Do not repeat the same underlying problem.

Return ONLY valid JSON, with no markdown or commentary:
{"errors":[{"document":"exact.pdf","category":"cross-document-conflict|code-violation|unit-error|missing-item","location":"PDF page 1, section/table/mark","description":"One sentence stating the wrong value, correct value, and evidence."}]}
""".strip()


VERIFY_PROMPT = r"""
Act as the final, skeptical QA/QC verifier. Re-read the attached construction
PDFs and check every candidate below against primary evidence. Return only
findings that are genuinely supported and belong to one of the four permitted
categories. Remove speculation and duplicates. Correct the filename, category,
page, mark, and values when needed. The `document` must be the exact filename
containing the incorrect information, not merely the reference document.

Because this is an injected-error benchmark, also add an error missed by the
candidate list only if it is unmistakable after comparing the documents.
Preserve distinctive tags and both wrong/correct values so a reviewer can find
the evidence quickly. Use 1-based PDF page numbers.

Return ONLY this JSON shape, with no markdown:
{"errors":[{"document":"exact.pdf","category":"cross-document-conflict|code-violation|unit-error|missing-item","location":"PDF page N, section/table/mark","description":"One evidence-based sentence."}]}

CANDIDATES:
""".strip()


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def pdf_files() -> list[Path]:
    if not DATASET_DIR.is_dir():
        raise RuntimeError(f"DATASET_DIR is not a directory: {DATASET_DIR}")
    files = sorted(
        (p for p in DATASET_DIR.iterdir() if p.is_file() and p.suffix.lower() == ".pdf"),
        key=lambda p: p.name.casefold(),
    )
    if not files:
        raise RuntimeError(f"No PDF files found in {DATASET_DIR}")
    return files


def pdf_content(files: list[Path], prompt: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build native-PDF message content plus the OpenRouter parser declaration."""
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for path in files:
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        content.append(
            {
                "type": "file",
                "file": {
                    "filename": path.name,
                    "file_data": f"data:application/pdf;base64,{encoded}",
                },
            }
        )
    plugins = [{"id": "file-parser", "pdf": {"engine": "native"}}]
    return content, plugins


def extracted_text(files: list[Path]) -> str:
    """Fallback for a provider that rejects native PDF input."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""

    parts: list[str] = []
    used = 0
    for path in files:
        try:
            reader = PdfReader(str(path))
            for page_number, page in enumerate(reader.pages, 1):
                text = page.extract_text() or ""
                block = f"\n===== {path.name} | PDF page {page_number} =====\n{text}\n"
                remaining = MAX_TEXT_CHARS - used
                if remaining <= 0:
                    return "".join(parts)
                parts.append(block[:remaining])
                used += min(len(block), remaining)
        except Exception as exc:  # one damaged file should not suppress all output
            log(f"Text extraction failed for {path.name}: {exc}")
    return "".join(parts)


def response_text(data: dict[str, Any]) -> str:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError("OpenRouter response has no choices")
    content = choices[0].get("message", {}).get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    raise RuntimeError("OpenRouter returned an unsupported message format")


def call_openrouter(
    *, model: str, content: list[dict[str, Any]], plugins: list[dict[str, Any]] | None
) -> str:
    if not API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY is not set")

    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "temperature": 0,
        "max_tokens": 12_000,
        "response_format": {"type": "json_object"},
    }
    if plugins:
        body["plugins"] = plugins
    payload = json.dumps(body).encode("utf-8")

    for attempt in range(3):
        remaining = DEADLINE - time.monotonic()
        if remaining < 15:
            raise TimeoutError("Run deadline reached")
        request = urllib.request.Request(
            API_URL,
            data=payload,
            headers={
                "Authorization": f"Bearer {API_KEY}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://hackathon.acelabusa.com",
                "X-Title": "AEC document error auditor",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=min(240, max(10, remaining - 5))) as resp:
                return response_text(json.loads(resp.read().decode("utf-8")))
        except urllib.error.HTTPError as exc:
            detail = exc.read(1000).decode("utf-8", errors="replace")
            if exc.code not in {408, 409, 429, 500, 502, 503, 504} or attempt == 2:
                raise RuntimeError(f"OpenRouter HTTP {exc.code}: {detail}") from exc
            wait = 2 ** (attempt + 1)
            log(f"OpenRouter HTTP {exc.code}; retrying in {wait}s")
            time.sleep(wait)
        except (TimeoutError, urllib.error.URLError) as exc:
            if attempt == 2:
                raise RuntimeError(f"OpenRouter request failed: {exc}") from exc
            time.sleep(2 ** (attempt + 1))
    raise RuntimeError("OpenRouter request failed")


def call_model(
    *, model: str, content: list[dict[str, Any]], plugins: list[dict[str, Any]] | None
) -> str:
    """Use a stable fallback if the preferred provider/model is unavailable."""
    failures: list[str] = []
    for candidate in dict.fromkeys((model, FALLBACK_MODEL)):
        try:
            return call_openrouter(model=candidate, content=content, plugins=plugins)
        except Exception as exc:
            failures.append(f"{candidate}: {exc}")
            log(f"Model {candidate} failed; trying fallback if available")
    raise RuntimeError("; ".join(failures))


def parse_json_object(text: str) -> dict[str, Any]:
    """Accept plain JSON or a JSON object surrounded by harmless model prose."""
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            value, _ = decoder.raw_decode(text[match.start() :])
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            continue
    return {}


def canonical_document(value: Any, files: list[Path]) -> str | None:
    if not isinstance(value, str):
        return None
    supplied = Path(value.strip().replace("\\", "/")).name
    by_name = {p.name.casefold(): p.name for p in files}
    if supplied.casefold() in by_name:
        return by_name[supplied.casefold()]
    stem = Path(supplied).stem.casefold()
    matches = [p.name for p in files if p.stem.casefold() == stem]
    return matches[0] if len(matches) == 1 else None


def clean_errors(data: dict[str, Any], files: list[Path]) -> list[dict[str, str]]:
    raw_errors = data.get("errors", [])
    if not isinstance(raw_errors, list):
        return []

    cleaned: list[dict[str, str]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for raw in raw_errors[:100]:
        if not isinstance(raw, dict):
            continue
        document = canonical_document(raw.get("document"), files)
        category = str(raw.get("category", "")).strip().lower()
        description = " ".join(str(raw.get("description", "")).split())
        location = " ".join(str(raw.get("location", "")).split())
        if not document or category not in ALLOWED_CATEGORIES or len(description) < 12:
            continue
        # Preserve separate issues while removing exact/near-exact repeated reports.
        anchors = " ".join(re.findall(r"[a-z]*\d+[a-z0-9./'-]*", f"{location} {description}".lower())[:5])
        key = (document.casefold(), category, location.casefold(), anchors)
        if key in seen:
            continue
        seen.add(key)
        finding = {
            "document": document,
            "category": category,
            "location": location or "Location identified in description",
            "description": description,
        }
        cleaned.append(finding)
    return cleaned


def audit(files: list[Path]) -> list[dict[str, str]]:
    total_bytes = sum(path.stat().st_size for path in files)
    native = total_bytes <= MAX_NATIVE_PDF_BYTES

    text_cache = ""
    if native:
        discovery_content, plugins = pdf_content(files, DISCOVERY_PROMPT)
    else:
        log(f"PDF set is {total_bytes / 1024 / 1024:.1f} MiB; using extracted text")
        text_cache = extracted_text(files)
        discovery_content = [{"type": "text", "text": DISCOVERY_PROMPT + "\n\n" + text_cache}]
        plugins = None

    try:
        raw = call_model(model=MODEL, content=discovery_content, plugins=plugins)
    except Exception as exc:
        if not native:
            raise
        log(f"Native PDF analysis failed; retrying with extracted text: {exc}")
        text_cache = extracted_text(files)
        if not text_cache:
            raise
        raw = call_model(
            model=MODEL,
            content=[{"type": "text", "text": DISCOVERY_PROMPT + "\n\n" + text_cache}],
            plugins=None,
        )
        native = False

    candidates = clean_errors(parse_json_object(raw), files)
    log(f"Discovery produced {len(candidates)} schema-valid candidate(s)")

    if time.monotonic() > DEADLINE - 45:
        return candidates

    verifier_prompt = VERIFY_PROMPT + "\n" + json.dumps({"errors": candidates}, ensure_ascii=False)
    try:
        if native:
            verify_content, verify_plugins = pdf_content(files, verifier_prompt)
        else:
            if not text_cache:
                text_cache = extracted_text(files)
            verify_content = [{"type": "text", "text": verifier_prompt + "\n\n" + text_cache}]
            verify_plugins = None
        verified_raw = call_model(
            model=VERIFY_MODEL, content=verify_content, plugins=verify_plugins
        )
        verified = clean_errors(parse_json_object(verified_raw), files)
        log(f"Verification retained {len(verified)} finding(s)")
        return verified
    except Exception as exc:
        log(f"Verification failed; keeping discovery results: {exc}")
        return candidates


def write_output(errors: list[dict[str, str]]) -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = OUTPUT_PATH.with_name(OUTPUT_PATH.name + ".tmp")
    temporary.write_text(
        json.dumps({"errors": errors}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(OUTPUT_PATH)


def main() -> int:
    errors: list[dict[str, str]] = []
    try:
        files = pdf_files()
        log(f"Auditing {len(files)} PDF(s): {', '.join(p.name for p in files)}")
        errors = audit(files)
    except Exception as exc:
        # The grader contract is better served by valid empty JSON than no file.
        log(f"Audit failed: {exc}")
    write_output(errors)
    log(f"Wrote {len(errors)} finding(s) to {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
