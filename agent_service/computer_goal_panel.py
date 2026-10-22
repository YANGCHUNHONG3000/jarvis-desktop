"""One approval for a bounded, reviewable cross-window plan."""
import queue
import threading

from .computer import ComputerError, ComputerSession
from .computer_goals import execute_goal, input_epoch, prepare_goal, propose_goal

MODES = ('计算并记到新页', '把所选文字记到新页', '新建文字页', '选择资源管理器项目',
         '网页 → Excel → Word', '主题检索 → Excel → Word', '观察决策循环（浏览器与办公）',
         '网页点击输入／编辑已有 Word、Excel', '已读 GitHub → Word → 下一任务',
         '自然语言编辑 Word/Excel', '多结果对比 → Excel → Word')
NAMES = {'calculate': '计算并核对实际显示', 'read_text': '读取实际文字',
         'new_note': '新建记事本页并回读', 'select_item': '选中项目并核对',
         'public_read': '浏览器读取公开网页', 'public_search': '公开检索并读取真实网页',
         'sources_sheet': '保存并核验 Excel 来源表',
         'sheet_document': '读取已核验 Excel 并生成 Word', 'agent_cycle':'观察、决策、执行与复核'}


def describe(plan):
    if plan.get('mode') == 'repository_document':
        from .repository_document import describe_repository_document
        return describe_repository_document(plan)
    if plan.get('mode') == 'agent':
        from .computer_agent import describe as describe_agent
        return describe_agent(plan)
    if plan.get('mode') == 'adapters':
        lines = ['本次批准范围（仅新建文件；公开网页摘录；执行阶段不调用模型）：']
        lines.append('原任务：' + plan['instruction'])
        for n, step in enumerate(plan['steps'], 1):
            if step['op'] == 'public_search':
                content = f"主题：{step['query']}；最多 {step['max_sources']} 个公开来源（搜索服务会接收此主题）"
            elif step['op'] == 'public_read':
                content = '、'.join(step['urls'])
            else:
                content = f"来自 {step['from']}；标题：{step['title']}"
            lines.append(f"{n}. {NAMES[step['op']]}｜{content} → {step['as']}")
        lines.append('最长 240 秒；可点停止。后台读取网页和自有办公文档，不操作所选窗口。')
        lines.append('每页最多保留 1200 字并标注来源；完成后 Word 自动带入下一任务，Excel 也可选用。')
        if any(step['op'] == 'public_search' for step in plan['steps']):
            lines.append('候选无法读取时尝试下一个；至少读到一个正文才生成文档。失败候选会在两份文档中明确标记。')
            lines.append('摘录优先定位完整检索词；没有命中则使用页首并标明，不保证语义相关或内容完整。')
        return '\n'.join(lines)
    lines = ['本次批准范围（会切换到对应窗口；记事本新页尚未保存到文件）：']
    for n, step in enumerate(plan['steps'], 1):
        obs = plan['snapshots'][step['window']]['observation']
        content = step.get('expression', step.get('text', ''))
        if 'control_id' in step:
            content = next(c['name'] for c in obs['controls'] if c['id'] == step['control_id'])
        suffix = ' → 输出 ' + step['as'] if 'as' in step else ''
        lines.append(f"{n}. {NAMES[step['op']]}｜{obs['window']['title']}｜{content}{suffix}")
    lines.append(f"最多 {plan['max_actions']} 个动作 / 240 秒；鼠标键盘接管即停止。")
    return '\n'.join(lines)


def attach_goal_tab(tabs, root, *, busy=lambda: False, on_busy=lambda value: None):
    from tkinter import ttk
    tab = ttk.Frame(tabs, padding=12)
    tabs.add(tab, text='连续任务')
    panel = GoalPanel(tab, root, busy=busy, on_busy=on_busy)
    tab.controller = panel
    return panel


class GoalPanel:
    def __init__(self, tab, root, *, busy=lambda: False, on_busy=lambda value: None):
        import tkinter as tk
        from tkinter import ttk
        self.tab, self.root, self.external_busy, self.on_busy = tab, root, busy, on_busy
        self.session = self.plan = None
        self.on_artifacts = lambda records: None
        self.interaction_request, self.interaction_fields = None, None
        self.comparison_goal, self.comparison_fields = None, None
        self.office_planning_files = ()
        self.office_artifact_refs = None
        self.office_scope = None
        self.get_artifacts = lambda: []
        self.repository_readings = []
        self.windows, self.controls, self.snapshots = [], [], {}
        self.running = self.closed = False
        self.stopped = threading.Event()
        self.human_pending = None
        self.messages = queue.Queue()
        ttk.Label(tab, text='预览目标后连续执行并逐步核验。可选窗口操作、公开网址或主题检索 → Excel → Word。', wraplength=710).pack(anchor='w')
        self.source = ttk.Combobox(tab, state='readonly')
        self.destination = ttk.Combobox(tab, state='readonly')
        for label, widget in [('来源窗口（计算器、文字或资源管理器）', self.source), ('目标窗口（记事本）', self.destination)]:
            ttk.Label(tab, text=label).pack(anchor='w')
            widget.pack(fill='x')
            widget.bind('<<ComboboxSelected>>', lambda _: self.invalidate(clear_snapshots=True))
        row = ttk.Frame(tab)
        row.pack(fill='x', pady=5)
        ttk.Button(row, text='刷新窗口列表', command=self.refresh).pack(side='left')
        ttk.Button(row, text='读取所选窗口', command=self.observe).pack(side='left', padx=5)
        self.mode = ttk.Combobox(tab, state='readonly', values=MODES)
        self.mode.set(MODES[0])
        self.mode.pack(fill='x')
        self.mode.bind('<<ComboboxSelected>>', self.mode_changed)
        self.expression_label = ttk.Label(tab, text='计算式（例如 12.5*4）；支持两个数的加减乘除')
        self.expression_label.pack(anchor='w')
        self.expression = ttk.Entry(tab)
        self.expression.pack(fill='x')
        self.expression.bind('<KeyRelease>', lambda _: self.invalidate())
        source_row = ttk.Frame(tab)
        source_row.pack(fill='x')
        self.interaction_button = ttk.Button(source_row, text='设置网页与已有文件编辑范围…', command=self.configure_interaction)
        self.interaction_button.pack(side='left')
        self.interaction_button.configure(state='disabled')
        self.output_edit_button = ttk.Button(source_row, text='使用上一任务 Word/Excel',
                                             command=self.use_generated_artifacts)
        self.output_edit_button.pack(side='left', padx=6)
        self.scope_button = ttk.Button(source_row, text='选择编辑范围…', command=self.configure_scope)
        self.scope_button.pack(side='left')
        self.recovery_button = ttk.Button(source_row, text='恢复连续任务…', command=self.list_recoveries)
        self.recovery_button.pack(side='left', padx=6)
        self.control_label = ttk.Label(tab, text='读取文字或选择项目时，选择来源控件')
        self.control_label.pack(anchor='w')
        self.control = ttk.Combobox(tab, state='readonly')
        self.control.pack(fill='x')
        self.control.bind('<<ComboboxSelected>>', lambda _: self.invalidate())
        self.text_label = ttk.Label(tab, text='新页文字（可留空使用默认文本）；云端规划时填写完整任务描述')
        self.text_label.pack(anchor='w')
        self.text = tk.Text(tab, height=3, wrap='word')
        self.text.pack(fill='x')
        self.text.bind('<<Modified>>', self.modified)
        row = ttk.Frame(tab)
        row.pack(fill='x', pady=5)
        ttk.Button(row, text='本地预览目标（不耗 API）', command=self.preview).pack(side='left')
        self.propose_button = ttk.Button(row, text='云端规划（上传所选窗口文字）', command=self.propose)
        self.propose_button.pack(side='left', padx=5)
        self.preview_box = tk.Text(tab, height=7, wrap='word', state='disabled')
        self.preview_box.pack(fill='both', expand=True)
        row = ttk.Frame(tab)
        row.pack(fill='x', pady=5)
        self.execute_button = ttk.Button(row, text='批准并连续执行此目标', command=self.execute, state='disabled')
        self.execute_button.pack(side='left')
        ttk.Button(row, text='停止连续任务', command=self.stop).pack(side='left', padx=5)
        self.pause_each_step = tk.BooleanVar(value=False)
        self.pause_checkbox = ttk.Checkbutton(tab, text='每步核验后暂停', variable=self.pause_each_step)
        self.pause_checkbox.pack(anchor='w')
        self.human_continue_button = ttk.Button(row, text='已完成登录／验证，继续任务',
                                               command=self.continue_human, state='disabled')
        self.human_continue_button.pack(side='left')
        self.status = tk.StringVar(value='先选择并读取窗口。本地执行不调用模型；云端沿用每日及滚动 24 小时各 ¥5 上限。')
        ttk.Label(tab, textvariable=self.status, wraplength=710).pack(anchor='w')
        self.poll_id = root.after(100, self.poll)

    def mode_changed(self, _=None):
        self.invalidate(clear_snapshots=True)
        adapter = self.mode.get() in MODES[4:]
        agent = self.mode.get() == MODES[6]
        interactive = self.mode.get() == MODES[7]
        office_intent = self.mode.get() == MODES[9]
        comparison = self.mode.get() == MODES[10]
        search = self.mode.get() in MODES[5:]
        self.expression_label.configure(text='文档标题（可留空使用主题，最多 200 字）' if search else '公开网页链接（最多 2 个，用空格分隔）' if adapter else '计算式（例如 12.5*4）；支持两个数的加减乘除')
        self.text_label.configure(text='检索主题；可选云端规划时填写完整任务描述（仅上传这里的文字）' if search else '文档标题／摘录主题（1～200 字）' if adapter else '新页文字（可留空使用默认文本）；云端规划时填写完整任务描述')
        self.propose_button.configure(text='云端理解指令（仅上传任务描述）' if search else '云端规划（上传所选窗口文字）')
        for widget in (self.source, self.destination, self.control):
            widget.configure(state='disabled' if adapter else 'readonly')
        self.status.set('填写主题即可本地预览；复杂指令可选云端规划，搜索仅在批准后进行。' if search else '填写公开网址和标题即可本地预览，无需选择窗口。' if adapter else '先选择并读取窗口。')
        if agent:
            self.text_label.configure(text='本地预览填写主题；云端理解填写完整目标、输出类型、所需来源数及必需字面词')
            self.propose_button.configure(text='理解目标（上传任务描述）')
            self.status.set('先预览目标范围；批准后根据实际观察动态选择操作。单选零模型，多选使用原预算。')
        self.interaction_button.configure(state='normal' if interactive or office_intent or comparison else 'disabled',
                                          text='选择最多四个 Word/Excel 文件…' if office_intent else
                                          '设置检索与对比范围…' if comparison else
                                          '设置网页与已有文件编辑范围…',
                                          command=self.configure_office_intent if office_intent else
                                          self.configure_comparison if comparison else self.configure_interaction)
        self.propose_button.configure(state='disabled' if interactive or comparison or self.mode.get() == MODES[8] else 'normal')
        self.control_label.configure(text='已核验的 GitHub 读取结果（最近的在前）' if self.mode.get() == MODES[8]
                                     else '读取文字或选择项目时，选择来源控件')
        if interactive:
            self.expression_label.configure(text='此模式在“设置网页与已有文件编辑范围”中选择来源和修改内容。')
            self.text_label.configure(text='任务说明（例如：查询状态后更新所选工作簿与文档）')
            self.status.set('先设置网页与编辑范围，再本地预览。浏览器和文件操作均按当前状态核验。')
        if self.mode.get() == MODES[8]:
            self.expression_label.configure(text='Word 文档标题（可留空使用默认标题）')
            self.text_label.configure(text='直接使用上方已核验读取记录，不需要找文件或重新登录。')
            self.propose_button.configure(state='disabled')
            self.refresh_repository_readings()
        if office_intent:
            self.expression_label.configure(text='无需填写段落、单元格或原值')
            self.text_label.configure(text='任务说明（自然语言描述希望如何修改所选文件）')
            self.propose_button.configure(text='云端规划（上传所选文件内容与任务说明）')
            self.status.set('选择最多四个文件，可再选择编辑范围；选中内容会上传云端模型，规划后先预览逐处差异。')
        if comparison:
            self.expression_label.configure(text='标题、检索主题和对比维度在“设置检索与对比范围”中填写。')
            self.text_label.configure(text='任务说明（可留空使用已保存的文档标题）')
            self.status.set('先设置对比范围，再本地预览；批准后检索真实候选，按来源原文生成对比表。')

    def configure_comparison(self):
        if self.running or self.external_busy():
            return
        from .comparison_panel import open_editor
        def saved(goal, fields):
            from .computer_agent import validate_goal
            self.comparison_goal = validate_goal(goal)
            self.comparison_fields = dict(fields)
            self.invalidate()
            self.status.set('对比范围已保存；请本地预览目标。')
        open_editor(self.root, saved, self.comparison_fields)

    def configure_office_intent(self):
        if self.running or self.external_busy():
            return
        from tkinter import filedialog
        paths = filedialog.askopenfilenames(parent=self.root, title='选择最多四个 Word/Excel 文件',
                   filetypes=[('Word/Excel 文件', '*.docx *.xlsx')])
        if not paths:
            return
        self.office_artifact_refs = None
        self.office_scope = None
        if len(paths) > 4:
            self.office_planning_files = ()
            self.invalidate()
            self.status.set('最多选择四个文件；请重新选择。')
            return
        if any(not path.lower().endswith(('.docx', '.xlsx')) for path in paths):
            self.office_planning_files = ()
            self.invalidate()
            self.status.set('仅支持 .docx 和 .xlsx 文件；请重新选择。')
            return
        self.office_planning_files = tuple(paths)
        self.invalidate()
        self.status.set(f'已选择 {len(paths)} 个文件；内容会上传至现有云端模型，提交前可重新选择。')

    def use_generated_artifacts(self, records=None):
        if self.running or self.external_busy():
            return
        from copy import deepcopy
        from .office_scope_panel import open_file_selector
        if records is None:
            records = deepcopy(self.get_artifacts())
            if not records:
                from .office_handoff import latest_generated_outputs
                self.start('generated_artifacts', lambda _: latest_generated_outputs(stop=self.stopped.is_set))
                return
        else:
            records = deepcopy(records)
        self.office_planning_files, self.office_artifact_refs, self.office_scope = (), None, None
        self.mode.set(MODES[9])
        self.mode_changed()
        try:
            open_file_selector(self.root, records, self.prepare_generated_scope)
        except ValueError as exc:
            self.status.set(str(exc))

    def prepare_generated_scope(self, records):
        from .office_handoff import select_outputs
        from .office_scope import inspect_files
        def work(session):
            refs = select_outputs(records)
            return inspect_files(session, [r['artifact'] for r in refs], artifact_refs=refs, stop=self.stopped.is_set)
        self.start('office_scope', work)

    def configure_scope(self):
        if self.running or self.external_busy():
            return
        if not self.office_planning_files:
            self.status.set('请先接入成果或选择文件。')
            return
        from copy import deepcopy
        from .office_scope import inspect_files
        files, refs = tuple(self.office_planning_files), deepcopy(self.office_artifact_refs)
        self.invalidate()
        self.start('office_scope', lambda s: inspect_files(s,files,artifact_refs=refs,stop=self.stopped.is_set))

    def accept_scope(self, result):
        if self.running or self.closed or self.stopped.is_set():
            return
        from copy import deepcopy
        result = deepcopy(result)
        self.office_planning_files = tuple(result['files'])
        self.office_artifact_refs = result['artifact_refs']
        self.office_scope = result['scope']
        self.invalidate()
        self.show_text('已选择文件与范围：\n'+'\n'.join(
            f"{part['path']}：{len(part['locations'])} 个位置" for part in self.office_scope))
        self.status.set(f'已接入 {len(self.office_planning_files)} 份文件的选中范围；未调用模型。填写任务说明后云端规划。')

    def configure_interaction(self):
        if self.running or self.external_busy():
            return
        from .interaction_panel import open_editor
        def saved(request, fields):
            self.interaction_request, self.interaction_fields = request, fields
            self.invalidate()
            self.status.set('操作范围已保存；请本地预览目标。')
        open_editor(self.root, saved, self.interaction_fields)

    def invalidate(self, clear_snapshots=False):
        self.plan = None
        self.execute_button.configure(state='disabled')
        self.show_text('尚无有效目标预览。')
        if clear_snapshots:
            self.snapshots, self.controls = {}, []
            self.control.set('')
            self.control.configure(values=[])

    def show_text(self, text):
        self.preview_box.configure(state='normal')
        self.preview_box.delete('1.0', 'end')
        self.preview_box.insert('1.0', text)
        self.preview_box.configure(state='disabled')

    def modified(self, _=None):
        if self.text.edit_modified():
            self.invalidate()
            self.text.edit_modified(False)

    def set_busy(self, value):
        self.running = value
        if not value:
            self.cancel_human()
        self.on_busy(value)
        self.pause_checkbox.configure(state='disabled' if value else 'normal')
        self.output_edit_button.configure(state='disabled' if value else 'normal')
        self.scope_button.configure(state='disabled' if value else 'normal')
        self.recovery_button.configure(state='disabled' if value else 'normal')
        self.interaction_button.configure(state='normal' if not value and self.mode.get()==MODES[7] else 'disabled')
        if not value and self.mode.get() in (MODES[9], MODES[10]):
            self.interaction_button.configure(state='normal')
        for widget in (self.source, self.destination, self.control, self.mode):
            widget.configure(state='disabled' if value else 'readonly')
        for widget in (self.expression, self.text):
            widget.configure(state='disabled' if value else 'normal')
        if not value and self.mode.get() in MODES[4:]:
            for widget in (self.source, self.destination, self.control):
                widget.configure(state='disabled')
            if self.mode.get() == MODES[8]:
                self.control.configure(state='readonly')
        self.execute_button.configure(state='disabled')

    def start(self, kind, operation):
        if self.running or self.external_busy():
            self.status.set('已有任务在运行，请等待或停止。')
            return
        self.set_busy(True)
        self.stopped.clear()
        self.status.set('正在处理……')
        def run():
            try:
                if self.session is None:
                    self.session = ComputerSession.create(host=self.host)
                result = operation(self.session)
                self.messages.put((kind, result, None))
            except Exception as exc:
                self.messages.put((kind, None, str(exc) if isinstance(exc, ComputerError) else 'operation_failed'))
        try:
            threading.Thread(target=run, daemon=True).start()
        except Exception:
            self.set_busy(False)
            self.status.set('无法启动后台任务。')

    def host(self, request, operation, *, stop):
        from .computer_host import run_supervised
        return run_supervised(request, operation, stop=stop, foreground_grant=self.grant_on_ui)

    def grant_on_ui(self, pid):
        # Windows foreground eligibility belongs to the approving GUI's input
        # thread. Marshal this one PID grant back to it before child startup.
        event, answer = threading.Event(), {'allowed': False}
        self.messages.put(('foreground', (pid, event, answer), None))
        ready = event.wait(3)
        event.set()  # Revoke a queued grant after timeout, before worker cleanup.
        return ready and answer['allowed'] and not self.stopped.is_set()

    def refresh(self):
        if self.mode.get() == MODES[8]:
            self.refresh_repository_readings()
            return
        if self.running or self.mode.get() in MODES[4:]:
            return
        self.invalidate(True)
        self.start('windows', lambda s: s.windows(self.stopped.is_set))

    def observe(self):
        if self.mode.get() == MODES[8]:
            self.refresh_repository_readings()
            return
        if self.running or self.mode.get() in MODES[4:]:
            return
        bindings = {}
        for alias, widget in [('source', self.source), ('destination', self.destination)]:
            index = widget.current()
            if 0 <= index < len(self.windows):
                bindings[alias] = dict(self.windows[index])
        if not bindings:
            self.status.set('请先刷新并选择窗口。')
            return
        self.invalidate(True)
        self.start('observe', lambda s: {k: s.observe(w, self.stopped.is_set) for k, w in bindings.items()})

    def preview(self):
        if self.running or self.external_busy():
            return
        try:
            text, expression, mode = self.text.get('1.0', 'end-1c'), self.expression.get(), self.mode.get()
            if mode == MODES[10]:
                from .computer_agent import prepare
                if self.comparison_goal is None:
                    raise ComputerError('请先设置检索与对比范围')
                if self.session is None:
                    self.session = ComputerSession.create(host=self.host)
                self.show_plan(prepare(self.session, text.strip() or self.comparison_goal['title'],
                                       self.comparison_goal))
                self.status.set('请核对来源数、网站数和维度；批准后读取真实结果并选取原文，保存 Excel 和 Word。')
                return
            if mode == MODES[9]:
                self.invalidate()
                self.status.set('自然语言编辑需要云端规划；本地预览不会调用 API。规划结果会先显示差异供你核对。')
                self.show_text('请先选择最多四个 .docx/.xlsx 文件，可选编辑范围，再填写任务说明并点击“云端规划”。')
                return
            if mode == MODES[8]:
                from .repository_document import prepare_repository_document
                index = self.control.current()
                if not 0 <= index < len(self.repository_readings):
                    raise ComputerError('请先选择一个已核验的 GitHub 读取结果')
                options = {'title':expression.strip()} if expression.strip() else {}
                self.show_plan(prepare_repository_document(self.repository_readings[index]['run'], **options))
                self.status.set('请核对来源；批准后本机生成 Word 并自动接到下一任务，不重新登录。')
                return
            elif mode == MODES[7]:
                from .computer_interaction import prepare
                if self.interaction_request is None:
                    raise ComputerError('请先设置网页与文件编辑范围')
                if self.session is None:
                    self.session = ComputerSession.create(host=self.host)
                self.show_plan(prepare(self.session, text.strip() or '完成所选网页查询与文件编辑', **self.interaction_request))
                self.status.set('请核对网页输入及文件逐处修改；批准后进入同一个观察决策循环。')
                return
            elif mode == MODES[6]:
                from .computer_agent import prepare
                if self.session is None:
                    self.session = ComputerSession.create(host=self.host)
                self.show_plan(prepare(self.session, text.strip(), {'queries':[text.strip()],
                    'title':expression.strip() or text.strip()[:200], 'min_sources':1,
                    'required_terms':[], 'outputs':['spreadsheet','document']}))
                self.status.set('请核对目标条件与动态操作范围；批准后开始观察决策循环。')
                return
            elif mode == MODES[5]:
                from .computer_goal_adapters import search_template
                steps = search_template(text.strip(), expression.strip() or text.strip()[:200])
                if self.session is None:
                    self.session = ComputerSession.create(host=self.host)
            elif mode == MODES[4]:
                from .computer_goal_adapters import template
                steps = template(expression.split(), text.strip())
                if self.session is None:
                    self.session = ComputerSession.create(host=self.host)
            elif mode == MODES[0]:
                steps = [{'op':'calculate', 'window':'source', 'expression':expression, 'as':'result'},
                         {'op':'new_note', 'window':'destination', 'text':text or f'{expression} = {{{{result}}}}'}]
            elif mode == MODES[1]:
                control = self.controls[self.control.current()] if self.control.current() >= 0 else None
                if control is None:
                    raise ComputerError('请先选择来源文字控件')
                steps = [{'op':'read_text', 'window':'source', 'control_id':control['id'], 'as':'source_text'},
                         {'op':'new_note', 'window':'destination', 'text':text or '{{source_text}}'}]
            elif mode == MODES[2]:
                steps = [{'op':'new_note', 'window':'destination', 'text':text}]
            else:
                control = self.controls[self.control.current()] if self.control.current() >= 0 else None
                if control is None:
                    raise ComputerError('请先选择资源管理器项目')
                steps = [{'op':'select_item', 'window':'source', 'control_id':control['id']}]
            if self.session is None:
                raise ComputerError('请先读取窗口')
            plan = prepare_goal(self.session, mode, {} if mode in MODES[4:] else self.snapshots, steps)
            self.show_plan(plan)
            self.status.set('请核对完整步骤；批准一次后自动逐步执行。')
        except (ValueError, IndexError, OSError) as exc:
            self.invalidate()
            self.status.set('未生成目标：' + str(exc))

    def show_plan(self, plan):
        self.plan = plan
        self.show_text(describe(plan) if plan else '当前目标不在已支持范围内。')
        self.execute_button.configure(state='normal' if plan else 'disabled')

    def list_recoveries(self):
        if self.running or self.external_busy():
            return
        from .interaction_recovery_panel import recent_candidates
        self.invalidate()
        self.start('recovery_candidates', lambda _: recent_candidates(stop=self.stopped.is_set))

    def select_recovery(self, row):
        if self.running or self.external_busy() or self.closed or self.stopped.is_set():
            return
        from .interaction_recovery_panel import prepare_selected
        self.invalidate()
        self.start('recovery_preview', lambda _: prepare_selected(row,host=self.host,stop=self.stopped.is_set))

    def propose(self):
        if self.running:
            return
        if self.mode.get() == MODES[10]:
            self.status.set('此模式请设置对比范围并本地预览；自然语言理解可使用“观察决策循环”模式。')
            return
        if self.mode.get() == MODES[9]:
            files = tuple(self.office_planning_files)
            instruction = self.text.get('1.0', 'end-1c').strip()
            if not files or not instruction:
                self.status.set('请先选择 Word/Excel 文件并填写自然语言任务说明。')
                return
            from .office_intent import propose
            from copy import deepcopy
            refs = deepcopy(self.office_artifact_refs)
            scope = deepcopy(self.office_scope)
            self.invalidate()
            self.start('propose', lambda s: propose(s, instruction, files, stop=self.stopped.is_set,
                                                   artifact_refs=refs,scope=scope))
            return
        if self.mode.get() in (MODES[7], MODES[8]):
            self.status.set('此模式按明确网页与文件修改范围本地预览；执行中多候选才调用原预算模型。')
            return
        if self.mode.get() == MODES[6]:
            from .computer_agent import propose
            instruction = self.text.get('1.0', 'end-1c')
            self.invalidate()
            self.start('propose', lambda s: propose(s, instruction, stop=self.stopped.is_set))
            return
        if self.mode.get() == MODES[5]:
            from .computer_goal_adapters import propose_research_goal
            instruction = self.text.get('1.0', 'end-1c')
            self.invalidate()
            self.start('propose', lambda s: propose_research_goal(s, instruction, stop=self.stopped.is_set))
            return
        if self.mode.get() == MODES[4]:
            self.status.set('网页办公任务使用本地预览即可，不需要调用云端模型。')
            return
        snapshots, instruction = dict(self.snapshots), self.text.get('1.0', 'end-1c')
        if not snapshots:
            self.status.set('请先读取所选窗口。')
            return
        self.invalidate()
        self.start('propose', lambda s: propose_goal(s, instruction, snapshots, stop=self.stopped.is_set))

    def execute(self):
        if self.running or self.external_busy() or self.plan is None:
            return
        if self.plan.get('mode') == 'repository_document':
            from .repository_document import execute_repository_document
            plan = self.plan
            self.invalidate()
            self.start('repository_document', lambda s: execute_repository_document(
                plan, approval=plan['sha256'], stop=self.stopped.is_set))
            return
        try:
            epoch = None if self.plan.get('mode') in {'adapters','agent'} else input_epoch()
        except Exception:
            self.status.set('无法启用接管检测，未开始执行。')
            return
        plan = self.plan
        agent_options = None
        if plan.get('mode') == 'agent':
            # Read Tk state on the UI thread; workers receive only a captured bool.
            pause_requested = bool(self.pause_each_step.get())
            agent_options = {'pause_after_step':lambda: pause_requested}
            if plan.get('goal',{}).get('kind') == 'interactive':
                agent_options['human_handoff'] = self.wait_for_human
        self.invalidate()
        self.start('execute', lambda s: execute_goal(s, plan, approval=plan['sha256'], epoch=epoch,
                   stop=self.stopped.is_set, progress=lambda p: self.messages.put(('progress', p, None)),
                   agent_options=agent_options))

    def refresh_repository_readings(self):
        if self.running or self.external_busy():
            return
        from .repository_document import list_repository_readings
        self.invalidate()
        self.start('repository_readings', lambda s: list_repository_readings())

    def wait_for_human(self, request, tick):
        event, answer = threading.Event(), {'accepted': False}
        self.messages.put(('human', (request, event, answer), None))
        try:
            while not event.wait(0.05):
                if self.stopped.is_set() or self.closed:
                    return False
                tick()
            return answer['accepted'] and not self.stopped.is_set() and not self.closed
        finally:
            event.set()  # revoke any late UI continuation
            self.messages.put(('human_finished', event, None))

    def continue_human(self):
        pending = self.human_pending
        if pending and self.running and not self.stopped.is_set() and not pending[1].is_set():
            pending[2]['accepted'] = True
            pending[1].set()
            self.human_continue_button.configure(state='disabled')
            self.status.set('正在重新观察并核对登录状态；通过后继续原任务。')
        else:
            self.human_continue_button.configure(state='disabled')
            if not (pending and pending[2]['accepted'] and self.running and not self.stopped.is_set()):
                self.status.set('本次登录等待已结束，不能继续旧任务。请点击“本地预览目标”，再批准新任务。')

    def cancel_human(self):
        if self.human_pending:
            self.human_pending[1].set()
            self.human_pending = None
        self.human_continue_button.configure(state='disabled')

    def stop(self):
        self.stopped.set()
        self.cancel_human()
        self.invalidate(True)
        self.status.set('已请求停止；已完成的操作会保留，不会重放或回滚。')

    def close(self):
        self.closed = True
        self.stopped.set()
        self.cancel_human()
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
                if kind == 'human':
                    request, event, answer = result
                    if self.running and not self.stopped.is_set() and not event.is_set():
                        self.human_pending = result
                        self.human_continue_button.configure(state='normal')
                        self.status.set('请在独立 Edge 窗口亲自完成 GitHub 登录或验证码，然后点“已完成登录／验证，继续任务”。最多等待15分钟，可停止。')
                        self.show_text('任务已暂停等待你操作；此时不读取登录页或调用模型。\n目标仓库：'+request['target_url']+
                                       f"\n本次第 {request.get('handoff', 1)}/3 次人工接管，最多等待 15 分钟。"+
                                       '\n请在本次独立 Edge 窗口登录，再点击下方继续。超时后会话结束，需重新预览并批准新任务。')
                    else:
                        event.set()
                    continue
                if kind == 'human_finished':
                    if self.human_pending and self.human_pending[1] is result:
                        self.cancel_human()
                    continue
                if kind == 'foreground':
                    from .computer_host import grant_foreground
                    pid, event, answer = result
                    try:
                        if self.running and not self.stopped.is_set() and not event.is_set():
                            answer['allowed'] = grant_foreground(pid)
                    except Exception:
                        answer['allowed'] = False
                    finally:
                        event.set()
                    continue
                if kind == 'progress':
                    if result.get('op') in {'agent_cycle','agent_reobserve','repository_document','page_office'}:
                        self.status.set(result['message'] + ' 可停止。')
                        continue
                    hint = '；可点击停止。' if result['op'] in {'public_read', 'public_search', 'sources_sheet', 'sheet_document'} else '；请暂勿操作鼠标键盘。'
                    self.status.set(f"正在执行 {result['step']}/{result['total']}：{NAMES[result['op']]}" + hint)
                    continue
                self.set_busy(False)
                if kind == 'repository_document' and result:
                    self.invalidate(True)
                    if result['ok']:
                        self.show_text('GitHub 页面资料已整理为 Word，文件与原生回读均通过。\n' +
                                       '\n'.join(a['path'] for a in result['artifacts']))
                        self.status.set('Word 已核验；正在自动选为下一任务资料。')
                        try:
                            self.on_artifacts(result['artifacts'])
                        except Exception:
                            self.status.set('Word 已保存，自动接力未完成。可再次选择同一读取记录核验接力，无需重新登录。')
                    else:
                        self.show_text('仓库读取记录仍保留。Word 阶段未完成：' + result['code'])
                        self.status.set('Word 未交付：' + result['code'] + '；未知执行结果不会自动重跑。')
                elif kind == 'execute' and result:
                    self.invalidate(True)
                    status = '目标步骤全部通过实际核验' if result['ok'] else '已停止，未全部完成：' + result['code']
                    if result.get('engine') == 'observed_agent':
                        status = '目标条件和实际成果已核验' if result['ok'] else '已停止，目标未完成：' + result['code']
                        failures = result.get('failures', [])
                        if result.get('goal_kind') == 'interactive':
                            status += f"；已核验办公成果 {len(result.get('artifacts', []))} 份；失败记录 {len(failures)} 条"
                            if result.get('reobservations'):
                                status += f"；页面变化重新观察 {len(result['reobservations'])} 次"
                        else:
                            status += (f"；有效来源 {result.get('source_reads', 0)} 个；"
                                   f"检索失败 {sum(f.get('kind') == 'search' for f in failures)} 次，"
                                   f"候选失败 {sum(f.get('kind') == 'read' for f in failures)} 个（详见执行记录）")
                    elif result.get('failures'):
                        status += f"；已读取 {result.get('source_reads', 0)} 个来源，{len(result['failures'])} 个候选未读到正文"
                        if result['ok']:
                            status += '（已在文档标明）'
                    if not result['ok'] and result.get('code') == 'browser_human_timeout':
                        status = ('登录等待已超过 15 分钟，本次任务和独立浏览器会话已结束，尚未验证登录。'
                                  '请点击“本地预览目标”，再批准新任务，在新开的 Edge 窗口登录；旧任务不能继续。')
                    self.status.set(status + f"；已核验 {len(result['completed'])} 步。记录：{result['run']}")
                    if result.get('mode') == 'adapters':
                        lines = [status, *[f"{a['kind']}：{a['path']}" for a in result['artifacts']]]
                        if result.get('repository_read'):
                            repo = result['repository_read']
                            lines += ['已核验GitHub搜索与仓库读取：'+repo['url'],
                                      '已登录会话' if repo['authenticated'] else '公开读取',
                                      '本机读取的仓库正文摘录：', repo['text'][:12000]]
                        if result.get('repository_report'):
                            report = result['repository_report']
                            lines.insert(1, 'Word 已生成并核验，将自动接到下一任务。' if report['ok'] else
                                         '仓库已读，Word 阶段未完成：'+report['code'])
                        self.show_text('\n'.join(lines))
                        if result['artifacts']:
                            try:
                                self.on_artifacts(result['artifacts'])
                            except Exception:
                                self.status.set(status + '；自动接力未完成，成果已保存，可从历史任务重新选用。')
                    else:
                        self.show_text('\n'.join([status, '新建记事本页未保存到文件。', *[f"{k} = {v}" for k, v in result['outputs'].items()]]))
                elif error or self.stopped.is_set():
                    self.invalidate(True)
                    self.status.set('未完成：' + (error or '已停止') + '。需要重新读取和预览。')
                elif kind == 'generated_artifacts':
                    self.use_generated_artifacts(result)
                elif kind == 'recovery_candidates':
                    if result:
                        from .interaction_recovery_panel import open_selector
                        open_selector(self.root,result,self.select_recovery)
                    self.status.set('请选择需恢复的连续任务；检查和预览不调用模型。' if result else
                                    '最近记录中没有未完成的浏览器／办公连续任务。')
                elif kind == 'recovery_preview':
                    self.session = ComputerSession(result['session_root'],host=self.host)
                    self.office_planning_files, self.office_artifact_refs, self.office_scope = (), None, None
                    goal = result['plan'].get('goal', {})
                    mode_index = 10 if 'comparison' in goal else 6 if 'queries' in goal else 7 if goal.get('browser') else 9
                    if mode_index == 10:
                        from .comparison_panel import fields_from_goal
                        from copy import deepcopy
                        self.comparison_goal = deepcopy(goal)
                        self.comparison_fields = fields_from_goal(goal)
                    self.mode.set(MODES[mode_index])
                    self.mode_changed()
                    self.text.delete('1.0','end')
                    self.text.insert('1.0',result['plan']['instruction'])
                    self.text.edit_modified(False)
                    self.show_plan(result['plan'])
                    self.status.set(result['reason'])
                elif kind == 'office_scope':
                    from .office_scope_panel import open_scope_selector
                    self.status.set('文件已本地核验，请选择编辑范围；尚未调用模型。')
                    open_scope_selector(self.root,result,self.accept_scope)
                elif kind == 'repository_readings':
                    self.repository_readings = result
                    self.control.configure(values=[
                        f"{i}. {r['url']} · {r['body_chars']} 字" for i, r in enumerate(result, 1)])
                    if result:
                        self.control.current(0)
                    self.status.set('已列出核验通过的 GitHub 读取结果。选择来源后点本地预览；不会重新登录。'
                                    if result else '尚无核验通过的 GitHub 读取记录，请先完成仓库搜索和读取。')
                elif kind == 'windows':
                    self.windows = result
                    for widget in (self.source, self.destination):
                        widget.configure(values=[f"{w['title']} · 窗口 {w['hwnd']}" for w in result])
                        widget.set('')
                    self.status.set('请选择本次任务的来源和目标窗口。')
                elif kind == 'observe':
                    self.snapshots = result
                    obs = result.get('source', {}).get('observation', {})
                    self.controls = [c for c in obs.get('controls', []) if c['visible'] and c['type'] in {'Edit', 'Document', 'Text', 'ListItem', 'TreeItem'}]
                    self.control.configure(values=[f"{c['name'][:90]} · {c['type']}" for c in self.controls])
                    self.status.set('读取完成；预览与批准需在 120 秒内完成。')
                elif kind == 'propose':
                    self.show_plan(result['plan'])
                    self.status.set(result['reason'])
        except queue.Empty:
            pass
        finally:
            if not self.closed:
                self.poll_id = self.root.after(100, self.poll)
