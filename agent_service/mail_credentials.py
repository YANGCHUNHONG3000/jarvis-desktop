"""Current-user Windows DPAPI storage for explicitly remembered mail credentials.

Only the local connection UI should call ``save_credential`` after its remember
checkbox was selected. No mailbox operation is performed here.
"""

from __future__ import annotations

import base64
import binascii
import ctypes
from ctypes import wintypes
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import secrets
import stat


_DEFAULT_ROOT = Path(__file__).resolve().parents[1] / "data" / "mail_accounts"
_DOMAINS = {"qq": "qq.com", "gmail": "gmail.com"}
_LOCAL = re.compile(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*\Z")
_KEY = re.compile(r"[0-9a-f]{64}\.cred\Z")
_ENTROPY = b"jarvis/mail_credentials/v1"
_MAX_RECORD = 16_384


class CredentialError(ValueError):
    """A stable failure code that never contains a secret, address, or path."""

    def __init__(self, code):
        super().__init__(code)
        self.safe_code = code


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _windows_only():
    if os.name != "nt":
        raise CredentialError("credential_windows_required")


def _account(provider, account):
    if type(provider) is not str or provider not in _DOMAINS:
        raise CredentialError("credential_provider_invalid")
    if type(account) is not str or not 3 <= len(account) <= 254 or not account.isascii():
        raise CredentialError("credential_account_invalid")
    if account.count("@") != 1 or any(ord(char) < 33 or ord(char) > 126 for char in account):
        raise CredentialError("credential_account_invalid")
    local, domain = account.split("@", 1)
    if len(local) > 64 or not _LOCAL.fullmatch(local) or domain.lower() != _DOMAINS[provider]:
        raise CredentialError("credential_account_invalid")
    return provider, local + "@" + domain.lower()


def _key(provider, account):
    return sha256((provider + "/" + account).encode("ascii")).hexdigest() + ".cred"


def _reparse(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise CredentialError("credential_path_invalid") from exc
    return (stat.S_ISLNK(info.st_mode)
            or bool(getattr(info, "st_file_attributes", 0)
                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)))


def _safe_path(path):
    current = path
    while True:
        if _reparse(current):
            raise CredentialError("credential_path_invalid")
        if current == current.parent:
            return
        current = current.parent


def _root(root, *, create=False):
    path = _DEFAULT_ROOT if root is None else Path(root)
    if not path.is_absolute():
        raise CredentialError("credential_path_invalid")
    _safe_path(path)
    if create:
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise CredentialError("credential_path_invalid") from exc
        _safe_path(path)
    if path.exists() and not path.is_dir():
        raise CredentialError("credential_path_invalid")
    return path


def _blob(data):
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer


def _crypt32():
    crypt = ctypes.WinDLL("Crypt32", use_last_error=True)
    crypt.CryptProtectData.argtypes = [ctypes.POINTER(_DataBlob), wintypes.LPCWSTR,
                                      ctypes.POINTER(_DataBlob), ctypes.c_void_p,
                                      ctypes.c_void_p, wintypes.DWORD,
                                      ctypes.POINTER(_DataBlob)]
    crypt.CryptProtectData.restype = wintypes.BOOL
    crypt.CryptUnprotectData.argtypes = [ctypes.POINTER(_DataBlob),
                                        ctypes.POINTER(wintypes.LPWSTR),
                                        ctypes.POINTER(_DataBlob), ctypes.c_void_p,
                                        ctypes.c_void_p, wintypes.DWORD,
                                        ctypes.POINTER(_DataBlob)]
    crypt.CryptUnprotectData.restype = wintypes.BOOL
    kernel = ctypes.WinDLL("Kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    return crypt, kernel


def _dpapi(data, *, protect):
    _windows_only()
    crypt, kernel = _crypt32()
    source, source_buffer = _blob(data)
    entropy, entropy_buffer = _blob(_ENTROPY)
    output = _DataBlob()
    try:
        if protect:
            okay = crypt.CryptProtectData(ctypes.byref(source), None,
                                          ctypes.byref(entropy), None, None, 1,
                                          ctypes.byref(output))
        else:
            okay = crypt.CryptUnprotectData(ctypes.byref(source), None,
                                            ctypes.byref(entropy), None, None, 1,
                                            ctypes.byref(output))
        if not okay or not output.pbData or output.cbData > _MAX_RECORD:
            raise CredentialError("credential_encrypt_failed" if protect
                                  else "credential_decrypt_failed")
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        if output.pbData:
            if not protect:
                ctypes.memset(output.pbData, 0, output.cbData)
            kernel.LocalFree(ctypes.cast(output.pbData, ctypes.c_void_p))
        ctypes.memset(source_buffer, 0, len(source_buffer))
        ctypes.memset(entropy_buffer, 0, len(entropy_buffer))


def _decode_record(raw, filename):
    if not 0 < len(raw) <= _MAX_RECORD:
        raise CredentialError("credential_record_invalid")
    try:
        outer = json.loads(raw.decode("ascii"))
        if type(outer) is not dict or set(outer) != {"version", "ciphertext"}:
            raise ValueError
        if type(outer["version"]) is not int or outer["version"] != 1:
            raise ValueError
        if type(outer["ciphertext"]) is not str:
            raise ValueError
        encrypted = base64.b64decode(outer["ciphertext"], validate=True)
    except (UnicodeError, ValueError, TypeError, binascii.Error) as exc:
        raise CredentialError("credential_record_invalid") from exc
    plain = _dpapi(encrypted, protect=False)
    try:
        record = json.loads(plain.decode("utf-8"))
        if type(record) is not dict or set(record) != {"provider", "account", "secret"}:
            raise ValueError
        provider, account = _account(record["provider"], record["account"])
        if (record["account"] != account or type(record["secret"]) is not str
                or not 1 <= len(record["secret"]) <= 4096 or _key(provider, account) != filename):
            raise ValueError
        return record
    except (UnicodeError, ValueError, TypeError, CredentialError) as exc:
        raise CredentialError("credential_record_invalid") from exc


def _read(path):
    _safe_path(path)
    if not path.is_file():
        raise CredentialError("credential_record_invalid")
    try:
        with path.open("rb") as file:
            raw = file.read(_MAX_RECORD + 1)
    except OSError as exc:
        raise CredentialError("credential_record_invalid") from exc
    _safe_path(path)
    return _decode_record(raw, path.name)


def save_credential(provider, account, secret, root=None) -> None:
    """Atomically remember one credential, encrypted for the current Windows user."""
    _windows_only()
    provider, account = _account(provider, account)
    if (type(secret) is not str or not 1 <= len(secret) <= 4096
            or "\x00" in secret or any(0xD800 <= ord(char) <= 0xDFFF for char in secret)):
        raise CredentialError("credential_secret_invalid")
    path = _root(root, create=True)
    payload = json.dumps({"provider": provider, "account": account, "secret": secret},
                         ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    encrypted = _dpapi(payload, protect=True)
    envelope = json.dumps({"version": 1,
                           "ciphertext": base64.b64encode(encrypted).decode("ascii")},
                          separators=(",", ":")).encode("ascii")
    if len(envelope) > _MAX_RECORD:
        raise CredentialError("credential_record_invalid")
    target = path / _key(provider, account)
    temporary = path / ("." + secrets.token_hex(16) + ".tmp")
    _safe_path(target)
    try:
        with temporary.open("xb") as file:
            file.write(envelope)
            file.flush()
            os.fsync(file.fileno())
        _safe_path(path)
        _safe_path(target)
        os.replace(temporary, target)
    except OSError as exc:
        raise CredentialError("credential_write_failed") from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def load_credential(provider, account, root=None) -> str | None:
    """Return one decrypted secret, or None when no credential was saved."""
    _windows_only()
    provider, account = _account(provider, account)
    path = _root(root) / _key(provider, account)
    _safe_path(path)
    if not path.exists():
        return None
    record = _read(path)
    return record["secret"]


def list_accounts(root=None) -> list[dict]:
    """List only records decryptable by this Windows user, without returning secrets."""
    _windows_only()
    path = _root(root)
    if not path.exists():
        return []
    accounts = []
    try:
        files = sorted(path.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise CredentialError("credential_path_invalid") from exc
    for file in files:
        if not file.name.endswith(".cred"):
            continue
        if not _KEY.fullmatch(file.name):
            raise CredentialError("credential_record_invalid")
        record = _read(file)
        accounts.append({"provider": record["provider"], "account": record["account"]})
    return accounts
