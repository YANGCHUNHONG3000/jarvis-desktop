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

### Bounded Office-plus-Windows checklist

In the main GUI, select verified Office sources or the current output batch,
enter the complete mixed instruction, and choose **拆分混合任务（云端）…**.
The proposed checklist contains 2–4 steps using both supported Office edits and
observed Windows targets. Review the split, then inspect and approve each step's
actual preview. The split does not approve future window actions automatically.

Use **找回混合任务…** to inspect a retained checklist after restart. Recovery
rechecks completed evidence, carries the verified Office batch forward and
continues unfinished work. An unknown effect or changed source can stop recovery;
it does not justify repeating the earlier action. This is a bounded workflow,
not a planner for arbitrary applications or unlimited task chains.

Office steps retain their adapter qualification and bounded-trial gates. This
release does not ship a reusable local approval or an unlimited execution grant.
When a trial allowance is exhausted, complete the reviewed qualification or
activation process before requesting another Office execution; changing the
candidate identity or counters is not a supported recovery method.

### Continuing with verified Office outputs

In the main GUI, use **历史任务 → 整批用于能力模式** to select one inspected
task's 2–4 Word/Excel outputs. The source label identifies the selected task;
**改用上一任务成果** explicitly switches back to the latest outputs.

In **连续任务 → 根据任务匹配技能／适配器**, enter the complete task and use
**按任务规划（云端）** for a metadata-only route proposal. This requires the same
local model permission and budget as other cloud calls. Check the selected
sources and full plan before approving execution. Ambiguous or unsupported joint
requests may require clarification; no new code is executed automatically.
Completed supported outputs can be carried into the next task without browsing
for their paths. Do not expect arbitrary document layouts or semantic merging.

Run a launcher from the project root, for example:

```powershell
python .\run_jarvis_assistant.py
python .\run_jarvis_office.py --help
python .\run_jarvis_research.py --help
```

The release contains these ten launchers: `run_jarvis_assistant.py`, `run_jarvis_computer.py`, `run_jarvis_desktop.py`, `run_jarvis_mail_export.py`, `run_jarvis_office.py`, `run_jarvis_research.py`, `run_jarvis_resume.py`, `run_jarvis_skill_resume.py`, `run_jarvis_skills.py`, and `run_jarvis_task.py`. Each entry point has its own supported inputs; inspect its help or source before use. Do not assume every launcher supports arbitrary apps or sites.

## Optional cloud model access

Calls require a local key and `data/agent_service/model-permission.json`. Neither file belongs in Git. Selected request content may be sent to the configured provider.

A limited-mode permission file is:

```json
{
  "authorized": true,
  "budget_mode": "limited",
  "daily_limit_cny": "5.00",
  "rolling_limit_cny": "5.00",
  "timezone": "America/New_York"
}
```

This defaults to the supported DeepSeek profile and local ignored `DEEPSEEK_API_KEY.txt`. For OpenAI, set `provider` to `openai`, `model` to the pinned `MODEL` in `agent_service/openai_profile.py`, and `key_path` to an absolute local key-file path. The model is pinned and pricing is checked before requests; arbitrary model names are rejected. No key or provider credit is supplied.

An owner who explicitly authorizes removal of the local amount caps can set `budget_mode` to `unlimited` and both `daily_limit_cny` and `rolling_limit_cny` to JSON `null`. Keep `authorized` and `timezone` as above. All providers continue to share the persistent ledger. Provider quotas, unknown holds, usage validation and stop-on-accounting-failure behavior remain in force.

Do not delete, replace or edit budget records to bypass a stop. Conservative local accounting is not a precise provider invoice.

## Optional Windows runtimes

Core native workflows and optional executors have separate requirements. Missing or changed runtime components stop the affected workflow; the service does not automatically download a replacement.

- Host image annotation uses Pillow from `requirements-desktop.txt`.
- CPU OCR uses a separate Python 3.12 environment at `data/vision_runtime` and `requirements-windows-vision.txt`; its dependencies must not overwrite the host environment. The alternative Windows OCR fallback requires separately available WinRT projection packages and language support; these are not supplied by the default host requirements. Missing OCR stops the affected visual operation.
- Windows-MCP uses a separate Python 3.14.6 environment at `data/tool_runtime/windows-mcp/b69ec872/venv`, with `requirements-windows-mcp.txt`. The pinned upstream commit, source hashes and SDK versions are checked. Upstream Type and unrestricted tool access are disabled.
- Winapp integration expects a separately obtained winappCli 0.7.1 executable at `data/tool_runtime/winapp/0.7.1/bin/winapp.exe` and the matching DLL hashes defined in `agent_service/windows_winapp.py`. This source release does not bundle the executable.

These are development-machine runtime contracts; a fresh-machine installation has not been accepted. Review [third-party notices](../THIRD_PARTY.md) before obtaining or redistributing dependencies.

## Data and recovery

Run records and documents are stored locally under data directories. Review those outputs before sharing them. A workflow marked `unknown` means an external effect may have occurred without a verified receipt; do not manually trigger a replay unless you have independently established the actual state. Sign-in and CAPTCHA are always completed manually.

The desktop service is separate from the voice runtime and does not require OpenClaw. Voice features and model weights are not included in this public desktop release.
