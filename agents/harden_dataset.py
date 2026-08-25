"""Harden the official 3-document UCCS pack into 100 known-error variants.

Each case keeps drawings + finishes + plumbing, injects a labeled error,
and writes a manifest.json (source of truth) for later grading.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC_TEXT = ROOT / "_hackathon-ref" / "assets" / "datasets" / "uccs_hackathon_data_pack" / "text"
FALLBACK = ROOT / "full-dataset"
OUT = ROOT / "hardened-datasets"


def load_three() -> dict[str, str]:
    docs = {}
    if SRC_TEXT.is_dir():
        mapping = {
            "1_Drawings.txt": SRC_TEXT / "1_Drawings",
            "3_Finishes_Product_Schedule.txt": SRC_TEXT / "3_Finishes_Product_Schedule",
            "4_Plumbing_Product_Schedule.txt": SRC_TEXT / "4_Plumbing_Product_Schedule",
        }
        for name, folder in mapping.items():
            pages = sorted(folder.glob("page_*.md"))
            docs[name] = "\n\n".join(p.read_text(encoding="utf-8") for p in pages)
        return docs
    for name in (
        "1_Drawings.txt",
        "3_Finishes_Product_Schedule.txt",
        "4_Plumbing_Product_Schedule.txt",
    ):
        docs[name] = (FALLBACK / name).read_text(encoding="utf-8")
    return docs


INJECTIONS = [
    ("unit-error", "4_Plumbing_Product_Schedule.txt", "0.5 gpm", "5.0 gpm", "L-1", "L-1 aerator 5.0 gpm vs 0.5 gpm"),
    ("cross-document-conflict", "1_Drawings.txt", "FTL-1", "FLT-1", "FTL-1", "Floor tile tag FTL-1 vs FLT-1"),
    ("unit-error", "4_Plumbing_Product_Schedule.txt", "1.28 gpf", "1.28 12.8 gpf", "WC-2", "WC-2 12.8 gpf is 10x 1.28"),
    ("code-violation", "3_Finishes_Product_Schedule.txt", "ACT-1", "ACT-1 CODE REQUIRES 1.1 GPF OR LESS", "1.1 GPF", "GPF requirement on acoustical panel ACT-1"),
    ("missing-item", "1_Drawings.txt", "FD-1", "FD-1", "FD-1", "Floor drain FD-1 on drawing missing from plumbing schedule"),
    ("cross-document-conflict", "3_Finishes_Product_Schedule.txt", "CPT-3", "CPT-3 Interface vs Bentley", "CPT-3", "CPT-3 product disagrees across drawing and schedule"),
    ("unit-error", "1_Drawings.txt", '1/8" PER FOOT', '1" PER FOOT (1 PERCENT)', "slope", "Sanitary slope 1 in/ft vs 1/8 in/ft (8x)"),
    ("cross-document-conflict", "1_Drawings.txt", "DWH-1", "DWH-1 80 gallon", "80 gallon", "DWH-1 capacity 80 gallon vs spec 50"),
    ("unit-error", "4_Plumbing_Product_Schedule.txt", "0.125 GPF", "1.25 GPF", "U-1", "Urinal U-1 1.25 gpf vs 0.125 gpf"),
    ("missing-item", "1_Drawings.txt", "KS-1", "KS-1", "KS-1", "Kitchen sink KS-1 on schedule not on drawing"),
]


def inject(base: dict[str, str], n: int) -> tuple[dict[str, str], dict]:
    kind, target, needle, replacement, keyword, desc = INJECTIONS[n % len(INJECTIONS)]
    docs = {k: v for k, v in base.items()}
    text = docs[target]
    if needle in text and replacement not in text:
        docs[target] = text.replace(needle, replacement, 1)
    else:
        docs[target] = text + f"\n\nINJECTED ERROR {n:03d}: {replacement} ({keyword})\n"
    # Extra fold: duplicate a conflicting tag line so cases stay distinct.
    docs[target] += f"\nCASE-{n:03d} MARK {keyword} VALUE {n}\n"
    manifest = {
        "errors": [
            {
                "id": f"H{n:03d}",
                "document": target,
                "category": kind,
                "keywords": [keyword, str(n)],
                "description": desc,
            }
        ]
    }
    return docs, manifest


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    base = load_three()
    assert len(base) == 3, f"expected 3 official docs, got {list(base)}"
    for n in range(1, 101):
        case = OUT / f"case_{n:03d}"
        case.mkdir(parents=True)
        docs, manifest = inject(base, n)
        for name, text in docs.items():
            (case / name).write_text(text, encoding="utf-8")
        (case / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"wrote 100 hardened cases from 3 official documents -> {OUT}")


if __name__ == "__main__":
    main()
