"""Synthetic, local-only DPAPI credential storage checks."""

import base64
from hashlib import sha256
import json
import os
from pathlib import Path
import secrets
from types import SimpleNamespace

import pytest

import agent_service.mail_credentials as vault


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI is required")


def test_dpapi_roundtrip_hash_path_and_no_plaintext_on_disk(tmp_path):
    secret = secrets.token_urlsafe(48)
    vault.save_credential("qq", "123456@qq.COM", secret, tmp_path)
    expected_name = sha256(b"qq/123456@qq.com").hexdigest() + ".cred"
    files = list(tmp_path.iterdir())
    assert len(files) == 1 and files[0].name == expected_name
    raw = files[0].read_bytes()
    assert secret.encode() not in raw
    assert b"123456@qq.com" not in raw
    assert vault.load_credential("qq", "123456@qq.com", tmp_path) == secret
    assert vault.list_accounts(tmp_path) == [{"provider": "qq", "account": "123456@qq.com"}]


def test_missing_and_separate_accounts(tmp_path):
    assert vault.load_credential("gmail", "alice@gmail.com", tmp_path) is None
    assert vault.list_accounts(tmp_path) == []
    first, second = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    vault.save_credential("gmail", "alice@gmail.com", first, tmp_path)
    vault.save_credential("qq", "123@qq.com", second, tmp_path)
    assert vault.load_credential("gmail", "alice@gmail.com", tmp_path) == first
    assert vault.load_credential("qq", "123@qq.com", tmp_path) == second
    assert {tuple(account.items()) for account in vault.list_accounts(tmp_path)} == {
        (("provider", "gmail"), ("account", "alice@gmail.com")),
        (("provider", "qq"), ("account", "123@qq.com")),
    }


@pytest.mark.parametrize("provider,account", [
    ("outlook", "a@outlook.com"),
    ("QQ", "123@qq.com"),
    ("qq", "123@gmail.com"),
    ("gmail", "a@googlemail.com"),
    ("gmail", "a\nb@gmail.com"),
    ("gmail", "a..b@gmail.com"),
    ("gmail", "é@gmail.com"),
    ("gmail", "a@gmail.com/../x"),
])
def test_provider_account_domain_and_path_rules(tmp_path, provider, account):
    with pytest.raises(vault.CredentialError):
        vault.save_credential(provider, account, secrets.token_urlsafe(16), tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("secret", ["", "\x00", "x" * 4097, b"bytes"])
def test_bad_secret_rejected_before_storage(tmp_path, secret):
    with pytest.raises(vault.CredentialError, match="credential_secret_invalid"):
        vault.save_credential("qq", "123@qq.com", secret, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_tampered_ciphertext_and_swapped_files_rejected(tmp_path):
    vault.save_credential("qq", "123@qq.com", secrets.token_urlsafe(32), tmp_path)
    vault.save_credential("qq", "456@qq.com", secrets.token_urlsafe(32), tmp_path)
    first = tmp_path / vault._key("qq", "123@qq.com")
    second = tmp_path / vault._key("qq", "456@qq.com")
    first_data, second_data = first.read_bytes(), second.read_bytes()
    first.write_bytes(second_data)
    with pytest.raises(vault.CredentialError, match="credential_record_invalid"):
        vault.load_credential("qq", "123@qq.com", tmp_path)
    first.write_bytes(first_data)
    envelope = json.loads(first_data)
    cipher = bytearray(base64.b64decode(envelope["ciphertext"]))
    cipher[len(cipher) // 2] ^= 1
    envelope["ciphertext"] = base64.b64encode(cipher).decode("ascii")
    first.write_text(json.dumps(envelope), encoding="ascii")
    with pytest.raises(vault.CredentialError):
        vault.load_credential("qq", "123@qq.com", tmp_path)
    with pytest.raises(vault.CredentialError):
        vault.list_accounts(tmp_path)


def test_malformed_record_and_unrecognized_record_name_rejected(tmp_path):
    vault.save_credential("qq", "123@qq.com", secrets.token_urlsafe(24), tmp_path)
    record = next(tmp_path.glob("*.cred"))
    record.write_text('{"version":1,"ciphertext":"%%%"}', encoding="ascii")
    with pytest.raises(vault.CredentialError, match="credential_record_invalid"):
        vault.load_credential("qq", "123@qq.com", tmp_path)
    record.unlink()
    (tmp_path / "not-hashed.cred").write_text("invalid", encoding="ascii")
    with pytest.raises(vault.CredentialError, match="credential_record_invalid"):
        vault.list_accounts(tmp_path)


def test_atomic_replace_failure_preserves_previous_ciphertext(tmp_path, monkeypatch):
    first, second = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    vault.save_credential("gmail", "alice@gmail.com", first, tmp_path)
    record = next(tmp_path.glob("*.cred"))
    previous = record.read_bytes()

    def fail_replace(_source, _target):
        raise OSError("simulated")

    monkeypatch.setattr(vault.os, "replace", fail_replace)
    with pytest.raises(vault.CredentialError, match="credential_write_failed"):
        vault.save_credential("gmail", "alice@gmail.com", second, tmp_path)
    assert record.read_bytes() == previous
    assert vault.load_credential("gmail", "alice@gmail.com", tmp_path) == first
    assert list(tmp_path.iterdir()) == [record]


def test_reparse_storage_and_record_rejected(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("Symlinks unavailable on this host")
    with pytest.raises(vault.CredentialError, match="credential_path_invalid"):
        vault.save_credential("qq", "123@qq.com", secrets.token_urlsafe(24), link)
    vault.save_credential("qq", "123@qq.com", secrets.token_urlsafe(24), real)
    record = next(real.glob("*.cred"))
    destination = tmp_path / "copy.cred"
    record.replace(destination)
    record.symlink_to(destination)
    with pytest.raises(vault.CredentialError, match="credential_path_invalid"):
        vault.load_credential("qq", "123@qq.com", real)
    with pytest.raises(vault.CredentialError, match="credential_path_invalid"):
        vault.list_accounts(real)


def test_relative_storage_root_rejected(tmp_path):
    with pytest.raises(vault.CredentialError, match="credential_path_invalid") as error:
        vault.save_credential("qq", "123@qq.com", secrets.token_urlsafe(24), Path("relative"))
    assert error.value.safe_code == "credential_path_invalid"


def test_non_windows_refuses_without_creating_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "os", SimpleNamespace(name="posix"))
    with pytest.raises(vault.CredentialError, match="credential_windows_required"):
        vault.save_credential("qq", "123@qq.com", secrets.token_urlsafe(24), tmp_path)
    with pytest.raises(vault.CredentialError, match="credential_windows_required"):
        vault.load_credential("qq", "123@qq.com", tmp_path)
    with pytest.raises(vault.CredentialError, match="credential_windows_required"):
        vault.list_accounts(tmp_path)
    assert list(tmp_path.iterdir()) == []
