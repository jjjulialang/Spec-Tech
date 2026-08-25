# Live proof (practice set)

Official practice PDFs + OpenRouter. Local grade vs `examples/practice-dataset/manifest.json`.

```
local grade vs manifest: matched=2/2 reported=2 P=1.00 R=1.00 F1=1.00
```

Written `output.json` (`tests/live_practice_output.json`):

```json
{
  "errors": [
    {
      "document": "schedule.pdf",
      "category": "cross-document-conflict",
      "description": "The fire rating for door D-202 is listed as 45 min in schedule.pdf, but spec.pdf requires a 90-minute fire rating for mechanical room doors.",
      "location": "page 1, D-202"
    },
    {
      "document": "schedule.pdf",
      "category": "unit-error",
      "description": "The flow rate for lavatory L-1 is listed as 5.0 gpm in schedule.pdf, but spec.pdf requires 0.5 gpm aerators maximum for lavatory faucets, indicating a 10x discrepancy.",
      "location": "page 1, L-1"
    }
  ]
}
```

Harder local pack (not submitted): 104 docs, 54 planted errors, F1 0.89 (52/54).

This branch is the grader entry: `run.sh` → `find_errors.py`.
