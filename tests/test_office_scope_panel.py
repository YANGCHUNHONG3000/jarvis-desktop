"""Offline checks for Office scope labels and selector input bounds."""
import pytest

from agent_service import office_scope_panel as panel


def test_shift_range_returns_visible_inclusive_rows_and_handles_missing_anchor():
    visible = ["file", "group", "entry1", "entry2"]
    assert panel._range_nodes(visible, "group", "entry2") == ("group", "entry1", "entry2")
    assert panel._range_nodes(visible, "entry2", "group") == ("group", "entry1", "entry2")
    assert panel._range_nodes(visible, "collapsed", "entry2") == ("entry2",)


def test_set_visible_range_selects_rows_and_focuses_target():
    class Tree:
        selected = ()
        focused = None
        def selection_set(self, *nodes):
            self.selected = nodes
        def focus(self, node):
            self.focused = node

    tree = Tree()
    panel._set_visible_range(tree, ["file", "group", "entry"], "file", "entry")
    assert tree.selected == ("file", "group", "entry")
    assert tree.focused == "entry"


def test_shift_bindings_keep_anchor_on_ctrl_click_and_select_range():
    class Tree:
        def __init__(self):
            self.bindings, self.selected, self.current = {}, (), "group"
            self.children = {"": ["file"], "file": ["group", "other"],
                             "group": ["entry"], "other": []}
        def bind(self, sequence, callback, add=None):
            self.bindings[sequence] = callback
        def identify_row(self, y):
            return y
        def get_children(self, node):
            return self.children[node]
        def item(self, node, option):
            return option == "open" and node in ("file", "group")
        def focus(self, node=None):
            if node is not None:
                self.current = node
            return self.current
        def selection(self):
            return self.selected
        def selection_set(self, *nodes):
            self.selected = nodes

    class Event:
        def __init__(self, state, y):
            self.state, self.y = state, y

    tree = Tree()
    anchor = panel._bind_shift_range(tree)
    tree.bindings["<Button-1>"](Event(0, "group"))
    tree.bindings["<Button-1>"](Event(0x0004, "other"))
    assert anchor["item"] == "group"
    tree.bindings["<Shift-End>"](None)
    assert tree.selected == ("group", "entry", "other")
    assert tree.current == "other"


def test_word_scope_labels_use_zero_based_indices_as_one_based_display():
    cell = {"at": {"table_index": 0, "row_index": 1, "column_index": 2, "paragraph_index": 0},
            "value": "complete cell value", "editable": True}
    paragraph = {"at": {"paragraph_index": 0}, "value": "body text", "editable": False}

    assert panel._group(cell, "document") == "表格 1"
    assert panel._label(cell, "document") == "表行2列3 第1段"
    assert panel._group(paragraph, "document") == "Word 正文"
    assert panel._label(paragraph, "document") == "第1段  [只读]"


def test_spreadsheet_labels_use_contract_fields():
    entry = {"at": {"sheet": "Budget", "cell": "C4"}, "value": "42", "editable": True}
    assert panel._group(entry, "spreadsheet") == "工作表 Budget"
    assert panel._label(entry, "spreadsheet") == "C4"


def test_detail_text_preserves_full_path_and_long_value():
    path = "C:/reports/" + "long-name" * 20 + ".docx"
    value = "important value " * 200
    detail = panel._detail_text(path, {"value": value}, "第1段")
    assert path in detail and value in detail and "第1段" in detail


@pytest.mark.parametrize("records", [[], [{}] * 33])
def test_file_selector_rejects_empty_or_over_limit_without_opening(monkeypatch, records):
    monkeypatch.setattr(panel, "_modal", lambda *args: pytest.fail("window must not open"))
    with pytest.raises(ValueError):
        panel.open_file_selector(object(), records, lambda _selected: None)
