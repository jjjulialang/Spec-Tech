"""Local pipeline: fetch similar data, harden 100x, align, edge-case, security twice."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable


def run(script: str) -> int:
    print(f"\n=== {script} ===")
    proc = subprocess.run([PY, str(ROOT / "agents" / script)], cwd=str(ROOT))
    return proc.returncode


def main() -> int:
    fetch_rc = run("fetch_similar.py")
    harden_rc = run("harden_dataset.py")
    if harden_rc != 0:
        return harden_rc
    align_rc = run("align_rules.py")
    if align_rc != 0:
        return align_rc
    edge_rc = run("edge_cases.py")
    if edge_rc != 0:
        return edge_rc
    sec1 = run("security_audit.py")
    if sec1 != 0:
        return sec1
    sec2 = run("security_audit.py")
    if sec2 != 0:
        return sec2
    print("\nPIPELINE COMPLETE")
    print(f"fetch={fetch_rc} harden={harden_rc} align={align_rc} edge={edge_rc} security={sec1}/{sec2}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
