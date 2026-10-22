"""Create one unsent, task-owned RFC 5322/MIME draft from verified artifacts."""

from __future__ import annotations

from email.message import EmailMessage
from email.parser import BytesParser
from email.policy import SMTP
from email.utils import getaddresses
from hashlib import sha256
import os
from pathlib import Path
import re


_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_BODY_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_LOCAL = re.compile(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*\Z")
_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_STEP = re.compile(r"s([1-8])\Z")
_MAX_ATTACHMENT = 20_000_000
_MAX_TOTAL = 30_000_000
_MIME = {
    ".docx": "vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


class DraftError(ValueError):
    """A stable internal failure code; never includes an address or file path."""


def _xml_safe(value: str) -> bool:
    return not any(0xD800 <= ord(char) <= 0xDFFF or ord(char) in (0xFFFE, 0xFFFF)
                   for char in value)


def _address(value):
    if type(value) is not str or not 3 <= len(value) <= 254 or not value.isascii():
        raise DraftError("draft_recipient_invalid")
    if _CONTROL.search(value) or value.count("@") != 1:
        raise DraftError("draft_recipient_invalid")
    local, domain = value.split("@", 1)
    labels = domain.split(".")
    if (len(local) > 64 or not _LOCAL.fullmatch(local) or len(labels) < 2
            or any(not _LABEL.fullmatch(label) for label in labels)):
        raise DraftError("draft_recipient_invalid")
    return local + "@" + domain.lower()


def validate_draft(data) -> dict:
    """Validate user-provided draft text and explicit recipient addresses."""
    if type(data) is not dict or not {"subject", "body"} <= set(data) or set(data) - {
            "subject", "body", "to"}:
        raise DraftError("draft_schema_invalid")
    subject, body, recipients = data["subject"], data["body"], data.get("to", [])
    if (type(subject) is not str or not 1 <= len(subject.strip()) <= 200
            or _CONTROL.search(subject) or not _xml_safe(subject)):
        raise DraftError("draft_subject_invalid")
    if (type(body) is not str or len(body) > 20_000 or _BODY_CONTROL.search(body)
            or not _xml_safe(body)):
        raise DraftError("draft_body_invalid")
    if type(recipients) is not list or len(recipients) > 10:
        raise DraftError("draft_recipient_invalid")
    addresses = [_address(item) for item in recipients]
    if len({item.casefold() for item in addresses}) != len(addresses):
        raise DraftError("draft_recipient_duplicate")
    return {"subject": subject.strip(), "body": body.replace("\r\n", "\n").replace("\r", "\n"),
            "to": addresses}


def _linked(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _attachment(spec, root: Path, current_step: int):
    if type(spec) is not dict or set(spec) != {"step", "path", "sha256"}:
        raise DraftError("draft_attachment_invalid")
    step, path_value, expected_hash = spec["step"], spec["path"], spec["sha256"]
    match = _STEP.fullmatch(step) if type(step) is str else None
    if (match is None or int(match.group(1)) >= current_step or type(path_value) is not str
            or type(expected_hash) is not str or not _SHA256.fullmatch(expected_hash)):
        raise DraftError("draft_attachment_invalid")
    path = Path(path_value)
    if not path.is_absolute() or path.name not in ("document.docx", "spreadsheet.xlsx"):
        raise DraftError("draft_attachment_invalid")
    owner = root.parent / step
    if _linked(owner) or _linked(path) or path.parent != owner or path.resolve() != owner / path.name:
        raise DraftError("draft_attachment_scope_invalid")
    if not path.is_file():
        raise DraftError("draft_attachment_missing")
    size = path.stat().st_size
    if not 0 < size <= _MAX_ATTACHMENT:
        raise DraftError("draft_attachment_size_invalid")
    data = path.read_bytes()
    if sha256(data).hexdigest() != expected_hash:
        raise DraftError("draft_attachment_hash_mismatch")
    suffix = path.suffix.lower()
    return {"step": step, "path": path, "filename": step + suffix,
            "sha256": expected_hash, "size": size, "data": data}


def _parsed_body(message):
    body_part = message.get_body(preferencelist=("plain",))
    if body_part is None or body_part.get_content_type() != "text/plain":
        raise DraftError("draft_body_mismatch")
    return body_part.get_content().replace("\r\n", "\n").replace("\r", "\n")


def _verify_eml(path: Path, draft: dict, attachments: list[dict]):
    with path.open("rb") as file:
        message = BytesParser(policy=SMTP).parse(file)
    if message.defects or message.get_all("X-Unsent") != ["1"] or "From" in message:
        raise DraftError("draft_mime_invalid")
    if str(message["Subject"]) != draft["subject"]:
        raise DraftError("draft_subject_mismatch")
    expected_to = draft["to"]
    actual_to = getaddresses(message.get_all("To", []))
    if actual_to != [("", item) for item in expected_to]:
        raise DraftError("draft_recipient_mismatch")
    expected_body = draft["body"] if draft["body"].endswith("\n") else draft["body"] + "\n"
    if _parsed_body(message) != expected_body:
        raise DraftError("draft_body_mismatch")
    parts = list(message.iter_attachments())
    if len(parts) != len(attachments):
        raise DraftError("draft_attachment_mismatch")
    observed = []
    for part, expected in zip(parts, attachments):
        suffix = Path(expected["filename"]).suffix.lower()
        if (part.get_filename() != expected["filename"]
                or part.get_content_type() != "application/" + _MIME[suffix]):
            raise DraftError("draft_attachment_mismatch")
        raw = part.get_payload(decode=True)
        if raw is None or sha256(raw).hexdigest() != expected["sha256"]:
            raise DraftError("draft_attachment_mismatch")
        observed.append({"step": expected["step"], "filename": expected["filename"],
                         "sha256": expected["sha256"]})
    return {"subject": draft["subject"], "body": draft["body"], "to": expected_to[:],
            "attachments": observed}


def execute_draft(spec, step_root: Path, should_stop=lambda: False) -> dict:
    """Exclusively write and reread one unsent draft; never open or send mail."""
    write_started = False
    artifact = None
    result = {"ok": False, "state": "failed", "file_verified": False, "draft_verified": False}

    def check():
        if should_stop():
            raise DraftError("cancelled")

    try:
        if type(spec) is not dict or spec.get("kind") != "email_draft" or set(spec) - {
                "kind", "subject", "body", "to", "attachments"}:
            raise DraftError("draft_schema_invalid")
        draft = validate_draft({key: spec[key] for key in ("subject", "body", "to") if key in spec})
        attachment_specs = spec.get("attachments", [])
        if type(attachment_specs) is not list or len(attachment_specs) > 8:
            raise DraftError("draft_attachment_invalid")
        root = Path(step_root).resolve(strict=True)
        if not root.is_dir() or _linked(Path(step_root)):
            raise DraftError("draft_root_invalid")
        current = _STEP.fullmatch(root.name)
        if current is None:
            raise DraftError("draft_root_invalid")
        artifact = root / "draft.eml"
        if artifact.exists() or _linked(artifact):
            raise DraftError("draft_path_conflict")
        result["artifact"] = str(artifact)
        check()
        attachments = []
        seen = set()
        total = 0
        for item in attachment_specs:
            check()
            entry = _attachment(item, root, int(current.group(1)))
            if entry["step"] in seen:
                raise DraftError("draft_attachment_duplicate")
            seen.add(entry["step"])
            total += entry["size"]
            if total > _MAX_TOTAL:
                raise DraftError("draft_attachment_size_invalid")
            attachments.append(entry)
        message = EmailMessage(policy=SMTP)
        message["Subject"] = draft["subject"]
        message["X-Unsent"] = "1"
        if draft["to"]:
            message["To"] = ", ".join(draft["to"])
        message.set_content(draft["body"], subtype="plain", charset="utf-8")
        for entry in attachments:
            suffix = entry["path"].suffix.lower()
            message.add_attachment(entry["data"], maintype="application", subtype=_MIME[suffix],
                                   filename=entry["filename"])
        payload = message.as_bytes(policy=SMTP)
        # Reverify live inputs immediately before the only draft write effect.
        for entry in attachments:
            check()
            refreshed = _attachment({"step": entry["step"], "path": str(entry["path"]),
                                     "sha256": entry["sha256"]}, root, int(current.group(1)))
            if refreshed["data"] != entry["data"]:
                raise DraftError("draft_attachment_changed")
        check()
        write_started = True
        with artifact.open("xb") as file:
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        check()
        observed = _verify_eml(artifact, draft, attachments)
        check()
        result.update(ok=True, state="succeeded", sha256=sha256(artifact.read_bytes()).hexdigest(),
                      file_verified=True, draft_verified=True, observed=observed)
    except Exception as error:
        code = str(error) if isinstance(error, DraftError) else "draft_operation_failed"
        result.update(ok=False, state="unknown" if write_started else "cancelled" if code == "cancelled"
                      else "failed", code=code)
    return result
