#!/bin/bash
# Entrypoint used by the Acelab grader. The documented runtime already includes
# pypdf, so avoid spending any of the 10-minute run limit on package setup.
set -u

python3 agent.py
