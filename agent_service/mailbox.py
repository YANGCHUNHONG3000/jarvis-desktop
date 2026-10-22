"""Explicit, read-only IMAP previews for one locally chosen mailbox.

No credential is stored here. Message bytes are bounded in memory and never
written, executed, sent to a model, or fetched as separate attachments.
"""

from email.parser import BytesHeaderParser, BytesParser
from email.policy import default
from hashlib import sha256
from html.parser import HTMLParser
import imaplib
import ipaddress
import queue
import re
import socket
import ssl
import sys
import threading
import time


_HOSTS = {"qq": "imap.qq.com", "gmail": "imap.gmail.com"}
_ACCOUNT = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@([A-Za-z0-9.-]+)\Z")
_NUMBER = re.compile(rb"[1-9][0-9]*\Z")
_UID = re.compile(rb"(?<![A-Z0-9])UID ([1-9][0-9]*)(?![0-9])")
_SIZE = re.compile(rb"(?<![A-Z0-9])RFC822\.SIZE ([0-9]+)(?![0-9])")
_HEADER_LIMIT = 64_000
_MESSAGE_LIMIT = 2_000_000
_BODY_LIMIT = 200_000
_PART_LIMIT = 32
_DEPTH_LIMIT = 8
_MAX_MESSAGES = 20
_SECONDS = 60
_OP_SECONDS = 15
_DNS_SLOTS = threading.BoundedSemaphore(2)


class MailboxError(ValueError):
    """A stable error code; raw server replies and credentials stay private."""

    def __init__(self, code):
        self.safe_code = code
        super().__init__(code)


def _check(stop, deadline):
    if stop():
        raise MailboxError("mail_cancelled")
    if time.monotonic() >= deadline:
        raise MailboxError("mail_timeout")


class _BoundedIMAP(imaplib.IMAP4_SSL):
    """Cap IMAP lines/literals before imaplib allocates their declared lengths."""

    def __init__(self, host, port, *, ssl_context, timeout, deadline, should_stop):
        self._deadline = deadline
        self._stop = should_stop
        self._literal_limit = _HEADER_LIMIT
        self._remaining = _HEADER_LIMIT * 2
        super().__init__(host, port, ssl_context=ssl_context, timeout=timeout)

    def _create_socket(self, timeout):
        # socket.create_connection performs unbounded DNS before its socket
        # timeout applies. Resolve on a daemon so the caller can honor its
        # deadline and cancellation, then connect only to numeric results.
        _check(self._stop, self._deadline)
        sys.audit("imaplib.open", self, self.host, self.port)
        dns_slots = _DNS_SLOTS
        if not dns_slots.acquire(blocking=False):
            raise MailboxError("mail_dns_busy")
        answer = queue.Queue(maxsize=1)

        def resolve():
            try:
                try:
                    value = socket.getaddrinfo(self.host, self.port, type=socket.SOCK_STREAM,
                                               proto=socket.IPPROTO_TCP)
                except Exception as error:
                    value = error
                answer.put_nowait(value)
            finally:
                dns_slots.release()

        try:
            threading.Thread(target=resolve, name="jarvis-imap-dns", daemon=True).start()
        except Exception:
            dns_slots.release()
            raise
        while True:
            _check(self._stop, self._deadline)
            try:
                addresses = answer.get(timeout=min(.1, max(.001, self._deadline - time.monotonic())))
                break
            except queue.Empty:
                continue
        if isinstance(addresses, Exception):
            raise OSError("mail address resolution failed") from None
        if type(addresses) is not list:
            raise OSError("mail address resolution invalid")

        attempted = 0
        for family, socktype, proto, _, sockaddr in addresses[:8]:
            if family not in {socket.AF_INET, socket.AF_INET6} or socktype != socket.SOCK_STREAM:
                continue
            if proto not in {0, socket.IPPROTO_TCP} or not isinstance(sockaddr, tuple):
                continue
            try:
                # Reject a resolver result that would make connect re-resolve.
                if ipaddress.ip_address(sockaddr[0]).version != (4 if family == socket.AF_INET else 6):
                    continue
                if sockaddr[1] != self.port:
                    continue
            except (IndexError, TypeError, ValueError):
                continue
            _check(self._stop, self._deadline)
            attempted += 1
            raw_sock = None
            secure_sock = None
            try:
                raw_sock = socket.socket(family, socket.SOCK_STREAM, proto)
                raw_sock.settimeout(min(_OP_SECONDS, max(.001, self._deadline - time.monotonic())))
                raw_sock.connect(sockaddr)
                _check(self._stop, self._deadline)
                # Keep the original provider hostname for certificate checks.
                raw_sock.settimeout(min(_OP_SECONDS, max(.001, self._deadline - time.monotonic())))
                secure_sock = self.ssl_context.wrap_socket(raw_sock, server_hostname=self.host)
                _check(self._stop, self._deadline)
                return secure_sock
            except Exception as error:
                if secure_sock is not None:
                    secure_sock.close()
                if raw_sock is not None:
                    raw_sock.close()
                if isinstance(error, (MailboxError, ssl.SSLError)) or not isinstance(error, OSError):
                    raise
        if not attempted:
            raise OSError("mail address resolution returned no usable address")
        raise OSError("mail connection failed")

    def set_limits(self, literal_limit, response_limit):
        self._literal_limit = literal_limit
        self._remaining = response_limit

    def _guard(self):
        _check(self._stop, self._deadline)
        if hasattr(self, "sock"):
            self.sock.settimeout(min(_OP_SECONDS, max(.001, self._deadline - time.monotonic())))

    def _consume(self, count):
        self._remaining -= count
        if self._remaining < 0:
            raise MailboxError("mail_response_oversize")

    def read(self, size):
        if type(size) is not int or size < 0 or size > self._literal_limit or size > self._remaining:
            raise MailboxError("mail_message_oversize")
        chunks = []
        remaining = size
        while remaining:
            self._guard()
            # read1 performs at most one socket read. BufferedReader.read can
            # wait for the full requested length while a peer trickles bytes.
            chunk = self.file.read1(min(8192, remaining))
            if not chunk:
                raise MailboxError("mail_truncated")
            self._consume(len(chunk))
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def readline(self):
        limit = min(_HEADER_LIMIT + 1024, self._remaining)
        pieces = []
        length = 0
        while length <= limit:
            self._guard()
            # peek makes at most one socket read, then consume precisely up
            # to LF so the next protocol response remains in the buffer.
            available = self.file.peek(1)
            if not available:
                raise MailboxError("mail_truncated")
            left = limit + 1 - length
            end = available.find(b"\n", 0, left)
            take = end + 1 if end >= 0 else min(len(available), left)
            chunk = self.file.read(take)
            self._consume(len(chunk))
            pieces.append(chunk)
            length += len(chunk)
            if end >= 0:
                return b"".join(pieces)
        raise MailboxError("mail_response_oversize")


_IMAP = _BoundedIMAP


def _credentials(provider, account, secret):
    if type(provider) is not str or provider not in _HOSTS:
        raise MailboxError("mail_provider_invalid")
    if type(account) is not str or not 3 <= len(account) <= 254 or not account.isascii():
        raise MailboxError("mail_account_invalid")
    match = _ACCOUNT.fullmatch(account)
    if match is None or match.group(1).lower() != provider + ".com":
        raise MailboxError("mail_account_invalid")
    if (type(secret) is not str or not 1 <= len(secret) <= 256 or not secret.isascii()
            or any(ord(char) < 33 or ord(char) > 126 for char in secret if char != " ")):
        raise MailboxError("mail_secret_invalid")
    # Both supported local IMAP routes use an application-specific code. Spaces
    # are accepted only as visual grouping in a 16-character code.
    normalized = secret.replace(" ", "")
    if not re.fullmatch(r"[A-Za-z0-9]{16}", normalized):
        raise MailboxError("mail_secret_invalid")
    return _HOSTS[provider], account, normalized


def _number(value, code):
    if type(value) is not bytes or not _NUMBER.fullmatch(value) or int(value) > 4_294_967_295:
        raise MailboxError(code)
    return value.decode("ascii")


def _select(conn, deadline, stop):
    _check(stop, deadline)
    _limits(conn, _HEADER_LIMIT, _HEADER_LIMIT * 2)
    kind, data = conn.select("INBOX", readonly=True)
    if kind != "OK" or not isinstance(data, list) or len(data) != 1:
        raise MailboxError("mail_select_failed")
    raw_count = data[0]
    if type(raw_count) is not bytes or not re.fullmatch(rb"[0-9]+", raw_count):
        raise MailboxError("mail_select_invalid")
    count = int(raw_count)
    if count > 2**32 - 1:
        raise MailboxError("mail_select_invalid")
    _, response = conn.response("UIDVALIDITY")
    if not isinstance(response, list) or len(response) != 1:
        raise MailboxError("mail_uidvalidity_missing")
    uidvalidity = _number(response[0], "mail_uidvalidity_invalid")
    return count, uidvalidity


def _limits(conn, literal, total):
    setter = getattr(conn, "set_limits", None)
    if callable(setter):
        setter(literal, total)


def _fetch_parts(data, *, literal):
    if type(data) is not list:
        raise MailboxError("mail_fetch_invalid")
    entries = [item for item in data if isinstance(item, tuple)]
    if len(entries) != 1 or len(entries[0]) != 2:
        raise MailboxError("mail_fetch_invalid")
    metadata, payload = entries[0]
    if type(metadata) is not bytes or type(payload) is not bytes:
        raise MailboxError("mail_fetch_invalid")
    uid_matches = _UID.findall(metadata)
    size_matches = _SIZE.findall(metadata)
    if len(uid_matches) != 1 or len(size_matches) != 1:
        raise MailboxError("mail_fetch_invalid")
    uid = _number(uid_matches[0], "mail_uid_invalid")
    size = int(size_matches[0])
    if size < 0 or size > 2**32 - 1 or len(payload) > literal:
        raise MailboxError("mail_message_oversize")
    return uid, size, payload


def _fetch_meta(data):
    if type(data) is not list:
        raise MailboxError("mail_fetch_invalid")
    entries = [item for item in data if type(item) is bytes and b"UID " in item]
    if len(entries) != 1:
        raise MailboxError("mail_fetch_invalid")
    uid_matches = _UID.findall(entries[0])
    size_matches = _SIZE.findall(entries[0])
    if len(uid_matches) != 1 or len(size_matches) != 1:
        raise MailboxError("mail_fetch_invalid")
    uid = _number(uid_matches[0], "mail_uid_invalid")
    size = int(size_matches[0])
    if size > 2**32 - 1:
        raise MailboxError("mail_message_oversize")
    return uid, size


def _header_text(message, key, *, maximum=500):
    values = message.get_all(key, [])
    if len(values) > 1:
        raise MailboxError("mail_header_invalid")
    if not values:
        return ""
    header = values[0]
    if getattr(header, "defects", ()):
        raise MailboxError("mail_header_invalid")
    value = " ".join(str(header).split())
    if (len(value) > maximum or any(ord(char) < 32 or 0x7f <= ord(char) <= 0x9f
                                    for char in value)):
        raise MailboxError("mail_header_invalid")
    return value


def _header_preview(raw):
    if len(raw) > _HEADER_LIMIT:
        raise MailboxError("mail_header_oversize")
    message = BytesHeaderParser(policy=default).parsebytes(raw)
    if message.defects:
        raise MailboxError("mail_header_invalid")
    return {"subject": _header_text(message, "Subject"),
            "from": _header_text(message, "From"),
            "date": _header_text(message, "Date", maximum=100)}


def _header_section(raw):
    positions = [value for value in (raw.find(b"\r\n\r\n"), raw.find(b"\n\n")) if value >= 0]
    if not positions or min(positions) > _HEADER_LIMIT:
        raise MailboxError("mail_header_oversize")
    return raw[:min(positions)]


class _PlainHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._hidden = 0
        self._pieces = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "head", "template", "iframe", "svg", "object"}:
            self._hidden += 1
        elif not self._hidden and tag in {"p", "div", "br", "li", "tr", "h1", "h2", "h3"}:
            self._pieces.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "head", "template", "iframe", "svg", "object"}:
            self._hidden = max(0, self._hidden - 1)
        elif not self._hidden and tag in {"p", "div", "li", "tr"}:
            self._pieces.append("\n")

    def handle_data(self, data):
        if not self._hidden:
            self._pieces.append(data)

    def text(self):
        return "\n".join(line.strip() for line in "".join(self._pieces).splitlines() if line.strip())


def _payload(part):
    encoding = (part.get("Content-Transfer-Encoding") or "7bit").lower().strip()
    if encoding not in {"7bit", "8bit", "binary", "base64", "quoted-printable"}:
        raise MailboxError("mail_encoding_unsupported")
    value = part.get_payload(decode=True)
    if part.defects or type(value) is not bytes:
        raise MailboxError("mail_mime_invalid")
    return value


def _text(part):
    raw = _payload(part)
    charset = part.get_content_charset() or "us-ascii"
    try:
        value = raw.decode(charset, errors="strict")
    except (LookupError, UnicodeError):
        raise MailboxError("mail_charset_unsupported") from None
    if len(value) > _BODY_LIMIT:
        raise MailboxError("mail_body_oversize")
    return value


def _mime(raw):
    _header_section(raw)
    message = BytesParser(policy=default).parsebytes(raw)
    if message.defects:
        raise MailboxError("mail_mime_invalid")
    plain, html, attachments = [], [], []
    count = 0

    def visit(part, depth):
        nonlocal count
        count += 1
        if depth > _DEPTH_LIMIT or count > _PART_LIMIT or part.defects:
            raise MailboxError("mail_mime_unsupported")
        content_type = part.get_content_type().lower()
        if (content_type in {"multipart/encrypted", "multipart/signed", "application/pkcs7-mime",
                             "application/x-pkcs7-mime", "application/pgp-encrypted"}
                or content_type.startswith("application/vnd.ms-outlook")):
            raise MailboxError("mail_encrypted_unsupported")
        if part.is_multipart():
            if content_type not in {"multipart/mixed", "multipart/alternative", "multipart/related"}:
                raise MailboxError("mail_mime_unsupported")
            for child in part.iter_parts():
                visit(child, depth + 1)
            return
        disposition = part.get_content_disposition()
        filename = part.get_filename()
        if filename is not None:
            filename = " ".join(filename.split())
            if (not filename or len(filename) > 255 or "/" in filename or "\\" in filename
                    or any(ord(char) < 32 for char in filename)):
                raise MailboxError("mail_filename_invalid")
        if disposition == "attachment" or filename is not None or content_type not in {"text/plain", "text/html"}:
            payload = _payload(part)
            attachments.append({"filename": filename or "", "content_type": content_type,
                                "size": len(payload)})
            return
        if content_type == "text/plain":
            plain.append(_text(part))
        else:
            html.append(_text(part))

    visit(message, 0)
    if plain:
        body = "\n\n".join(plain)
        body_format = "text/plain"
    elif html:
        parser = _PlainHTML()
        parser.feed("\n\n".join(html))
        parser.close()
        body = parser.text()
        body_format = "html_to_plain"
    else:
        body, body_format = "", "none"
    if len(body) > _BODY_LIMIT:
        raise MailboxError("mail_body_oversize")
    return {"subject": _header_text(message, "Subject"),
            "from": _header_text(message, "From"), "to": _header_text(message, "To", maximum=1000),
            "date": _header_text(message, "Date", maximum=100), "body": body,
            "attachments": attachments,
            "coverage": {"scope": "selected_full_message", "body_format": body_format,
                         "parts": count, "attachments": "metadata_only",
                         "html_alternative_ignored": bool(plain and html)}}


def _connect(provider, account, secret, deadline, stop):
    host, account, secret = _credentials(provider, account, secret)
    _check(stop, deadline)
    context = ssl.create_default_context()
    conn = _IMAP(host, 993, ssl_context=context,
                 timeout=min(_OP_SECONDS, max(.001, deadline - time.monotonic())),
                 deadline=deadline, should_stop=stop)
    try:
        _check(stop, deadline)
        _limits(conn, _HEADER_LIMIT, _HEADER_LIMIT * 2)
        try:
            kind, _ = conn.login(account, secret)
        except imaplib.IMAP4.error:
            raise MailboxError("mail_auth_failed") from None
        if kind != "OK":
            raise MailboxError("mail_auth_failed")
        return conn
    except Exception:
        _disconnect(conn)
        raise


def _disconnect(conn):
    if conn is None:
        return
    try:
        conn.shutdown()
    except Exception:
        pass


def _safe_call(operation):
    try:
        return operation()
    except MailboxError:
        raise
    except (socket.timeout, TimeoutError):
        raise MailboxError("mail_timeout") from None
    except ssl.SSLError:
        raise MailboxError("mail_tls_failed") from None
    except imaplib.IMAP4.error:
        raise MailboxError("mail_server_failed") from None
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, IndexError):
        raise MailboxError("mail_operation_failed") from None
    except Exception:
        raise MailboxError("mail_operation_failed") from None


def list_messages(provider, account, secret, *, limit=20, should_stop=lambda: False):
    """Preview headers of at most twenty recent sequence positions, newest first."""
    if type(limit) is not int or not 1 <= limit <= _MAX_MESSAGES:
        raise MailboxError("mail_limit_invalid")
    if not callable(should_stop):
        raise MailboxError("mail_cancel_callback_invalid")

    def operation():
        deadline = time.monotonic() + _SECONDS
        conn = None
        try:
            conn = _connect(provider, account, secret, deadline, should_stop)
            count, uidvalidity = _select(conn, deadline, should_stop)
            messages = []
            seen = set()
            for sequence in range(count, max(0, count - limit), -1):
                _check(should_stop, deadline)
                _limits(conn, _HEADER_LIMIT, _HEADER_LIMIT * 2)
                kind, data = conn.fetch(str(sequence),
                                        "(UID RFC822.SIZE BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE)])")
                if kind != "OK":
                    raise MailboxError("mail_fetch_failed")
                uid, size, header = _fetch_parts(data, literal=_HEADER_LIMIT)
                if uid in seen:
                    raise MailboxError("mail_uid_duplicate")
                seen.add(uid)
                messages.append({"uid": uid, **_header_preview(header), "size": size})
            _check(should_stop, deadline)
            return {"ok": True, "uidvalidity": uidvalidity, "messages": messages}
        finally:
            _disconnect(conn)

    return _safe_call(operation)


def read_message(provider, account, secret, uid, uidvalidity, *, should_stop=lambda: False):
    """Read one explicit UID with BODY.PEEK after rechecking UIDVALIDITY."""
    if not callable(should_stop):
        raise MailboxError("mail_cancel_callback_invalid")
    try:
        wanted_uid = _number(uid.encode("ascii") if type(uid) is str else b"", "mail_uid_invalid")
        wanted_validity = _number(uidvalidity.encode("ascii") if type(uidvalidity) is str else b"",
                                  "mail_uidvalidity_invalid")
    except UnicodeError:
        raise MailboxError("mail_uid_invalid") from None

    def operation():
        deadline = time.monotonic() + _SECONDS
        conn = None
        try:
            conn = _connect(provider, account, secret, deadline, should_stop)
            _, current_validity = _select(conn, deadline, should_stop)
            if current_validity != wanted_validity:
                raise MailboxError("mail_uidvalidity_changed")
            _check(should_stop, deadline)
            _limits(conn, _HEADER_LIMIT, _HEADER_LIMIT * 2)
            kind, data = conn.uid("FETCH", wanted_uid, "(UID RFC822.SIZE)")
            if kind != "OK":
                raise MailboxError("mail_fetch_failed")
            fetched_uid, size = _fetch_meta(data)
            if fetched_uid != wanted_uid:
                raise MailboxError("mail_uid_changed")
            if size > _MESSAGE_LIMIT:
                raise MailboxError("mail_message_oversize")
            _check(should_stop, deadline)
            _limits(conn, _MESSAGE_LIMIT, _MESSAGE_LIMIT + _HEADER_LIMIT)
            kind, data = conn.uid("FETCH", wanted_uid, "(UID RFC822.SIZE BODY.PEEK[])")
            if kind != "OK":
                raise MailboxError("mail_fetch_failed")
            fetched_uid, fetched_size, raw = _fetch_parts(data, literal=_MESSAGE_LIMIT)
            if fetched_uid != wanted_uid or fetched_size != size or len(raw) != size:
                raise MailboxError("mail_message_changed")
            _check(should_stop, deadline)
            parsed = _mime(raw)
            _check(should_stop, deadline)
            return {"ok": True, **parsed, "uid": wanted_uid, "uidvalidity": current_validity,
                    "sha256": sha256(raw).hexdigest()}
        finally:
            _disconnect(conn)

    return _safe_call(operation)
