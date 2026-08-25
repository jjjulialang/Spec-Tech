# Synthetic hard dataset

This deterministic fixture is deliberately more adversarial than the two-error
practice set. `build_fixture.py` creates six small, valid PDFs at test time, so
the repository does not need to carry generated binaries.

The answer key has eight findings across all four supported categories. The
documents also contain harmless distractors:

- equivalent units (`2 in` / `50.8 mm`, `55 F` / `12.8 C`);
- equivalent ratings (`1 hour` / `60 min`);
- context-qualified, similar door tags;
- nearby equipment with different voltages;
- prompt-injection-like consultant text embedded in a drawing.

The tests never call OpenRouter. They exercise PDF enumeration and extraction,
the two-pass orchestration with deterministic model responses, output cleanup,
one-to-one grader-style scoring, and rejection of hallucinated or invalid
findings.
