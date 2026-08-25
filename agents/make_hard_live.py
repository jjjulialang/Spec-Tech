"""Build a ~100-file live test: 50 schedule/spec pairs + practice PDFs + a fat CSV."""

from __future__ import annotations

import csv
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "live-hard"
PRACTICE = ROOT / "practice-dataset"


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    errors = []

    # Official practice PDFs (2 known errors)
    for name in ("schedule.pdf", "spec.pdf"):
        shutil.copy2(PRACTICE / name, OUT / name)
    errors.extend(
        json.loads((PRACTICE / "manifest.json").read_text(encoding="utf-8"))["errors"]
    )

    for i in range(50):
        n = 400 + i
        kind = i % 5
        sched = OUT / f"pair_{i:02d}_schedule.txt"
        spec = OUT / f"pair_{i:02d}_spec.txt"
        if kind == 0:
            sched.write_text(
                f"Door Schedule\nMark D-{n} Location Mechanical 101 Type HM Fire Rating 45 min Hardware HW-5\n",
                encoding="utf-8",
            )
            spec.write_text(
                f"Section 08 11 00 Doors\nDoor D-{n} at mechanical rooms shall carry a 90-minute fire rating.\n",
                encoding="utf-8",
            )
            errors.append(
                {
                    "id": f"H{i:02d}",
                    "document": sched.name,
                    "category": "cross-document-conflict",
                    "keywords": [f"D-{n}", "45 min"],
                    "description": f"D-{n} scheduled 45 min; spec requires 90-minute mechanical door.",
                }
            )
        elif kind == 1:
            sched.write_text(
                f"Plumbing Fixture Schedule\nMark L-{n} Fixture Lavatory Flow 5.0 gpm Notes wall hung\n",
                encoding="utf-8",
            )
            spec.write_text(
                f"Section 22 40 00 Plumbing Fixtures\nLavatory L-{n} faucets: 0.5 gpm aerators maximum per energy code.\n",
                encoding="utf-8",
            )
            errors.append(
                {
                    "id": f"H{i:02d}",
                    "document": sched.name,
                    "category": "unit-error",
                    "keywords": [f"L-{n}", "5.0 gpm"],
                    "description": f"L-{n} listed 5.0 gpm; spec requires 0.5 gpm.",
                }
            )
        elif kind == 2:
            sched.write_text(
                f"Plumbing Plan notes\nSlope sanitary waste at 1\" per foot (1 percent) unless otherwise noted.\nTag CO-{n}\n",
                encoding="utf-8",
            )
            spec.write_text(
                f"IPC 704.1\nCO-{n}: Slope sanitary piping 3\" or larger at 1/8\" per foot (1 percent).\n",
                encoding="utf-8",
            )
            errors.append(
                {
                    "id": f"H{i:02d}",
                    "document": sched.name,
                    "category": "unit-error",
                    "keywords": [f"CO-{n}", '1" per foot'],
                    "description": f"CO-{n} slope 1 in/ft vs spec 1/8 in/ft (factor of 8).",
                }
            )
        elif kind == 3:
            sched.write_text(
                f"Drawing plumbing plan\nFixture FD-{n} floor drain at Mechanical 101. Trap primer required.\n",
                encoding="utf-8",
            )
            spec.write_text(
                "Plumbing Fixture Schedule\nWC-1 Water Closet 1.28 gpf\nL-1 Lavatory 0.5 gpm\nSS-1 Service Sink mop basin\n",
                encoding="utf-8",
            )
            errors.append(
                {
                    "id": f"H{i:02d}",
                    "document": sched.name,
                    "category": "missing-item",
                    "keywords": [f"FD-{n}"],
                    "description": f"FD-{n} appears on the drawing but is missing from the fixture schedule.",
                }
            )
        else:
            sched.write_text(
                f"Civil grading note\nRamp R-{n} running slope is 1:8 (12.5 percent).\n",
                encoding="utf-8",
            )
            spec.write_text(
                f"ADA 405.2\nRamp R-{n} running slope shall not exceed 1:12 (8.33 percent).\n",
                encoding="utf-8",
            )
            errors.append(
                {
                    "id": f"H{i:02d}",
                    "document": sched.name,
                    "category": "code-violation",
                    "keywords": [f"R-{n}", "1:8"],
                    "description": f"Ramp R-{n} is 1:8; ADA 405.2 requires 1:12 maximum.",
                }
            )

    csv_path = OUT / "fixture_takeoff.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["tag", "fixture", "flow_gpm", "notes"])
        for i in range(30000):
            tag = f"FX-{i}"
            flow = 5.0 if i in {1000, 20000} else 0.5
            w.writerow([tag, "lavatory", flow, "row"])
    spec_csv = OUT / "fixture_spec.txt"
    spec_csv.write_text(
        "Section 22 40 00\nFX-1000 lavatory faucet maximum 0.5 gpm.\nFX-20000 lavatory faucet maximum 0.5 gpm.\n",
        encoding="utf-8",
    )
    errors.append(
        {
            "id": "CSV1",
            "document": "fixture_takeoff.csv",
            "category": "unit-error",
            "keywords": ["FX-1000", "5.0"],
            "description": "FX-1000 listed 5.0 gpm; spec requires 0.5 gpm.",
        }
    )
    errors.append(
        {
            "id": "CSV2",
            "document": "fixture_takeoff.csv",
            "category": "unit-error",
            "keywords": ["FX-20000", "5.0"],
            "description": "FX-20000 listed 5.0 gpm; spec requires 0.5 gpm.",
        }
    )

    (OUT / "manifest.json").write_text(json.dumps({"errors": errors}, indent=2), encoding="utf-8")
    files = [p.name for p in OUT.iterdir() if p.name not in {"manifest.json"}]
    print(f"live-hard: {len(files)} documents, {len(errors)} answer-key errors -> {OUT}")


if __name__ == "__main__":
    main()
