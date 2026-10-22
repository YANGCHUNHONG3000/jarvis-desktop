"""Local selected-window UI. Explicit preview/execute; no unattended loop."""
import json
import queue
import threading

from .computer import ComputerError, ComputerSession, action_policy

ACTION_LABELS = {'聚焦': 'focus', '填写文字': 'set_value', '点击按钮': 'invoke',
                 '选择标签或项目': 'select', '展开': 'expand', '折叠': 'collapse'}
ACTION_NAMES = {v: k for k, v in ACTION_LABELS.items()}
TYPE_NAMES = {'Window':'窗口', 'Button':'按钮', 'Text':'文字', 'Edit':'输入框',
              'Document':'编辑区', 'TabItem':'标签页', 'TreeItem':'文件夹', 'ListItem':'项目'}


def attach_computer_tab(tabs, root, *, busy=lambda: False, on_busy=lambda value: None):
    import tkinter as tk
    from tkinter import ttk
    tab = ttk.Frame(tabs, padding=12)
    tabs.add(tab, text='电脑操作')
    controller = ComputerPanel(tab, root, busy=busy, on_busy=on_busy)
    tab.controller = controller
    return controller


class ComputerPanel:
    def __init__(self, tab, root, *, busy=lambda: False, on_busy=lambda value: None):
        import tkinter as tk
        from tkinter import ttk
        self.root, self.tab, self.external_busy = root, tab, busy
        self.on_busy = on_busy
        self.session = None
        self.snapshot = self.plan = None
        self.windows = []
        self.running = False
        self.stopped = threading.Event()
        self.messages = queue.Queue()
        self.closed = False
        ttk.Label(tab, text='选择窗口，读取控件，再预览并执行一步。支持计算器、记事本编辑/标签页、资源管理器选择/展开；'
                  '其他应用目前只支持观察和聚焦。', wraplength=700).pack(anchor='w')
        row = ttk.Frame(tab)
        row.pack(fill='x', pady=6)
        self.window = ttk.Combobox(row, state='readonly', width=58)
        self.window.pack(side='left', fill='x', expand=True)
        self.window.bind('<<ComboboxSelected>>', lambda _: self.invalidate())
        ttk.Button(row, text='刷新窗口', command=self.refresh).pack(side='left')
        ttk.Button(row, text='读取控件', command=self.observe).pack(side='left')
        self.tree = ttk.Treeview(tab, columns=('type', 'name', 'value', 'actions'), show='headings', height=9)
        for key, label, width in [('type', '类型', 80), ('name', '控件', 180), ('value', '实际内容', 230), ('actions', '支持动作', 180)]:
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width)
        self.tree.pack(fill='both', expand=True)
        self.tree.bind('<<TreeviewSelect>>', lambda _: self.invalidate(keep_snapshot=True))
        row = ttk.Frame(tab)
        row.pack(fill='x', pady=6)
        self.action = ttk.Combobox(row, state='readonly', values=tuple(ACTION_LABELS), width=16)
        self.action.set('聚焦')
        self.action.pack(side='left')
        self.action.bind('<<ComboboxSelected>>', lambda _: self.invalidate(keep_snapshot=True))
        ttk.Label(row, text='填写文字时，在下框输入完整的新内容。').pack(side='left')
        self.value = tk.Text(tab, height=3, wrap='word')
        self.value.pack(fill='x')
        self.value.bind('<<Modified>>', self.value_changed)
        ttk.Button(tab, text='预览所选操作（本地）', command=self.preview).pack(anchor='w', pady=5)
        self.instruction = ttk.Entry(tab)
        self.instruction.pack(fill='x')
        ttk.Label(tab, text='上框填写下一步目标。点击云端建议将上传当前所选窗口的控件文字及目标；'
                  '每会话最多 6 次，沿用每日及滚动 24 小时各 ¥5 上限。', wraplength=700).pack(anchor='w')
        ttk.Button(tab, text='云端建议下一步（上传所选窗口内容）', command=self.propose).pack(anchor='w', pady=5)
        self.preview_text = tk.StringVar(value='尚无操作预览。')
        ttk.Label(tab, textvariable=self.preview_text, wraplength=700).pack(anchor='w')
        row = ttk.Frame(tab)
        row.pack(fill='x', pady=6)
        self.execute_button = ttk.Button(row, text='执行预览的这一步', command=self.execute, state='disabled')
        self.execute_button.pack(side='left')
        ttk.Button(row, text='停止', command=self.stop).pack(side='left', padx=8)
        self.status = tk.StringVar(value='本地读取不调用模型。操作后会重新读取；按钮已调用不等于整个任务已完成。')
        ttk.Label(tab, textvariable=self.status, wraplength=700).pack(anchor='w')
        self.poll_id = root.after(100, self.poll)

    def invalidate(self, *, keep_snapshot=False):
        self.plan = None
        self.execute_button.configure(state='disabled')
        self.preview_text.set('选择或内容已变化，请重新预览。')
        if not keep_snapshot:
            self.snapshot = None
            self.tree.delete(*self.tree.get_children())

    def value_changed(self, _=None):
        if self.value.edit_modified():
            self.invalidate(keep_snapshot=True)
            self.value.edit_modified(False)

    def start(self, kind, operation):
        if self.running or self.external_busy():
            self.status.set('已有任务正在运行，请先等待或停止。')
            return
        self.running = True
        self.on_busy(True)
        self.stopped.clear()
        self.execute_button.configure(state='disabled')
        self.window.configure(state='disabled')
        self.action.configure(state='disabled')
        self.value.configure(state='disabled')
        self.instruction.configure(state='disabled')
        self.status.set('正在处理……')
        def run():
            try:
                if self.session is None:
                    self.session = ComputerSession.create()
                result = operation(self.session)
                self.messages.put((kind, result, None))
            except Exception as exc:
                code = str(exc) if isinstance(exc, ComputerError) else 'operation_failed'
                self.messages.put((kind, None, code))
        try:
            threading.Thread(target=run, daemon=True).start()
        except Exception:
            self.running = False
            self.on_busy(False)
            self.window.configure(state='readonly')
            self.action.configure(state='readonly')
            self.value.configure(state='normal')
            self.instruction.configure(state='normal')
            self.status.set('无法启动后台操作。')

    def refresh(self):
        if self.running:
            return
        self.invalidate()
        self.start('windows', lambda s: s.windows(self.stopped.is_set))

    def observe(self):
        if self.running:
            return
        index = self.window.current()
        if not 0 <= index < len(self.windows):
            self.status.set('请先刷新并选择窗口。')
            return
        identity = dict(self.windows[index])
        self.invalidate()
        self.start('observe', lambda s: s.observe(identity, self.stopped.is_set))

    def show_snapshot(self, snapshot):
        self.invalidate()
        self.snapshot = snapshot
        for c in snapshot['observation']['controls']:
            if c['visible'] and (c['type'] in TYPE_NAMES or c['value']):
                allowed = [p for p in c['patterns'] if action_policy(snapshot['observation']['window'], c, p)[0]]
                self.tree.insert('', 'end', iid=c['id'], values=(TYPE_NAMES.get(c['type'], '控件'), c['name'], (c['value'] or '')[:300], '、'.join(ACTION_NAMES[p] for p in allowed)))

    def show_plan(self, plan):
        self.plan = plan
        if plan is None:
            self.execute_button.configure(state='disabled')
            return
        p = plan['preview']
        text = f"窗口：{p['window']}；控件：{p['control']}；动作：{ACTION_NAMES[plan['action']]}"
        if plan['action'] == 'set_value':
            text += '\n将编辑区替换为：' + plan['value']
        if not plan['allowed']:
            text += '\n暂不执行：尚未验证该控件的作用。'
        self.preview_text.set(text)
        self.execute_button.configure(state='normal' if plan['allowed'] else 'disabled')

    def preview(self):
        if self.running:
            return
        selected = self.tree.selection()
        if self.snapshot is None or not selected:
            self.status.set('请读取窗口并选择控件。')
            return
        try:
            action = ACTION_LABELS[self.action.get()]
            plan = self.session.prepare(self.snapshot, selected[0], action,
                                        self.value.get('1.0', 'end-1c') if action == 'set_value' else '')
            self.show_plan(plan)
        except Exception as exc:
            self.invalidate(keep_snapshot=True)
            self.status.set(str(exc) if isinstance(exc, ComputerError) else '预览失败，请重新读取。')

    def propose(self):
        if self.running:
            return
        snapshot, instruction = self.snapshot, self.instruction.get()
        if snapshot is None:
            self.status.set('请先读取所选窗口。')
            return
        self.invalidate(keep_snapshot=True)
        self.start('propose', lambda s: s.propose(instruction, snapshot, stop=self.stopped.is_set))

    def execute(self):
        plan = self.plan
        if self.running or plan is None or not plan['allowed']:
            return
        self.invalidate(keep_snapshot=True)
        # Approval comes only from this explicit local button, never model JSON.
        self.start('execute', lambda s: s.execute(plan, approval=plan['sha256'], stop=self.stopped.is_set))

    def stop(self):
        self.stopped.set()
        self.invalidate()
        self.status.set('停止已请求；已发生的动作不会回滚。请重新读取后再决定下一步。')

    def close(self):
        self.closed = True
        self.stopped.set()
        if self.poll_id is not None:
            self.root.after_cancel(self.poll_id)
            self.poll_id = None

    def poll(self):
        if self.poll_id is not None:
            self.root.after_cancel(self.poll_id)
            self.poll_id = None
        if self.closed:
            return
        try:
            while True:
                kind, result, error = self.messages.get_nowait()
                self.running = False
                self.on_busy(False)
                self.window.configure(state='readonly')
                self.action.configure(state='readonly')
                self.value.configure(state='normal')
                self.instruction.configure(state='normal')
                if error:
                    self.invalidate()
                    self.status.set('未完成：' + error + '。请重新读取，不要盲目重试。')
                elif self.stopped.is_set():
                    self.invalidate()
                    self.status.set('已停止；记录已保留，下一步前请重新读取。')
                elif kind == 'windows':
                    self.windows = result
                    self.window.configure(values=[f"{w['title']} · 窗口 {w['hwnd']}" for w in result])
                    self.window.set('')
                    self.status.set(f'找到 {len(result)} 个可选择窗口。')
                elif kind == 'observe':
                    self.show_snapshot(result)
                    suffix = '（列表有截断）' if result['observation']['truncated'] else ''
                    self.status.set('读取完成' + suffix + '；快照 120 秒内有效。')
                elif kind == 'propose':
                    self.show_plan(result['proposal'])
                    self.status.set('模型建议：' + result['reason'])
                elif kind == 'execute':
                    if result.get('snapshot'):
                        self.show_snapshot(result['snapshot'])
                    else:
                        self.invalidate()
                    labels = {'verified': '本步已通过实际回读。', 'executed_unverified': '按钮已调用并重新读取，请核对实际结果；未宣称整个任务完成。'}
                    self.status.set(labels.get(result.get('state'), '未核验成功：' + result.get('code', 'unknown')))
        except queue.Empty:
            pass
        finally:
            if not self.closed:
                self.poll_id = self.root.after(100, self.poll)


def main():
    import tkinter as tk
    from tkinter import ttk
    root = tk.Tk()
    root.title('Jarvis · 电脑操作')
    root.geometry('900x780')
    tabs = ttk.Notebook(root)
    tabs.pack(fill='both', expand=True)
    panel = attach_computer_tab(tabs, root)
    def close():
        panel.close()
        root.destroy()
    root.protocol('WM_DELETE_WINDOW', close)
    root.mainloop()
