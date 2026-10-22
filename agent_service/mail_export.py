"""Explicit local copy of one already-read message into a verified Office document.

No mailbox connection, credential, model, or raw MIME object enters this module.
The caller binds the selected read result to its account generation before making
the strict snapshot accepted here.
"""

from __future__ import annotations

from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import time
import uuid

from . import office_workflow as workflow
from . import task_history as history
from .office import validate_spec


_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_NUMBER = re.compile(r"[1-9][0-9]*\Z")
_MIME = re.compile(r"[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+\Z")
_TASK_NAME = re.compile(r"task-[0-9a-f]{32}\Z")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_REQUEST_LIMIT = 240_000
_SNAPSHOT_FIELDS = {"provider", "uid", "uidvalidity", "subject", "from", "to",
                    "date", "body", "attachments", "sha256", "coverage"}
_COVERAGE_FIELDS = {"scope", "body_format", "parts", "attachments",
                    "html_alternative_ignored"}
_ATTACHMENT_FIELDS = {"filename", "content_type", "size"}


class MailExportError(ValueError):
    """Stable rejection code; never includes private message contents."""


def _linked(path):
    path = Path(path)
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _linked_ancestor(path):
    current = Path(path).absolute()
    while True:
        if _linked(current):
            return True
        if current == current.parent:
            return False
        current = current.parent


def _task(task_root, base):
    try:
        if _linked_ancestor(base) or _linked_ancestor(task_root):
            raise MailExportError("mail_export_task_scope_invalid")
        return history._task(task_root, history._base(base))
    except (ValueError, OSError) as exc:
        raise MailExportError("mail_export_task_scope_invalid") from exc


def _prepare_task(task_root, base):
    """Create only the exact owned task path, after checking every ancestor."""
    root = Path(task_root).absolute()
    base_path = Path(base).absolute()
    if (not _TASK_NAME.fullmatch(root.name) or root.parent != base_path
            or ".." in root.parts or ".." in base_path.parts
            or _linked_ancestor(base_path) or _linked_ancestor(root)):
        raise MailExportError("mail_export_task_scope_invalid")
    for directory in reversed((base_path, *base_path.parents)):
        if _linked(directory):
            raise MailExportError("mail_export_task_scope_invalid")
        if directory.exists():
            if not directory.is_dir():
                raise MailExportError("mail_export_task_scope_invalid")
            continue
        try:
            directory.mkdir(exist_ok=False)
        except OSError as exc:
            raise MailExportError("mail_export_task_scope_invalid") from exc
        if _linked(directory) or not directory.is_dir():
            raise MailExportError("mail_export_task_scope_invalid")
    if not root.exists():
        if _linked_ancestor(root):
            raise MailExportError("mail_export_task_scope_invalid")
        try:
            root.mkdir(exist_ok=False)
        except OSError as exc:
            raise MailExportError("mail_export_task_scope_invalid") from exc
    return _task(root, base_path)


def _text(value, maximum, code, *, multiline=False):
    if type(value) is not str or len(value) > maximum:
        raise MailExportError(code)
    allowed = "\r\n\t" if multiline else ""
    if any((_CONTROL.fullmatch(char) and char not in allowed)
           or 0xD800 <= ord(char) <= 0xDFFF or ord(char) in (0xFFFE, 0xFFFF)
           for char in value):
        raise MailExportError(code)
    return value


def _uid(value):
    if (type(value) is not str or not _NUMBER.fullmatch(value)
            or int(value) > 4_294_967_295):
        raise MailExportError("mail_export_identity_invalid")
    return value


def validate_snapshot(snapshot) -> dict:
    """Copy the exact read-only mailbox fields accepted for an export request."""
    if type(snapshot) is not dict or set(snapshot) != _SNAPSHOT_FIELDS:
        raise MailExportError("mail_export_snapshot_invalid")
    if snapshot["provider"] != "qq":
        raise MailExportError("mail_export_provider_invalid")
    uid = _uid(snapshot["uid"])
    uidvalidity = _uid(snapshot["uidvalidity"])
    subject = _text(snapshot["subject"], 500, "mail_export_header_invalid")
    from_header = _text(snapshot["from"], 500, "mail_export_header_invalid")
    to_header = _text(snapshot["to"], 1000, "mail_export_header_invalid")
    date = _text(snapshot["date"], 100, "mail_export_header_invalid")
    body = _text(snapshot["body"], 200_000, "mail_export_body_invalid", multiline=True)
    digest = snapshot["sha256"]
    if type(digest) is not str or not _DIGEST.fullmatch(digest):
        raise MailExportError("mail_export_digest_invalid")
    coverage = snapshot["coverage"]
    if type(coverage) is not dict or set(coverage) != _COVERAGE_FIELDS:
        raise MailExportError("mail_export_coverage_invalid")
    if (coverage["scope"] != "selected_full_message"
            or coverage["body_format"] not in {"text/plain", "html_to_plain", "none"}
            or type(coverage["parts"]) is not int or not 1 <= coverage["parts"] <= 32
            or coverage["attachments"] != "metadata_only"
            or type(coverage["html_alternative_ignored"]) is not bool
            or (coverage["body_format"] == "none" and body)):
        raise MailExportError("mail_export_coverage_invalid")
    attachments = snapshot["attachments"]
    if type(attachments) is not list or len(attachments) > 32 or len(attachments) > coverage["parts"]:
        raise MailExportError("mail_export_attachments_invalid")
    copied = []
    total_size = 0
    for item in attachments:
        if type(item) is not dict or set(item) != _ATTACHMENT_FIELDS:
            raise MailExportError("mail_export_attachments_invalid")
        filename = _text(item["filename"], 255, "mail_export_attachments_invalid")
        if "/" in filename or "\\" in filename:
            raise MailExportError("mail_export_attachments_invalid")
        content_type = item["content_type"]
        if (type(content_type) is not str or len(content_type) > 100
                or not _MIME.fullmatch(content_type)):
            raise MailExportError("mail_export_attachments_invalid")
        size = item["size"]
        if type(size) is not int or not 0 <= size <= 2_000_000:
            raise MailExportError("mail_export_attachments_invalid")
        total_size += size
        copied.append({"filename": filename, "content_type": content_type, "size": size})
    if total_size > 2_000_000:
        raise MailExportError("mail_export_attachments_invalid")
    canonical = {"provider": "qq", "uid": uid, "uidvalidity": uidvalidity,
                 "subject": subject, "from": from_header, "to": to_header,
                 "date": date, "body": body, "attachments": copied, "sha256": digest,
                 "coverage": dict(coverage)}
    _document_spec(canonical)  # All content must fit before any local effect.
    return canonical


def _document_spec(snapshot):
    coverage = snapshot["coverage"]
    format_label = {"text/plain": "纯文本正文", "html_to_plain": "从 HTML 提取的纯文本",
                    "none": "原邮件无可显示正文"}[coverage["body_format"]]
    paragraphs = [
        "来源：QQ 邮箱中已读邮件的本地副本。",
        "主题：" + snapshot["subject"],
        "发件人（邮件头显示）：" + snapshot["from"],
        "收件人（邮件头显示）：" + snapshot["to"],
        "日期（邮件头显示）：" + snapshot["date"],
        "正文格式：" + format_label,
        "正文换行拆为文档段落；制表符展开为四个空格。以下为所显示正文的完整副本。",
        "正文：",
    ]
    body = snapshot["body"].replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
    paragraphs.extend(body.split("\n"))
    paragraphs.append("附件：以下仅列邮件中的附件元数据；附件内容未保存。")
    attachment_lines = [
        f"附件 {index}：名称={item['filename'] or '（空）'}；类型={item['content_type']}；大小={item['size']} 字节。"
        for index, item in enumerate(snapshot["attachments"], 1)
    ]
    packed = ""
    for line in attachment_lines:
        if packed and len(packed) + 1 + len(line) > 3000:
            paragraphs.append(packed)
            packed = ""
        packed = line if not packed else packed + "\u3000" + line
    if packed:
        paragraphs.append(packed)
    try:
        return validate_spec({"kind": "document", "title": "邮件本地副本", "paragraphs": paragraphs})
    except ValueError as exc:
        raise MailExportError("mail_export_document_limit") from exc


def _request_bytes(snapshot):
    canonical = validate_snapshot(snapshot)
    snapshot_bytes = json.dumps(canonical, ensure_ascii=False, sort_keys=True,
                                separators=(",", ":")).encode("utf-8")
    request = {"version": 1, "snapshot": canonical,
               "snapshot_sha256": sha256(snapshot_bytes).hexdigest()}
    raw = json.dumps(request, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    if len(raw) > _REQUEST_LIMIT:
        raise MailExportError("mail_export_request_oversize")
    return raw


def write_export_request(task_root, base, snapshot) -> str:
    """Safely create the owned task and exclusively store the GUI-approved snapshot."""
    raw = _request_bytes(snapshot)
    root = _prepare_task(task_root, base)
    if any(entry.name != "cancel.flag" for entry in root.iterdir()):
        raise MailExportError("mail_export_task_not_empty")
    cancel = root / "cancel.flag"
    if _linked(cancel) or (cancel.exists() and not cancel.is_file()):
        raise MailExportError("mail_export_task_scope_invalid")
    path = root / "mail-export-request.json"
    try:
        with path.open("xb") as file:
            file.write(raw)
            file.flush()
            os.fsync(file.fileno())
    except FileExistsError as exc:
        raise MailExportError("mail_export_request_exists") from exc
    return sha256(raw).hexdigest()


def request_export_cancel(task_root, base) -> bool:
    """Set the owned cancellation flag only while its task path remains local."""
    try:
        root = _task(task_root, base)
        flag = root / "cancel.flag"
        if _linked_ancestor(flag):
            return False
        try:
            with flag.open("xb") as file:
                file.flush()
                os.fsync(file.fileno())
        except FileExistsError:
            return not _linked_ancestor(flag) and flag.is_file()
        return not _linked_ancestor(flag) and flag.is_file()
    except (MailExportError, OSError):
        return False


def _read_request(root, expected_sha256):
    path = root / "mail-export-request.json"
    if _linked(path) or not path.is_file() or not 0 < path.stat().st_size <= _REQUEST_LIMIT:
        raise MailExportError("mail_export_request_invalid")
    raw = path.read_bytes()
    if sha256(raw).hexdigest() != expected_sha256:
        raise MailExportError("mail_export_request_mismatch")
    try:
        request = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise MailExportError("mail_export_request_invalid") from exc
    if (type(request) is not dict or set(request) != {"version", "snapshot", "snapshot_sha256"}
            or type(request["version"]) is not int or request["version"] != 1):
        raise MailExportError("mail_export_request_invalid")
    canonical = validate_snapshot(request["snapshot"])
    if canonical != request["snapshot"] or _request_bytes(canonical) != raw:
        raise MailExportError("mail_export_request_invalid")
    return canonical


def _publish_parent(root, result):
    path = root / "result.json"
    if path.exists() or _linked(path):
        raise MailExportError("mail_export_result_exists")
    temporary = root / ("result-" + uuid.uuid4().hex + ".tmp")
    workflow._write_new(temporary, result)
    os.replace(temporary, path)


def run_mail_export(task_root, base, request_sha256, *, execute=None, deadline=None) -> dict:
    """Run exactly one deterministic Office document from a durable request."""
    if type(request_sha256) is not str or not _DIGEST.fullmatch(request_sha256):
        raise MailExportError("mail_export_digest_invalid")
    if deadline is None:
        deadline = time.monotonic() + 600
    if type(deadline) not in (int, float) or not math.isfinite(deadline):
        raise MailExportError("mail_export_deadline_invalid")
    root = _task(task_root, base)
    _read_request(root, request_sha256)
    try:
        with workflow._owner(root):
            snapshot = _read_request(root, request_sha256)
            allowed = {"mail-export-request.json", "cancel.flag", "owner.lock"}
            if (any(entry.name not in allowed for entry in root.iterdir())
                    or (root / "result.json").exists()
                    or (root / "mail-export-run.json").exists()):
                raise MailExportError("mail_export_replay_blocked")
            cancel = root / "cancel.flag"
            if _linked(cancel) or (cancel.exists() and not cancel.is_file()):
                raise MailExportError("mail_export_task_scope_invalid")
            workflow_root = root / ("workflow-" + uuid.uuid4().hex)
            claim = {"version": 1, "request_sha256": request_sha256,
                     "workflow_root": str(workflow_root)}
            workflow._write_new(root / "mail-export-run.json", claim)
            steps = [{"id": "s1", "action": "document", "data": _document_spec(snapshot)}]
            run = None
            error = None
            try:
                run = workflow.run_office_workflow(
                    steps, workflow_root, execute=execute, deadline=deadline,
                    cancel_requested=lambda: cancel.exists())
            except Exception:
                error = "mail_export_effect_unknown"
            receipts = {}
            if workflow_root.is_dir():
                try:
                    checked, _unknown = workflow._resume_preflight(steps, workflow_root)
                    receipts = checked
                except (ValueError, TypeError, KeyError, OSError):
                    error = "mail_export_receipt_unknown"
            completed = list(receipts)
            artifacts = [{"step": key, "path": receipt["artifact"],
                          "sha256": receipt["sha256"]} for key, receipt in receipts.items()]
            state = (run.get("state") if type(run) is dict else "unknown")
            if (error is not None or state == "succeeded" and completed != ["s1"]
                    or state not in {"succeeded", "cancelled", "timed_out", "unknown"}):
                state = "unknown"
            result = {"ok": state == "succeeded", "state": state, "kind": "office",
                      "root": str(root), "workflow_root": str(workflow_root),
                      "completed": completed, "artifacts": artifacts,
                      "artifact": artifacts[-1]["path"] if artifacts else None,
                      "readback_sha256": artifacts[-1]["sha256"] if artifacts else None,
                      "mail_export": {"request_sha256": request_sha256,
                                      "source_sha256": snapshot["sha256"],
                                      "provenance": "local_read_mail_copy"},
                      "error": error or (run.get("error") if type(run) is dict else None)}
            _publish_parent(root, result)
            return result
    except workflow.WorkflowError as exc:
        raise MailExportError(str(exc)) from exc
