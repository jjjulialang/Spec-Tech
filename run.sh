#!/bin/bash
# Entry point. The grader executes this file at the repo root.
#
# Environment provided by the runner:
#   DATASET_DIR         - directory of PDF documents (enumerate; do not hardcode names)
#   OUTPUT_PATH         - where to write output.json (required schema)
#   OPENROUTER_API_KEY  - credential for https://openrouter.ai/api/v1
#
# Network: pip (PyPI) then OpenRouter only. Finish within 10 minutes.
set -u
pip3 install --disable-pip-version-check --quiet pymupdf pypdf || pip3 install --disable-pip-version-check --quiet pypdf || true
python3 find_errors.py
