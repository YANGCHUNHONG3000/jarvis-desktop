"""Offline read-only mailbox protocol tests; no account or network is used."""

from email.message import EmailMessage
from email.policy import SMTP
from hashlib import sha256
import io
import imaplib
import socket
import threading
import time

import pytest

from agent_service import mailbox


ACCOUNT = "person@gmail.com"
SECRET = "abcd efgh ijkl mnop"


def _message(subject="Hello", body="Plain text", *, attachment=False, html=False):
    mail = EmailMessage(policy=SMTP)
    mail["Subject"] = subject
    mail["From"] = "Sender <sender@example.org>"
    mail["To"] = ACCOUNT
    mail["Date"] = "Sat, 17 Oct 2026 12:00:00 +0000"
    if html:
        mail.set_content('<div>Hello <b>world</b><script>bad()</script><img src="https://example.org/x"></div>',
                         subtype="html", charset="utf-8")
    else:
        mail.set_content(body, charset="utf-8")
    if attachment:
        mail.add_attachment(b"not saved", maintype="application", subtype="octet-stream",
                            filename="data.bin")
    return mail.as_bytes(policy=SMTP)


class FakeIMAP:
    instances = []
    uidvalidity = b"77"
    mailbox = {"101": _message("Older"), "102": _message("Newer", attachment=True)}
    override_size = None
    override_uid = None
    expected_host = "imap.gmail.com"
    expected_account = ACCOUNT

    def __init__(self, host, port, *, ssl_context, timeout, deadline, should_stop):
        assert host == self.expected_host and port == 993
        assert ssl_context.check_hostname and ssl_context.verify_mode != 0
        assert 0 < timeout <= 15 and deadline > time.monotonic()
        self.calls = []
        self.closed = False
        self.should_stop = should_stop
        type(self).instances.append(self)

    def set_limits(self, literal_limit, response_limit):
        self.calls.append(("limits", literal_limit, response_limit))

    def login(self, account, secret):
        self.calls.append(("login", account))
        assert account == self.expected_account and secret == "abcdefghijklmnop"
        return "OK", [b"logged in"]

    def select(self, name, readonly=False):
        self.calls.append(("select", name, readonly))
        return "OK", [str(len(self.mailbox)).encode()]

    def response(self, name):
        self.calls.append(("response", name))
        return name, [self.uidvalidity]

    def fetch(self, sequence, query):
        self.calls.append(("fetch", sequence, query))
        assert "BODY.PEEK[HEADER.FIELDS" in query
        uid = str(sorted(map(int, self.mailbox))[int(sequence) - 1])
        raw = self.mailbox[uid]
        header = raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
        metadata = f"{sequence} (UID {uid} RFC822.SIZE {len(raw)} BODY[HEADER.FIELDS] {{{len(header)}}}".encode()
        return "OK", [(metadata, header), b")"]

    def uid(self, verb, uid, query):
        self.calls.append(("uid", verb, uid, query))
        assert verb == "FETCH" and query in {"(UID RFC822.SIZE)",
                                              "(UID RFC822.SIZE BODY.PEEK[])"}
        raw = self.mailbox[uid]
        actual_uid = self.override_uid or uid
        size = self.override_size if self.override_size is not None else len(raw)
        if "BODY.PEEK[]" in query:
            return "OK", [(f"1 (UID {actual_uid} RFC822.SIZE {size} BODY[] {{{len(raw)}}}".encode(), raw), b")"]
        return "OK", [f"1 (UID {actual_uid} RFC822.SIZE {size})".encode()]

    def shutdown(self):
        self.closed = True


@pytest.fixture(autouse=True)
def fake_imap(monkeypatch):
    FakeIMAP.instances = []
    FakeIMAP.uidvalidity = b"77"
    FakeIMAP.mailbox = {"101": _message("Older"), "102": _message("Newer", attachment=True)}
    FakeIMAP.override_size = None
    FakeIMAP.override_uid = None
    FakeIMAP.expected_host = "imap.gmail.com"
    FakeIMAP.expected_account = ACCOUNT
    monkeypatch.setattr(mailbox, "_IMAP", FakeIMAP)


def test_list_uses_verified_tls_readonly_peek_and_recent_sequences():
    result = mailbox.list_messages("gmail", ACCOUNT, SECRET, limit=2)
    assert result == {"ok": True, "uidvalidity": "77", "messages": [
        {"uid": "102", "subject": "Newer", "from": "Sender <sender@example.org>",
         "date": "Sat, 17 Oct 2026 12:00:00 +0000", "size": len(FakeIMAP.mailbox["102"])},
        {"uid": "101", "subject": "Older", "from": "Sender <sender@example.org>",
         "date": "Sat, 17 Oct 2026 12:00:00 +0000", "size": len(FakeIMAP.mailbox["101"])}]}
    conn = FakeIMAP.instances[0]
    assert ("select", "INBOX", True) in conn.calls
    assert [call[1] for call in conn.calls if call[0] == "fetch"] == ["2", "1"]
    assert not any(call[0] in {"search", "store", "delete"} for call in conn.calls)
    assert conn.closed


def test_read_uses_uid_and_returns_body_hash_attachment_metadata_only():
    result = mailbox.read_message("gmail", ACCOUNT, SECRET, "102", "77")
    assert result["ok"] is True and result["uid"] == "102" and result["uidvalidity"] == "77"
    assert result["subject"] == "Newer" and result["body"].strip() == "Plain text"
    assert result["sha256"] == sha256(FakeIMAP.mailbox["102"]).hexdigest()
    assert result["attachments"] == [{"filename": "data.bin", "content_type": "application/octet-stream",
                                      "size": len(b"not saved")}]
    assert result["coverage"]["attachments"] == "metadata_only"
    assert "not saved" not in str(result)
    calls = [call for call in FakeIMAP.instances[0].calls if call[0] == "uid"]
    assert calls == [("uid", "FETCH", "102", "(UID RFC822.SIZE)"),
                     ("uid", "FETCH", "102", "(UID RFC822.SIZE BODY.PEEK[])")]


def test_html_only_is_converted_without_remote_load_or_script_text():
    FakeIMAP.mailbox = {"101": _message(html=True)}
    result = mailbox.read_message("gmail", ACCOUNT, SECRET, "101", "77")
    assert result["body"] == "Hello world"
    assert result["coverage"]["body_format"] == "html_to_plain"
    assert "bad()" not in result["body"] and "https://" not in result["body"]


def test_qq_uses_fixed_host_and_authorization_code():
    FakeIMAP.expected_host = "imap.qq.com"
    FakeIMAP.expected_account = "person@qq.com"
    result = mailbox.list_messages("qq", "person@qq.com", SECRET, limit=1)
    assert result["messages"][0]["uid"] == "102"


def test_unknown_charset_or_malformed_base64_is_not_reported_as_complete():
    FakeIMAP.mailbox = {"101": b"Subject: X\r\nContent-Type: text/plain; charset=unknown-charset\r\n\r\nhello"}
    with pytest.raises(mailbox.MailboxError, match="mail_charset_unsupported"):
        mailbox.read_message("gmail", ACCOUNT, SECRET, "101", "77")
    FakeIMAP.mailbox = {"101": b"Subject: X\r\nContent-Type: text/plain\r\n"
                                   b"Content-Transfer-Encoding: base64\r\n\r\n@@@@"}
    with pytest.raises(mailbox.MailboxError, match="mail_mime_invalid"):
        mailbox.read_message("gmail", ACCOUNT, SECRET, "101", "77")


def test_uidvalidity_changed_rejects_before_fetch():
    with pytest.raises(mailbox.MailboxError, match="mail_uidvalidity_changed"):
        mailbox.read_message("gmail", ACCOUNT, SECRET, "102", "76")
    assert not any(call[0] == "uid" for call in FakeIMAP.instances[0].calls)


def test_oversize_message_rejected_from_metadata_before_body_fetch():
    FakeIMAP.override_size = 2_000_001
    with pytest.raises(mailbox.MailboxError, match="mail_message_oversize"):
        mailbox.read_message("gmail", ACCOUNT, SECRET, "102", "77")
    assert len([call for call in FakeIMAP.instances[0].calls if call[0] == "uid"]) == 1


def test_server_uid_mismatch_rejected():
    FakeIMAP.override_uid = "103"
    with pytest.raises(mailbox.MailboxError, match="mail_uid_changed"):
        mailbox.read_message("gmail", ACCOUNT, SECRET, "102", "77")


def test_changed_size_or_truncated_response_not_reported_success():
    FakeIMAP.override_size = len(FakeIMAP.mailbox["102"]) - 1
    with pytest.raises(mailbox.MailboxError, match="mail_message_changed"):
        mailbox.read_message("gmail", ACCOUNT, SECRET, "102", "77")


def test_cancellation_before_connection_and_between_fetches():
    with pytest.raises(mailbox.MailboxError, match="mail_cancelled"):
        mailbox.list_messages("gmail", ACCOUNT, SECRET, should_stop=lambda: True)
    assert FakeIMAP.instances == []
    calls = 0

    def stop():
        nonlocal calls
        calls += 1
        return calls >= 4

    with pytest.raises(mailbox.MailboxError, match="mail_cancelled"):
        mailbox.list_messages("gmail", ACCOUNT, SECRET, should_stop=stop)
    assert FakeIMAP.instances[-1].closed


@pytest.mark.parametrize("provider,account,secret,code", [
    ("smtp", ACCOUNT, SECRET, "mail_provider_invalid"),
    ("qq", ACCOUNT, SECRET, "mail_account_invalid"),
    ("gmail", ACCOUNT, "not-the-app-code", "mail_secret_invalid"),
])
def test_invalid_credentials_rejected_without_network(provider, account, secret, code):
    with pytest.raises(mailbox.MailboxError, match=code) as error:
        mailbox.list_messages(provider, account, secret)
    assert FakeIMAP.instances == [] and secret not in str(error.value)


def test_bounded_imap_refuses_declared_large_literal_before_read():
    conn = object.__new__(mailbox._BoundedIMAP)
    conn._deadline = time.monotonic() + 60
    conn._stop = lambda: False
    conn._literal_limit = 64_000
    conn._remaining = 128_000
    conn.file = io.BytesIO(b"unused")
    with pytest.raises(mailbox.MailboxError, match="mail_message_oversize"):
        conn.read(64_001)


def test_login_rejection_closes_socket_and_hides_server_reply(monkeypatch):
    class BadLogin(FakeIMAP):
        def login(self, account, secret):
            raise imaplib.IMAP4.error("private auth reply with account and secret")

    monkeypatch.setattr(mailbox, "_IMAP", BadLogin)
    with pytest.raises(mailbox.MailboxError, match="mail_auth_failed") as error:
        mailbox.list_messages("gmail", ACCOUNT, SECRET)
    assert BadLogin.instances[-1].closed
    assert ACCOUNT not in str(error.value) and SECRET not in str(error.value)


def test_bounded_imap_line_keeps_next_response_and_caps_first_line():
    conn = object.__new__(mailbox._BoundedIMAP)
    conn._deadline = time.monotonic() + 60
    conn._stop = lambda: False
    conn._literal_limit = 64_000
    conn._remaining = 128_000
    conn.file = io.BufferedReader(io.BytesIO(b"first\r\nsecond\r\n"))
    assert conn.readline() == b"first\r\n"
    assert conn.readline() == b"second\r\n"
    conn._remaining = 4
    conn.file = io.BufferedReader(io.BytesIO(b"toolong\r\n"))
    with pytest.raises(mailbox.MailboxError, match="mail_response_oversize"):
        conn.readline()


def _unconnected_imap(deadline, stop=lambda: False):
    conn = object.__new__(mailbox._BoundedIMAP)
    conn._deadline = deadline
    conn._stop = stop
    conn.host = "imap.gmail.com"
    conn.port = 993
    conn.ssl_context = object()
    return conn


@pytest.mark.parametrize("cancel", [False, True])
def test_blocking_dns_obeys_deadline_or_cancellation(monkeypatch, cancel):
    release_dns = threading.Event()
    cancelled = threading.Event()

    def blocked_dns(*args, **kwargs):
        release_dns.wait(5)
        return []

    monkeypatch.setattr(mailbox.socket, "getaddrinfo", blocked_dns)
    if cancel:
        timer = threading.Timer(.02, cancelled.set)
        timer.start()
    conn = _unconnected_imap(time.monotonic() + (.5 if cancel else .03), cancelled.is_set)
    try:
        with pytest.raises(mailbox.MailboxError, match="mail_cancelled" if cancel else "mail_timeout"):
            conn._create_socket(15)
    finally:
        release_dns.set()
        if cancel:
            timer.join(1)


def test_stuck_dns_attempts_have_bounded_background_threads(monkeypatch):
    release_dns = threading.Event()
    entered_dns = threading.Event()
    monkeypatch.setattr(mailbox, "_DNS_SLOTS", threading.BoundedSemaphore(1))

    def blocked_dns(*args, **kwargs):
        entered_dns.set()
        release_dns.wait(5)
        return []

    monkeypatch.setattr(mailbox.socket, "getaddrinfo", blocked_dns)
    try:
        with pytest.raises(mailbox.MailboxError, match="mail_timeout"):
            _unconnected_imap(time.monotonic() + .03)._create_socket(15)
        assert entered_dns.is_set()
        with pytest.raises(mailbox.MailboxError, match="mail_dns_busy"):
            _unconnected_imap(time.monotonic() + .5)._create_socket(15)
    finally:
        release_dns.set()


def test_failed_numeric_connect_closes_socket_without_redns(monkeypatch):
    created = []

    class FailedSocket:
        def __init__(self, family, kind, proto):
            assert (family, kind, proto) == (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP)
            self.closed = False
            created.append(self)

        def settimeout(self, value):
            assert 0 < value <= 15

        def connect(self, address):
            assert address == ("127.0.0.1", 993)
            raise OSError("private connection detail")

        def close(self):
            self.closed = True

    monkeypatch.setattr(mailbox.socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 993))])
    monkeypatch.setattr(mailbox.socket, "socket", FailedSocket)
    conn = _unconnected_imap(time.monotonic() + 2)
    with pytest.raises(OSError, match="mail connection failed"):
        conn._create_socket(15)
    assert len(created) == 1 and created[0].closed


def test_tls_failure_keeps_original_hostname_and_closes_socket(monkeypatch):
    created = []
    wrapped = []

    class NumericSocket:
        def __init__(self, *args):
            self.closed = False
            created.append(self)

        def settimeout(self, value):
            assert 0 < value <= 15

        def connect(self, address):
            assert address == ("127.0.0.1", 993)

        def close(self):
            self.closed = True

    class FailedTLS:
        def wrap_socket(self, sock, *, server_hostname):
            wrapped.append(server_hostname)
            raise mailbox.ssl.SSLError("private certificate detail")

    monkeypatch.setattr(mailbox.socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 993))])
    monkeypatch.setattr(mailbox.socket, "socket", NumericSocket)
    conn = _unconnected_imap(time.monotonic() + 2)
    conn.ssl_context = FailedTLS()
    with pytest.raises(mailbox.ssl.SSLError):
        conn._create_socket(15)
    assert wrapped == ["imap.gmail.com"] and created[0].closed
