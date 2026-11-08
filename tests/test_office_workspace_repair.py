"""Offline two-stage preparation and explicit Office workspace repair tests."""

from copy import deepcopy

import pytest
from types import ModuleType

from agent_service import computer, office_scope, office_scope_panel, office_workspace
from agent_service.office_workspace_repair import RepairPreparation
from test_office_workspace_panel import _Goal, _Thread, _material, _make_panel


def _ticket(context, records, scope=None, draft="saved draft", ident="a" * 32):
    return {"saved": {"id": ident, "payload": {"draft": draft,
            "records": deepcopy(records), "scope": deepcopy(scope)}},
            "context": deepcopy(context), "message": "源文件变更"}


def _prep(tmp_path, monkeypatch, context, *, clock=lambda: 1.0, ready=None,
          busy=lambda: False, on_busy=lambda _value: None, status=None):
    _Thread.all = []
    monkeypatch.setattr("agent_service.workspace_resume.threading.Thread", _Thread)
    seen_status = [] if status is None else status
    prep = RepairPreparation(context=lambda: deepcopy(context), busy=busy,
        on_busy=on_busy, status=seen_status.append, ready=ready or (lambda *_: None),
        task_base=tmp_path, clock=clock)
    return prep, seen_status


def _stub_stages(monkeypatch, records, scope):
    refs = [{"artifact": item["path"], "sha256": item["sha256"],
             "task_root": "fake-task"} for item in records]
    calls = []
    def verify(payload, _base):
        calls.append(deepcopy(payload))
        return {"payload": deepcopy(payload), "refs": deepcopy(refs), "source_ref": None}
    catalog = {"files": [{"path": item["path"], "entries": []} for item in records],
               "artifact_refs": refs}
    monkeypatch.setattr(office_workspace, "verify", verify)
    monkeypatch.setattr(office_scope, "inspect_files", lambda *_args, **_kwargs: deepcopy(catalog))
    monkeypatch.setattr(computer.ComputerSession, "create", lambda **_kwargs: object())
    return refs, calls, catalog


def test_two_stage_file_then_scope_preparation_returns_verified_payload_and_ticket(
        tmp_path, monkeypatch):
    records, scope, _refs = _material(tmp_path)
    saved_payload = {"draft": "  exact draft\r\n", "records": records, "scope": scope}
    context = {"payload": deepcopy(saved_payload), "revision": 4,
               "binding": {"generation": 2}, "unavailable": False}
    ticket = _ticket(context, records, scope, saved_payload["draft"])
    refs, calls, catalog = _stub_stages(monkeypatch, records, scope)
    ready_values, busy = [], []
    prep, statuses = _prep(tmp_path, monkeypatch, context,
                           ready=lambda result, saved: ready_values.append((result, saved)),
                           on_busy=busy.append)
    assert prep.start(records, ticket, scope=None, select_scope=True)
    assert busy == [True]
    _Thread.all[-1].run()
    prep.poll()
    assert ready_values[-1] == (catalog, ticket)
    assert calls[0]["scope"] is None

    prepared_ticket = deepcopy(ticket)
    assert prep.start(records, prepared_ticket, scope=scope, select_scope=False)
    _Thread.all[-1].run()
    prep.poll()
    verified, returned_ticket = ready_values[-1]
    assert verified["payload"] == saved_payload
    assert verified["refs"] == refs
    assert returned_ticket == ticket
    assert calls[1]["scope"] == scope
    assert busy == [True, False, True, False]
    assert "原草稿尚未替换" in statuses[0]


def test_context_change_then_return_and_late_result_are_discarded(tmp_path, monkeypatch):
    records, scope, _refs = _material(tmp_path)
    value = {"payload": {"draft": "draft", "records": records, "scope": scope},
             "revision": 7, "binding": {"source": "A", "generation": 3},
             "unavailable": False}
    live = deepcopy(value)
    ticket = _ticket(value, records, scope)
    _stub_stages(monkeypatch, records, scope)
    ready, busy = [], []
    prep, statuses = _prep(tmp_path, monkeypatch, live,
                           ready=lambda *args: ready.append(args), on_busy=busy.append)
    # _prep owns a deepcopy provider, so use a live provider after construction.
    prep.context = lambda: deepcopy(live)
    assert prep.start(records, ticket, scope=None, select_scope=True)
    live["binding"]["source"] = "B"
    live["binding"]["source"] = "A"
    live["binding"]["generation"] += 2
    _Thread.all[-1].run()
    prep.poll()
    assert not ready
    assert busy == [True, False]
    assert any("旧结果未接入" in text for text in statuses)


@pytest.mark.parametrize("interrupt", ["stop", "timeout", "close"])
def test_cancel_stop_timeout_and_close_preserve_inputs_and_reject_old_callback(
        tmp_path, monkeypatch, interrupt):
    records, scope, _refs = _material(tmp_path)
    live = {"payload": {"draft": "keep", "records": records, "scope": scope},
            "revision": 2, "binding": {"generation": 1}, "unavailable": False}
    ticket = _ticket(live, records, scope)
    _stub_stages(monkeypatch, records, scope)
    ready, busy = [], []
    now = {"value": 10.0}
    prep, statuses = _prep(tmp_path, monkeypatch, live, clock=lambda: now["value"],
                           ready=lambda *args: ready.append(args), on_busy=busy.append)
    prep.context = lambda: deepcopy(live)
    assert prep.start(records, ticket, scope=None, select_scope=True)
    if interrupt == "stop":
        prep.pending["stop"].set()
        live["binding"]["stopped"] = True
    elif interrupt == "timeout":
        now["value"] = 41.0
    else:
        prep.close()
    worker = _Thread.all[-1]
    worker.run()
    prep.poll()
    assert not ready
    assert busy == [True, False]
    assert live["payload"]["draft"] == "keep"
    assert live["payload"]["records"] == records
    assert any("未接入" in text or "输入已变化" in text for text in statuses)


def test_real_goal_busy_poll_does_not_invalidate_active_preparation(tmp_path, monkeypatch):
    records, scope, _refs = _material(tmp_path)
    target = _Goal()
    active = {"busy": False}
    def on_busy(value):
        target.set_busy(value)
        active["busy"] = value
    target.on_busy = lambda value: active.update(busy=value)
    before_generation = target.capability_source_generation
    context = {"payload": {"draft": "x", "records": records, "scope": scope},
               "revision": 1, "binding": {"generation": 0}, "unavailable": False}
    ticket = _ticket(context, records, scope, "x")
    _stub_stages(monkeypatch, records, scope)
    ready = []
    prep, _ = _prep(tmp_path, monkeypatch, context, ready=lambda *args: ready.append(args),
                    busy=lambda: active["busy"], on_busy=on_busy)
    assert prep.start(records, ticket, scope=None, select_scope=True)
    target.poll()
    assert target.running and target.capability_source_generation == before_generation
    _Thread.all[-1].run()
    prep.poll()
    assert ready
    assert not target.running and not active["busy"]
    assert target.capability_source_generation == before_generation


def test_late_scope_selection_callback_refuses_after_context_change(tmp_path, monkeypatch):
    records, scope, refs = _material(tmp_path)
    goal = _Goal(files=[item["path"] for item in records], refs=refs, scope=scope,
                 draft="original")
    goal.root = object()
    panel = _make_panel(goal, tmp_path / "late-store", tmp_path, monkeypatch)
    saved = {"id": "e" * 32, "payload": {"draft": "original", "records": records,
                                               "scope": scope}}
    error = office_workspace.WorkspaceVerificationError("range_invalid", 0, "range")
    panel.failed(saved, error, panel.context())
    catalog = {"files": [{"path": item["path"], "entries": []} for item in records],
               "artifact_refs": refs}
    selected_callback = []
    monkeypatch.setattr(office_scope_panel, "open_scope_selector",
                        lambda _root, _catalog, callback: selected_callback.append(callback))
    panel.repair_ready(catalog, deepcopy(panel.failure))
    assert selected_callback
    goal.capability_source_generation += 1
    selected_callback[0]({"scope": scope})
    assert panel.preparation.pending is None
    assert not _Thread.all
    assert goal.office_planning_files == tuple(item["path"] for item in records)


class _FakeWidget:
    all = []

    def __init__(self, *args, **kwargs):
        self.options = kwargs
        self.command = kwargs.get("command")
        self.__class__.all.append(self)

    def pack(self, **_kwargs):
        pass

    def configure(self, **kwargs):
        self.options.update(kwargs)

    def invoke(self):
        return self.command()


class _FakeWindow:
    pass


def test_explicit_repair_retains_new_draft_and_cas_uses_failed_snapshot_id(
        tmp_path, monkeypatch):
    records, scope, refs = _material(tmp_path)
    store = tmp_path / "repair-store"
    goal = _Goal(files=[item["path"] for item in records], refs=refs,
                 scope=scope, draft="saved original")
    goal.root = object()
    panel = _make_panel(goal, store, tmp_path, monkeypatch)
    assert panel.save()
    saved = office_workspace.load(store)
    original_context = panel.context()
    error = office_workspace.WorkspaceVerificationError("changed", 1, "source")
    error.detail = "SECRET_RAW_PROVIDER_PATH"
    failure_message = panel.failed(saved, error, original_context)
    assert "第2份文件" in failure_message
    goal.instruction_draft.adopt("  newly edited exact draft\r\n")
    goal.text.edit_modified(True)

    _FakeWidget.all = []
    fake_tk = ModuleType("tkinter")
    fake_ttk = ModuleType("tkinter.ttk")
    fake_ttk.Label = _FakeWidget
    fake_ttk.Button = _FakeWidget
    fake_tk.ttk = fake_ttk
    monkeypatch.setitem(__import__("sys").modules, "tkinter", fake_tk)
    monkeypatch.setitem(__import__("sys").modules, "tkinter.ttk", fake_ttk)
    win = _FakeWindow()
    monkeypatch.setattr(office_scope_panel, "_modal", lambda *_args: (win, lambda: None))
    selector_calls = []
    monkeypatch.setattr(office_scope_panel, "open_scope_selector",
                        lambda _root, catalog, callback: selector_calls.append((catalog, callback)))
    refs_seen, verified_calls, catalog = _stub_stages(monkeypatch, records, scope)

    assert panel.repair_options()
    range_button = next(widget for widget in _FakeWidget.all
                        if widget.options.get("text") == "沿用文件，重新选范围")
    range_button.invoke()
    assert panel.preparation.pending is not None
    assert panel.preparation.pending["ticket"]["saved"]["payload"]["draft"] == \
        "  newly edited exact draft\r\n"
    _Thread.all[-1].run()
    panel.poll()
    assert selector_calls and selector_calls[0][0] == catalog
    selected_scope = {"scope": scope}
    selector_calls[0][1](selected_scope)
    assert panel.preparation.pending is not None
    _Thread.all[-1].run()
    panel.poll()
    assert verified_calls[-1]["scope"] == scope
    assert goal.instruction_draft.text == "  newly edited exact draft\r\n"
    assert goal.office_scope == scope
    assert goal.plan is None
    assert panel.failure is None
    assert panel.controller.expected_id == saved["id"]

    receipt_paths = list(store.glob("failure-*.json"))
    assert len(receipt_paths) == 1
    receipt = receipt_paths[0].read_text(encoding="utf-8")
    assert saved["id"] in receipt and '"reason":"changed"' in receipt
    assert "SECRET_RAW_PROVIDER_PATH" not in receipt
    assert all(item["path"] not in receipt for item in records)
    assert "newly edited exact draft" not in receipt

    external_payload = deepcopy(saved["payload"])
    external_payload["draft"] = "external winner"
    office_workspace.save(store, external_payload, saved["id"])
    assert not panel.save()
    assert "更新的工作区" in goal.status.get()


@pytest.mark.parametrize("failed_draft,edits,expected", [
    ("saved before failure", ("clear",), ""),
    ("", ("type", "clear"), ""),
    ("", (), "saved original"),
])
def test_repair_options_preserves_explicit_clear_but_uses_saved_for_untouched_blank(
        tmp_path, monkeypatch, failed_draft, edits, expected):
    records, scope, refs = _material(tmp_path)
    goal = _Goal(files=[item["path"] for item in records], refs=refs,
                 scope=None, draft=failed_draft)
    goal.root = object()
    store = tmp_path / ("clear-store-" + str(len(edits)) + failed_draft[:1])
    panel = _make_panel(goal, store, tmp_path, monkeypatch)
    failed_context = panel.context()
    saved = {"id": "9" * 32, "payload": {"draft": "saved original",
            "records": records, "scope": None}}
    panel.failed(saved, office_workspace.WorkspaceVerificationError("missing"), failed_context)
    for edit in edits:
        if edit == "type":
            goal.instruction_draft.adopt("temporary text")
        else:
            goal.instruction_draft.adopt("")

    _FakeWidget.all = []
    fake_tk = ModuleType("tkinter")
    fake_ttk = ModuleType("tkinter.ttk")
    fake_ttk.Label = _FakeWidget
    fake_ttk.Button = _FakeWidget
    fake_tk.ttk = fake_ttk
    monkeypatch.setitem(__import__("sys").modules, "tkinter", fake_tk)
    monkeypatch.setitem(__import__("sys").modules, "tkinter.ttk", fake_ttk)
    monkeypatch.setattr(office_scope_panel, "_modal",
                        lambda *_args: (_FakeWindow(), lambda: None))
    assert panel.repair_options()
    range_button = next(widget for widget in _FakeWidget.all
                        if widget.options.get("text") == "沿用文件，重新选范围")
    range_button.invoke()
    assert panel.preparation.pending is not None
    assert panel.preparation.pending["ticket"]["saved"]["payload"]["draft"] == expected
