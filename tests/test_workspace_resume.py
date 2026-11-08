from copy import deepcopy

import pytest

from agent_service import workspace_resume, workspace_state


class _Thread:
    all = []

    def __init__(self, target, name, **_kwargs):
        self.target, self.name = target, name
        self.done = False

    def start(self):
        self.all.append(self)

    def run(self):
        self.target(); self.done = True


def _payload(tmp_path, draft="resume me"):
    path = str(tmp_path / "output.docx")
    record = {"path": path, "sha256": "a" * 64}
    return {"draft": draft, "records": [record], "source": dict(record)}


def _harness(tmp_path, monkeypatch, *, payload=None, binding=None, unavailable=False,
             on_failure=None):
    _Thread.all = []
    monkeypatch.setattr(workspace_resume.threading, "Thread", _Thread)
    monkeypatch.setattr(workspace_state, "verify", lambda value, _base: {
        "payload": deepcopy(value), "refs": [], "source_ref": None})
    saved_payload = _payload(tmp_path) if payload is None else deepcopy(payload)
    state = {"payload": deepcopy(saved_payload), "revision": 1,
             "binding": binding or {"source": None, "batch": None},
             "unavailable": unavailable}
    busy = {"value": False, "events": []}
    applied = []
    statuses = []
    def apply(value):
        applied.append(deepcopy(value)); state["payload"] = deepcopy(value["payload"])
        return True
    controller = workspace_resume.WorkspaceResume(
        context=lambda: deepcopy(state), busy=lambda: busy["value"],
        on_busy=lambda value: (busy.update(value=value), busy["events"].append(value)),
        apply=apply, status=statuses.append, store=tmp_path / "workspace",
        task_base=tmp_path, on_failure=on_failure)
    return controller, state, busy, applied, statuses


def _save(controller, tmp_path):
    assert controller.save()
    return workspace_state.load(tmp_path / "workspace")


class _GenericBackend:
    WorkspaceError = workspace_state.WorkspaceError

    def __init__(self):
        self.saved = None
        self.calls = []

    def load(self, _store):
        self.calls.append("load")
        return deepcopy(self.saved)

    def save(self, _store, payload, expected_id):
        self.calls.append("save")
        if (self.saved or {}).get("id") != expected_id:
            raise workspace_state.WorkspaceError("workspace_conflict")
        self.saved = {"id": "d" * 32, "payload": deepcopy(payload)}
        return deepcopy(self.saved)

    def verify(self, payload, _base):
        self.calls.append("verify")
        return {"payload": deepcopy(payload), "refs": ["verified"],
                "source_ref": {"scope": payload["scope"]}}


def test_save_is_explicit_and_roundtrips_exact_draft_source_and_records(tmp_path, monkeypatch):
    controller, state, _, _, statuses = _harness(tmp_path, monkeypatch,
                                                  payload=_payload(tmp_path, "  x\r\n"))
    assert workspace_state.load(tmp_path / "workspace") is None
    saved = _save(controller, tmp_path)
    assert saved["payload"] == state["payload"]
    assert saved["payload"]["draft"] == "  x\r\n"
    assert "不会自动执行" in statuses[-1]


def test_restore_rejects_nonempty_different_fields_but_allows_empty_or_identical(tmp_path, monkeypatch):
    controller, state, _, applied, _ = _harness(tmp_path, monkeypatch)
    _save(controller, tmp_path)
    state["payload"]["draft"] = "different"
    assert not controller.restore()
    assert not applied
    state["payload"]["draft"] = "resume me"
    state["payload"]["records"] = []
    state["payload"]["source"] = None
    state["revision"] += 1
    assert controller.restore()
    worker = _Thread.all[-1]
    assert worker.name == "workspace-restore"
    worker.run()
    assert controller.poll()
    assert applied[0]["payload"] == _payload(tmp_path)


def test_restore_verification_failure_does_not_apply_or_replace_records(tmp_path, monkeypatch):
    controller, state, _, applied, statuses = _harness(tmp_path, monkeypatch)
    _save(controller, tmp_path)
    old = deepcopy(state["payload"])
    monkeypatch.setattr(workspace_state, "verify", lambda *_: (_ for _ in ()).throw(
        workspace_state.WorkspaceError("workspace_reference_invalid")))
    assert controller.restore()
    _Thread.all[-1].run(); controller.poll()
    assert not applied and state["payload"] == old
    assert "无法核验" in statuses[-1]


def test_failure_callback_receives_saved_record_error_and_exact_context(tmp_path, monkeypatch):
    callback_calls = []
    def explain(saved, error, context):
        callback_calls.append((deepcopy(saved), error, deepcopy(context)))
        return "成果文件校验失败，请重新接入。"
    controller, state, busy, applied, statuses = _harness(
        tmp_path, monkeypatch, on_failure=explain)
    saved = _save(controller, tmp_path)
    captured = deepcopy(state)
    failure = workspace_state.WorkspaceError("workspace_reference_invalid")
    monkeypatch.setattr(workspace_state, "verify", lambda *_: (_ for _ in ()).throw(failure))
    assert controller.restore()
    _Thread.all[-1].run()
    controller.poll()
    assert callback_calls == [(saved, failure, captured)]
    assert busy["events"] == [True, False]
    assert not applied
    assert statuses[-1] == "成果文件校验失败，请重新接入。"


def test_failure_callback_not_called_after_context_changes_and_default_message_remains(
        tmp_path, monkeypatch):
    calls = []
    controller, state, _busy, applied, statuses = _harness(
        tmp_path, monkeypatch, on_failure=lambda *args: calls.append(args) or "custom")
    _save(controller, tmp_path)
    monkeypatch.setattr(workspace_state, "verify", lambda *_: (_ for _ in ()).throw(
        workspace_state.WorkspaceError("workspace_reference_invalid")))
    assert controller.restore()
    state["revision"] += 1
    _Thread.all[-1].run()
    controller.poll()
    assert not calls and not applied
    assert "内容已变化" in statuses[-1]

    plain, _state, _busy, _applied, plain_status = _harness(tmp_path / "plain", monkeypatch)
    _save(plain, tmp_path / "plain")
    monkeypatch.setattr(workspace_state, "verify", lambda *_: (_ for _ in ()).throw(
        workspace_state.WorkspaceError("workspace_reference_invalid")))
    assert plain.restore()
    _Thread.all[-1].run()
    plain.poll()
    assert plain_status[-1] == "已保存成果无法核验，当前内容未更改。"


@pytest.mark.parametrize("callback", [lambda *_: None,
                                       lambda *_: "",
                                       lambda *_: (_ for _ in ()).throw(RuntimeError("secret path"))])
def test_failure_callback_invalid_result_or_exception_falls_back_to_generic(
        tmp_path, monkeypatch, callback):
    controller, _state, _busy, _applied, statuses = _harness(
        tmp_path, monkeypatch, on_failure=callback)
    _save(controller, tmp_path)
    monkeypatch.setattr(workspace_state, "verify", lambda *_: (_ for _ in ()).throw(
        workspace_state.WorkspaceError("workspace_reference_invalid")))
    assert controller.restore()
    _Thread.all[-1].run()
    controller.poll()
    assert statuses[-1] == "已保存成果无法核验，当前内容未更改。"
    assert "secret path" not in statuses[-1]


def test_context_change_including_edit_then_revert_revision_discards_late_result(tmp_path, monkeypatch):
    controller, state, _, applied, statuses = _harness(tmp_path, monkeypatch)
    _save(controller, tmp_path)
    assert controller.restore()
    state["revision"] += 1  # text may return to same bytes; revision still differs
    _Thread.all[-1].run(); controller.poll()
    assert not applied
    assert "已变化" in statuses[-1]


def test_timeout_cancel_and_close_invalidate_callbacks_and_prevent_parallel_worker(tmp_path, monkeypatch):
    now = {"t": 1.0}
    monkeypatch.setattr(workspace_resume.threading, "Thread", _Thread)
    monkeypatch.setattr(workspace_state, "verify", lambda *_: (_ for _ in ()).throw(
        workspace_state.WorkspaceError("workspace_reference_invalid")))
    payload = _payload(tmp_path)
    state = {"payload": payload, "revision": 1, "binding": {}, "unavailable": False}
    applied, statuses, busy = [], [], []
    failures = []
    ctl = workspace_resume.WorkspaceResume(context=lambda: deepcopy(state), busy=lambda: False,
        on_busy=busy.append, apply=lambda v: applied.append(v) or True,
        status=statuses.append, store=tmp_path / "workspace", task_base=tmp_path,
        clock=lambda: now["t"], on_failure=lambda *args: failures.append(args) or "failure")
    assert ctl.save() and ctl.restore()
    worker = _Thread.all[-1]
    now["t"] = 32.0
    assert ctl.poll()
    assert not ctl.restore()  # timed-out worker is still running
    worker.run(); assert ctl.poll(); assert not applied
    assert ctl.restore(); second = _Thread.all[-1]
    ctl.close(); second.run(); assert ctl.poll(); assert not applied
    assert busy == [True, False, True, False]
    assert "取消" in statuses[-1]
    assert not failures


def test_save_conflict_and_unavailable_context_are_path_free_and_nonmutating(tmp_path, monkeypatch):
    controller, state, _, _, statuses = _harness(tmp_path, monkeypatch)
    first = _save(controller, tmp_path)
    workspace_state.save(tmp_path / "workspace", _payload(tmp_path, "other window"), first["id"])
    state["payload"]["draft"] = "my new draft"
    assert not controller.save()
    assert statuses[-1] == "已有更新的工作区，未覆盖；请重新打开后恢复。"
    assert str(tmp_path) not in statuses[-1]
    state["unavailable"] = True
    assert not controller.restore()
    assert not controller.save()


@pytest.mark.parametrize("field", ["draft", "records", "source"])
def test_restore_preserves_any_nonempty_different_record_or_source(tmp_path, monkeypatch, field):
    controller, state, _, applied, _ = _harness(tmp_path, monkeypatch)
    _save(controller, tmp_path)
    if field == "draft": state["payload"][field] = "other"
    elif field == "records": state["payload"][field] = [{"path": "different", "sha256": "b" * 64}]
    else: state["payload"][field] = {"path": "different", "sha256": "b" * 64}
    assert not controller.restore()
    assert not applied


def test_generic_backend_supports_draft_records_scope_and_named_thread(tmp_path, monkeypatch):
    backend = _GenericBackend()
    payload = {"draft": "office task", "records": [{"id": "x"}],
               "scope": {"sheet": "Data", "range": "A1:B8"}}
    _Thread.all = []
    monkeypatch.setattr(workspace_resume.threading, "Thread", _Thread)
    state = {"payload": deepcopy(payload), "revision": 1, "binding": {},
             "unavailable": False}
    applied, busy = [], []
    ctl = workspace_resume.WorkspaceResume(
        context=lambda: deepcopy(state), busy=lambda: False, on_busy=busy.append,
        apply=lambda value: applied.append(deepcopy(value)) or True, status=lambda _text: None,
        store=tmp_path / "generic", task_base=tmp_path, backend=backend,
        thread_name="office-workspace-restore")
    assert ctl.save()
    assert backend.saved["payload"] == payload
    assert ctl.restore()
    worker = _Thread.all[-1]
    assert worker.name == "office-workspace-restore"
    worker.run()
    assert ctl.poll()
    assert applied[0]["payload"] == payload
    assert applied[0]["source_ref"] == {"scope": payload["scope"]}
    assert backend.calls.count("verify") == 1


@pytest.mark.parametrize("payload_mutation", [
    lambda p: p.update(scope={"sheet": "Other"}),
    lambda p: p.pop("scope"),
    lambda p: p.update(extra="unexpected"),
])
def test_generic_restore_rejects_changed_or_missing_or_extra_current_payload_keys(
        tmp_path, monkeypatch, payload_mutation):
    backend = _GenericBackend()
    saved_payload = {"draft": "office task", "records": [],
                     "scope": {"sheet": "Data"}}
    backend.saved = {"id": "d" * 32, "payload": deepcopy(saved_payload)}
    current_payload = deepcopy(saved_payload)
    payload_mutation(current_payload)
    _Thread.all = []
    monkeypatch.setattr(workspace_resume.threading, "Thread", _Thread)
    ctl = workspace_resume.WorkspaceResume(
        context=lambda: {"payload": deepcopy(current_payload), "revision": 3,
                         "binding": {}, "unavailable": False},
        busy=lambda: False, on_busy=lambda _value: None, apply=lambda _value: True,
        status=lambda _text: None, store=tmp_path / "generic", task_base=tmp_path,
        backend=backend, thread_name="office-workspace-restore")
    assert not ctl.restore()
    assert not _Thread.all
    assert "verify" not in backend.calls


def test_blocked_reason_is_shown_but_never_persisted(tmp_path, monkeypatch):
    controller, state, _, _, statuses = _harness(tmp_path, monkeypatch)
    state.update(unavailable=True, blocked_reason="Office 资料尚未完成回读")
    assert not controller.save()
    assert statuses[-1] == "Office 资料尚未完成回读"
    assert workspace_state.load(tmp_path / "workspace") is None
    assert not controller.restore()
    assert statuses[-1] == "Office 资料尚未完成回读"
