# Contributing

Thanks for helping improve Jarvis Desktop Agent. Keep contributions within the documented Windows desktop scope and describe what was actually verified.

## Before opening a change

- Explain the user-visible behavior, limits, and any external side effects.
- Do not include API keys, credentials, personal data, private history, run databases, generated user documents, or machine-specific paths.
- Do not claim a workflow is supported based only on a mock or fixture. Identify fixture-driven checks separately from real Windows/WPS/Edge verification.
- Keep unknown external outcomes terminal for that action; do not automatically replay uncertain clicks, submissions, or document writes.

## Development checks

Use Python 3.12. Install the desktop requirements and, for fixture tests, `pytest==8.4.2` as a development-only dependency. A focused example is:

```powershell
python -m pytest -q tests/test_model_budget.py
```

Check the test file names in the checkout before choosing a test command. This example is fixture-driven and does not verify a fresh Windows installation or real external service behavior. Do not submit private run data as test fixtures.

## Pull requests

Describe the change, scope, relevant checks and their results, and known limitations. For changes that touch browser or Office effects, include the evidence needed to distinguish verified success from an unknown result. Keep documentation and public claims aligned with the tested capability.