"""Save only Office task inputs; restored inputs require fresh planning/approval."""
from copy import deepcopy

from . import office_workspace
from .computer_goal_panel import MODES
from .workspace_resume import WorkspaceResume
from .office_workspace_repair import RepairPreparation, record_failure


class OfficeWorkspacePanel:
    def __init__(self, goal, *, store, task_base, busy, on_busy, widgets=True):
        self.goal = goal
        self.store, self.failure = store, None
        self.repair_button = None
        self.controller = WorkspaceResume(
            context=self.context, busy=lambda: busy() or goal.running or goal.closed,
            on_busy=on_busy, apply=self.apply, status=goal.status.set, store=store,
            task_base=task_base, backend=office_workspace,
            thread_name='office-workspace-restore', on_failure=self.failed)
        self.preparation = RepairPreparation(
            context=self.context, busy=self.controller.busy, on_busy=on_busy,
            status=goal.status.set, ready=self.repair_ready, task_base=task_base)
        from .office_clarification_panel import OfficeClarificationPanel
        self.clarifications = OfficeClarificationPanel(self)
        goal.office_planning_context = self.context
        goal.on_office_planning_result = self.clarifications.on_result
        if widgets:
            from tkinter import ttk
            recovery_button = getattr(goal, 'intent_clarification_button', None)
            if recovery_button is not None:
                recovery_button.pack_forget()
            row = ttk.Frame(goal.tab)
            row.pack(fill='x', pady=3, before=getattr(goal, 'preview_box', None))
            ttk.Button(row, text='保存编辑工作（本机）', command=self.save).pack(side='left')
            ttk.Button(row, text='恢复编辑工作（本机）', command=self.restore).pack(side='left', padx=6)
            self.repair_button = ttk.Button(row, text='修正文件/范围…', command=self.repair_options, state='disabled')
            self.repair_button.pack(side='left')
            ttk.Label(row, text='本机保存；恢复或修正后重新规划批准。',
                      wraplength=260).pack(side='left', padx=4)
            review_row = ttk.Frame(goal.tab)
            review_row.pack(fill='x', pady=3, before=getattr(goal, 'preview_box', None))
            ttk.Button(review_row, text='核对输入再规划…', command=self.review_inputs).pack(side='left')
            ttk.Button(review_row, text='定位并缩小范围…', command=self.narrow_inputs).pack(side='left', padx=6)
            ttk.Button(review_row, text='补充编辑要求…', command=self.clarifications.open_answer).pack(side='left')
            ttk.Button(review_row, text='找回编辑澄清…', command=self.clarifications.choose_history).pack(side='left', padx=6)

    def narrow_inputs(self):
        if self.controller.busy() or self.controller.pending or self.preparation.pending:
            self.goal.status.set('任务正忙，请结束当前操作后再定位范围。')
            return False
        current = self.context()
        payload = current['payload']
        if current['unavailable'] or self.goal.mode.get() != MODES[9] or not payload['records']:
            self.goal.status.set(current['blocked_reason'] or '请先接入要编辑的办公成果。')
            return False
        if not payload['scope']:
            self.goal.status.set('请先选择编辑范围；定位只在已有范围内进行。')
            return False
        ticket = {'purpose': 'scope_narrow', 'context': deepcopy(current),
                  'saved': {'id': self.controller.expected_id, 'payload': deepcopy(payload)}}
        return self.preparation.start(payload['records'], ticket, scope=payload['scope'], select_scope=False)

    def narrow_ready(self, verified, ticket):
        from .office_scope_narrow import checked_selection, open_selector
        consumed = False

        def selected(selection):
            nonlocal consumed
            if (consumed or not self.preparation.current(ticket['context']) or self.controller.busy()
                    or self.preparation.pending or self.controller.pending):
                self.goal.status.set('任务说明、来源或范围已变化；请重新定位，原输入保留。')
                return False
            try:
                selection = checked_selection(verified, selection)
            except (ValueError, KeyError, TypeError):
                self.goal.status.set('所选位置不是原范围的有效子集；原输入保留。')
                return False
            consumed = True
            records = [{'path': part['path'], 'sha256': part['sha256']} for part in selection['scope']]
            continuation = {**deepcopy(ticket), 'purpose': 'scope_narrow_apply'}
            return self.preparation.start(records, continuation, scope=selection['scope'], select_scope=False)

        if not self.preparation.current(ticket['context']) or self.controller.busy():
            self.goal.status.set('草稿或来源已变化，旧范围未打开。')
            return False
        self.goal.status.set('在当前范围内按原值或位置查找，再选择要接入的行；不调用模型。')
        open_selector(self.goal.root, verified, selected)
        return True

    def review_inputs(self):
        if self.controller.busy() or self.controller.pending or self.preparation.pending:
            self.goal.status.set('任务正忙，请结束当前操作后再核对输入。')
            return False
        current = self.context()
        payload = current['payload']
        if current['unavailable'] or self.goal.mode.get() != MODES[9]:
            self.goal.status.set(current['blocked_reason'] or '请先接入要编辑的办公成果。')
            return False
        if not payload['scope']:
            self.goal.status.set('请先选择编辑范围，再核对输入；不会自动扩大到整个文件。')
            return False
        from .office_input_review import validate_instruction
        try:
            validate_instruction(payload['draft'])
        except ValueError:
            self.goal.status.set('请填写1至1000字的单行修改说明，不含换行或控制字符，再核对输入。')
            return False
        ticket = {'purpose': 'input_review', 'context': deepcopy(current),
                  'saved': {'id': None, 'payload': deepcopy(payload)}}
        return self.preparation.start(payload['records'], ticket, scope=payload['scope'], select_scope=False)

    def review_ready(self, verified, ticket):
        from .office_input_review import open_review, validate_for_planning
        try:
            validate_for_planning(verified)
        except (ValueError, KeyError, TypeError):
            self.goal.status.set('所选范围与任务说明无法满足规划限制；请缩小范围或简化说明，再核对输入。')
            return False
        consumed = False
        def proceed():
            nonlocal consumed
            if (consumed or not self.preparation.current(ticket['context']) or self.controller.busy()
                    or self.preparation.pending or self.controller.pending):
                self.goal.status.set('任务说明、来源或范围已变化；请重新核对，未调用模型。')
                return False
            consumed = True
            self.goal.propose()
            return True
        self.goal.status.set('本地核对已准备；确认后再云端规划，取消保留当前输入。')
        open_review(self.goal.root, verified, proceed)

    def failed(self, saved, error, context):
        message = office_workspace.describe_error(error)
        self.failure = {'saved': deepcopy(saved), 'context': deepcopy(context), 'message': message}
        if self.repair_button is not None:
            self.repair_button.configure(state='normal')
        try:
            record_failure(self.store, saved['id'], error)
        except (OSError, ValueError):
            message += ' 本机失败回执未能保存，旧工作快照仍保留。'
        return message + ' 点击“修正文件/范围”继续，原任务说明保留。'

    def repair_options(self):
        if self.failure is None or self.controller.busy() or self.preparation.pending:
            self.goal.status.set('请先恢复并核验保存的编辑工作，再修正失败项。')
            return False
        ticket = deepcopy(self.failure)
        current = self.context()
        # An explicit repair may retain a newly typed instruction, but cannot
        # silently replace new files, scope, mode or an execution plan.
        old, new = deepcopy(ticket['context']), deepcopy(current)
        for value in (old, new):
            value.pop('revision', None)
            value['payload'].pop('draft', None)
        if current['unavailable'] or new != old:
            self.goal.status.set('模式、文件或范围已变化；当前输入保留，请重新核验后修正。')
            return False
        if (current['payload']['draft']
                or current['revision'] != ticket['context']['revision']):
            ticket['saved']['payload']['draft'] = current['payload']['draft']
        ticket['context'] = deepcopy(current)
        from tkinter import ttk
        from .office_scope_panel import _modal, open_file_selector
        win, close = _modal(self.goal.root, '修正文件与编辑范围', '680x220')
        ttk.Label(win, text=ticket['message'] + '\n核验后接入保存的任务说明；你新写或清空的内容优先保留。',
                  wraplength=640).pack(anchor='w', padx=12, pady=12)

        def ranges():
            close()
            self.preparation.start(ticket['saved']['payload']['records'], ticket)

        def files():
            records, seen = [], set()
            for item in list(ticket['saved']['payload']['records']) + list(self.goal.get_artifacts()):
                if type(item) is not dict or set(item) != {'path', 'sha256'}:
                    continue
                key = str(item['path']).casefold()
                if key not in seen:
                    records.append(deepcopy(item))
                    seen.add(key)
            close()
            open_file_selector(self.goal.root, records[:32],
                               lambda selected: self.preparation.start(selected, ticket))

        ttk.Button(win, text='沿用文件，重新选范围', command=ranges).pack(anchor='w', padx=12, pady=4)
        ttk.Button(win, text='从保存文件和当前成果重新选文件…', command=files).pack(anchor='w', padx=12, pady=4)
        ttk.Button(win, text='取消', command=close).pack(anchor='e', padx=12, pady=4)
        return True

    def repair_ready(self, result, ticket):
        if not self.preparation.current(ticket['context']) or self.controller.busy():
            self.goal.status.set('草稿或来源已变化，修正结果未接入。')
            return
        if ticket.get('purpose') == 'input_review':
            self.review_ready(result, ticket)
            return
        if ticket.get('purpose') in {'office_answer', 'office_clarification_restore'}:
            self.clarifications.ready(result, ticket)
            return
        if ticket.get('purpose') == 'scope_narrow':
            self.narrow_ready(result, ticket)
            return
        if ticket.get('purpose') == 'scope_narrow_apply':
            if self.controller.pending or self.preparation.pending:
                self.goal.status.set('仍有其他核验正在进行，旧范围未接入。')
                return
            if self.apply(result):
                self.goal.status.set('已接入缩小后的范围，原任务说明保留；核对后再规划，尚未保存或执行。')
            return
        if 'payload' in result:
            if self.apply(result):
                self.controller.expected_id = ticket['saved']['id']
                self.failure = None
                if self.repair_button is not None:
                    self.repair_button.configure(state='disabled')
                self.goal.status.set('文件与范围已修正，任务说明已保留；请保存或继续规划，再批准执行。')
            return
        from .office_scope_panel import open_scope_selector

        def selected(selection):
            records = [{'path': part['path'], 'sha256': part['sha256']} for part in selection['scope']]
            self.preparation.start(records, ticket, scope=selection['scope'], select_scope=False)

        self.goal.status.set('文件核验通过，请重新选择编辑范围；取消不会替换当前草稿。')
        open_scope_selector(self.goal.root, result, selected)

    def context(self):
        panel = self.goal
        files = tuple(panel.office_planning_files)
        refs = deepcopy(panel.office_artifact_refs)
        records = [{'path': path, 'sha256': refs[n].get('sha256')
                    if type(refs) is list and n < len(refs) and type(refs[n]) is dict else None}
                   for n, path in enumerate(files)]
        unrelated = {name: deepcopy(getattr(panel, name, None)) for name in (
            'interaction_request', 'interaction_fields', 'comparison_goal', 'comparison_fields',
            'comparison_source', 'capability_history_seed', 'human_pending')}
        controls = {name: getattr(panel, name).get() for name in ('source', 'destination', 'expression', 'control')}
        ticket = panel.instruction_draft.snapshot()
        reason = ''
        if panel.plan is not None:
            reason = '当前已有目标预览；请先处理当前计划，恢复不会替换执行计划。'
        elif any(unrelated.values()) or panel.snapshots or any(controls.values()):
            reason = '连续页还包含其他操作的输入；当前只保存和恢复办公编辑输入。'
        elif (panel.mode.get() not in (MODES[0], MODES[9])
              or (panel.mode.get() != MODES[9] and (ticket['text'] or files or panel.office_scope))):
            reason = '当前不是空白连续页或自然语言编辑模式；原有内容已保留。'
        elif files and (type(refs) is not list or len(refs) != len(files)
                        or any(type(ref) is not dict or ref.get('artifact') != path
                               for path, ref in zip(files, refs))):
            reason = '所选文件尚无完整的成果来源证明；请通过上一任务成果接入。'
        elif not files and (refs or panel.office_scope):
            reason = '文件与范围绑定不完整；请重新选择成果和范围。'
        return deepcopy({
            'payload': {'draft': ticket['text'], 'records': records, 'scope': panel.office_scope},
            'revision': ticket,
            'binding': {'mode': panel.mode.get(), 'refs': refs, 'plan': panel.plan,
                        'unrelated': unrelated, 'controls': controls, 'snapshots': panel.snapshots,
                        'stopped': panel.stopped.is_set(),
                        'route_generation': panel.daily_task_route_generation,
                        'source_generation': panel.capability_source_generation},
            'unavailable': bool(reason), 'blocked_reason': reason})

    def save(self):
        if not self.goal.office_planning_files:
            self.goal.status.set('请先接入要编辑的 Word/Excel 成果，再保存工作。')
            return False
        return self.controller.save()

    def restore(self):
        if not (self.goal.running or self.goal.closed or self.goal.external_busy()):
            # An explicit new preparation starts with a fresh stop signal.
            # A stop during its worker is captured and rejects the late result.
            self.goal.stopped.clear()
        return self.controller.restore()

    def apply(self, verified):
        panel = self.goal
        if panel.running or panel.closed or panel.external_busy():
            return False
        payload, refs = deepcopy(verified['payload']), deepcopy(verified['refs'])
        panel.mode.set(MODES[9])
        panel.mode_changed(refresh_sources=False)
        panel.office_planning_files = tuple(record['path'] for record in payload['records'])
        panel.office_artifact_refs = refs
        panel.office_scope = payload['scope']
        panel.instruction_draft.adopt(payload['draft'])
        panel.text.edit_modified(False)
        panel.invalidate()
        count = sum(len(part['locations']) for part in payload['scope'] or [])
        panel.show_text(f'已恢复 {len(refs)} 份办公成果、{count} 个选中位置及任务说明。\n'
                        '尚无执行计划；请核对范围，再云端规划并重新批准。')
        return True

    def poll(self):
        result = self.controller.poll()
        self.preparation.poll()
        return result

    def close(self):
        self.preparation.close()
        return self.controller.close()
