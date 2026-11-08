"""Explicit Office question/answer recovery; never grants execution permission."""
from copy import deepcopy
from pathlib import Path

from . import office_edit_clarification as records


class OfficeClarificationPanel:
    def __init__(self, owner, *, store=records.DEFAULT_STORE):
        self.owner, self.store, self.active = owner, store, None

    def idle(self):
        o = self.owner
        return not (o.controller.busy() or o.controller.pending or o.preparation.pending)

    def on_result(self, result, _ticket, guard):
        o = self.owner
        if (result.get('office_result') or {}).get('kind') != 'clarify':
            self.active = None
            return
        if not self.idle() or o.context() != guard:
            o.goal.status.set('任务输入已变化，旧澄清问题未接入。')
            return
        try:
            self.active = records.capture(self.store, o.goal.session, result, guard)
        except (OSError, ValueError, KeyError, TypeError):
            o.goal.status.set('澄清问题未能完整保存；原指令保留，请重新核对输入。')
            return
        o.goal.show_text('需要补充编辑要求：' + self.active['question']
                         + '\n原任务、文件及范围已保存；补充后重新规划。')
        self.open_answer()

    def open_answer(self):
        o = self.owner
        recovery = getattr(o.goal, 'intent_recovery', None)
        if recovery is not None and recovery.active is not None:
            return recovery.open_answer()
        if not self.idle() or not self.active:
            o.goal.status.set('请先取得编辑澄清问题，或从“找回编辑澄清”恢复。')
            return False
        current = o.context()
        try:
            question = records.load(self.store, self.active['id'])
            records.subset(question['original'], current['payload'])
            if current['unavailable'] or current['payload']['draft'] != question['original']['draft']:
                raise ValueError('new_input')
        except (OSError, ValueError, KeyError, TypeError):
            o.goal.status.set('当前草稿、文件或范围与原问题不符；新输入保留，请重新规划。')
            return False
        open_question(o.goal.root, question, current['payload'],
                      lambda answer: self.submit(question, current, answer), o.narrow_inputs)
        return True

    def submit(self, question, context, answer):
        o = self.owner
        if not self.idle() or not o.preparation.current(context):
            o.goal.status.set('输入已变化或任务正忙，补充说明未接入；原输入保留。')
            return False
        try:
            payload = records.compose(question, context['payload'], answer)
        except (OSError, ValueError, KeyError, TypeError):
            o.goal.status.set('补充说明须为有效单行文本（最多600字，合计1000字）；文件和范围不能超出原问题。')
            return False
        ticket = {'purpose': 'office_answer', 'context': deepcopy(context),
                  'saved': {'id': None, 'payload': payload},
                  'question': deepcopy(question), 'answer': answer}
        return o.preparation.start(payload['records'], ticket, scope=payload['scope'], select_scope=False)

    def restore(self, record):
        o = self.owner
        if not self.idle():
            o.goal.status.set('请结束当前任务后再找回编辑澄清。')
            return False
        current = o.context()
        try:
            record = records.load(self.store, record['id'])
            payload = deepcopy(record['payload'] if record['kind'] == 'answer' else record['original'])
            existing = current['payload']
            if (current['unavailable'] or (existing['draft'] and existing['draft'] != payload['draft'])
                    or (existing['records'] and existing['records'] != payload['records'])
                    or (existing['scope'] and existing['scope'] != payload['scope'])):
                raise ValueError('new_input')
        except (OSError, ValueError, KeyError, TypeError):
            o.goal.status.set('记录无法核验，或当前已有不同输入；当前内容保留。请在空白连续页找回。')
            return False
        # Only an explicit new restore clears a previous stop signal.
        o.goal.stopped.clear()
        ticket = {'purpose': 'office_clarification_restore', 'context': o.context(),
                  'saved': {'id': None, 'payload': payload}, 'record': record}
        return o.preparation.start(payload['records'], ticket, scope=payload['scope'], select_scope=False)

    def ready(self, verified, ticket):
        o = self.owner
        if not self.idle() or not o.preparation.current(ticket['context']):
            o.goal.status.set('输入已变化，旧补充说明未接入。')
            return False
        try:
            if ticket['purpose'] == 'office_answer':
                record = records.save_answer(self.store, ticket['question'], verified, ticket['answer'])
            else:
                record = records.load(self.store, ticket['record']['id'])
                expected = record['payload'] if record['kind'] == 'answer' else record['original']
                if expected != verified['payload']:
                    raise ValueError('restore_changed')
        except (OSError, ValueError, KeyError, TypeError):
            o.goal.status.set('澄清记录核验或保存失败，原输入保留。')
            return False
        if not o.apply(verified):
            return False
        self.active = record if record['kind'] == 'question' else None
        o.goal.status.set('编辑说明已找回；请核对输入后重新规划，旧批准不会恢复。' if self.active else
                          '补充说明已保存并接回原任务；请核对输入后重新规划，再批准差异。')
        return True

    def choose_history(self):
        if not self.idle():
            self.owner.goal.status.set('任务正忙，请稍后找回。')
            return False
        try:
            items = records.history(self.store)
        except (OSError, ValueError, KeyError, TypeError):
            items = []
        if not items:
            recovery = getattr(self.owner.goal, 'intent_recovery', None)
            if recovery is not None:
                return recovery.choose_history()
            self.owner.goal.status.set('没有可核验的编辑澄清记录；已有文件不变。')
            return False
        # Keep legacy records reachable; new session continuations have their own picker.
        recovery = getattr(self.owner.goal, 'intent_recovery', None)
        open_history(self.owner.goal.root, items, self.restore,
                     recent=recovery.choose_history if recovery is not None else None)
        return True


def open_question(root, question, payload, submit, narrow):
    import tkinter as tk
    from tkinter import ttk
    from .office_scope_panel import _modal
    win, close = _modal(root, '补充编辑要求', '740x420')
    count = sum(len(p['locations']) for p in payload['scope'])
    body = ('原任务：' + question['original']['draft'] + '\n\n需要你明确：' + question['question']
            + f'\n\n当前 {len(payload["records"])} 份文件、{count} 个位置：\n'
            + '\n'.join(Path(p['path']).name for p in payload['records']))
    box = tk.Text(win, height=12, wrap='word')
    box.pack(fill='both', expand=True, padx=12, pady=10)
    box.insert('1.0', body)
    box.configure(state='disabled')
    ttk.Label(win, text='补充一句明确要求；只接回用户说明，不自动执行。').pack(anchor='w', padx=12)
    answer = tk.StringVar(win)
    entry = ttk.Entry(win, textvariable=answer)
    entry.pack(fill='x', padx=12, pady=8)
    consumed = False

    def accept():
        nonlocal consumed
        if not consumed and submit(answer.get()):
            consumed = True
            close()

    def select_range():
        close()
        narrow()

    row = ttk.Frame(win)
    row.pack(fill='x', padx=12, pady=10)
    ttk.Button(row, text='保存补充并接回任务', command=accept).pack(side='right')
    ttk.Button(row, text='定位当前范围…', command=select_range).pack(side='left')
    ttk.Button(row, text='稍后补充', command=close).pack(side='left', padx=6)
    entry.focus_set()
    return win


def open_history(root, items, restore, recent=None):
    from tkinter import ttk
    from .office_scope_panel import _modal
    win, close = _modal(root, '找回编辑澄清', '760x380')
    ttk.Label(win, text='选择本机记录，重新核验文件后找回输入；不会恢复旧计划或批准。').pack(padx=10, pady=10)
    tree = ttk.Treeview(win, columns=('kind', 'task'), show='headings', selectmode='browse')
    tree.heading('kind', text='状态')
    tree.heading('task', text='任务说明')
    tree.column('kind', width=90, stretch=False)
    tree.column('task', width=620)
    tree.pack(fill='both', expand=True, padx=10)
    for n, item in enumerate(items):
        draft = item['payload']['draft'] if item['kind'] == 'answer' else item['original']['draft']
        tree.insert('', 'end', iid=str(n), values=('已补充' if item['kind'] == 'answer' else '待补充', draft))
    consumed = False

    def accept():
        nonlocal consumed
        selected = tree.selection()
        if not consumed and len(selected) == 1 and restore(items[int(selected[0])]):
            consumed = True
            close()

    row = ttk.Frame(win)
    row.pack(fill='x', padx=10, pady=10)
    ttk.Button(row, text='核验并找回', command=accept).pack(side='right')
    ttk.Button(row, text='取消', command=close).pack(side='right', padx=8)
    if recent is not None:
        def new_records():
            close()
            recent()
        ttk.Button(row, text='查看新续接任务…', command=new_records).pack(side='left')
    return win
