"""Allowlisted capabilities. Content from sources can never grant permission."""
import ipaddress
from pathlib import Path
import re
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from .contracts import ActionRequest, ApprovalRequired, PolicyError


def source_url(value: str) -> str:
    if not isinstance(value, str) or len(value) > 2048:
        raise PolicyError("Source URL must be a string of at most 2048 characters")
    if any(ord(c) < 33 for c in value) or any(c in value for c in "\\<>\"'`"):
        raise PolicyError("Invalid URL characters")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise PolicyError("Invalid source URL") from exc
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None):
        raise PolicyError("Only credential-free HTTP(S) source URLs are allowed")
    host = parsed.hostname.lower().rstrip(".")
    if port not in {None, 80, 443}:
        raise PolicyError("Only standard HTTP(S) ports are allowed")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise PolicyError("Local source addresses are not allowed")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host or not re.fullmatch(r"[a-z0-9.-]+", host):
            raise PolicyError("A public DNS hostname is required")
    else:
        if not address.is_global:
            raise PolicyError("Private/reserved source addresses are not allowed")
    if any(part in {".", ".."} for part in unquote(parsed.path).split("/")):
        raise PolicyError("Source URLs must not contain dot path segments")
    if re.search(r"%(?![0-9a-fA-F]{2})", parsed.path + parsed.query):
        raise PolicyError("Invalid percent escape in source URL")
    authority = f"[{host}]" if ":" in host else host
    if port is not None and port != (443 if parsed.scheme == "https" else 80):
        authority += f":{port}"
    path = quote(parsed.path or "/", safe="/%:@!$&()*+,;=-._~")
    query = quote(parsed.query, safe="/%?:@!$&()*+,;=-._~")
    return urlunsplit((parsed.scheme, authority, path, query, ""))


def output_name(value: str) -> str:
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}\.md", value)
            or value.split(".")[0].upper() in {
                "CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(10)],
                *[f"LPT{i}" for i in range(10)],
            }):
        raise PolicyError("Output must be a safe ASCII .md basename (no directories)")
    return value


class ActionPolicy:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()

    def artifact_path(self, name):
        path = self.root / "artifacts" / output_name(name)
        if path.parent.is_symlink() or path.is_symlink() or path.resolve() != path:
            raise PolicyError("Artifact paths must not traverse links or junctions")
        return path

    def check(self, job: dict, action: ActionRequest):
        if action.kind in {"payment", "file.delete", "file.overwrite", "model.call"}:
            raise ApprovalRequired(f"Explicit user authorization required: {action.kind}")
        if action.kind == "browser.read" and action.target in job["urls"]:
            source_url(action.target)
            return
        if action.kind in {"document.create", "document.verify"} and action.target == job["output_name"]:
            self.artifact_path(action.target)
            return
        raise PolicyError("Action is outside this job's supported capabilities")
