"""Explicit local Windows targets, observation, and restart from saved receipts."""
from copy import deepcopy
from pathlib import Path
import queue
import threading

from .computer import ComputerError, same_identity
from . import windows_task

BASE = Path(__file__).resolve().parents[1] / 'data' / 'windows_tasks'
STATES = {'选中': 'selected', '展开': 'expanded', '折叠': 'collapsed', '聚焦': 'focused',
          '输入框内容': 'value', '勾选': 'checked', '取消勾选': 'unchecked'}


def condition_from_control(observation, control_id, state, value='', *, retain=True, number=1):
    controls = [c for c in observation['controls'] if c['id'] == control_id]
    if len(controls) != 1 or state not in STATES.values() or type(retain) is not bool:
        raise ComputerError('windows_target_invalid')
    control = controls[0]
    unchecked = state == 'unchecked'
    if unchecked:
        state = 'checked'
    selector = {k: control[k] for k in ('type', 'name', 'automation_id')}
    try:
        windows_task.locate(observation, selector)
    except ComputerError:
        for ancestor in control.get('ancestors', []):
            candidate = {**selector, 'ancestor': {k: ancestor.get(k, '') for k in ('type', 'name', 'automation_id')}}
            try:
                if windows_task.locate(observation, candidate) == control:
                    selector = candidate
                    break
            except ComputerError:
                continue
        else:
            raise ComputerError('windows_target_ambiguous') from None
    return {'id': f'target_{number}', 'window': deepcopy(observation['window']), 'selector': selector,
            'state': state, 'value': value if state == 'value' else 0 if state == 'collapsed' or unchecked else True,
            'retain': retain if state != 'focused' else False}


def condition_for_find(window, text, *, number=1):
    from .windows_access import FIND_SELECTOR
    if (window.get('profile') != 'observe_only' or window.get('class_name') != 'OpusApp'
            or type(text) is not str or not text or len(text) > 300
            or any(ord(c) < 32 for c in text)):
        raise ComputerError('windows_find_target_invalid')
    return {'id': f'target_{number}', 'window': deepcopy(window), 'selector': deepcopy(FIND_SELECTOR),
            'state': 'value', 'value': text, 'retain': True}


def condition_for_open(window, path, *, number=1, readonly=False):
    from .windows_access import _kind, _file_application
    from .windows_file_dialog import OPEN_DOCUMENT_SELECTOR, encode_token
    if _kind(window) != 'wps' or not _file_application(window, path):
        raise ComputerError('windows_file_application_invalid')
    from .windows_file_actions import READONLY_SELECTOR
    return {'id': f'target_{number}', 'window': deepcopy(window),
            'selector': deepcopy(READONLY_SELECTOR if readonly else OPEN_DOCUMENT_SELECTOR), 'state': 'value',
            'value': encode_token(path), 'retain': True}


def condition_for_content(opened, text, *, number=1, occurrence=1, scope='text'):
    from .windows_file_actions import is_goal
    from .windows_file_dialog import decode_token
    from .windows_content_find import SELECTOR
    from .windows_content_query import encode
    if not is_goal(opened['selector']) or opened['state'] != 'value':
        raise ComputerError('windows_content_source_required')
    snapshot = decode_token(opened['value'])
    return {'id': f'target_{number}', 'window': deepcopy(opened['window']), 'selector': deepcopy(SELECTOR),
            'state': 'value', 'value': encode(snapshot, text, occurrence, scope), 'retain': False}


def conditions_for_copy(window, path, *, number=1, parent=None):
    from .windows_document_copy import prepare
    from .windows_document_lifecycle import SAVE_SELECTOR, CLOSE_SELECTOR
    opened = condition_for_open(window, path, number=number)
    opened['retain'] = False
    token = prepare(path, parent=parent) if parent is not None else prepare(path)
    return [opened, *[{'id': f'target_{number + index}', 'window': deepcopy(window),
        'selector': deepcopy(selector), 'state': 'value', 'value': token, 'retain': True}
        for index, selector in enumerate((SAVE_SELECTOR, CLOSE_SELECTOR), 1)]]


def window_for_copy(backend, original, path, *, preferred=None):
    """Choose a currently registered Office window for an unexecuted draft."""
    from .windows_access import _kind, _file_application
    # This is a shared app chooser; an Explorer goal is not an Office choice.
    if preferred is not None and preferred.get('profile') == 'explorer':
        preferred = None
    candidates = [w for w in backend.list_windows()
                  if _kind(w) == 'wps' and _file_application(w, path)]
    exact = [w for w in candidates if same_identity(w, preferred or original)]
    if len(exact) == 1:
        return deepcopy(exact[0])
    if preferred is not None:
        raise ComputerError('windows_handoff_window_changed')
    if len(candidates) != 1:
        raise ComputerError('windows_handoff_window_required')
    return deepcopy(candidates[0])


def attach_windows_tab(tabs, root, *, busy=lambda: False, on_busy=lambda value: None):
    from tkinter import ttk
    tab = ttk.Frame(tabs, padding=12)
    tabs.add(tab, text='Windows目标')
    panel = WindowsPanel(tab, root, busy=busy, on_busy=on_busy)
    tab.controller = panel
    return panel


class WindowsPanel:
    def __init__(self, tab, root, *, busy=lambda: False, on_busy=lambda value: None, backend=None, task_base=BASE):
        import tkinter as tk
        from tkinter import ttk
        self.root, self.tab, self.external_busy, self.on_busy = root, tab, busy, on_busy
        self.base_busy, self.stage_panel = busy, None
        self.external_busy = lambda: busy() or bool(self.stage_panel and self.stage_panel.running)
        self.backend = backend
        self.task_base = Path(task_base)
        self.windows, self.observation, self.conditions = [], None, []
        self.approved_observations = []
        self.sources = []
        self.saved_task = None
        self.on_artifacts = None
        self.saved_goal = None
        self.saved_choices = []
        self.draft_revision = self.instruction_revision = 0
        self.saved_instruction_revision = None
        self.task_root = None
        self.proposal_task = None
        self.cancel_recheck_context = self.cancel_recheck_receipt = None
        self.running = self.closed = False
        self.stopped = threading.Event()
        self.messages = queue.Queue()
        self.widgets = []
        ttk.Label(tab, text='本地观察资源管理器和WPS，选择控件并指定要达到的状态。支持导航、离屏找回、查找和打开指定本地Word/Excel；'
                  '每步实际回读，未知作用停止。', wraplength=760).pack(anchor='w')
        row = ttk.Frame(tab)
        row.pack(fill='x', pady=5)
        self.window = ttk.Combobox(row, state='readonly', width=58)
        self.window.pack(side='left', fill='x', expand=True)
        self.window.bind('<<ComboboxSelected>>', self.window_changed)
        self.widgets.append((self.window, 'readonly'))
        self.button(row, '刷新窗口', self.refresh).pack(side='left')
        self.button(row, '观察控件', self.observe).pack(side='left')
        row = ttk.Frame(tab)
        row.pack(fill='x', pady=3)
        self.button(row, '试用 Winapp 办公', self.use_winapp).pack(side='left')
        self.button(row, 'Windows-MCP 试用', self.use_mcp).pack(side='left')
        self.button(row, '自动选择执行方式', self.use_auto).pack(side='left')
        self.button(row, '视觉导航目标', self.add_visual).pack(side='left')
        self.tree = ttk.Treeview(tab, columns=('type', 'name', 'value'), show='headings', height=7)
        for key, label, width in [('type', '类型', 100), ('name', '控件', 290), ('value', '当前内容/状态', 300)]:
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width)
        self.tree.pack(fill='both', expand=True)
        row = ttk.Frame(tab)
        row.pack(fill='x', pady=5)
        self.state = ttk.Combobox(row, state='readonly', values=tuple(STATES), width=13)
        self.state.set('选中')
        self.state.pack(side='left')
        self.widgets.append((self.state, 'readonly'))
        self.state.bind('<<ComboboxSelected>>', self.state_changed)
        self.value = ttk.Entry(row, width=28)
        self.value.pack(side='left', fill='x', expand=True, padx=5)
        self.widgets.append((self.value, 'normal'))
        self.retain = tk.BooleanVar(value=True)
        self.retain_button = ttk.Checkbutton(row, text='后续保持此状态', variable=self.retain)
        self.retain_button.pack(side='left')
        self.widgets.append((self.retain_button, 'normal'))
        self.button(row, '添加目标', self.add_condition).pack(side='left')
        self.button(row, '添加WPS查找目标', self.add_find).pack(side='left', padx=4)
        self.targets = tk.Listbox(tab, height=4)
        self.targets.pack(fill='x')
        row = ttk.Frame(tab)
        row.pack(fill='x', pady=3)
        self.content_source = ttk.Combobox(row, state='readonly', width=42)
        self.content_source.pack(side='left', fill='x', expand=True)
        self.widgets.append((self.content_source, 'readonly'))
        self.content_scope = ttk.Combobox(row, state='readonly', values=('内容', '公式'), width=6)
        self.content_scope.set('内容')
        self.content_scope.pack(side='left', padx=4)
        self.widgets.append((self.content_scope, 'readonly'))
        ttk.Label(row, text='第').pack(side='left')
        self.content_occurrence = ttk.Spinbox(row, from_=1, to=100, width=4)
        self.content_occurrence.set('1')
        self.content_occurrence.pack(side='left')
        self.widgets.append((self.content_occurrence, 'normal'))
        ttk.Label(row, text='处；关键词填上方输入框').pack(side='left')
        self.button(row, '添加内容定位目标', self.add_content_find).pack(side='left', padx=4)
        row = ttk.Frame(tab)
        row.pack(fill='x', pady=5)
        self.button(row, '移除末项', self.remove_last).pack(side='left')
        self.button(row, '清空目标', self.clear).pack(side='left', padx=5)
        self.button(row, '添加打开Office目标', self.add_open).pack(side='left', padx=5)
        self.button(row, '占用时只读打开', lambda: self.add_open(readonly=True)).pack(side='left')
        self.button(row, '另存副本并关闭', self.add_copy).pack(side='left', padx=5)
        ttk.Label(row, text='不同窗口可依次观察后添加；聚焦只在本步核验。').pack(side='left')
        row = ttk.Frame(tab)
        row.pack(fill='x', pady=3)
        self.copy_choice = ttk.Combobox(row, state='readonly', width=62)
        self.copy_choice.pack(side='left', fill='x', expand=True)
        self.widgets.append((self.copy_choice, 'readonly'))
        self.button(row, '副本接入下一任务', self.next_copy).pack(side='left', padx=4)
        self.button(row, '编辑已定位内容', self.edit_located).pack(side='left', padx=4)
        self.instruction_var = tk.StringVar(value='完成上述Windows目标')
        self.instruction = ttk.Entry(tab, textvariable=self.instruction_var)
        self.instruction.pack(fill='x', pady=4)
        self.instruction_var.trace_add('write', lambda *_: self.instruction_changed())
        self.widgets.append((self.instruction, 'normal'))
        row = ttk.Frame(tab)
        row.pack(fill='x', pady=4)
        self.button(row, '按指令生成目标（云端）', self.propose).pack(side='left')
        self.button(row, '分阶段推进／找回', self.open_stages).pack(side='left', padx=6)
        ttk.Label(tab, text='点击上方按钮将上传指令，以及最近明确观察的最多两个窗口中的获准控件标签和状态选项。'
                  '沿用当前本地费用设置与共享账本；生成后仍需核对、预览并批准执行。', wraplength=760).pack(anchor='w')
        row = ttk.Frame(tab)
        row.pack(fill='x', pady=5)
        self.button(row, '预览目标（本地）', self.preview).pack(side='left')
        self.execute_button = self.button(row, '执行预览目标', self.execute)
        self.execute_button.pack(side='left', padx=5)
        self.execute_button.configure(state='disabled')
        self.pause = tk.BooleanVar(value=False)
        pause_button = ttk.Checkbutton(row, text='每个目标后暂停', variable=self.pause)
        pause_button.pack(side='left')
        self.widgets.append((pause_button, 'normal'))
        ttk.Button(row, text='停止', command=self.stop).pack(side='left', padx=5)
        self.resume_button = self.button(row, '重新观察并继续', self.resume)
        self.resume_button.pack(side='left')
        self.resume_button.configure(state='disabled')
        self.button(row, '仅复核当前视觉页面', self.recheck_visual).pack(side='left', padx=4)
        row = ttk.Frame(tab)
        row.pack(fill='x', pady=3)
        self.cancel_recheck_button = self.button(row, '只读复核取消结果', self.recheck_cancel)
        self.cancel_recheck_button.pack(side='left')
        self.cancel_next_button = self.button(row, '复核后开始下一任务', self.next_after_cancel)
        self.cancel_next_button.pack(side='left', padx=5)
        self.control_recheck_buttons()
        row = ttk.Frame(tab)
        row.pack(fill='x', pady=5)
        self.task_path = tk.StringVar(value='尚无保存任务')
        ttk.Label(row, textvariable=self.task_path, wraplength=590).pack(side='left', fill='x', expand=True)
        self.button(row, '找回任务', self.restore).pack(side='left')
        self.button(row, '打开保存副本', self.open_copy).pack(side='left', padx=4)
        self.status = tk.StringVar(value='新任务默认按目标自动选择现有执行方式；先观察窗口、生成待审方案，确认后执行。')
        ttk.Label(tab, textvariable=self.status, wraplength=760).pack(anchor='w')
        self.poll_id = root.after(100, self.poll)

    def button(self, parent, text, command):
        from tkinter import ttk
        button = ttk.Button(parent, text=text, command=command)
        self.widgets.append((button, 'normal'))
        return button

    def invalidate(self):
        self.draft_revision += 1
        self.task_root = None
        self.task_path.set('目标有变化，请重新预览。')
        self.execute_button.configure(state='disabled')
        self.execute_button.configure(text='执行预览目标')
        self.resume_button.configure(state='disabled')
        self.control_recheck_buttons()

    def control_recheck_buttons(self):
        context = self.cancel_recheck_context
        self.cancel_recheck_button.configure(state='normal' if context else 'disabled')
        self.cancel_next_button.configure(state='normal' if context and self.cancel_recheck_receipt
            and self.conditions == context['conditions'] else 'disabled')

    def configure_cancel_recheck(self, summary):
        self.cancel_recheck_context = self.cancel_recheck_receipt = None
        if self.task_root is not None and summary.get('state') == 'unknown':
            from . import windows_control_recovery as recovery
            try:
                recovery.inspect(self.task_root)
                receipt = recovery.latest(self.task_root)
            except ComputerError:
                pass
            else:
                self.cancel_recheck_context = {'parent': Path(self.task_root),
                    'conditions': deepcopy(self.conditions), 'instruction': self.instruction.get(),
                    'instruction_revision': self.instruction_revision}
                self.cancel_recheck_receipt = Path(receipt['root']) if receipt else None
        self.control_recheck_buttons()

    def recheck_cancel(self):
        if self.running or self.external_busy() or self.cancel_recheck_context is None:
            return
        from . import windows_control_recovery as recovery
        parent = self.cancel_recheck_context['parent']
        def read_only(_backend):
            if self.stopped.is_set():
                raise ComputerError('windows_control_recheck_cancelled')
            receipt = recovery.latest(parent)
            if receipt is None:
                # Production proof owns its read-only worker; a UI-injected backend
                # cannot create follow-up authority.
                receipt = recovery.run(parent, stop=self.stopped.is_set)
            if self.stopped.is_set():
                raise ComputerError('windows_control_recheck_cancelled')
            return {'parent': parent, 'receipt': receipt}
        self.start('cancel_recheck', read_only)

    def next_after_cancel(self):
        context, receipt = self.cancel_recheck_context, self.cancel_recheck_receipt
        if self.running or self.external_busy() or context is None or receipt is None:
            return
        if self.conditions != context['conditions']:
            self.status.set('已有新的操作目标，未清空或覆盖；请继续当前草稿。')
            return
        revision = self.draft_revision
        clear = (self.instruction_revision == context['instruction_revision']
                 and self.instruction.get() == context['instruction'])
        def fresh_context(backend):
            from .windows_control_recovery import verified_window
            window = verified_window(receipt)
            observed = backend.observe(window, stop=self.stopped.is_set)
            if (self.stopped.is_set() or type(observed) is not dict or observed.get('window') != window
                    or observed.get('application') != 'wps' or observed.get('scope') != 'visible_controls'
                    or observed.get('truncated') is not False or observed.get('blocked_reason')
                    or observed.get('dialogs') != []):
                raise ComputerError('windows_control_recheck_next_observation_invalid')
            windows_task._observation(observed, window)
            if verified_window(receipt) != window:
                raise ComputerError('windows_control_recheck_source_changed')
            return {'parent': context['parent'], 'receipt': receipt, 'revision': revision,
                    'clear_instruction': clear, 'observation': observed}
        self.start('cancel_next', fresh_context)

    def instruction_changed(self):
        self.instruction_revision += 1
        self.invalidate()

    def remember_saved_task(self, summary):
        from .windows_document_lifecycle import SAVE_SELECTOR
        from .windows_file_dialog import OPEN_DOCUMENT_SELECTOR
        from .windows_file_actions import READONLY_SELECTOR
        # A restored unrelated/unfinished task must not keep an old task's menu.
        self.saved_task = self.saved_goal = self.saved_instruction_revision = None
        self.saved_choices = []
        self.copy_choice.configure(values=[])
        self.copy_choice.set('')
        inherited = {s['target_id']: s for s in self.sources} if summary.get('state') == 'verified' else {}
        choices = [deepcopy(c) for c in self.conditions
                   if c['id'] in summary.get('completed', []) and
                   (c['selector'] == SAVE_SELECTOR or
                    (c['id'] in inherited and c['selector'] in (OPEN_DOCUMENT_SELECTOR, READONLY_SELECTOR)))]
        if self.task_root is None or not choices or summary.get('state') == 'unknown':
            return
        self.saved_task = self.task_root
        self.saved_goal = {'instruction': self.instruction.get(), 'conditions': deepcopy(self.conditions)}
        self.saved_choices = choices
        self.saved_instruction_revision = self.instruction_revision
        self.copy_choice.configure(values=[f"{c['id']} · " +
            (Path(inherited[c['id']]['final']['path']).name if c['id'] in inherited else c['window']['title']) + ' · ' +
            ('已保存副本' if c['selector'] == SAVE_SELECTOR else '已核验接续副本') for c in choices])
        self.copy_choice.set('')
        if len(choices) == 1:
            self.copy_choice.current(0)

    def edit_located(self):
        if self.running or self.external_busy():
            return
        if self.saved_task is None:
            self.status.set('请先找回已核验内容命中的Windows任务。')
            return
        from .located_edit_panel import open_panel
        self.located_editor = open_panel(self.root, task_root=self.saved_task,
            busy=lambda: self.running or self.external_busy(), on_busy=self.on_busy,
            on_artifacts=self.on_artifacts)

    def next_copy(self):
        if self.running or self.external_busy():
            return
        index = self.copy_choice.current()
        if self.saved_task is None or not 0 <= index < len(self.saved_choices):
            self.status.set('请先找回已保存副本的任务，并选择要接续的成果。')
            return
        if self.task_root is not None and self.task_root != self.saved_task:
            self.status.set('当前已有另一份任务预览；先保留该任务并明确编辑草稿，接续不会覆盖预览。')
            return
        def from_parent(c):
            # A just-staged carried open can match the old open byte-for-byte.
            # Its new binding identifies it as this draft, not a parent replay.
            if any(s['target_id'] == c['id'] and s['parent_root'] == str(self.saved_task) for s in self.sources):
                return False
            return any(same_identity(c['window'], old['window'])
                       and all(c[key] == old[key] for key in ('selector', 'state', 'value'))
                       for old in self.saved_goal['conditions'])
        # A user may already have appended new goals or removed one old goal.
        # Do not mistake that mixed list for an entirely new draft and replay
        # the remaining parent OPEN/SAVE/CLOSE sequence in a fresh task.
        base = deepcopy([c for c in self.conditions if not from_parent(c)])
        original = not base
        ids = {c['id'] for c in base}
        sources = deepcopy([s for s in self.sources if s['target_id'] in ids])
        if len(base) >= 12:
            self.status.set('每个任务最多12个目标；新草稿已保留。')
            return
        chosen = deepcopy(self.saved_choices[index])
        parent = Path(self.saved_task)
        if any(s['parent_root'] == str(parent) and s.get('save_id', s.get('source_id')) == chosen['id'] for s in sources):
            self.status.set('该副本已在当前草稿中；可添加其他窗口目标，无需重复接入。')
            return
        revision = self.draft_revision
        window_index = self.window.current()
        preferred = deepcopy(self.windows[window_index]) if 0 <= window_index < len(self.windows) else None
        clear_instruction = original and self.instruction_revision == self.saved_instruction_revision
        used = {c['id'] for c in base}
        number = next(n for n in range(1, 13) if f'target_{n}' not in used)
        def prepare(backend):
            from .windows_handoff import capture, verify
            ref = capture(parent, chosen['id'], target_id=f'target_{number}')
            window = window_for_copy(backend, chosen['window'], ref['final']['path'], preferred=preferred)
            condition = condition_for_open(window, ref['final']['path'], number=number)
            conditions = [*base, condition]
            refs = [*sources, ref]
            verify({'schema': 1, 'instruction': '接续已核验保存副本',
                    'conditions': conditions, 'max_steps': 12}, refs)
            return {'revision': revision, 'parent': parent, 'choice_id': chosen['id'],
                    'conditions': conditions, 'sources': refs, 'clear_instruction': clear_instruction}
        self.start('handoff', prepare)

    def window_changed(self, _=None):
        if self.running:
            return
        self.observation = None
        self.tree.delete(*self.tree.get_children())

    def state_changed(self, _=None):
        if self.state.get() == '聚焦':
            self.retain.set(False)
            self.retain_button.configure(state='disabled')
        else:
            self.retain_button.configure(state='normal')

    def start(self, kind, operation):
        if self.running or self.external_busy():
            self.status.set('已有任务正在运行，请先等待或停止。')
            return
        self.running = True
        self.stopped.clear()
        self.on_busy(True)
        for widget, _ in self.widgets:
            widget.configure(state='disabled')
        self.status.set('正在本地观察或核验……')
        def work():
            try:
                if self.backend is None:
                    from .windows_access import AutoWindowsBackend
                    self.backend = AutoWindowsBackend()
                result = operation(self.backend)
                self.messages.put((kind, result, None))
            except Exception as exc:
                code = str(exc) if isinstance(exc, ComputerError) else 'windows_operation_failed'
                self.messages.put((kind, None, code))
        try:
            threading.Thread(target=work, daemon=True).start()
        except Exception:
            self.running = False
            self.on_busy(False)
            self.enable_widgets()
            self.status.set('无法启动本地操作。')

    def enable_widgets(self):
        for widget, state in self.widgets:
            widget.configure(state=state)
        self.execute_button.configure(state='disabled')
        self.resume_button.configure(state='disabled')
        self.state_changed()
        self.control_recheck_buttons()

    def refresh(self):
        self.start('windows', lambda backend: backend.list_windows(stop=self.stopped.is_set))

    def observe(self):
        index = self.window.current()
        if not 0 <= index < len(self.windows):
            self.status.set('请先刷新并选择窗口。')
            return
        window = deepcopy(self.windows[index])
        self.start('observe', lambda backend: backend.observe(window, stop=self.stopped.is_set))

    def add_visual(self):
        """One data-defined navigation goal, sharing ordinary durable recovery."""
        from tkinter import simpledialog
        from .windows_visual import condition, LABELS
        if self.running or self.external_busy():
            return
        index = self.window.current()
        if not 0 <= index < len(self.windows) or len(self.conditions) >= 12:
            self.status.set('请先刷新并选择窗口；每个任务最多12个目标。')
            return
        window = deepcopy(self.windows[index])
        label = simpledialog.askstring('视觉导航', '要进入哪个顶部页签？例如：视图、审阅、查看。', parent=self.root)
        if label is None:
            return
        if label.strip() not in LABELS:
            self.status.set('当前视觉操作只支持顶部导航页签，不开放任意按钮。')
            return
        expected = simpledialog.askstring('目标界面核验',
            '进入后必须看到的2～4项独立文字，用中文逗号分隔。截图和识别仅在本机处理。', parent=self.root)
        if expected is None:
            return
        try:
            target = condition(window, label.strip(), [x.strip() for x in expected.replace(',', '，').split('，')],
                               number=len(self.conditions)+1)
            self.conditions.append(target)
            self.invalidate()
            self.show_targets()
            self.status.set('已添加本地视觉导航目标；预览批准后将切到所选窗口、重新观察并核验目标界面。')
        except ComputerError as exc:
            self.status.set('未添加：'+str(exc))

    def add_condition(self):
        if self.running:
            return
        selected = self.tree.selection()
        if self.observation is None or not selected:
            self.status.set('请先观察并选择控件。')
            return
        if len(self.conditions) >= 12:
            self.status.set('每个任务最多12个目标。')
            return
        try:
            target = condition_from_control(self.observation, selected[0], STATES[self.state.get()],
                self.value.get(), retain=self.retain.get(), number=len(self.conditions) + 1)
            goal = {'schema': 1, 'instruction': self.instruction.get(),
                    'conditions': [*self.conditions, target], 'max_steps': 12}
            windows_task.validate_goal(goal)
            candidates = self.backend.candidates(self.observation, target)
            if not windows_task.satisfied(self.observation, target) and not candidates:
                raise ComputerError('windows_no_safe_action')
            self.conditions.append(target)
            self.invalidate()
            self.show_targets()
            self.status.set('目标已添加；预览后执行。')
        except Exception as exc:
            self.status.set('无法添加：' + (str(exc) if isinstance(exc, ComputerError) else 'windows_target_invalid'))

    def show_targets(self):
        self.targets.delete(0, 'end')
        for index, target in enumerate(self.conditions, 1):
            value = f" = {target['value']}" if target['state'] == 'value' else (' = 勾选' if target['value'] else ' = 取消勾选') if target['state']=='checked' else ''
            from .windows_file_actions import is_goal
            if is_goal(target['selector']):
                import json
                value = ' = ' + json.loads(target['value'])['path']
            from .windows_document_lifecycle import is_goal as lifecycle_goal
            from .windows_content_find import is_goal as content_goal
            if content_goal(target['selector']):
                import json
                request = json.loads(target['value'])
                value = f" = {Path(request['file']['path']).name} / {request['scope']} / {request['query']} / 第{request['occurrence']}处"
            if lifecycle_goal(target['selector']):
                import json
                value = ' = ' + json.loads(target['value'])['directory'] + '（核验后直接打开副本）'
            from .windows_visual import is_goal as visual_goal, decode
            if visual_goal(target['selector']):
                decoded=decode(target['selector'],target['value'])
                value = ('；视觉导航，核验唯一选中标记及至少两处实际工具文字' if decoded.get('selected')
                         else '；视觉导航，需实际读回：' + '、'.join(decoded['all']))
            keep = '，后续保持' if target['retain'] else ''
            from .windows_visual_forms import is_goal as form_goal, review_text
            if form_goal(target['selector']):value='；'+review_text(target['selector'],target['value'])
            from .windows_reviewed_controls import is_goal as reviewed_goal, review_text as reviewed_text
            if reviewed_goal(target['selector']):value='；'+reviewed_text(target['selector'],target['value'])
            self.targets.insert('end', f"{index}. {target['window']['title']} / {target['selector']['name']} → {target['state']}{value}{keep}")
        from .windows_file_actions import is_goal
        import json
        choices = [c for c in self.conditions if is_goal(c['selector'])]
        self.content_source.configure(values=[f"{c['id']} · {Path(json.loads(c['value'])['path']).name}" for c in choices])
        self.content_source.set('')
        if len(choices) == 1:
            self.content_source.current(0)

    def add_content_find(self):
        if self.running or self.external_busy():
            return
        from .windows_file_actions import is_goal
        choices = [c for c in self.conditions if is_goal(c['selector'])]
        index = self.content_source.current()
        try:
            if not 0 <= index < len(choices):
                raise ComputerError('windows_content_source_required')
            used = {c['id'] for c in self.conditions}
            if len(used) >= 12:
                raise ComputerError('windows_goal_invalid')
            number = next(n for n in range(1, 13) if f'target_{n}' not in used)
            target = condition_for_content(choices[index], self.value.get(), number=number,
                occurrence=int(self.content_occurrence.get()), scope='formula' if self.content_scope.get() == '公式' else 'text')
            updated = deepcopy(self.conditions)
            # An open is a prerequisite; later search/app goals may change tabs.
            for condition in updated:
                if condition['id'] == choices[index]['id']:
                    condition['retain'] = False
            updated.append(target)
            windows_task.validate_goal({'schema': 1, 'instruction': self.instruction.get(),
                                       'conditions': updated, 'max_steps': 12})
            self.conditions = updated
            self.invalidate()
            self.show_targets()
            self.status.set('已添加实际命中目标：核对文字/公式、文件与位置后才算完成；未找到会停止。不会修改或保存文件。')
        except (ComputerError, ValueError) as exc:
            code = str(exc) if isinstance(exc, ComputerError) else 'windows_content_query_invalid'
            self.status.set('无法添加内容定位：' + code + '。请选已有打开目标、填写字面关键词及1–100命中序号。')

    def add_find(self):
        if self.running:
            return
        index = self.window.current()
        if not 0 <= index < len(self.windows):
            self.status.set('请先选择WPS主窗口，并在输入框填写查找文字。')
            return
        try:
            target = condition_for_find(self.windows[index], self.value.get(), number=len(self.conditions) + 1)
            windows_task.validate_goal({'schema': 1, 'instruction': self.instruction.get(),
                                       'conditions': [*self.conditions, target], 'max_steps': 12})
            self.conditions.append(target)
            self.invalidate()
            self.show_targets()
            self.status.set('WPS查找目标已添加：执行时自动打开查找框，再填入文字。')
        except ComputerError as exc:
            self.status.set('无法添加：' + str(exc))

    def add_open(self, *, readonly=False):
        if self.running or self.external_busy():
            return
        index = self.window.current()
        from .windows_access import _kind
        try:
            if not 0 <= index < len(self.windows) or _kind(self.windows[index]) != 'wps':
                raise ComputerError('windows_file_application_invalid')
            from tkinter import filedialog
            path = filedialog.askopenfilename(parent=self.root, title='选择要在对应WPS窗口打开的已有Office文件',
                                              filetypes=[('Word或Excel', '*.docx *.xlsx')])
            if not path:
                return
            target = condition_for_open(self.windows[index], path, number=len(self.conditions) + 1, readonly=readonly)
            windows_task.validate_goal({'schema': 1, 'instruction': self.instruction.get(),
                                       'conditions': [*self.conditions, target], 'max_steps': 12})
            self.conditions.append(target)
            self.invalidate()
            self.show_targets()
            self.status.set('已固定文件指纹；占用时只选择已核验的只读打开。' if readonly else
                            '已固定文件路径与内容指纹；执行时识别文件框，打开后核验完整路径。文件变化会停止。')
        except ComputerError as exc:
            self.status.set('无法添加打开文件目标：' + str(exc))

    def add_copy(self):
        if self.running or self.external_busy():
            return
        index = self.window.current()
        try:
            if not 0 <= index < len(self.windows) or len(self.conditions) > 9:
                raise ComputerError('windows_copy_target_invalid')
            from tkinter import filedialog
            path = filedialog.askopenfilename(parent=self.root, title='选择已有Office文件：另存新副本后关闭',
                                              filetypes=[('Word或Excel', '*.docx *.xlsx')])
            if not path:
                return
            added = conditions_for_copy(self.windows[index], path, number=len(self.conditions) + 1)
            updated = deepcopy(self.conditions)
            from .windows_file_actions import is_goal
            import json
            for condition in updated:
                if (is_goal(condition['selector']) and same_identity(condition['window'], self.windows[index])
                        and Path(json.loads(condition['value'])['path']) == Path(path)):
                    condition['retain'] = False
            updated.extend(added)
            windows_task.validate_goal({'schema': 1, 'instruction': self.instruction.get(),
                                       'conditions': updated, 'max_steps': 12})
            self.conditions = updated
            self.invalidate()
            self.show_targets()
            self.status.set('已添加打开→另存新副本→关闭；原件保留，相同文件的打开状态只当步核验。保存完成后可暂停。')
        except ComputerError as exc:
            self.status.set('无法添加保存关闭目标：' + str(exc))

    def open_copy(self):
        if self.running or self.external_busy():
            return
        try:
            from .windows_document_lifecycle import SAVE_SELECTOR
            from .windows_document_copy import saved_receipt
            from .word_open import request_open_word, WordOpenError
            target = next((c for c in reversed(self.conditions) if c['selector'] == SAVE_SELECTOR), None)
            if target is None:
                raise ComputerError('windows_copy_receipt_required')
            receipt = saved_receipt(target['value'])
            if receipt is None:
                raise ComputerError('windows_copy_receipt_required')
            if Path(receipt['final']['path']).suffix.casefold() == '.xlsx':
                from .workbook_open import request_open_workbook
                request_open_workbook(receipt['final']['path'])
            else:
                request_open_word(receipt['final']['path'])
            self.status.set('已请求打开核验过的保存副本。')
        except ComputerError as exc:
            self.status.set('副本尚不能打开：' + str(exc))
        except WordOpenError as exc:
            self.status.set('副本已保留，打开失败：' + str(exc))

    def remove_last(self):
        if not self.running and self.conditions:
            self.conditions.pop()
            ids = {c['id'] for c in self.conditions}
            self.sources = [s for s in self.sources if s['target_id'] in ids]
            self.invalidate()
            self.show_targets()

    def clear(self):
        if not self.running:
            self.conditions.clear()
            self.proposal_task = None
            self.sources.clear()
            self.invalidate()
            self.show_targets()

    def preview(self):
        goal = {'schema': 1, 'instruction': self.instruction.get(),
                'conditions': deepcopy(self.conditions), 'max_steps': 12}
        sources = deepcopy(self.sources)
        if self.proposal_task is not None:
            try:
                loaded=windows_task.load(self.proposal_task)
                if loaded['goal']!=goal:raise ComputerError('windows_visual_proposal_changed')
                self.task_root=loaded['root']
                self.task_path.set(str(self.task_root))
                self.show_outcome(loaded['summary'])
            except ComputerError:
                self.status.set('方案内容已变化，请重新生成并审阅；不会沿用旧批准或改写原任务。')
            return
        self.start('preview', lambda backend: windows_task.create(self.task_base, goal,
                   **({'sources': sources} if sources else {}),
                   **({'provider_binding': backend.provider_binding} if getattr(backend, 'provider_binding', None) else {})))

    def use_winapp(self):
        if self.running or self.external_busy():
            return
        if self.conditions or self.sources or self.task_root is not None:
            self.status.set('请保留当前任务；Winapp试用仅用于没有目标和来源的新草稿。找回任务会沿用其原后端。')
            return
        from .windows_access import WinappOfficeBackend
        self.backend = WinappOfficeBackend()
        self.observation, self.approved_observations = None, []
        self.status.set('已选择 Winapp 0.7.1 试用：仅受控状态操作，仍需预览确认；请重新观察窗口。')

    def use_auto(self):
        if self.running or self.external_busy():
            return
        if (self.conditions or self.sources or self.task_root is not None
                or self.proposal_task is not None or self.saved_task is not None):
            self.status.set('当前任务保留原执行方式；请在没有目标和来源的新草稿中切换。')
            return
        from .windows_access import AutoWindowsBackend
        self.backend = AutoWindowsBackend()
        self.observation, self.approved_observations = None, []
        self.tree.delete(*self.tree.get_children())
        self.status.set('已选择自动方式：按可核验目标选择原生、Winapp 或 MCP，失败不换工具重复执行；请重新观察。')

    def use_mcp(self):
        if self.running or self.external_busy():
            return
        if (self.conditions or self.sources or self.task_root is not None
                or self.proposal_task is not None or self.saved_task is not None):
            self.status.set('请保留当前任务；Windows-MCP试用仅用于没有目标和来源的新草稿。找回任务会沿用其原后端。')
            return
        from .windows_access import WindowsMcpBackend
        self.backend = WindowsMcpBackend()
        self.observation, self.approved_observations = None, []
        self.tree.delete(*self.tree.get_children())
        self.status.set('已选择 Windows-MCP 试用：仅复选框和安全选项卡/单选项，输入请继续使用Winapp；请重新观察窗口并预览确认。')

    def open_stages(self):
        if self.stage_panel is not None and not self.stage_panel.closed:
            self.stage_panel.top.lift()
            return
        if self.running or self.external_busy():
            return
        if self.sources:
            self.status.set('当前草稿绑定了办公成果；请保留现有接续任务，分阶段入口仅使用明确观察的窗口。')
            return
        from .windows_stages_panel import open_panel
        self.stage_panel = open_panel(self.root, instruction=self.instruction.get(),
            observations=deepcopy(self.approved_observations),
            busy=lambda: self.running or self.base_busy(), on_busy=self.on_busy, task_base=self.task_base,
            backend=self.backend)

    def propose(self):
        if self.running:
            return
        if self.sources:
            self.status.set('当前草稿已绑定保存成果；请观察并添加其他窗口目标，接续来源不会被新提案替换。')
            return
        if (self.observation is None or not self.approved_observations
                or self.observation.get('truncated') or self.observation.get('blocked_reason')):
            self.status.set('请先明确观察当前选择的窗口，再按指令生成目标。')
            return
        instruction = self.instruction.get()
        revision = self.draft_revision
        observations = deepcopy(self.approved_observations)
        def generate(backend):
            from .windows_visual_proposal import propose
            from .model import CloudClient
            client = CloudClient(should_stop=self.stopped.is_set)
            binding = getattr(backend, 'provider_binding', None)
            result=propose(instruction, observations, backend, client, stop=self.stopped.is_set,task_base=self.task_base,
                **({'task_create_kwargs': {'provider_binding': binding}} if binding else {}))
            return {**result,'instruction':instruction,'revision':revision}
        self.start('propose', generate)

    def execute(self):
        if self.task_root is None:
            return
        task_root, pause = self.task_root, self.pause.get()
        def execute_approved(backend):
            from .windows_access import backend_for_task
            backend = backend_for_task(task_root, fallback=backend)
            self.backend = backend
            from .windows_visual_proposal import approve, restore_task
            if self.stopped.is_set():raise ComputerError('windows_task_cancelled')
            loaded=restore_task(task_root,backend,stop=self.stopped.is_set,task_base=self.task_base)
            if loaded.get('migrated_from'):
                return {'migration':loaded}
            approve(task_root)
            return windows_task.run(task_root,backend,stop=self.stopped.is_set,pause_after_step=pause)
        self.start('run', execute_approved)

    def resume(self):
        self.execute()

    def recheck_visual(self):
        if self.running or self.external_busy():
            return
        if self.task_root is None:
            self.status.set('请先找回原视觉失败任务；此入口仅独立复核当前页面，不点击、不重放。')
            return
        from .windows_visual_recovery import run
        original = self.task_root
        self.start('visual_recheck', lambda backend: run(original, stop=self.stopped.is_set))

    def restore(self):
        from tkinter import filedialog
        if self.running or self.external_busy():
            return
        directory = filedialog.askdirectory(parent=self.root, title='选择已保存的Windows目标任务目录',
                                            initialdir=str(self.task_base))
        if not directory:
            return
        def inspect(backend):
            from .windows_access import backend_for_task
            backend = backend_for_task(directory, fallback=backend)
            self.backend = backend
            from .windows_visual_proposal import restore_task
            return restore_task(directory,backend,stop=self.stopped.is_set,task_base=self.task_base)
        self.start('restore', inspect)

    def stop(self):
        self.stopped.set()
        self.status.set('已请求停止。已核验步骤保留；继续前重新观察，未知作用不重放。')

    def close(self):
        self.closed = True
        self.stopped.set()
        if self.stage_panel is not None:
            self.stage_panel.close()
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
                self.enable_widgets()
                if error:
                    detail = {
                        'windows_handoff_source_changed': '上一任务的来源或回执已变化，接续已停止；旧任务和新草稿均保留。',
                        'windows_handoff_target_changed': '当前打开目标已不是绑定的保存副本，请重新选择接续成果并预览。',
                        'windows_handoff_source_unknown': '上一任务含未确认作用，不能自动接续或重放；请先核对原任务。',
                        'windows_handoff_save_required': '该任务尚无已完成并核验的保存步骤，不能将它作为下一任务来源。',
                        'windows_handoff_source_incomplete': '上一轮接续任务尚未全部核验完成，请先找回并继续原任务；已完成步骤不会重复。',
                        'windows_handoff_source_required': '该打开目标没有已核验副本的来源绑定，不能自动接续；原任务保留。',
                        'windows_visual_recheck_ineligible': '只可复核最后一个视觉目标已点击但缺少结果证明的任务；其他未知作用不复核、不重放。',
                        'windows_visual_recheck_source_changed': '原任务证据发生变化，独立复核无效；原任务不改写。',
                        'windows_control_recheck_ineligible': '仅可只读复核最后一步取消结果未知、前面步骤均完成的受审文件框任务；不会重放动作。',
                        'windows_control_recheck_source_changed': '原任务或复核来源已变化，接续已停止；旧记录和新草稿保留。',
                        'windows_control_recheck_native_required': '缺少完整原生复核回执，不能接入下一任务。',
                        'windows_control_recheck_next_observation_invalid': '原窗口不可用、出现弹窗或观察不完整；未创建下一任务，也未清空草稿。',
                        'windows_visual_approval_required': '视觉方案仍待审阅；请核对目标及自动核验文字，确认后再执行。',
                        'owned_window_requires_manual_attention': '所选窗口有弹窗，请先在本地处理，再重新生成方案；本次未调用模型、未执行操作。',
                        'windows_application_blocked': '所选窗口有阻塞弹窗，请先在本地处理；原任务和草稿保留。',
                        'windows_visual_proposal_changed': '方案或来源证据已变化，请重新生成审阅；旧任务保留，不执行旧批准。',
                        'windows_visual_selection_unavailable': '当前工具栏缺少可唯一核验的选中标记或两处工具文字；未执行点击。请展开工具栏后重新找回任务，已完成步骤保留。',
                        'windows_form_label_unproven': '输入框或按钮的视觉标签未与实际控件位置唯一对应；没有执行。请让查找窗口完整显示后重新观察。',
                        'windows_visual_form_popup_requires_attention': '出现未批准的弹窗，已暂停且不点击。请在本地处理弹窗后继续，已完成步骤不会重做。',
                        'windows_visual_form_dialog_changed': '原查找窗口已被替换，不能沿用旧方案操作同名新窗口；旧任务和回执保留。',
                        'windows_visual_form_dialog_missing': '原查找窗口不可用；未重放已完成输入。请核对窗口后再继续，必要时生成新的待审方案。',
                        'windows_form_close_unproven': '尚未核验原查找窗口销毁；不会重复点击，原记录保留。',
                        'windows_visual_discovery_target_missing': '新截图中未找到唯一的目标页签；未执行点击。请让工具栏完整显示后重新找回任务。',
                        'windows_visual_prediction_already_visible': '模型提出的核验文字已在当前页，不能证明进入新页面。本次未点击，原记录保留；请重新生成方案。',
                        'windows_visual_continuation_exists': '原任务已有续接任务；请找回原任务，会直接打开同一续接结果，不会重做已完成步骤。',
                        'windows_visual_rebind_document_required': '旧方案缺少原文档的完整归属证据，不能只凭同名窗口迁移；原任务保留。',
                        'windows_visual_rebind_window_not_unique': '没有找到唯一匹配的文档新窗口；请在本地打开原文档，关闭同文档重复窗口后重新找回任务。',
                        'windows_visual_rebind_document_changed': '原文档或窗口绑定已变化，续接停止；不会把其他文件当成原任务。',
                        'windows_visual_rebind_generation_limit': '这个任务已迁移过窗口；暂不支持再次迁移，原任务和结果保留。',
                        'windows_intent_unsupported_or_ambiguous': '现有控件或已核验视觉页面还不足以覆盖这条指令；没有猜测按钮或执行动作。可缩小任务目标，未学习页面需后续适配。',
                        'windows_intent_no_supported_targets': '没有可用控件或同窗口已核验的视觉页面；未调用模型、未点击。',
                        'windows_handoff_window_required': '未找到唯一可用的WPS文档窗口。请打开WPS文档并刷新窗口；有多个窗口时请选择用于接续的窗口。',
                        'windows_handoff_window_changed': '选定的WPS窗口已变化或不支持此文件，请刷新窗口后重新选择；新草稿保留。',
                        'windows_handoff_receipt_required': '保存回执不完整，接续已停止；已有文件和记录保留。',
                        'windows_handoff_source_invalid': '暂时无法核验原任务或副本，可能被占用；恢复可读后再继续，旧步骤不会重做。',
                        'windows_handoff_depth_limit': '接续来源已到四层核验上限，未继续执行；现有任务和成果保留。',
                    }.get(error)
                    from .windows_reviewed_controls import failure_text
                    self.status.set(detail or failure_text(error) or '未完成：' + error + '。已保存证据保留。')
                elif self.stopped.is_set() and kind != 'run':
                    self.status.set('已停止；未采用迟到的观察或目标提案。')
                elif kind == 'windows':
                    self.windows = result
                    self.window.configure(values=[f"{w['title']} · {w['hwnd']}" for w in result])
                    self.window.set('')
                    self.window_changed()
                    self.status.set(f'可选择{len(result)}个受支持窗口。')
                elif kind == 'observe':
                    self.observation = result
                    if not result.get('truncated') and not result.get('blocked_reason'):
                        self.approved_observations = [o for o in self.approved_observations
                            if not same_identity(o['window'], result['window'])]
                        self.approved_observations.append(deepcopy(result))
                        self.approved_observations = self.approved_observations[-2:]
                    self.tree.delete(*self.tree.get_children())
                    for control in result['controls']:
                        if control.get('visible'):
                            current = control.get('value')
                            if current is None:
                                current = f"选中:{control.get('selected')} 展开:{control.get('expanded')} 勾选:{control.get('checked')}"
                            self.tree.insert('', 'end', iid=control['id'], values=(control['type'], control['name'], str(current)[:180]))
                    from .windows_reviewed_controls import failure_text
                    self.status.set((failure_text(result.get('blocked_reason')) or '当前有阻塞弹窗，先由用户处理。') if result.get('blocked_reason') else
                                    '观察有截断，请缩小目标范围。' if result.get('truncated') else
                                    '已识别查找输入框与关闭按钮；输入任务后点“按指令生成目标（云端）”，审阅后执行。' if result.get('form_snapshot') else
                                    '已自动识别可核验的输入框与按钮；描述任务并生成方案，确认后执行。' if result.get('reviewed_snapshot') else
                                    '观察完成；选择控件并添加目标。')
                elif kind == 'preview':
                    self.task_root = Path(result)
                    self.task_path.set(str(result))
                    self.execute_button.configure(state='normal')
                    self.status.set(f'已保存{len(self.conditions)}个顺序目标；点击执行批准这份预览。')
                elif kind == 'propose':
                    if 'goal' in result:
                        if result.get('revision')!=self.draft_revision or result.get('instruction')!=self.instruction.get():
                            self.status.set('指令或草稿已变化，迟到的方案未覆盖当前内容；已保存的待审任务仍保留。')
                            continue
                        envelope=result
                        result=envelope['goal']
                    else:
                        envelope={}
                    self.conditions = deepcopy(result['conditions'])
                    self.sources = []
                    self.invalidate()
                    self.show_targets()
                    self.proposal_task=envelope.get('root')
                    if self.proposal_task is not None:
                        try:
                            loaded=windows_task.load(self.proposal_task)
                        except ComputerError as exc:
                            self.status.set('待审方案暂不能核验，未执行；原记录保留：'+str(exc))
                            continue
                        self.task_root=loaded['root']
                        self.task_path.set(str(self.task_root))
                        self.show_outcome(loaded['summary'])
                        continue
                    self.status.set('目标提案已生成，请核对窗口、控件及目标状态，再本地预览并批准执行。')
                elif kind == 'restore':
                    self.conditions = deepcopy(result['goal']['conditions'])
                    self.sources = deepcopy(result.get('sources', []))
                    self.instruction.delete(0, 'end')
                    self.instruction.insert(0, result['goal']['instruction'])
                    self.task_root = result['root']
                    self.proposal_task = result['root'] if 'approval_required' in result['summary'] else None
                    self.show_targets()
                    self.task_path.set(str(self.task_root))
                    self.show_outcome(result['summary'])
                elif kind == 'handoff':
                    index = self.copy_choice.current()
                    if (result['revision'] != self.draft_revision or result['parent'] != self.saved_task
                            or not 0 <= index < len(self.saved_choices)
                            or self.saved_choices[index]['id'] != result['choice_id']):
                        self.status.set('草稿或成果选择已变化，迟到的接续结果未采用；新内容已保留。')
                        continue
                    self.conditions = deepcopy(result['conditions'])
                    self.sources = deepcopy(result['sources'])
                    if result['clear_instruction']:
                        self.instruction_var.set('接续保存副本，完成本次Windows目标')
                    self.invalidate()
                    self.show_targets()
                    self.status.set('保存副本已接入下一任务，原任务保留。可继续添加其他窗口目标，再预览并执行；不会重新保存原副本。')
                elif kind == 'run':
                    if 'migration' in result:
                        self.messages.put(('restore',result['migration'],None))
                    else:
                        self.show_outcome(result)
                elif kind == 'visual_recheck':
                    self.execute_button.configure(state='disabled')
                    self.resume_button.configure(state='disabled')
                    text = '当前页面独立复核通过' if result['ok'] else '当前页面独立复核未通过'
                    self.status.set(f"{text}；原任务仍为作用未知，原失败保留，禁止重放。复核回执：{result['root']}")
                elif kind == 'cancel_recheck':
                    receipt = result['receipt']
                    context = self.cancel_recheck_context
                    if context and context['parent'] == result['parent']:
                        self.cancel_recheck_receipt = Path(receipt['root']) if receipt['ok'] else None
                    self.control_recheck_buttons()
                    self.execute_button.configure(state='disabled')
                    self.resume_button.configure(state='disabled')
                    if receipt['ok']:
                        self.status.set('已保存原文件框关闭的只读复核；旧任务仍保留未知记录，不重放。'
                            '可开始下一任务，届时会重新观察窗口。回执：'+receipt['root'])
                    else:
                        self.status.set('取消结果尚未核验，未执行任何动作。原任务和草稿保留：'+receipt['code'])
                elif kind == 'cancel_next':
                    context = self.cancel_recheck_context
                    if (context is None or context['parent'] != result['parent']
                            or self.cancel_recheck_receipt != result['receipt']
                            or self.draft_revision != result['revision']):
                        self.status.set('草稿已变化，新的窗口观察未覆盖当前内容；复核回执仍保留。')
                        continue
                    observed = result['observation']
                    self.conditions, self.sources = [], []
                    self.proposal_task = None
                    self.saved_task = self.saved_goal = self.saved_instruction_revision = None
                    self.saved_choices = []
                    self.copy_choice.configure(values=[])
                    self.copy_choice.set('')
                    self.cancel_recheck_context = self.cancel_recheck_receipt = None
                    if result['clear_instruction']:
                        self.instruction_var.set('')
                    self.invalidate()
                    self.show_targets()
                    self.windows = [deepcopy(observed['window'])]
                    self.window.configure(values=[f"{observed['application']} · {observed['window']['title']} · {observed['window']['hwnd']}"])
                    self.window.current(0)
                    self.approved_observations = []
                    self.messages.put(('observe', observed, None))
                    self.messages.put(('cancel_next_ready', result, None))
                elif kind == 'cancel_next_ready':
                    self.status.set('原窗口已重新观察并接入下一任务；旧目标已清空，新写的指令保留。请输入本次目标，再审阅新方案。未执行任何操作。')
        except queue.Empty:
            pass
        finally:
            if not self.closed:
                self.poll_id = self.root.after(100, self.poll)

    def show_outcome(self, result):
        self.remember_saved_task(result)
        count = len(result['completed'])
        if result.get('manual_attention'):
            self.status.set(f'已核验{count}个目标；检测到未受审弹窗，已停止自动观察。' +
                ('原操作作用未知，保留原证据，不提供自动重放。' if result['state'] == 'unknown' else
                 '请在原应用处理后点击继续；将重新观察，只执行未完成目标。'))
        elif result.get('unsealed_effect'):
            self.status.set('已派发的操作尚未完成核验；保留旧记录，不重复执行。请先核对当前窗口。')
        elif result['state'] == 'verified':
            self.status.set(f'{count}个目标均已实际回读核验。任务和原回执已保存。')
        elif result['state'] == 'ready':
            self.status.set('已载入待执行目标；核对后点击“重新观察并继续”执行，尚未进行操作。')
        elif result['state'] == 'unknown':
            self.status.set('作用未知，已停止；不提供自动重放。保留原证据，请人工核对。')
        else:
            explanations = {
                'windows_visual_popup_requires_attention': '所选窗口出现弹窗，已停止自动观察和点击。处理弹窗后点击重新观察并继续，已完成步骤不重做。',
                'windows_visual_foreground_required': '所选窗口未处于前台，已停止；切回目标窗口后继续。',
                'windows_visual_postcondition_failed': '已发出导航点击，但目标文字未核验通过。作用未知，保留原记录，不自动再次点击。',
                'windows_ocr_unavailable': '本机文字识别组件不可用，视觉任务已停止；没有上传截图或自动安装组件。',
                'windows_ocr_language_unavailable': '本机缺少可用识别语言，视觉任务已停止；原控件操作仍可使用。',
                'foreground_permission_denied': 'Windows尚未授予前台切换权限，请在Jarvis中点击“重新观察并继续”。',
                'user_takeover': '检测到人工接管；结束手动操作后，可在Jarvis中重新观察并继续。',
                'windows_application_blocked': '窗口有阻塞弹窗，请先由用户处理，再重新观察。',
                'windows_completed_state_changed': '先前已完成目标的状态已变化，请核对后再继续。',
                'windows_final_state_changed': '最终回读发现目标状态有变化，请核对当前窗口。',
                'windows_task_paused': '已按要求暂停。',
                'windows_task_cancelled': '已停止。',
                'windows_no_safe_action': '当前窗口没有符合目标的允许动作，请重新观察并检查目标。',
                'windows_content_match_missing': '没有找到指定序号的内容命中；文字区分大小写，Excel内容与公式分开查找。未进行定位操作。',
                'windows_content_span_invalid': '原生正文范围与文字跨度不一致，无法可靠定位；原件和任务保留。',
                'windows_content_changed': '内容或选区在两次核验间变化，已停止；稳定后可继续未完成步骤。',
                'windows_content_native_unavailable': '当前WPS未提供所需内容或选区信息，不能把输入关键词当作命中。',
                'windows_content_selection_wrong_file': '实际选区不属于指定文件，已停止，未确认命中。',
                'windows_content_selection_wrong_sheet': '实际工作表不是本次命中位置，已停止，保留原记录。',
                'windows_action_limit': '已达到本任务批准的动作上限，请核对当前窗口。',
                'windows_file_changed': '指定文件内容已变化，原授权不会沿用；请重新添加该文件并预览。',
                'windows_file_unavailable': '指定文件暂时无法读取，可能仍被其他程序占用；解除占用后可继续，已保存步骤不会重复。',
                'windows_file_dialog_invalid': '当前不是已绑定的打开文件框；不会操作另存为或未知弹窗。',
                'windows_document_com_unreadable': '暂时无法核对WPS当前文档的完整路径，未将窗口标题当作成功。',
                'windows_document_visibility_unavailable': '无法确认当前可见的文档编辑区，已停止；不会把组件内保留的活动文件名当作成功。',
                'windows_document_not_visible': '目标文档标签当前不可见，已停止输入；重新观察后再继续，不会关闭其他文档。',
                'windows_reveal_not_visible': '滚动后目标仍不可见，已停止，未重复已完成操作。',
                'windows_target_ambiguous': '找到了多个同名控件，请重新观察并明确目标范围。',
                'windows_reobservation_limit': '连续观察到界面变化，已停止本步；待窗口稳定后重新观察并继续。',
                'windows_visual_form_popup_requires_attention': '查找窗口上出现了其他弹窗；请先处理弹窗，再继续原任务。已完成步骤不重放。',
                'windows_visual_form_dialog_changed': '原查找窗口被替换或文档发生变化，不能沿用原方案；请重新观察并审阅。',
                'windows_form_close_unproven': '尚未核验原查找窗口销毁；不会重复点击，原回执保留。',
                'windows_visual_form_close_focus_unverified': '无法确认查找控件焦点，当前步骤已停止；请保持窗口可见后重新观察。',
                'windows_document_unsaved_changes': '副本出现未保存修改，已停止关闭；请处理当前修改后重新观察。',
                'windows_document_not_active': '请在WPS切回本任务的副本文档，再点击重新观察并继续；不会关闭其他标签页。',
                'windows_document_state_changed': '两次读取到的WPS文档状态不同，已停止操作；待文档稳定后重新观察并继续。',
                'windows_copy_source_changed': '原件发生变化，原任务已停止；请保留记录并重新选择来源。',
                'windows_copy_incomplete': '发现尚未确认的保存作用，保留副本和回执，不会自动再次保存。',
            }
            from .windows_reviewed_controls import failure_text
            detail = (explanations.get(result.get('cause_code')) or explanations.get(result.get('code'))
                      or failure_text(result.get('cause_code')) or failure_text(result.get('code')) or '当前步骤未完成，证据已保留。')
            self.status.set(f'已核验{count}个目标；{detail} 继续前会重新观察已完成状态。')
        can_resume = result.get('resumable') and result['state'] not in {'verified', 'unknown'}
        self.resume_button.configure(state='normal' if can_resume else 'disabled',
            text='已处理弹窗，重新观察并继续' if can_resume and result.get('manual_attention') else '重新观察并继续')
        if result.get('approval_required'):
            self.execute_button.configure(text='确认方案并执行',state='normal')
            self.resume_button.configure(state='disabled')
            from .windows_visual_forms import is_goal as form_goal
            from .windows_reviewed_controls import is_goal as reviewed_goal
            rule = ('输入将核验字段实际回读；关闭将核验原弹窗销毁且文档未变。'
                    if any(form_goal(c['selector']) for c in self.conditions) else
                    '每个输入核验实际回读，按钮按上方列出的独立状态规则核验。'
                    if any(reviewed_goal(c['selector']) for c in self.conditions) else
                    '新页面核验真实选中标记和工具区文字，旧文字条件必须实际读回。')
            self.status.set('视觉目标及核验规则已自动生成。'+rule+'请审阅后点击“确认方案并执行”。仅接续未完成目标；尚未执行，重启可找回。')
        self.configure_cancel_recheck(result)
        if self.cancel_recheck_context is not None:
            self.status.set('原取消步骤仍保留未知记录，不重放。'+
                ('已有独立只读复核回执；可重新观察窗口后开始下一任务。'
                 if self.cancel_recheck_receipt else '可点击“只读复核取消结果”，核对原文件框是否已经关闭。'))
