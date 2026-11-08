"""Local literal lookup restricted to previously verified Office selections."""
from copy import deepcopy
import json
from pathlib import Path

from .computer import digest
from .office_input_review import location_label
from .office_scope import selection_from_indices


def catalog_from_review(verified):
    payload, review, refs = verified['payload'], verified['review'], verified['refs']
    records, scope = payload['records'], payload['scope']
    if not scope or not 1 <= len(records) <= 4 or not len(records) == len(review) == len(scope) == len(refs):
        raise ValueError('office_narrow_missing')
    files = []
    for record, selected, observed, ref in zip(records, scope, review, refs):
        rows = observed['rows']
        if (selected['path'] != record['path'] or observed['path'] != record['path']
                or selected['sha256'] != record['sha256'] or ref.get('artifact') != record['path']
                or ref.get('sha256') != record['sha256'] or not rows
                or len({digest(r['at']) for r in rows}) != len(rows)
                or {digest(r['at']) for r in rows} != {digest(at) for at in selected['locations']}):
            raise ValueError('office_narrow_mismatch')
        files.append({**deepcopy(record), 'entries': deepcopy(rows)})
    return {'files': files, 'artifact_refs': deepcopy(refs)}


def find_rows(catalog, query):
    if type(query) is not str or len(query) > 200:
        raise ValueError('筛选词最多200字')
    query = query.strip().casefold()
    result = []
    for fi, file in enumerate(catalog['files']):
        for ei, row in enumerate(file['entries']):
            text = ' '.join((Path(file['path']).name, location_label(row['at']),
                             json.dumps(row['value'], ensure_ascii=False)))
            if not query or query in text.casefold():
                result.append((fi, ei))
    return result


def checked_selection(verified, selection):
    """Rebuild a strict subset; don't trust an arbitrary UI callback payload."""
    catalog = catalog_from_review(verified)
    if type(selection) is not dict or type(selection.get('scope')) is not list:
        raise ValueError('office_narrow_selection_invalid')
    lookup = {f['path']: (i, f) for i, f in enumerate(catalog['files'])}
    indices = []
    for part in selection['scope']:
        fi, file = lookup[part['path']]
        if part['sha256'] != file['sha256']:
            raise ValueError('office_narrow_selection_changed')
        positions = {digest(row['at']): ei for ei, row in enumerate(file['entries'])}
        indices.extend((fi, positions[digest(at)]) for at in part['locations'])
    rebuilt = selection_from_indices(catalog, indices)
    if rebuilt != selection:
        raise ValueError('office_narrow_not_subset')
    return rebuilt


def open_selector(root, verified, on_selected):
    import tkinter as tk
    from tkinter import ttk
    from .office_scope_panel import _bind_shift_range, _modal
    verified = deepcopy(verified)
    catalog = catalog_from_review(verified)
    win, close = _modal(root, '定位并缩小办公范围', '940x620')
    ttk.Label(win, text='仅在当前范围内查找。重复内容会分别列出，请明确选择；不自动扩大范围。',
              wraplength=900).pack(anchor='w', padx=12, pady=10)
    query = tk.StringVar(win)
    values = list(dict.fromkeys(row['value'] for file in catalog['files'] for row in file['entries']
                               if type(row['value']) is str and 0 < len(row['value']) <= 80))[:80]
    bar = ttk.Frame(win)
    bar.pack(fill='x', padx=12)
    ttk.Label(bar, text='原值、文件名或位置：').pack(side='left')
    entry = ttk.Combobox(bar, textvariable=query, values=values)
    entry.pack(side='left', fill='x', expand=True)
    ttk.Button(bar, text='清除筛选', command=lambda: query.set('')).pack(side='left', padx=6)
    ttk.Label(win, text='Ctrl/Shift 多选；筛选变化会清除旧选择。公式等只读位置不能单独编辑。').pack(anchor='w', padx=12, pady=5)
    area = ttk.Frame(win)
    area.pack(fill='both', expand=True, padx=12)
    tree = ttk.Treeview(area, columns=('file', 'position', 'value'), show='headings', selectmode='extended')
    for key, name, width in [('file', '文件', 200), ('position', '位置 / 状态', 270), ('value', '原值（JSON）', 380)]:
        tree.heading(key, text=name)
        tree.column(key, width=width)
    scroll = ttk.Scrollbar(area, command=tree.yview)
    tree.configure(yscrollcommand=scroll.set)
    tree.pack(side='left', fill='both', expand=True)
    scroll.pack(side='right', fill='y')
    range_anchor = _bind_shift_range(tree)
    detail = tk.Text(win, height=4, wrap='word', state='disabled')
    detail.pack(fill='x', padx=12, pady=6)
    status = tk.StringVar(win)
    ttk.Label(win, textvariable=status).pack(anchor='w', padx=12)
    visible = {}

    def set_detail(text=''):
        detail.configure(state='normal')
        detail.delete('1.0', 'end')
        detail.insert('1.0', text)
        detail.configure(state='disabled')

    def filter_rows(*_args):
        tree.delete(*tree.get_children())
        range_anchor['item'] = None
        visible.clear()
        set_detail()
        try:
            matches = find_rows(catalog, query.get())
        except ValueError as exc:
            status.set(str(exc))
            return
        for n, (fi, ei) in enumerate(matches):
            file, row = catalog['files'][fi], catalog['files'][fi]['entries'][ei]
            key = str(n)
            visible[key] = (fi, ei)
            tree.insert('', 'end', iid=key, values=(f'{fi + 1}. {Path(file["path"]).name}',
                location_label(row['at']) + (' [只读]' if row['editable'] is not True else ''),
                json.dumps(row['value'], ensure_ascii=False)))
        status.set(f'匹配 {len(matches)} 个位置；请明确选中要接入的行。' if matches else '未找到匹配位置；原范围和任务说明保持不变。')

    def show_detail(_event=None):
        pairs = [visible[node] for node in tree.selection() if node in visible]
        lines = []
        for fi, ei in pairs:
            file, row = catalog['files'][fi], catalog['files'][fi]['entries'][ei]
            lines.append(file['path'] + '\n' + location_label(row['at']) + '\n原值：' + json.dumps(row['value'], ensure_ascii=False))
        set_detail('\n\n'.join(lines))

    consumed = False
    def accept():
        nonlocal consumed
        if consumed:
            return
        try:
            selection = selection_from_indices(catalog, [visible[n] for n in tree.selection() if n in visible])
            selection = checked_selection(verified, selection)
        except (ValueError, KeyError, TypeError) as exc:
            status.set(str(exc))
            return
        consumed = True
        close()
        on_selected(selection)

    query.trace_add('write', filter_rows)
    tree.bind('<<TreeviewSelect>>', show_detail)
    actions = ttk.Frame(win)
    actions.pack(fill='x', padx=12, pady=12)
    ttk.Button(actions, text='核验并接入选中范围', command=accept).pack(side='right')
    ttk.Button(actions, text='取消', command=close).pack(side='right', padx=8)
    filter_rows()
    return win
