# Initial public snapshot checks

This is a source-only desktop release prepared on Windows with Python 3.12.
No claim of a clean-machine installation or full Windows automation is made.

Release preparation included:

- Static Python syntax checks for 90 production/launcher files and 122 test files.
- An independent review of the selected code for credentials, personal data,
  local module dependencies and third-party source copies. Pattern scans are not
  a guarantee that every possible security issue has been found.
- Targeted checks from the public snapshot:

  ```powershell
  python -m pytest tests/test_model_budget.py tests/test_document_table_skill.py tests/test_document_numeric_skill.py tests/test_comparison_crosssite_discovery.py -q
  ```

  Result: **93 passed, 1 skipped**. The skipped check requires a private captured
  page fixture that is intentionally not distributed. No cloud-model calls,
  private inbox reads or native Office actions were performed by this test group.

Existing development-machine browser/Office acceptance informed the capability
description. Private screenshots, documents, mail, captures, budgets and credentials
are not included. This snapshot does not copy the previous repository history.

Next release priorities: fresh-machine onboarding, broader compatibility checks,
and exposing the verified cross-site follow-up entry in the main GUI.
