"""Check the submission against organizer + official-repo contract."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "agents" / "reports" / "alignment.json"
CATEGORIES = {
    "cross-document-conflict",
    "code-violation",
    "unit-error",
    "missing-item",
}


def main() -> int:
    fails = []
    warns = []
    py = (ROOT / "find_errors.py").read_text(encoding="utf-8")
    sh = (ROOT / "run.sh").read_text(encoding="utf-8")
    schema = json.loads((ROOT / "schema.json").read_text(encoding="utf-8"))

    if not (ROOT / "run.sh").is_file():
        fails.append("run.sh missing at repo root")
    if "DATASET_DIR" not in py or "OUTPUT_PATH" not in py or "OPENROUTER_API_KEY" not in py:
        fails.append("agent must read DATASET_DIR, OUTPUT_PATH, OPENROUTER_API_KEY")
    if "os.listdir" not in py and "os.walk" not in py:
        fails.append("must enumerate DATASET_DIR, not hardcode names")
    if "schedule.pdf" in py and "hardcode" not in py.lower():
        # mention in comments is ok; assignment of a single filename is not
        if re.search(r'["\']schedule\.pdf["\']', py):
            fails.append("hardcoded schedule.pdf filename")
    for cat in CATEGORIES:
        if cat not in py:
            fails.append(f"category {cat} missing from agent")
    props = schema["properties"]["errors"]["items"]["properties"]["category"]["enum"]
    if set(props) != CATEGORIES:
        fails.append("schema.json categories drifted from organizer enum")
    if "AGENT_DEADLINE_SEC" not in py:
        fails.append("no wall-clock budget for the 10-minute timeout")
    if "MAX_LLM_CALLS" not in py:
        fails.append("no LLM call cap (limit is 300)")
    if "pip3 install" not in sh:
        warns.append("run.sh does not pip-install extras (ok if using preinstalled pypdf)")
    if "openrouter.ai" not in py:
        fails.append("must call OpenRouter")
    if "json.dump" not in py or '{"errors"' not in py.replace(" ", "") and '"errors"' not in py:
        if '"errors"' not in py:
            fails.append("output must be {errors: [...]}")

    # Submission must not ship secrets or the organizer repo.
    if (ROOT / "_hackathon-ref").exists() and "_hackathon-ref/" not in (ROOT / ".gitignore").read_text(
        encoding="utf-8"
    ):
        fails.append("_hackathon-ref must be gitignored")
    if ".env" not in (ROOT / ".gitignore").read_text(encoding="utf-8"):
        fails.append(".env must be gitignored")

    payload = {"fails": fails, "warnings": warns, "ok": not fails}
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload, indent=2))
    if fails:
        print("ALIGNMENT FAILED")
        return 1
    print("ALIGNMENT PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
