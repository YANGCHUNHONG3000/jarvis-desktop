# Public snapshot checks

## October 23, 2026 update

This update contains 121 production/launcher Python files and 173 test files.
It keeps the public repository's own history and does not import the private
development repository history, credentials, runtime data, audio or captures.

- All 294 selected Python files parsed successfully. Static local imports and
  sibling test imports were checked for completeness; inspected third-party
  imports are covered by the existing dependency list.
- Independent source review found no release blocker in the selected changes.
  This is a scoped review, not a comprehensive security audit.
- Public-directory test collection succeeded: **4,400 tests collected**. This
  is a discovery/import check, not 4,400 passing tests.
- Targeted offline public-directory validation:

  ```powershell
  python -m pytest tests/test_model_budget.py tests/test_budgeted_model.py tests/test_task_diagnostics.py tests/test_capability_router.py tests/test_adapter_development.py -q --tb=short
  ```

  Result: **91 passed, 1 skipped**. Some checks across the larger suite require
  private captured pages or Office evidence that is intentionally not included;
  they skip when that evidence is absent. No private fixtures were copied to
  make these checks pass.

The update covers bounded adapter development, supported complex document
sources and edits, project-scoped attribution, retained capture import and
recovery, and request-sized model reservations with durable usage anomaly
accounting. The adapter lifecycle supports reviewed implementations; it does
not establish arbitrary autonomous code generation as a working capability.

This update did not repeat native browser/Office acceptance, read a private
inbox, or make paid model calls. Fresh-machine setup remains unverified. The
latest retained five-source formula-comparison task has not completed final
semantic review and Word/Excel delivery; this release does not claim otherwise.
The original release checks below are historical, not new test results.

## Initial public snapshot

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
