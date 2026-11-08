# Jarvis Desktop Agent

[中文说明](README.zh-CN.md) · [Quickstart](docs/QUICKSTART.md) · [Contributing](CONTRIBUTING.md)

Jarvis Desktop Agent is a Windows desktop automation and research service. This public release covers the independent `agent_service` and ten `run_jarvis_*.py` launchers. It does not require the voice runtime or OpenClaw.

## What it can do

- Search and inspect supported websites, check claims against quoted evidence, and create source-linked reports. Sign-in and CAPTCHA require local manual input.
- Create, edit, verify and hand off supported WPS Word/Excel documents, including bounded table and multi-sheet workflows. Natural-language edits use observed locations, a reviewed plan, preserved originals and native readback.
- Carry verified Office outputs into the next task; supported recovery rechecks completed steps and runs only the remaining work.
- Split a supported Office-plus-Windows instruction into a bounded 2–4-step checklist. Review the split and each step before execution; verified Office output batches carry forward, and supported recovery continues unfinished steps.
- Observe supported Windows windows and controls, propose bounded goals, and select a compatible native, Winapp or Windows-MCP executor. Receipts record the selected route; an unknown external effect stops execution without automatic replay.
- Use optional selected-window image assistance for reviewed candidates. Image assistance requires explicit opt-in; it does not authorize arbitrary screen coordinates.
- Match tasks to reviewed capabilities and manage adapter drafts through offline checks, review, bounded trials and activation.

These implemented workflows are bounded by their supported source, application and evidence contracts. General desktop autonomy, arbitrary generated-code execution, arbitrary document layouts and a complete voice-integrated assistant remain outside this desktop release. Missing or changed evidence can require clarification or stop execution.

Office execution also requires the existing reviewed adapter qualification or bounded trial allowance. A completed demonstration does not grant unlimited activation; an exhausted allowance stops new execution until the qualification/activation process is completed.

The current update adds guarded Windows providers, natural-language Office editing and verified continuation. See [release checks and limits](docs/RELEASE_CHECKS.md). Development-machine acceptance and public-snapshot checks are recorded separately; private evidence fixtures are absent.

## Core creators

- **[YANGCHUNHONG3000](https://github.com/YANGCHUNHONG3000)** — project initiator, product direction and maintainer.
- **ChatGPT / Codex (AI development collaborator)** — contributed to architecture, implementation, bug fixes, testing, code review and technical documentation.

This credit records human–AI collaboration. Final project and release decisions remain with the human maintainer.

## Requirements and setup

Windows 10/11, Python 3.12, Microsoft Edge, and WPS Office with Writer and Spreadsheets installed. See [Quickstart](docs/QUICKSTART.md) for setup and launch examples.

Fresh-machine installation has not been verified. Word and Excel COM compatibility across different Office installations has not been accepted; this release documents WPS. The desktop release does not include voice, local/cloud model weights, user data, credentials, or private history.

## Cloud model and spending controls

Cloud calls require an owner-created local permission file and a local API key. Supported DeepSeek and OpenAI paths share one persistent usage/cost ledger. Selected task text, and an explicitly enabled window image where applicable, may be sent to the configured provider.

Local authorization supports `limited` mode (CNY 5 for both the New York calendar day and rolling 24 hours) or explicitly authorized `unlimited` mode (no local amount cap). Unlimited mode does not provide provider credits or remove platform quotas. Accounting failures still stop calls, and unknown reservations remain held. Do not replace the ledger or clear unresolved holds to bypass a stop.

Keys and permission files must stay local and uncommitted. See [Quickstart](docs/QUICKSTART.md) for the configuration contract. Cost estimates are conservative local accounting, not a provider invoice.

## License and status

The original project is released under the MIT License. Copyright (c) 2026 YANGCHUNHONG3000 and contributors. See [Third-party notices](THIRD_PARTY.md) for dependency information. This is an early public release; no external adoption or fresh-install claim is made.
