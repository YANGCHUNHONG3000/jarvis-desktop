"""Small local launcher for Jarvis's Notepad and public-source workflows."""
import hashlib
import argparse
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
import uuid


ROOT = Path(__file__).resolve().parent
REQUESTS = ROOT / "data" / "user_requests"
RESEARCH_ROOT = ROOT / "data" / "agent_service"
DESKTOP_ROOT = ROOT / "data" / "desktop_runs"
TASK_ROOT = ROOT / "data" / "assistant_tasks"
PYTHON = str(Path(sys.executable).with_name("python.exe")) if os.name == "nt" else sys.executable
SOURCE_SUFFIXES = {".csv", ".xlsx", ".docx"}
RETRY_SOURCE_LABEL = "已绑定原任务核验资料（新任务开始前重验）"
RETRY_NO_SOURCE_LABEL = "已绑定原任务请求（原任务没有资料）"
SKILL_CHOICES = {
    "表格整理：Excel + Word + 本地未发送草稿（第二列求和）": "table-office-package",
    "表格原样复制：仅新建 Excel，不求和、不生成报告或草稿": "table-office-copy",
    "Word 副本：新建文档（保留可读取正文和表格）": "document-office-copy",
    "Word表格转Excel与报告：仅提取唯一表格，原文保留": "document-table-office-package",
    "Word表格第2列转数字求和：Excel＋Word报告（无草稿）": "document-numeric-report",
}
SKILL_CONTRACTS = {
    "table-office-package": ("table_report", [1], True),
    "table-office-copy": ("table_copy", [], False),
    "document-office-copy": ("document_copy", [], False),
    "document-table-office-package": ("document_table_report", [], False),
    "document-numeric-report": ("document_numeric_report", [1], False),
}


def _published_skill_status(store, skill_id, base):
    """Check the publisher gate and the exact recipe advertised by this GUI."""
    from agent_service import local_skills
    state = local_skills.status(store, skill_id, base)
    if type(state) is not dict or state.get("enabled") is not True:
        return state
    remaining = state.get("remaining_runs")
    if type(remaining) is not int or remaining <= 0:
        return {**state, "enabled": False}
    digest = state.get("active")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        return {**state, "enabled": False, "contract_mismatch": True}
    _, manifest = local_skills._version(store, skill_id, digest)
    expected = SKILL_CONTRACTS.get(skill_id)
    actual = (manifest.get("recipe"), manifest.get("sum_columns"), manifest.get("draft"))
    return state if expected == actual else {**state, "enabled": False, "contract_mismatch": True}


def _validate_source_file(value):
    """Resolve and validate a user-selected source without reading its contents."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("请选择 CSV、XLSX 或 DOCX 文件。")
    raw = value.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {'"', "'"}:
        raw = raw[1:-1]
    path = Path(raw).expanduser()
    if path.suffix.lower() not in SOURCE_SUFFIXES:
        raise ValueError("资料文件仅支持 CSV、XLSX 或 DOCX。")
    try:
        path = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ValueError("所选资料文件不存在或无法访问。") from None
    if not path.is_file():
        raise ValueError("所选资料文件不存在或无法访问。")
    try:
        if path.is_relative_to(TASK_ROOT.resolve()) and path.suffix.lower() in {".docx", ".xlsx"}:
            raise ValueError("本机生成的 Word/Excel 成果请用“用于下一任务”选取并核验。")
    except OSError:
        raise ValueError("任务目录暂不可核验；请稍后重试。") from None
    return path


def _history_state_label(state):
    if state == "succeeded":
        return "成功"
    if state == "recovered":
        return "部分完成"
    if state == "unsupported":
        return "暂不支持"
    if state == "failed":
        return "失败"
    return "待核验"


def _history_list_state_label(state):
    if state == "succeeded":
        return "记录成功·待核验"
    if state == "recovered":
        return "记录部分完成·待核验"
    if state == "unsupported":
        return "未支持·可查候选"
    if state == "failed":
        return "失败·可查记录"
    return "待核验"


def _original_state_label(state):
    return {"succeeded": "成功", "cancelled": "已停止", "timed_out": "超时",
            "failed": "失败", "unsupported": "暂不支持", "unknown": "结果未知",
            "recovered": "部分完成"}.get(
                state, "待核验")


def _skill_input_failure_label(code):
    return {
        "skill_source_kind_mismatch": "资料类型与这个技能不匹配",
        "skill_document_table_required": "所选 Word 没有可读取的唯一表格",
        "source_existing_total_unsupported": "Word 表格已有合计行，不能再次对它求和；请核对资料后新建任务",
        "source_numeric_cell_invalid": "指定数字列含缺失或含糊的文本，无法可靠转换求和；请核对资料后新建任务",
        "source_numeric_values_invalid": "指定数字列的数据或精度不适合可靠求和；请核对资料后新建任务",
        "source_numeric_columns_invalid": "指定的数字列与求和列不匹配；请核对列目标后新建任务",
        "skill_source_invalid": "资料无法完整读取",
        "skill_source_changed": "资料在核验前已变化",
        "skill_gate_stale": "技能发布证据已过期",
        "skill_preflight_rejected": "资料或配方未通过开始前检查",
    }.get(code, "资料或配方未通过开始前检查")


def _gap_retry_reason_label(code):
    return {
        "gap_instruction_missing": "原任务描述缺失",
        "gap_instruction_ambiguous": "原任务描述有多个匹配记录，无法确认原文",
        "gap_instruction_changed": "原任务描述已变化",
        "gap_instruction_invalid": "原任务描述无法完整核验",
        "gap_source_invalid": "原任务资料无法完整核验",
        "gap_source_changed": "原任务资料已变化",
        "gap_provenance_missing": "原任务来源接力记录缺失",
        "gap_provenance_changed": "原任务来源接力记录已变化",
        "gap_effect_unknown": "原任务可能已有结果未知的操作，不能自动接力",
        "gap_result_invalid": "原未支持结果无法核验",
        "gap_changed": "原任务缺口记录已变化",
        "gap_events_invalid": "原任务事件记录无法核验",
        "gap_origin_invalid": "原任务来源链无法核验",
        "gap_origin_changed": "原任务来源链已变化",
        "gap_token_changed": "原任务记录在准备期间已变化",
    }.get(code, "原任务记录暂不能完整核验")


def _resume_reason_label(reason):
    """Only show bounded, local continuation status codes in the desktop UI."""
    labels = {
        "ready": "可以继续", "complete": "办公步骤已经完成",
        "already_complete": "办公步骤已经完成",
        "resume_already_complete": "办公步骤已经完成",
        "resume_unavailable": "续跑条件尚未满足",
        "resume_parent_invalid": "原任务结果无法核验",
        "resume_parent_mismatch": "原任务成果与步骤回执不一致",
        "resume_unclaimed_receipts": "存在未经可信续跑记录确认的中间成果，需人工核查",
        "resume_workflow_scope_invalid": "办公工作流目录无法核验",
        "resume_workflow_ambiguous": "办公工作流目录不唯一",
        "resume_manifest_invalid": "办公步骤清单无法核验",
        "resume_workflow_invalid": "办公步骤记录无法核验",
        "resume_unknown_effect": "存在结果未知的步骤，需人工核查",
        "resume_skill_owned": "这是受限技能任务；须重核候选并新建任务，不能直接续跑",
        "resume_bundle_owned": "联合核对任务须由专用恢复路径继续",
        "later_step_conflict": "步骤记录顺序异常，需人工核查",
        "resume_preview_stale": "任务记录已变化，请重新选择并核验",
        "resume_record_invalid": "续跑记录无法核验",
        "resume_attempt_exists": "本次续跑记录已存在",
        "skill_resume_unavailable": "技能续跑条件尚未满足",
        "skill_resume_parent_invalid": "原技能任务结果无法核验",
        "skill_resume_permit_invalid": "原技能任务没有可信续跑许可",
        "skill_resume_evidence_changed": "技能续跑许可绑定的记录已变化",
        "skill_resume_candidate_not_blocked": "技能发布状态已变化，不能按原许可续跑",
        "skill_resume_changed": "技能实施或已有步骤已变化",
        "skill_resume_plan_changed": "技能来源或固定步骤已变化",
        "skill_resume_source_invalid": "原技能来源无法完整核验",
        "skill_resume_boundary_invalid": "原任务没有可确认的取消或派发前阻塞边界",
        "skill_resume_chain_invalid": "技能续跑记录不完整或已变化，需人工核查",
        "skill_resume_attempt_limit": "本任务已达到技能续跑次数上限，需人工核查",
        "skill_resume_request_invalid": "原技能请求无法核验",
        "skill_resume_cancel_invalid": "原取消记录无法核验",
        "skill_resume_native_unverified": "已有办公成果的应用与文件证据无法核验",
        "skill_resume_draft_unverified": "已有草稿文件无法核验",
        "skill_resume_already_attempted": "本任务已尝试技能续跑，不能再次执行",
        "skill_resume_preview_stale": "技能续跑记录已变化，请重新选择核验",
        "task_not_found": "任务记录不可用", "result_missing": "原任务结果缺失",
        "result_invalid": "原任务结果无法核验", "not_office": "仅办公任务支持继续",
        "workflow_invalid": "办公步骤记录无法核验", "manifest_invalid": "步骤清单无法核验",
        "receipt_invalid": "已有成果无法核验", "artifact_changed": "已有成果已更改",
        "unknown_effect": "存在结果未知的步骤，需人工核查",
        "inconsistent_steps": "步骤记录顺序异常，需人工核查",
        "workflow_busy": "另一进程正在处理此工作流",
        "office_ui_blocked": "Office窗口被对话框阻塞或无响应；请处理该窗口后到历史任务继续。当前步骤尚未派发",
        "office_ui_unavailable": "暂时无法检查Office窗口；当前步骤尚未派发，请稍后在历史任务重试",
        "stale_preview": "任务记录已变化，请重新选择并核验",
        "cancelled": "已停止", "timed_out": "继续执行超时",
        "blocked": "等待处理阻塞条件",
        "unknown": "执行结果待核验", "failed": "继续执行未完成",
    }
    return labels.get(reason, "暂不可继续，请核验任务记录")


def _resume_handoff_outputs(inspection, outcome, task_root, attempt_id):
    """Check one reconciled batch in the worker, never trust stdout success."""
    if (type(inspection) is not dict or type(outcome) is not dict
            or inspection.get("root") != str(task_root)
            or outcome.get("task_root") != str(task_root)
            or outcome.get("attempt_id") != attempt_id
            or inspection.get("state") == "needs_review"
            or outcome.get("ok") is not True or outcome.get("state") != "succeeded"):
        return []
    records, expected = inspection.get("artifacts"), outcome.get("artifacts")
    if type(records) is not list or type(expected) is not list:
        return []
    # Research's separately verified Markdown stays in history. Only the exact
    # resumed Office/draft batch is eligible for this next-task handoff.
    records = [r for r in records if not (type(r) is dict and type(r.get("path")) is str
               and Path(r["path"]).suffix.lower() == ".md")]
    if not 1 <= len(records) <= 8 or len(records) != len(expected):
        return []
    outputs, labels, paths = [], set(), set()
    for record, reference in zip(records, expected):
        if (type(record) is not dict or type(reference) is not dict
                or type(record.get("label")) is not str or not record["label"]
                or record["label"] in labels
                or record.get("path") != reference.get("path")
                or type(record.get("sha256")) is not str
                or re.fullmatch(r"[0-9a-f]{64}", record["sha256"]) is None
                or record.get("sha256") != reference.get("sha256")):
            return []
        limit = 45_000_000 if str(record.get("path", "")).lower().endswith(".eml") else 20_000_000
        path = _saved_path(record.get("path"), task_root, record["sha256"], max_bytes=limit)
        if (path is None or path in paths or path.suffix.lower() not in {".docx", ".xlsx", ".eml"}
                or path.parent.name != reference.get("step")):
            return []
        labels.add(record["label"])
        paths.add(path)
        outputs.append((record["label"], path, record["sha256"]))
    return outputs


def _resume_command(task_root, token, attempt_id):
    if (not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{64}", token)
            or not isinstance(attempt_id, str) or not re.fullmatch(r"[0-9a-f]{32}", attempt_id)):
        raise ValueError("resume_argument_invalid")
    return [PYTHON, str(ROOT / "run_jarvis_resume.py"), "--task-root", str(task_root),
            "--token", token, "--attempt-id", attempt_id]


def _bundle_resume_command(task_root, token, attempt_id):
    command = _resume_command(task_root, token, attempt_id)
    command.insert(2, "--bundle")
    return command


def _skill_resume_command(task_root, token, attempt_id):
    if (not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{64}", token)
            or not isinstance(attempt_id, str) or not re.fullmatch(r"[0-9a-f]{32}", attempt_id)):
        raise ValueError("skill_resume_argument_invalid")
    return [PYTHON, str(ROOT / "run_jarvis_skill_resume.py"), "--task-root", str(task_root),
            "--token", token, "--attempt-id", attempt_id]


def _mail_export_command(task_root, request_sha256):
    if not isinstance(request_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", request_sha256):
        raise ValueError("mail_export_request_invalid")
    return [PYTHON, str(ROOT / "run_jarvis_mail_export.py"), "--task-root", str(task_root),
            "--request-sha256", request_sha256]


def _mail_export_prepare_label(code):
    """Map stable preparation codes to non-private, actionable UI text."""
    labels = {
        "mail_export_document_limit": "邮件内容超过本机 Word 文档容量，请选择较短的邮件。",
        "mail_export_request_oversize": "邮件内容超过本机导出请求容量，请选择较短的邮件。",
        "mail_export_body_invalid": "邮件正文包含暂不支持的内容或超过容量，无法完整导出。",
        "mail_export_header_invalid": "邮件头内容暂不支持完整导出。",
        "mail_export_attachments_invalid": "邮件附件元数据暂不支持完整导出。",
        "mail_export_coverage_invalid": "邮件正文范围无法确认完整，请重新读取后再导出。",
        "mail_export_task_scope_invalid": "任务目录无法安全使用；请检查任务存储位置。",
    }
    return labels.get(code, "本机邮件导出请求未能准备；请重新读取后再试。")


def _verified_history_path(value, expected_sha, base=TASK_ROOT):
    if not isinstance(value, str):
        return None
    suffix = Path(value).suffix.lower()
    limits = {".docx": 20_000_000, ".xlsx": 20_000_000, ".eml": 45_000_000,
              ".md": 2_000_000}
    if suffix not in limits:
        return None
    return _saved_path(value, base, expected_sha, max_bytes=limits[suffix])


def _source_file_argument(argv=None):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--source-file")
    args, _ = parser.parse_known_args(argv)
    if not args.source_file:
        return None
    try:
        return _validate_source_file(args.source_file)
    except ValueError as exc:
        return exc


def _saved_path(value, base, expected_sha=None, max_bytes=2_000_000):
    if (not isinstance(value, str) or not isinstance(expected_sha, str)
            or len(expected_sha) != 64):
        return None
    try:
        path = Path(value).resolve()
        if not path.is_file() or not path.is_relative_to(base.resolve()):
            return None
        if path.stat().st_size > max_bytes or hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha:
            return None
    except (OSError, ValueError):
        return None
    return path


def _office_artifacts(result, base=TASK_ROOT):
    """Return Office files whose path and content hash match this task result."""
    if type(result) is not dict or result.get("kind") != "office":
        return []
    verified = []
    artifacts = result.get("artifacts")
    if not isinstance(artifacts, list):
        return verified
    for item in artifacts:
        if type(item) is not dict:
            continue
        limit = 45_000_000 if str(item.get("path", "")).lower().endswith(".eml") else 20_000_000
        path = _saved_path(item.get("path"), base, item.get("sha256"), max_bytes=limit)
        step = item.get("step")
        if path is None or not isinstance(step, str) or not re.fullmatch(r"s[1-9][0-9]*", step):
            continue
        suffix = path.suffix.lower()
        if suffix not in {".docx", ".xlsx", ".eml"}:
            continue
        format_name = {".docx": "文档 (.docx)", ".xlsx": "表格 (.xlsx)", ".eml": "未发送邮件草稿"}[suffix]
        verified.append((f"{step} · {format_name}", path))
    report = _saved_path(result.get("research_artifact"), base, result.get("research_sha256"))
    if report is not None and report.suffix.lower() == ".md":
        verified.append(("资料归纳与来源 (.md)", report))
    return verified


def _durable_task_result(task_root, base=TASK_ROOT):
    """Read only this launch's persisted result, never another run's newest file."""
    try:
        path = Path(task_root).resolve()
        if path.parent != base.resolve() or not re.fullmatch(r"task-[0-9a-f]{32}", path.name):
            return {}
        file = path / "result.json"
        if file.stat().st_size > 100_000:
            return {}
        value = json.loads(file.read_text(encoding="utf-8"))
        if type(value) is not dict or value.get("root") != str(path):
            return {}
        return value
    except (TypeError, OSError, ValueError):
        return {}


def _desktop_saved_path(result, returncode, base=DESKTOP_ROOT):
    return (_saved_path(result.get("document"), base, result.get("file_sha256"))
            if returncode == 0 and result.get("ok") is True and result.get("file_saved") is True
            and result.get("ui_verified") is True else None)


def _record_desktop_outcome(task_root, result, base=TASK_ROOT, desktop_base=DESKTOP_ROOT, returncode=0):
    prepared = _durable_task_result(task_root, base)
    if prepared.get("kind") != "notepad" or prepared.get("state") != "prepared":
        raise ValueError("task_not_prepared")
    from agent_service.notepad import normalize_text
    expected = hashlib.sha256(normalize_text(prepared.get("text", "")).encode("utf-8")).hexdigest()
    saved = _desktop_saved_path(result, returncode, desktop_base)
    if result.get("expected_sha256") != expected or result.get("readback_sha256") != expected:
        saved = None
    outcome = {"ok": bool(saved), "state": "succeeded" if saved else "unknown",
               "root": str(Path(task_root).resolve()), "desktop_result": result}
    with (Path(task_root) / "desktop-result.json").open("x", encoding="utf-8") as file:
        json.dump(outcome, file, ensure_ascii=False)
        file.flush()
        os.fsync(file.fileno())
    return outcome


def _last_json_lines(output):
    for line in reversed(output.splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and "ok" in value:
            return value
    return {}


def _task_command(request, task_root, source_file=None, source_artifact=None, source_retry=None):
    if sum(value is not None for value in (source_file, source_artifact, source_retry)) > 1:
        raise ValueError("source_ambiguous")
    command = [PYTHON, str(ROOT / "run_jarvis_task.py"), "--text-file", str(request),
               "--root", str(TASK_ROOT), "--task-root", str(task_root)]
    if source_file:
        command.extend(["--source-file", str(source_file)])
    if source_artifact is not None:
        command.extend(["--source-artifact-json", json.dumps(source_artifact,
                        ensure_ascii=False, sort_keys=True, separators=(",", ":"))])
    if source_retry is not None:
        if (type(source_retry) is not dict or set(source_retry) != {"task_root", "token"}
                or not isinstance(source_retry["task_root"], str)
                or not isinstance(source_retry["token"], str)
                or re.fullmatch(r"[0-9a-f]{64}", source_retry["token"]) is None):
            raise ValueError("source_retry_invalid")
        command.extend(["--source-retry-json", json.dumps(source_retry,
                        ensure_ascii=False, sort_keys=True, separators=(",", ":"))])
    return command


def _skill_command(skill_id, task_root, *, source_file=None, source_artifact=None,
                   expected_sha256=None):
    if (skill_id not in SKILL_CHOICES.values()
            or (source_file is None) == (source_artifact is None)
            or not isinstance(expected_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None):
        raise ValueError("skill_gui_argument_invalid")
    command = [PYTHON, str(ROOT / "run_jarvis_skills.py"),
               "--store", str(ROOT / "data" / "capabilities"), "--base", str(TASK_ROOT),
               "run", skill_id, "--task-root", str(task_root),
               "--expected-sha256", expected_sha256]
    if source_artifact is not None:
        command.extend(["--source-artifact-json", json.dumps(source_artifact,
                        ensure_ascii=False, sort_keys=True, separators=(",", ":"))])
    else:
        command.extend(["--source-file", str(source_file)])
    return command


def _read_draft_preview(path):
    from email.parser import BytesParser
    from email.policy import default
    path = Path(path)
    if path.suffix.lower() != ".eml" or not 0 < path.stat().st_size <= 45_000_000:
        raise ValueError("draft_invalid")
    message = BytesParser(policy=default).parsebytes(path.read_bytes())
    body = message.get_body(preferencelist=("plain",))
    if (message.defects or message.get_all("X-Unsent") != ["1"] or body is None
            or "From" in message or not str(message.get("Subject", "")).strip()):
        raise ValueError("draft_invalid")
    content = body.get_content()
    if len(content) > 20_010:
        raise ValueError("draft_oversize")
    return {"subject": str(message.get("Subject", "")), "to": str(message.get("To", "未填写")),
            "body": content, "attachments": [part.get_filename() or "附件" for part in message.iter_attachments()]}


def main(argv=None):
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.title("Jarvis · 个人助手")
    root.geometry("780x720")
    messages = queue.Queue()
    history_messages = queue.Queue()
    mail_export_messages = queue.Queue()
    source_ref_messages = queue.Queue()
    skill_messages = queue.Queue()
    active = {"kind": None, "process": None, "stop": False, "task_root": None,
              "stop_deadline": None, "attempt_id": None, "attempt_root": None,
              "history_id": None, "verify_deadline": None}
    last_paths = {"notepad": None, "research": None, "agent": None}
    initial_source = _source_file_argument(argv)
    agent_outputs = {}
    agent_output_hashes = {}
    note_parent = {"root": None}
    budget_clock = {"updated": 0.0}

    tabs = ttk.Notebook(root)
    note_tab, research_tab = ttk.Frame(tabs, padding=14), ttk.Frame(tabs, padding=14)
    task_tab = ttk.Frame(tabs, padding=14)
    history_tab = ttk.Frame(tabs, padding=14)
    capability_tab = ttk.Frame(tabs, padding=14)
    tabs.add(task_tab, text="交给贾维斯")
    tabs.add(note_tab, text="记事本")
    tabs.add(research_tab, text="查资料")
    tabs.add(history_tab, text="历史任务")
    tabs.add(capability_tab, text="本机能力")
    from agent_service.computer_panel import attach_computer_tab
    def computer_busy_changed(value):
        active.update(kind='computer' if value else None)
        notify_goal_external_activity(bool(value), force=bool(value))

    computer_panel = attach_computer_tab(tabs, root, busy=lambda: active['kind'] is not None,
                                        on_busy=computer_busy_changed)
    from agent_service.computer_goal_panel import attach_goal_tab
    def goal_busy_changed(value):
        active.update(kind='computer_goal' if value else None)
        if not value and history_selected.get('inspection') is not None:
            count = sum(path.suffix.lower() in {'.docx', '.xlsx'}
                        for path, _sha in history_artifacts.values())
            history_batch_button.configure(
                state='normal' if active['kind'] is None and 2 <= count <= 4
                and not source_choice['pending'] else 'disabled')

    goal_panel = attach_goal_tab(tabs, root, busy=lambda: active['kind'] is not None,
                                on_busy=goal_busy_changed)

    def invalidate_history_batch_source(reason):
        invalidate = getattr(goal_panel, 'invalidate_history_batch_source', None)
        if callable(invalidate):
            invalidate(reason)

    def notify_goal_external_activity(active_now=True, force=False):
        notify = getattr(goal_panel, 'notify_external_activity', None)
        if callable(notify):
            notify(active_now, force=force)

    from agent_service.mailbox_panel import attach_mailbox_tab
    mailbox_tab = attach_mailbox_tab(tabs, root, lambda snapshot: start_mail_export(snapshot))
    if mailbox_tab is None:
        unavailable = ttk.Frame(tabs, padding=14)
        tabs.add(unavailable, text="邮箱（未启动）")
        ttk.Label(unavailable, text="邮箱界面未能加载；其他功能仍可使用。请保留此错误反馈。").pack(anchor="w")
    tabs.pack(fill="both", expand=True)

    ttk.Label(capability_tab, text="本机能力目录仅列已有受限适配器与验证范围；"
              "查找不会调用模型、安装技能或执行动作。", wraplength=650).pack(anchor="w", pady=(0, 8))
    capability_query = ttk.Entry(capability_tab, width=52)
    capability_query.pack(anchor="w", fill="x")
    capability_status = tk.StringVar(value="正在读取本机静态能力目录……")
    capability_records = {}
    capability_list = ttk.Treeview(capability_tab, columns=("name", "scope"),
                                   show="headings", height=10)
    capability_list.heading("name", text="能力")
    capability_list.heading("scope", text="验证状态")
    capability_list.column("name", width=420, stretch=True)
    capability_list.column("scope", width=180, stretch=False)

    def show_capability(_event=None):
        selected = capability_list.selection()
        item = capability_records.get(selected[0]) if selected else None
        if item is None:
            return
        evidence = {"offline_and_live_target": "离线测试和指定目标实测",
                    "offline_and_synthetic_live_target": "离线测试和合成目标实测",
                    "offline_with_user_reported_live": "离线测试；用户确认读取正常",
                    "offline_tests_only": "仅离线测试"}.get(
                        item.get("evidence_status"), "证据待核验")
        availability = {"implemented": "已实现，限所列范围",
                        "implemented_auth_required": "已实现，需用户账号授权"}.get(
                            item.get("availability"), "尚不可用")
        capability_detail.set("\n".join([
            str(item.get("title", "")) + " · " + availability + " · " + evidence,
            "范围：" + str(item.get("scope", "")),
            "输入：" + "；".join(item.get("inputs", [])),
            "输出：" + "；".join(item.get("outputs", [])),
            "边界：" + "；".join(item.get("limits", [])),
            "目录展示不代表本次任务已执行，也不启用未验证技能。",
        ]))

    def search_capabilities():
        query = capability_query.get().strip()
        try:
            from agent_service.capabilities import list_capabilities
            records = list_capabilities(query if query else None, limit=50)
        except ValueError:
            capability_status.set("查找词须为 1 至 200 个可显示字符；未调用模型。")
            return
        except Exception:
            capability_status.set("本机能力目录暂不可读；未调用模型。")
            return
        capability_records.clear()
        for row in capability_list.get_children():
            capability_list.delete(row)
        for item in records:
            if type(item) is not dict or not isinstance(item.get("id"), str):
                continue
            capability_records[item["id"]] = item
            label = {"offline_and_live_target": "含指定目标实测",
                     "offline_and_synthetic_live_target": "含合成目标实测",
                     "offline_with_user_reported_live": "用户已确认读取正常",
                     "offline_tests_only": "仅离线测试"}.get(
                         item.get("evidence_status"), "待核验")
            capability_list.insert("", "end", iid=item["id"],
                                   values=(item.get("title", item["id"]), label))
        capability_status.set(f"本机目录匹配 {len(capability_records)} 项。"
                              + (" 无匹配项不代表自动获得新能力。" if not capability_records else ""))
        capability_detail.set("请选择一项查看明确的输入、输出与边界。")

    capability_row = ttk.Frame(capability_tab)
    capability_row.pack(anchor="w", pady=6)
    ttk.Button(capability_row, text="查找本机能力", command=search_capabilities).pack(side="left")
    ttk.Button(capability_row, text="显示全部", command=lambda: (
        capability_query.delete(0, "end"), search_capabilities())).pack(side="left", padx=6)
    ttk.Label(capability_tab, textvariable=capability_status, wraplength=650).pack(anchor="w", pady=4)
    capability_list.pack(fill="both", expand=True)
    capability_list.bind("<<TreeviewSelect>>", show_capability)
    capability_query.bind("<Return>", lambda _event: search_capabilities())
    capability_detail = tk.StringVar(value="请选择一项查看明确的输入、输出与边界。")
    ttk.Label(capability_tab, textvariable=capability_detail, wraplength=650,
              justify="left").pack(anchor="w", pady=8)
    search_capabilities()

    ttk.Label(task_tab, text="用自己的话描述任务：").pack(anchor="w")
    task_text = tk.Text(task_tab, height=6, wrap="word")
    task_text.pack(fill="both", expand=True, pady=8)
    ttk.Label(task_tab, text="例如：查资料并保存带来源的摘要；或把所选 Word 的唯一表格按原文转为 Excel 并写 Word 报告。"
              "表格提取保留已有合计，不重新计算，也不复制原文档其他段落。",
              wraplength=620).pack(anchor="w", pady=4)
    source_choice = {"path": str(initial_source) if isinstance(initial_source, Path) else "",
                     "artifact_ref": None, "retry_ref": None, "retry_instruction": None,
                     "retry_changed": False, "auto_required": False,
                     "generation": 0, "pending": False,
                     "pending_deadline": None, "setting": False}
    skill_gate = {"generation": 0, "id": None, "enabled": False, "digest": None,
                  "checking": False, "deadline": None}
    skill_run_root = {"root": None}
    source_label = tk.StringVar(value=source_choice["path"])

    def choose_source_file():
        from tkinter import filedialog
        selected = filedialog.askopenfilename(
            title="选择任务资料",
            filetypes=[("支持的资料", "*.csv *.xlsx *.docx"),
                       ("CSV 文件", "*.csv"), ("Excel 工作簿", "*.xlsx"),
                       ("Word 文档", "*.docx")])
        if selected:
            source_choice["artifact_ref"] = None
            source_choice["retry_ref"] = None
            source_choice["retry_instruction"] = None
            source_choice["retry_changed"] = False
            source_choice["path"] = selected
            source_label.set(selected)
            source_clear.configure(state="normal")

    def clear_source_file():
        source_choice["auto_required"] = False
        source_choice["artifact_ref"] = None
        source_choice["retry_ref"] = None
        source_choice["retry_instruction"] = None
        source_choice["retry_changed"] = False
        source_choice["generation"] += 1
        source_choice["pending"] = False
        source_choice["pending_deadline"] = None
        source_choice["path"] = ""
        source_label.set("")
        source_clear.configure(state="disabled")

    source_row = ttk.Frame(task_tab)
    source_row.pack(fill="x", anchor="w", pady=3)
    source_pick = ttk.Button(source_row, text="选择资料文件…", command=choose_source_file)
    source_pick.pack(side="left")
    source_entry = ttk.Entry(source_row, textvariable=source_label, width=38)
    source_entry.pack(side="left", padx=6)
    source_clear = ttk.Button(source_row, text="清除", command=clear_source_file,
                              state="normal" if source_choice["path"] else "disabled")
    source_clear.pack(side="left")
    def source_text_changed(*_):
        if not source_choice["setting"]:
            source_choice["auto_required"] = False
            if source_choice["retry_ref"] is not None:
                source_choice["retry_changed"] = True
            source_choice["artifact_ref"] = None
            source_choice["retry_ref"] = None
            source_choice["retry_instruction"] = None
            source_choice["generation"] += 1
            source_choice["pending"] = False
            source_choice["pending_deadline"] = None
            source_choice["path"] = source_label.get()
        source_clear.configure(state="normal" if source_label.get().strip()
                               and active["kind"] is None else "disabled")
        update_skill_button()

    source_label.trace_add("write", source_text_changed)

    def clear_changed_retry_draft():
        source_choice["auto_required"] = False
        if source_choice["retry_ref"] is None:
            return
        source_choice["retry_ref"] = None
        source_choice["retry_instruction"] = None
        source_choice["retry_changed"] = True
        source_choice["generation"] += 1
        source_choice["setting"] = True
        try:
            source_label.set("")
        finally:
            source_choice["setting"] = False
        source_choice["path"] = ""
        source_clear.configure(state="disabled")
        task_status.set("任务描述已修改，原任务资料绑定已明确取消。请核对新请求后再次点击开始；当前未调用模型。")
        update_skill_button()

    def task_text_changed(_event=None):
        if (source_choice["retry_ref"] is not None
                and task_text.get("1.0", "end-1c") != source_choice["retry_instruction"]):
            clear_changed_retry_draft()

    task_text.bind("<KeyRelease>", task_text_changed)
    ttk.Label(task_tab, text="点击“开始任务”时，所选 CSV、Excel 或 Word 内容会提供给云端模型；"
              "运行本地技能不调用模型、不上传资料。",
              wraplength=620).pack(anchor="w", pady=2)
    skill_row = ttk.Frame(task_tab)
    skill_row.pack(fill="x", anchor="w", pady=5)
    skill_choice = tk.StringVar()
    skill_list = ttk.Combobox(skill_row, textvariable=skill_choice,
                              values=tuple(SKILL_CHOICES), state="readonly", width=49)
    skill_list.pack(side="left")
    skill_status = tk.StringVar(value="本地技能需先发布并通过真实状态门；请选受限配方。")

    def update_skill_button():
        skill_id = SKILL_CHOICES.get(skill_choice.get())
        source_ref = source_choice["artifact_ref"]
        source_path = source_ref.get("artifact") if type(source_ref) is dict else source_label.get().strip()
        suffix = Path(source_path).suffix.lower() if source_path else ""
        compatible = (suffix in {".csv", ".xlsx"} if skill_id in {
                          "table-office-package", "table-office-copy"}
                      else suffix == ".docx" if skill_id in {
                          "document-office-copy", "document-table-office-package",
                          "document-numeric-report"} else False)
        ready = (active["kind"] is None and not source_choice["pending"]
                 and source_choice["retry_ref"] is None
                 and skill_gate["id"] == skill_id and skill_gate["enabled"]
                 and not skill_gate["checking"] and compatible)
        skill_run.configure(state="normal" if ready else "disabled")

    def refresh_skill_gate(_event=None, *, outcome=None):
        skill_id = SKILL_CHOICES.get(skill_choice.get())
        skill_gate["generation"] += 1
        generation = skill_gate["generation"]
        skill_gate.update(id=skill_id, enabled=False, digest=None, checking=bool(skill_id),
                          deadline=time.monotonic() + 30 if skill_id else None,
                          outcome=outcome if isinstance(outcome, str) else None)
        skill_run.configure(state="disabled")
        if skill_id is None:
            skill_status.set("请从固定配方中选择本地技能。")
            return
        skill_status.set((outcome + " " if outcome else "") +
                         "正在后台重新核对发布状态和剩余运行次数；不调用模型。")

        def work():
            try:
                state = _published_skill_status(ROOT / "data" / "capabilities", skill_id, TASK_ROOT)
                skill_messages.put(("status", generation, skill_id, state, None))
            except Exception:
                skill_messages.put(("status", generation, skill_id, None,
                                    "skill_status_unavailable"))

        try:
            threading.Thread(target=work, daemon=True, name="jarvis-skill-status").start()
        except RuntimeError:
            skill_messages.put(("status", generation, skill_id, None,
                                "skill_status_unavailable"))

    def start_skill():
        if active["kind"] is not None or source_choice["pending"]:
            return
        if source_choice["retry_ref"] is not None:
            skill_status.set("原任务缺口资料已绑定；请先清除或另选资料，才能运行固定本地技能。")
            return
        skill_id = SKILL_CHOICES.get(skill_choice.get())
        if (skill_id is None or skill_gate["id"] != skill_id
                or not skill_gate["enabled"] or skill_gate["checking"]
                or not isinstance(skill_gate["digest"], str)
                or re.fullmatch(r"[0-9a-f]{64}", skill_gate["digest"]) is None):
            skill_status.set("技能尚未通过当前发布状态门；不会执行。")
            return
        expected_digest = skill_gate["digest"]
        source_ref = source_choice["artifact_ref"]
        source_value = source_ref.get("artifact") if type(source_ref) is dict else source_label.get().strip()
        expected = ({".csv", ".xlsx"} if skill_id in {
            "table-office-package", "table-office-copy"} else {".docx"})
        if not source_value or Path(source_value).suffix.lower() not in expected:
            skill_status.set("请先选与配方匹配的 CSV/Excel 或 Word 资料。")
            return
        if source_ref is not None and source_label.get() != source_value:
            skill_status.set("资料选择已变化；请重新选用成果。")
            return
        task_root = TASK_ROOT / ("task-" + uuid.uuid4().hex)
        source_generation = source_choice["generation"]
        original_ref = dict(source_ref) if type(source_ref) is dict else None
        skill_gate["enabled"] = False
        notify_goal_external_activity(True, force=True)
        active.update(kind="skill_preparing", process=None, stop=False,
                      task_root=task_root, stop_deadline=None, attempt_id=None,
                      attempt_root=None, history_id=None,
                      verify_deadline=time.monotonic() + 30)
        note_start.configure(state="disabled")
        research_start.configure(state="disabled")
        task_start.configure(state="disabled")
        source_pick.configure(state="disabled")
        source_entry.configure(state="disabled")
        source_clear.configure(state="disabled")
        task_stop.configure(state="normal")
        skill_run.configure(state="disabled")
        skill_status.set("正在核验已发布技能和资料；固定配方不读取任务描述，也不调用模型。")

        def work():
            try:
                state = _published_skill_status(ROOT / "data" / "capabilities", skill_id, TASK_ROOT)
                if (type(state) is not dict or state.get("enabled") is not True
                        or state.get("active") != expected_digest):
                    raise ValueError("skill_disabled")
                if original_ref is not None:
                    from agent_service.artifact_source import prepare_artifact_source
                    fresh = prepare_artifact_source(original_ref["artifact"],
                                                    original_ref["sha256"], TASK_ROOT)
                    if fresh != original_ref:
                        raise ValueError("skill_source_changed")
                    source = {"ref": original_ref}
                else:
                    source = {"path": str(_validate_source_file(source_value))}
                skill_messages.put(("prepare", task_root, skill_id,
                                    (source_generation, source_value, source, expected_digest), None))
            except Exception:
                skill_messages.put(("prepare", task_root, skill_id,
                                    (source_generation, source_value, None, expected_digest),
                                    "skill_prepare_failed"))

        try:
            threading.Thread(target=work, daemon=True, name="jarvis-skill-prepare").start()
        except RuntimeError:
            skill_messages.put(("prepare", task_root, skill_id,
                                (source_generation, source_value, None, expected_digest),
                                "skill_prepare_failed"))

    skill_run = ttk.Button(skill_row, text="运行本地技能（不调用模型）", state="disabled",
                           command=start_skill)
    skill_run.pack(side="left", padx=6)
    skill_list.bind("<<ComboboxSelected>>", refresh_skill_gate)
    ttk.Label(task_tab, textvariable=skill_status, wraplength=620).pack(anchor="w", pady=3)
    ttk.Label(task_tab, text="表格整理按第二列求和，生成 Excel、Word 与本地未发送草稿；表格原样复制仅新建 Excel，"
              "不求和、不生成报告或草稿；Word 副本保留可读取正文和表格；"
              "Word表格转Excel与报告仅提取唯一表格，所有单元格按原文保存，已有合计保留、不重新计算，不复制原文档其他段落。"
              "Word表格第2列转数字求和保留唯一表格的所有原列和明细行，仅将第2列严格数字文本转为数值，"
              "在新Excel求和并生成Word报告；已有合计行或含糊数字不支持，不复制原文档其他段落、原版式，不生成草稿。"
              "本地技能仅使用所选资料，忽略上方任务描述。",
              wraplength=620).pack(anchor="w", pady=2)
    budget_status = tk.StringVar(value="正在读取模型预算……")
    ttk.Label(task_tab, textvariable=budget_status, wraplength=620).pack(anchor="w", pady=4)
    initial_status = (f"已载入资料路径：{initial_source}。填写任务后再点击开始。"
                      if isinstance(initial_source, Path) else
                      f"资料路径未载入：{initial_source}。可在任务页重新选择或粘贴路径。"
                      if isinstance(initial_source, ValueError) else
                      "支持公开资料研究、记事本，以及经核验的 Word 文档和 Excel 表格任务。")
    task_status = tk.StringVar(value=initial_status)
    ttk.Label(task_tab, textvariable=task_status, wraplength=620).pack(anchor="w", pady=6)

    def refresh_budget():
        budget_clock["updated"] = time.monotonic()
        try:
            from agent_service.budget import BudgetLedger
            budget = BudgetLedger().snapshot()
            budget_status.set(f"每日模型预算 ¥5.00 · 今日计账 ¥{budget['charged_micro']/1e6:.4f}"
                              f" · 可用 ¥{budget['remaining_micro']/1e6:.4f}"
                              + (f" · 待核费用 ¥{budget['held_micro']/1e6:.4f}" if budget['held_micro'] else ""))
        except Exception:
            budget_status.set("费用账本暂不可读；模型任务会在预算检查失败时停止。")

    def load_history():
        invalidate_history_batch_source(
            "历史列表正在刷新；原历史批次已失效，请重新检查并选择。")
        history_scan["generation"] += 1
        generation = history_scan["generation"]
        only_gaps = gap_only.get()
        if active["kind"] not in {"resume", "resume_verifying"}:
            history_selected["id"] = None
            history_selected["generation"] += 1
            history_selected["inspection"] = None
            gap_state.update(generation=gap_state["generation"] + 1, pending=False,
                             deadline=None, preview=None, preparing=False,
                             prepare_deadline=None, prepared=False)
            history_resume.configure(state="disabled")
            history_skill_resume.configure(state="disabled")
            history_bundle_resume.configure(state="disabled")
            history_batch_button.configure(state="disabled")
            history_prepare.configure(state="disabled")
            history_gap_cancel.configure(state="disabled")
            history_detail.set("正在读取本机任务目录……")
            history_candidate_detail.set("")
        def work():
            try:
                from agent_service.task_history import list_tasks
                records = list_tasks(TASK_ROOT, limit=200 if only_gaps else 30)
                if only_gaps:
                    records = [item for item in records if item.get("state") == "unsupported"]
                history_messages.put(("list", generation, records))
            except Exception as exc:
                history_messages.put(("list_error", generation, type(exc).__name__))
        threading.Thread(target=work, daemon=True, name="jarvis-history-list").start()

    def inspect_history_selection(_event=None):
        if active["kind"] in {"resume", "resume_verifying"}:
            owned_id = active.get("history_id")
            if owned_id and history_list.selection() != (owned_id,):
                history_list.selection_set(owned_id)
            return
        selected = history_list.selection()
        if not selected:
            invalidate_history_batch_source(
                "历史选择已清除；原历史批次已失效，请重新选择并核验。")
            history_selected["generation"] += 1
            history_selected["id"] = None
            history_selected["inspection"] = None
            history_artifacts.clear()
            history_output_list.configure(values=())
            history_output_choice.set("")
            history_batch_button.configure(state="disabled")
            return
        task_id = selected[0]
        record = history_records.get(task_id)
        if not record:
            return
        invalidate_history_batch_source(
            "历史选择已变化；原历史批次已失效，请重新选择并核验。")
        history_selected["id"] = task_id
        history_selected["generation"] += 1
        gap_state.update(generation=gap_state["generation"] + 1, pending=False,
                         deadline=None, preview=None, preparing=False,
                         prepare_deadline=None, prepared=False)
        generation = history_selected["generation"]
        history_selected["inspection"] = None
        history_artifacts.clear()
        history_output_list.configure(values=())
        history_output_choice.set("")
        history_open.configure(state="disabled")
        history_dir.configure(state="disabled")
        history_resume.configure(state="disabled")
        history_skill_resume.configure(state="disabled")
        history_bundle_resume.configure(state="disabled")
        history_batch_button.configure(state="disabled")
        history_prepare.configure(state="disabled")
        history_gap_cancel.configure(state="disabled")
        history_detail.set(f"{_history_state_label(record.get('state'))} · 正在后台核验成果：{record['root']}")
        history_candidate_detail.set("")
        def work():
            try:
                from agent_service.task_history import inspect_task
                result = inspect_task(record["root"], TASK_ROOT)
                history_messages.put(("inspect", (task_id, generation), result))
            except Exception as exc:
                history_messages.put(("inspect_error", (task_id, generation), type(exc).__name__))
        threading.Thread(target=work, daemon=True, name="jarvis-history-inspect").start()

    def open_history_path(directory=False):
        record = history_artifacts.get(history_output_choice.get())
        if record is None:
            return
        path, digest = record
        path = _verified_history_path(str(path), digest)
        if path is None:
            history_detail.set("待核验 · 成果在检查后已更改或不可用，未打开。")
            return
        open_artifact(path, directory=directory, status=history_detail)

    def update_source_use_buttons():
        current_label = agent_output_choice.get()
        current_path = agent_outputs.get(current_label)
        current_digest = agent_output_hashes.get(current_label)
        historical = history_artifacts.get(history_output_choice.get())
        available = active["kind"] is None and not source_choice["pending"]
        task_use_source.configure(state="normal" if available and current_path is not None
                                  and current_path.suffix.lower() in {".docx", ".xlsx"}
                                  and isinstance(current_digest, str) else "disabled")
        history_use_source.configure(state="normal" if available and historical is not None
                                     and historical[0].suffix.lower() in {".docx", ".xlsx"}
                                     and history_selected["inspection"] is not None else "disabled")

    def select_generated_source(origin):
        if active["kind"] is not None or source_choice["pending"]:
            return
        if origin == "current":
            label = agent_output_choice.get()
            path, digest = agent_outputs.get(label), agent_output_hashes.get(label)
            selection = (label, str(path), digest)
        else:
            label = history_output_choice.get()
            record = history_artifacts.get(label)
            if record is None or history_selected["inspection"] is None:
                return
            path, digest = record
            selection = (history_selected["id"], history_selected["generation"],
                         label, str(path), digest)
        if (path is None or path.suffix.lower() not in {".docx", ".xlsx"}
                or not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None):
            task_status.set("所选成果不是可核验的 Word 或 Excel 文件。")
            return
        source_choice["generation"] += 1
        generation = source_choice["generation"]
        source_choice["pending"] = True
        source_choice["pending_deadline"] = time.monotonic() + 30
        update_source_use_buttons()
        tabs.select(task_tab)
        task_status.set("正在后台核验成果与原办公步骤；核验后才会用于下一任务，当前不会调用模型。")

        def work():
            try:
                from agent_service.artifact_source import prepare_artifact_source
                ref = prepare_artifact_source(path, digest, TASK_ROOT)
                source_ref_messages.put(("select", generation, origin, selection, ref, None))
            except Exception:
                source_ref_messages.put(("select", generation, origin, selection, None,
                                         "source_artifact_invalid"))

        try:
            threading.Thread(target=work, daemon=True,
                             name="jarvis-artifact-source-select").start()
        except RuntimeError:
            source_ref_messages.put(("select", generation, origin, selection, None,
                                     "source_artifact_unavailable"))

    def stage_next_office_source():
        """Stage a visible Office default after success, without starting a task."""
        if active['kind'] is not None or source_choice['pending']:
            return False
        labels = [label for label, path in agent_outputs.items()
                  if path.suffix.lower() in {'.docx', '.xlsx'}]
        if not labels:
            return False
        label = agent_output_choice.get()
        if label not in labels:
            label = labels[0]
        # A failed automatic check must never leave an older source selected.
        clear_source_file()
        source_choice['auto_required'] = True
        source_choice['path'] = str(agent_outputs[label])
        source_choice['setting'] = True
        try:
            source_label.set(source_choice['path'])
        finally:
            source_choice['setting'] = False
        source_clear.configure(state='normal')
        agent_output_choice.set(label)
        select_generated_source('current')
        return True

    def accept_goal_artifacts(records):
        # Called on the Tk thread after the goal worker's receipt/history checks.
        # Next-task selection still uses the existing asynchronous revalidation.
        if active['kind'] is not None or source_choice['pending']:
            raise ValueError('goal_handoff_busy')
        choices = {}
        for n, record in enumerate(records, 1):
            path = _saved_path(record.get('path'), TASK_ROOT, record.get('sha256'))
            if path is None or path.suffix.lower() not in {'.docx', '.xlsx'}:
                raise ValueError('goal_artifact_changed')
            label = f"连续任务 {n} · {'Word' if path.suffix.lower() == '.docx' else 'Excel'}"
            choices[label] = (path, record['sha256'])
        if not choices:
            return
        agent_outputs.clear()
        agent_output_hashes.clear()
        for label, (path, sha) in choices.items():
            agent_outputs[label] = path
            agent_output_hashes[label] = sha
        agent_output_list.configure(values=tuple(choices))
        last = next(reversed(choices))
        agent_output_choice.set(last)
        last_paths['agent'] = choices[last][0]
        task_open.configure(state='normal')
        task_dir.configure(state='normal')
        select_generated_source('current')
        load_history()

    def accept_resume_artifacts(outputs):
        # Batch/path/hash checks ran in finish_resume's background worker.
        if not outputs or source_choice['pending'] or active['kind'] is not None:
            return False
        agent_outputs.clear()
        agent_output_hashes.clear()
        for label, path, digest in outputs:
            agent_outputs[label] = path
            agent_output_hashes[label] = digest
        agent_output_list.configure(values=tuple(agent_outputs))
        office_labels = [label for label, path in agent_outputs.items()
                         if path.suffix.lower() in {'.docx', '.xlsx'}]
        label = office_labels[-1] if office_labels else next(iter(agent_outputs))
        agent_output_choice.set(label)
        last_paths['agent'] = agent_outputs[label]
        task_open.configure(state='normal')
        task_dir.configure(state='normal')
        if office_labels:
            return stage_next_office_source()
        return False

    goal_panel.on_artifacts = accept_goal_artifacts
    goal_panel.get_artifacts = lambda: [
        {'path': str(path), 'sha256': agent_output_hashes.get(label)}
        for label, path in agent_outputs.items() if path.suffix.lower() in {'.docx', '.xlsx'}]

    def edit_history_outputs():
        if (active['kind'] is not None or source_choice['pending'] or goal_panel.running
                or history_selected['inspection'] is None):
            return
        records = [{'path': str(path), 'sha256': sha}
                   for path, sha in history_artifacts.values() if path.suffix.lower() in {'.docx', '.xlsx'}]
        tabs.select(goal_panel.tab)
        goal_panel.use_generated_artifacts(records)

    def select_history_artifact_batch():
        if (active['kind'] is not None or source_choice['pending'] or goal_panel.running
                or history_selected['inspection'] is None):
            return
        records = [{'path': str(path), 'sha256': sha}
                   for path, sha in history_artifacts.values()
                   if path.suffix.lower() in {'.docx', '.xlsx'}]
        if not 2 <= len(records) <= 4:
            history_detail.set('整批能力来源需要同一历史任务的 2 至 4 份 Word/Excel 成果；没有截断或混入其他任务。')
            return
        task_id = history_selected['id']
        selection_generation = history_selected['generation']
        inspection = history_selected['inspection']

        def is_current():
            return (active['kind'] in (None, 'computer_goal') and not source_choice['pending']
                    and history_selected['id'] == task_id
                    and history_selected['generation'] == selection_generation
                    and history_selected['inspection'] is inspection)

        if is_current():
            tabs.select(goal_panel.tab)
            if not goal_panel.use_history_artifact_batch(task_id, records, is_current):
                history_detail.set('历史批次选择已变化或成果数量不符合要求；请重新检查后选择。')

    ttk.Label(note_tab, text="输入要写入新记事本文档的文字：").pack(anchor="w")
    note_text = tk.Text(note_tab, height=11, wrap="word")
    note_text.pack(fill="both", expand=True, pady=8)
    note_status = tk.StringVar(value="准备就绪。点击开始后，请在执行窗口继续。")
    ttk.Label(note_tab, textvariable=note_status, wraplength=620).pack(anchor="w", pady=5)

    ttk.Label(research_tab, text="输入公开资料检索主题：").pack(anchor="w")
    topic = ttk.Entry(research_tab)
    topic.pack(fill="x", pady=8)
    count_row = ttk.Frame(research_tab)
    count_row.pack(anchor="w")
    ttk.Label(count_row, text="来源数量：").pack(side="left")
    count = tk.IntVar(value=2)
    ttk.Combobox(count_row, textvariable=count, values=(1, 2, 3), width=5, state="readonly").pack(side="left")
    ttk.Label(research_tab, text="仅保存网页原文摘录和来源；不进行模型归纳或独立事实核查。",
              wraplength=620).pack(anchor="w", pady=12)
    research_status = tk.StringVar(value="准备就绪。")
    ttk.Label(research_tab, textvariable=research_status, wraplength=620).pack(anchor="w", pady=8)

    history_records = {}
    history_artifacts = {}
    history_selected = {"id": None, "generation": 0, "inspection": None}
    history_scan = {"generation": 0}
    gap_state = {"generation": 0, "pending": False, "deadline": None,
                 "preview": None, "preparing": False, "prepare_deadline": None,
                 "prepared": False}
    gap_only = tk.BooleanVar(value=False)
    ttk.Label(history_tab, text="默认显示最近 30 条任务；勾选缺口筛选时读取最近 200 条中的未支持记录。选择后只读核验。",
              wraplength=620).pack(anchor="w", pady=(0, 8))
    ttk.Checkbutton(history_tab, text="仅看未支持任务／能力缺口", variable=gap_only,
                    command=lambda: load_history()).pack(anchor="w", pady=(0, 5))
    history_list = ttk.Treeview(history_tab, columns=("state", "kind", "date"), show="headings", height=10)
    history_list.heading("state", text="状态")
    history_list.heading("kind", text="类型")
    history_list.heading("date", text="时间")
    history_list.column("state", width=152, stretch=False)
    history_list.column("kind", width=100, stretch=False)
    history_list.column("date", width=150, stretch=False)
    history_list.pack(fill="both", expand=True)
    history_list.bind("<<TreeviewSelect>>", inspect_history_selection)
    history_detail = tk.StringVar(value="点击刷新读取本机任务记录。")
    ttk.Label(history_tab, textvariable=history_detail, wraplength=620).pack(anchor="w", pady=6)
    history_candidate_detail = tk.StringVar(value="")
    ttk.Label(history_tab, textvariable=history_candidate_detail, wraplength=620).pack(anchor="w", pady=3)
    history_row = ttk.Frame(history_tab)
    history_row.pack(fill="x", anchor="w")
    history_refresh = ttk.Button(history_row, text="刷新", command=lambda: load_history())
    history_refresh.pack(side="left")
    history_output_choice = tk.StringVar()
    history_output_list = ttk.Combobox(history_row, textvariable=history_output_choice,
                                       values=(), width=24, state="readonly")
    history_output_list.pack(side="left", padx=6)
    history_open = ttk.Button(history_row, text="打开已核验成果", state="disabled",
                              command=lambda: open_history_path())
    history_open.pack(side="left")
    history_dir = ttk.Button(history_row, text="打开目录", state="disabled",
                             command=lambda: open_history_path(directory=True))
    history_dir.pack(side="left", padx=6)
    history_use_source = ttk.Button(history_row, text="用于下一任务", state="disabled",
                                    command=lambda: select_generated_source("history"))
    history_use_source.pack(side="left", padx=6)
    ttk.Button(history_row, text="继续编辑", command=edit_history_outputs).pack(side="left")
    history_output_list.bind("<<ComboboxSelected>>", lambda _event: update_source_use_buttons())
    history_resume_row = ttk.Frame(history_tab)
    history_resume_row.pack(fill="x", anchor="w", pady=6)
    history_resume = ttk.Button(history_resume_row, text="继续剩余办公步骤", state="disabled",
                                command=lambda: start_resume(skill=False))
    history_resume.pack(side="left")
    history_skill_resume = ttk.Button(history_resume_row, text="继续剩余技能步骤", state="disabled",
                                      command=lambda: start_resume(skill=True))
    history_skill_resume.pack(side="left", padx=6)
    history_bundle_resume = ttk.Button(history_resume_row, text="继续联合核对步骤", state="disabled",
                                       command=lambda: start_resume(resume_kind="bundle_resume"))
    history_bundle_resume.pack(side="left", padx=6)
    history_batch_row = ttk.Frame(history_tab)
    history_batch_row.pack(fill="x", anchor="w", pady=3)
    history_batch_button = ttk.Button(
        history_batch_row, text="整批用于能力模式", state="disabled",
        command=lambda: select_history_artifact_batch())
    history_batch_button.pack(side="left")
    history_prepare = ttk.Button(history_resume_row, text="查看候选／准备新任务", state="disabled",
                                 command=lambda: prepare_from_history())
    history_prepare.pack(side="left", padx=6)
    history_gap_cancel = ttk.Button(history_resume_row, text="取消缺口核验", state="disabled",
                                    command=lambda: cancel_gap_check())
    history_gap_cancel.pack(side="left", padx=6)
    history_stop = ttk.Button(history_resume_row, text="停止继续执行", state="disabled",
                              command=lambda: stop_resume())
    history_stop.pack(side="left", padx=6)

    def launch(kind, command, task_root=None, attempt_id=None, history_id=None,
               initial_stop=False, resume_kind=None):
        notify_goal_external_activity(True, force=True)
        active.update(kind=kind, process=None, stop=initial_stop, task_root=task_root,
                      stop_deadline=time.monotonic() + 3 if initial_stop else None,
                      attempt_id=attempt_id,
                      attempt_root=task_root / (("skill-resume-" if resume_kind == "skill_resume"
                                                else "resume-") + attempt_id) if attempt_id else None,
                      history_id=history_id, verify_deadline=None, resume_kind=resume_kind)
        note_start.configure(state="disabled")
        research_start.configure(state="disabled")
        task_start.configure(state="disabled")
        source_pick.configure(state="disabled")
        source_entry.configure(state="disabled")
        source_clear.configure(state="disabled")
        research_stop.configure(state="normal" if kind == "research" else "disabled")
        task_stop.configure(state="normal" if kind in {"agent", "mail_export"}
                            and not initial_stop else "disabled")
        history_resume.configure(state="disabled")
        history_skill_resume.configure(state="disabled")
        history_bundle_resume.configure(state="disabled")
        history_prepare.configure(state="disabled")
        history_stop.configure(state="normal" if kind == "resume" else "disabled")
        options = dict(cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       text=True, encoding="utf-8", errors="replace")
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW

        def work():
            try:
                process = subprocess.Popen(command, **options)
                active["process"] = process
                if active["stop"] and process.poll() is None:
                    if kind in {"agent", "resume", "mail_export"}:
                        deadline = active.get("stop_deadline") or time.monotonic()
                        threading.Thread(target=terminate_agent_after_grace,
                                         args=(process, deadline), daemon=True).start()
                    else:
                        process.terminate()
                try:
                    output, _ = process.communicate(timeout=960 if kind in {"agent", "resume", "mail_export"} else 180 if kind == "research" else None)
                    messages.put((kind, process.returncode, output, False))
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        output, _ = process.communicate(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        output, _ = process.communicate(timeout=5)
                    messages.put((kind, process.returncode, output, True))
            except (OSError, subprocess.SubprocessError) as exc:
                messages.put((kind, 1, type(exc).__name__, False))

        try:
            threading.Thread(target=work, daemon=True, name="jarvis-" + kind).start()
        except RuntimeError:
            messages.put((kind, 1, "thread_start_failed", False))

    def start_note(parent=None):
        if active["kind"]:
            return
        note_parent["root"] = None
        from agent_service.notepad import validate_text
        try:
            text = validate_text(note_text.get("1.0", "end-1c"))
            REQUESTS.mkdir(parents=True, exist_ok=True)
            request = REQUESTS / (uuid.uuid4().hex + ".txt")
            with request.open("x", encoding="utf-8", newline="") as file:
                file.write(text)
                file.flush()
                os.fsync(file.fileno())
        except (ValueError, OSError) as exc:
            note_status.set("无法准备文字：" + str(exc))
            return
        last_paths["notepad"] = None
        note_open.configure(state="disabled")
        note_dir.configure(state="disabled")
        note_status.set("执行窗口已打开；请点击其中的开始按钮。")
        note_parent["root"] = parent
        launch("notepad", [PYTHON, str(ROOT / "run_jarvis_desktop.py"),
                           "--text-file", str(request), "--save", "--autoclose"])

    def start_research():
        if active["kind"]:
            return
        value = topic.get().strip()
        if not 1 <= len(value) <= 500:
            research_status.set("主题需为 1 至 500 个字符。")
            return
        last_paths["research"] = None
        research_open.configure(state="disabled")
        research_dir.configure(state="disabled")
        research_status.set("Jarvis 正在搜索公开网页并保存来源摘录……")
        launch("research", [PYTHON, str(ROOT / "run_jarvis_research.py"), value,
                            "--root", str(RESEARCH_ROOT), "--max-sources", str(count.get())])

    def start_mail_export(snapshot):
        """Accept one already-read mail snapshot; prepare it away from Tk."""
        if (active["kind"] is not None or source_choice["pending"]
                or type(snapshot) is not dict):
            return False
        task_root = TASK_ROOT / ("task-" + uuid.uuid4().hex)
        last_paths["agent"] = None
        agent_outputs.clear()
        agent_output_hashes.clear()
        agent_output_list.configure(values=())
        agent_output_choice.set("")
        task_open.configure(state="disabled")
        task_dir.configure(state="disabled")
        notify_goal_external_activity(True, force=True)
        active.update(kind="mail_export_preparing", process=None, stop=False,
                      task_root=task_root, stop_deadline=None, attempt_id=None,
                      attempt_root=None, history_id=None,
                      verify_deadline=time.monotonic() + 30)
        note_start.configure(state="disabled")
        research_start.configure(state="disabled")
        task_start.configure(state="disabled")
        source_pick.configure(state="disabled")
        source_entry.configure(state="disabled")
        source_clear.configure(state="disabled")
        history_resume.configure(state="disabled")
        history_skill_resume.configure(state="disabled")
        history_bundle_resume.configure(state="disabled")
        task_stop.configure(state="normal")
        tabs.select(task_tab)
        task_status.set("正在准备已读邮件的本机 Word 导出；不调用模型、网络或附件下载。")

        def prepare():
            try:
                from agent_service.mail_export import write_export_request
                digest = write_export_request(task_root, TASK_ROOT, snapshot)
                mail_export_messages.put((task_root, digest, None))
            except Exception as exc:
                code = str(exc) if isinstance(exc, ValueError) else "mail_export_prepare_failed"
                code = code if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code) else "mail_export_prepare_failed"
                mail_export_messages.put((task_root, None, code))

        try:
            threading.Thread(target=prepare, daemon=True, name="jarvis-mail-export-prepare").start()
        except RuntimeError:
            mail_export_messages.put((task_root, None, "mail_export_prepare_failed"))
        return True

    def start_task():
        if active["kind"]:
            return
        raw_text = task_text.get("1.0", "end-1c")
        if source_choice["retry_changed"]:
            source_choice["retry_changed"] = False
            task_status.set("原任务资料绑定已取消。请核对新请求与资料，再次点击开始；当前未调用模型。")
            return
        if (source_choice["retry_ref"] is not None
                and raw_text != source_choice["retry_instruction"]):
            clear_changed_retry_draft()
            return
        text = raw_text.strip()
        if not 1 <= len(text) <= 4000:
            task_status.set("任务描述需为 1 至 4000 个字符。")
            return
        if source_choice["pending"]:
            task_status.set("成果来源仍在核验；请等核验完成后再开始任务。")
            return
        if source_choice["auto_required"]:
            task_status.set("新成果尚未通过来源核验。请重新点击“用于下一任务”，"
                            "或明确清除/更换来源；当前不会按无来源任务执行。")
            return
        retry_ref = source_choice["retry_ref"]
        if retry_ref is not None:
            if (type(retry_ref) is not dict or set(retry_ref) != {"task_root", "token"}
                    or source_label.get() not in {RETRY_SOURCE_LABEL, RETRY_NO_SOURCE_LABEL}
                    or raw_text != source_choice["retry_instruction"]):
                task_status.set("原任务请求绑定已变化；请重新从历史准备。尚未调用模型。")
                return
            task_root = TASK_ROOT / ("task-" + uuid.uuid4().hex)
            generation = source_choice["generation"]
            original_ref = dict(retry_ref)
            history_key = (history_selected["id"], history_selected["generation"])
            notify_goal_external_activity(True, force=True)
            active.update(kind="agent_retry_preparing", process=None, stop=False,
                          task_root=task_root, stop_deadline=None, attempt_id=None,
                          attempt_root=None, history_id=None,
                          verify_deadline=time.monotonic() + 30)
            note_start.configure(state="disabled")
            research_start.configure(state="disabled")
            task_start.configure(state="disabled")
            source_pick.configure(state="disabled")
            source_entry.configure(state="disabled")
            source_clear.configure(state="disabled")
            task_stop.configure(state="normal")
            task_status.set("正在重新核验原未支持请求和来源；通过前不会创建新任务或调用模型。")

            def work():
                try:
                    from agent_service.gap_recovery import prepare_retry
                    fresh = prepare_retry(original_ref["task_root"], TASK_ROOT,
                                          original_ref["token"])
                    source_ref_messages.put(("retry_start", generation, None,
                                             (task_root, original_ref, raw_text, history_key), fresh, None))
                except Exception:
                    source_ref_messages.put(("retry_start", generation, None,
                                             (task_root, original_ref, raw_text, history_key), None,
                                             "gap_retry_invalid"))

            try:
                threading.Thread(target=work, daemon=True,
                                 name="jarvis-gap-start").start()
            except RuntimeError:
                source_ref_messages.put(("retry_start", generation, None,
                                         (task_root, original_ref, raw_text, history_key), None,
                                         "gap_retry_unavailable"))
            return
        artifact_ref = source_choice["artifact_ref"]
        if artifact_ref is not None:
            if (type(artifact_ref) is not dict or source_label.get() != artifact_ref.get("artifact")):
                source_choice["artifact_ref"] = None
                source_choice["auto_required"] = True
                source_clear.configure(state="normal")
                task_status.set("成果来源选择已变化；请重新选用成果。尚未调用模型。")
                return
            task_root = TASK_ROOT / ("task-" + uuid.uuid4().hex)
            generation = source_choice["generation"]
            original_ref = dict(artifact_ref)
            notify_goal_external_activity(True, force=True)
            active.update(kind="agent_source_preparing", process=None, stop=False,
                          task_root=task_root, stop_deadline=None, attempt_id=None,
                          attempt_root=None, history_id=None,
                          verify_deadline=time.monotonic() + 30)
            note_start.configure(state="disabled")
            research_start.configure(state="disabled")
            task_start.configure(state="disabled")
            source_pick.configure(state="disabled")
            source_entry.configure(state="disabled")
            source_clear.configure(state="disabled")
            task_stop.configure(state="normal")
            task_status.set("正在重新核验选用的成果与办公回执；验证通过前不会调用模型。")

            def work():
                try:
                    from agent_service.artifact_source import prepare_artifact_source
                    fresh = prepare_artifact_source(original_ref["artifact"],
                                                    original_ref["sha256"], TASK_ROOT)
                    source_ref_messages.put(("start", generation, None,
                                             (task_root, original_ref, text), fresh, None))
                except Exception:
                    source_ref_messages.put(("start", generation, None,
                                             (task_root, original_ref, text), None,
                                             "source_artifact_invalid"))

            try:
                threading.Thread(target=work, daemon=True,
                                 name="jarvis-artifact-source-start").start()
            except RuntimeError:
                source_ref_messages.put(("start", generation, None,
                                         (task_root, original_ref, text), None,
                                         "source_artifact_unavailable"))
            return
        source_path = None
        if source_label.get().strip():
            try:
                source_path = _validate_source_file(source_label.get())
            except ValueError as exc:
                task_status.set(str(exc) + " 尚未调用模型。")
                return
            source_choice["path"] = str(source_path)
        try:
            REQUESTS.mkdir(parents=True, exist_ok=True)
            TASK_ROOT.mkdir(parents=True, exist_ok=True)
            task_root = TASK_ROOT / ("task-" + uuid.uuid4().hex)
            task_root.mkdir(parents=False, exist_ok=False)
            request = REQUESTS / (uuid.uuid4().hex + ".txt")
            with request.open("x", encoding="utf-8") as file:
                file.write(text)
                file.flush()
                os.fsync(file.fileno())
        except OSError:
            task_status.set("无法准备任务记录或请求；尚未调用模型。")
            return
        last_paths["agent"] = None
        agent_outputs.clear()
        agent_output_hashes.clear()
        agent_output_list.configure(values=())
        agent_output_choice.set("")
        task_open.configure(state="disabled")
        task_dir.configure(state="disabled")
        task_status.set("正在理解任务、查找资料并核对结果……")
        launch("agent", _task_command(request, task_root, source_path), task_root=task_root)

    def stop_research():
        if active["kind"] not in {"research", "agent", "agent_source_preparing",
                                  "agent_retry_preparing",
                                  "skill_preparing",
                                  "mail_export_preparing", "mail_export"}:
            return
        already_stopping = active["stop"]
        if not already_stopping:
            active["stop"] = True
            if active["kind"] in {"agent", "mail_export_preparing", "mail_export"}:
                task_root = active.get("task_root")
                if task_root is not None:
                    try:
                        from agent_service.mail_export import request_export_cancel
                        request_export_cancel(task_root, TASK_ROOT)
                    except OSError:
                        pass
                active["stop_deadline"] = time.monotonic() + 3
        if active["kind"] == "skill_preparing":
            skill_status.set("正在停止本地技能准备；未调用模型。")
            task_stop.configure(state="disabled")
        elif active["kind"] == "agent_source_preparing":
            task_status.set("正在停止成果来源核验；未启动模型任务。")
            task_stop.configure(state="disabled")
        elif active["kind"] == "agent_retry_preparing":
            task_status.set("正在停止原请求核验；未创建新任务或调用模型。")
            task_stop.configure(state="disabled")
        elif active["kind"] in {"mail_export_preparing", "mail_export"}:
            task_status.set("正在停止本机导出；已有任务记录保留。")
            task_stop.configure(state="disabled")
        else:
            (task_status if active["kind"] == "agent" else research_status).set(
                "正在停止任务；已有文件、记录和待核费用保留。")
        process = active["process"]
        if active["kind"] in {"agent", "mail_export"}:
            if process is not None and process.poll() is None and not already_stopping:
                threading.Thread(target=terminate_agent_after_grace,
                                 args=(process, active["stop_deadline"]), daemon=True).start()
        elif process is not None and process.poll() is None:
            # BrowserProcess owns its Edge descendants in a kill-on-close Windows
            # Job. Terminating this one research parent closes that Job handle.
            process.terminate()

    def request_gap_preview():
        task_id = history_selected["id"]
        record = history_records.get(task_id)
        if (type(record) is not dict or history_selected["inspection"] is None
                or history_selected["inspection"].get("state") != "unsupported"):
            return
        gap_state.update(generation=gap_state["generation"] + 1, pending=True,
                         deadline=time.monotonic() + 30, preview=None, prepared=False)
        key = (task_id, history_selected["generation"], gap_state["generation"])
        history_prepare.configure(state="disabled")
        history_gap_cancel.configure(state="normal")
        history_candidate_detail.set("正在只读重核原请求、资料与当前技能候选；候选仅是线索。")

        def work():
            try:
                from agent_service.gap_recovery import preview_retry
                result = preview_retry(record["root"], TASK_ROOT)
                history_messages.put(("gap_preview", key, result))
            except Exception:
                history_messages.put(("gap_preview_error", key, None))

        try:
            threading.Thread(target=work, daemon=True, name="jarvis-gap-preview").start()
        except RuntimeError:
            history_messages.put(("gap_preview_error", key, None))

    def show_gap_preview(preview):
        gap_state["preview"] = preview if type(preview) is dict else None
        history_gap_cancel.configure(state="disabled")
        if type(preview) is not dict:
            history_candidate_detail.set("原任务请求或资料暂不能重核；请刷新历史。没有启动任务。")
            history_prepare.configure(state="disabled")
            return
        candidates = preview.get("candidates")
        rank = {"available": 1, "disabled": 2, "incompatible": 3}
        families = {}
        if type(candidates) is list:
            for item in candidates[:50]:
                if (type(item) is not dict or item.get("status") not in rank
                        or not isinstance(item.get("id"), str)):
                    continue
                priority = (0 if item["status"] == "available"
                            and item.get("newly_available") is True else rank[item["status"]])
                existing = families.get(item["id"])
                if existing is None or priority < existing[0]:
                    families[item["id"]] = (priority, item)
        ordered = sorted(families.values(), key=lambda pair: (pair[0], pair[1]["id"]))
        descriptions = []
        for _, item in ordered[:6]:
            status = item["status"]
            label = ("新可用线索" if status == "available" and item.get("newly_available") is True
                     else "当前可用线索" if status == "available"
                     else "当前停用" if status == "disabled" else "来源不匹配")
            name = item.get("name") if isinstance(item.get("name"), str) else "本地候选"
            descriptions.append(name[:80] + "（" + label + "）")
        available_count = sum(item["status"] == "available" for _, item in ordered)
        detail = (f"已核验 {min(len(candidates), 50) if type(candidates) is list else 0} 个候选版本，"
                  f"覆盖 {len(families)} 个技能家族，其中 {available_count} 个当前可用；"
                  "仅是重新规划的线索，不代表能完成原请求。")
        if descriptions:
            detail += " 候选：" + "；".join(descriptions) + "。"
        eligible = (preview.get("eligible") is True
                    and isinstance(preview.get("instruction"), str)
                    and isinstance(preview.get("token"), str)
                    and re.fullmatch(r"[0-9a-f]{64}", preview["token"]) is not None
                    and preview.get("task_root") == history_records.get(
                        history_selected["id"], {}).get("root"))
        if eligible:
            detail += " 原请求与来源可恢复为草稿；准备不会创建任务或调用模型。"
        else:
            detail += " " + _gap_retry_reason_label(preview.get("reason")) + "；不能恢复旧草稿，请另写新任务。"
        history_candidate_detail.set(detail)
        history_prepare.configure(state="normal" if eligible and active["kind"] is None
                                  and not gap_state["preparing"] else "disabled")

    def prepare_from_history():
        """Navigate to a fresh task draft; never run or replay the selected task."""
        if active["kind"] is not None:
            return
        task_id = history_selected["id"]
        inspection = history_selected["inspection"]
        record = history_records.get(task_id)
        if (type(inspection) is not dict or type(record) is not dict
                or inspection.get("root") != record.get("root")
                or not (type(inspection.get("capability_gap")) is dict
                        or type(inspection.get("input_failure")) is dict)):
            history_prepare.configure(state="disabled")
            return
        if inspection.get("state") == "unsupported":
            preview = gap_state["preview"]
            if (gap_state["pending"] or gap_state["preparing"] or gap_state["prepared"]
                    or type(preview) is not dict or preview.get("eligible") is not True
                    or preview.get("task_root") != record["root"]
                    or not isinstance(preview.get("token"), str)
                    or re.fullmatch(r"[0-9a-f]{64}", preview["token"]) is None):
                history_prepare.configure(state="disabled")
                return
            key = (task_id, history_selected["generation"], gap_state["generation"])
            draft = {"text": task_text.get("1.0", "end-1c"),
                     "source_generation": source_choice["generation"],
                     "source_label": source_label.get(),
                     "artifact_ref": source_choice["artifact_ref"],
                     "retry_ref": source_choice["retry_ref"]}
            gap_state.update(preparing=True, prepare_deadline=time.monotonic() + 30,
                             draft=draft)
            history_prepare.configure(state="disabled")
            history_gap_cancel.configure(state="normal")
            history_candidate_detail.set("正在重新核对旧请求与来源；只准备草稿，不创建任务或调用模型。")

            def work():
                try:
                    from agent_service.gap_recovery import prepare_retry
                    result = prepare_retry(record["root"], TASK_ROOT, preview["token"])
                    history_messages.put(("gap_prepare", key, result))
                except Exception:
                    history_messages.put(("gap_prepare_error", key, None))

            try:
                threading.Thread(target=work, daemon=True, name="jarvis-gap-prepare").start()
            except RuntimeError:
                history_messages.put(("gap_prepare_error", key, None))
            return
        tabs.select(task_tab)
        task_status.set("原任务结果保留。请核对任务描述、重新选择匹配资料与固定配方；"
                        "只有再次点击开始任务或运行本地技能才会创建新任务。")

    def cancel_gap_check():
        if not gap_state["pending"] and not gap_state["preparing"]:
            return
        gap_state.update(generation=gap_state["generation"] + 1, pending=False,
                         deadline=None, preview=None, preparing=False,
                         prepare_deadline=None)
        history_prepare.configure(state="disabled")
        history_gap_cancel.configure(state="disabled")
        history_candidate_detail.set("已取消本次只读缺口核验；原任务和当前草稿不变。")

    def start_resume(*, skill=False, resume_kind=None):
        if active["kind"] is not None:
            return
        task_id = history_selected["id"]
        record = history_records.get(task_id)
        inspection = history_selected["inspection"]
        if not record or type(inspection) is not dict:
            history_resume.configure(state="disabled")
            history_skill_resume.configure(state="disabled")
            history_bundle_resume.configure(state="disabled")
            return
        preview = inspection.get("resume")
        requested_kind = resume_kind or ("skill_resume" if skill else "office_resume")
        preview_kind = preview.get("kind") if type(preview) is dict else None
        if (type(preview) is not dict or preview.get("eligible") is not True
                or (preview_kind != requested_kind and not
                    (requested_kind == "office_resume" and preview_kind is None))
                or inspection.get("original_state", inspection.get("state")) == "succeeded"):
            history_resume.configure(state="disabled")
            history_skill_resume.configure(state="disabled")
            history_bundle_resume.configure(state="disabled")
            return
        try:
            from agent_service.task_history import _base, _task
            task_root = _task(record["root"], _base(TASK_ROOT))
            if (not re.fullmatch(r"task-[0-9a-f]{32}", task_root.name)
                    or inspection.get("root") != str(task_root)
                    or preview.get("task_root") != str(task_root)
                    or task_id != task_root.name):
                raise ValueError("selected_task_changed")
            token = preview["token"]
            attempt_id = uuid.uuid4().hex
            command = ({"skill_resume": _skill_resume_command,
                        "bundle_resume": _bundle_resume_command,
                        "office_resume": _resume_command}[requested_kind])(
                task_root, token, attempt_id)
            attempt_root = task_root / (("skill-resume-" if requested_kind == "skill_resume"
                                        else "resume-") + attempt_id)
            if requested_kind != "bundle_resume":
                attempt_root.mkdir(exist_ok=False)
        except (KeyError, OSError, ValueError):
            history_resume.configure(state="disabled")
            history_skill_resume.configure(state="disabled")
            history_bundle_resume.configure(state="disabled")
            history_detail.set("任务记录已变化或无法建立续跑记录；请重新选择核验。未执行办公步骤。")
            return
        remaining = preview.get("remaining")
        if not isinstance(remaining, int) or remaining < 0:
            remaining = "剩余"
        completed = preview.get("completed")
        total = preview.get("total")
        progress = (f"已有 {completed}/{total} 步核验；" if type(completed) is int
                    and type(total) is int and 0 <= completed <= total else "")
        history_scan["generation"] += 1
        history_selected["generation"] += 1
        label = {"skill_resume": "技能", "bundle_resume": "联合核对", "office_resume": "办公"}[requested_kind]
        history_detail.set(f"{progress}正在继续 {remaining} 个{label}步骤；本次不调用模型 API。原任务结果保留。")
        launch("resume", command, task_root=task_root, attempt_id=attempt_id,
               history_id=task_id, resume_kind=requested_kind)

    def stop_resume():
        if active["kind"] != "resume":
            return
        if not active["stop"]:
            active["stop"] = True
            active["stop_deadline"] = time.monotonic() + 3
            task_root = active.get("task_root")
            attempt_id = active.get("attempt_id")
            attempt_root = active.get("attempt_root")
            deadline = active["stop_deadline"]
            if (task_root is not None and isinstance(attempt_id, str)
                    and attempt_root is not None):
                def mark_when_attempt_exists():
                    if (active.get("kind") != "resume" or not active.get("stop")
                            or active.get("task_root") != task_root
                            or active.get("attempt_id") != attempt_id
                            or active.get("attempt_root") != attempt_root
                            or time.monotonic() > deadline):
                        return
                    prefix = "skill-resume-" if active.get("resume_kind") == "skill_resume" else "resume-"
                    if attempt_root != task_root / (prefix + attempt_id):
                        return
                    try:
                        from agent_service.office_source import _linked
                        if _linked(attempt_root):
                            return
                        if attempt_root.is_dir():
                            marker = attempt_root / "cancel.flag"
                            if not _linked(marker):
                                marker.touch(exist_ok=True)
                            return
                        if attempt_root.exists():
                            return
                        root.after(100, mark_when_attempt_exists)
                    except OSError:
                        history_detail.set("停止标记未能保存；正在停止本次续跑进程。")
                mark_when_attempt_exists()
            process = active["process"]
            if process is not None and process.poll() is None:
                threading.Thread(target=terminate_agent_after_grace,
                                 args=(process, active["stop_deadline"]), daemon=True).start()
        history_stop.configure(state="disabled")
        history_detail.set("正在停止本次续跑；原任务结果和已有成果保留。")

    def terminate_agent_after_grace(process, deadline):
        time.sleep(max(0, deadline - time.monotonic()))
        if process.poll() is None:
            process.terminate()

    def open_artifact(path, directory=False, status=None):
        if path is None or not path.is_file():
            return
        try:
            if not directory and path.suffix.lower() == ".eml":
                draft = _read_draft_preview(path)
                viewer = tk.Toplevel(root)
                viewer.title("邮件草稿 · 尚未发送")
                viewer.geometry("720x540")
                ttk.Label(viewer, text="本地邮件草稿；尚未上传到邮箱或发送。", padding=10).pack(anchor="w")
                ttk.Label(viewer, text="收件人：" + draft["to"], wraplength=680).pack(anchor="w", padx=10)
                ttk.Label(viewer, text="主题：" + draft["subject"], wraplength=680).pack(anchor="w", padx=10, pady=8)
                body = tk.Text(viewer, wrap="word", height=15)
                body.insert("1.0", draft["body"])
                body.configure(state="disabled")
                body.pack(fill="both", expand=True, padx=10)
                ttk.Label(viewer, text="附件：" + ("、".join(draft["attachments"]) or "无"),
                          wraplength=680, padding=10).pack(anchor="w")
                return
            os.startfile(str(path.parent if directory else path))
        except (OSError, ValueError) as exc:
            (status or task_status).set("无法打开：" + str(exc))

    def open_path(kind, directory=False):
        path = last_paths[kind]
        if kind == "agent" and not directory:
            path = agent_outputs.get(agent_output_choice.get(), path)
            digest = agent_output_hashes.get(agent_output_choice.get())
            if digest is not None:
                path = _verified_history_path(str(path), digest, TASK_ROOT)
                if path is None:
                    task_status.set("成果在核验后已更改或不可用，未打开。")
                    return
        open_artifact(path, directory, note_status if kind == "notepad" else task_status if kind == "agent" else research_status)

    task_buttons = ttk.Frame(task_tab)
    task_buttons.pack(anchor="w", pady=10)
    task_start = ttk.Button(task_buttons, text="开始任务", command=start_task)
    task_start.pack(side="left")
    task_stop = ttk.Button(task_buttons, text="停止", state="disabled", command=stop_research)
    task_stop.pack(side="left", padx=6)
    task_open = ttk.Button(task_buttons, text="打开成果", state="disabled", command=lambda: open_path("agent"))
    task_open.pack(side="left")
    agent_output_choice = tk.StringVar()
    agent_output_list = ttk.Combobox(task_buttons, textvariable=agent_output_choice,
                                     values=(), width=24, state="readonly")
    agent_output_list.pack(side="left", padx=6)
    task_dir = ttk.Button(task_buttons, text="打开目录", state="disabled", command=lambda: open_path("agent", True))
    task_dir.pack(side="left", padx=6)
    task_use_source = ttk.Button(task_buttons, text="用于下一任务", state="disabled",
                                 command=lambda: select_generated_source("current"))
    task_use_source.pack(side="left", padx=6)
    agent_output_list.bind("<<ComboboxSelected>>", lambda _event: update_source_use_buttons())

    note_buttons = ttk.Frame(note_tab)
    note_buttons.pack(anchor="w", pady=10)
    note_start = ttk.Button(note_buttons, text="开始记事本任务", command=start_note)
    note_start.pack(side="left")
    note_open = ttk.Button(note_buttons, text="打开成果", state="disabled", command=lambda: open_path("notepad"))
    note_open.pack(side="left", padx=6)
    note_dir = ttk.Button(note_buttons, text="打开目录", state="disabled", command=lambda: open_path("notepad", True))
    note_dir.pack(side="left")
    research_buttons = ttk.Frame(research_tab)
    research_buttons.pack(anchor="w", pady=12)
    research_start = ttk.Button(research_buttons, text="开始查资料", command=start_research)
    research_start.pack(side="left")
    research_stop = ttk.Button(research_buttons, text="停止", state="disabled", command=stop_research)
    research_stop.pack(side="left", padx=6)
    research_open = ttk.Button(research_buttons, text="打开成果", state="disabled", command=lambda: open_path("research"))
    research_open.pack(side="left")
    research_dir = ttk.Button(research_buttons, text="打开目录", state="disabled", command=lambda: open_path("research", True))
    research_dir.pack(side="left", padx=6)

    def show_history_inspection(result):
        """Render one freshly inspected task; never promote stale metadata."""
        if type(result) is not dict or result.get("root") != history_records.get(
                history_selected["id"], {}).get("root"):
            history_detail.set("历史记录无法核验，请刷新后重新选择。")
            history_resume.configure(state="disabled")
            history_skill_resume.configure(state="disabled")
            history_bundle_resume.configure(state="disabled")
            history_prepare.configure(state="disabled")
            history_candidate_detail.set("")
            return False
        history_selected["inspection"] = result
        history_artifacts.clear()
        records = result.get("artifacts", [])
        if isinstance(records, list):
            for item in records:
                if (type(item) is dict and isinstance(item.get("label"), str)
                        and isinstance(item.get("path"), str)
                        and isinstance(item.get("sha256"), str)):
                    label = item["label"]
                    suffix = 2
                    while label in history_artifacts:
                        label = f"{item['label']} [{suffix}]"
                        suffix += 1
                    history_artifacts[label] = (Path(item["path"]), item["sha256"])
        labels = tuple(history_artifacts)
        history_output_list.configure(values=labels)
        history_output_choice.set(labels[0] if labels else "")
        history_open.configure(state="normal" if labels else "disabled")
        history_dir.configure(state="normal" if labels else "disabled")
        office_records = [(path, sha) for path, sha in history_artifacts.values()
                          if path.suffix.lower() in {".docx", ".xlsx"}]
        history_batch_button.configure(
            state="normal" if 2 <= len(office_records) <= 4
            and active["kind"] is None else "disabled")
        original = result.get("original_state", result.get("state"))
        summary = f"原任务：{_original_state_label(original)} · 已核验成果 {len(labels)} 个"
        continuation = result.get("continuation")
        if type(continuation) is dict:
            state = continuation.get("state")
            summary += " · 续跑：" + ("已完成" if state == "succeeded" else _resume_reason_label(state))
        if result.get("preflight_block") in {"office_ui_blocked", "office_ui_unavailable"}:
            summary += " · " + _resume_reason_label(result["preflight_block"])
        preview = result.get("resume")
        eligible = (type(preview) is dict and preview.get("eligible") is True
                    and original != "succeeded"
                    and isinstance(preview.get("token"), str)
                    and re.fullmatch(r"[0-9a-f]{64}", preview["token"]) is not None
                    and active["kind"] is None)
        skill_eligible = eligible and preview.get("kind") == "skill_resume"
        bundle_eligible = eligible and preview.get("kind") == "bundle_resume"
        office_eligible = eligible and preview.get("kind") not in {"skill_resume", "bundle_resume"}
        history_resume.configure(state="normal" if office_eligible else "disabled")
        history_skill_resume.configure(state="normal" if skill_eligible else "disabled")
        history_bundle_resume.configure(state="normal" if bundle_eligible else "disabled")
        if eligible:
            summary += (f" · 可继续剩余 {preview.get('remaining', '?')} "
                        f"{'个技能' if skill_eligible else '个联合核对' if bundle_eligible else '个办公'}步骤；无需模型 API")
        elif original == "succeeded" and result.get("state") == "succeeded":
            summary += " · 已完成；可将成果用于下一任务"
        elif type(preview) is dict and result.get("kind") == "office":
            summary += " · " + _resume_reason_label(preview.get("reason"))
            if (preview.get("kind") != "skill_resume"
                    and type(result.get("input_failure")) is not dict
                    and type(result.get("capability_gap")) is not dict):
                summary += "；若属技能任务，需重新核对候选并新建任务，不直接续跑"
        gap = result.get("capability_gap")
        failure = result.get("input_failure")
        if result.get("state") == "unsupported" and type(gap) is dict:
            candidates = gap.get("candidates")
            shown = []
            if type(candidates) is list:
                for item in candidates[:4]:
                    if type(item) is dict and item.get("status") in {"available", "disabled", "incompatible"}:
                        label = {"available": "记录时可用", "disabled": "当时未启用",
                                 "incompatible": "资料不匹配"}[item["status"]]
                        shown.append(str(item.get("name", "候选技能")) + "（" + label + "）")
            details = "候选仅为原任务记录时的线索，启动前会重新核验发布状态、配方和资料。"
            details += (" 候选：" + "；".join(shown) + "。" if shown else " 没有记录到可用本地候选。")
            if gap.get("development_queued") is True:
                details += " 开发缺口已记录；没有自动安装或执行新代码。"
            history_candidate_detail.set(details)
            summary += " · 原请求未执行；可准备新的任务尝试"
        elif result.get("state") == "failed" and type(failure) is dict:
            history_candidate_detail.set(_skill_input_failure_label(failure.get("code"))
                                         + "；作用尚未开始。请重新选择正确资料或技能，新建任务尝试。"
                                         "原失败结果保留，不能直接续跑。")
            summary += " · 输入检查失败，未开始办公操作"
        else:
            history_candidate_detail.set("")
        history_prepare.configure(state="normal" if active["kind"] is None and (
            result.get("state") == "unsupported" and type(gap) is dict
            or result.get("state") == "failed" and type(failure) is dict) else "disabled")
        history_detail.set(summary + f" · {result['root']}")
        if result.get("state") == "unsupported" and type(gap) is dict:
            request_gap_preview()
        return True

    def finish_resume(task_id, attempt_id, returncode, timed_out, stopped, advisory_error,
                      resume_kind):
        def work():
            try:
                from agent_service.task_history import inspect_task
                task_root = TASK_ROOT / task_id
                if resume_kind == "skill_resume":
                    from agent_service.skill_resume import read_resume_result
                    outcome = read_resume_result(
                        task_root, task_root / ("skill-resume-" + attempt_id), TASK_ROOT)
                elif resume_kind == "bundle_resume":
                    from agent_service.office_bundle_resume import read_resume_result
                    outcome = read_resume_result(task_root, TASK_ROOT, attempt_id)
                else:
                    from agent_service.office_resume import read_resume_result
                    outcome = read_resume_result(task_root, TASK_ROOT, attempt_id)
                inspection = inspect_task(task_root, TASK_ROOT)
                handoff = _resume_handoff_outputs(inspection, outcome, task_root, attempt_id)
                history_messages.put(("resume_final", (task_id, attempt_id),
                                      {"outcome": outcome, "inspection": inspection,
                                       "handoff": handoff,
                                       "returncode": returncode, "timed_out": timed_out,
                                       "stopped": stopped, "advisory_error": advisory_error,
                                       "resume_kind": resume_kind}))
            except Exception as exc:
                history_messages.put(("resume_final_error", (task_id, attempt_id),
                                      type(exc).__name__))
        try:
            threading.Thread(target=work, daemon=True, name="jarvis-resume-reconcile").start()
        except RuntimeError:
            history_messages.put(("resume_final_error", (task_id, attempt_id),
                                  "thread_start_failed"))

    def finish_mail_export(task_root, returncode, timed_out, stopped):
        def work():
            try:
                from agent_service.task_history import inspect_task
                durable = _durable_task_result(task_root, TASK_ROOT)
                inspection = inspect_task(task_root, TASK_ROOT)
                history_messages.put(("mail_export_final", str(task_root),
                                      {"result": durable, "inspection": inspection,
                                       "returncode": returncode, "timed_out": timed_out,
                                       "stopped": stopped}))
            except Exception:
                history_messages.put(("mail_export_final_error", str(task_root), None))
        try:
            threading.Thread(target=work, daemon=True, name="jarvis-mail-export-reconcile").start()
        except RuntimeError:
            history_messages.put(("mail_export_final_error", str(task_root), None))

    def poll_once():
        if time.monotonic() - budget_clock["updated"] >= 5:
            refresh_budget()
        try:
            while True:
                event, key, skill_id, payload, error = skill_messages.get_nowait()
                if event == "status":
                    if (key != skill_gate["generation"] or skill_id != skill_gate["id"]):
                        continue
                    skill_gate["checking"] = False
                    skill_gate["deadline"] = None
                    skill_gate["enabled"] = (type(payload) is dict
                                             and payload.get("enabled") is True)
                    skill_gate["digest"] = payload.get("active") if skill_gate["enabled"] else None
                    outcome = skill_gate.get("outcome")
                    if skill_gate["enabled"]:
                        remaining = payload.get("remaining_runs")
                        message = (f"已发布并通过状态门；剩余 {remaining} 次受限运行。"
                                   "仅使用所选资料，不调用模型。")
                    elif type(payload) is dict and payload.get("contract_mismatch") is True:
                        message = "已发布版本的配方与本界面固定说明不一致；需重新评审，当前禁用。"
                    else:
                        message = ("技能未发布、验收已过期、次数用尽或执行状态待核；"
                                   "当前不可运行，未调用模型。")
                    skill_status.set((outcome + " " if outcome else "") + message)
                elif event == "prepare":
                    if active["kind"] != "skill_preparing" or active.get("task_root") != key:
                        continue
                    source_generation, source_value, source, expected_digest = payload
                    stopped = active["stop"]
                    active.update(kind=None, process=None, stop=False, task_root=None,
                                  stop_deadline=None, attempt_id=None, attempt_root=None,
                                  history_id=None, verify_deadline=None)
                    note_start.configure(state="normal")
                    research_start.configure(state="normal")
                    task_start.configure(state="normal")
                    source_pick.configure(state="normal")
                    source_entry.configure(state="normal")
                    source_clear.configure(state="normal" if source_choice["path"] else "disabled")
                    task_stop.configure(state="disabled")
                    if stopped:
                        skill_status.set("已停止；本地技能尚未运行，未调用模型。")
                        refresh_skill_gate(outcome=skill_status.get())
                        continue
                    if (error is not None or source is None
                            or SKILL_CHOICES.get(skill_choice.get()) != skill_id
                            or source_choice["generation"] != source_generation
                            or source_label.get().strip() != source_value):
                        skill_status.set("技能发布状态或资料在开始前已变化；未执行，"
                                         "请重新选择配方并核验。")
                        refresh_skill_gate(outcome=skill_status.get())
                        continue
                    try:
                        command = _skill_command(skill_id, key,
                                                 source_file=source.get("path"),
                                                 source_artifact=source.get("ref"),
                                                 expected_sha256=expected_digest)
                        from agent_service.artifact_source import prepare_artifact_task_root
                        prepare_artifact_task_root(key, TASK_ROOT)
                    except (ValueError, OSError):
                        skill_status.set("技能参数无法核验；未执行。")
                        refresh_skill_gate(outcome=skill_status.get())
                        continue
                    skill_status.set("本地技能已启动；固定配方不使用任务描述或模型。")
                    task_status.set("本地技能正在按固定配方处理所选资料；不调用模型。")
                    skill_run_root["root"] = key
                    launch("agent", command, task_root=key)
        except queue.Empty:
            pass
        try:
            while True:
                event, generation, origin, selection, ref, error = source_ref_messages.get_nowait()
                if event == "select":
                    if (generation != source_choice["generation"]
                            or not source_choice["pending"]):
                        continue
                    source_choice["pending"] = False
                    source_choice["pending_deadline"] = None
                    if origin == "current":
                        label = agent_output_choice.get()
                        current = (label, str(agent_outputs.get(label)),
                                   agent_output_hashes.get(label))
                    else:
                        label = history_output_choice.get()
                        record = history_artifacts.get(label)
                        current = ((history_selected["id"], history_selected["generation"],
                                    label, str(record[0]), record[1]) if record is not None
                                   else None)
                    if active["kind"] is not None or current != selection:
                        task_status.set("成果选择已变化；请重新选择并核验。")
                        continue
                    expected_path = selection[1] if origin == "current" else selection[3]
                    expected_sha = selection[2] if origin == "current" else selection[4]
                    if (error is not None or type(ref) is not dict
                            or ref.get("artifact") != expected_path
                            or ref.get("sha256") != expected_sha):
                        task_status.set("新成果或办公回执无法核验；未带入该成果。"
                                        "请重新核验，或明确清除来源后再开始新任务。尚未调用模型。")
                        continue
                    source_choice["setting"] = True
                    try:
                        source_label.set(ref["artifact"])
                    finally:
                        source_choice["setting"] = False
                    source_choice["path"] = ref["artifact"]
                    source_choice["artifact_ref"] = dict(ref)
                    source_choice["auto_required"] = False
                    source_choice["retry_ref"] = None
                    source_choice["retry_instruction"] = None
                    source_choice["retry_changed"] = False
                    source_clear.configure(state="normal")
                    draft_note = ("本批邮件草稿仅保存在本机，未发送。"
                                  if origin == "current" and any(
                                      p.suffix.lower() == ".eml" for p in agent_outputs.values()) else "")
                    task_status.set(draft_note + "已选经核验的 Word/Excel 成果。填写新任务后点击开始；"
                                    "届时文件内容会提供给云端模型，下一任务尚未启动。")
                elif event == "start":
                    task_root, original_ref, task_instruction = selection
                    if (active["kind"] != "agent_source_preparing"
                            or active.get("task_root") != task_root
                            or generation != source_choice["generation"]):
                        continue
                    stopped = active["stop"]
                    active.update(kind=None, process=None, stop=False, task_root=None,
                                  stop_deadline=None, attempt_id=None, attempt_root=None,
                                  history_id=None, verify_deadline=None)
                    note_start.configure(state="normal")
                    research_start.configure(state="normal")
                    task_start.configure(state="normal")
                    source_pick.configure(state="normal")
                    source_entry.configure(state="normal")
                    source_clear.configure(state="normal" if source_choice["path"] else "disabled")
                    task_stop.configure(state="disabled")
                    if stopped:
                        task_status.set("已停止；未启动任务或调用模型。")
                        continue
                    if (error is not None or ref != original_ref
                            or source_choice["artifact_ref"] != original_ref
                            or source_label.get() != original_ref.get("artifact")
                            or task_text.get("1.0", "end-1c").strip() != task_instruction):
                        task_status.set("任务内容、成果或办公回执在开始前已变化；未调用模型，请重新选用成果。")
                        source_choice["artifact_ref"] = None
                        source_choice["auto_required"] = True
                        source_clear.configure(state="normal")
                        continue
                    try:
                        from agent_service.artifact_source import prepare_artifact_task_root
                        prepare_artifact_task_root(task_root, TASK_ROOT)
                        request = task_root / "request.txt"
                        with request.open("x", encoding="utf-8") as file:
                            file.write(task_instruction)
                            file.flush()
                            os.fsync(file.fileno())
                    except (OSError, ValueError):
                        task_status.set("无法准备任务记录；尚未调用模型。")
                        continue
                    last_paths["agent"] = None
                    agent_outputs.clear()
                    agent_output_hashes.clear()
                    agent_output_list.configure(values=())
                    agent_output_choice.set("")
                    task_open.configure(state="disabled")
                    task_dir.configure(state="disabled")
                    task_status.set("成果已复核，正在为新任务建立来源快照；之后才会按 ¥5 每日预算调用模型。")
                    launch("agent", _task_command(request, task_root, source_artifact=original_ref),
                           task_root=task_root)
                elif event == "retry_start":
                    task_root, original_ref, task_instruction, history_key = selection
                    if (active["kind"] != "agent_retry_preparing"
                            or active.get("task_root") != task_root
                            or generation != source_choice["generation"]):
                        continue
                    stopped = active["stop"]
                    active.update(kind=None, process=None, stop=False, task_root=None,
                                  stop_deadline=None, attempt_id=None, attempt_root=None,
                                  history_id=None, verify_deadline=None)
                    note_start.configure(state="normal")
                    research_start.configure(state="normal")
                    task_start.configure(state="normal")
                    source_pick.configure(state="normal")
                    source_entry.configure(state="normal")
                    source_clear.configure(state="normal" if source_choice["path"] else "disabled")
                    task_stop.configure(state="disabled")
                    if stopped:
                        task_status.set("已停止原请求核验；未创建新任务或调用模型。")
                        continue
                    if (error is not None or type(ref) is not dict
                            or ref.get("eligible") is not True
                            or ref.get("task_root") != original_ref["task_root"]
                            or ref.get("token") != original_ref["token"]
                            or ref.get("instruction") != task_instruction
                            or history_key != (history_selected["id"],
                                               history_selected["generation"])
                            or source_choice["retry_ref"] != original_ref
                            or source_label.get() not in {RETRY_SOURCE_LABEL,
                                                          RETRY_NO_SOURCE_LABEL}
                            or task_text.get("1.0", "end-1c") != task_instruction):
                        task_status.set("原请求、资料或任务草稿在开始前已变化；未创建新任务或调用模型。"
                                        "请从历史重新核验。")
                        continue
                    try:
                        from agent_service.artifact_source import prepare_artifact_task_root
                        prepare_artifact_task_root(task_root, TASK_ROOT)
                        request = task_root / "request.txt"
                        with request.open("x", encoding="utf-8") as file:
                            file.write(task_instruction)
                            file.flush()
                            os.fsync(file.fileno())
                        command = _task_command(request, task_root, source_retry=original_ref)
                    except (OSError, ValueError):
                        task_status.set("无法建立新任务记录；未调用模型。原失败任务保留。")
                        continue
                    last_paths["agent"] = None
                    agent_outputs.clear()
                    agent_output_hashes.clear()
                    agent_output_list.configure(values=())
                    agent_output_choice.set("")
                    task_open.configure(state="disabled")
                    task_dir.configure(state="disabled")
                    task_status.set("新任务正在重新核验原来源；通过后才按每日 ¥5 预算调用模型。"
                                    "旧未支持任务保持不变。")
                    launch("agent", command, task_root=task_root)
        except queue.Empty:
            pass
        try:
            while True:
                export_root, request_sha256, prepare_error = mail_export_messages.get_nowait()
                if (active["kind"] != "mail_export_preparing"
                        or active.get("task_root") != export_root):
                    continue
                stopped = active["stop"]
                if request_sha256 is None or prepare_error is not None:
                    active.update(kind=None, process=None, stop=False, task_root=None,
                                  stop_deadline=None, attempt_id=None, attempt_root=None,
                                  history_id=None, verify_deadline=None)
                    note_start.configure(state="normal")
                    research_start.configure(state="normal")
                    task_start.configure(state="normal")
                    source_pick.configure(state="normal")
                    source_entry.configure(state="normal")
                    source_clear.configure(state="normal" if source_choice["path"] else "disabled")
                    task_stop.configure(state="disabled")
                    task_status.set(_mail_export_prepare_label(prepare_error)
                                    + " 未执行 Office 操作或调用模型。")
                    continue
                try:
                    command = _mail_export_command(export_root, request_sha256)
                    if stopped:
                        from agent_service.mail_export import request_export_cancel
                        if not request_export_cancel(export_root, TASK_ROOT):
                            raise ValueError("mail_export_task_scope_invalid")
                except (OSError, ValueError):
                    mail_export_messages.put((export_root, None, "mail_export_prepare_failed"))
                    continue
                launch("mail_export", command, task_root=export_root, initial_stop=stopped)
                task_status.set("已读邮件正在本机生成 Word 并等待应用回读；不调用模型或邮箱网络。"
                                if not stopped else "正在停止本机邮件导出；任务记录保留。")
        except queue.Empty:
            pass
        try:
            while True:
                event, task_id, data = history_messages.get_nowait()
                if event == "list":
                    if (task_id != history_scan["generation"]
                            or active["kind"] in {"resume", "resume_verifying"}):
                        continue
                    history_records.clear()
                    for row in history_list.get_children():
                        history_list.delete(row)
                    for record in data:
                        task_id_value = record.get("id")
                        if not isinstance(task_id_value, str):
                            continue
                        history_records[task_id_value] = record
                        timestamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(record.get("mtime", 0)))
                        history_list.insert("", "end", iid=task_id_value,
                                            values=(_history_list_state_label(record.get("state")),
                                                    record.get("kind", "unknown"), timestamp))
                    history_detail.set(f"已读取 {len(history_records)} 条记录。选择一条可只读核验成果。")
                    history_candidate_detail.set("")
                    history_prepare.configure(state="disabled")
                elif event == "list_error":
                    if (task_id == history_scan["generation"]
                            and active["kind"] not in {"resume", "resume_verifying"}):
                        history_detail.set("历史记录暂不可读：" + str(data))
                elif event == "inspect" and task_id == (
                        history_selected["id"], history_selected["generation"]
                ) and active["kind"] not in {"resume", "resume_verifying"}:
                    show_history_inspection(data)
                elif event == "inspect_error" and task_id == (
                        history_selected["id"], history_selected["generation"]
                ) and active["kind"] not in {"resume", "resume_verifying"}:
                    history_resume.configure(state="disabled")
                    history_skill_resume.configure(state="disabled")
                    history_bundle_resume.configure(state="disabled")
                    history_prepare.configure(state="disabled")
                    history_candidate_detail.set("")
                    history_detail.set(f"待核验 · 后台核验未完成（{data}）；未执行任务。")
                elif event in {"gap_preview", "gap_preview_error"}:
                    if task_id != (history_selected["id"], history_selected["generation"],
                                   gap_state["generation"]) or not gap_state["pending"]:
                        continue
                    show_gap_preview(data if event == "gap_preview" else None)
                    gap_state.update(pending=False, deadline=None)
                elif event in {"gap_prepare", "gap_prepare_error"}:
                    if (task_id != (history_selected["id"], history_selected["generation"],
                                    gap_state["generation"]) or not gap_state["preparing"]):
                        continue
                    draft = gap_state.get("draft")
                    gap_state.update(preparing=False, prepare_deadline=None)
                    history_gap_cancel.configure(state="disabled")
                    if (active["kind"] is not None or type(draft) is not dict
                            or task_text.get("1.0", "end-1c") != draft["text"]
                            or source_choice["generation"] != draft["source_generation"]
                            or source_label.get() != draft["source_label"]
                            or source_choice["artifact_ref"] != draft["artifact_ref"]
                            or source_choice["retry_ref"] != draft["retry_ref"]):
                        history_candidate_detail.set("任务草稿或当前操作已变化；旧准备结果已忽略，现有内容保留。")
                        continue
                    record = history_records.get(history_selected["id"])
                    preview = gap_state["preview"]
                    if (event != "gap_prepare" or type(data) is not dict
                            or type(record) is not dict or type(preview) is not dict
                            or data.get("eligible") is not True
                            or data.get("task_root") != record.get("root")
                            or data.get("token") != preview.get("token")
                            or not isinstance(data.get("instruction"), str)
                            or not 1 <= len(data["instruction"]) <= 4000):
                        gap_state["preview"] = None
                        history_prepare.configure(state="disabled")
                        history_candidate_detail.set("原请求或资料已变化，不能恢复草稿；请刷新历史核验。没有调用模型。")
                        continue
                    task_text.delete("1.0", "end")
                    task_text.insert("1.0", data["instruction"])
                    source_choice["setting"] = True
                    try:
                        source_label.set(RETRY_SOURCE_LABEL if data.get("source_kind") is not None
                                         else RETRY_NO_SOURCE_LABEL)
                    finally:
                        source_choice["setting"] = False
                    source_choice.update(path=source_label.get(), artifact_ref=None,
                                         retry_ref={"task_root": record["root"],
                                                    "token": data["token"]},
                                         retry_instruction=data["instruction"],
                                         retry_changed=False, auto_required=False,
                                         generation=source_choice["generation"] + 1)
                    source_clear.configure(state="normal")
                    gap_state["prepared"] = True
                    history_prepare.configure(state="disabled")
                    update_skill_button()
                    tabs.select(task_tab)
                    task_status.set("原未支持请求已恢复为草稿，旧结果保留。候选仅是线索；"
                                    "点击开始任务才会新建任务、重验来源并按预算调用模型。")
                elif event in {"resume_final", "resume_final_error"}:
                    if (active["kind"] != "resume_verifying" or task_id != (
                            active.get("history_id"), active.get("attempt_id"))):
                        continue
                    selected_id, attempt_id = task_id
                    active.update(kind=None, process=None, stop=False, task_root=None,
                                  stop_deadline=None, attempt_id=None, attempt_root=None,
                                  history_id=None, verify_deadline=None)
                    note_start.configure(state="normal")
                    research_start.configure(state="normal")
                    task_start.configure(state="normal")
                    source_pick.configure(state="normal")
                    source_entry.configure(state="normal")
                    source_clear.configure(state="normal" if source_choice["path"] else "disabled")
                    history_stop.configure(state="disabled")
                    if event == "resume_final_error":
                        history_resume.configure(state="disabled")
                        history_skill_resume.configure(state="disabled")
                        history_bundle_resume.configure(state="disabled")
                        history_detail.set("本次续跑结果尚未能核验；请刷新任务历史。已有文件保留。")
                        continue
                    inspection = data.get("inspection")
                    outcome = data.get("outcome")
                    verified = show_history_inspection(inspection)
                    continuation = inspection.get("continuation") if type(inspection) is dict else None
                    expected_kind = data.get("resume_kind")
                    kind_matches = (type(outcome) is dict and type(continuation) is dict
                                    and ((outcome.get("kind") == expected_kind
                                          and continuation.get("kind") == expected_kind)
                                         if expected_kind in {"skill_resume", "bundle_resume"}
                                         else outcome.get("kind") in {None, "office_resume"}
                                         and continuation.get("kind") in {None, "office_resume"}))
                    complete = (verified and type(outcome) is dict and outcome.get("ok") is True
                                and outcome.get("state") == "succeeded"
                                and type(continuation) is dict
                                and continuation.get("attempt_id") == attempt_id
                                and continuation.get("state") == "succeeded"
                                and kind_matches)
                    if complete:
                        original_state = inspection.get("original_state", inspection.get("state"))
                        history_detail.set(f"原任务：{_original_state_label(original_state)} · "
                                           "本次续跑已完成并重新核验成果。"
                                           f"已核验 {len(history_artifacts)} 个成果。")
                        if not data.get("stopped") and not data.get("timed_out"):
                            if accept_resume_artifacts(data.get("handoff")):
                                history_detail.set(history_detail.get() + "正在带入下一任务，尚未启动新任务。")
                            elif not data.get("handoff"):
                                history_detail.set(history_detail.get() + "自动接力核验未通过，请重新核验成果。")
                    else:
                        state = outcome.get("state") if type(outcome) is dict else "unknown"
                        reason = outcome.get("error", state) if type(outcome) is dict else state
                        if (reason == "unknown" and isinstance(data.get("advisory_error"), str)):
                            reason = data["advisory_error"]
                        if data.get("timed_out"):
                            reason = "timed_out"
                        original_state = (inspection.get("original_state", inspection.get("state"))
                                          if type(inspection) is dict else None)
                        history_detail.set(f"原任务：{_original_state_label(original_state)} · "
                                           "本次续跑未确认完成：" + _resume_reason_label(reason)
                                           + f"。已核验 {len(history_artifacts)} 个成果。")
                elif event in {"mail_export_final", "mail_export_final_error"}:
                    if (active["kind"] != "mail_export_verifying"
                            or task_id != str(active.get("task_root"))):
                        continue
                    active.update(kind=None, process=None, stop=False, task_root=None,
                                  stop_deadline=None, attempt_id=None, attempt_root=None,
                                  history_id=None, verify_deadline=None)
                    note_start.configure(state="normal")
                    research_start.configure(state="normal")
                    task_start.configure(state="normal")
                    source_pick.configure(state="normal")
                    source_entry.configure(state="normal")
                    source_clear.configure(state="normal" if source_choice["path"] else "disabled")
                    task_stop.configure(state="disabled")
                    if event == "mail_export_final_error":
                        task_status.set("本机邮件导出结果尚未能核验；请在历史任务中检查持久记录。")
                        load_history()
                        continue
                    durable = data.get("result")
                    inspection = data.get("inspection")
                    artifacts = inspection.get("artifacts") if type(inspection) is dict else None
                    verified = []
                    if isinstance(artifacts, list):
                        for item in artifacts:
                            if (type(item) is dict and isinstance(item.get("label"), str)
                                    and isinstance(item.get("path"), str)
                                    and isinstance(item.get("sha256"), str)
                                    and Path(item["path"]).suffix.lower() == ".docx"):
                                verified.append(item)
                    success = (type(durable) is dict and durable.get("ok") is True
                               and durable.get("state") == "succeeded"
                               and durable.get("kind") == "office"
                               and durable.get("root") == task_id
                               and type(inspection) is dict
                               and inspection.get("root") == task_id
                               and inspection.get("state") == "succeeded"
                               and len(verified) == 1)
                    if success:
                        item = verified[0]
                        path = Path(item["path"])
                        label = "已读邮件本机 Word · .docx"
                        agent_outputs[label] = path
                        agent_output_hashes[label] = item["sha256"]
                        agent_output_list.configure(values=(label,))
                        agent_output_choice.set(label)
                        last_paths["agent"] = path
                        task_open.configure(state="normal")
                        task_dir.configure(state="normal")
                        if not data.get("stopped") and not data.get("timed_out"):
                            stage_next_office_source()
                            task_status.set("已读邮件已保存为本机 Word 并完成应用回读；正在核验下一任务来源，附件内容未保存。")
                        else:
                            task_status.set("已读邮件已保存为本机 Word 并完成应用回读；收到停止或超时，未自动带入下一任务。")
                    else:
                        task_status.set("本机邮件导出未确认完成；请在历史任务中核验记录。未重新读取邮件或调用模型。")
                    load_history()
        except queue.Empty:
            pass
        try:
            while True:
                kind, returncode, output, timed_out = messages.get_nowait()
                stopped = active["stop"]
                task_root = active["task_root"]
                if kind == "resume":
                    task_id = active.get("history_id")
                    attempt_id = active.get("attempt_id")
                    active["kind"] = "resume_verifying"
                    active["process"] = None
                    active["verify_deadline"] = time.monotonic() + 120
                    history_stop.configure(state="disabled")
                    history_detail.set("本次续跑进程已结束，正在读取持久结果并重新核验文件……")
                    if isinstance(task_id, str) and isinstance(attempt_id, str):
                        advisory = _last_json_lines(output).get("error")
                        advisory = (advisory if isinstance(advisory, str)
                                    and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", advisory)
                                    else None)
                        finish_resume(task_id, attempt_id, returncode, timed_out,
                                      stopped, advisory, active.get("resume_kind"))
                    else:
                        history_messages.put(("resume_final_error", (task_id, attempt_id),
                                              "missing_attempt"))
                    continue
                if kind == "mail_export":
                    active["kind"] = "mail_export_verifying"
                    active["process"] = None
                    active["verify_deadline"] = time.monotonic() + 120
                    task_stop.configure(state="disabled")
                    task_status.set("本机 Word 导出进程已结束，正在核验持久结果和文件回读……")
                    if task_root is not None:
                        finish_mail_export(task_root, returncode, timed_out, stopped)
                    else:
                        history_messages.put(("mail_export_final_error", "", None))
                    continue
                active.update(kind=None, process=None, stop=False, task_root=None)
                note_start.configure(state="normal")
                research_start.configure(state="normal")
                task_start.configure(state="normal")
                source_pick.configure(state="normal")
                source_entry.configure(state="normal")
                source_clear.configure(state="normal" if source_choice["path"] else "disabled")
                research_stop.configure(state="disabled")
                task_stop.configure(state="disabled")
                preview = (history_selected["inspection"] or {}).get("resume")
                original_state = ((history_selected["inspection"] or {}).get("original_state")
                                  or (history_selected["inspection"] or {}).get("state"))
                can_resume = (type(preview) is dict and preview.get("eligible") is True
                              and original_state != "succeeded")
                skill_preview = can_resume and preview.get("kind") == "skill_resume"
                bundle_preview = can_resume and preview.get("kind") == "bundle_resume"
                office_preview = can_resume and preview.get("kind") in {None, "office_resume"}
                history_resume.configure(state="normal" if office_preview
                                         else "disabled")
                history_skill_resume.configure(state="normal" if skill_preview else "disabled")
                history_bundle_resume.configure(state="normal" if bundle_preview else "disabled")
                refresh_budget()
                if kind == "notepad":
                    result = _last_json_lines(output)
                    path = _desktop_saved_path(result, returncode)
                    if path:
                        last_paths[kind] = path
                        note_open.configure(state="normal")
                        note_dir.configure(state="normal")
                        note_status.set("已保存并回读：" + str(path))
                    else:
                        note_status.set("记事本任务未确认保存：" + str(result.get("code") or result.get("state") or "无有效结果"))
                    if note_parent["root"]:
                        try:
                            outcome = _record_desktop_outcome(note_parent["root"], result, returncode=returncode)
                            task_status.set("记事本已保存并回读。" if outcome["ok"] else "记事本任务未确认完成，执行记录已保留。")
                        except (OSError, ValueError):
                            task_status.set("记事本结果见记事本页；上层任务记录未能落盘。")
                        if path:
                            last_paths["agent"] = path
                            agent_outputs.clear()
                            agent_output_hashes.clear()
                            agent_outputs["Notepad document · .txt"] = path
                            agent_output_list.configure(values=tuple(agent_outputs))
                            agent_output_choice.set("Notepad document · .txt")
                            task_open.configure(state="normal")
                            task_dir.configure(state="normal")
                        note_parent["root"] = None
                elif kind == "agent":
                    durable = _durable_task_result(task_root, TASK_ROOT) if task_root else {}
                    result = durable or _last_json_lines(output)
                    was_skill = skill_run_root["root"] == task_root and task_root is not None
                    if was_skill:
                        skill_run_root["root"] = None
                    if (not stopped and not timed_out and returncode == 0 and result.get("ok") is True
                            and result.get("kind") == "notepad" and result.get("state") == "prepared"):
                        text = result.get("text")
                        if isinstance(text, str):
                            note_text.delete("1.0", "end")
                            note_text.insert("1.0", text)
                            task_status.set("已理解写入内容，正在交给记事本执行。最终结果见记事本页。")
                            tabs.select(note_tab)
                            start_note(parent=task_root)
                        else:
                            task_status.set("模型未返回有效写入内容；未执行记事本操作。")
                        continue
                    office_outputs = _office_artifacts(result, task_root) if durable and task_root else []
                    if office_outputs:
                        agent_outputs.clear()
                        agent_output_hashes.clear()
                        for label, path in office_outputs:
                            agent_outputs[label] = path
                            for item in result.get("artifacts", []):
                                if (type(item) is dict and item.get("path") == str(path)
                                        and isinstance(item.get("sha256"), str)
                                        and re.fullmatch(r"[0-9a-f]{64}", item["sha256"])):
                                    agent_output_hashes[label] = item["sha256"]
                                    break
                        agent_output_list.configure(values=tuple(agent_outputs))
                        agent_output_choice.set(next(iter(agent_outputs)))
                        last_paths[kind] = next(iter(agent_outputs.values()))
                        task_open.configure(state="normal")
                        task_dir.configure(state="normal")
                    if result.get("kind") == "office":
                        if office_outputs:
                            completed = result.get("completed")
                            done_count = len(completed) if isinstance(completed, list) else 0
                            if result.get("ok") is True and result.get("state") == "succeeded":
                                task_status.set(f"已核验 {len(office_outputs)} 个文件；完成步骤 {done_count} 个。邮件草稿均未发送。" if any(path.suffix.lower() == ".eml" for _, path in office_outputs)
                                                else f"办公工作流已核验 {len(office_outputs)} 个文件；完成步骤 {done_count} 个。选择文件后可打开。")
                            else:
                                reason = result.get("error") or result.get("state") or "未知状态"
                                if reason in {"office_ui_blocked", "office_ui_unavailable"}:
                                    reason = _resume_reason_label(reason)
                                task_status.set(f"部分完成：已核验 {len(office_outputs)} 个文件，后续步骤未完成（{reason}）。已有成果可以打开。")
                        else:
                            reason = "已停止" if stopped else "超时" if timed_out else result.get("error") or result.get("state") or "没有核验文件"
                            if reason in {"office_ui_blocked", "office_ui_unavailable"}:
                                reason = _resume_reason_label(reason)
                            task_status.set(f"Office 任务未完成：{reason}。任务记录：{task_root}")
                        if (result.get("ok") is True and result.get("state") == "succeeded"
                                and office_outputs and not stopped and not timed_out):
                            stage_next_office_source()
                        if was_skill:
                            skill_status.set("本地技能成果已保存并核验；不调用模型。"
                                             if result.get("ok") is True and office_outputs
                                             else "本地技能未确认完成；请在历史任务核验，"
                                                  "正在重新检查发布状态。")
                            refresh_skill_gate(outcome=skill_status.get())
                            load_history()
                        continue
                    if was_skill:
                        skill_status.set("本地技能未确认完成；请在历史任务核验，"
                                         "正在重新检查发布状态。")
                        refresh_skill_gate(outcome=skill_status.get())
                        load_history()
                    path = (_saved_path(result.get("artifact"), TASK_ROOT, result.get("readback_sha256"))
                            if (durable or (not stopped and not timed_out and returncode == 0)) and result.get("ok") is True
                            and result.get("state") == "succeeded" else None)
                    if path:
                        last_paths[kind] = path
                        agent_outputs.clear()
                        agent_output_hashes.clear()
                        agent_outputs["Research summary · .md"] = path
                        agent_output_list.configure(values=tuple(agent_outputs))
                        agent_output_choice.set("Research summary · .md")
                        task_open.configure(state="normal")
                        task_dir.configure(state="normal")
                        task_status.set("带来源的归纳文档已保存并回读：" + str(path))
                    else:
                        reason = "已停止" if stopped else "超时" if timed_out else result.get("error") or result.get("reason") or result.get("state") or "无有效结果"
                        task_status.set(f"任务未完成：{reason}。任务记录：{task_root}")
                else:
                    try:
                        result = json.loads(output)
                    except ValueError:
                        result = {}
                    if type(result) is not dict:
                        result = {}
                    path = (_saved_path(result.get("artifact"), RESEARCH_ROOT, result.get("readback_sha256"))
                            if not stopped and not timed_out and returncode == 0 and result.get("ok") is True
                            and result.get("state") == "succeeded" else None)
                    if path:
                        last_paths[kind] = path
                        research_open.configure(state="normal")
                        research_dir.configure(state="normal")
                        sources = result.get("sources")
                        count_sources = len(sources) if isinstance(sources, list) else 0
                        research_status.set(f"来源摘录已保存并回读（{count_sources} 个来源）：{path}")
                    else:
                        reason = "已停止" if stopped else "超时" if timed_out else result.get("error") or result.get("reason") or "无有效结果"
                        research_status.set("查资料未完成：" + str(reason))
        except queue.Empty:
            pass
        if (source_choice["pending"] and source_choice["pending_deadline"] is not None
                and time.monotonic() >= source_choice["pending_deadline"]):
            source_choice["pending"] = False
            source_choice["pending_deadline"] = None
            source_choice["generation"] += 1
            task_status.set("成果来源核验超时；请重新选择。尚未调用模型。")
        if (gap_state["pending"] and gap_state["deadline"] is not None
                and time.monotonic() >= gap_state["deadline"]):
            gap_state.update(generation=gap_state["generation"] + 1, pending=False,
                             deadline=None, preview=None)
            history_gap_cancel.configure(state="disabled")
            history_candidate_detail.set("缺口与当前候选核验超时；请重新选择历史任务。没有创建新任务。")
        if (gap_state["preparing"] and gap_state["prepare_deadline"] is not None
                and time.monotonic() >= gap_state["prepare_deadline"]):
            gap_state.update(generation=gap_state["generation"] + 1, preparing=False,
                             prepare_deadline=None, preview=None)
            history_gap_cancel.configure(state="disabled")
            history_candidate_detail.set("原请求草稿准备超时；现有草稿保留。请重新选择历史任务。")
        if (skill_gate["checking"] and skill_gate["deadline"] is not None
                and time.monotonic() >= skill_gate["deadline"]):
            skill_gate["generation"] += 1
            skill_gate.update(checking=False, enabled=False, digest=None, deadline=None)
            outcome = skill_gate.get("outcome")
            skill_status.set((outcome + " " if outcome else "") +
                             "本地技能状态核验超时；当前不可运行，未调用模型。")
        if (active["kind"] in {"resume_verifying", "agent_source_preparing",
                                "agent_retry_preparing",
                                "skill_preparing",
                                "mail_export_preparing", "mail_export_verifying"}
                and active.get("verify_deadline") is not None
                and time.monotonic() >= active["verify_deadline"]):
            timed_out_kind = active["kind"]
            active.update(kind=None, process=None, stop=False, task_root=None,
                          stop_deadline=None, attempt_id=None, attempt_root=None,
                          history_id=None, verify_deadline=None)
            note_start.configure(state="normal")
            research_start.configure(state="normal")
            task_start.configure(state="normal")
            source_pick.configure(state="normal")
            source_entry.configure(state="normal")
            source_clear.configure(state="normal" if source_choice["path"] else "disabled")
            task_stop.configure(state="disabled")
            history_resume.configure(state="disabled")
            history_skill_resume.configure(state="disabled")
            history_bundle_resume.configure(state="disabled")
            history_stop.configure(state="disabled")
            if timed_out_kind == "resume_verifying":
                history_detail.set("本次续跑结果核验超时；请刷新任务历史查看持久记录。")
            elif timed_out_kind == "agent_source_preparing":
                task_status.set("成果来源核验超时；未启动任务或调用模型。")
            elif timed_out_kind == "agent_retry_preparing":
                task_status.set("原请求或来源核验超时；未创建新任务或调用模型。")
            elif timed_out_kind == "skill_preparing":
                skill_status.set("技能发布状态或资料核验超时；未运行本地技能或调用模型。")
                refresh_skill_gate(outcome=skill_status.get())
            elif timed_out_kind == "mail_export_preparing":
                task_status.set("本机邮件导出准备超时；未确认创建导出任务，请在历史任务中核查。")
            else:
                task_status.set("本机邮件导出结果核验超时；请在历史任务中检查持久记录。")
        update_source_use_buttons()
        update_skill_button()
        selected_inspection = history_selected["inspection"]
        history_prepare.configure(state="normal" if active["kind"] is None
                                  and type(selected_inspection) is dict
                                  and selected_inspection.get("root") == history_records.get(
                                      history_selected["id"], {}).get("root")
                                  and (selected_inspection.get("state") == "failed"
                                       and type(selected_inspection.get("input_failure")) is dict
                                       or selected_inspection.get("state") == "unsupported"
                                       and type(selected_inspection.get("capability_gap")) is dict
                                       and not gap_state["pending"] and not gap_state["preparing"]
                                       and not gap_state["prepared"]
                                       and type(gap_state["preview"]) is dict
                                       and gap_state["preview"].get("eligible") is True)
                                  else "disabled")

    def poll():
        # Tk reports callback exceptions to stderr. Keep the timer alive so a
        # single failed event cannot also disable cancellation and deadlines.
        try:
            poll_once()
        finally:
            root.after(100, poll)

    def close():
        if active['kind'] == 'computer_goal':
            goal_panel.stop()
            root.after(200, close)
        elif active['kind'] == 'computer':
            computer_panel.stop()
            root.after(200, close)
        elif active["kind"] in {"research", "agent", "agent_source_preparing",
                              "agent_retry_preparing",
                              "skill_preparing",
                              "mail_export_preparing", "mail_export"}:
            stop_research()
            root.after(200, close)
        elif active["kind"] == "resume":
            stop_resume()
            root.after(200, close)
        elif active["kind"] == "resume_verifying":
            history_detail.set("正在核验本次续跑结果，请稍候关闭。")
            root.after(200, close)
        elif active["kind"] == "mail_export_verifying":
            task_status.set("正在核验本机邮件导出结果，请稍候关闭。")
            root.after(200, close)
        elif active["kind"] == "notepad":
            note_status.set("请先在记事本执行窗口完成或停止任务。")
        else:
            computer_panel.close()
            goal_panel.close()
            root.destroy()

    root.protocol("WM_DELETE_WINDOW", close)
    refresh_budget()
    load_history()
    root.after(100, poll)
    root.mainloop()


if __name__ == "__main__":
    main()
