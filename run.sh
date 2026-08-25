#!/bin/bash
# Entrypoint used by the Acelab grader. The documented runtime already includes
# pypdf, so avoid spending any of the 10-minute run limit on package setup.
set -u

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$SCRIPT_DIR/agent.py"
