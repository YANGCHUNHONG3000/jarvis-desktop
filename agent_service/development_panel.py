"""Review and continue persisted capability development without a file picker."""
from pathlib import Path
import queue
import threading

from .computer_goal_adapters import TASK_BASE


STATES = {
    'queued': '等待开发方案', 'candidate': '等待测试', 'proposed': '等待测试',
    'offline_passed': '测试通过，等待独立审查', 'reviewed': '审查通过，等待实机试运行',
    'trial_ready': '可以实机试运行', 'trial_running': '试运行待核验',
    'trial_succeeded': '实机通过，等待限次启用', 'live_passed': '实机通过，等待限次启用',
    'trial_interrupted': '试运行中断', 'recoverable': '可恢复未完成步骤',
    'enabled': '已限次启用', 'limited': '已限次启用', 'activated': '已限次启用',
    'exhausted': '本阶段次数用完', 'stale': '实现或证据变化，需要重新验证',
    'blocked': '已停止，需开发者处理', 'suspended': '已停用',
    'maintainer_adapter_required': '需要开发新的适配器',
    'needs_review': '证据变化，需重新审查', 'ready_for_trial': '等待实机试运行',
    'ready_for_activation': '实机通过，等待限次启用',
    'not_started': '试运行尚未开始', 'resumable': '中断已有完整回执，可继续剩余步骤',
    'succeeded': '试运行已完成', 'evidence_stale': '实现或验证证据已变化，需要新一轮审查',
    'activation_unknown': '启用过程被中断，次数保持封锁，需开发者核查',
    'interrupted': '执行结果不明，禁止重复操作，需开发者核查',
    'failed': '试运行未通过', 'suspended': '候选已停用',
}
ACTIONS = [('test', '运行离线测试'), ('trial', '批准实机试运行'),
           ('resume', '恢复未完成步骤'), ('verify_live', '核验试运行'),
           ('activate_one', '限用 1 次'), ('activate_three', '扩展到 3 次'),
           ('suspend', '停用候选')]


def allowed_actions(proposal):
    if not proposal:
        return []
    actions = list(proposal.get('actions', []))
    # The backend rechecks ownership and in-flight effects before suspension.
    if proposal.get('stages', {}).get('activation') is True:
        actions.append('suspend')
    return actions


def describe(row, proposal=None):
    lines = ['原任务：' + row.get('instruction', ''),
             '请求进度：' + ('已建立开发方案' if proposal else
                            STATES.get(row.get('state'), row.get('state', '未知')))]
    if row.get('reason'):
        lines.append('原任务缺口：' + str(row['reason']))
    if proposal:
        m = proposal.get('manifest', {})
        lines.extend(['', f"方案：{m.get('name', '')} · 版本 {m.get('version', '')}",
                      '作用范围：' + str(proposal.get('effect', '')),
                      '阶段：' + STATES.get(proposal.get('state'), proposal.get('state', '未知'))])
        if m.get('recipe') == 'workbook_audit_report':
            lines.append('成果内容：全部受支持工作表的逐格记录，包含空格、坐标、原公式及核验值；生成 Excel 和 Word。原工作簿保留，成果中的公式作为文字记录。')
        elif m.get('recipe') == 'document_structure_report':
            lines.append('成果内容：按原顺序记录全部正文、表格、合并与嵌套位置；生成 Excel 和 Word 结构记录。原文档保留，成果不保留原版式。')
        source = proposal.get('source_artifact') or {}
        if source:
            label = '外部来源任务副本：' if source.get('kind') == 'external_office_snapshot' else '固定来源：'
            lines.append(label + str(source.get('artifact', '')))
        for stage, value in proposal.get('stages', {}).items():
            name = {'offline': '离线测试', 'review': '独立审查', 'live': '实机核验',
                    'activation': '限次启用'}.get(stage, stage)
            if isinstance(value, dict):
                state = '已通过' if value.get('passed') is True else str(value.get('state', '已记录'))
            else:
                state = '已通过' if value is True else '尚未通过' if value is False else str(value)
            lines.append(f'{name}：{state}')
        if proposal.get('reason'):
            lines.append('阻塞原因：' + str(proposal['reason']))
        if proposal.get('remaining_runs') is not None:
            lines.append(f"本阶段剩余次数：{proposal['remaining_runs']}")
    lines.extend(['', '实机试运行只创建任务副本，原文件保留。完成的步骤不重做；作用不明时停止。',
                  '独立代码审查由开发者记录。测试、审查和实机核验通过后，才可限次启用。'])
    return '\n'.join(lines)


class DevelopmentPanel:
    def __init__(self, owner, *, base=TASK_BASE):
        import tkinter as tk
        from tkinter import ttk
        self.owner, self.base = owner, Path(base)
        self.store = self.base.parent / 'capabilities'
        self.running = self.closed = False
        self.stop_event = threading.Event()
        self.messages = queue.Queue()
        self.rows, self.row, self.proposals = [], None, []
        self.window = tk.Toplevel(owner.root)
        self.window.title('Jarvis · 能力开发进度')
        self.window.geometry('1080x650')
        self.window.protocol('WM_DELETE_WINDOW', self.close)
        body = ttk.Frame(self.window, padding=12)
        body.pack(fill='both', expand=True)
        ttk.Label(body, text='选择开发请求，检查方案与验证记录，再继续未完成的阶段。').pack(anchor='w')
        self.requests = ttk.Combobox(body, state='readonly')
        self.requests.pack(fill='x', pady=5)
        self.requests.bind('<<ComboboxSelected>>', self.select_request)
        self.versions = ttk.Combobox(body, state='readonly')
        self.versions.pack(fill='x')
        self.versions.bind('<<ComboboxSelected>>', lambda _: self.show())
        self.detail = tk.Text(body, wrap='word', state='disabled', height=20)
        self.detail.pack(fill='both', expand=True, pady=8)
        bar = ttk.Frame(body)
        bar.pack(fill='x')
        self.refresh_button = ttk.Button(bar, text='刷新进度', command=self.refresh)
        self.refresh_button.pack(side='left')
        self.propose_button = ttk.Button(bar, text='为外部来源创建方案', state='disabled',
                                         command=lambda: self.run('propose_external'))
        self.propose_button.pack(side='left', padx=2)
        self.buttons = {}
        for action, label in ACTIONS:
            button = ttk.Button(bar, text=label, state='disabled', command=lambda a=action: self.run(a))
            button.pack(side='left', padx=2)
            self.buttons[action] = button
        self.stop_button = ttk.Button(body, text='停止试运行', state='disabled', command=self.stop_event.set)
        self.stop_button.pack(anchor='w', pady=5)
        self.status = tk.StringVar(value='正在读取开发记录……')
        ttk.Label(body, textvariable=self.status, wraplength=850).pack(anchor='w')
        self.poll_id = self.window.after(100, self.poll)
        self.refresh()

    def selected(self):
        index = self.versions.current()
        return self.proposals[index] if 0 <= index < len(self.proposals) else None

    def show(self):
        chosen = self.selected()
        self.detail.configure(state='normal')
        self.detail.delete('1.0', 'end')
        self.detail.insert('1.0', describe(self.row or {}, chosen))
        self.detail.configure(state='disabled')
        actions = allowed_actions(chosen)
        self.propose_button.configure(state='normal' if (self.row or {}).get('external_proposal_available')
                                      and not self.running else 'disabled')
        for name, button in self.buttons.items():
            button.configure(state='normal' if name in actions and not self.running else 'disabled')

    def start(self, kind, work):
        if self.closed or self.running or self.owner.running or self.owner.external_busy():
            self.status.set('已有任务在运行，请等待。')
            return
        self.running = True
        self.stop_event.clear()
        self.owner.set_busy(True)
        for widget in (self.requests, self.versions, self.refresh_button,
                       self.propose_button, *self.buttons.values()):
            widget.configure(state='disabled')
        self.stop_button.configure(state='normal' if kind in {'trial', 'resume'} else 'disabled')
        self.status.set('正在核验并处理；请等待结果。')
        def worker():
            try:
                self.messages.put((kind, work(), None))
            except Exception as exc:
                import re
                code = str(exc)
                if not re.fullmatch(r'[a-z][a-z0-9_]{0,100}', code):
                    code = 'development_operation_failed'
                self.messages.put((kind, None, code))
        try:
            threading.Thread(target=worker, daemon=True).start()
        except Exception:
            self.messages.put((kind, None, 'development_worker_unavailable'))

    def refresh(self):
        from . import adapter_development as dev
        self.start('list', lambda: dev.list_requests(self.base, self.store))

    def select_request(self, _=None):
        from . import adapter_development as dev
        index = self.requests.current()
        if 0 <= index < len(self.rows):
            task = self.rows[index]['task_root']
            self.start('inspect', lambda: dev.inspect(task, self.base, self.store))

    def run(self, action):
        from . import adapter_development as dev
        if action == 'propose_external':
            if not (self.row or {}).get('external_proposal_available'):
                return
            task = self.row['task_root']
            self.start(action, lambda: dev.propose_external(task, self.base, self.store,
                                                            proposer='Local maintainer'))
            return
        proposal = self.selected()
        if not proposal or action not in allowed_actions(proposal):
            return
        task, digest = self.row['task_root'], proposal['digest']
        def work():
            args = (task, self.base, self.store, digest)
            if action.startswith('activate_'):
                dev.activate(*args, runs=1 if action == 'activate_one' else 3)
            elif action in {'trial', 'resume'}:
                getattr(dev, action)(*args, stop=self.stop_event.is_set)
            elif action in {'test', 'verify_live', 'suspend'}:
                getattr(dev, action)(*args)
            return dev.inspect(task, self.base, self.store, digest=digest)
        self.start(action, work)

    def poll(self):
        if self.closed:
            return
        try:
            kind, result, error = self.messages.get_nowait()
        except queue.Empty:
            pass
        else:
            self.running = False
            self.owner.set_busy(False)
            self.requests.configure(state='readonly')
            self.versions.configure(state='readonly')
            self.refresh_button.configure(state='normal')
            self.stop_button.configure(state='disabled')
            self.status.set('处理完成，以下为重新核验后的进度。' if not error else
                            '未完成：' + error + '。已有记录保留；刷新后查看可恢复阶段。')
            if kind == 'list' and not error:
                previous_task = (self.row or {}).get('task_root')
                self.rows = result
                self.requests.configure(values=[f"{r['instruction'][:55]} · {'已有开发方案' if r.get('versions') else STATES.get(r['state'], r['state'])}"
                                                 for r in result])
                self.row, self.proposals = None, []
                self.versions.set('')
                self.versions.configure(values=[])
                self.show()
                if result:
                    selected = next((i for i, row in enumerate(result) if row['task_root'] == previous_task), 0)
                    self.requests.current(selected)
                    self.select_request()
                else:
                    self.status.set('暂无已核验的适配器开发请求。')
            elif result is not None:
                previous = (self.selected() or {}).get('digest')
                self.row = result
                self.proposals = result.get('proposals', [])
                self.versions.configure(values=[f"{(p.get('manifest') or {}).get('name', '方案需审查')} · v{(p.get('manifest') or {}).get('version', '?')} · {STATES.get(p['state'], p['state'])}"
                                                for p in self.proposals])
                if self.proposals:
                    selected = next((i for i, p in enumerate(self.proposals) if p.get('digest') == previous),
                                    len(self.proposals) - 1)
                    self.versions.current(selected)
                self.show()
                if kind in {'trial', 'resume', 'verify_live'}:
                    selected = self.selected() or {}
                    if selected.get('artifacts'):
                        try:
                            self.owner.on_artifacts(selected['artifacts'])
                            self.status.set('成果已核验，并已接入下一任务。')
                        except Exception:
                            self.status.set('成果已保存；接入下一任务未完成，可刷新后重试。')
            else:
                # Failed or stale operation must not leave old approval buttons enabled.
                for button in self.buttons.values():
                    button.configure(state='disabled')
        self.poll_id = self.window.after(100, self.poll)

    def close(self):
        if self.running:
            self.stop_event.set()
            self.status.set('正在停止；请等待阶段记录保存后再关闭。')
            return
        self.closed = True
        if self.poll_id:
            self.window.after_cancel(self.poll_id)
        self.window.destroy()
