"""Download similar public AEC spec-vs-drawing datasets (AEC-Bench)."""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "similar-datasets"
MAX_BYTES = 45 * 1024 * 1024

TASKS = [
    {
        "name": "uccs-hollow-metal-doors-hard",
        "files": {
            "spec.pdf": "https://nomic-public-data.com/data/aec-bench-v1/spec-drawing-sync/uccs-shared/spec.pdf",
            "drawings.pdf": "https://nomic-public-data.com/data/aec-bench-v1/spec-drawing-sync/uccs-shared/drawings.pdf",
        },
        "note": "Same UCCS project as the official pack: spec vs drawings, hollow-metal door conflicts.",
    },
    {
        "name": "nmacon-hollow-metal-doors-medium",
        "files": {
            "spec.pdf": "https://nomic-public-data.com/data/aec-bench-v1/spec-drawing-sync/nmacon-shared/spec.pdf",
            "drawings.pdf": "https://nomic-public-data.com/data/aec-bench-v1/spec-drawing-sync/nmacon-shared/drawings.pdf",
        },
        "note": "Independent project, same task family: fire ratings / door spec vs drawings.",
    },
]


def download(url: str, dest: Path) -> str:
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "acelab-hackathon-local-dev"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = resp.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        dest.write_bytes(data[:MAX_BYTES])
        return f"truncated to {MAX_BYTES} bytes"
    dest.write_bytes(data)
    return f"{len(data)} bytes"


def main() -> None:
    report = []
    OUT.mkdir(parents=True, exist_ok=True)
    for task in TASKS:
        folder = OUT / task["name"]
        folder.mkdir(parents=True, exist_ok=True)
        row = {"name": task["name"], "note": task["note"], "files": {}}
        for name, url in task["files"].items():
            try:
                row["files"][name] = download(url, folder / name)
                print(f"downloaded {task['name']}/{name}: {row['files'][name]}")
            except Exception as exc:  # noqa: BLE001
                row["files"][name] = f"FAILED: {type(exc).__name__}"
                print(f"failed {task['name']}/{name}: {exc}")
        (folder / "README.txt").write_text(task["note"] + "\n", encoding="utf-8")
        report.append(row)
    (OUT / "index.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("similar datasets index written")


if __name__ == "__main__":
    main()
