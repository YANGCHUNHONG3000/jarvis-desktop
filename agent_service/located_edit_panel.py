"""Explicit preview and approval for edits at verified native content matches."""
from copy import deepcopy
import json
from pathlib import Path
import queue
import threading

from . import located_edit_workflow as workflow
from . import windows_located_edit
from .computer_interaction import describe
from .interaction_panel import scalar


def _source(choice):
    # Display only. Backend revalidates the complete immutable reference.
    return json.loads(choice['ref']['token'])['file']['path']


def _complete(state):
    return bool(state and state.get('outcome') and
                state['outcome'].get('ok') is True and state['outcome'].get('state') == 'verified')


class LocatedEditPanel:
    def __init__(self, parent, *, task_root=None, session_root=None,
                 busy=lambda: False, on_busy=lambda value: None, on_artifacts=None):
        import tkinter as tk
        from tkinter import ttk
        self.external_busy, self.on_busy, self.on_artifacts = busy, on_busy, on_artifacts
        self.task_root, self.session_root = task_root, session_root
        self.state = None
        self.rows = []
        self.choices_loaded = False
        self.running = self.closed = self.closing = self.reviewed = False
        self.generation = self.revision = 0
        self.messages = queue.Queue()
        self.stop_event = threading.Event()
        self.poll_id = None
        self.top = tk.Toplevel(parent)
        self.top.title('已定位内容 · 明确范围副本编辑')
        self.top.geometry('900x720')
        self.top.protocol('WM_DELETE_WINDOW', self.close)
        self.status = tk.StringVar(value='正在读取已核验位置。')
        self.instruction = tk.StringVar()
        self.instruction.trace_add('write', self._changed)
        self.pause_after_step = tk.BooleanVar(value=True)
        ttk.Label(self.top, text='勾选已命中位置并填写新值；最多4份文件、合计20处。同文件一次生成副本，原件保留。',
                  wraplength=860).pack(anchor='w', padx=12, pady=8)
        locations = ttk.Frame(self.top)
        locations.pack(fill='x', padx=12)
        self.rows_canvas = tk.Canvas(locations, height=235, highlightthickness=0)
        scrollbar = ttk.Scrollbar(locations, orient='vertical', command=self.rows_canvas.yview)
        scrollbar.pack(side='right', fill='y')
        self.rows_canvas.pack(side='left', fill='x', expand=True)
        self.rows_canvas.configure(yscrollcommand=scrollbar.set)
        self.rows_frame = ttk.Frame(self.rows_canvas)
        content = self.rows_canvas.create_window((0, 0), window=self.rows_frame, anchor='nw')
        self.rows_frame.bind('<Configure>', lambda _: self.rows_canvas.configure(
            scrollregion=self.rows_canvas.bbox('all')))
        self.rows_canvas.bind('<Configure>', lambda event: self.rows_canvas.itemconfigure(
            content, width=event.width))
        ttk.Label(self.top, text='本次任务说明').pack(anchor='w', padx=12, pady=(8, 0))
        self.instruction_entry = ttk.Entry(self.top, textvariable=self.instruction)
        self.instruction_entry.pack(fill='x', padx=12)
        self.preview_text = tk.Text(self.top, height=13, wrap='word', state='disabled')
        self.preview_text.pack(fill='both', expand=True, padx=12, pady=8)
        ttk.Label(self.top, textvariable=self.status, wraplength=860).pack(anchor='w', padx=12)
        actions = ttk.Frame(self.top)
        actions.pack(fill='x', padx=12, pady=10)
        self.preview_button = ttk.Button(actions, text='预览明确范围编辑', command=self.preview)
        self.preview_button.pack(side='left')
        self.execute_button = ttk.Button(actions, text='执行已审预览 / 继续', command=self.execute)
        self.execute_button.pack(side='left', padx=6)
        self.stop_button = ttk.Button(actions, text='停止', command=self.stop)
        self.stop_button.pack(side='left')
        self.pause_button = ttk.Checkbutton(actions, text='每文件完成后暂停', variable=self.pause_after_step)
        self.pause_button.pack(side='left', padx=8)
        self.next_button = ttk.Button(actions, text='副本接下一任务', command=self.next_task)
        self.next_button.pack(side='right')
        self._refresh()
        self.poll_id = self.top.after(80, self.poll)
        if session_root is not None:
            self._start('load', lambda: workflow.load(session_root))
        elif task_root is not None:
            self._start('choices', lambda: windows_located_edit.choices(task_root))
        else:
            self.status.set('缺少已核验任务或持久编辑会话。')

    def _changed(self, *_):
        self.revision += 1
        self.reviewed = False
        if hasattr(self, 'execute_button'):
            self._refresh()

    def _show(self, text):
        self.preview_text.configure(state='normal')
        self.preview_text.delete('1.0', 'end')
        self.preview_text.insert('1.0', text)
        self.preview_text.configure(state='disabled')

    def _choices(self, choices):
        self.choices_loaded = True
        import tkinter as tk
        from tkinter import ttk
        for choice in choices:
            path = _source(choice)
            hit = choice['ref']['match']['hit']
            location = (f"{hit['sheet']}!{hit['address']}" if 'sheet' in hit else
                        f"文字位置 {hit.get('start')}–{hit.get('end')}")
            frame = ttk.Frame(self.rows_frame)
            frame.pack(fill='x', pady=4)
            selected = tk.BooleanVar(value=False)
            selected.trace_add('write', self._changed)
            check = ttk.Checkbutton(frame, variable=selected,
                text=f"{Path(path).name} · {location} · {choice['condition_id']}")
            check.pack(anchor='w')
            ttk.Label(frame, text=f"{path}\n原值：{hit['text']}", wraplength=840).pack(anchor='w')
            value = tk.Text(frame, height=2, wrap='word')
            value.pack(fill='x')
            value.bind('<<Modified>>', lambda event: self._text_changed(event.widget))
            if not choice['editable']:
                ttk.Label(frame, text='不可编辑：'+choice['reason'], wraplength=840).pack(anchor='w')
            self.rows.append({'choice': deepcopy(choice), 'selected': selected,
                              'check': check, 'value': value})
        self.status.set('请选择位置并填写新值；Excel数字按JSON解析，文字数字请加双引号。'
                        if choices else '没有可用的已核验内容命中。')
        self._refresh()

    def _text_changed(self, widget):
        if widget.edit_modified():
            widget.edit_modified(False)
            self._changed()

    def _refresh(self):
        if self.closed:
            return
        blocked = self.running or self.closing or self.external_busy()
        self.preview_button.configure(state='disabled' if blocked else 'normal')
        self.execute_button.configure(state='normal' if not blocked and self.reviewed else 'disabled')
        self.stop_button.configure(state='normal' if self.running and not self.closing else 'disabled')
        self.pause_button.configure(state='disabled' if blocked else 'normal')
        self.next_button.configure(state='normal' if not blocked and _complete(self.state) else 'disabled')
        frozen = blocked or self.session_root is not None
        self.instruction_entry.configure(state='disabled' if frozen else 'normal')
        for row in self.rows:
            disabled = frozen or not row['choice']['editable']
            for name in ('check', 'value'):
                row[name].configure(state='disabled' if disabled else 'normal')

    def _start(self, kind, operation):
        if self.running or self.closed or self.closing or self.external_busy():
            return False
        self.running = True
        self.generation += 1
        generation, revision = self.generation, self.revision
        self.stop_event.clear()
        self.reviewed = False
        self.on_busy(True)
        self._refresh()
        def work():
            try:
                result, error = operation(), None
            except Exception as exc:
                result, error = None, str(exc)
            self.messages.put((generation, kind, revision, result, error))
        try:
            threading.Thread(target=work, name='located-edit-panel', daemon=True).start()
        except Exception as exc:
            self.running = False
            self.on_busy(False)
            self.status.set('无法启动：'+str(exc)+'；当前草稿保留。')
            self._refresh()
            return False
        return True

    def preview(self):
        if self.running or self.closed or self.closing or self.external_busy():
            return False
        if self.session_root is not None:
            root = self.session_root
            return self._start('preview', lambda: workflow.preview(root))
        if not self.choices_loaded and self.task_root is not None:
            return self._start('choices', lambda: windows_located_edit.choices(self.task_root))
        try:
            selections, files = [], set()
            for row in self.rows:
                if not row['selected'].get():
                    continue
                choice = row['choice']
                if not choice['editable']:
                    raise ValueError('公式位置不可编辑。')
                path = _source(choice)
                files.add(str(Path(path)).casefold())
                text = row['value'].get('1.0', 'end-1c')
                after = text if Path(path).suffix.casefold() == '.docx' else scalar(text)
                selections.append({'ref': deepcopy(choice['ref']), 'after': after})
            if not 1 <= len(selections) <= 20 or len(files) > 4:
                raise ValueError('请选择1至4份文件、合计1至20处可编辑位置；同文件可多选。')
            instruction = self.instruction.get()
            if not instruction.strip():
                raise ValueError('请填写本次任务说明。')
        except (ValueError, TypeError) as exc:
            self.status.set(str(exc)+' 当前草稿保留。')
            return False
        parent = self.task_root
        return self._start('preview', lambda: workflow.prepare(parent, selections, instruction))

    def execute(self):
        if not self.reviewed or self.state is None:
            return False
        if self.state.get('readback_preview'):
            root = self.session_root
            approval = self.state['readback_preview']['sha256']
            return self._start('recheck', lambda: workflow.recheck(root,
                approval=approval, stop=self.stop_event.is_set))
        root, plan = self.session_root, deepcopy(self.state['plan'])
        pause = self.pause_after_step.get()
        def progress(*message):
            self.messages.put((generation, 'progress', None, ' '.join(map(str, message)), None))
        generation = self.generation + 1
        return self._start('execute', lambda: workflow.execute(root, plan,
            stop=self.stop_event.is_set, progress=progress, pause_after_step=pause))

    def stop(self):
        if self.running:
            self.stop_event.set()
            self.status.set('已请求停止，等待当前步骤安全结束；记录保留。')

    def next_task(self):
        if not _complete(self.state):
            return False
        root = self.session_root
        return self._start('outputs', lambda: workflow.outputs(root))

    def _handoff(self, records):
        if self.on_artifacts is not None:
            self.on_artifacts(records)
            self.status.set('已核验副本并接入下一任务。')
            return
        import tkinter as tk
        from tkinter import ttk
        from .computer_goal_panel import GoalPanel, MODES
        top = tk.Toplevel(self.top.master)
        top.title('使用编辑副本继续下一任务')
        top.geometry('1000x820')
        tab = ttk.Frame(top)
        tab.pack(fill='both', expand=True)
        panel = GoalPanel(tab, top, busy=self.external_busy, on_busy=self.on_busy)
        panel.mode.set(MODES[9])
        panel.mode_changed()
        top.protocol('WM_DELETE_WINDOW', lambda: (panel.close(), top.destroy()))
        self.next_panel = panel
        panel.prepare_generated_scope(records)
        self.status.set('已打开下一任务并直接绑定核验副本。')

    def poll(self):
        if self.poll_id is not None:
            self.top.after_cancel(self.poll_id)
            self.poll_id = None
        if self.closed:
            return
        while True:
            try:
                generation, kind, revision, result, error = self.messages.get_nowait()
            except queue.Empty:
                break
            if not self.running or generation != self.generation:
                continue
            if kind == 'progress':
                if not self.closing:
                    self.status.set(str(result))
                continue
            self.running = False
            self.on_busy(False)
            if self.closing:
                self._destroy()
                return
            try:
                if error:
                    self.status.set(error+'；当前草稿及原记录保留，请处理后重新预览。')
                elif kind == 'choices':
                    self._choices(result)
                elif kind == 'outputs':
                    if revision == self.revision:
                        self._handoff(result)
                    else:
                        self.status.set('输入已变化，未接入迟到成果；请重新核验。')
                else:
                    self.state = deepcopy(result)
                    self.session_root = result['session_root']
                    if result.get('readback'):
                        self._show('仅重新核验已有副本，不重做编辑、不重生成文件。\n'
                            '原任务的未知/失败回执完整保留；独立复核通过后才允许接下一任务。\n'
                            + '\n'.join(item['path'] for item in result['readback']['records']))
                    else:
                        self._show(describe(result['plan']))
                    self.reviewed = kind == 'preview' and revision == self.revision
                    outcome = result.get('outcome')
                    status = (str(outcome.get('state'))+' · '+str(outcome.get('code', ''))
                              if outcome else '待执行')
                    self.status.set(f'会话：{self.session_root}\n状态：{status}。'+
                        ('已核验，可继续使用副本。' if _complete(result) else
                         '请核对预览后批准；继续只处理原会话剩余步骤。'))
            except Exception as exc:
                self.reviewed = False
                self.status.set(str(exc)+'；草稿及持久记录保留。')
            self._refresh()
        self._refresh()
        self.poll_id = self.top.after(80, self.poll)

    def close(self):
        if self.closed or self.closing:
            return
        if self.running:
            self.closing = True
            self.stop_event.set()
            self.status.set('正在停止并等待后台步骤结束，随后关闭。')
            self._refresh()
        else:
            self._destroy()

    def _destroy(self):
        self.closed = True
        if self.poll_id is not None:
            self.top.after_cancel(self.poll_id)
            self.poll_id = None
        self.top.destroy()


def open_panel(parent, *, task_root=None, session_root=None, busy=lambda: False,
               on_busy=lambda value: None, on_artifacts=None):
    return LocatedEditPanel(parent, task_root=task_root, session_root=session_root,
                            busy=busy, on_busy=on_busy, on_artifacts=on_artifacts)
