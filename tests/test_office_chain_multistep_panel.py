"""Four-step Office task chain UI contracts; lightweight widgets only."""
import pytest

from agent_service import office_task_chain_panel as module
from test_office_task_chain_panel import Thread, finish, harness, mutate


class Widget:
    all = []

    def __init__(self, *args, **options):
        self.options = options
        self.value = ""
        self.state = options.get("state")
        self.command = options.get("command")
        self.bindings = {}
        self.insertions = []
        Widget.all.append(self)

    def pack(self, **_options):
        pass

    def insert(self, _where, value):
        self.value = value
        self.insertions.append(value)

    def get(self, *_args):
        return self.value

    def delete(self, *_args):
        self.value = ""

    def configure(self, **options):
        self.state = options.get("state", self.state)

    def heading(self, *_args, **_kwargs):
        pass

    def column(self, *_args, **_kwargs):
        pass

    def bind(self, event, callback):
        self.bindings[event] = callback

    def insert_row(self, parent, index, *, iid, values):
        self.rows[iid] = values

    def selection(self):
        return self.selected


class Tree(Widget):
    def __init__(self, *args, **options):
        super().__init__(*args, **options)
        self.rows = {}
        self.selected = ()

    def insert(self, parent, index, *, iid, values):
        self.rows[iid] = values

    def select(self, iid):
        self.selected = (iid,)
        self.bindings["<<TreeviewSelect>>"]()


def install_widgets(monkeypatch, *, treeview=True):
    import tkinter as tk
    from tkinter import ttk
    from agent_service import office_scope_panel

    Widget.all = []
    monkeypatch.setattr(tk, "Text", Widget)
    monkeypatch.setattr(ttk, "Label", Widget)
    monkeypatch.setattr(ttk, "Button", Widget)
    if treeview:
        monkeypatch.setattr(ttk, "Treeview", Tree)
    closed = []
    monkeypatch.setattr(office_scope_panel, "_modal", lambda *_: (object(), lambda: closed.append(True)))
    return closed


def text_fields():
    return [widget for widget in Widget.all if "height" in widget.options]


def button_command():
    return next(widget.command for widget in Widget.all if widget.command is not None)


@pytest.mark.parametrize("count", [3, 4])
def test_new_dialog_saves_three_or_four_authored_steps_once(tmp_path, monkeypatch, count):
    panel, _context, _log, state = harness(tmp_path, monkeypatch)
    closed = install_widgets(monkeypatch, treeview=False)
    saved = []
    def create(instructions, snapshot):
        saved.append((instructions, snapshot))
        panel.pending = {"creating": True}
        return True
    monkeypatch.setattr(panel, "create", create)

    assert panel.new_dialog()
    fields = text_fields()
    assert len(fields) == 4
    values = ["step one", "step two", "step three", "step four"]
    for field, value in zip(fields, values):
        field.value = value
    if count == 3:
        fields[3].value = "   "

    save = button_command()
    save()
    save()

    assert len(saved) == 1
    assert saved[0][0] == values[:count]
    assert closed == [True]


@pytest.mark.parametrize("values", [
    ["one", "two", "", "four"],
    ["", "", "", ""],
    ["one", "two", "bad\x00step", ""],
])
def test_new_dialog_rejects_gaps_empty_requirements_and_control_characters(tmp_path, monkeypatch, values):
    panel, _context, log, _state = harness(tmp_path, monkeypatch)
    closed = install_widgets(monkeypatch, treeview=False)
    calls = []
    monkeypatch.setattr(panel, "create", lambda *args: calls.append(args) or True)

    assert panel.new_dialog()
    for field, value in zip(text_fields(), values):
        field.value = value
    save = button_command()
    save()

    assert calls == [] and closed == []
    assert log.statuses and "连续填写2至4条" in log.statuses[-1]


@pytest.mark.parametrize("changed", ["main_new", "source"])
def test_new_dialog_does_not_create_when_draft_or_source_changes(tmp_path, monkeypatch, changed):
    panel, context, _log, _state = harness(tmp_path, monkeypatch)
    closed = install_widgets(monkeypatch, treeview=False)
    calls = []
    original_create = panel.create
    def create(instructions, snapshot):
        calls.append(instructions)
        return original_create(instructions, snapshot)
    monkeypatch.setattr(panel, "create", create)
    assert panel.new_dialog()
    for field, value in zip(text_fields(), ["one", "two", "three", ""]):
        field.value = value
    mutate(context, changed)
    save = button_command()
    save()

    assert not Thread.all and calls and closed == []
    assert panel.pending is None


def test_history_shows_all_four_instructions_and_restores_selected_id_once(tmp_path, monkeypatch):
    panel, _context, _log, _state = harness(tmp_path, monkeypatch)
    closed = install_widgets(monkeypatch)
    instructions = ["first", "second", "third", "fourth"]
    monkeypatch.setattr(module.core, "list_chains", lambda _store: [
        {"id": "f" * 32, "instructions": instructions}
    ])
    restored = []
    monkeypatch.setattr(panel, "restore", lambda ident: restored.append(ident) or True)

    assert panel.history_dialog()
    tree = next(widget for widget in Widget.all if isinstance(widget, Tree))
    details = next(widget for widget in text_fields() if widget.options.get("state") == "disabled")
    assert tree.rows["0"] == (4, "first")
    tree.select("0")

    assert "第1步：first" in details.value
    assert "第2步：second" in details.value
    assert "第3步：third" in details.value
    assert "第4步：fourth" in details.value
    assert details.state == "disabled"

    resume = button_command()
    resume()
    resume()
    assert restored == ["f" * 32]
    assert closed == [True]


@pytest.mark.parametrize("index,total,label", [
    (2, 3, "第3/3步"), (3, 4, "第4/4步"), (1, None, "第2/2步")
])
def test_restore_status_uses_total_and_keeps_legacy_default(tmp_path, monkeypatch, index, total, label):
    panel, _context, log, state = harness(tmp_path, monkeypatch)
    state.update(index=index, status="ready")
    if total is not None:
        state["total"] = total

    assert panel.restore(state["id"])
    finish(panel)

    assert log.applied[-1]["payload"]["draft"] == state["payload"]["draft"]
    assert any(label in status for status in log.statuses)
