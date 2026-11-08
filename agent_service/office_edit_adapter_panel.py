"""Thin UI for the reviewed copied-Office editing profile."""
import re
from copy import deepcopy
from . import office_edit_adapter as adapter


def is_edit_plan(plan):
    goal = (plan or {}).get('goal', {})
    return (plan or {}).get('mode') == 'agent' and goal.get('kind') == 'interactive' and bool(goal.get('edits')) and not goal.get('browser')


def describe_state(state):
    return ('保留格式的 Word/Excel 编辑适配器\n'
            f"离线检查：{'通过' if state['offline'] else '待执行'}；独立审查：{'通过' if state['reviewed'] else '待维护者审查'}\n"
            f"实机试运行：{'通过' if state['trial_passed'] else '待完成'}；可用次数：{state['remaining']}\n"
            '试运行会生成实际编辑副本，已完成步骤不会为了启用而再做一遍。\n'
            'Excel 公式保持，数值在原生应用重算核对；文件内旧缓存不改写。')


def uses_adapter(session, plan):
    # Missing development records must never turn recovery into an ungated run.
    return is_edit_plan(plan)


def capture_instruction(parent, session, plan, ticket):
    """UI-thread draft identity for this exact approved edit and its continuations."""
    from .computer import digest
    tracker = getattr(parent, 'instruction_draft', None)
    if tracker is None or not is_edit_plan(plan):
        return ticket
    session_root = getattr(session, 'root', None)
    if 'recovery' in plan:
        binding = getattr(parent, '_office_edit_instruction', None)
        if (session_root is not None and binding is not None
                and binding['session_root'] == str(session_root)
                and digest(binding['plan']) == digest(plan)
                and tracker.unchanged(binding['ticket'])):
            return dict(binding['ticket'])
        return None
    parent._office_edit_instruction = None
    bound = getattr(parent, 'instruction_ticket', None)
    if (session_root is not None and ticket is not None and callable(bound) and bound(plan) == ticket
            and tracker.unchanged(ticket)):
        parent._office_edit_instruction = {
            'session_root': str(session_root), 'plan': deepcopy(plan), 'ticket': dict(ticket)}
    return ticket


def _continued_instruction(session, plan, binding):
    """Only a verified preview continuing the exact locally submitted plan inherits it."""
    from .computer import digest, read_record
    if binding is None or binding['session_root'] != str(session.root):
        return None
    original = binding['plan']
    saved = session.root / 'goals' / (original['sha256'] + '.json')
    parent_run = session.root / 'goals' / (original['sha256'] + '.run')
    approved_parent = str(parent_run)
    if 'recovery' in original and not parent_run.exists():
        # Re-previewing an unstarted continuation creates a sibling preview.
        # Its exact verified parent remains the only permitted alternative.
        approved_parent = original['recovery']['parent_run']
    if (plan.get('recovery', {}).get('parent_run') != approved_parent
            or plan.get('goal') != original.get('goal')
            or plan.get('instruction') != original.get('instruction')
            or digest(read_record(saved)) != digest(original)):
        return None
    return {'session_root': str(session.root), 'plan': deepcopy(plan),
            'ticket': dict(binding['ticket'])}


def open_panel(parent):
    from tkinter import Toplevel, ttk
    if parent.running or parent.external_busy() or parent.closed:
        return
    from .computer import digest
    session, plan = parent.session, parent.plan
    opened_parent_plan_sha256 = digest(plan)
    historical_preview = not is_edit_plan(plan)
    try:
        current=adapter.current_candidate()
        if (adapter.DEFAULT_STORE/current).is_dir():
            existing=adapter.status(adapter.DEFAULT_STORE,current)
            if existing['reviewed']:
                adapter.reconcile(adapter.DEFAULT_STORE,current)
        if is_edit_plan(plan) and 'recovery' not in plan:
            state = adapter.ensure_candidate(adapter.DEFAULT_STORE, session, plan, proposer='Jarvis desktop maintainer')
        else:
            state = adapter.status(adapter.DEFAULT_STORE,current)
            if not is_edit_plan(plan):
                from .computer import ComputerSession,read_record
                root=adapter.DEFAULT_STORE/state['candidate']
                intents=sorted((p for p in root.iterdir() if re.fullmatch(r'active-[1-9][0-9]*-intent\.json',p.name)),
                               key=lambda p:int(p.name.split('-')[1]))
                intent=read_record(intents[-1] if intents else root/'trial-intent.json')
                session=ComputerSession(intent['session_root'],host=parent.host)
                plan=read_record(session.root/'goals'/(intent['plan_sha256']+'.json'))
                parent.session=session
    except (OSError,ValueError,KeyError) as exc:
        parent.status.set('编辑适配器未就绪：'+str(exc))
        return
    candidate = state['candidate']
    opened_plan_sha256 = digest(plan)
    binding = getattr(parent, 'instruction_ticket', None)
    opened_instruction_ticket = (binding(plan)
        if not historical_preview and callable(binding) else None)
    tracker = getattr(parent, 'instruction_draft', None)
    opened_draft = tracker.snapshot() if tracker is not None else None
    window = Toplevel(parent.root)
    window.title('Jarvis · 保留格式编辑适配器')
    ttk.Label(window, text=describe_state(state), justify='left', padding=12).pack(fill='x')

    def run(kind, operation, instruction_ticket=None):
        if parent.running or parent.external_busy() or parent.closed:
            return
        window.destroy()
        if instruction_ticket is not None:
            parent.start_instruction(kind, lambda _: operation(), instruction_ticket)
        else:
            parent.start(kind, lambda _: operation())

    def current_preview(*, refresh=False):
        expected = (opened_parent_plan_sha256
                    if refresh and historical_preview else opened_plan_sha256)
        if (digest(parent.plan) != expected
                or tracker is not None and not tracker.unchanged(opened_draft)
                or opened_instruction_ticket is not None
                and (not callable(binding) or binding(plan) != opened_instruction_ticket)):
            parent.status.set('指令或差异预览已变化；旧试运行窗口已失效，请重新预览并打开编辑适配器。')
            return False
        return True

    def refresh_preview():
        if not current_preview(refresh=True):
            return
        from copy import deepcopy
        provider = getattr(parent, 'office_planning_context', None)
        guard = deepcopy(provider()) if callable(provider) else None
        run('propose', lambda: {'plan': adapter.refresh_plan(session, plan),
            'reason': '原修改内容已重新核验；请核对差异并批准。',
            '_office_guard': guard}, opened_instruction_ticket)

    def execute_trial():
        if not current_preview():
            return
        pause = bool(parent.pause_each_step.get())
        capture_instruction(parent, session, plan, opened_instruction_ticket)
        run('execute', lambda: adapter.trial(adapter.DEFAULT_STORE,candidate,session,plan,
            approval=plan['sha256'],stop=parent.stopped.is_set,
            progress=lambda p: parent.messages.put(('progress',p,None)),
            pause_after_step=lambda:pause)['outcome'], opened_instruction_ticket)

    def resume_preview():
        if not current_preview(refresh=True):
            return
        # Capture Tk identity before the worker; it must never inspect widgets.
        lineage = deepcopy(getattr(parent, '_office_edit_instruction', None))
        if (lineage is not None and (tracker is None
                or not tracker.unchanged(lineage['ticket']))):
            lineage = None

        def prepare():
            from .computer_goal_panel import _InstructionResult
            result=adapter.prepare_resume(adapter.DEFAULT_STORE,candidate,session)
            response = {'plan':result['plan'],'reason':'已核对原生回执；只执行剩余编辑，请批准新预览。'}
            continued = _continued_instruction(session, result['plan'], lineage)
            if continued is not None:
                parent._office_edit_instruction = continued
                return _InstructionResult(response, continued['ticket'])
            return response

        run('propose', prepare)

    def adoption_request():
        result=adapter.prepare_adoption(adapter.DEFAULT_STORE,candidate,session,plan,
                                        proposer='Jarvis desktop maintainer')
        request=result['request']
        return {'plan':plan,'reason':(
            '历史Office编辑接管审阅请求已保存；尚未占用执行次数，也未执行编辑。\n'
            f"请求：{result['request_sha256']}\n"
            f"原计划：{request['origin_plan_sha256']}\n"
            f"原证据：{request['parent_evidence_sha256']}\n"
            f"已完成：{', '.join(request['completed_edits']) or '无'}；"
            f"待完成：{', '.join(request['remaining_edits']) or '无'}\n"
            '请另一位维护者独立核对来源、原生回执和候选门禁，再用维护CLI记录该请求的审查。')}

    def bind_reviewed_adoption():
        from .interaction_recovery import prepare
        fresh=prepare(session,plan['recovery']['parent_run'])['plan']
        result=adapter.prepare_adoption(adapter.DEFAULT_STORE,candidate,session,fresh,
                                        proposer='Jarvis desktop maintainer')
        bound=adapter.adopt(adapter.DEFAULT_STORE,candidate,session,fresh,result['request_sha256'])
        return {'plan':fresh,'reason':(
            f"已将历史任务绑定至当前受审适配器第 {bound['index']} 次限用名额；"
            '原成果继续核验，只执行未开始的步骤。请再次核对差异并批准此恢复计划。')}

    actions = [
        ('运行离线检查', lambda: run('edit_adapter_state',lambda:(adapter.offline(adapter.DEFAULT_STORE,candidate),adapter.status(adapter.DEFAULT_STORE,candidate))[1])),
        ('批准当前差异并试运行',execute_trial),
        ('预览未完成步骤',resume_preview),
        ('核验并限用 1 次',lambda:run('edit_adapter_state',lambda:adapter.activate(adapter.DEFAULT_STORE,candidate,runs=1))),
        ('成功后扩至总计 3 次',lambda:run('edit_adapter_state',lambda:adapter.activate(adapter.DEFAULT_STORE,candidate,runs=3))),
        ('刷新原差异预览（不调用模型）',refresh_preview),
    ]
    if is_edit_plan(plan) and 'recovery' in plan and adapter.owns_recovery(adapter.DEFAULT_STORE,session,plan) is None:
        actions = [
            ('生成历史编辑接管审阅请求',lambda:run('propose',adoption_request)),
            ('绑定已审历史编辑至当前限用名额',lambda:run('propose',bind_reviewed_adoption)),
        ]
    for label, action in actions:
        ttk.Button(window,text=label,command=action).pack(fill='x',padx=12,pady=3)
    ttk.Label(window,text='独立代码审查由维护者记录，未通过时试运行会停止。',padding=12).pack(fill='x')


def execute(parent, session, plan, *, progress, pause_after_step):
    candidate=adapter.current_candidate()
    kwargs={'approval':plan['sha256'],'stop':parent.stopped.is_set,
            'progress':progress,'pause_after_step':pause_after_step}
    if 'recovery' in plan:
        owner=adapter.owns_recovery(adapter.DEFAULT_STORE,session,plan)
        if owner is None:
            raise ValueError('edit_adapter_recovery_not_owned')
        return adapter.resume(adapter.DEFAULT_STORE,owner['candidate'],session,plan,**kwargs)['outcome']
    return adapter.execute(adapter.DEFAULT_STORE,candidate,session,plan,**kwargs)['outcome']
