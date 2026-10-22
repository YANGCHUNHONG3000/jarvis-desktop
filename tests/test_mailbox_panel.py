import sys
import threading
from types import ModuleType

from agent_service.mailbox_panel import (
    _account_values,
    _error_message,
    _export_candidate,
    _public_result,
    _safe_code,
    _secret_shape,
    _session_matches,
    attach_mailbox_tab,
)


def test_safe_error_display_never_uses_untrusted_exception_text():
    class BadError(Exception):
        safe_code = "invalid_credential"

    assert _safe_code(BadError("secret: don't show me")) == "invalid_credential"
    assert _safe_code(RuntimeError("password=hidden")) == "mailbox_failed"


def test_special_credential_format_precheck_does_not_claim_to_identify_password_type():
    assert _secret_shape("abcd efgh ijkl mnop")
    assert _secret_shape("1234567890ABCDEF")
    assert not _secret_shape("ordinary-password")
    assert not _secret_shape("密码12345678901234")
    assert not _secret_shape("abcdefghijklmnop!")
    assert "普通登录密码" in _error_message("mail_secret_invalid")
    assert "有效" in _error_message("mail_auth_failed")
    assert "password=hidden" not in _error_message("password=hidden")
    assert "mail_timeout" not in _error_message("mail_timeout")
    assert "mail_dns_busy" not in _error_message("mail_dns_busy")


def test_session_matching_prevents_account_or_generation_mixups():
    session = {"provider": "qq", "account": "a@qq.com", "generation": 7}
    assert _session_matches(session, "qq", "a@qq.com", 7)
    assert not _session_matches(session, "gmail", "a@qq.com", 7)
    assert not _session_matches(session, "qq", "b@qq.com", 7)
    assert not _session_matches(session, "qq", "a@qq.com", 8)


def test_local_account_metadata_is_filtered_to_provider_addresses():
    records = [{"provider": "qq", "account": "a@qq.com"},
               {"provider": "gmail", "account": "b@gmail.com"},
               {"provider": "qq", "account": "x@gmail.com"},
               {"provider": "qq", "account": "a@qq.com"},
               {"provider": "qq", "account": "not-an-email"}]
    assert _account_values(records, "qq") == ("a@qq.com",)
    assert _account_values(records, "gmail") == ("b@gmail.com",)


def test_worker_result_projection_never_queues_credential_fields():
    result = _public_result("fetch", {
        "ok": True, "uidvalidity": "11", "secret": "must-not-queue",
        "messages": [{"uid": 2, "subject": "hello", "from": "a@example.com",
                      "date": "today", "size": 12, "credential": "must-not-queue"}],
    })
    assert result == {"ok": True, "uidvalidity": "11", "messages": [
        {"uid": 2, "subject": "hello", "from": "a@example.com", "date": "today", "size": 12}
    ]}

    read_result = _public_result("read", {
        "ok": True, "body": "local body", "secret": "must-not-queue",
        "coverage": {"scope": "selected_full_message", "body_format": "plain",
                     "private": "must-not-queue"},
    })
    assert read_result["coverage"] == {"scope": "selected_full_message", "body_format": "plain"}
    assert "secret" not in read_result


def test_export_candidate_requires_complete_qq_read_and_projects_only_metadata():
    result = {"ok": True, "uid": "12", "uidvalidity": "7", "subject": "Synthetic",
              "from": "sender@example.test", "to": "reader@qq.com", "date": "today",
              "body": "Hello", "attachments": [{"filename": "summary.txt",
                                                  "content_type": "text/plain", "size": 5}],
              "sha256": "a" * 64, "coverage": {
                  "scope": "selected_full_message", "body_format": "text/plain",
                  "parts": 2, "attachments": "metadata_only",
                  "html_alternative_ignored": False},
              "secret": "must never export", "account": "reader@qq.com"}
    snapshot = _export_candidate("qq", result)
    assert snapshot is not None
    assert "secret" not in snapshot and "account" not in snapshot
    assert _export_candidate("gmail", result) is None
    assert _export_candidate("qq", {**result, "uid": "13"}) is not None
    assert _export_candidate("qq", {**result, "coverage": {"scope": "truncated"}}) is None
    assert _export_candidate("qq", {**result, "body": "x" * 200_001}) is None
    assert _export_candidate("qq", {**result, "coverage": {
        **result["coverage"], "body_format": "none"}}) is None


def test_tab_construction_does_not_import_mailbox_or_start_threads(monkeypatch):
    import agent_service.mailbox_panel as panel
    monkeypatch.delitem(sys.modules, "agent_service.mailbox", raising=False)

    class Widget:
        def __init__(self, *args, **kwargs):
            self.kwargs = kwargs
            self.children = []

        def pack(self, *args, **kwargs):
            pass

        def configure(self, *args, **kwargs):
            pass

        def bind(self, *args, **kwargs):
            pass

        def heading(self, *args, **kwargs):
            pass

        def column(self, *args, **kwargs):
            pass

        def get_children(self):
            return tuple(self.children)

        def delete(self, *args, **kwargs):
            pass

        def insert(self, *args, **kwargs):
            pass

        def selection(self):
            return ()

    class Variable:
        def __init__(self, value=None):
            self.value = value

        def get(self):
            return self.value

        def set(self, value):
            self.value = value

        def trace_add(self, *_args):
            pass

    class Notebook(Widget):
        def add(self, *_args, **_kwargs):
            pass

    class Root:
        def after(self, *_args):
            pass

    tk = ModuleType("tkinter")
    tk.StringVar = Variable
    tk.BooleanVar = Variable
    tk.Text = Widget
    ttk = ModuleType("tkinter.ttk")
    for name in ("Frame", "Label", "Combobox", "Entry", "Checkbutton", "Button", "Treeview"):
        setattr(ttk, name, Widget)
    tk.ttk = ttk
    monkeypatch.setitem(sys.modules, "tkinter", tk)
    monkeypatch.setitem(sys.modules, "tkinter.ttk", ttk)

    class NoThread:
        def __init__(self, *args, **kwargs):
            raise AssertionError("tab construction must not start worker threads")

    monkeypatch.setattr(panel.threading, "Thread", NoThread)
    tab = attach_mailbox_tab(Notebook(), Root())
    assert tab is not None
    assert "agent_service.mailbox" not in sys.modules


def test_explicit_fetch_uses_captured_secret_but_never_queues_or_displays_it(monkeypatch):
    import queue
    import agent_service
    import agent_service.mailbox_panel as panel

    class Widget:
        all = []

        def __init__(self, *args, **kwargs):
            self.kwargs = kwargs
            self.children = []
            self.command = kwargs.get("command")
            self.values = None
            Widget.all.append(self)

        def pack(self, *args, **kwargs): pass
        def bind(self, *args, **kwargs): pass
        def heading(self, *args, **kwargs): pass
        def column(self, *args, **kwargs): pass
        def configure(self, *args, **kwargs):
            self.kwargs.update(kwargs)
            self.command = kwargs.get("command", self.command)
            self.values = kwargs.get("values", self.values)
        def insert(self, *args, **kwargs): self.children.append(kwargs.get("iid", "row"))
        def delete(self, *args, **kwargs): self.children.clear()
        def get_children(self): return tuple(self.children)
        def selection(self): return ()
        def invoke(self): self.command()

    class Variable:
        all = []
        def __init__(self, value=None): self.value, self.callbacks = value, []
        def get(self): return self.value
        def set(self, value):
            self.value = value
            for callback in self.callbacks: callback("var", "", "write")
        def trace_add(self, _mode, callback): self.callbacks.append(callback)

    class Notebook(Widget):
        def add(self, *_args, **_kwargs): pass

    class Root:
        def __init__(self): self.callbacks = []
        def after(self, _delay, callback): self.callbacks.append(callback)

    tk = ModuleType("tkinter")
    tk.StringVar = Variable
    tk.BooleanVar = Variable
    tk.Text = Widget
    ttk = ModuleType("tkinter.ttk")
    for name in ("Frame", "Label", "Combobox", "Entry", "Checkbutton", "Button", "Treeview"):
        setattr(ttk, name, Widget)
    tk.ttk = ttk
    monkeypatch.setitem(sys.modules, "tkinter", tk)
    monkeypatch.setitem(sys.modules, "tkinter.ttk", ttk)

    secret = "abcd efgh ijkl mnop"
    calls = []
    mailbox = ModuleType("agent_service.mailbox")
    def list_messages(provider, account, password, *, limit, should_stop):
        calls.append((provider, account, password, limit))
        return {"ok": True, "uidvalidity": "9", "secret": password,
                "messages": [{"uid": "12", "subject": "Hello", "from": "sender@example.com",
                              "date": "today", "size": 100, "secret": password}]}
    mailbox.list_messages = list_messages
    monkeypatch.setitem(sys.modules, "agent_service.mailbox", mailbox)
    monkeypatch.setattr(agent_service, "mailbox", mailbox, raising=False)
    credentials = ModuleType("agent_service.mail_credentials")
    credentials.list_accounts = lambda: []
    credentials.save_credential = lambda *_args: None
    monkeypatch.setitem(sys.modules, "agent_service.mail_credentials", credentials)

    queued = []
    class CaptureQueue:
        def put(self, item): queued.append(item)
        def get_nowait(self):
            if not queued: raise queue.Empty
            return queued.pop(0)
    monkeypatch.setattr(panel.queue, "Queue", CaptureQueue)

    class InlineThread:
        def __init__(self, *, target, args, kwargs, **_other):
            self.target, self.args, self.kwargs = target, args, kwargs
        def start(self): self.target(*self.args, **self.kwargs)
    monkeypatch.setattr(panel.threading, "Thread", InlineThread)

    root = Root()
    attach_mailbox_tab(Notebook(), root)
    variables = [widget.kwargs["textvariable"] for widget in Widget.all
                 if "textvariable" in widget.kwargs]
    provider_var, account_var, credential_var, remember_var = variables[:4]
    account_var.set("person@qq.com")
    credential_var.set(secret)
    # Before any session exists, changing provider or account must discard the
    # credential typed for the previous identity. Rewriting the same value
    # should not discard a credential for the current identity.
    provider_var.set("gmail")
    assert credential_var.get() == ""
    account_var.set("person@gmail.com")
    credential_var.set(secret)
    account_var.set("person@gmail.com")
    assert credential_var.get() == secret
    account_var.set("other@gmail.com")
    assert credential_var.get() == ""
    provider_var.set("qq")
    assert credential_var.get() == ""
    account_var.set("person@qq.com")
    credential_var.set(secret)
    provider_var.set("qq")
    assert credential_var.get() == secret
    fetch = next(widget for widget in Widget.all
                 if widget.kwargs.get("text") == "连接并读取最近邮件")
    fetch.invoke()

    assert calls == [("qq", "person@qq.com", secret, 20)]
    assert secret not in repr(queued)
    root.callbacks.pop(0)()
    assert secret not in repr([widget.kwargs for widget in Widget.all])
