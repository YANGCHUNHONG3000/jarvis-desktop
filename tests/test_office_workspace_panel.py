"""Offline Office workspace panel persistence guards."""

from copy import deepcopy
import queue
import threading

import pytest

from agent_service import computer_goal_panel, office_workspace
from agent_service.office_workspace_panel import OfficeWorkspacePanel


class _Var:
    def __init__(self, value=None):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value

    def configure(self, **_options):
        pass


class _Draft:
    def __init__(self, text=""):
        self.text = text
        self.revision = 0

    def snapshot(self):
        return {"text": self.text, "revision": self.revision}

    def adopt(self, text):
        if self.text != text:
            self.text = text
            self.revision += 1
        return self.snapshot()


class _Text:
    def __init__(self):
        self.modified = True

    def edit_modified(self, value):
        self.modified = value

    def configure(self, **_options):
        pass


class _Thread:
    all = []

    def __init__(self, target, name, **_kwargs):
        self.target = target
        self.name = name

    def start(self):
        self.all.append(self)

    def run(self):
        self.target()


class _Goal:
    def __init__(self, *, mode=None, files=(), refs=None, scope=None, draft=""):
        self.mode = _Var(mode or computer_goal_panel.MODES[9])
        self.office_planning_files = tuple(files)
        self.office_artifact_refs = deepcopy(refs or [])
        self.office_scope = deepcopy(scope)
        self.instruction_draft = _Draft(draft)
        self.plan = None
        self.running = False
        self.closed = False
        self.status = _Var("")
        self.text = _Text()
        self.tab = object()
        self.preview_box = None
        self.snapshots = []
        self.source = _Var("")
        self.destination = _Var("")
        self.expression = _Var("")
        self.control = _Var("")
        self.interaction_request = None
        self.interaction_fields = None
        self.comparison_goal = None
        self.comparison_fields = None
        self.comparison_source = None
        self.capability_history_seed = None
        self.human_pending = None
        self.daily_task_route_generation = 0
        self.capability_source_generation = 0
        self.invalidate_calls = 0
        self.executions = 0
        self.mode_refreshes = []
        self.shown = ""
        self.stopped = threading.Event()
        self.stopped.set()
        self.on_busy = lambda _value: None
        self.external_activity_seen = False
        self.messages = queue.Queue()
        self.poll_id = None
        self.root = _Root()
        self.output_edit_button = _Button()
        self.scope_button = _Button()
        self.recovery_button = _Button()
        self.development_button = _Button()
        self.comparison_source_button = _Button()
        self.comparison_reset_button = _Button()
        self.capability_clarification_button = _Button()
        self.daily_task_route_button = _Button()
        self.capability_previous_button = _Button()
        self.interaction_button = _Button()
        self.execute_button = _Button()
        self.capability_history_seed = None
        self.external = {"busy": False}
        self.external_busy = lambda: self.external["busy"]
        self.cancel_human = lambda: None

    def external_busy(self):
        return False

    def mode_changed(self, refresh_sources=True):
        self.mode_refreshes.append(refresh_sources)

    def invalidate(self):
        self.invalidate_calls += 1
        self.plan = None

    def show_text(self, value):
        self.shown = value

    def _update_pause_checkbox(self):
        pass

    def update_office_bundle_button(self):
        pass

    def set_busy(self, value):
        computer_goal_panel.GoalPanel.set_busy(self, value)

    def notify_external_activity(self, active=True, *, force=False):
        if active:
            self.capability_source_generation += 1
            self.external_activity_seen = True
        else:
            self.external_activity_seen = False

    def poll(self):
        computer_goal_panel.GoalPanel.poll(self)


class _Button:
    def __init__(self):
        self.options = {}

    def configure(self, **options):
        self.options.update(options)


class _Root:
    def after_cancel(self, _ident):
        pass

    def after(self, _delay, _callback):
        return "timer"


def _material(tmp_path):
    first = str((tmp_path / "edit.docx").resolve())
    second = str((tmp_path / "data.xlsx").resolve())
    records = [{"path": first, "sha256": "a" * 64},
               {"path": second, "sha256": "b" * 64}]
    scope = [{"path": first, "sha256": "a" * 64,
              "locations": [{"paragraph_index": 3}]},
             {"path": second, "sha256": "b" * 64,
              "locations": [{"sheet": "Data", "cell": "B4"}]}]
    refs = [{"artifact": item["path"], "sha256": item["sha256"],
             "task_root": str(tmp_path)} for item in records]
    return records, scope, refs


def _make_panel(goal, store, task_base, monkeypatch):
    _Thread.all = []
    monkeypatch.setattr("agent_service.workspace_resume.threading.Thread", _Thread)
    return OfficeWorkspacePanel(goal, store=store, task_base=task_base,
                                busy=lambda: False, on_busy=lambda _value: None,
                                widgets=False)


def test_save_and_restore_preserve_exact_draft_refs_scope_without_plan_or_execution(
        tmp_path, monkeypatch):
    records, scope, refs = _material(tmp_path)
    original = _Goal(files=[item["path"] for item in records], refs=refs,
                     scope=scope, draft="  编辑说明\r\n")
    store = tmp_path / "office-workspace"
    panel = _make_panel(original, store, tmp_path, monkeypatch)
    assert panel.save()
    saved = office_workspace.load(store)
    assert saved["payload"] == {"draft": "  编辑说明\r\n", "records": records,
                                "scope": scope}

    restored_goal = _Goal(mode=computer_goal_panel.MODES[0])
    restored = _make_panel(restored_goal, store, tmp_path, monkeypatch)
    assert restored.restore()
    worker = _Thread.all[-1]
    assert worker.name == "office-workspace-restore"
    # The evidence verifier is exercised through the real office backend API,
    # while filesystem-backed source inspection stays mocked and side-effect-free.
    verified = {"payload": deepcopy(saved["payload"]), "refs": deepcopy(refs),
                "source_ref": None}
    monkeypatch.setattr(office_workspace, "verify", lambda payload, _base: deepcopy(verified))
    worker.run()
    assert restored.poll()
    assert restored_goal.instruction_draft.snapshot() == {
        "text": "  编辑说明\r\n", "revision": 1}
    assert restored_goal.office_planning_files == tuple(item["path"] for item in records)
    assert restored_goal.office_artifact_refs == refs
    assert restored_goal.office_scope == scope
    assert restored_goal.plan is None
    assert not restored_goal.stopped.is_set()
    assert restored_goal.text.modified is False
    assert restored_goal.mode.get() == computer_goal_panel.MODES[9]
    assert restored_goal.executions == 0
    assert "重新批准" in restored_goal.shown


@pytest.mark.parametrize("change", ["scope", "refs", "new_mode", "plan"])
def test_conflicting_scope_refs_new_mode_or_plan_refuses_restore(tmp_path, monkeypatch, change):
    records, scope, refs = _material(tmp_path)
    source = _Goal(files=[item["path"] for item in records], refs=refs,
                   scope=scope, draft="saved draft")
    store = tmp_path / ("store-" + change)
    source_panel = _make_panel(source, store, tmp_path, monkeypatch)
    assert source_panel.save()

    target = _Goal()
    if change == "scope":
        target.office_scope = deepcopy(scope)
        target.office_scope[0]["locations"][0]["paragraph_index"] = 8
        target.office_planning_files = tuple(item["path"] for item in records)
        target.office_artifact_refs = deepcopy(refs)
    elif change == "refs":
        target.office_planning_files = tuple(item["path"] for item in records)
        target.office_artifact_refs = deepcopy(refs)
        target.office_artifact_refs[0]["sha256"] = "c" * 64
    elif change == "new_mode":
        target.mode.set(computer_goal_panel.MODES[1])
    else:
        target.plan = {"preview": "existing"}
    panel = _make_panel(target, store, tmp_path, monkeypatch)
    assert not panel.controller.restore()
    assert not _Thread.all
    assert target.plan == ({"preview": "existing"} if change == "plan" else None)


def test_generation_change_then_returning_to_same_source_binding_discards_restore(
        tmp_path, monkeypatch):
    records, scope, refs = _material(tmp_path)
    store = tmp_path / "generation-store"
    source_panel = _make_panel(_Goal(files=[x["path"] for x in records], refs=refs,
                                     scope=scope, draft="same draft"),
                               store, tmp_path, monkeypatch)
    assert source_panel.save()
    target = _Goal()
    target.capability_source_generation = 9
    panel = _make_panel(target, store, tmp_path, monkeypatch)
    verified = {"payload": {"draft": "same draft", "records": records, "scope": scope},
                "refs": refs, "source_ref": None}
    monkeypatch.setattr(office_workspace, "verify", lambda *_: deepcopy(verified))
    status_before = target.status.get()
    assert panel.restore()
    # Simulate selecting another source and then returning to the same source;
    # the value is back, but the monotonic generation still changed twice.
    target.capability_source_generation += 1
    target.capability_source_generation += 1
    _Thread.all[-1].run()
    assert panel.poll()
    assert target.office_planning_files == ()
    assert target.instruction_draft.text == ""
    assert target.plan is None
    assert "内容已变化" in target.status.get()
    assert target.status.get() != status_before


@pytest.mark.parametrize("interrupt", ["stop", "timeout"])
def test_real_goal_busy_poll_stop_and_timeout_discard_late_restore(
        tmp_path, monkeypatch, interrupt):
    records, scope, refs = _material(tmp_path)
    store = tmp_path / ("busy-store-" + interrupt)
    source = _Goal(files=[item["path"] for item in records], refs=refs,
                   scope=scope, draft="busy poll draft")
    source_panel = _make_panel(source, store, tmp_path, monkeypatch)
    assert source_panel.save()

    target = _Goal()
    busy_events = []
    def on_busy(value):
        target.set_busy(value)
        target.external["busy"] = value
        busy_events.append(value)
    target.on_busy = lambda value: target.external.update(busy=value)
    panel = OfficeWorkspacePanel(target, store=store, task_base=tmp_path,
                                 busy=lambda: target.external["busy"],
                                 on_busy=on_busy, widgets=False)
    verified = {"payload": {"draft": "busy poll draft", "records": records,
                             "scope": scope}, "refs": refs, "source_ref": None}
    monkeypatch.setattr(office_workspace, "verify", lambda *_: deepcopy(verified))
    starting_generation = target.capability_source_generation
    assert panel.restore()
    assert target.running and target.external["busy"]
    # Exercise the real GoalPanel.poll external-busy check. Running=True must
    # prevent its regular activity notification from invalidating this restore.
    target.poll()
    assert target.capability_source_generation == starting_generation

    worker = _Thread.all[-1]
    if interrupt == "stop":
        target.stopped.set()
    else:
        panel.controller.deadline = -1
    worker.run()
    panel.poll()
    assert target.office_planning_files == ()
    assert target.instruction_draft.text == ""
    assert target.plan is None
    assert not target.running and not target.external["busy"]
    assert busy_events == [True, False]


def test_office_files_without_complete_artifact_refs_are_not_saved_or_restored(
        tmp_path, monkeypatch):
    records, scope, _refs = _material(tmp_path)
    goal = _Goal(files=[item["path"] for item in records], refs=[], scope=scope,
                 draft="unverified")
    store = tmp_path / "no-ref-store"
    panel = _make_panel(goal, store, tmp_path, monkeypatch)
    assert not panel.save()
    assert "来源证明" in goal.status.get()
    assert office_workspace.load(store) is None


def test_panel_failure_callback_records_only_safe_reason_and_failed_snapshot_id(
        tmp_path, monkeypatch):
    records, scope, refs = _material(tmp_path)
    goal = _Goal(files=[item["path"] for item in records], refs=refs,
                 scope=scope, draft="private draft")
    store = tmp_path / "failure-ticket-store"
    panel = _make_panel(goal, store, tmp_path, monkeypatch)
    saved = {"id": "f" * 32, "payload": {"draft": "private draft",
                                               "records": records, "scope": scope}}
    error = office_workspace.WorkspaceVerificationError("changed", 1, "source")
    error.detail = "PRIVATE_ERROR_WITH_PATH"
    message = panel.controller.on_failure(saved, error, panel.context())
    assert "第2份文件" in message
    assert panel.failure["saved"] == saved
    receipt = next(store.glob("failure-*.json")).read_text(encoding="utf-8")
    assert saved["id"] in receipt
    assert '"reason":"changed"' in receipt and '"file_index":1' in receipt
    assert "PRIVATE_ERROR_WITH_PATH" not in receipt
    assert all(item["path"] not in receipt for item in records)
    assert "private draft" not in receipt


def test_empty_new_office_panel_can_restore_saved_workspace(tmp_path, monkeypatch):
    records, scope, refs = _material(tmp_path)
    store = tmp_path / "empty-target-store"
    source = _Goal(files=[item["path"] for item in records], refs=refs,
                   scope=scope, draft="restore me")
    assert _make_panel(source, store, tmp_path, monkeypatch).save()
    target = _Goal(mode=computer_goal_panel.MODES[0])
    panel = _make_panel(target, store, tmp_path, monkeypatch)
    verified = {"payload": {"draft": "restore me", "records": records, "scope": scope},
                "refs": refs, "source_ref": None}
    monkeypatch.setattr(office_workspace, "verify", lambda *_: deepcopy(verified))
    assert panel.restore()
    _Thread.all[-1].run()
    panel.poll()
    assert target.instruction_draft.text == "restore me"
    assert target.plan is None
    assert not target.stopped.is_set()
    assert target.executions == 0
