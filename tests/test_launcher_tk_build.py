"""Build this application's real Tk widgets without showing a window or networking."""
import tkinter as tk
from tkinter import ttk
from types import ModuleType
import sys
import gc
import pytest


@pytest.fixture(scope="module")
def hidden_tk():
    root = tk.Tk()
    root.withdraw()
    yield root
    root.destroy()
    # Release Tcl variables on the main thread before later concurrency tests.
    gc.collect()


def _descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from _descendants(child)


def _mailbox_widgets(hidden_tk, on_export=None):
    from agent_service.mailbox_panel import attach_mailbox_tab

    root = hidden_tk
    try:
        notebook = ttk.Notebook(root)
        notebook.pack(fill="both", expand=True)

        class PollRoot:
            def __init__(self):
                self.callbacks = []

            def after(self, _delay, callback):
                self.callbacks.append(callback)

        poll_root = PollRoot()
        tab = attach_mailbox_tab(notebook, poll_root, on_export=on_export)
        assert tab is not None
        widgets = list(_descendants(tab))
        buttons = {widget.cget("text"): widget for widget in widgets if isinstance(widget, ttk.Button)}
        account = next(widget for widget in widgets
                       if isinstance(widget, ttk.Combobox) and int(widget.cget("width")) == 34)
        credential = next(widget for widget in widgets
                          if isinstance(widget, ttk.Entry) and widget.cget("show"))
        status = next(widget for widget in tab.winfo_children()
                      if isinstance(widget, ttk.Label) and widget.cget("textvariable"))
        return root, tab, poll_root, buttons, account, credential, status
    except Exception:
        notebook.destroy()
        raise


def test_real_tk_build_has_mailbox_and_preserves_full_source_path(tmp_path, monkeypatch, hidden_tk):
    import run_jarvis_assistant as launcher
    source = tmp_path / "资料.csv"
    source.write_text("名称,数量\n示例,1\n", encoding="utf-8")
    original_tk = tk.Tk
    def hidden_root():
        return hidden_tk

    class NoStartThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    seen = []

    def check_tree(root):
        root.update_idletasks()
        tabs = next(widget for widget in root.winfo_children() if isinstance(widget, ttk.Notebook))
        labels = [tabs.tab(tab, "text") for tab in tabs.tabs()]
        assert "邮箱（只读）" in labels and "邮箱（未启动）" not in labels
        assert "历史任务" in labels
        assert "本机能力" in labels

        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)

        fields = [widget.get() for widget in descendants(root) if isinstance(widget, ttk.Entry)]
        assert str(source.resolve()) in fields
        reuse = [widget for widget in descendants(root) if isinstance(widget, ttk.Button)
                 and widget.cget("text") == "用于下一任务"]
        assert len(reuse) == 2
        assert all(str(widget.cget("state")) == "disabled" for widget in reuse)
        local_run = [widget for widget in descendants(root) if isinstance(widget, ttk.Button)
                     and widget.cget("text") == "运行本地技能（不调用模型）"]
        assert len(local_run) == 1 and str(local_run[0].cget("state")) == "disabled"
        rendered = "\n".join(str(widget.cget("text")) for widget in descendants(root)
                             if isinstance(widget, (tk.Label, ttk.Label)))
        assert "点击“开始任务”时" in rendered
        assert "运行本地技能不调用模型、不上传资料" in rendered
        assert any("本地未发送草稿" in label for label in launcher.SKILL_CHOICES)
        assert any("仅新建 Excel" in label for label in launcher.SKILL_CHOICES)
        assert any("Word表格转Excel与报告" in label for label in launcher.SKILL_CHOICES)
        assert "不求和、不生成报告或草稿" in rendered
        assert "仅提取唯一表格" in rendered
        assert "已有合计保留、不重新计算" in rendered
        seen.append(True)

    monkeypatch.setattr(tk, "Tk", hidden_root)
    monkeypatch.setattr(original_tk, "mainloop", check_tree)
    monkeypatch.setattr(launcher.threading, "Thread", NoStartThread)
    launcher.main(["--source-file", str(source)])
    assert seen == [True]


def test_mailbox_status_is_between_action_buttons_and_expanding_mail_views(hidden_tk):
    root, tab, _, buttons, _, _, status = _mailbox_widgets(hidden_tk)
    try:
        root.update_idletasks()
        packed = tab.pack_slaves()
        action_row = buttons["连接并读取最近邮件"].master
        tree = next(child for child in tab.winfo_children() if isinstance(child, ttk.Treeview))
        assert packed.index(action_row) < packed.index(status) < packed.index(tree)
        assert int(status.cget("wraplength")) == 620
        assert "普通登录密码不能用于此连接" in root.getvar(status.cget("textvariable"))
    finally:
        tab.master.destroy()


def test_invalid_code_is_rejected_locally_and_thread_start_failure_restores_buttons(monkeypatch, hidden_tk):
    import agent_service.mailbox_panel as panel

    class FailedThread:
        attempts = 0

        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            self.__class__.attempts += 1
            raise RuntimeError("private OS message")

    monkeypatch.setattr(panel.threading, "Thread", FailedThread)
    root, tab, _, buttons, account, credential, status = _mailbox_widgets(hidden_tk)
    try:
        account.insert(0, "example@qq.com")
        credential.insert(0, "ordinary-password")
        buttons["连接并读取最近邮件"].invoke()
        message = root.getvar(status.cget("textvariable"))
        assert "普通登录密码" in message and "16 位" in message
        assert FailedThread.attempts == 0

        credential.insert(0, "abcd efgh ijkl mnop")
        buttons["连接并读取最近邮件"].invoke()
        assert FailedThread.attempts == 1
        assert root.getvar(status.cget("textvariable")) == "读取任务未能启动，请重试。"
        assert str(buttons["连接并读取最近邮件"].cget("state")) == "normal"
        assert str(buttons["载入已保存凭据"].cget("state")) == "normal"
        assert str(buttons["取消"].cget("state")) == "disabled"
    finally:
        tab.master.destroy()


def test_old_account_cancelled_result_does_not_replace_new_account_status(monkeypatch, hidden_tk):
    import agent_service
    import agent_service.mailbox_panel as panel

    class DeferredThread:
        pending = []

        def __init__(self, *, target, args, kwargs, **_unused):
            self.target, self.args, self.kwargs = target, args, kwargs

        def start(self):
            self.pending.append(self)

        def run(self):
            self.target(*self.args, **self.kwargs)

    mailbox = ModuleType("agent_service.mailbox")
    mailbox.list_messages = lambda *_args, **_kwargs: {
        "ok": True, "uidvalidity": "7", "messages": [{"uid": "1", "subject": "old account",
                                               "from": "sender@example.com", "date": "today", "size": 100}]}
    monkeypatch.setitem(sys.modules, "agent_service.mailbox", mailbox)
    monkeypatch.setattr(agent_service, "mailbox", mailbox, raising=False)
    monkeypatch.setattr(panel.threading, "Thread", DeferredThread)
    root, tab, poll_root, buttons, account, credential, status = _mailbox_widgets(hidden_tk)
    try:
        account.insert(0, "old@qq.com")
        credential.insert(0, "abcd efgh ijkl mnop")
        buttons["连接并读取最近邮件"].invoke()
        account.delete(0, "end")
        account.insert(0, "new@qq.com")
        account.delete(0, "end")
        account.insert(0, "old@qq.com")
        useful = root.getvar(status.cget("textvariable"))
        assert "账号或服务已更改" in useful
        DeferredThread.pending.pop(0).run()
        poll_root.callbacks.pop(0)()
        assert root.getvar(status.cget("textvariable")) == useful
        assert str(buttons["连接并读取最近邮件"].cget("state")) == "normal"
    finally:
        tab.master.destroy()


@pytest.mark.parametrize("code,expected", [
    ("mail_auth_failed", "邮箱拒绝了凭据"),
    ("mail_timeout", "超时"),
    ("mail_tls_failed", "无法建立安全连接"),
    ("mail_operation_failed", "检查网络"),
])
def test_fake_mail_service_errors_are_clear_and_never_show_raw_reply(monkeypatch, hidden_tk, code, expected):
    import agent_service
    import agent_service.mailbox_panel as panel

    class SafeFailure(Exception):
        safe_code = code

    mailbox = ModuleType("agent_service.mailbox")

    def fail(*_args, **_kwargs):
        raise SafeFailure("server said password=private-app-code")

    mailbox.list_messages = fail
    monkeypatch.setitem(sys.modules, "agent_service.mailbox", mailbox)
    monkeypatch.setattr(agent_service, "mailbox", mailbox, raising=False)

    class InlineThread:
        def __init__(self, *, target, args, kwargs, **_unused):
            self.target, self.args, self.kwargs = target, args, kwargs

        def start(self):
            self.target(*self.args, **self.kwargs)

    monkeypatch.setattr(panel.threading, "Thread", InlineThread)
    root, tab, poll_root, buttons, account, credential, status = _mailbox_widgets(hidden_tk)
    try:
        account.insert(0, "example@qq.com")
        credential.insert(0, "abcd efgh ijkl mnop")
        buttons["连接并读取最近邮件"].invoke()
        poll_root.callbacks.pop(0)()
        message = root.getvar(status.cget("textvariable"))
        assert expected in message
        assert "password=" not in message and "mail_" not in message
    finally:
        tab.master.destroy()


def test_selected_read_can_export_only_its_current_allowlisted_snapshot(monkeypatch, hidden_tk):
    import agent_service
    import agent_service.mailbox_panel as panel

    class DeferredThread:
        pending = []

        def __init__(self, *, target, args, kwargs, **_unused):
            self.target, self.args, self.kwargs = target, args, kwargs

        def start(self):
            self.pending.append(self)

        def run(self):
            self.target(*self.args, **self.kwargs)

    secret = "abcd efgh ijkl mnop"
    mail = ModuleType("agent_service.mailbox")
    mail.list_messages = lambda *_args, **_kwargs: {
        "ok": True, "uidvalidity": "7", "messages": [
            {"uid": "12", "subject": "First", "from": "a@example.test",
             "date": "today", "size": 100},
            {"uid": "13", "subject": "Second", "from": "b@example.test",
             "date": "today", "size": 100}]}

    def read_message(_provider, _account, _secret, uid, uidvalidity, **_kwargs):
        return {"ok": True, "uid": uid, "uidvalidity": uidvalidity,
                "subject": "First", "from": "a@example.test", "to": "reader@qq.com",
                "date": "today", "body": "Synthetic body", "attachments": [],
                "sha256": "a" * 64,
                "coverage": {"scope": "selected_full_message", "body_format": "text/plain",
                             "parts": 1, "attachments": "metadata_only",
                             "html_alternative_ignored": False},
                "secret": secret}

    mail.read_message = read_message
    monkeypatch.setitem(sys.modules, "agent_service.mailbox", mail)
    monkeypatch.setattr(agent_service, "mailbox", mail, raising=False)
    monkeypatch.setattr(panel.threading, "Thread", DeferredThread)
    exported = []
    root, tab, poll_root, buttons, account, credential, _status = _mailbox_widgets(
        hidden_tk, on_export=lambda snapshot: exported.append(snapshot) or True)
    tree = next(widget for widget in tab.winfo_children() if isinstance(widget, ttk.Treeview))
    export = buttons["将已读邮件保存为 Word（仅本机）"]
    try:
        account.insert(0, "reader@qq.com")
        credential.insert(0, secret)
        buttons["连接并读取最近邮件"].invoke()
        DeferredThread.pending.pop(0).run()
        poll_root.callbacks.pop(0)()
        tree.selection_set("0")
        root.update()
        buttons["读取选中邮件"].invoke()
        DeferredThread.pending.pop(0).run()
        poll_root.callbacks.pop(0)()
        assert str(export.cget("state")) == "normal"
        assert "First" in export.cget("text")
        tree.selection_set("1")
        root.update()
        assert str(export.cget("state")) == "disabled"
        tree.selection_set("0")
        root.update()
        buttons["读取选中邮件"].invoke()
        DeferredThread.pending.pop(0).run()
        poll_root.callbacks.pop(0)()
        export.invoke()
        assert len(exported) == 1
        assert set(exported[0]) == {"provider", "uid", "uidvalidity", "subject", "from",
                                    "to", "date", "body", "attachments", "sha256", "coverage"}
        assert exported[0]["uid"] == "12"
        assert "account" not in exported[0] and "secret" not in exported[0]
        assert secret not in repr(exported)
        account.delete(0, "end")
        account.insert(0, "other@qq.com")
        assert str(export.cget("state")) == "disabled"
    finally:
        tab.master.destroy()


@pytest.mark.parametrize("invalidate", ["selection", "cancel", "wrong_uid"])
def test_mail_export_drops_late_or_mismatched_read(monkeypatch, hidden_tk, invalidate):
    import agent_service
    import agent_service.mailbox_panel as panel

    class DeferredThread:
        pending = []

        def __init__(self, *, target, args, kwargs, **_unused):
            self.target, self.args, self.kwargs = target, args, kwargs

        def start(self):
            self.pending.append(self)

        def run(self):
            self.target(*self.args, **self.kwargs)

    mail = ModuleType("agent_service.mailbox")
    mail.list_messages = lambda *_args, **_kwargs: {
        "ok": True, "uidvalidity": "7", "messages": [
            {"uid": "12", "subject": "First", "from": "a@example.test",
             "date": "today", "size": 100},
            {"uid": "13", "subject": "Second", "from": "b@example.test",
             "date": "today", "size": 100}]}
    mail.read_message = lambda *_args, **_kwargs: {
        "ok": True, "uid": "99" if invalidate == "wrong_uid" else "12",
        "uidvalidity": "7", "subject": "First",
        "from": "a@example.test", "to": "reader@qq.com", "date": "today",
        "body": "Old selected body", "attachments": [], "sha256": "a" * 64,
        "coverage": {"scope": "selected_full_message", "body_format": "text/plain",
                     "parts": 1, "attachments": "metadata_only",
                     "html_alternative_ignored": False}}
    monkeypatch.setitem(sys.modules, "agent_service.mailbox", mail)
    monkeypatch.setattr(agent_service, "mailbox", mail, raising=False)
    monkeypatch.setattr(panel.threading, "Thread", DeferredThread)
    exported = []
    root, tab, poll_root, buttons, account, credential, _status = _mailbox_widgets(
        hidden_tk, on_export=lambda snapshot: exported.append(snapshot) or True)
    tree = next(widget for widget in tab.winfo_children() if isinstance(widget, ttk.Treeview))
    export = buttons["将已读邮件保存为 Word（仅本机）"]
    try:
        account.insert(0, "reader@qq.com")
        credential.insert(0, "abcd efgh ijkl mnop")
        buttons["连接并读取最近邮件"].invoke()
        DeferredThread.pending.pop(0).run()
        poll_root.callbacks.pop(0)()
        tree.selection_set("0")
        root.update()
        buttons["读取选中邮件"].invoke()
        if invalidate == "selection":
            tree.selection_set("1")
            root.update()
        elif invalidate == "cancel":
            buttons["取消"].invoke()
        DeferredThread.pending.pop(0).run()
        poll_root.callbacks.pop(0)()
        assert str(export.cget("state")) == "disabled"
        assert exported == []
    finally:
        tab.master.destroy()


@pytest.mark.parametrize("code,expected", [
    ("mail_timeout", "超时"),
    ("mail_operation_failed", "检查网络"),
])
def test_selected_read_failure_keeps_backend_error(monkeypatch, hidden_tk, code, expected):
    import agent_service
    import agent_service.mailbox_panel as panel

    class InlineThread:
        def __init__(self, *, target, args, kwargs, **_unused):
            self.target, self.args, self.kwargs = target, args, kwargs

        def start(self):
            self.target(*self.args, **self.kwargs)

    mail = ModuleType("agent_service.mailbox")
    mail.list_messages = lambda *_args, **_kwargs: {
        "ok": True, "uidvalidity": "7", "messages": [
            {"uid": "12", "subject": "First", "from": "a@example.test",
             "date": "today", "size": 100}]}
    mail.read_message = lambda *_args, **_kwargs: {"ok": False, "code": code}
    monkeypatch.setitem(sys.modules, "agent_service.mailbox", mail)
    monkeypatch.setattr(agent_service, "mailbox", mail, raising=False)
    monkeypatch.setattr(panel.threading, "Thread", InlineThread)
    root, tab, poll_root, buttons, account, credential, status = _mailbox_widgets(hidden_tk)
    tree = next(widget for widget in tab.winfo_children() if isinstance(widget, ttk.Treeview))
    try:
        account.insert(0, "reader@qq.com")
        credential.insert(0, "abcd efgh ijkl mnop")
        buttons["连接并读取最近邮件"].invoke()
        poll_root.callbacks.pop(0)()
        tree.selection_set("0")
        root.update()
        buttons["读取选中邮件"].invoke()
        poll_root.callbacks.pop(0)()
        message = root.getvar(status.cget("textvariable"))
        assert expected in message
        assert "标识已变化" not in message
        assert str(buttons["将已读邮件保存为 Word（仅本机）"].cget("state")) == "disabled"
    finally:
        tab.master.destroy()
