"""Verified chain outputs, with explicit Office-only next-task selection."""
from copy import deepcopy
from pathlib import Path

from . import office_task_chain as core, workspace_state


def delivery_records(state):
    offices = [(entry['index'], record) for entry in state.get('completed_office', [])
               for record in entry['records']]
    drafts = [(entry['index'], record) for entry in state.get('deliveries', [])
              for record in entry['records'] if Path(record['path']).suffix.lower() == '.eml']
    return sorted(offices + drafts, key=lambda item: item[0])


def _office_selection(state, selected):
    if type(selected) is not list or not 1 <= len(selected) <= 4:
        raise ValueError('office_chain_delivery_selection_invalid')
    allowed = delivery_records(state)
    records = []
    for choice in selected:
        if (type(choice) not in (tuple, list) or len(choice) != 2 or type(choice[0]) is not int
                or type(choice[1]) is not dict or (choice[0], choice[1]) not in allowed
                or Path(choice[1]['path']).suffix.lower() not in {'.docx', '.xlsx'}):
            raise ValueError('office_chain_delivery_selection_invalid')
        records.append(deepcopy(choice[1]))
    if len({item['path'].casefold() for item in records}) != len(records):
        raise ValueError('office_chain_delivery_selection_invalid')
    return records


class OfficeChainDelivery:
    def __init__(self, panel, *, open_result):
        self.panel, self.open_result = panel, open_result

    def _current(self, ident):
        return self.panel.active is not None and self.panel.active['id'] == ident

    def show(self):
        p = self.panel
        if p.active is None:
            p.status('请先找回任务清单，再查看其中已完成的成果。')
            return False
        ident = p.active['id']
        return p._begin(lambda: core.inspect(p.store, ident),
                        complete=lambda state, _: self._dialog(state), retryable=True)

    def open(self, ident, index, record):
        p = self.panel
        if not self._current(ident) or type(index) is not int:
            return False
        selected = deepcopy(record)

        def complete(state, _):
            if (not self._current(ident) or state['id'] != ident
                    or (index, selected) not in delivery_records(state)):
                p.status('所选成果已不属于当前核验的清单；未打开。')
                return
            if self.open_result(selected):
                p.status('已打开核验成果；草稿未发送，任务输入未改变。')
                return True
            return False

        return p._begin(lambda: core.inspect(p.store, ident), complete=complete, retryable=True)

    def use_office(self, ident, selected):
        p = self.panel
        if not self._current(ident) or p.active['status'] != 'complete':
            p.status('请先完成当前清单；中途不能用成果替换既定任务。')
            return False
        before = deepcopy(p.context())
        if (before['workspace']['unavailable'] or before['goal']['draft']['text']
                or before['goal']['plan'] is not None):
            p.status('资料尚未就绪或连续页已有指令/预览；请先处理，当前内容保留。')
            return False
        try:
            records = _office_selection(p.active, selected)
        except (ValueError, KeyError, TypeError):
            p.status('请选择1至4份不同的Word/Excel成果；草稿只能预览，不能混入下一任务。')
            return False
        selected = deepcopy(selected)
        payload = {'draft': before['workspace']['payload']['draft'], 'records': records,
                   'source': records[0] if len(records) == 1 else None}
        verified = {}

        def operation():
            verified.clear()
            state = core.inspect(p.store, ident)
            if state['status'] != 'complete' or _office_selection(state, selected) != records:
                raise ValueError('office_chain_delivery_selection_invalid')
            verified['value'] = workspace_state.verify(payload, p.task_base)
            return state

        def complete(state, _):
            if (not self._current(ident) or state['id'] != ident or state['status'] != 'complete'
                    or verified.get('value', {}).get('payload') != payload):
                p.status('清单或选择已变化，未接入下一任务。')
                return
            if not p.apply(verified['value']):
                p.status('所选成果已核验，但当前无法接入；当前内容保留。')
                return False
            p.set_scope('当前选中资料' if len(records) == 1 else '当前成果批次')
            p.active = state
            p.issued = p.cleared = p.handoff = p.route_context = None
            p.status(f'已接入{len(records)}份办公成果，保留当前指令；请核对后规划下一任务。')
            return True

        return p._begin(operation, complete=complete, retryable=True)

    def _dialog(self, state):
        p = self.panel
        if not self._current(state['id']):
            p.status('当前清单已变化，请重新查看成果。')
            return False
        choices = delivery_records(state)
        if not choices:
            p.status('这份清单尚无已完成并核验的成果。')
            return False
        import tkinter as tk
        from tkinter import ttk
        from .office_scope_panel import _bind_shift_range
        win = tk.Toplevel(p.root)
        win.title('任务清单 · 全部成果')
        win.geometry('710x360')
        ttk.Label(win, text='选择文件可核验后打开；清单完成后，最多4份办公成果可用于下一任务。草稿只供预览，未发送。',
                  wraplength=680).pack(anchor='w', padx=12, pady=10)
        tree = ttk.Treeview(win, columns=('step', 'kind', 'file', 'use'), show='headings', selectmode='extended', height=8)
        _bind_shift_range(tree)
        for key, title, width in [('step', '步骤', 45), ('kind', '类型', 65), ('file', '文件', 350), ('use', '用途', 170)]:
            tree.heading(key, text=title)
            tree.column(key, width=width)
        tree.pack(fill='both', expand=True, padx=12)
        defaults = []
        for n, (index, record) in enumerate(choices):
            path = Path(record['path'])
            kind = {'.docx': 'Word', '.xlsx': 'Excel', '.eml': '邮件草稿'}[path.suffix.lower()]
            tree.insert('', 'end', iid=str(n), values=(index+1, kind, path.name,
                        '仅预览（未发送）' if kind == '邮件草稿' else '可用于下一任务'))
            if record in state['payload']['records']:
                defaults.append(str(n))
        tree.selection_set(defaults)
        generation, epoch = p.generation, p.resume_epoch

        def act(use):
            if (p.generation != generation or p.resume_epoch != epoch or p.closed
                    or not self._current(state['id'])):
                p.status('成果窗口已过期，请重新查看；当前内容保留。')
                return
            selected = [choices[int(n)] for n in tree.selection()]
            if use:
                started = self.use_office(state['id'], selected)
            elif len(selected) == 1:
                started = self.open(state['id'], *selected[0])
            else:
                p.status('请仅选择一份成果打开；多个办公文件可一起用于下一任务。')
                return
            if started:
                win.destroy()

        row = ttk.Frame(win)
        row.pack(anchor='e', padx=12, pady=10)
        ttk.Button(row, text='核验并打开', command=lambda: act(False)).pack(side='left', padx=6)
        ttk.Button(row, text='所选办公文件用于下一任务', command=lambda: act(True),
                   state='normal' if state['status'] == 'complete' else 'disabled').pack(side='left')
        return True
