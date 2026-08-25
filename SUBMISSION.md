# Spec-Tech submission guide

This repository's participant entry consists of `run.sh` and `agent.py` at
the repository root. The grader executes `run.sh`; the rest of the original
ACELAB repository is reference and organizer infrastructure.

## Run against the practice dataset

From the repository root, enter the OpenRouter key without saving it in shell
history or a tracked file:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install pypdf

read -s OPENROUTER_API_KEY
export OPENROUTER_API_KEY
export DATASET_DIR="$PWD/examples/practice-dataset"
export OUTPUT_PATH="$PWD/output.json"
export AEC_MODEL="google/gemini-3.1-pro-preview"
bash run.sh
python3 -m json.tool "$OUTPUT_PATH"
unset OPENROUTER_API_KEY
```

The expected practice result contains two findings in `schedule.pdf`:

- `D-202`: 45-minute rating conflicts with the 90-minute mechanical-room requirement.
- `L-1`: 5.0 gpm conflicts with the required 0.5 gpm maximum.

Do not copy those answers into the agent. They are listed here only so a local
practice run can be evaluated. Production code enumerates the runtime PDFs and
never reads `manifest.json`.

Optional model overrides:

```bash
export AEC_MODEL="google/gemini-3.1-pro-preview"
export AEC_VERIFY_MODEL="$AEC_MODEL"
export AEC_FALLBACK_MODEL="google/gemini-2.5-pro"
```

## Run local checks

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile agent.py tests/test_agent.py
bash -n run.sh
```

To spend two real OpenRouter calls and validate the complete Gemini path:

```bash
export RUN_LIVE_OPENROUTER_TEST=1
python3 -m unittest discover -s tests -p 'test_live_openrouter.py' -v
unset RUN_LIVE_OPENROUTER_TEST
```

## Submit to the grader

After the pull request is merged, submit:

```text
jjjulialang/Spec-Tech@main
```

Before merge, the grader also accepts the feature branch explicitly:

```text
jjjulialang/Spec-Tech@codex/error-catching-agent
```

The grader downloads that ref, requires executable logic at root `run.sh`,
provides the environment variables below, and runs the script in an isolated
sandbox:

- `DATASET_DIR`: read-only directory containing only the run's PDF documents.
- `OUTPUT_PATH`: required destination for the JSON result.
- `OPENROUTER_API_KEY`: sandbox credential for OpenRouter.

The sandbox allows 600 seconds and at most 300 OpenRouter calls. Internet is
deny-by-default except OpenRouter and the permitted Python/Node package hosts.
The answer-key manifest is never copied into the sandbox. The test set returns
its score immediately; the one final run uses a hidden dataset and seals its
score until the event reveal.
