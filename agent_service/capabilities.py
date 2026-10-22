"""Static, read-only catalog of the independent desktop service capabilities.

This module is data-only: it does not import adapters, inspect the network,
activate code, or infer support from a search result. Evidence labels describe
the recorded scope and do not grant permission to run an adapter.
"""

from __future__ import annotations

from copy import deepcopy
import re
import unicodedata


_QUERY = re.compile(r"[^\x00-\x1f\x7f]{1,200}\Z")
_CAPABILITY_ID = re.compile(r"[a-z][a-z0-9_.-]{0,99}\Z")
_ALIASES = {
    "word表格第二列求和": ("office.document_table.numeric_sum_to_report",),
    "word表格数值求和": ("office.document_table.numeric_sum_to_report",),
    "docx表格数字列求和": ("office.document_table.numeric_sum_to_report",),
    "word表格转excel": ("office.document_table.extract_to_sheet",),
    "把word里的表格转到excel": ("office.document_table.extract_to_sheet",),
    "docx转xlsx": ("office.document_table.extract_to_sheet",),
}

_CATALOG = (
    {
        'id': 'computer.observed_agent.public_office',
        'kind': 'service',
        'title': '持续观察、决策与核验的浏览器办公 Agent',
        'description': '批准目标范围后，根据持久观察选择下一动作；读取失败换查询或来源，独立核验来源和办公成果。',
        'availability': 'implemented',
        'scope': '公开网页来源摘录与新建Excel/Word；支持字面词和来源数条件，有限恢复。尚不含任意窗口、登录网页或研究结论。',
        'inputs': ['reviewed objective and effect bounds', 'approved queries and literal source requirements'],
        'outputs': ['per-cycle observations, decisions and verification receipts',
                    'verified owned Office artifacts and next-task source'],
        'limits': ['2 queries; 4 distinct URLs; one read per URL and one write per output kind',
                   '12 actions; 360 seconds; 6 model calls per session including objective planning',
                   'single choices use no model; shared CNY5 daily and rolling ledger',
                   'unknown Office effects stop; no write replay or source expansion after writing'],
        'implementation': ['agent_service/computer_agent.py','agent_service/computer_agent_tools.py',
                           'agent_service/computer_goal_panel.py'],
        'evidence_status': 'offline_and_live_target',
        'offline_tests': ['tests/test_computer_agent.py','tests/test_computer_agent_tools.py',
                          'tests/test_computer_goal_panel.py'],
        'live_evidence': ['validation/artifacts/observed-agent-loop-20261017/acceptance.json'],
    },
    {
        'id': 'computer.approved_goal.topic_office',
        'kind': 'adapter',
        'title': '主题检索 → Excel → Word 连续接力',
        'description': '输入主题，检索最多两个公开候选并读取正文，生成来源表和摘录文档，自动选为下一任务来源。',
        'availability': 'implemented',
        'scope': '连续任务中的主题模板；可选一次云端理解指令，再本机批准。保存来源摘录，不提供综合研究结论或登录网站操作。',
        'inputs': ['public search topic or supported instruction', 'local approval of bounded plan'],
        'outputs': ['verified Excel and Word files', 'failed-candidate disclosures', 'next-task source reference'],
        'limits': ['at most 2 candidates; at least 1 actual body required',
                   'unread candidates explicitly marked; no invented content',
                   '1200 characters per source; 25-second search and 240-second goal limits',
                   'local template zero model calls; optional shared-budget planner',
                   'single-use claim; unknown effects stop without replay'],
        'implementation': ['agent_service/computer_search.py', 'agent_service/computer_goal_adapters.py',
                           'agent_service/computer_goal_panel.py'],
        'evidence_status': 'offline_and_live_target',
        'offline_tests': ['tests/test_computer_search_goals.py', 'tests/test_computer_goal_panel.py'],
        'live_evidence': ['validation/artifacts/computer-search-goals-20261017/acceptance.json'],
    },
    {
        'id': 'computer.approved_goal.web_office',
        'kind': 'adapter',
        'title': '公开网页 → Excel → Word 连续接力',
        'description': '一次预览批准后，读取公开网页、保存来源表，再读取实际 Excel 生成 Word；成果自动带入下一任务。',
        'availability': 'implemented',
        'scope': '连续任务中的本地网页办公模板；需要明确公开网址和标题，保存来源摘录，不含云端归纳或登录网站操作。',
        'inputs': ['one or two public URLs', 'title', 'local approval of bounded plan'],
        'outputs': ['verified Excel and Word files', 'history receipts and next-task source reference'],
        'limits': ['1200 characters per page in Office with explicit truncation note',
                   '240-second deadline and stop button; isolated new documents',
                   'single-use claim; unknown effects stop without replay', 'zero model calls'],
        'implementation': ['agent_service/computer_goal_adapters.py', 'agent_service/computer_goals.py',
                           'agent_service/computer_goal_panel.py', 'run_jarvis_assistant.py'],
        'evidence_status': 'offline_and_live_target',
        'offline_tests': ['tests/test_computer_goal_adapters.py', 'tests/test_computer_goal_panel.py'],
        'live_evidence': ['validation/artifacts/computer-adapters-20261017/acceptance.json'],
    },
    {
        'id': 'computer.approved_goal.sequence',
        'kind': 'service',
        'title': '一次批准后连续执行并核验电脑任务',
        'description': '组合计算、读取文字、新建记事本页和项目选择；前步真实结果自动传下一步。',
        'availability': 'implemented',
        'scope': '仅已绑定窗口和预览步骤；本机模板零模型调用，云端可规划但不能批准。笔记是未保存的新标签。',
        'inputs': ['user-selected windows', 'reviewed finite plan and local approval'],
        'outputs': ['verified outputs and per-step receipts', 'unsaved Notepad tab when requested'],
        'limits': ['8 operations, 64 native actions, 240 seconds', '120-second preview, single-use claim',
                   'fixed input epoch; unknown effects stop without replay', 'shared CNY 5 daily/rolling API budget'],
        'implementation': ['agent_service/computer_goals.py', 'agent_service/computer_goal_panel.py',
                           'agent_service/computer.py', 'agent_service/computer_host.py', 'agent_service/computer_uia.py'],
        'evidence_status': 'offline_and_live_target',
        'offline_tests': ['tests/test_computer_goals.py', 'tests/test_computer_goal_panel.py'],
        'live_evidence': ['validation/artifacts/computer-goals-20261017/acceptance.json'],
    },
    {
        'id': 'computer.selected_window.step',
        'kind': 'service',
        'title': '观察所选窗口并执行经预览的一步操作',
        'description': '读取真实 UIA 控件，预览、核对过期状态、单次执行并回读；可请求一次云端建议。',
        'availability': 'implemented',
        'scope': '计算器算术按钮、记事本文字与标签页、资源管理器选择与展开；未知应用仅观察和聚焦。逐步执行须本机确认，尚非任意任务自主操作。',
        'inputs': ['user-selected window', 'observed control ID and one literal action', 'optional explicit cloud proposal'],
        'outputs': ['local before/after snapshots and durable per-step outcome'],
        'limits': ['200 nodes, depth 12, 4000-character value', '120-second single-use snapshots',
                   '25-second native worker', 'six model proposals per session; shared CNY 5 daily/rolling budget'],
        'implementation': ['agent_service/computer.py', 'agent_service/computer_host.py',
                           'agent_service/computer_uia.py', 'agent_service/computer_panel.py'],
        'evidence_status': 'offline_and_live_target',
        'offline_tests': ['tests/test_computer_contracts.py', 'tests/test_computer_panel.py'],
        'live_evidence': ['validation/artifacts/computer-live-20261017/acceptance.json'],
    },
    {
        "id": "web.public_research_to_markdown",
        "kind": "adapter",
        "title": "公开网页检索并保存来源摘要",
        "description": "检索公开网页、读取有限来源并保存带来源的 Markdown 摘要。",
        "availability": "implemented",
        "scope": "只读取公开 HTTP(S) 页面；不登录、不处理内网地址、不执行页面指令。",
        "inputs": ["query: 1..500 characters", "max_sources: integer 1..3"],
        "outputs": ["task-owned .md report with source references"],
        "limits": ["search deadline 20 seconds", "workflow deadline 160 seconds",
                   "at most 3 source pages", "each HTML response at most 512 KiB"],
        "implementation": ["agent_service/research.py", "agent_service/browser.py",
                           "agent_service/worker.py"],
        "evidence_status": "offline_and_live_target",
        "offline_tests": ["tests/test_research_workflow.py", "tests/test_agent_browser.py"],
        "live_evidence": ["validation/artifacts/budgeted-agent-20261017-final/acceptance.json"],
    },
    {
        "id": "desktop.notepad.create_text_file",
        "kind": "adapter",
        "title": "在 Windows 记事本创建并回读新文档",
        "description": "通过受限桌面监督器创建新的文本文件并验证界面与磁盘回读。",
        "availability": "implemented",
        "scope": "只创建任务拥有的新文本文件；不打开或覆盖用户已有文档。",
        "inputs": ["text: 1..12000 characters"],
        "outputs": ["new .txt file with verified text"],
        "limits": ["one bounded write attempt", "Windows Notepad interaction required"],
        "implementation": ["agent_service/notepad.py", "run_jarvis_desktop.py"],
        "evidence_status": "offline_and_live_target",
        "offline_tests": ["tests/test_notepad_adapter.py", "tests/test_desktop_supervisor.py"],
        "live_evidence": ["validation/artifacts/desktop-recovery-20261017-cfb3330126b74cd2ae4d8176f7556b42/acceptance.json"],
    },
    {
        "id": "office.document.create",
        "kind": "adapter",
        "title": "创建新 Word 兼容文档",
        "description": "用固定 Office 文档规格创建并回读新的 DOCX。",
        "availability": "implemented",
        "scope": "只在任务目录创建新文件；不编辑、覆盖或删除已有用户文件。",
        "inputs": ["title <= 200 characters", "paragraphs: at most 40, each <= 3000 characters",
                   "optional table: 1..12 columns and at most 200 rows"],
        "outputs": ["verified .docx artifact with readback evidence"],
        "limits": ["document text <= 60000 characters", "one bounded supervised Office step"],
        "implementation": ["agent_service/office.py", "agent_service/office_host.py",
                           "agent_service/office_workflow.py"],
        "evidence_status": "offline_and_live_target",
        "offline_tests": ["tests/test_office_adapter.py", "tests/test_office_host.py",
                          "tests/test_office_workflow.py"],
        "live_evidence": ["validation/artifacts/office-resume-live-20261017/acceptance.json"],
    },
    {
        "id": "office.spreadsheet.create",
        "kind": "adapter",
        "title": "创建新 Excel 兼容表格",
        "description": "用固定表格规格创建并回读新的 XLSX；可核验指定列的总计。",
        "availability": "implemented",
        "scope": "只在任务目录创建新文件；不编辑、覆盖或删除已有用户文件。",
        "inputs": ["title <= 200 characters", "1..12 headers", "at most 200 data rows",
                   "cells are strings, booleans, null, or finite numbers"],
        "outputs": ["verified .xlsx artifact; selected totals are derived and read back"],
        "limits": ["numeric values limited to Excel's 15 significant digits", "no formulas"],
        "implementation": ["agent_service/office.py", "agent_service/office_host.py",
                           "agent_service/office_workflow.py"],
        "evidence_status": "offline_and_live_target",
        "offline_tests": ["tests/test_office_adapter.py", "tests/test_office_numeric_precision.py",
                          "tests/test_office_host.py"],
        "live_evidence": ["validation/artifacts/office-resume-live-20261017/acceptance.json"],
    },
    {
        "id": "office.document.from_verified_sheet",
        "kind": "adapter",
        "title": "根据已核验表格创建报告",
        "description": "读取本工作流较早表格步骤的实际已保存值和总计，再创建文档。",
        "availability": "implemented",
        "scope": "只接受同一工作流中先前完成且回执与文件哈希均有效的表格步骤。",
        "inputs": ["from: earlier spreadsheet step", "title and bounded report paragraphs"],
        "outputs": ["new .docx containing read-back spreadsheet values and verified totals"],
        "limits": ["source spreadsheet must be a prior step in the same workflow"],
        "implementation": ["agent_service/office_workflow.py", "agent_service/office.py"],
        "evidence_status": "offline_and_live_target",
        "offline_tests": ["tests/test_office_workflow.py", "tests/test_office_recovery_live_contract.py"],
        "live_evidence": ["validation/artifacts/office-resume-live-20261017/acceptance.json"],
    },
    {
        "id": "office.source.snapshot",
        "kind": "adapter",
        "title": "读取所选简单来源文件并制作任务副本",
        "description": "对所选 CSV、简单单表 XLSX 或简单 DOCX 建立稳定副本、哈希和结构化观察。",
        "availability": "implemented",
        "scope": "只读取用户显式选择的来源；制作任务拥有的副本，不改写原件。",
        "inputs": ["CSV, simple XLSX, or simple DOCX path"],
        "outputs": ["task-owned byte copy, SHA-256, and bounded table/document observation"],
        "limits": ["source <= 20 MB", "package uncompressed <= 40 MB", "at most 1000 entries",
                   "each package part <= 10 MB", "tables <= 12 columns and 200 data rows"],
        "implementation": ["agent_service/office_source.py", "agent_service/assistant.py"],
        "evidence_status": "offline_and_live_target",
        "offline_tests": ["tests/test_office_source.py", "tests/test_assistant_source.py"],
        "live_evidence": ["docs/PROJECT_STATE.md#16 (recorded selected-source WPS tasks)"],
    },
    {
        "id": "office.source.table_to_sheet",
        "kind": "adapter",
        "title": "从所选表格生成新的工作簿",
        "description": "按受限源列选择、数值汇总及用户明确要求的 set/add/append 生成新 XLSX。",
        "availability": "implemented",
        "scope": "只接受已哈希的 selected 表格观察；原来源只读，编辑写入新文件。",
        "inputs": ["source_office.spreadsheet_from_source schema", "zero-based unique source columns",
                   "optional sum columns", "at most 50 cell edits and 50 appended rows"],
        "outputs": ["new verified .xlsx and source-edit before/after audit"],
        "limits": ["at most 12 output columns", "at most 200 resulting rows",
                   "each source cell may be edited once", "numeric precision <= 15 significant digits"],
        "implementation": ["agent_service/office_source_plan.py", "agent_service/office_source.py",
                           "agent_service/office_workflow.py"],
        "evidence_status": "offline_and_live_target",
        "offline_tests": ["tests/test_office_source_plan.py", "tests/test_office_source_edits.py",
                          "tests/test_assistant_source.py"],
        "live_evidence": ["docs/PROJECT_STATE.md#16 (recorded selected-source WPS tasks)"],
    },
    {
        "id": "office.source.sheet_report",
        "kind": "adapter",
        "title": "根据来源表格创建报告",
        "description": "报告由工作流中已保存并回读的来源表格生成，不从模型计划接受编造的值。",
        "availability": "implemented",
        "scope": "只引用同工作流中先前生成的来源表格步骤。",
        "inputs": ["source_office.report_from_sheet schema", "from: earlier selected-source sheet"],
        "outputs": ["new .docx using actual sheet values and computed totals"],
        "limits": ["data rows plus optional totals row <= 200"],
        "implementation": ["agent_service/office_source_plan.py", "agent_service/office_workflow.py"],
        "evidence_status": "offline_and_live_target",
        "offline_tests": ["tests/test_office_source_plan.py", "tests/test_assistant_source.py"],
        "live_evidence": ["docs/PROJECT_STATE.md#16 (recorded selected-source WPS tasks)"],
    },
    {
        "id": "office.source.document_copy",
        "kind": "adapter",
        "title": "将所选 DOCX 内容复制到新文档",
        "description": "复制已提取的段落和表格内容到新格式化文档；不声称保留原版式。",
        "availability": "implemented",
        "scope": "只接受已哈希的简单 DOCX 观察；原件只读。",
        "inputs": ["source_office.document_from_source schema", "title"],
        "outputs": ["new verified .docx preserving extracted paragraphs and table values"],
        "limits": ["paragraphs <= 40, each <= 3000 characters", "table <= 12 columns and 200 rows",
                   "layout fidelity is not guaranteed"],
        "implementation": ["agent_service/office_source.py", "agent_service/office_source_plan.py",
                           "agent_service/office_workflow.py"],
        "evidence_status": "offline_and_live_target",
        "offline_tests": ["tests/test_office_source.py", "tests/test_office_source_plan.py"],
        "live_evidence": ["docs/PROJECT_STATE.md#16 (recorded selected-source WPS tasks)"],
    },
    {
        "id": "mail.qq.read_only",
        "kind": "adapter",
        "title": "QQ 邮箱只读列表与选中邮件预览",
        "description": "显式连接后读取最近标题，并按 UID/UIDVALIDITY 预览所选邮件；不修改邮箱。",
        "availability": "implemented_auth_required",
        "scope": "QQ IMAP INBOX readonly；不标已读、发送、移动、删除、保存或打开附件；整封 MIME 读取可能包含附件编码字节。用户已确认读取正常，开发代理未登录账户。",
        "inputs": ["QQ account and locally entered authorization code/app password"],
        "outputs": ["at most 20 message headers; selected bounded body text and attachment metadata"],
        "limits": ["MIME message <= 2 MB", "body <= 200000 characters", "at most 32 MIME parts",
                   "nesting depth <= 8", "bounded connection/read deadlines and cancellation"],
        "implementation": ["agent_service/mailbox.py", "agent_service/mailbox_panel.py"],
        "evidence_status": "offline_with_user_reported_live",
        "offline_tests": ["tests/test_mailbox.py", "tests/test_mailbox_panel.py"],
        "live_evidence": ["docs/PROJECT_STATE.md#16 (QQ reading confirmed by user)"],
    },
    {
        "id": "mail.gmail.read_only",
        "kind": "adapter",
        "title": "Gmail IMAP 只读列表与选中邮件预览",
        "description": "同一受限只读 IMAP 接口，使用用户本地输入的应用专用密码时需显式连接。",
        "availability": "implemented_auth_required",
        "scope": "Gmail IMAP INBOX readonly；代码存在不代表账户认证已验收。",
        "inputs": ["Gmail account and locally entered app-specific password"],
        "outputs": ["at most 20 message headers; selected bounded body text and attachment metadata"],
        "limits": ["MIME message <= 2 MB", "body <= 200000 characters", "at most 32 MIME parts",
                   "nesting depth <= 8", "bounded connection/read deadlines and cancellation"],
        "implementation": ["agent_service/mailbox.py", "agent_service/mailbox_panel.py"],
        "evidence_status": "offline_tests_only",
        "offline_tests": ["tests/test_mailbox.py", "tests/test_mailbox_panel.py"],
        "live_evidence": [],
    },
    {
        "id": "mail.qq.export_selected_to_word",
        "kind": "adapter",
        "title": "将已读 QQ 邮件保存为本机 Word 副本",
        "description": "从当前已读完整快照创建一个新的 DOCX；只保存正文和邮件/附件元数据。",
        "availability": "implemented",
        "scope": "仅接受 QQ 的 selected_full_message 快照；不接收邮箱凭据，不发送、不上传、不下载附件载荷。",
        "inputs": ["strict read snapshot with UID and UIDVALIDITY", "complete body coverage"],
        "outputs": ["one verified .docx task artifact and history record"],
        "limits": ["body <= 200000 characters before Office document limits", "Office document text <= 60000 characters",
                   "oversize/incomplete snapshots are rejected, not truncated"],
        "implementation": ["agent_service/mail_export.py", "run_jarvis_mail_export.py",
                           "agent_service/office_workflow.py"],
        "evidence_status": "offline_and_synthetic_live_target",
        "offline_tests": ["tests/test_mail_export.py", "tests/test_assistant_email.py"],
        "live_evidence": ["validation/artifacts/mail-office-live-20261017/acceptance.json",
                          "validation/artifacts/mail-office-live-20261017/live.json"],
    },
    {
        "id": "email.local_unsent_draft.create",
        "kind": "adapter",
        "title": "生成未发送的本地 .eml 草稿",
        "description": "从明确提供的主题、正文、收件人和前序 Office 附件步骤生成并验证本地邮件文件。",
        "availability": "implemented",
        "scope": "只创建本地 .eml；没有 SMTP、邮箱上传或发送能力。",
        "inputs": ["bounded subject/body", "explicit user-provided recipients only", "earlier workflow attachment steps"],
        "outputs": ["verified .eml with attachment metadata and byte hashes"],
        "limits": ["subject <= 200 characters", "body <= 20000 characters", "at most 10 recipients",
                   "at most 8 workflow steps", "attachment <= 20 MB each and <= 30 MB total"],
        "implementation": ["agent_service/email_draft.py", "agent_service/office_workflow.py"],
        "evidence_status": "offline_and_live_target",
        "offline_tests": ["tests/test_email_draft.py", "tests/test_office_email_workflow.py"],
        "live_evidence": ["validation/artifacts/office-resume-live-20261017/acceptance.json"],
    },
    {
        "id": "history.task.inspect",
        "kind": "service",
        "title": "只读检查历史任务及成果",
        "description": "列举有限任务记录，并在显式选择后重核工作流回执和成果哈希。",
        "availability": "implemented",
        "scope": "不执行、恢复或修改任务；元数据列表不等同于成果核验。",
        "inputs": ["task root under assistant task directory"],
        "outputs": ["task state, verified artifacts, and recovery eligibility metadata"],
        "limits": ["list at most 200 records per call", "GUI shows recent 30"],
        "implementation": ["agent_service/task_history.py", "agent_service/office_resume.py"],
        "evidence_status": "offline_and_live_target",
        "offline_tests": ["tests/test_task_history.py", "tests/test_history_resume_gui.py"],
        "live_evidence": ["validation/artifacts/office-resume-live-20261017/acceptance.json"],
    },
    {
        "id": "office.workflow.resume_verified_prefix",
        "kind": "service",
        "title": "显式继续剩余的已核验办公工作流",
        "description": "不重新规划或调用模型；仅跳过已有有效回执并运行下一缺失步骤。",
        "availability": "implemented",
        "scope": "仅适用于证据连续、旧成果哈希有效、无未知副作用的 Office 任务。",
        "inputs": ["task root", "fresh preview token", "unique attempt ID"],
        "outputs": ["separate durable continuation outcome; original result remains immutable"],
        "limits": ["at most 8 fixed steps", "at most 600 seconds per attempt",
                   "unknown intent/effect blocks continuation", "bounded cancellation"],
        "implementation": ["agent_service/office_resume.py", "agent_service/office_workflow.py",
                           "run_jarvis_resume.py"],
        "evidence_status": "offline_and_live_target",
        "offline_tests": ["tests/test_office_resume.py", "tests/test_office_recovery_live_contract.py"],
        "live_evidence": ["validation/artifacts/office-resume-live-20261017/acceptance.json"],
    },
    {
        "id": "office.artifact.use_as_source",
        "kind": "service",
        "title": "将已核验的 Office 成果作为新任务来源",
        "description": "从历史任务选择通过回执和 SHA-256 重核的 XLSX 或 DOCX，复制到新任务后提取有界内容。",
        "availability": "implemented",
        "scope": "只接受受管理历史任务中的已核验 Office 产物引用；不接受任意路径冒充产物，原文件保持不变。",
        "inputs": ["artifact reference with task/workflow/step, artifact SHA-256, receipt SHA-256, and preview token"],
        "outputs": ["new task-owned XLSX or DOCX copy, bounded extracted values, and provenance metadata"],
        "limits": ["XLSX/DOCX only", "source <= 20 MB", "package uncompressed <= 40 MB",
                   "at most 1000 package entries and 10 MB per part", "simple one-sheet/table layouts only"],
        "implementation": ["agent_service/artifact_source.py", "agent_service/office_resume.py",
                           "agent_service/office_source.py", "agent_service/assistant.py"],
        "evidence_status": "offline_and_live_target",
        "offline_tests": ["tests/test_artifact_source.py", "tests/test_assistant_source.py"],
        "live_evidence": ["validation/artifacts/artifact-skills-live-20261017/skills-live.json"],
    },
    {
        "id": "office.document_table.extract_to_sheet",
        "kind": "adapter",
        "title": "将已核验 DOCX 表格提取到新工作簿",
        "description": "从简单 DOCX 的唯一表格选择或重排列，原样保留所有表头、数据行和文本单元格；可继续生成 Word 报告。",
        "availability": "implemented",
        "scope": "只读取已核验文档快照中的唯一表格；正文段落和原版式不导出。禁止改值、删行、数值转换、公式及重新求和；源文件不变。",
        "inputs": ["用户选择的 Word 文档或已核验的 Word 成果", "可按要求选择并调整列顺序"],
        "outputs": ["新建 Excel 工作簿", "可选：根据工作簿实际内容生成新的 Word 报告"],
        "limits": ["仅支持一张简单表格，最多 12 列、200 行，表头不可为空",
                   "保留表头、单元格文字、空白内容和现有合计行", "不转换数值、不重新计算合计"],
        "implementation": ["agent_service/office_source.py", "agent_service/office_source_plan.py",
                           "agent_service/office_workflow.py", "agent_service/assistant.py",
                           "agent_service/office.py"],
        "evidence_status": "offline_and_live_target",
        "offline_tests": ["tests/test_document_table_skill.py", "tests/test_office_source_plan.py",
                          "tests/test_assistant_source.py"],
        "live_evidence": ["validation/artifacts/document-table-live-20261017/recipes-live.json"],
    },
    {
        "id": "office.document_table.numeric_sum_to_report",
        "kind": "adapter",
        "title": "Word 表格指定数字列转换求和并生成报告",
        "description": "从已核验 DOCX 的唯一简单表格保留全部明细行，把用户指定的严格数字文本列转换为数值，在新 Excel 求和，再根据核验结果生成 Word 报告。",
        "availability": "implemented",
        "scope": "仅对显式指定的数字列转换与求和；原文档只读，未转换列和所有明细行保留。已有合计行、含糊数字或单列表格拒绝。正文段落和原版式不导出。",
        "inputs": ["用户选择的 Word 文档或已核验的 Word 成果，含唯一简单表格",
                   "source_office.spreadsheet_from_document_table_numeric 七字段计划；数字列和求和列必须精确对应"],
        "outputs": ["新建并核验的 Excel 工作簿及 SUM", "可选：根据新工作簿回读值生成 Word 报告"],
        "limits": ["最多 12 列、199 明细行用于报告；至少两列且第一输出列不转换或求和",
                   "数字列仅接受严格 ASCII 十进制，拒绝空白、前导零、单位、公式、已有合计和精度不明确值",
                   "不删除行、不改未指定列、不添加模型提供的单元格值"],
        "implementation": ["agent_service/office_source.py", "agent_service/office_source_plan.py",
                           "agent_service/office_workflow.py", "agent_service/assistant.py",
                           "agent_service/office.py"],
        "evidence_status": "offline_and_live_target",
        "offline_tests": ["tests/test_document_numeric_skill.py", "tests/test_assistant_document_numeric.py"],
        "live_evidence": ["validation/artifacts/document-numeric-live-20261017/gates.json"],
    },
    {
        "id": "skills.local_office_recipes",
        "kind": "service",
        "title": "本地固定办公配方",
        "description": "复用受发布状态门控制的本地办公配方：Excel 复制、表格与报告、Word 副本、Word 单表原文提取或显式数字列转换求和。",
        "availability": "implemented",
        "scope": "仅支持固定 table_copy、table_report、document_copy、document_table_report 和 document_numeric_report 配方。table_copy 只复制 Excel。document_table_report 原文保留 DOCX 唯一表格，不转换或重算；document_numeric_report 保留全部原列和明细行，只转换并求和指定数字列，再生成报告，不导出正文段落。自然语言任务先动态核验候选与完整任务意图；目录命中不授权执行。激活需离线契约检查、维护者签核和真实 WPS 成果门禁；成功激活最多运行 10 次，执行失败或未知副作用禁用该版本，作用前输入失败不扣次数。已有实机证据仅代表其记录的历史实现哈希；五个配方已有真实 WPS 验收，当前候选须动态查询 status 并确认实施哈希有效。",
        "inputs": ["用户明确选择的简单 CSV、Excel 或 Word 文件，或已核验的本机成果",
                   "固定表格整理、文档复制或 Word 表格转 Excel 配方；Word 表格配方保留所有原列，不支持选列",
                   "可选：生成未发送的本地邮件草稿"],
        "outputs": ["新建并核验的 Excel 工作簿、Word 文档或 Word 报告；可选本地未发送邮件草稿"],
        "limits": ["来源文件不超过 20 MB；表格最多 12 列、200 行",
                   "Word 表格原文提取保留全部原行列和文字，不支持选列、改值或追加行",
                   "Word 数字配方保留全部原行列，仅把第二列严格数字文本转换并求和，不支持已有合计行",
                   "只运行固定配方，每个已启用版本最多运行 10 次",
                   "输入失败可换新任务纠正；未知作用与技能所属部分工作流不得自动重跑"],
        "implementation": ["agent_service/local_skills.py", "agent_service/office_source_plan.py",
                           "agent_service/office_workflow.py", "agent_service/artifact_source.py",
                           "agent_service/skill_matching.py", "run_jarvis_skills.py"],
        "evidence_status": "offline_and_live_target",
        "offline_tests": ["tests/test_local_skills.py", "tests/test_capabilities.py"],
        "live_evidence": ["validation/artifacts/document-numeric-live-20261017/gates.json"],
        "historical_live_evidence": ["validation/artifacts/artifact-skills-live-20261017/skills-live.json",
                                     "validation/artifacts/document-table-live-20261017/recipes-live.json",
                                     "validation/artifacts/skill-matching-live-20261017/recipes-final.json",
                                     "validation/artifacts/skill-resume-live-20261017/gates.json"],
    },
)

_BY_ID = {entry["id"]: entry for entry in _CATALOG}
_ACQUISITION_STEPS = (
    "check_applicable_verified_local_skill",
    "check_supported_official_adapter",
    "review_mature_library_and_license",
    "research_and_propose_new_adapter_or_REA_path",
)


def _validate_query(query):
    if type(query) is not str or not _QUERY.fullmatch(query.strip()):
        raise ValueError("capability_query_invalid")
    return query.strip()


def _alias_key(value):
    """Normalize only for static alias matching; raw query validation stays bounded."""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(char for char in normalized if not char.isspace())


def _alias_capabilities(query):
    normalized = _alias_key(query)
    matches = []
    for alias, capability_ids in _ALIASES.items():
        start = 0
        while True:
            index = normalized.find(alias, start)
            if index < 0:
                break
            end = index + len(alias)
            left = normalized[index - 1] if index else ""
            right = normalized[end] if end < len(normalized) else ""
            left_word = alias[0].isascii() and alias[0].isalnum()
            right_word = alias[-1].isascii() and alias[-1].isalnum()
            if not ((left_word and left.isascii() and left.isalnum())
                    or (right_word and right.isascii() and right.isalnum())):
                for capability_id in capability_ids:
                    if capability_id not in matches:
                        matches.append(capability_id)
                break
            start = index + 1
    return matches


def list_capabilities(query=None, limit=50) -> list[dict]:
    """Return static records via specific alias hints or bounded substring search."""
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("capability_limit_invalid")
    checked_query = _validate_query(query) if query is not None else None
    if checked_query is not None:
        alias_ids = _alias_capabilities(checked_query)
        if alias_ids:
            return [deepcopy(_BY_ID[capability_id]) for capability_id in alias_ids[:limit]]
    needle = checked_query.casefold() if checked_query is not None else None
    matches = []
    for entry in _CATALOG:
        if needle is not None:
            searchable = " ".join((entry["id"], entry["title"], entry["description"],
                                   entry["scope"], *entry["inputs"], *entry["outputs"],
                                   *entry["limits"], *entry["implementation"])).casefold()
            if needle not in searchable:
                continue
        matches.append(deepcopy(entry))
        if len(matches) >= limit:
            break
    return matches


def get_capability(capability_id) -> dict | None:
    """Return an independent copy of one static record, or None when not found."""
    if type(capability_id) is not str or not _CAPABILITY_ID.fullmatch(capability_id):
        raise ValueError("capability_id_invalid")
    entry = _BY_ID.get(capability_id)
    return deepcopy(entry) if entry is not None else None


def capability_gap(query) -> dict:
    """Describe local matches and a proposed acquisition order; perform no acquisition."""
    normalized = _validate_query(query)
    matches = list_capabilities(normalized)
    return {"query": normalized,
            "matched_ids": [item["id"] for item in matches],
            "gap": not bool(matches),
            "acquisition_steps": list(_ACQUISITION_STEPS) if not matches else []}

