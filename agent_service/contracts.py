"""Versioned values exchanged by the service and its adapters."""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


@dataclass(frozen=True)
class ActionRequest:
    operation_id: str
    kind: str
    target: str

    @property
    def fingerprint(self):
        return digest(json.dumps(asdict(self), sort_keys=True).encode("utf-8"))


@dataclass(frozen=True)
class Observation:
    requested_url: str
    final_url: str
    title: str
    text: str
    captured_at: str

    def to_dict(self):
        result = asdict(self)
        result["sha256"] = digest(self.text.encode("utf-8"))
        return result


class PolicyError(ValueError):
    pass


class ApprovalRequired(PolicyError):
    pass


class BrowserReadError(RuntimeError):
    def __init__(self, code):
        allowed = {"dns_failed", "tls_failed", "network_timeout", "network_failed",
                   "source_rejected", "policy_rejected", "missing_dependency", "browser_failed"}
        self.safe_code = code if code in allowed else "browser_failed"
        super().__init__(self.safe_code)


TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
