"""Optional read-only local mailbox tab for the desktop launcher.

Importing this module does not import the mailbox backend, authenticate, or
contact a network service. All mailbox work starts after an explicit button
press and runs away from Tk's UI thread.
"""
import queue
import re
import threading


_SAFE_CODE = re.compile(r"[a-z][a-z0-9_]{0,39}\Z")
_APP_CODE = re.compile(r"[A-Za-z0-9]{16}\Z")
_MAIL_NUMBER = re.compile(r"[1-9][0-9]*\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_EXPORT_FIELDS = ("uid", "uidvalidity", "subject", "from", "to", "date", "body",
                  "attachments", "sha256", "coverage")
_ERROR_MESSAGES = {
    "mail_secret_invalid": "此入口需要 16 位英文字母或数字组成的邮箱授权码/应用专用密码（可含分组空格）；普通登录密码不能用于此连接。",
    "mail_auth_failed": "邮箱拒绝了凭据。请核对是否已开启 IMAP，并使用有效的邮箱授权码/应用专用密码；普通登录密码不能用于此连接。",
    "mail_account_invalid": "邮箱地址格式与所选服务不符，请核对完整地址。",
    "mail_provider_invalid": "请选择 QQ 或 Gmail 邮箱。",
    "mail_cancelled": "本次读取已取消。",
    "mail_timeout": "连接或读取超时，请检查网络后重试。",
    "mail_dns_busy": "邮箱地址解析仍在等待，请稍后重试。",
    "mail_tls_failed": "无法建立安全连接，请检查系统时间和网络后重试。",
    "mail_operation_failed": "连接或读取未完成，请检查网络后重试。",
    "mail_server_failed": "邮箱服务器未完成请求，请稍后重试。",
    "mail_select_failed": "无法以只读方式打开收件箱，请检查邮箱的 IMAP 设置。",
    "mail_fetch_failed": "邮箱服务器未返回所选邮件，请稍后重试。",
    "mail_message_oversize": "邮件超过本地只读预览的大小限制，未显示不完整内容。",
    "mail_header_oversize": "邮件标题超过预览限制，未显示不完整内容。",
    "mail_body_oversize": "邮件正文超过预览限制，未显示不完整内容。",
    "mail_truncated": "邮箱返回了不完整数据，请稍后重试。",
    "mail_charset_unsupported": "这封邮件使用了无法识别的文字编码，未显示不完整内容。",
    "mail_uidvalidity_changed": "收件箱已变化，请重新读取邮件列表后再选择。",
    "mail_message_changed": "邮件在读取期间发生变化，请重新读取邮件列表。",
    "credential_not_found": "本机没有保存该账号的专用凭据，请手动输入。",
}


def _safe_code(exc):
    code = getattr(exc, "safe_code", None)
    return code if isinstance(code, str) and _SAFE_CODE.fullmatch(code) else "mailbox_failed"


def _secret_shape(secret):
    """Mirror the backend's format gate without claiming credential validity."""
    return (type(secret) is str and 1 <= len(secret) <= 256 and secret.isascii()
            and _APP_CODE.fullmatch(secret.replace(" ", "")) is not None)


def _error_message(code):
    return _ERROR_MESSAGES.get(code, "邮箱操作未完成，请稍后重试。") if isinstance(code, str) else "邮箱操作未完成，请稍后重试。"


def _session_matches(session, provider, account, generation):
    return (type(session) is dict and session.get("generation") == generation
            and session.get("provider") == provider and session.get("account") == account)


def _account_values(records, provider):
    """Extract only account names from local profile metadata."""
    values = []
    if not isinstance(records, (list, tuple)):
        return ()
    suffix = "@qq.com" if provider == "qq" else "@gmail.com"
    for record in records:
        if type(record) is dict:
            item_provider = record.get("provider")
            account = record.get("account")
        elif isinstance(record, (tuple, list)) and len(record) >= 2:
            item_provider, account = record[0], record[1]
        else:
            continue
        if (item_provider == provider and isinstance(account, str)
                and account.lower().endswith(suffix) and account not in values):
            values.append(account)
    return tuple(values)


def _plain(value, limit=300):
    if not isinstance(value, str):
        value = ""
    return " ".join("".join(ch if ch >= " " else " " for ch in value).split())[:limit]


def _secret_from_store(value):
    if isinstance(value, str) and value:
        return value
    if type(value) is dict:
        secret = value.get("secret")
        return secret if isinstance(secret, str) and secret else None
    return None


def _public_result(operation, value):
    """Keep queue payloads limited to the backend's documented response fields."""
    if type(value) is not dict:
        return {"ok": False, "code": "mailbox_failed"}
    output = {"ok": value.get("ok") is True}
    code = value.get("code")
    if isinstance(code, str):
        output["code"] = code if _SAFE_CODE.fullmatch(code) else "mailbox_failed"
    common = {"uidvalidity", "messages"} if operation == "fetch" else {
        "uid", "uidvalidity", "subject", "from", "to", "date", "body",
        "attachments", "sha256", "coverage"}
    for key in common:
        item = value.get(key)
        if key == "messages" and isinstance(item, list):
            output[key] = [{field: row[field] for field in ("uid", "subject", "from", "date", "size")
                            if field in row and isinstance(row[field], (str, int, float))}
                           for row in item if type(row) is dict]
        elif key == "attachments" and isinstance(item, list):
            output[key] = [{field: row[field] for field in ("filename", "content_type", "size")
                            if field in row and isinstance(row[field], (str, int, float))}
                           for row in item if type(row) is dict]
        elif key == "coverage" and type(item) is dict:
            output[key] = {field: item[field] for field in
                           ("scope", "body_format", "parts", "attachments", "html_alternative_ignored")
                           if field in item and isinstance(item[field], (str, int, float, bool))}
        elif isinstance(item, (str, int, float)) or item is None:
            output[key] = item
    return output


def _export_candidate(provider, result):
    """Project one complete read into the mail-export allowlist, or refuse it."""
    if provider != "qq" or type(result) is not dict or result.get("ok") is not True:
        return None
    if any(field not in result for field in _EXPORT_FIELDS):
        return None
    snapshot = {"provider": provider, **{field: result[field] for field in _EXPORT_FIELDS}}
    if (any(type(snapshot[field]) is not str for field in
            ("uid", "uidvalidity", "subject", "from", "to", "date", "body", "sha256"))
            or not _MAIL_NUMBER.fullmatch(snapshot["uid"])
            or not _MAIL_NUMBER.fullmatch(snapshot["uidvalidity"])
            or not _DIGEST.fullmatch(snapshot["sha256"])
            or len(snapshot["body"]) > 200_000):
        return None
    coverage = snapshot["coverage"]
    if (type(coverage) is not dict
            or set(coverage) != {"scope", "body_format", "parts", "attachments",
                                 "html_alternative_ignored"}
            or coverage["scope"] != "selected_full_message"
            or coverage["body_format"] not in {"text/plain", "html_to_plain", "none"}
            or type(coverage["parts"]) is not int or not 1 <= coverage["parts"] <= 32
            or coverage["attachments"] != "metadata_only"
            or type(coverage["html_alternative_ignored"]) is not bool
            or (coverage["body_format"] == "none" and snapshot["body"])):
        return None
    attachments = snapshot["attachments"]
    if type(attachments) is not list or len(attachments) > 32:
        return None
    for item in attachments:
        if (type(item) is not dict or set(item) != {"filename", "content_type", "size"}
                or type(item["filename"]) is not str or type(item["content_type"]) is not str
                or type(item["size"]) is not int or item["size"] < 0):
            return None
    return snapshot


def attach_mailbox_tab(notebook, root, on_export=None):
    """Attach a QQ/Gmail read-only panel; return its frame or a safe failure."""
    try:
        import tkinter as tk
        from tkinter import ttk
    except Exception:
        return None

    try:
        tab = ttk.Frame(notebook, padding=12)
        notebook.add(tab, text="邮箱（只读）")
        ttk.Label(tab, text="仅在本机预览最近邮件，不调用模型、不发送或修改邮件。",
                  wraplength=620).pack(anchor="w", pady=(0, 8))

        provider = tk.StringVar(value="qq")
        account = tk.StringVar(value="")
        credential = tk.StringVar(value="")
        remember = tk.BooleanVar(value=False)
        status = tk.StringVar(value="未连接。请输入邮箱授权码/应用专用密码；普通登录密码不能用于此连接。")

        provider_row = ttk.Frame(tab)
        provider_row.pack(fill="x", pady=2)
        ttk.Label(provider_row, text="服务：").pack(side="left")
        provider_picker = ttk.Combobox(provider_row, textvariable=provider,
                                       values=("qq", "gmail"), width=10, state="readonly")
        provider_picker.pack(side="left")
        ttk.Label(provider_row, text="账号：").pack(side="left", padx=(8, 0))
        account_picker = ttk.Combobox(provider_row, textvariable=account, values=(),
                                      width=34, state="normal")
        account_picker.pack(side="left", padx=4)
        profiles_button = ttk.Button(provider_row, text="列出本机账号")
        profiles_button.pack(side="left")

        credential_row = ttk.Frame(tab)
        credential_row.pack(fill="x", pady=2)
        ttk.Label(credential_row, text="授权码/应用专用密码：").pack(side="left")
        credential_entry = ttk.Entry(credential_row, textvariable=credential, show="•", width=34)
        credential_entry.pack(side="left", padx=4)
        ttk.Checkbutton(credential_row, text="在本机安全保存", variable=remember).pack(side="left")
        ttk.Label(tab, text="普通登录密码不能用于此连接。QQ：在网页版邮箱设置中开启 IMAP/SMTP 并生成授权码；Gmail：在 Google 账号安全设置中创建应用专用密码（账号支持时）。",
                  wraplength=620).pack(anchor="w", pady=3)

        action_row = ttk.Frame(tab)
        action_row.pack(fill="x", pady=6)
        fetch_button = ttk.Button(action_row, text="连接并读取最近邮件")
        fetch_button.pack(side="left")
        cancel_button = ttk.Button(action_row, text="取消", state="disabled")
        cancel_button.pack(side="left", padx=5)
        saved_button = ttk.Button(action_row, text="载入已保存凭据")
        saved_button.pack(side="left")
        status_label = ttk.Label(tab, textvariable=status, wraplength=620)
        status_label.pack(fill="x", anchor="w", pady=(0, 5))
        ttk.Label(tab, text="仅输入该邮箱生成的专用凭据；本入口要求 16 位英文字母或数字，可保留分组空格。格式符合也不代表授权有效。",
                  wraplength=620).pack(anchor="w")
        support_row = ttk.Frame(tab)
        support_row.pack(fill="x", pady=(0, 6))
        qq_help = ttk.Label(support_row, text="QQ 邮箱帮助", foreground="#1769aa", cursor="hand2")
        qq_help.pack(side="left")
        ttk.Label(support_row, text="  ·  ").pack(side="left")
        gmail_help = ttk.Label(support_row, text="Google 应用专用密码说明", foreground="#1769aa", cursor="hand2")
        gmail_help.pack(side="left")

        list_tree = ttk.Treeview(tab, columns=("from", "date", "subject"),
                                 show="headings", height=7)
        for key, label, width in (("from", "发件人", 170), ("date", "日期", 130),
                                  ("subject", "主题", 300)):
            list_tree.heading(key, text=label)
            list_tree.column(key, width=width, stretch=(key == "subject"))
        list_tree.pack(fill="both", expand=True, pady=4)
        read_button = ttk.Button(tab, text="读取选中邮件", state="disabled")
        read_button.pack(anchor="w", pady=3)
        export_button = ttk.Button(tab, text="将已读邮件保存为 Word（仅本机）", state="disabled")
        export_button.pack(anchor="w", pady=3)
        ttk.Label(tab, text="仅保存已读正文、邮件头和附件名称/大小；不保存附件内容，不调用模型或发送邮件。",
                  wraplength=620).pack(anchor="w")
        preview = tk.Text(tab, height=10, wrap="word")
        preview.pack(fill="both", expand=True)
        preview.configure(state="disabled")

        messages = queue.Queue()
        task = {"generation": 0, "event": None, "busy": False}
        current_session = {"value": None}
        rows = {}
        exportable = {"value": None}

        def invalidate_export(clear_preview=False):
            exportable["value"] = None
            export_button.configure(state="disabled", text="将已读邮件保存为 Word（仅本机）")
            if clear_preview:
                set_preview("")

        def set_preview(text):
            preview.configure(state="normal")
            preview.delete("1.0", "end")
            preview.insert("1.0", text)
            preview.configure(state="disabled")

        def current_key():
            return provider.get(), account.get().strip()

        # Track the identity represented by the input fields independently of
        # whether a mailbox session has been created. A credential typed before
        # the first connection belongs to that identity just as much as a
        # credential held by an established session.
        observed_identity = [current_key()]

        def display_saved_accounts(_event=None):
            try:
                from agent_service.mail_credentials import list_accounts
                entries = list_accounts()
                account_picker.configure(values=_account_values(entries, provider.get()))
            except Exception:
                account_picker.configure(values=())

        def invalidate_session(*_event):
            identity = current_key()
            if identity == observed_identity[0]:
                return
            observed_identity[0] = identity
            invalidate_export(clear_preview=True)
            task["requested_read"] = None
            # Never carry a manually entered or loaded credential across an
            # account/provider change. Setting the StringVar does not trigger
            # these identity traces.
            credential.set("")
            old = current_session["value"]
            if old is not None and (old["provider"], old["account"]) != current_key():
                task["generation"] += 1
                if task["event"] is not None:
                    task["account_change_cancel"] = task.get("active_generation")
                    task["event"].set()
                current_session["value"] = None
                loaded = task.pop("loaded_secret", None)
                if loaded is not None:
                    task.pop("then_fetch", None)
                rows.clear()
                for row in list_tree.get_children():
                    list_tree.delete(row)
                read_button.configure(state="disabled")
                status.set("账号或服务已更改；请重新连接。")

        def selection_changed(_event=None):
            selected = list_tree.selection()
            current_export = exportable["value"]
            requested = task.get("requested_read")
            if (type(current_export) is dict and selected == (current_export.get("iid"),)):
                return
            if (task.get("operation") == "read" and type(requested) is tuple
                    and selected == (requested[5],)):
                return
            if exportable["value"] is None and task.get("operation") != "read":
                return
            invalidate_export(clear_preview=True)
            task["requested_read"] = None
            if task.get("operation") == "read" and task.get("event") is not None:
                task["event"].set()
                status.set("选中邮件已变化，正在取消旧读取。")
            else:
                status.set("选中邮件已变化；请重新读取后再导出。")

        provider.trace_add("write", invalidate_session)
        account.trace_add("write", invalidate_session)
        account_picker.bind("<<ComboboxSelected>>", invalidate_session)
        list_tree.bind("<<TreeviewSelect>>", selection_changed)
        profiles_button.configure(command=display_saved_accounts)

        def worker(operation, generation, provider_value, account_value, secret_value,
                   stop_event, **kwargs):
            try:
                from agent_service import mailbox
                if operation == "fetch":
                    result = mailbox.list_messages(provider_value, account_value, secret_value,
                                                   limit=20, should_stop=stop_event.is_set)
                    result = _public_result(operation, result)
                    save_error = None
                    if (result.get("ok") is True and kwargs.get("remember")
                            and not stop_event.is_set()):
                        try:
                            from agent_service.mail_credentials import save_credential
                            save_credential(provider_value, account_value, secret_value)
                        except Exception as exc:
                            save_error = _safe_code(exc)
                    messages.put((operation, generation, provider_value, account_value,
                                  result, save_error))
                elif operation == "read":
                    result = mailbox.read_message(provider_value, account_value, secret_value,
                                                  kwargs["uid"], kwargs["uidvalidity"],
                                                  should_stop=stop_event.is_set)
                    result = _public_result(operation, result)
                    messages.put((operation, generation, provider_value, account_value, result, None))
                elif operation == "load":
                    try:
                        from agent_service.mail_credentials import load_credential
                        stored = load_credential(provider_value, account_value)
                        secret = _secret_from_store(stored)
                        if stop_event.is_set():
                            messages.put((operation, generation, provider_value, account_value,
                                          {"ok": False, "code": "cancelled"}, None))
                            return
                        if not secret:
                            messages.put((operation, generation, provider_value, account_value,
                                          {"ok": False, "code": "credential_not_found"}, None))
                        else:
                            # The secret is kept only in the in-memory session object,
                            # never in the queue payload.
                            task["loaded_secret"] = (generation, provider_value, account_value, secret)
                            messages.put((operation, generation, provider_value, account_value,
                                          {"ok": True}, None))
                    except Exception as exc:
                        messages.put((operation, generation, provider_value, account_value,
                                      {"ok": False, "code": _safe_code(exc)}, None))
            except Exception as exc:
                messages.put((operation, generation, provider_value, account_value,
                              {"ok": False, "code": _safe_code(exc)}, None))

        def begin(operation, session, **kwargs):
            if task["busy"]:
                return False
            event = threading.Event()
            then_fetch = bool(kwargs.pop("then_fetch", False))
            task.update(event=event, busy=True, active_generation=session["generation"],
                        then_fetch=then_fetch, operation=operation)
            cancel_button.configure(state="normal")
            fetch_button.configure(state="disabled")
            saved_button.configure(state="disabled")
            read_button.configure(state="disabled")
            if operation == "fetch":
                status.set("正在安全连接并读取邮件标题……")
            elif operation == "read":
                status.set("正在读取所选邮件内容……")
            else:
                status.set("正在读取本机凭据库……")
            try:
                thread = threading.Thread(
                    target=worker,
                    args=(operation, session["generation"], session["provider"],
                          session["account"], session.get("secret"), event),
                    kwargs=kwargs, daemon=True, name="jarvis-mailbox-" + operation)
                thread.start()
            except Exception:
                event.set()
                task["busy"] = False
                task["event"] = None
                task["active_generation"] = None
                task["operation"] = None
                task["requested_read"] = None
                task.pop("then_fetch", None)
                cancel_button.configure(state="disabled")
                fetch_button.configure(state="normal")
                saved_button.configure(state="normal")
                read_button.configure(state="normal" if rows else "disabled")
                status.set("读取任务未能启动，请重试。")
                return False
            return True

        def start_fetch():
            if task["busy"]:
                return
            invalidate_export(clear_preview=True)
            task["requested_read"] = None
            provider_value, account_value = current_key()
            expected_suffix = "@qq.com" if provider_value == "qq" else "@gmail.com" if provider_value == "gmail" else None
            if (expected_suffix is None or not account_value.lower().endswith(expected_suffix)):
                status.set("请选择 QQ 或 Gmail，并填写对应的完整邮箱地址。")
                return
            secret = credential.get()
            previous = current_session["value"]
            if (not secret and _session_matches(previous, provider_value, account_value,
                                                 previous.get("generation") if previous else None)):
                secret = previous.get("secret")
            if secret and not _secret_shape(secret):
                credential.set("")
                status.set(_error_message("mail_secret_invalid"))
                return
            session = {"generation": task["generation"] + 1, "provider": provider_value,
                       "account": account_value, "secret": secret, "remember": bool(remember.get())}
            task["generation"] = session["generation"]
            current_session["value"] = session
            for row in list_tree.get_children():
                list_tree.delete(row)
            rows.clear()
            read_button.configure(state="disabled")
            credential.set("")
            if secret:
                begin("fetch", session, remember=session["remember"])
            else:
                # Only the explicit fetch action may look up a locally remembered secret.
                begin("load", session, then_fetch=True, remember=session["remember"])

        def start_read():
            if task["busy"]:
                return
            invalidate_export(clear_preview=True)
            task["requested_read"] = None
            session = current_session["value"]
            if not session or (session["provider"], session["account"]) != current_key():
                status.set("请先连接当前账号并读取邮件列表。")
                return
            selected = list_tree.selection()
            if not selected or selected[0] not in rows:
                status.set("请先选择一封邮件。")
                return
            item = rows[selected[0]]
            if item.get("uid") is None or item.get("uidvalidity") is None:
                status.set("邮件标识不完整，请重新读取邮件列表。")
                return
            task["generation"] += 1
            session["generation"] = task["generation"]
            task["requested_read"] = (session["provider"], session["account"],
                                      session["generation"], item["uidvalidity"],
                                      item["uid"], selected[0])
            begin("read", session, uid=item["uid"], uidvalidity=item["uidvalidity"])

        def cancel():
            event = task.get("event")
            if event is not None:
                invalidate_export(clear_preview=True)
                task["requested_read"] = None
                event.set()
                status.set("正在取消读取；已取得的邮件不会被修改。")

        def load_saved():
            if task["busy"]:
                return
            invalidate_export(clear_preview=True)
            task["requested_read"] = None
            provider_value, account_value = current_key()
            if not account_value:
                status.set("先填写账号，再载入本机凭据。")
                return
            session = {"generation": task["generation"] + 1, "provider": provider_value,
                       "account": account_value, "secret": None}
            task["generation"] = session["generation"]
            current_session["value"] = session
            begin("load", session, then_fetch=False, remember=False)

        def show_help(url):
            import webbrowser
            webbrowser.open(url)

        qq_help.bind("<Button-1>", lambda _event: show_help("https://service.mail.qq.com/"))
        gmail_help.bind("<Button-1>", lambda _event: show_help("https://support.google.com/accounts/answer/185833"))
        fetch_button.configure(command=start_fetch)
        cancel_button.configure(command=cancel)
        saved_button.configure(command=load_saved)
        read_button.configure(command=start_read)

        def start_export():
            selected_snapshot = exportable["value"]
            if (not callable(on_export) or type(selected_snapshot) is not dict
                    or task["busy"]):
                return
            session = current_session["value"]
            selected = list_tree.selection()
            if (not session or selected_snapshot.get("identity") !=
                    (session["provider"], session["account"], session["generation"])
                    or selected != (selected_snapshot.get("iid"),)):
                invalidate_export(clear_preview=True)
                status.set("选中邮件已变化；请重新读取后再导出。")
                return
            try:
                accepted = on_export(selected_snapshot["snapshot"])
            except Exception:
                accepted = False
            if accepted is True:
                invalidate_export()
                status.set("已提交本机 Word 导出；仅保存正文与元数据，不保存附件内容。")
            else:
                status.set("本机导出暂未启动；请先完成当前桌面任务后重试。")

        export_button.configure(command=start_export)

        def poll():
            try:
                while True:
                    operation, generation, provider_value, account_value, result, save_error = messages.get_nowait()
                    session = current_session["value"]
                    event = task.get("event")
                    if task.get("active_generation") == generation and event is not None and event.is_set():
                        account_change_cancel = task.pop("account_change_cancel", None) == generation
                        loaded = task.pop("loaded_secret", None)
                        if loaded is not None and loaded[0] == generation:
                            task.pop("then_fetch", None)
                        task["busy"] = False
                        task["event"] = None
                        task["active_generation"] = None
                        task["operation"] = None
                        task["requested_read"] = None
                        fetch_button.configure(state="normal")
                        saved_button.configure(state="normal")
                        cancel_button.configure(state="disabled")
                        read_button.configure(state="normal" if rows else "disabled")
                        if not account_change_cancel and (provider_value, account_value) == current_key():
                            status.set("读取已取消；未显示迟到的邮件内容。")
                        continue
                    if (not _session_matches(session, provider_value, account_value, generation)
                            or (provider_value, account_value) != current_key()):
                        if task.get("active_generation") == generation:
                            task["busy"] = False
                            task["event"] = None
                            task["active_generation"] = None
                            task["operation"] = None
                            task["requested_read"] = None
                            fetch_button.configure(state="normal")
                            saved_button.configure(state="normal")
                            cancel_button.configure(state="disabled")
                            read_button.configure(state="normal" if rows else "disabled")
                        continue
                    if operation == "load":
                        if result.get("ok") is True:
                            loaded = task.pop("loaded_secret", None)
                            if (loaded and loaded[:3] == (generation, provider_value, account_value)):
                                session["secret"] = loaded[3]
                                if not _secret_shape(session["secret"]):
                                    session["secret"] = None
                                    task.pop("then_fetch", None)
                                    status.set(_error_message("mail_secret_invalid"))
                                else:
                                    if task.pop("then_fetch", False):
                                        task["generation"] += 1
                                        session["generation"] = task["generation"]
                                        task["busy"] = False
                                        task["event"] = None
                                        begin("fetch", session, remember=session["remember"])
                                        continue
                                    status.set("已载入本机凭据；点击“连接并读取最近邮件”开始连接。")
                            else:
                                status.set("本机凭据载入失败。")
                        else:
                            status.set("没有可用的本机凭据；请手动输入邮箱授权码或应用专用密码。")
                    elif operation == "fetch":
                        invalidate_export(clear_preview=True)
                        if result.get("ok") is True:
                            current_session["value"] = session
                            items = result.get("messages", [])
                            if not isinstance(items, list):
                                items = []
                            rows.clear()
                            for index, item in enumerate(items):
                                if type(item) is not dict or item.get("uid") is None:
                                    continue
                                iid = str(index)
                                rows[iid] = {**item, "uidvalidity": result.get("uidvalidity")}
                                list_tree.insert("", "end", iid=iid,
                                                 values=(_plain(item.get("from"), 100),
                                                         _plain(item.get("date"), 40),
                                                         _plain(item.get("subject"), 250)))
                            read_button.configure(state="normal" if rows else "disabled")
                            status.set(f"已读取 {len(rows)} 封邮件标题。选择邮件后可在本机预览正文。"
                                       + (" 本机安全保存凭据失败。" if save_error else ""))
                        else:
                            status.set("连接失败：" + _error_message(result.get("code")))
                    elif operation == "read":
                        requested = task.get("requested_read")
                        selected = list_tree.selection()
                        identity_changed = (type(requested) is not tuple or len(requested) != 6
                                or requested[:3] != (provider_value, account_value, generation)
                                or selected != (requested[5],)
                                or rows.get(requested[5], {}).get("uid") != requested[4]
                                or rows.get(requested[5], {}).get("uidvalidity") != requested[3])
                        response_changed = (not identity_changed and result.get("ok") is True
                                            and (result.get("uid") != requested[4]
                                                 or result.get("uidvalidity") != requested[3]))
                        if identity_changed or response_changed:
                            invalidate_export(clear_preview=True)
                            status.set("所选邮件或收件箱标识已变化；请重新读取。")
                            result = {"ok": False, "code": "mail_message_changed"}
                        if result.get("ok") is True:
                            attachments = result.get("attachments", [])
                            names = [a.get("filename", "附件") for a in attachments
                                     if type(a) is dict and isinstance(a.get("filename"), str)] if isinstance(attachments, list) else []
                            body = result.get("body", "")
                            coverage = result.get("coverage", {})
                            coverage_text = (_plain(coverage.get("body_format"), 80)
                                             if type(coverage) is dict else "unknown")
                            details = [f"发件人：{_plain(result.get('from'), 240)}",
                                       f"收件人：{_plain(result.get('to'), 240)}",
                                       f"日期：{_plain(result.get('date'), 80)}",
                                       f"主题：{_plain(result.get('subject'), 300)}",
                                       f"附件：{'、'.join(_plain(name, 120) for name in names) or '无'}",
                                       f"正文格式：{coverage_text or '未标注'}；附件仅显示元数据",
                                       "", body if isinstance(body, str) else ""]
                            set_preview("\n".join(details))
                            snapshot = _export_candidate(provider_value, result)
                            if callable(on_export) and snapshot is not None:
                                exportable["value"] = {"identity": (provider_value, account_value,
                                                                      generation),
                                                       "iid": requested[5], "snapshot": snapshot}
                                export_button.configure(
                                    state="normal",
                                    text="保存为 Word（仅本机）：" + _plain(
                                        snapshot["subject"] or "无主题", 36))
                                status.set("邮件仅在本机预览。可明确保存为 Word；附件仅记名称和大小。")
                            else:
                                invalidate_export()
                                status.set("邮件仅在本机预览；未发送、未修改，也未交给模型。")
                        else:
                            status.set("读取失败：" + _error_message(result.get("code")))
                    task["busy"] = False
                    task["event"] = None
                    task["active_generation"] = None
                    task["operation"] = None
                    task["requested_read"] = None
                    fetch_button.configure(state="normal")
                    saved_button.configure(state="normal")
                    cancel_button.configure(state="disabled")
                    if operation != "fetch":
                        read_button.configure(state="normal" if rows else "disabled")
            except queue.Empty:
                pass
            root.after(100, poll)

        def start_polling():
            root.after(100, poll)

        # No worker is started until one of the explicit action buttons is pressed.
        start_polling()
        return tab
    except Exception:
        return None
