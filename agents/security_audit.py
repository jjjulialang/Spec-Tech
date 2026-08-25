"""Static security audit of the submission agent. Exit 1 if blocking findings."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "agents" / "reports" / "security_audit.json"

BLOCKING = []
WARN = []


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8", errors="replace")


def check() -> None:
    py = read("find_errors.py")
    sh = (ROOT / "run.sh").read_bytes()
    gitignore = read(".gitignore")

    if b"\r\n" in sh or sh.endswith(b"\r"):
        BLOCKING.append("run.sh has CRLF line endings; Linux sandbox will fail.")
    if not sh.startswith(b"#!/bin/bash"):
        BLOCKING.append("run.sh must start with #!/bin/bash")
    if b"python3 find_errors.py" not in sh:
        BLOCKING.append("run.sh must execute python3 find_errors.py")

    if re.search(r"sk-or-v1-[A-Za-z0-9]{10,}", py):
        BLOCKING.append("find_errors.py appears to contain a hardcoded API key.")
    if ".env" not in gitignore:
        BLOCKING.append(".env is not gitignored.")
    if "eval(" in py or "exec(" in py or "os.system" in py or "subprocess" in py:
        BLOCKING.append("find_errors.py uses eval/exec/os.system/subprocess.")
    if "pickle" in py:
        BLOCKING.append("find_errors.py imports pickle.")

    urls = re.findall(r"https?://[^\s\"']+", py)
    allowed = ("openrouter.ai", "hackathon.acelabusa.com")
    for u in urls:
        if not any(a in u for a in allowed):
            BLOCKING.append(f"Unexpected URL in find_errors.py: {u}")

    if "OPENROUTER_URL" not in py or "openrouter.ai/api/v1/chat/completions" not in py:
        BLOCKING.append("OpenRouter chat completions URL missing.")
    if "DATASET_DIR" not in py or "OUTPUT_PATH" not in py:
        BLOCKING.append("Required env vars not referenced.")
    if "is_inside_dataset" not in py:
        BLOCKING.append("Path confinement (is_inside_dataset) missing.")
    if "MAX_FILE_BYTES" not in py:
        WARN.append("No MAX_FILE_BYTES cap.")
    if "sk-or-[redacted]" not in py:
        WARN.append("HTTP errors may leak key material.")
    if "print(" in py and "API_KEY" in py:
        for line in py.splitlines():
            if "print(" in line and "API_KEY" in line and "OPENROUTER_API_KEY is missing" not in line:
                BLOCKING.append(f"Possible API key log: {line.strip()}")

    env = ROOT / ".env"
    if env.exists() and env.stat().st_size > 0:
        WARN.append(".env exists locally (ok); confirm it stays gitignored.")


def main() -> int:
    check()
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    payload = {"blocking": BLOCKING, "warnings": WARN, "ok": not BLOCKING}
    REPORT.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload, indent=2))
    if BLOCKING:
        print("SECURITY AUDIT FAILED")
        return 1
    print("SECURITY AUDIT PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
