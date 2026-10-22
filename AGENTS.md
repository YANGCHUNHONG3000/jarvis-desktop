# Contributor instructions

Read README.md and docs/QUICKSTART.md before changing behavior. This repository
contains the independent desktop agent source snapshot; voice runtime and private
development history are outside this release.

- Keep work scoped. Locate symbols and read relevant slices instead of whole files.
- Preserve bounded actions, source/receipt integrity, human login handoffs and the
  refusal to replay effects whose outcome is unknown.
- Cloud calls require explicit local permission and the shared budget ledger.
  Never bypass unresolved reservations or commit credentials/runtime artifacts.
- Prefer existing adapters. New capabilities need a clear effect contract,
  targeted offline checks and separate real-application acceptance.
- Do not label fixture tests as real browser/Office acceptance. Do not transmit
  private test documents, email or credentials in issues, PRs or CI logs.
- Test only the affected scope; keep tool output short. Never start paid model
  calls, native UI actions or scheduled work without the operator's authorization.
