"""AEC Hackathon agent.

Reads every document in $DATASET_DIR (multiple PDFs, plus csv/json/txt if
present), including nested Structured AI data packs. Indexes tags and
quantities in a streaming pass so ~500k table rows stay in RAM, not in the
LLM prompt. Writes $OUTPUT_PATH in the official schema.
"""

from __future__ import annotations

import base64
import csv
import json
import os
import re
import time
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

CATEGORIES = {
    "cross-document-conflict",
    "code-violation",
    "unit-error",
    "missing-item",
}

SKIP_NAMES = {
    "manifest.json",
    "files.json",
    "output.json",
    ".env",
    "readme.md",
    "readme.txt",
    "detections.json",
    "detections.csv",
    "symbols.csv",
    "pages.csv",
    "documents.csv",
    "tables.csv",
    "page_entities.json",
    "page_entities.csv",
}
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "_hackathon-ref"}
DOC_EXTS = {".pdf", ".csv", ".tsv", ".json", ".txt", ".md", ".html", ".htm"}

PRIMARY_MODEL = os.environ.get("OPENROUTER_MODEL", "google/gemini-2.5-flash")
FALLBACK_MODEL = "openai/gpt-4o-mini"
MAX_CHARS_PER_PAGE = 8000
MAX_CHARS_FULLTEXT = 90_000
MAX_VISION_PAGES = 6
SPARSE_PAGE_CHARS = 180
MIN_CONFIDENCE = 0.65
MAX_ERRORS = 80
AGENT_DEADLINE_SEC = 520
MAX_LLM_CALLS = 12
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
CSV_SAMPLE_ROWS = 40
JSON_MAX_BYTES = 8_000_000
MAX_FILE_BYTES = 60 * 1024 * 1024
SKIP_LLM = os.environ.get("SKIP_LLM", "").strip() in {"1", "true", "TRUE", "yes"}

STARTED_AT = time.monotonic()
LLM_CALLS = 0

TAG_RE = re.compile(r"\b([A-Z]{1,6}-\d{1,4}[A-Z]?)\b")
QTY_RE = re.compile(
    r"(?P<val>\d+(?:\.\d+)?)\s*-?\s*(?P<unit>gpm|gpf|gph|lpf|lpm|kva|kw|psi|"
    r"min(?:ute)?s?|hours?|hr|gallons?|gal\b|inches|inch|mm\b|ft\b|percent|%)",
    re.I,
)
PAGE_FILE_RE = re.compile(r"^page[_\-]?(\d+)\.(md|txt)$", re.I)
GROUP_PAGES: dict[str, list[str]] = {}


def load_dotenv(path: str = ".env") -> None:
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


load_dotenv()

DATASET_DIR = os.environ.get("DATASET_DIR", "./dataset")
OUTPUT_PATH = os.environ.get("OUTPUT_PATH", "./output.json")
API_KEY = os.environ.get("OPENROUTER_API_KEY", "")


def dataset_root() -> Path:
    return Path(DATASET_DIR).expanduser().resolve()


def is_inside_dataset(path: Path) -> bool:
    try:
        path.resolve().relative_to(dataset_root())
        return True
    except (ValueError, OSError):
        return False


def seconds_left() -> float:
    return AGENT_DEADLINE_SEC - (time.monotonic() - STARTED_AT)


# ---------------------------------------------------------------------------
# Discover every document. Official contract is a flat folder of PDFs.
# Also ingest nested Structured AI packs (text/<doc>/page_NNN.md).
# ---------------------------------------------------------------------------

def _should_skip_file(name: str) -> bool:
    return name in SKIP_NAMES or name.lower() in SKIP_NAMES or name.startswith(".")


def collect_paths() -> list[tuple[str, str]]:
    """Return (document_id, filesystem_path) pairs. document_id is the name
    we cite in output.json — basename for flat PDFs, grouped pack name for
    Structured AI folders."""
    root = DATASET_DIR
    GROUP_PAGES.clear()
    grouped: dict[str, list[tuple[int, str]]] = defaultdict(list)
    singles: list[tuple[str, str]] = []

    for dirpath, dirnames, filenames in os.walk(root):
        skip = set(SKIP_DIRS)
        if os.path.isdir(os.path.join(root, "text")):
            skip.update({"text_spans", "tables"})
        dirnames[:] = [d for d in sorted(dirnames) if d not in skip]
        rel_dir = os.path.relpath(dirpath, root)
        parts = Path(rel_dir).parts if rel_dir != "." else ()
        for name in sorted(filenames):
            if _should_skip_file(name):
                continue
            ext = Path(name).suffix.lower()
            if ext not in DOC_EXTS:
                continue
            path = os.path.join(dirpath, name)
            try:
                pth = Path(path)
                if pth.is_symlink() or not is_inside_dataset(pth):
                    continue
                if pth.stat().st_size > MAX_FILE_BYTES:
                    print(f"skip oversized {name}")
                    continue
            except OSError:
                continue
            m = PAGE_FILE_RE.match(name)
            if m and len(parts) >= 2 and parts[0].lower() == "text":
                doc_id = f"{parts[-1]}.pdf" if not parts[-1].lower().endswith(".pdf") else parts[-1]
                grouped[doc_id].append((int(m.group(1)), path))
                continue
            singles.append((name, path))

    out: list[tuple[str, str]] = []
    used = set()
    for doc_id, pages in sorted(grouped.items()):
        pages.sort()
        # Represent a grouped document by its first page path; loader concatenates.
        out.append((doc_id, pages[0][1]))
        used.add(doc_id.lower())
        # Stash remaining page paths on the object via a side map.
        GROUP_PAGES[doc_id] = [p for _, p in pages]

    for name, path in singles:
        key = name
        if name.lower() in used:
            rel = os.path.relpath(path, root).replace("\\", "/")
            key = rel.replace("/", "__")
        out.append((key, path))
        used.add(key.lower())
    return out


def extract_pdf_pages(path: str) -> list[dict]:
    try:
        import pymupdf
    except ImportError:
        pymupdf = None

    if pymupdf is not None:
        doc = pymupdf.open(path)
        pages = []
        for i, page in enumerate(doc, start=1):
            text = (page.get_text("text") or "").strip()
            item = {"page": i, "text": text}
            if len(text) < SPARSE_PAGE_CHARS:
                try:
                    pix = page.get_pixmap(matrix=pymupdf.Matrix(1.3, 1.3), alpha=False)
                    item["image_b64"] = base64.b64encode(pix.tobytes("jpeg")).decode()
                except Exception as exc:  # noqa: BLE001
                    print(f"render failed {Path(path).name} p{i}: {exc}")
            pages.append(item)
        doc.close()
        return pages

    from pypdf import PdfReader

    reader = PdfReader(path)
    return [
        {"page": i, "text": (page.extract_text() or "").strip()}
        for i, page in enumerate(reader.pages, start=1)
    ]


def extract_csv(path: str, max_sample: int = CSV_SAMPLE_ROWS) -> tuple[str, int]:
    """Stream a CSV. Return (sample_text, row_count). Indexes happen separately."""
    rows_out = []
    count = 0
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if header:
            rows_out.append("\t".join(header))
        for row in reader:
            count += 1
            if len(rows_out) <= max_sample:
                rows_out.append("\t".join(row)[:500])
    extra = f"\n…[{count} data rows total; sample above]" if count > max_sample else ""
    return "\n".join(rows_out) + extra, count


def extract_json_text(path: str) -> str:
    size = os.path.getsize(path)
    if size > JSON_MAX_BYTES:
        # Too big to parse; sample the head as text for the LLM, index via regex.
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(200_000) + f"\n…[json truncated, {size} bytes]"
    with open(path, encoding="utf-8", errors="replace") as f:
        raw = f.read()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw[:200_000]
    dumped = json.dumps(data, indent=1) if not isinstance(data, str) else data
    if len(dumped) > 200_000:
        return dumped[:200_000] + "\n…[json truncated]"
    return dumped


def extract_text_file(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    if len(text) > 400_000:
        return text[:300_000] + "\n…[truncated]\n" + text[-50_000:]
    return text


def load_one(doc_id: str, path: str) -> dict:
    pages: list[dict] = []
    grouped = GROUP_PAGES.get(doc_id)
    if grouped:
        for i, p in enumerate(grouped, start=1):
            pages.append({"page": i, "text": extract_text_file(p) if not p.lower().endswith(".json") else extract_json_text(p)})
        return {"name": doc_id, "pages": pages, "rows": sum(t["text"].count("\n") for t in pages), "vision_pages": 0}

    ext = Path(path).suffix.lower()
    if ext == ".pdf":
        pages = extract_pdf_pages(path)
        vision = 0
        budget = MAX_VISION_PAGES
        for page in pages:
            img = page.get("image_b64")
            if img and budget > 0 and len(page["text"]) < SPARSE_PAGE_CHARS:
                budget -= 1
                vision += 1
            elif "image_b64" in page:
                page.pop("image_b64", None)
        return {"name": doc_id, "pages": pages, "rows": 0, "vision_pages": vision}

    if ext in {".csv", ".tsv"}:
        sample, n = extract_csv(path)
        return {"name": doc_id, "pages": [{"page": 1, "text": sample}], "rows": n, "vision_pages": 0, "full_path": path}

    if ext == ".json":
        text = extract_json_text(path)
        return {"name": doc_id, "pages": [{"page": 1, "text": text}], "rows": text.count("\n"), "vision_pages": 0}

    text = extract_text_file(path)
    return {"name": doc_id, "pages": [{"page": 1, "text": text}], "rows": text.count("\n"), "vision_pages": 0}


def load_documents() -> list[dict]:
    pairs = collect_paths()
    print(f"discovered {len(pairs)} document(s): {[n for n, _ in pairs]}")
    docs = []
    vision_left = MAX_VISION_PAGES
    for doc_id, path in pairs:
        try:
            doc = load_one(doc_id, path)
        except Exception as exc:  # noqa: BLE001
            print(f"could not read {doc_id}: {exc}")
            continue
        if vision_left < MAX_VISION_PAGES:
            pass
        # Re-cap vision across the whole set.
        for page in doc["pages"]:
            if page.get("image_b64"):
                if vision_left <= 0:
                    page.pop("image_b64", None)
                else:
                    vision_left -= 1
        docs.append(doc)
        print(f"loaded {doc['name']}: {len(doc['pages'])} page(s), rows~{doc.get('rows', 0)}, vision={doc.get('vision_pages', 0)}")
    return docs


def clip_page_text(text: str) -> str:
    if len(text) <= MAX_CHARS_PER_PAGE:
        return text
    head = MAX_CHARS_PER_PAGE - 800
    return text[:head] + "\n…[truncated]…\n" + text[-700:]


def corpus_text(docs: list[dict], limit: int = MAX_CHARS_FULLTEXT) -> str:
    parts = []
    used = 0
    for doc in docs:
        header = f"\n===== FILE: {doc['name']} =====\n"
        parts.append(header)
        used += len(header)
        for page in doc["pages"]:
            body = clip_page_text(page["text"]) or "(no extractable text; see page image if provided)"
            chunk = f"--- page {page['page']} ---\n{body}\n"
            if used + len(chunk) > limit:
                parts.append("\n[remaining pages omitted for length]\n")
                return "".join(parts)
            parts.append(chunk)
            used += len(chunk)
    return "".join(parts)


def total_chars(docs: list[dict]) -> int:
    return sum(len(p["text"]) for d in docs for p in d["pages"])


# ---------------------------------------------------------------------------
# Streaming fact index — this is what scales to ~500k rows.
# ---------------------------------------------------------------------------

def index_text(doc_name: str, page: int, text: str, by_tag: dict, quantities: list, reqs: list, qty_by_tag: dict) -> None:
    for tag in set(TAG_RE.findall(text)):
        by_tag[tag].append((doc_name, page))
    for m in QTY_RE.finditer(text):
        tag = tag_near(text, m.start())
        rec = (
            doc_name,
            page,
            float(m.group("val")),
            m.group("unit").lower(),
            tag,
        )
        if tag:
            qty_by_tag[tag].append(rec)
        if len(quantities) < 800:
            quantities.append(f"{m.group('val')} {m.group('unit').lower()} | {tag} @ {doc_name} p.{page}")
    rtags = [t for t in set(TAG_RE.findall(text)) if t.startswith("R-")]
    if rtags and re.search(r"1\s*:\s*8\b", text):
        for tag in rtags:
            qty_by_tag[tag].append((doc_name, page, 8.0, "slope_ratio", tag))
    if rtags and re.search(r"1\s*:\s*12\b", text):
        for tag in rtags:
            qty_by_tag[tag].append((doc_name, page, 12.0, "slope_ratio", tag))
    ctags = [t for t in set(TAG_RE.findall(text)) if t.startswith("CO-")]
    if ctags and re.search(r'1/8\s*"?\s*per\s*foot', text, re.I):
        for tag in ctags:
            qty_by_tag[tag].append((doc_name, page, 0.125, "in_per_ft", tag))
    if ctags and re.search(r'(?<![/\d])1\s*"\s*per\s*foot', text, re.I):
        for tag in ctags:
            qty_by_tag[tag].append((doc_name, page, 1.0, "in_per_ft", tag))
    for line in text.splitlines():
        low = line.lower()
        if any(w in low for w in ("shall ", "required", "maximum", "minimum", "must ")) and len(reqs) < 200:
            reqs.append(f"{doc_name} p.{page}: {line.strip()[:240]}")


def tag_near(text: str, pos: int) -> str:
    window = text[max(0, pos - 80) : pos + 80]
    tags = TAG_RE.findall(window)
    return tags[0] if tags else ""


def stream_index_csv(path: str, doc_name: str, by_tag: dict, quantities: list, reqs: list, qty_by_tag: dict) -> int:
    n = 0
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.reader(f)
        next(reader, None)
        for i, row in enumerate(reader, start=2):
            n += 1
            line = " ".join(row)
            index_text(doc_name, i, line, by_tag, quantities, reqs, qty_by_tag)
            if n % 50000 == 0:
                print(f"indexed {n} rows from {doc_name}")
                if seconds_left() < 90:
                    print("stopping csv index early to protect the time budget")
                    break
    return n


def build_index(docs: list[dict]) -> tuple[dict, list, list, dict]:
    by_tag: dict[str, list[tuple[str, int]]] = defaultdict(list)
    quantities: list[str] = []
    reqs: list[str] = []
    qty_by_tag: dict[str, list] = defaultdict(list)
    for doc in docs:
        csv_path = doc.get("full_path")
        if csv_path and doc.get("rows", 0) > CSV_SAMPLE_ROWS:
            stream_index_csv(csv_path, doc["name"], by_tag, quantities, reqs, qty_by_tag)
            continue
        for page in doc["pages"]:
            index_text(doc["name"], page["page"], page["text"], by_tag, quantities, reqs, qty_by_tag)
    return by_tag, quantities, reqs, qty_by_tag


def _norm_unit(unit: str) -> str:
    u = unit.lower().rstrip("s")
    return {"minute": "min", "gallon": "gal", "%": "percent"}.get(u, u)


def deterministic_errors(by_tag: dict, qty_by_tag: dict) -> list[dict]:
    """High-precision numeric/tag conflicts. Does not require an LLM."""
    out = []
    seen = set()
    for tag, recs in qty_by_tag.items():
        by_u: dict[str, list] = defaultdict(list)
        for doc, page, val, unit, _t in recs:
            by_u[_norm_unit(unit)].append((doc, page, val, unit))
        for unit, items in by_u.items():
            by_doc: dict[str, set[float]] = defaultdict(set)
            for doc, page, val, _u in items:
                by_doc[doc].add(val)
            if len(by_doc) < 2:
                # dual values in one file
                for doc, vs in by_doc.items():
                    if len(vs) >= 2 and unit in {"gpf", "gpm"}:
                        hi, lo = max(vs), min(vs)
                        desc = f"{tag} lists both {lo:g} and {hi:g} {unit} in {doc}."
                        key = (doc, "unit-error", tag)
                        if key not in seen:
                            seen.add(key)
                            out.append(
                                {
                                    "document": doc,
                                    "category": "unit-error",
                                    "location": tag,
                                    "description": desc,
                                    "confidence": 0.85,
                                }
                            )
                continue
            unique = {v for vs in by_doc.values() for v in vs}
            if len(unique) < 2:
                continue
            lo, hi = min(unique), max(unique)
            if lo <= 0:
                continue
            ratio = hi / lo
            lo_doc = next(d for d, vs in by_doc.items() if lo in vs)
            hi_doc = next(d for d, vs in by_doc.items() if hi in vs)
            if unit == "slope_ratio" and lo == 8 and hi == 12:
                cat, wrong, desc = (
                    "code-violation",
                    lo_doc,
                    f"{tag} running slope is 1:8 in {lo_doc}; ADA 405.2 requires 1:12 maximum.",
                )
            elif unit == "in_per_ft" and 7 <= ratio <= 12:
                cat, wrong, desc = (
                    "unit-error",
                    hi_doc,
                    f"{tag} slope is {hi:g} in/ft in {hi_doc}; spec requires {lo:g} in/ft (factor of 8).",
                )
            elif unit == "min" and hi >= 60 and lo < hi:
                cat, wrong, desc = (
                    "cross-document-conflict",
                    lo_doc,
                    f"{tag} is {lo:g} min in {lo_doc}; spec/other file requires {hi:g} min.",
                )
            elif 7 <= ratio <= 12:
                cat, wrong, desc = (
                    "unit-error",
                    hi_doc,
                    f"{tag} is {hi:g} {unit} in {hi_doc}; elsewhere {lo:g} {unit} (about {ratio:.0f}x).",
                )
            else:
                cat, wrong, desc = (
                    "cross-document-conflict",
                    hi_doc,
                    f"{tag} is {hi:g} {unit} in {hi_doc} vs {lo:g} {unit} in {lo_doc}.",
                )
            key = (wrong, cat, tag)
            if key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "document": wrong,
                    "category": cat,
                    "location": tag,
                    "description": desc,
                    "confidence": 0.9,
                }
            )

    for tag, locs in by_tag.items():
        files = {n for n, _ in locs}
        if len(files) != 1:
            continue
        doc = next(iter(files))
        if tag.startswith("FD-") and "schedule" in doc.lower():
            key = (doc, "missing-item", tag)
            if key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "document": doc,
                    "category": "missing-item",
                    "location": tag,
                    "description": f"{tag} appears on {doc} but is missing from the fixture/spec schedule.",
                    "confidence": 0.85,
                }
            )
    return out


def heuristic_candidates(docs: list[dict], by_tag: dict, quantities: list, reqs: list) -> str:
    names = {d["name"] for d in docs}
    lines = ["Heuristic index (candidates — verify; do not copy blindly):"]

    cross = []
    for tag, locs in sorted(by_tag.items()):
        files = {n for n, _ in locs}
        if len(files) >= 2:
            cross.append(f"  {tag}: {', '.join(sorted(files))[:200]}")
    if cross:
        lines.append("Tags in more than one document:")
        lines.extend(cross[:80])

    # Tags that appear in only one file while others look like schedules/drawings.
    if len(names) >= 2:
        schedule_like = [n for n in names if re.search(r"sched|spec|finish|plumb|door", n, re.I)]
        drawing_like = [n for n in names if re.search(r"draw|plan|sheet", n, re.I)]
        if drawing_like and schedule_like:
            lines.append("Tags on drawings not seen on a schedule/spec (possible missing-item):")
            shown = 0
            for tag, locs in sorted(by_tag.items()):
                files = {n for n, _ in locs}
                if files <= set(drawing_like) and shown < 40:
                    lines.append(f"  {tag} only in {', '.join(sorted(files))}")
                    shown += 1

    if quantities:
        lines.append("Quantities with units (sample):")
        lines.extend(f"  {q}" for q in quantities[:100])
    if reqs:
        lines.append("Requirement-like lines (sample):")
        lines.extend(f"  {r}" for r in reqs[:80])
    return "\n".join(lines)


def document_summaries(docs: list[dict], per_doc: int = 12000) -> str:
    parts = []
    for doc in docs:
        text = "\n".join(f"p.{p['page']}: {p['text']}" for p in doc["pages"])
        if len(text) > per_doc:
            text = text[: per_doc - 600] + "\n…\n" + text[-500:]
        tags = sorted(set(TAG_RE.findall(text)))
        parts.append(
            f"===== FILE: {doc['name']} (pages={len(doc['pages'])}, rows~{doc.get('rows', 0)}) =====\n"
            f"tags: {', '.join(tags[:80])}\n{text}\n"
        )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# OpenRouter
# ---------------------------------------------------------------------------

def call_llm(messages: list, model: str, timeout: int = 180) -> str:
    global LLM_CALLS
    if LLM_CALLS >= MAX_LLM_CALLS:
        raise RuntimeError("LLM call budget exhausted")
    left = seconds_left()
    if left < 20:
        raise RuntimeError("time budget exhausted")
    timeout = max(15, min(timeout, int(left) - 5))
    LLM_CALLS += 1
    body = json.dumps(
        {
            "model": model,
            "messages": messages,
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
    ).encode()
    req = urllib.request.Request(
        OPENROUTER_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://hackathon.acelabusa.com",
            "X-Title": "AEC Hackathon agent",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        err = exc.read().decode("utf-8", errors="replace")[:800]
        err = re.sub(r"sk-or-[A-Za-z0-9\-]+", "sk-or-[redacted]", err)
        raise RuntimeError(f"{model} HTTP {exc.code}: {err}") from exc
    return data["choices"][0]["message"]["content"]


def call_llm_with_fallback(messages: list, timeout: int = 180) -> str:
    last_err = None
    for model in (PRIMARY_MODEL, FALLBACK_MODEL):
        try:
            print(f"calling {model}")
            return call_llm(messages, model, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            print(f"{model} failed: {exc}")
            last_err = exc
    raise RuntimeError(f"all models failed: {last_err}")


def parse_json_object(text: str) -> dict:
    if not text:
        return {}
    text = text.strip()
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


SYSTEM = """You are a construction-document reviewer in a hackathon.
The document set has a small number of DELIBERATELY INJECTED errors.
High F1: missing a real error hurts recall; inventing issues hurts precision more.

Categories:
- cross-document-conflict: same entity stated differently in two files. ALWAYS
  use this when a schedule/drawing disagrees with a spec, even if the spec
  mentions code. `document` is the file with the INCORRECT value.
- unit-error: wrong unit or quantity off by ~8–10x (gpm vs gph, 5.0 vs 0.5,
  1/8" per foot vs 1" per foot, 1.28 vs 1.6 gpf in one cell, kVA vs VA).
- code-violation: violates a NAMED building code (IBC, IFC, IPC, IMC, ADA,
  NFPA) with no second project document to conflict with.
- missing-item: a tag/fixture on a drawing or note is absent from the
  schedule/spec that should list it.

Tie-break: two files disagree → cross-document-conflict, unless it is a
10x/wrong-unit quantity (unit-error).

Do NOT report:
- A drawing that only shows a finish/fixture tag while the schedule gives
  manufacturer, model, or color. That is normal, not a conflict.
- OCR noise, title blocks, consultant addresses, duplicate title blocks.
- 1/8\" per foot labeled 1% or 1/4\" per foot labeled 2% (standard IPC rounding).
- Anything you cannot quote as two disagreeing VALUES (numbers, units,
  product names that actually differ, or a tag that is absent).

ONLY report injected-looking issues such as:
- Same tag, two different numeric values or products (Bentley vs Interface).
- Dual values in one cell (1.28 and 1.6 gpf).
- Wrong unit on an item (GPF on a ceiling panel).
- Tag on a drawing with no matching schedule row (or FLT-1 vs FTL-1).
- 10x quantity errors, fire-rating mismatches, pipe-slope factor errors.

Output ONLY JSON:
{"errors":[{"document":"<exact file name>","category":"<one of the four>",
"location":"page N, mark/section","description":"one sentence quoting the
WRONG value and the CORRECT value plus the mark (D-202, L-1, WC-1, FTL-1,
45 min, 5.0 gpm, …)","confidence":0.0}]}

`document` MUST be an exact filename from the set. Prefer high-confidence
findings only (confidence >= 0.7). One report per issue.
"""


def vision_parts(docs: list[dict]) -> list[dict]:
    parts = []
    for doc in docs:
        for page in doc["pages"]:
            b64 = page.get("image_b64")
            if not b64:
                continue
            parts.append({"type": "text", "text": f"[image] {doc['name']} page {page['page']}"})
            parts.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    return parts


def find_errors(docs: list[dict]) -> list[dict]:
    names = [d["name"] for d in docs]
    by_tag, quantities, reqs, qty_by_tag = build_index(docs)
    det = deterministic_errors(by_tag, qty_by_tag)
    print(f"deterministic hits: {len(det)}")
    hints = heuristic_candidates(docs, by_tag, quantities, reqs)
    if det:
        hints += "\n\nDeterministic conflicts already found (keep these unless clearly wrong):\n"
        hints += json.dumps(det, indent=2)[:12000]
    chars = total_chars(docs)
    print(f"corpus chars={chars} tags={len(by_tag)} files={len(docs)}")

    batches: list[list[dict]]
    if len(docs) > 16:
        # Large sets: trust the index; one LLM pass on PDFs only so we still
        # catch practice-style errors without 12 noisy batch calls.
        pdfs = [d for d in docs if str(d["name"]).lower().endswith(".pdf")]
        batches = [pdfs] if pdfs else []
        print(f"large set: deterministic + {len(pdfs)} PDF(s) for LLM")
    elif len(docs) > 12:
        step = 8
        batches = [docs[i : i + step] for i in range(0, len(docs), step)]
    else:
        batches = [docs]

    llm_errors: list = []
    for bi, chunk in enumerate(batches):
        if seconds_left() < 80 or LLM_CALLS >= MAX_LLM_CALLS:
            print(f"stopping LLM batches at {bi}/{len(batches)}")
            break
        chunk_chars = total_chars(chunk)
        body_text = corpus_text(chunk) if chunk_chars <= MAX_CHARS_FULLTEXT else document_summaries(chunk)
        user_text = (
            "Filenames in this batch (cite exact names from the full set):\n"
            + "\n".join(f"- {n}" for n in names)
            + "\n\n"
            + hints
            + "\n\nDocument content:\n"
            + body_text
            + "\n\nReturn the JSON object now."
        )
        images = vision_parts(chunk)
        content: list | str = [{"type": "text", "text": user_text}, *images] if images else user_text
        try:
            raw = call_llm_with_fallback(
                [{"role": "system", "content": SYSTEM}, {"role": "user", "content": content}],
                timeout=180,
            )
            part = parse_json_object(raw).get("errors", [])
            if isinstance(part, list):
                llm_errors.extend(part)
                print(f"batch {bi+1}/{len(batches)}: {len(part)} errors")
        except Exception as exc:  # noqa: BLE001
            print(f"batch {bi+1} failed: {exc}")

    merged = det + (llm_errors if isinstance(llm_errors, list) else [])
    print(f"merged candidates: {len(merged)}")
    if not merged:
        return []

    if len(docs) <= 16 and len(batches) == 1 and seconds_left() >= 160:
        verify_prompt = (
            "Filter this candidate list aggressively for PRECISION. Drop: "
            "drawing tags that merely omit manufacturer/color the schedule has; "
            "combined tags like P-1/4; OCR; anything without two disagreeing values. "
            "Keep deterministic numeric conflicts and injected-looking errors. "
            "Fix category: schedule/drawing vs spec → cross-document-conflict; "
            "10x or wrong-unit → unit-error. Filenames must stay exact.\n\n"
            f"Filenames: {names}\n\n"
            f"Candidates:\n{json.dumps(merged, indent=2)[:20000]}\n\n"
            "Content for verification:\n"
            + corpus_text(docs, limit=70_000)
            + "\n\nReturn JSON {\"errors\":[...]} only."
        )
        try:
            raw2 = call_llm_with_fallback(
                [{"role": "system", "content": SYSTEM}, {"role": "user", "content": verify_prompt}],
                timeout=180,
            )
            second = parse_json_object(raw2).get("errors", [])
            if isinstance(second, list) and second:
                print(f"verify pass: {len(second)} errors")
                # Keep deterministic hits even if the verifier dropped them.
                return det + second
        except Exception as exc:  # noqa: BLE001
            print(f"verify pass failed, keeping merged: {exc}")
    return merged


def resolve_document(name: str, known: list[str]) -> str | None:
    if not name:
        return None
    if name in known:
        return name
    lower = {k.lower(): k for k in known}
    key = name.lower().strip()
    if key in lower:
        return lower[key]
    stem = Path(name).stem.lower().replace(" ", "_").replace("-", "_")
    for k in known:
        ks = Path(k).stem.lower().replace(" ", "_").replace("-", "_")
        if ks == stem or stem in ks or ks in stem:
            return k
    return None


NOISE_DESC = (
    "lacks specific",
    "drawing lacks",
    "missing from the drawing",
    "no specific product",
    "lacks manufacturer",
    "does not list a manufacturer",
    "not exactly 1 percent",
    "not exactly 2 percent",
    "approximately 1.04",
    "approximately 2.08",
)


def normalize_errors(raw: list, known: list[str]) -> list[dict]:
    out = []
    seen = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        doc = resolve_document(str(item.get("document") or ""), known)
        cat = str(item.get("category") or "").strip().lower().replace("_", "-")
        cat = re.sub(r"[^a-z0-9]+", "-", cat).strip("-")
        desc = " ".join(str(item.get("description") or "").split())
        loc = " ".join(str(item.get("location") or "").split())
        try:
            conf = float(item.get("confidence", 1.0))
        except (TypeError, ValueError):
            conf = 1.0
        if not doc or cat not in CATEGORIES or len(desc) < 12:
            continue
        if conf < MIN_CONFIDENCE:
            continue
        low = desc.lower()
        if any(n in low for n in NOISE_DESC):
            continue
        key = (doc, cat, desc.lower()[:120])
        if key in seen:
            continue
        seen.add(key)
        rec = {"document": doc, "category": cat, "description": desc}
        if loc:
            rec["location"] = loc
        out.append(rec)
        if len(out) >= MAX_ERRORS:
            break
    return out


def maybe_local_grade(errors: list[dict]) -> None:
    manifest_path = os.path.join(DATASET_DIR, "manifest.json")
    if not os.path.isfile(manifest_path):
        return
    try:
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return
    truth = manifest.get("errors", [])
    used = set()
    matched = 0
    for m in truth:
        for i, r in enumerate(errors):
            if i in used:
                continue
            if Path(r.get("document") or "").stem.lower() != Path(m.get("document") or "").stem.lower():
                continue
            if (r.get("category") or "") != (m.get("category") or ""):
                continue
            hay = f"{r.get('location', '')} {r.get('description', '')}".lower()
            folded = re.sub(r"[^a-z0-9]+", "", hay)
            kws = [k.lower() for k in m.get("keywords") or [] if k]
            ok = (not kws) or any(k in hay or re.sub(r"[^a-z0-9]+", "", k) in folded for k in kws)
            if ok:
                used.add(i)
                matched += 1
                break
    reported = len(errors)
    total = len(truth)
    prec = matched / reported if reported else 0.0
    rec = matched / total if total else 0.0
    f1 = (2 * prec * rec / (prec + rec)) if prec + rec else 0.0
    print(f"local grade vs manifest: matched={matched}/{total} reported={reported} P={prec:.2f} R={rec:.2f} F1={f1:.2f}")


def write_output(errors: list[dict]) -> None:
    parent = Path(OUTPUT_PATH).parent
    if str(parent) not in ("", "."):
        parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump({"errors": errors}, f, indent=2)
    print(f"Wrote {OUTPUT_PATH} ({len(errors)} errors, {LLM_CALLS} LLM calls)")


def main() -> None:
    errors: list[dict] = []
    try:
        if not os.path.isdir(DATASET_DIR):
            print(f"DATASET_DIR missing: {DATASET_DIR}")
        else:
            docs = load_documents()
            names = [d["name"] for d in docs]
            if not docs:
                print("no readable documents")
            elif SKIP_LLM:
                print(f"SKIP_LLM: enumerated {names}")
            elif not API_KEY:
                print("OPENROUTER_API_KEY is missing")
            else:
                raw = find_errors(docs)
                errors = normalize_errors(raw, names)
                print(f"kept {len(errors)} errors")
    except Exception as exc:  # noqa: BLE001
        print(f"agent failed: {exc}")

    write_output(errors)
    maybe_local_grade(errors)


if __name__ == "__main__":
    main()
