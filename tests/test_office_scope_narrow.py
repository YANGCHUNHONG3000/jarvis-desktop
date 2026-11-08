"""Subset-only lookup, type preservation and real Tk selection controls."""
from copy import deepcopy
import tkinter as tk
from tkinter import ttk

import pytest

from agent_service import office_scope_narrow as narrow
from agent_service.office_scope import selection_from_indices
from test_computer_goal_panel import hidden_root
from test_office_workspace import prepared


@pytest.fixture
def verified(tmp_path):
    rows = [[{'at': {'sheet': 'Data', 'cell': f'B{i + 1}'}, 'value': value, 'editable': i != 4}
             for i, value in enumerate(['处理事项', 0, False, None, '=SUM(A1:A2)'])],
            [{'at': {'table_index': 2, 'row_index': 0, 'column_index': 1, 'paragraph_index': 0},
              'value': '处理事项', 'editable': True}]]
    records = [{'path': str(tmp_path / name), 'sha256': str(i + 1) * 64}
               for i, name in enumerate(('same.xlsx', 'same.docx'))]
    return {'payload': {'draft': 'keep exact draft', 'records': records,
                        'scope': [{**r, 'locations': [deepcopy(row['at']) for row in values]}
                                  for r, values in zip(records, rows)]},
            'refs': [{'artifact': r['path'], 'sha256': r['sha256']} for r in records],
            'review': [{'path': r['path'], 'rows': values} for r, values in zip(records, rows)]}


@pytest.mark.parametrize('query,expected', [
    ('处理事项', [(0, 0), (1, 0)]), ('b2', [(0, 1)]), ('false', [(0, 2)]),
    ('null', [(0, 3)]), ('SUM(A1:A2)', [(0, 4)]), ('表格 3', [(1, 0)]),
    ('same.docx', [(1, 0)]), ('missing', []), ('[.*]', []),
])
def test_literal_lookup_keeps_all_duplicate_locations_and_types(verified, query, expected):
    before = deepcopy(verified)
    assert narrow.find_rows(narrow.catalog_from_review(verified), query) == expected
    assert verified == before


@pytest.mark.parametrize('query', [None, 2, 'a' * 201])
def test_query_bound(verified, query):
    with pytest.raises(ValueError):
        narrow.find_rows(narrow.catalog_from_review(verified), query)


def test_subset_keeps_typed_originals_and_exact_refs(verified):
    catalog = narrow.catalog_from_review(verified)
    selected = selection_from_indices(catalog, [(0, 1), (0, 2), (0, 3), (1, 0)])
    assert narrow.checked_selection(verified, selected) == selected
    assert [r['value'] for r in catalog['files'][0]['entries'][1:4]] == [0, False, None]
    selected['scope'][0]['locations'].append({'sheet': 'Data', 'cell': 'Z99'})
    with pytest.raises((ValueError, KeyError)):
        narrow.checked_selection(verified, selected)


@pytest.mark.parametrize('change', ['path', 'sha256', 'refs', 'duplicate', 'extra_location', 'files'])
def test_tampered_callback_cannot_expand_or_rebind_selection(verified, change):
    selection = selection_from_indices(narrow.catalog_from_review(verified), [(0, 0)])
    if change == 'path':
        selection['scope'][0]['path'] = 'unverified.xlsx'
    elif change == 'sha256':
        selection['scope'][0]['sha256'] = 'f' * 64
    elif change == 'refs':
        selection['artifact_refs'][0]['sha256'] = 'f' * 64
    elif change == 'duplicate':
        selection['scope'][0]['locations'] *= 2
    elif change == 'extra_location':
        selection['scope'][0]['locations'].append({'sheet': 'Data', 'cell': 'C1'})
    else:
        selection['files'] = ('different.xlsx',)
    with pytest.raises((ValueError, KeyError)):
        narrow.checked_selection(verified, selection)


def test_inconsistent_review_rejected_and_readonly_not_only_target(verified):
    catalog = narrow.catalog_from_review(verified)
    with pytest.raises(ValueError, match='只读'):
        selection_from_indices(catalog, [(0, 4)])
    verified['review'][0]['rows'][0]['at']['cell'] = 'Z99'
    with pytest.raises(ValueError, match='mismatch'):
        narrow.catalog_from_review(verified)


def test_real_verified_files_need_no_new_reads_for_filtering(prepared, tmp_path):
    from agent_service import office_workspace
    payload, paths, _ = prepared
    before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in paths]
    verified = office_workspace.verify(payload, tmp_path / 'tasks')
    catalog = narrow.catalog_from_review(verified)
    matches = narrow.find_rows(catalog, '')
    editable = [pair for pair in matches if catalog['files'][pair[0]]['entries'][pair[1]]['editable']]
    selection = selection_from_indices(catalog, editable[:1])
    assert narrow.checked_selection(verified, selection) == selection
    assert [(p.read_bytes(), p.stat().st_mtime_ns) for p in paths] == before


def _children(widget):
    for child in widget.winfo_children():
        yield child
        yield from _children(child)


def _controls(win):
    children = list(_children(win))
    combo = next(w for w in children if isinstance(w, ttk.Combobox))
    tree = next(w for w in children if isinstance(w, ttk.Treeview))
    accept = next(w for w in children if isinstance(w, ttk.Button) and w.cget('text') == '核验并接入选中范围')
    return combo, tree, accept


def test_real_selector_clears_hidden_selection_and_requires_explicit_choice(hidden_root, verified):
    accepted = []
    win = narrow.open_selector(hidden_root, verified, accepted.append)
    combo, tree, accept = _controls(win)
    assert not tree.selection()
    accept.invoke()
    assert not accepted
    combo.set('处理事项')
    assert len(tree.get_children()) == 2 and not tree.selection()
    tree.selection_set('0', '1')
    combo.set('does not exist')
    assert not tree.get_children() and not tree.selection()
    accept.invoke()
    assert not accepted
    combo.set('处理事项')
    assert not tree.selection()
    tree.selection_set('1')
    accept.invoke()
    assert len(accepted) == 1
    assert accepted[0]['files'] == (verified['payload']['records'][1]['path'],)
    assert len(accepted[0]['scope'][0]['locations']) == 1


def test_real_selector_cannot_accept_readonly_and_cancel_is_noop(hidden_root, verified):
    accepted = []
    before = deepcopy(verified)
    win = narrow.open_selector(hidden_root, verified, accepted.append)
    combo, tree, accept = _controls(win)
    combo.set('=SUM')
    tree.selection_set('0')
    accept.invoke()
    assert not accepted
    combo.set('x' * 201)
    assert not tree.get_children()
    next(w for w in _children(win) if isinstance(w, ttk.Button) and w.cget('text') == '取消').invoke()
    assert not accepted and verified == before


def test_selector_uses_explicit_shift_binding_and_resets_anchor_after_filter(hidden_root, verified, monkeypatch):
    from agent_service import office_scope_panel
    original, anchors = office_scope_panel._bind_shift_range, []
    def bind(tree):
        anchor = original(tree)
        anchors.append(anchor)
        return anchor
    monkeypatch.setattr(office_scope_panel, '_bind_shift_range', bind)
    win = narrow.open_selector(hidden_root, verified, lambda _: None)
    combo, tree, _ = _controls(win)
    assert tree.bind('<Shift-Down>') and tree.bind('<Shift-End>')
    anchors[0]['item'] = '0'
    combo.set('处理事项')
    assert anchors[0]['item'] is None and not tree.selection()
    win.destroy()
