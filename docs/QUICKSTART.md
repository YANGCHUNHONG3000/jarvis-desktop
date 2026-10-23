# Quickstart (Windows)

## Prerequisites

- Windows 10 or 11 and Python 3.12.
- Microsoft Edge.
- WPS Office with Writer and Spreadsheets, for native document workflows.

A clean-machine install has not been verified. These commands describe the current project environment and may need adjustment for another machine.

## Create an environment

From the project root in PowerShell:

```powershell
py -3.12 -m venv .venv-desktop
.\.venv-desktop\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements-desktop.txt
```

For fixture tests only, install the pinned test runner separately:

```powershell
python -m pip install pytest==8.4.2
python -m pytest -q tests/test_model_budget.py
```

The test command is a focused fixture-driven example; it is not an end-to-end acceptance test.

## Launchers

Run a launcher from the project root, for example:

```powershell
python .\run_jarvis_assistant.py
python .\run_jarvis_office.py --help
python .\run_jarvis_research.py --help
```

The release contains these ten launchers: `run_jarvis_assistant.py`, `run_jarvis_computer.py`, `run_jarvis_desktop.py`, `run_jarvis_mail_export.py`, `run_jarvis_office.py`, `run_jarvis_research.py`, `run_jarvis_resume.py`, `run_jarvis_skill_resume.py`, `run_jarvis_skills.py`, and `run_jarvis_task.py`. Each entry point has its own supported inputs; inspect its help or source before use. Do not assume every launcher supports arbitrary apps or sites.

## Optional cloud model access

Cloud model calls require both a local API key and an explicit local permission file. Never commit either file. Create `DEEPSEEK_API_KEY.txt` in the project root using a secure local method. The permission file path is `data/agent_service/model-permission.json`; only an owner who understands that selected task content may be sent to the configured provider should create it. Its required values are:

```json
{
  "authorized": true,
  "daily_limit_cny": "5.00",
  "timezone": "America/New_York"
}
```

The service applies both a New York calendar-day and rolling-24-hour cap of CNY 5. Unresolved reservations count against the cap. Do not remove or edit budget records to make more spending available.

The current text-only client reserves against the serialized request size and
the bounded output, not the full model context. It validates provider pricing
and model limits before a paid request. If credible returned usage exceeds the
request bound, it records that usage and persistently stops further paid calls.
Keep the ledger for investigation; replacing it is not a recovery procedure.

## Data and recovery

Run records and documents are stored locally under data directories. Review those outputs before sharing them. A workflow marked `unknown` means an external effect may have occurred without a verified receipt; do not manually trigger a replay unless you have independently established the actual state. Sign-in and CAPTCHA are always completed manually.

The desktop service is separate from the voice runtime and does not require OpenClaw. Voice features and model weights are not included in this public desktop release.
