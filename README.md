# Jarvis Desktop Agent

[中文说明](README.zh-CN.md) · [Quickstart](docs/QUICKSTART.md) · [Contributing](CONTRIBUTING.md)

Jarvis Desktop Agent is a Windows desktop automation and research service. This public release covers the independent `agent_service` and ten `run_jarvis_*.py` launchers. It does not require the voice runtime or OpenClaw.

## What it can do

- Search the web, inspect and summarize pages, and perform bounded browser input, clicks, and reads on supported sites. Sign-in and CAPTCHA steps are manual.
- Create, edit, verify, and hand off native WPS Writer and Spreadsheets documents through Windows COM automation.
- Preserve durable receipts for supported workflows. When an external action may have happened but cannot be verified, the result is marked unknown and that action is not replayed automatically.
- Compare claims with quoted passages and publisher evidence. This is evidence checking, not a guarantee that a claim is true.

The v4 follow-up backend exists, but its entry point is not yet connected to the main GUI. This is not a general-purpose autonomous computer agent: only implemented workflows and supported sites/apps are in scope.

## Requirements and setup

Windows 10/11, Python 3.12, Microsoft Edge, and WPS Office with Writer and Spreadsheets installed. See [Quickstart](docs/QUICKSTART.md) for setup and launch examples.

Fresh-machine installation has not been verified. Word and Excel COM compatibility across different Office installations has not been accepted; this release documents WPS. The desktop release does not include voice, local/cloud model weights, user data, credentials, or private history.

## Cloud model and spending controls

Cloud model calls are disabled by default unless a local owner-created permission file allows them. When enabled, the required permission values are `authorized: true`, `daily_limit_cny: "5.00"`, and `timezone: "America/New_York"`. This authorizes sending the selected request content to the configured model provider; review what a task may include before enabling it. The local API key belongs in the ignored `DEEPSEEK_API_KEY.txt` file and must never be committed.

The budget ledger enforces both a New York calendar-day cap and a rolling 24-hour cap of CNY 5. Unresolved reservations remain held against both limits. The cap is a spending ceiling, not a promise of free use or an estimate of remaining balance.

## License and status

The original project is released under the MIT License. Copyright (c) 2026 YANGCHUNHONG3000 and contributors. See [Third-party notices](THIRD_PARTY.md) for dependency information. This is an early public release; no external adoption or fresh-install claim is made.
