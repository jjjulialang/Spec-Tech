"""Run edge cases against find_errors.py with SKIP_LLM=1 (no API spend)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "agents" / "reports" / "edge_cases.json"
PY = sys.executable


def run_case(name: str, dataset: Path, expect_ok: bool = True) -> dict:
    out = Path(tempfile.mkdtemp()) / "output.json"
    env = os.environ.copy()
    env["DATASET_DIR"] = str(dataset)
    env["OUTPUT_PATH"] = str(out)
    env["SKIP_LLM"] = "1"
    env.pop("OPENROUTER_API_KEY", None)
    proc = subprocess.run(
        [PY, str(ROOT / "find_errors.py")],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    parsed = None
    if out.is_file():
        parsed = json.loads(out.read_text(encoding="utf-8"))
    ok = proc.returncode == 0 and parsed is not None and isinstance(parsed.get("errors"), list)
    return {
        "name": name,
        "ok": ok if expect_ok else proc.returncode == 0,
        "returncode": proc.returncode,
        "wrote_output": parsed is not None,
        "n_errors": len(parsed["errors"]) if parsed else None,
        "stderr_tail": (proc.stderr or "")[-400:],
        "stdout_tail": (proc.stdout or "")[-400:],
    }


def main() -> int:
    results = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp_p = Path(tmp)

        empty = tmp_p / "empty"
        empty.mkdir()
        results.append(run_case("empty_dir", empty))

        missing = tmp_p / "does-not-exist"
        results.append(run_case("missing_dir", missing))

        one = tmp_p / "one"
        one.mkdir()
        (one / "spec.txt").write_text("Doors at mechanical rooms shall carry a 90-minute fire rating.\n", encoding="utf-8")
        results.append(run_case("single_file", one))

        multi = tmp_p / "multi"
        multi.mkdir()
        (multi / "schedule.txt").write_text("D-202 Mechanical 45 min\nL-1 5.0 gpm\n", encoding="utf-8")
        (multi / "spec.txt").write_text("mechanical rooms 90-minute\nlavatory 0.5 gpm\n", encoding="utf-8")
        results.append(run_case("two_docs", multi))

        nested = tmp_p / "pack"
        (nested / "text" / "Drawings").mkdir(parents=True)
        (nested / "text" / "Drawings" / "page_001.md").write_text("FTL-1 floor tile\nWC-1\n", encoding="utf-8")
        (nested / "text" / "Drawings" / "page_002.md").write_text("slope 1/8 inch per foot\n", encoding="utf-8")
        results.append(run_case("structured_ai_pack", nested))

        huge = tmp_p / "huge"
        huge.mkdir()
        csv_path = huge / "rows.csv"
        with csv_path.open("w", encoding="utf-8") as f:
            f.write("tag,qty,unit\n")
            for i in range(20000):
                f.write(f"EQ-{i},0.5,gpm\n")
        (huge / "spec.txt").write_text("EQ-1 shall be 0.5 gpm\n", encoding="utf-8")
        results.append(run_case("20k_csv_plus_spec", huge))

        junk = tmp_p / "junk"
        junk.mkdir()
        (junk / "broken.pdf").write_bytes(b"%PDF-1.4\nnot a real pdf\n")
        (junk / "ok.txt").write_text("L-1 0.5 gpm required\n", encoding="utf-8")
        results.append(run_case("garbage_pdf_and_txt", junk))

        # Hardened case 001 if present
        h = ROOT / "hardened-datasets" / "case_001"
        if h.is_dir():
            results.append(run_case("hardened_case_001", h))

        # Official practice PDFs
        prac = ROOT / "practice-dataset"
        if prac.is_dir():
            results.append(run_case("practice_enumerate", prac))

        # Official 3-doc pack
        pack = ROOT / "_hackathon-ref" / "assets" / "datasets" / "uccs_hackathon_data_pack"
        if pack.is_dir():
            results.append(run_case("official_uccs_pack", pack))

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    failed = [r for r in results if not r["ok"]]
    payload = {"cases": results, "failed": len(failed), "ok": not failed}
    REPORT.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps({"failed": len(failed), "total": len(results), "names": [r["name"] for r in results]}, indent=2))
    for r in failed:
        print("FAIL", r["name"], r.get("stdout_tail"), r.get("stderr_tail"))
    if failed:
        print("EDGE CASES FAILED")
        return 1
    print("EDGE CASES PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
