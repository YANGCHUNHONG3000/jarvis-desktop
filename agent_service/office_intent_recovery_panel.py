"""Explicit, revision-bound continuation of session-local Office questions."""
from copy import deepcopy
from pathlib import Path

from .computer import ComputerError, ComputerSession, ROOT

ISSUES = {'ambiguous': '指令有歧义', 'missing_target': '没有找到可核验的目标',
          'out_of_scope': '当前编辑范围不足或超限'}


class OfficeIntentRecovery:
    def __init__(self, goal):
        self.goal, self.active, self.binding = goal, None, None

    def idle(self):
        g = self.goal
        return not (g.running or g.closed or g.external_busy())

    def context(self):
        g = self.goal
        return deepcopy({'inputs': g._generated_scope_context(),
                         'session': str(g.session.root) if g.session else None})

    def current(self, context, *, initial_session=False):
        if not self.idle() or self.goal.stopped.is_set():
            return False
        now = self.context()
        if initial_session and context['session'] is None:
            now['session'] = None
        return now == context

    def _guard(self):
        provider = getattr(self.goal, 'office_planning_context', None)
        return deepcopy(provider()) if callable(provider) else None

    def _decorate(self, session, result, context, guard):
        from .office_clarification import load
        return {**result, '_office_guard': guard, '_intent_context': context,
                '_office_continuation': load(session, result),
                '_intent_session': str(session.root)}

    def accepts(self, result):
        return (self.current(result['_intent_context'], initial_session=True)
                and self.goal.session is not None
                and str(self.goal.session.root) == result['_intent_session'])

    def propose(self):
        g = self.goal
        if not self.idle():
            return
        if self.active and self.binding and self.context() == self.binding:
            return self.open_answer()
        instruction = g.text.get('1.0', 'end-1c').strip()
        files = tuple(g.office_planning_files)
        if not files or not instruction:
            g.status.set('请先接入 Word/Excel 成果并填写自然语言任务说明。')
            return
        from .office_clarification import begin
        refs, scope = deepcopy(g.office_artifact_refs), deepcopy(g.office_scope)
        g.invalidate()
        g.stopped.clear()
        context, guard = self.context(), self._guard()
        self.active = self.binding = None
        def work(session):
            result = begin(session, instruction, files, artifact_refs=refs, scope=scope,
                           stop=g.stopped.is_set)
            return self._decorate(session, result, context, guard)
        g.start_instruction('propose', work, g.draft_ticket())

    def on_result(self, result):
        self.active = deepcopy(result['_office_continuation'])
        self.binding = self.context()
        if self.active['state'] == 'pending':
            self.describe()
            self.open_answer()
        else:
            self.active = self.binding = None

    def describe(self):
        g, item = self.goal, self.active
        g.show_text(ISSUES.get(item['issue'], '需要补充说明') + '：' + item['question']
                    + '\n原要求：' + item['original_instruction']
                    + '\n原会话与来源已保留。补充后只继续规划，不重做已完成的办公步骤。')
        g.status.set('等待确认：补充明确位置或选择原文件内的范围；尚未执行修改。')

    def open_answer(self):
        if not self.active:
            return self.choose_history()
        if not self.binding or not self.current(self.binding):
            self.goal.status.set('当前草稿、文件或范围已变化；新输入保留，请从空白页找回原澄清。')
            return False
        return open_question(self, deepcopy(self.active), deepcopy(self.binding))

    def submit(self, item, context, answer, selection=None):
        g = self.goal
        if self.active != item or not self.current(context):
            g.status.set('问题或输入已变化，未提交；当前草稿保留。')
            return False
        from .office_clarification import compose_instruction, continue_attempt, validate_answer
        try:
            instruction = compose_instruction(item, answer)
            validate_answer(item, answer, selection)
        except (ValueError, KeyError, TypeError):
            g.status.set('补充或范围尚不有效：请填写明确位置/新值并选择1–120个可编辑位置；'
                         '公式只读，补充最多600字，合计1000字。当前输入未改。')
            return False
        selected = deepcopy(selection)
        # Adopt only after the modal's exact revision/context has been checked.
        if selected is not None:
            g.accept_scope(selected)
        g.instruction_draft.adopt(instruction)
        g.text.edit_modified(False)
        g.invalidate()
        g.stopped.clear()
        current, guard = self.context(), self._guard()
        self.active = self.binding = None
        def work(session):
            result = continue_attempt(session, item, answer, selection=selected,
                                      stop=g.stopped.is_set)
            return self._decorate(session, result, current, guard)
        g.start_instruction('propose', work, g.draft_ticket())
        return True

    def failure(self, code):
        if code and ('source' in code or 'handoff' in code or 'record_changed' in code):
            detail = '原文件或来源回执已变化，不能沿用旧位置；请核对原文件，旧问题和失败记录保留。'
        elif code and ('budget' in code or 'call_limit' in code):
            detail = '模型预算或会话调用次数已用尽，未解除预留或切换账本；请保留当前记录。'
        elif code and ('claimed' in code or 'resolved' in code):
            detail = '这条补充已提交，不能重复调用；请找回后续记录。'
        else:
            detail = '本次未生成可执行差异；已有成果和本机记录保留，不自动重试未知调用。'
        self.goal.status.set(detail + (' 原因：'+code if code else ' 已停止。'))

    def choose_history(self):
        if not self.idle():
            return False
        g = self.goal
        g.stopped.clear()
        context = self.context()
        def work(_):
            from .office_clarification import recent
            items = []
            base = ROOT/'data/computer_sessions'
            roots = sorted(base.glob('computer-*'), key=lambda p:p.stat().st_mtime_ns, reverse=True)[:40]
            for root in roots:
                if g.stopped.is_set():
                    raise ComputerError('office_clarification_cancelled')
                try:
                    session = ComputerSession(root)
                    items.extend({'session':str(root), 'item':item} for item in recent(session, limit=8))
                except (ValueError, OSError, KeyError, TypeError):
                    continue
                if len(items) >= 20:
                    break
            return {'items':items[:20], 'context':context}
        g.start('office_pending_history', work)
        return True

    def restore(self, choice, context):
        g = self.goal
        if not self.current(context, initial_session=True):
            g.status.set('当前输入已变化，未载入旧问题。')
            return False
        item = choice['item']
        current_text = g.text.get('1.0', 'end-1c')
        if (g.plan is not None or current_text.strip() and current_text != item['instruction']
                or g.office_planning_files and list(g.office_planning_files) != list(item['files'])):
            g.status.set('当前有其他任务或草稿；请在空白连续任务页找回，当前内容保留。')
            return False
        def work(_):
            from .office_clarification import load
            session = ComputerSession(choice['session'], host=g.host)
            return {'item':load(session, item), 'session':str(session.root), 'context':context}
        g.start('office_pending_restore', work)
        return True

    def handle(self, kind, result, error):
        if kind not in {'office_pending_history','office_pending_restore'}:
            return False
        g = self.goal
        if error or g.stopped.is_set():
            g.status.set('澄清未载入：'+(error or '已停止')+'；原记录和当前输入保留。')
            return True
        if not self.current(result['context'], initial_session=True):
            g.status.set('输入已变化，迟到的澄清记录未接入。')
            return True
        if kind == 'office_pending_history':
            if not result['items']:
                g.status.set('没有本轮可继续的编辑澄清；旧版记录仍可从原工作区入口找回。')
            else:
                open_history(g.root,result['items'],lambda choice:self.restore(choice,result['context']))
            return True
        item = result['item']
        if item['state'] != 'pending':
            g.status.set('该问题已提交或已完成；未重复规划，请找回其后续记录。')
            return True
        from .computer_goal_panel import MODES
        g.session = ComputerSession(result['session'],host=g.host)
        g.mode.set(MODES[9])
        g.mode_changed()
        g.office_planning_files = tuple(item['files'])
        g.office_artifact_refs, g.office_scope = deepcopy(item['artifact_refs']),deepcopy(item['scope'])
        g.instruction_draft.adopt(item['instruction'])
        g.text.edit_modified(False)
        self.active, self.binding = item,self.context()
        self.describe()
        self.open_answer()
        return True


def open_question(controller, item, context):
    import tkinter as tk
    from tkinter import ttk
    from .office_scope_panel import _modal, open_scope_selector
    g = controller.goal
    win, close = _modal(g.root, '明确编辑要求并继续原任务', '780x470')
    body = tk.Text(win,height=10,wrap='word')
    body.pack(fill='both',expand=True,padx=12,pady=10)
    body.insert('1.0', ISSUES.get(item['issue'],'需要明确')+'：'+item['question']
                +'\n\n原任务：'+item['original_instruction']+'\n当前要求：'+item['instruction']
                +'\n\n来源：\n'+'\n'.join(Path(p).name for p in item['files']))
    body.configure(state='disabled')
    ttk.Label(win,text='补充位置或新值；确认后复用原快照继续规划，核对差异后才修改。').pack(anchor='w',padx=12)
    answer=tk.StringVar(win)
    entry=ttk.Entry(win,textvariable=answer)
    entry.pack(fill='x',padx=12,pady=8)
    selected={'value':None}
    label=tk.StringVar(win,value='沿用原范围；如需调整，请明确选择原文件中的位置（公式只读）。')
    ttk.Label(win,textvariable=label,wraplength=740).pack(anchor='w',padx=12)
    consumed=False
    def accept():
        nonlocal consumed
        if not consumed and controller.submit(item,context,answer.get(),selected['value']):
            consumed=True
            close()
    def scope():
        if consumed or not controller.current(context):
            label.set('当前输入已变化，请关闭后重新找回。')
            return
        from .office_clarification import catalog
        try:
            source=catalog(g.session,item)
        except (OSError,ValueError,KeyError,TypeError) as exc:
            label.set('原来源核验失败，未改变范围：'+str(exc))
            return
        def apply(selection):
            if not consumed and controller.current(context):
                selected['value']=deepcopy(selection)
                label.set('待确认范围：'+'；'.join(Path(s['path']).name+f" · {len(s['locations'])}处" for s in selection['scope']))
                win.lift()
                win.grab_set()
        open_scope_selector(win,source,apply)
    row=ttk.Frame(win)
    row.pack(fill='x',padx=12,pady=12)
    ttk.Button(row,text='明确选择文件内范围…',command=scope).pack(side='left')
    ttk.Button(row,text='稍后继续（已保存）',command=close).pack(side='left',padx=6)
    ttk.Button(row,text='确认补充并继续规划',command=accept).pack(side='right')
    entry.focus_set()
    return win


def open_history(root, items, restore):
    from tkinter import ttk
    from .office_scope_panel import _modal
    win,close=_modal(root,'找回待澄清的办公任务','760x360')
    ttk.Label(win,text='原任务与来源保留，找回不调用模型；确认补充后才继续规划。').pack(padx=10,pady=10)
    tree=ttk.Treeview(win,columns=('reason','task'),show='headings',selectmode='browse')
    tree.heading('reason',text='原因'); tree.heading('task',text='原任务')
    tree.column('reason',width=160); tree.column('task',width=550)
    tree.pack(fill='both',expand=True,padx=10)
    for n,choice in enumerate(items):
        item=choice['item']
        tree.insert('','end',iid=str(n),values=(ISSUES.get(item['issue'],'待补充'),item['instruction']))
    consumed=False
    def accept():
        nonlocal consumed
        selected=tree.selection()
        if not consumed and len(selected)==1 and restore(items[int(selected[0])]):
            consumed=True
            close()
    ttk.Button(win,text='核验并继续这项任务',command=accept).pack(pady=10)
    return win
