"""Reviewed heterogeneous tasks; Tk owns widgets, workers own verification."""
from copy import deepcopy
import json
from pathlib import Path
import queue
import threading

from . import mixed_tasks as core, workspace_state


def delivery_payload(records, current, submitted):
    """Clear only this panel's unchanged submission; keep any newer draft."""
    draft = current['payload']['draft']
    if submitted is not None and current['revision'] == submitted:
        draft = ''
    return {'draft': draft, 'records': deepcopy(records), 'source': None}


def actions(view):
    state = (view or {}).get('state')
    current = (view or {}).get('current') or {}
    resumable = current.get('summary', {}).get('resumable') is True
    return {
        'approve': state == 'review',
        'prepare': state == 'ready',
        'execute': state in {'preview', 'child_verified'} or
                   (current.get('kind') == 'windows' and state in {'paused', 'stopped'} and resumable),
        'resume': current.get('kind') == 'office_edit' and state != 'preview' and resumable,
        'deliver': state == 'complete',
    }


def preview_lines(current):
    plan = current['plan']
    if current['kind'] == 'office_edit':
        lines = ['执行方式：Office 编辑适配器；保存副本并原生回读，保留格式和公式。']
        for edit in plan['goal']['edits']:
            spec = edit['spec']
            slot = current['source_ids'][int(edit['id'][1:]) - 1]
            record = current['input_records'][int(slot[1:]) - 1]
            lines.append(f"{slot} · {'Word' if spec['kind'] == 'document' else 'Excel'} · {record['path']}")
            for change in spec['changes']:
                if 'cell' in change:
                    position = f"{change['sheet']}!{change['cell']}"
                elif set(change) <= {'paragraph_index', 'before', 'after'}:
                    position = f"正文段落 {change['paragraph_index'] + 1}"
                else:
                    position = json.dumps({k: v for k, v in change.items() if k not in {'before', 'after'}}, ensure_ascii=False)
                lines.append(f"  {position}：{change.get('before')!s} → {change.get('after')!s}")
        return lines
    lines = ['执行方式：按控件自动选择受审后端；每步原生回读。']
    for condition in plan['conditions']:
        lines.append(f"{condition['window']['title']} / {condition['selector'].get('name', '')}："
                     f"{condition['state']} → {condition['value']}；后续保持={condition.get('retain', False)}")
    return lines


class MixedTasksPanel:
    def __init__(self, *, root, context, workspace_context, windows, busy, on_busy,
                 apply, status, task_base):
        self.root, self.context, self.workspace_context = root, context, workspace_context
        self.windows, self.busy, self.on_busy = windows, busy, on_busy
        self.apply, self.status, self.task_base = apply, status, task_base
        self.dialog = None
        self.running = self.closing = self.closed = self.hide_pending = False
        self.stopped = threading.Event()
        self.messages = queue.Queue()
        self.task_root = self.view = self.submitted = None
        self.epoch = 0
        root.after(100, self.poll)

    def _show(self):
        import tkinter as tk
        from tkinter import ttk
        if self.dialog is not None and self.dialog.winfo_exists():
            self.dialog.deiconify()
            self.dialog.lift()
            return
        self.dialog = tk.Toplevel(self.root)
        self.dialog.title('Jarvis · 混合任务清单')
        self.dialog.geometry('1080x760')
        self.dialog.protocol('WM_DELETE_WINDOW', self.hide)
        self.label = tk.StringVar(value='先拆分完整任务，再逐步检查预览并执行。')
        ttk.Label(self.dialog, textvariable=self.label, wraplength=1020).pack(anchor='w', padx=12, pady=8)
        frame = ttk.Frame(self.dialog)
        frame.pack(fill='both', expand=True, padx=12)
        self.detail = tk.Text(frame, wrap='word', height=28)
        self.detail.pack(side='left', fill='both', expand=True)
        scroll = ttk.Scrollbar(frame, command=self.detail.yview)
        scroll.pack(side='right', fill='y')
        self.detail.configure(yscrollcommand=scroll.set, state='disabled')
        bar = ttk.Frame(self.dialog)
        bar.pack(fill='x', padx=12, pady=8)
        self.buttons = {}
        for name, text, command in (
            ('approve', '确认拆分清单', self.approve),
            ('prepare', '生成当前步预览', self.prepare),
            ('execute', '确认执行当前步', self.execute),
            ('resume', '预览剩余操作', self.resume),
            ('deliver', '成果接到下一任务', self.deliver)):
            button = ttk.Button(bar, text=text, command=command)
            button.pack(side='left', padx=(0, 5))
            self.buttons[name] = button
        self.stop_button = ttk.Button(bar, text='停止', command=self.stop)
        self.stop_button.pack(side='left')
        ttk.Label(self.dialog, text='每次只执行已确认的当前步；窗口/控件变化会重新观察，未知作用不重放。关闭窗口保留原任务。').pack(anchor='w', padx=12, pady=(0, 10))
        self.render()

    def render(self):
        if self.dialog is None or not self.dialog.winfo_exists():
            return
        enabled = actions(self.view)
        for name, button in self.buttons.items():
            button.configure(state='normal' if enabled[name] and not self.running else 'disabled')
        self.stop_button.configure(state='normal' if self.running else 'disabled')
        view = self.view or {}
        lines = [f"任务：{self.task_root or '尚未建立'}", f"状态：{view.get('state', '准备中')}；剩余 {view.get('remaining_steps', '?')} 步"]
        if view.get('instruction'):
            lines.append('\n完整指令：' + view['instruction'])
        for i, record in enumerate(view.get('records', []), 1):
            lines.append(f"s{i} 当前成果：{record['path']}")
        for i, window in enumerate(view.get('windows', []), 1):
            lines.append(f"w{i} 已绑定窗口：{window['title']}")
        for i, step in enumerate(view.get('steps', [])):
            marker = '已核验' if i < view['current_index'] else '当前' if i == view['current_index'] else '待做'
            lines.append(f"\n{i+1}. [{marker}] {'Office 编辑' if step['kind'] == 'office_edit' else 'Windows 操作'}\n{step['instruction']}\n来源 {', '.join(step['source_ids']) or '沿用成果'}；窗口 {', '.join(step['window_ids']) or '文件适配器'}")
        current = view.get('current')
        if current:
            lines.append('\n当前步具体预览（检查目标、修改内容及核验条件后再执行）：')
            lines.extend(preview_lines(current))
        if view.get('state') in {'unknown', 'planning_pending', 'split_pending'}:
            lines.append('\n已有调用/作用记录尚不能确认；已停止自动重试，原任务和回执保留。')
        self.detail.configure(state='normal')
        self.detail.delete('1.0', 'end')
        self.detail.insert('1.0', '\n'.join(lines))
        self.detail.configure(state='disabled')

    def _launch(self, operation, *, delivery_context=None):
        if self.closed or self.closing or self.running or self.busy():
            self.status('当前任务正忙；未开始另一次混合任务操作。')
            return False
        self._show()
        self.running = True
        self.stopped.clear()
        self.epoch += 1
        epoch = self.epoch
        self.on_busy(True)
        self.label.set('正在核验或规划当前步骤；可停止，已完成步骤不会重做。')
        self.render()

        def work():
            result, error = None, None
            try:
                result = operation()
            except Exception as exc:
                # Stable application errors only; never expose arbitrary provider text.
                code = str(exc)
                error = code if code.isascii() and code.replace('_', '').isalnum() and len(code) < 100 else type(exc).__name__
            self.messages.put(('finished', epoch, result, error, delivery_context))
        try:
            threading.Thread(target=work, name='mixed-task', daemon=True).start()
        except Exception:
            self.running = False
            self.on_busy(False)
            self.label.set('后台任务未启动；内容已保留。')
            self.render()
            return False
        return True

    def new(self):
        if self.closed or self.closing or self.running or self.busy():
            self.status('当前任务正忙，稍后再建立混合清单。')
            return False
        context = deepcopy(self.context())
        if context['unavailable'] or not context['instruction'] or not context['records']:
            self.status('请先输入混合指令，并选择已核验的 Office 来源或当前成果批次。')
            return False
        windows = deepcopy(self.windows())
        ticket = deepcopy(self.workspace_context()['revision'])
        self.view, self.task_root, self.submitted = None, None, ticket
        def operation():
            from .model import CloudClient
            from .windows_access import AutoWindowsBackend
            targets = windows or AutoWindowsBackend().list_windows(stop=self.stopped.is_set)
            root = core.create(context['instruction'], context['records'], targets, task_base=self.task_base)
            self.messages.put(('created', self.epoch, str(root), None, None))
            return core.split(root, CloudClient(should_stop=self.stopped.is_set), self.stopped.is_set)
        return self._launch(operation)

    def restore(self, path=None):
        if self.closed or self.closing or self.running or self.busy():
            return False
        if path is None:
            from tkinter import filedialog
            path = filedialog.askdirectory(parent=self.root, title='找回原混合任务目录', initialdir=str(core.BASE))
        if not path:
            return False
        self.task_root, self.submitted, self.view = str(Path(path)), None, None
        return self._launch(lambda: core.inspect(self.task_root))

    def approve(self):
        if not actions(self.view)['approve']:
            return False
        root, sha = self.task_root, self.view['plan_sha256']
        return self._launch(lambda: core.approve(root, sha))

    def prepare(self):
        if not actions(self.view)['prepare']:
            return False
        root = self.task_root
        def operation():
            from .model import CloudClient
            return core.prepare(root, CloudClient(should_stop=self.stopped.is_set), self.stopped.is_set)
        return self._launch(operation)

    def execute(self):
        if not actions(self.view)['execute']:
            return False
        root, sha = self.task_root, self.view['current']['plan_sha256']
        return self._launch(lambda: core.execute(root, sha, self.stopped.is_set))

    def resume(self):
        if not actions(self.view)['resume']:
            return False
        root = self.task_root
        return self._launch(lambda: core.resume_preview(root))

    def deliver(self):
        if not actions(self.view)['deliver']:
            return False
        snapshot = deepcopy(self.workspace_context())
        if snapshot['unavailable']:
            self.status('主界面来源仍在变化，请稍后接入成果。')
            return False
        root, submitted = self.task_root, deepcopy(self.submitted)
        def operation():
            records = core.outputs(root)
            return workspace_state.verify(delivery_payload(records, snapshot, submitted), self.task_base)
        return self._launch(operation, delivery_context=snapshot)

    def poll(self):
        if self.closed:
            return
        try:
            while True:
                kind, epoch, result, error, snapshot = self.messages.get_nowait()
                if epoch != self.epoch:
                    continue
                if kind == 'created':
                    self.task_root = result
                    self.render()
                    continue
                self.running = False
                self.on_busy(False)
                if self.closing:
                    self.close()
                    return
                if error:
                    self.label.set('Office 适配器的试运行资格已用完或未通过评审；本步未调用模型、未建立执行任务。完成启用评审后可找回原清单继续。'
                                   if error == 'mixed_office_qualification_unavailable' else
                                   f'本步停止：{error}。原任务已保留；可找回重新核验，不会重建或重放。')
                    # A failed call invalidates the visible approval; restore is read-only.
                    self.view = None
                elif snapshot is not None:
                    if self.stopped.is_set() or self.workspace_context() != snapshot:
                        self.label.set('主界面草稿或来源已变化，未覆盖；成果保留，可重新点击接续。')
                    elif self.apply(result):
                        self.label.set('已把核验成果接到主界面；新草稿保留，请明确开始下一任务。')
                else:
                    self.view, self.task_root = result, result['root']
                    self.label.set('当前步已核验。继续预览剩余步骤。' if result['state'] == 'ready' else '请检查清单状态及当前预览。')
                self.render()
                if self.hide_pending:
                    self.hide_pending = False
                    self.hide()
        except queue.Empty:
            pass
        finally:
            if not self.closed:
                self.root.after(100, self.poll)

    def stop(self):
        self.stopped.set()

    def hide(self):
        if self.running:
            self.hide_pending = True
            self.stop()
            self.label.set('正在停止，等待保存恢复记录后再关闭。')
        elif self.dialog is not None:
            self.dialog.withdraw()

    def close(self):
        self.closing = True
        self.stop()
        if self.running:
            return
        self.closed = True
        if self.dialog is not None and self.dialog.winfo_exists():
            self.dialog.destroy()
