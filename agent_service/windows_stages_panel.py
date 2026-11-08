"""Review observed stages without replacing the main Windows task draft."""
from copy import deepcopy
from pathlib import Path
import queue
import threading

from .computer import ComputerError
from . import windows_stages as workflow


def target_text(condition):
    from . import windows_winapp_office as office
    from .windows_reviewed_controls import is_goal, review_text
    from .windows_visual_forms import is_goal as form_goal, review_text as form_text
    from .windows_visual import is_goal as visual_goal, decode
    selector, value = condition['selector'], condition['value']
    if office.is_goal(selector):
        item, source = office.decode(selector, value)
        detail = ('执行查找并核验实际命中，查询词：' + repr(source['query'])
                  if item['source']['role'] == 'next_hit' else '关闭原查找窗口并核验其销毁；保留工作簿')
    elif is_goal(selector):
        detail = review_text(selector, value)
    elif form_goal(selector):
        detail = form_text(selector, value)
    elif visual_goal(selector):
        decoded = decode(selector, value)
        detail = ('核验选中标记与实际工具文字' if decoded.get('selected')
                  else '核验：' + '、'.join(decoded['all']))
    else:
        names = {'selected': '选中', 'expanded': '展开', 'collapsed': '折叠',
                 'focused': '聚焦', 'checked': '勾选状态', 'value': '字段内容'}
        detail = ('收起（展开状态 = False）' if condition['state'] == 'collapsed' else
                  names.get(condition['state'], condition['state']) + ' = ' + str(value))
    return f"{condition['window']['title']} / {selector['name']}：{detail}"


class WindowsStagesPanel:
    def __init__(self, parent, *, instruction='', observations=(), session_root=None,
                 busy=lambda: False, on_busy=lambda value: None, backend=None,
                 client_factory=None, session_base=workflow.BASE, task_base=None):
        import tkinter as tk
        from tkinter import ttk
        self.external_busy, self.on_busy = busy, on_busy
        self.instruction, self.observations = instruction, deepcopy(observations)
        self.session_root, self.session_base, self.task_base = session_root, session_base, task_base
        self.backend, self.client_factory = backend, client_factory
        self.state = None
        self.running = self.closed = self.closing = False
        self.messages, self.stopped = queue.Queue(), threading.Event()
        self.top = tk.Toplevel(parent)
        self.top.title('Jarvis · 分阶段电脑任务')
        self.top.geometry('940x700')
        self.top.protocol('WM_DELETE_WINDOW', self.close)
        self.status = tk.StringVar(value='先生成当前可见阶段，核对完整任务、当前目标和待续内容后确认。')
        self.path = tk.StringVar(value=str(session_root or '尚未保存'))
        self.pause = tk.BooleanVar(value=False)
        self.visual_context = tk.BooleanVar(value=False)
        ttk.Label(self.top, text='每阶段重新观察、生成待审目标、执行并核验。只继续未完成阶段；未知作用停止。',
                  wraplength=900).pack(anchor='w', padx=12, pady=8)
        self.preview = tk.Text(self.top, wrap='word', state='disabled', height=23)
        self.preview.pack(fill='both', expand=True, padx=12)
        row = ttk.Frame(self.top); row.pack(fill='x', padx=12, pady=8)
        self.plan_button = ttk.Button(row, text='生成当前阶段（云端）', command=self.advance)
        self.plan_button.pack(side='left')
        self.execute_button = ttk.Button(row, text='确认本阶段并执行', command=self.execute)
        self.execute_button.pack(side='left', padx=6)
        self.restore_button = ttk.Button(row, text='找回分阶段任务', command=self.restore)
        self.restore_button.pack(side='left')
        self.recheck_button = ttk.Button(row, text='只读复核关闭结果', command=self.recheck_close)
        self.recheck_button.pack(side='left', padx=6)
        ttk.Button(row, text='停止', command=self.stop).pack(side='left', padx=6)
        self.pause_button = ttk.Checkbutton(row, text='每个目标后暂停', variable=self.pause)
        self.pause_button.pack(side='left')
        self.visual_button = ttk.Checkbutton(self.top, text='截图辅助规划（所选整窗含可见文档发往 OpenAI；截图本机留存）',
            variable=self.visual_context)
        self.visual_button.pack(anchor='w', padx=12)
        ttk.Label(self.top, text='沿用现有云模型、当前本地费用设置与共享账本。'
                  '原始任务保留；阶段通过不代表整个任务已验收。', wraplength=900).pack(anchor='w', padx=12)
        ttk.Label(self.top, textvariable=self.path, wraplength=900).pack(anchor='w', padx=12, pady=4)
        ttk.Label(self.top, textvariable=self.status, wraplength=900).pack(anchor='w', padx=12, pady=6)
        self.render()
        self.poll_id = self.top.after(100, self.poll)
        if session_root is not None:
            self.start(lambda: workflow.load(session_root))

    def render(self):
        state = self.state
        text = '完整任务：\n' + (state['instruction'] if state else self.instruction or '请从主界面填写指令并观察窗口，或找回已有任务。')
        if state:
            goal, summary = state['goal'], state['summary']
            if state.get('completed_stages'):
                text += '\n\n已核验阶段：\n' + '\n'.join(
                    f"{v['number']}. {v['instruction']}" + ('（当前状态复核通过；原操作仍待核验）' if v.get('reconciled') else '')
                    for v in state['completed_stages'])
            text += f"\n\n当前阶段：{state['stage_number']}\n"
            if goal:
                text += goal['instruction'] + '\n'
                for c in goal['conditions']:
                    mark = ('当前状态只读复核通过，原 unknown 保留' if state.get('close_reconciled') else
                            '已核验' if c['id'] in summary['completed'] else '待执行/核验')
                    text += f"\n[{mark}] {target_text(c)}"
            if summary and summary.get('manual_attention'):
                attention = summary['manual_attention']
                text += ('\n\n检测到未受审弹窗，已停止自动操作与观察。请在原应用中处理；'
                         '不要在聊天里提供密码或验证码。')
                text += ('\n原操作作用未知，禁止直接重试。' if summary['state'] == 'unknown' else
                         '\n处理后点击“已处理弹窗，重新观察并继续”，复用原批准目标。')
            text += '\n\n本阶段之后待续（模型草稿，请核对）：\n' + (state['remaining_instruction'] or '模型未列出更多步骤；完整任务仍待你验收。')
            self.path.set(str(state['root']))
        self.preview.configure(state='normal'); self.preview.delete('1.0', 'end')
        self.preview.insert('1.0', text); self.preview.configure(state='disabled')
        eligible = not self.running and not self.closing
        can_plan = state['can_advance'] if state else bool(self.instruction.strip() and self.observations)
        self.plan_button.configure(state='normal' if eligible and can_plan else 'disabled',
            text='根据新观察规划下一阶段（云端）' if state and state['stage_number'] else '生成当前阶段（云端）')
        summary = state['summary'] if state else None
        can_run = (summary is not None and summary['state'] not in {'verified', 'unknown'}
                   and summary.get('eligible', True) and not state.get('close_reconciled'))
        self.execute_button.configure(state='normal' if eligible and can_run else 'disabled',
            text=('已处理弹窗，重新观察并继续' if summary and summary.get('manual_attention') and can_run else
                  '确认本阶段并执行' if state is None or state['needs_acceptance'] else '重新观察并继续本阶段'))
        self.restore_button.configure(state='normal' if eligible else 'disabled')
        self.recheck_button.configure(state='normal' if eligible and state and state.get('can_recheck_close') else 'disabled')
        self.pause_button.configure(state='normal' if eligible else 'disabled')
        if state:
            self.visual_context.set(state.get('visual_context', False))
        self.visual_button.configure(state='normal' if eligible and self.session_root is None and state is None else 'disabled')

    def start(self, operation):
        if self.running or self.closed or self.closing or self.external_busy():
            self.status.set('已有操作正在进行，请先等待或停止。')
            return
        self.running = True; self.stopped.clear(); self.on_busy(True); self.render()
        self.status.set('正在核对来源和当前阶段……')
        def work():
            try:
                self.messages.put(('done', operation(), None))
            except Exception as exc:
                self.messages.put(('done', None, str(exc) if isinstance(exc, ComputerError) else 'windows_stages_failed'))
        try:
            threading.Thread(target=work, daemon=True).start()
        except Exception:
            self.running = False; self.on_busy(False); self.render()
            self.status.set('无法启动操作，原任务保留。')

    def get_backend(self):
        expected = self.state.get('provider_binding') if self.state else getattr(self.backend, 'provider_binding', None)
        if self.state and getattr(self.backend, 'provider_binding', None) != expected:
            from .windows_access import backend_for_binding
            self.backend = backend_for_binding(expected)
        if self.backend is None:
            if self.state is not None:
                from .windows_access import backend_for_binding
                self.backend = backend_for_binding(expected)
            else:
                from .windows_access import AutoWindowsBackend
                self.backend = AutoWindowsBackend()
        return self.backend

    def advance(self):
        root = self.session_root
        # Snapshot Tk state on the main thread before starting the worker.
        visual_context = self.visual_context.get()
        def plan():
            nonlocal root
            if self.stopped.is_set():
                raise ComputerError('windows_intent_cancelled')
            if root is None:
                binding = getattr(self.get_backend(), 'provider_binding', None)
                root = workflow.create(self.instruction, self.observations, parent=self.session_base,
                    visual_context=visual_context,
                    **({'provider_binding': binding} if binding else {}))
                self.messages.put(('created', root, None))
            current = workflow.load(root)
            if not current['can_advance']:
                return current
            if self.client_factory is None:
                from .model import CloudClient
                client = CloudClient(should_stop=self.stopped.is_set)
            else:
                client = self.client_factory(self.stopped.is_set)
            return workflow.advance(root, self.get_backend(), client, stop=self.stopped.is_set, task_base=self.task_base)
        self.start(plan)

    def execute(self):
        if self.session_root is None or self.state is None:
            return
        root, pause = self.session_root, self.pause.get()
        self.start(lambda: workflow.execute(root, self.get_backend(), stop=self.stopped.is_set, pause_after_step=pause))

    def restore(self):
        from tkinter import filedialog
        if self.running or self.external_busy():
            return
        path = filedialog.askdirectory(parent=self.top, title='选择分阶段任务目录', initialdir=str(self.session_base))
        if path:
            self.start(lambda: workflow.load(path))

    def recheck_close(self):
        if self.session_root is None or not self.state or not self.state.get('can_recheck_close'):
            return
        root = self.session_root
        self.start(lambda: workflow.recheck_current_close(root, self.get_backend(), stop=self.stopped.is_set))

    def stop(self):
        self.stopped.set()
        self.status.set('已请求停止；保存记录后可继续，已核验目标不会重复执行。')

    def poll(self):
        self.poll_id = None
        if self.closed:
            return
        try:
            while True:
                kind, result, error = self.messages.get_nowait()
                if kind == 'created':
                    self.session_root = result; self.path.set(str(result)); continue
                self.running = False; self.on_busy(False)
                if error:
                    if self.session_root is not None:
                        try: self.state = workflow.load(self.session_root)
                        except Exception: self.state = None
                    messages = {'windows_unknown_requires_review': '存在未知作用，已停止，不重放。',
                        'model_budget_exceeded': '共享预算不足，任务已保留；不追加消费。',
                        'windows_intent_cancelled': '已停止，任务和失败记录保留。',
                        'windows_stages_model_limit': '本任务已达到规划次数上限，请核对记录。',
                        'windows_stages_response_multiple_targets': '模型一次提出多个目标，本阶段未采用；已完成步骤保留，可重新观察后规划单个目标。',
                        'windows_intent_unsupported_or_ambiguous': '当前没有可唯一核验的目标；原任务和剩余内容保留。',
                        'windows_stages_changed': '阶段来源或回执已变化，停止接续。'}
                    self.status.set(messages.get(error, '本阶段未通过，记录保留；原因：' + error))
                else:
                    self.state, self.session_root = result, result['root']
                    summary = result['summary']
                    if summary and summary.get('manual_attention'):
                        self.status.set('检测到未受审弹窗，已停止自动观察；' +
                            ('原操作作用未知，保留记录且禁止重放。' if summary['state'] == 'unknown' else
                             '请在原应用处理后点继续；只重新观察和执行未完成目标。'))
                    elif summary and summary.get('unsealed_effect'):
                        self.status.set('已派发的操作尚未完成核验，停止接续且不重放；旧记录保留，请先核对当前窗口。')
                    elif result.get('close_reconciled'):
                        self.status.set('原关闭操作仍为未知；独立只读复核确认当前查找框已销毁、工作簿未变。可规划后续，不重放关闭。')
                    elif summary and summary['state'] == 'verified':
                        self.status.set('当前阶段已核验；可按新观察规划后续。' if result['can_advance'] else
                                        '当前阶段已核验；请核对完整任务。未自动宣布全部完成。')
                    elif summary and summary['state'] == 'unknown':
                        self.status.set('操作后的状态未通过核验，可能有弹窗或目标未达到；请查看原窗口。'
                            '停止接续且不重放，原记录保留。原因：' + summary.get('cause_code', summary.get('code', 'unknown')))
                    elif summary and summary['state'] == 'failed':
                        self.status.set('本阶段未通过，已完成步骤保留；可重新观察后继续。原因：' +
                                        summary.get('cause_code', summary.get('code', 'failed')))
                    elif result['needs_acceptance']:
                        self.status.set('方案已保存，尚未执行；请核对完整任务、本阶段目标和待续内容后确认。')
                    else:
                        self.status.set('已找回阶段，可重新观察并继续未完成目标。')
                self.render()
        except queue.Empty:
            pass
        if self.closing and not self.running:
            self.close(); return
        self.poll_id = self.top.after(100, self.poll)

    def close(self):
        if self.closed:
            return
        if self.running:
            self.closing = True; self.stop(); self.render(); return
        self.closed = True
        if self.poll_id is not None:
            self.top.after_cancel(self.poll_id); self.poll_id = None
        self.top.destroy()


def open_panel(parent, **options):
    return WindowsStagesPanel(parent, **options)
