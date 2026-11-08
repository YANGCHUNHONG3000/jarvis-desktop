"""Explicitly authored Office tasks; adopt inputs, never execute them."""
from copy import deepcopy
from pathlib import Path
import queue
import threading
import time

from . import office_task_chain as core, workspace_state, verification_progress
from . import office_chain_payload
from .office_chain_diagnostics import verification_failure


_VERIFY_SILENCE = 40
_VERIFY_LIMIT = 300
_TIMEOUT_REASONS = {'核验已达到300秒总上限', '核验已连续40秒没有新进度'}


class _VerificationObserver:
    """Observe only this request's successful, real counter checkpoints."""
    def __init__(self, state, report):
        self.state, self.report = state, report

    @property
    def records(self):
        return self.state.records

    @property
    def files(self):
        return self.state.files

    def check(self, **kwargs):
        before = self.records+self.files
        self.state.check(**kwargs)
        if self.records+self.files > before:
            self.report(f'{self.state.stage}；已读取记录 {self.records} 次、校验文件 {self.files} 次。')


class OfficeTaskChainPanel:
    def __init__(self, *, root, context, busy, on_busy, apply, prepare, set_scope,
                 status, store, task_base, computer_base, clock=time.monotonic,
                 office_context=None, apply_office=None, prepare_edit=None,
                 start_resume=None, clear_resume_goal=None, open_draft=None):
        self.root, self.context, self.busy, self.on_busy = root, context, busy, on_busy
        self.apply, self.prepare, self.set_scope, self.status = apply, prepare, set_scope, status
        self.store, self.task_base, self.computer_base = store, task_base, computer_base
        self.clock = clock
        self.office_context, self.apply_office, self.prepare_edit = office_context, apply_office, prepare_edit
        self.start_resume, self.clear_resume_goal = start_resume, clear_resume_goal
        self.open_draft = open_draft
        self.resume_owned = None
        self.resume_epoch = 0
        self.active = self.issued = self.cleared = self.pending = None
        self.handoff = None
        self.route_context = None
        self._restore_display = None
        self._restore_route = None
        self.closed, self.generation = False, 0
        self.messages = queue.Queue()
        self.observed = set()
        self.failed_verification = None

    def idle(self):
        return not (self.closed or self.pending or self.busy())

    def current(self, snapshot):
        return not self.closed and self.context() == snapshot

    def _begin(self, operation, *, auto=False, complete=None, settled=None, retryable=False):
        if not self.idle():
            self.status('当前任务正忙，请稍后接续清单。')
            return False
        snapshot = deepcopy(self.context())
        self.failed_verification = None
        self.generation += 1
        generation = self.generation
        started = self.clock()
        cancelled = threading.Event()
        self.pending = {'context': snapshot, 'auto': auto, 'deadline': started+_VERIFY_SILENCE,
                        'limit': started+_VERIFY_LIMIT, 'started': started, 'progress': 0,
                        'progress_at': started,
                        'cancelled': cancelled, 'discard': False, 'discard_reason': None,
                        'complete': complete, 'settled': settled,
                        'retry_operation': operation if retryable else None,
                        'retry_active': deepcopy(self.active), 'retry_epoch': self.resume_epoch}
        self.on_busy(True)
        self.status('正在核验任务清单和既有成果；不会重新执行已完成步骤。')

        def work():
            result, error = None, None
            marks = {'deadline': started+_VERIFY_SILENCE, 'count': 0, 'reason': None}

            def stop():
                now = self.clock()
                if not cancelled.is_set():
                    if now >= started+_VERIFY_LIMIT:
                        marks['reason'] = '核验已达到300秒总上限'
                        cancelled.set()
                    elif now >= marks['deadline']:
                        marks['reason'] = '核验已连续40秒没有新进度'
                        cancelled.set()
                return cancelled.is_set()

            def progress(message):
                # The callback runs inside the verification context. Only real
                # record/file checkpoints renew silence; stage text cannot.
                state = verification_progress._CURRENT.get()
                count = state.records+state.files if state is not None else 0
                if count <= marks['count'] or stop():
                    return
                at = self.clock()
                marks.update(count=count, deadline=at+_VERIFY_SILENCE)
                self.messages.put((generation, 'progress', at, (count, message), None))

            try:
                with verification_progress.verification_scope(stop, progress, '核验任务清单及祖先回执'):
                    # Shared display throttling can suppress a read's count
                    # checkpoint after its zero-count pre-read checkpoint.
                    # Observe the actual count immediately, without changing
                    # the shared state, methods or cancellation contract.
                    original = verification_progress._CURRENT.get()
                    token = verification_progress._CURRENT.set(_VerificationObserver(original, progress))
                    try:
                        with office_chain_payload.capture() as handoff:
                            state = operation()
                            verification_progress.checkpoint(stage='接收本次清单核验成果')
                            adoptable = state['status'] in {'ready', 'preview', 'complete', 'needs_scope', 'edit_preview', 'resume_available'}
                            verified = handoff.take(state, self.task_base) if adoptable else None
                        if adoptable and verified is None:
                            verification_progress.checkpoint(stage='重验清单当前成果')
                            verified = workspace_state.verify(state['payload'], self.task_base)
                        if state['status'] == 'edit_preview':
                            verification_progress.checkpoint(stage='重验编辑文件和原范围')
                            from .office_workspace import verify
                            verified['office_verified'] = verify(state['edit_payload'], self.task_base)
                        result = (state, verified)
                    finally:
                        verification_progress._CURRENT.reset(token)
            except Exception as exc:
                result = None
                error = verification_failure(exc)
            self.messages.put((generation, 'finished', self.clock(), result, marks['reason'] or error))

        try:
            threading.Thread(target=work, name='office-task-chain', daemon=True).start()
        except Exception:
            self.pending = None
            self.on_busy(False)
            self.status('无法启动清单核验，当前内容保留。')
            return False
        return True

    def _remember_failed_verification(self, pending):
        if (pending.get('retry_operation') is None or self.closed
                or not self.current(pending['context'])
                or self.active != pending['retry_active']
                or self.resume_epoch != pending['retry_epoch']):
            return False
        self.failed_verification = {
            'pending': pending, 'generation': self.generation,
        }
        return True

    def retry_verification(self):
        """Retry only a completed verification request, never a task execution."""
        if not self.idle():
            self.status('请等待当前核验或任务结束，再重新核验。')
            return False
        failed = self.failed_verification
        if failed is None:
            self.status('没有可原地重验的失败请求；请先找回清单或查看草稿。')
            return False
        pending = failed['pending']
        if (failed['generation'] != self.generation or not self.current(pending['context'])
                or self.active != pending['retry_active']
                or self.resume_epoch != pending['retry_epoch']):
            self.failed_verification = None
            self.status('输入、选择或清单已变化；旧重验请求已失效，当前内容保留。')
            return False
        if not self._begin(pending['retry_operation'], auto=pending['auto'],
                           complete=pending['complete'], settled=pending['settled'], retryable=True):
            return False
        if pending.get('resuming') is not None:
            self.pending['resuming'] = deepcopy(pending['resuming'])
        return True

    def create(self, instructions, snapshot):
        if not self.current(snapshot) or snapshot['workspace']['unavailable']:
            self.status('来源或草稿已变化，请重新打开清单；当前内容保留。')
            return False
        def operation():
            payload = deepcopy(snapshot['workspace']['payload'])
            if snapshot['scope'] == '当前成果批次':
                payload['source'] = None
            ident = core.create(self.store, instructions, payload,
                                task_base=self.task_base, computer_base=self.computer_base)
            return core.inspect(self.store, ident)
        return self._begin(operation)

    def restore(self, ident):
        snapshot = self.context()
        owned_pause = (self.active is not None and self.active['id'] == ident
                       and self.active['status'] == 'resume_available'
                       and self._owns_paused_input(snapshot, self.active))
        if snapshot['workspace']['unavailable'] or snapshot['goal']['draft']['text']:
            if not owned_pause:
                self.status('连续页已有草稿或来源未就绪，请先处理；清单不会覆盖它。')
                return False
        return self._begin(lambda: core.advance(self.store, ident), auto=owned_pause, retryable=True)

    def _owns_paused_input(self, context, state):
        return (not context['workspace']['unavailable']
                and self.issued == context['workspace']['revision']
                and self.route_context is not None
                and {k: context[k] for k in ('workspace', 'scope')} == self.route_context
                and context['goal']['draft']['text'].strip() ==
                    (state.get('edit_payload') or state['payload'])['draft'])

    def bind(self, session, instruction, routed, refs, source_ticket):
        """Called before existing UI adopts a routed preview; unrelated tasks pass."""
        restored, self._restore_route = self._restore_route, None
        if restored is not None:
            return self._show_restored_route(restored, session, instruction, routed, refs, source_ticket)
        if not self.active:
            return True
        if self.issued != source_ticket:
            self.active = self.issued = self.cleared = self.handoff = None
            self.route_context = None
            return True
        if instruction != self.active['payload']['draft']:
            return True
        try:
            self.active = core.bind_preview(self.store, self.active['id'], self.active['index'],
                                           session, routed, refs)
            context = self.context()
            self.route_context = deepcopy({key: context[key] for key in ('workspace', 'scope')})
        except (OSError, ValueError, KeyError, TypeError):
            self.status('本步骤无法绑定到完整的生成或核对计划；清单要求保留，未执行部分任务。')
            return False
        return True

    def _show_restored_route(self, restored, session, instruction, routed, refs, source_ticket):
        """Reuse this worker result only during its synchronous handoff."""
        try:
            state, context = restored['state'], self.context()
            binding = state['binding']
            if (self.closed or context['goal']['stopped']
                    or self.resume_epoch != restored['epoch'] or self.generation != restored['generation']
                    or self.active != state or context != restored['context']
                    or state['status'] not in {'preview', 'needs_scope'}
                    or self.issued != source_ticket or source_ticket != context['workspace']['revision']
                    or {k: context[k] for k in ('workspace', 'scope')} != self.route_context
                    or instruction != state['payload']['draft']
                    or binding != {'session_root': str(session.root), 'routed': routed, 'refs': refs}):
                return False
            plan = routed['plan']
            if state['status'] == 'needs_scope':
                return routed['state'] == 'needs_scope' and plan is None and state['run'] is None
            if routed['state'] != 'ready' or plan['mode'] not in {'capability', 'office_bundle'}:
                return False
            suffix = '.bundle' if plan['mode'] == 'office_bundle' else '.capability'
            run = session.root / 'goals' / (plan['sha256'] + suffix)
            saved = run.with_suffix('.json')
            workspace_state._safe_components(saved)
            workspace_state._safe_components(run)
            return (Path(state['run']) == run and not run.exists()
                    and workspace_state._read(saved, 200_000)[0] == plan)
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return False

    def before_plan(self, session, plan, ticket, result=None):
        """Bind an edit diff before the shared goal UI enables its approval."""
        restored, self._restore_display = self._restore_display, None
        if restored is not None:
            return self._show_restored_plan(restored, session, plan, ticket, result)
        state = self.active
        if (not state or not state.get('binding')
                or state['binding']['routed']['state'] != 'needs_scope' or plan is None):
            return True
        context = self.context()
        if (self.closed or context['goal']['stopped']
                or {key: context[key] for key in ('workspace', 'scope')} != self.route_context
                or self.issued != context['workspace']['revision']):
            return False
        continuation = (result or {}).get('_office_continuation') or {}
        clarified = (continuation.get('state') == 'resolved'
                     and continuation.get('original_instruction') == state['payload']['draft']
                     and ticket and continuation.get('instruction') == ticket['text'].strip()
                     and plan.get('instruction') == continuation['instruction'])
        if (not ticket or ticket != self.context()['goal']['draft']
                or ticket['text'].strip() != state['payload']['draft'] and not clarified):
            return False
        try:
            if result and result.get('office_result'):
                original = {k: result[k] for k in ('plan', 'reason', 'office_result')}
                self.active = core.bind_edit_preview(
                    self.store, state['id'], state['index'], session, plan, original,
                    self.office_context()['payload'])
            else:
                if self.office_context()['payload'] != state.get('edit_payload'):
                    return False
                self.active = core.bind_edit_refresh(
                    self.store, state['id'], state['index'], session, plan)
        except (OSError, ValueError, KeyError, TypeError):
            self.status('编辑差异与清单的原指令、来源或范围不符；未开放执行，旧记录保留。')
            return False
        return True

    def _show_restored_plan(self, restored, session, plan, ticket, result):
        """Consume this synchronous display only; execution keeps its own gates."""
        state = restored['state']
        context = self.context()
        if (result is not None or self.closed or context['goal']['stopped']
                or self.resume_epoch != restored['epoch']
                or self.generation != restored['generation']
                or self.active != state or context != restored['context']
                or state.get('status') != 'edit_preview'
                or not state.get('binding')
                or state['binding']['routed']['state'] != 'needs_scope'
                or {key: context[key] for key in ('workspace', 'scope')} != self.route_context
                or self.issued != context['workspace']['revision']
                or ticket != context['goal']['draft'] or not ticket
                or ticket['text'].strip() != state['edit_payload']['draft']
                or plan != state['edit_plan']
                or self.office_context()['payload'] != state['edit_payload']
                or str(session.root) != state['binding']['session_root']):
            return False
        try:
            # The worker verified ancestry. Recheck the small saved plan and
            # dispatch boundary here without rebuilding that ancestry on Tk.
            run = Path(state['run'])
            expected = session.root / 'goals' / (plan['sha256'] + '.run')
            if run != expected:
                return False
            saved = expected.with_suffix('.json')
            workspace_state._safe_components(saved)
            if workspace_state._read(saved, 200_000)[0] != plan:
                return False
            workspace_state._safe_components(run)
            if run.exists():
                return False
        except (OSError, ValueError, TypeError, KeyError):
            return False
        return True

    def instruction_cleared(self, submitted, cleared):
        if self.active and submitted == self.issued:
            context = self.context()
            self.cleared = ({'revision': deepcopy(context['workspace']['revision']),
                             'goal': {k: deepcopy(v) for k, v in context['goal'].items() if k != 'plan'},
                             'scope': context['scope']} if cleared else None)
            self.handoff = None

    def artifacts_announced(self, records, *, history_before=None):
        if not self.active or self.cleared is None:
            return
        context = self.context()
        # The same Tk callback refreshes history after a routed task succeeds.
        # Its existing source invalidation may increment this generation once;
        # absorb only that precisely bracketed change, preserving user edits.
        after = {k: deepcopy(v) for k, v in context['goal'].items() if k != 'plan'}
        before = ({k: v for k, v in history_before.items() if k != 'plan'}
                  if type(history_before) is dict else None)
        owned_refresh = (before == self.cleared['goal']
                and type(before.get('source_generation')) is int
                and type(after.get('source_generation')) is int
                and after['source_generation'] - before['source_generation'] in (0, 1)
                and {k: v for k, v in before.items() if k != 'source_generation'}
                == {k: v for k, v in after.items() if k != 'source_generation'})
        if history_before is not None and not owned_refresh:
            self.handoff = None
            return
        if owned_refresh:
            self.cleared['goal'] = after
        self.handoff = {'records': deepcopy(records),
                        'generation': context['workspace']['binding']['source']['generation'],
                        'selected': context['workspace']['binding']['selected']}

    def _adopt(self, state, verified, *, auto, before):
        context = self.context()
        payload = state['payload']
        current = context['workspace']['payload']
        if state['status'] == 'resume_available':
            # A paused stage still owns its submitted instruction. Do not clear
            # it, replace the input with a partial output, or replay the route.
            owned = self._owns_paused_input(context, state)
            if auto and not owned:
                self.status('剩余步骤已保存；当前输入保留，请从任务清单找回。')
                return False
            if not auto:
                if (context['workspace']['unavailable'] or context['goal']['draft']['text']
                        or (current['draft'] and current['draft'] != payload['draft'])
                        or (current['records'] and current['records'] != payload['records'])):
                    self.status('当前有其他草稿或成果；请先保存，再找回清单的剩余步骤。')
                    return False
                if not self.apply(verified):
                    return False
                self.set_scope('当前选中资料' if state['scope'] == 'selected' else '当前成果批次')
                self.issued = deepcopy(self.context()['workspace']['revision'])
            return self.resume_dialog(state)
        if before.get('resuming') is not None:
            previous = before['resuming']
            if (state['id'] != previous['id'] or state['index'] != previous['index'] + 1
                    or state['status'] not in {'ready', 'complete'}):
                self.status('续跑尚未完整核验；原指令和剩余步骤保留。')
                return False
            if self.clear_resume_goal is None or not self.clear_resume_goal(context['goal']['draft']):
                self.status('续跑已核验，当前新稿保留；请稍后找回下一步骤。')
                return False
            context = self.context()
        if context['workspace']['unavailable'] or context['goal']['draft']['text']:
            self.status('清单进度已保存；当前连续页有新内容，待办未覆盖它，请稍后找回。')
            return False
        if auto:
            allowed = (self.cleared is not None and self.handoff is not None
                       and context['workspace']['revision'] == self.cleared['revision']
                       and {k: v for k,v in context['goal'].items() if k != 'plan'} == self.cleared['goal']
                       and context['goal']['plan'] is None and context['scope'] == self.cleared['scope']
                       and context['workspace']['binding']['source']['generation'] == self.handoff['generation']
                       and context['workspace']['binding']['selected'] == self.handoff['selected']
                       and current['source'] == self.handoff['records'][-1]
                       and payload['records'] == self.handoff['records']
                       and not current['draft'] and current['records'] == payload['records']
                       and not context['goal']['stopped'])
        else:
            # Creation may replace the explicitly confirmed first instruction;
            # history restore never overwrites another draft or source batch.
            allowed = (not current['draft'] or current['draft'] == payload['draft']
                       or before.get('creating') is True or before.get('resuming') is not None)
            if before.get('creating') is not True and before.get('resuming') is None:
                allowed = allowed and (not current['records'] or current['records'] == payload['records'])
        if not allowed:
            self.status('清单进度已保存；当前新草稿或来源保留，请在空白工作区找回待办。')
            return False
        if not self.apply(verified):
            self.status('清单进度已保存，但输入接入未完成；请稍后找回。')
            return False
        self.set_scope('当前选中资料' if state['scope'] == 'selected' else '当前成果批次')
        self.issued = deepcopy(self.context()['workspace']['revision'])
        context = self.context()
        self.route_context = deepcopy({key: context[key] for key in ('workspace', 'scope')})
        self.cleared = None
        self.handoff = None
        if state['status'] in {'preview', 'needs_scope', 'edit_preview'}:
            from .computer import ComputerSession
            binding = state['binding']
            expected = deepcopy(self.context()['workspace'])
            try:
                if state['status'] == 'edit_preview':
                    accepted = self.apply_office(verified['office_verified'])
                    if accepted:
                        self._restore_display = {'state': deepcopy(state),
                            'context': deepcopy(self.context()), 'epoch': self.resume_epoch,
                            'generation': self.generation}
                        try:
                            accepted = self.prepare_edit(state)
                        finally:
                            self._restore_display = None
                else:
                    self._restore_route = {'state': deepcopy(state), 'context': deepcopy(self.context()),
                                           'epoch': self.resume_epoch, 'generation': self.generation}
                    try:
                        accepted = self.prepare(ComputerSession(binding['session_root']), payload['draft'],
                                                binding['routed'], binding['refs'],
                                                lambda: not self.closed and self.context()['workspace'] == expected)
                    finally:
                        self._restore_route = None
            except (OSError, ValueError, KeyError, TypeError):
                accepted = False
            self.status(('编辑要求已找回；请确认文件与范围，再规划差异。' if state['status'] == 'needs_scope'
                         else '已找回原预览；请重新核对并批准，未重复调用模型。') if accepted else
                        '输入已找回，原预览未接入；请检查连续页，未重复调用模型。')
            return bool(accepted)
        self.status('清单全部步骤已完成并核验，成果可继续使用。' if state['status'] == 'complete' else
                    f'第{state["index"]+1}/{state.get("total", 2)}步已接入；点击“按任务规划”，核对后单独批准。')
        return True

    @staticmethod
    def _draft_records(state):
        return [(entry['index'], record) for entry in state.get('deliveries', [])
                for record in entry['records'] if Path(record['path']).suffix.lower() == '.eml']

    def view_drafts(self):
        """Read current durable proofs without adopting or executing a task."""
        if self.active is None:
            self.status('请先找回任务清单，再查看其中已完成的邮件草稿。')
            return False
        ident = self.active['id']
        return self._begin(lambda: core.inspect(self.store, ident),
                           complete=lambda state, _verified: self._drafts_dialog(state), retryable=True)

    def preview_draft(self, ident, index, record):
        """Recheck the whole chain again when a dialog selection is opened."""
        if (self.active is None or self.active['id'] != ident or self.open_draft is None
                or Path(record.get('path', '')).suffix.lower() != '.eml'):
            self.status('清单或草稿选择已变化；请重新找回并核验。')
            return False
        selected = deepcopy(record)

        def show(state, _verified):
            if (self.active is None or self.active['id'] != ident or state['id'] != ident
                    or (index, selected) not in self._draft_records(state)):
                self.status('草稿已不属于当前核验的清单；未打开。')
                return
            if self.open_draft(selected):
                self.status('已打开本地未发送草稿；任务输入和清单进度未改变。')
                return True
            return False

        return self._begin(lambda: core.inspect(self.store, ident), complete=show, retryable=True)

    def _drafts_dialog(self, state):
        if self.active is None or self.active['id'] != state['id']:
            self.status('当前清单已变化；请重新查看草稿。')
            return False
        choices = self._draft_records(state)
        if not choices:
            self.status('这份清单尚无已完成且通过核验的邮件草稿。')
            return False
        import tkinter as tk
        from tkinter import ttk
        win = tk.Toplevel(self.root)
        win.title('任务清单 · 本地未发送草稿')
        win.geometry('560x280')
        ttk.Label(win, text='草稿保留在原任务中；预览前再次核验。下一任务只使用Word/Excel。',
                  wraplength=530).pack(anchor='w', padx=12, pady=10)
        tree = ttk.Treeview(win, columns=('step', 'file'), show='headings', height=5)
        tree.heading('step', text='清单项')
        tree.heading('file', text='草稿')
        tree.column('step', width=80)
        tree.column('file', width=400)
        tree.pack(fill='both', expand=True, padx=12)
        for n, (index, record) in enumerate(choices):
            tree.insert('', 'end', iid=str(n), values=(index+1, Path(record['path']).name))

        def accept():
            selected = tree.selection()
            if len(selected) == 1:
                index, record = choices[int(selected[0])]
                if self.preview_draft(state['id'], index, record):
                    win.destroy()

        ttk.Button(win, text='核验并预览（未发送）', command=accept).pack(anchor='e', padx=12, pady=10)
        return True

    def resume_dialog(self, state):
        """Offer the exact durable continuation; an explicit click is required."""
        if self.start_resume is None:
            self.status(state['reason'])
            return False
        from tkinter import ttk
        from .office_scope_panel import _modal
        snapshot = deepcopy(self.context())
        self.resume_epoch += 1
        epoch = self.resume_epoch
        win, close = _modal(self.root, '继续清单未完成步骤', '660x240')
        preview = state['resume_preview']
        ttk.Label(win, text=(f'第{state["index"]+1}/{state.get("total", 2)}项：{state["payload"]["draft"]}\n\n'
                  f'已有 {preview["completed"]}/{preview["total"]} 步核验，只继续剩余 {preview["remaining"]} 步。\n'
                  '保留原暂停/失败记录；本次不再调用模型，不重做已完成步骤。'),
                  wraplength=610).pack(anchor='w', padx=16, pady=16)
        consumed = False
        def accept():
            nonlocal consumed
            if consumed or epoch != self.resume_epoch or not self.idle() or not self.current(snapshot):
                self.status('输入或运行状态已变化；本次续跑未启动，请重新找回清单。')
                return
            consumed = True
            attempt = self.start_resume(deepcopy(state))
            if not attempt:
                self.status('剩余步骤未启动，原记录保留；请重新核验。')
                return
            # launch owns one synchronous invalidation of the goal panel.
            self.resume_owned = {'id': state['id'], 'index': state['index'],
                'task_root': state['task_root'], 'attempt': attempt,
                'context': deepcopy(self.context())}
            close()
        ttk.Button(win, text='核对后继续剩余步骤', command=accept).pack(anchor='e', padx=16, pady=10)
        self.status(state['reason'])
        return True

    def resume_finished(self, task_root, attempt_id, *, stopped=False):
        """Consume only our exact attempt, then independently verify its proof."""
        owned = self.resume_owned
        if not owned or (str(task_root), attempt_id) != (owned['task_root'], owned['attempt']):
            return False
        self.resume_owned = None
        if stopped or not self.current(owned['context']):
            self.status('续跑结果已保存；停止或当前输入变化，未覆盖草稿。请稍后找回清单。')
            return True
        if self._begin(lambda: core.advance(self.store, owned['id'], resume_attempt=attempt_id,
                                           resume_index=owned['index']), retryable=True):
            self.pending['resuming'] = owned
        return True

    def poll(self):
        pending = self.pending
        if pending and not pending['discard'] and not self.current(pending['context']):
            self._discard('核验期间输入已变化')
        latest_progress = None
        while True:
            try:
                message = self.messages.get_nowait()
            except queue.Empty:
                break
            # Retain the former completion envelope for existing local callers.
            if len(message) == 3:
                generation, result, error = message
                kind, at = 'finished', self.clock()
            else:
                generation, kind, at, result, error = message
            if not self.pending or generation != self.generation:
                continue
            pending = self.pending
            if not pending['discard']:
                if at >= pending['limit']:
                    self._discard('核验已达到300秒总上限')
                elif at >= pending['deadline']:
                    self._discard('核验已连续40秒没有新进度')
            if kind == 'progress':
                count, text = result
                if (not pending['discard'] and at >= pending['progress_at']
                        and count > pending['progress']):
                    pending.update(progress=count, progress_at=at, deadline=at+_VERIFY_SILENCE)
                    latest_progress = (generation, text)
                continue
            pending, self.pending = self.pending, None
            self.on_busy(False)
            if pending.get('settled') is not None:
                try:
                    pending['settled']()
                except Exception:
                    self.status('等待状态未能恢复；当前输入和已保存进度保留。')
                    continue
            if pending['discard'] or not self.current(pending['context']):
                reason = pending['discard_reason'] or '核验期间输入已变化'
                retry = reason in _TIMEOUT_REASONS and self._remember_failed_verification(pending)
                self.status(reason+'；核验工作已结束，当前输入和已保存进度保留。'+
                            ('可点击“重新核验”，不会重做任务。' if retry else '请稍后找回。'))
                continue
            if error or result is None:
                retry = self._remember_failed_verification(pending)
                self.status((error or '清单尚未完成核验。')+
                            ('处理原因后可点击“重新核验”。' if retry else ''))
                continue
            state, verified = result
            if pending.get('complete') is not None:
                try:
                    delivered = pending['complete'](state, verified)
                except Exception as exc:
                    retry = self._remember_failed_verification(pending)
                    self.status(verification_failure(exc)+
                                ('处理原因后可点击“重新核验”。' if retry else ''))
                    continue
                if delivered is False:
                    # The preview callback already reported its specific cause.
                    # Keep that diagnostic while retaining the same read request.
                    self._remember_failed_verification(pending)
                continue
            self.active = state
            if verified is None:
                self.status(state['reason'])
                continue
            self._adopt(state, verified, auto=pending['auto'], before=pending)
        # Timely queued progress/completion must be consumed before testing the
        # UI's current clock. Polling delay cannot turn finished work into timeout.
        pending = self.pending
        if pending and not pending['discard']:
            if self.clock() >= pending['limit']:
                self._discard('核验已达到300秒总上限')
            elif self.clock() >= pending['deadline']:
                self._discard('核验已连续40秒没有新进度')
        if (latest_progress is not None and self.pending and not self.pending['discard']
                and latest_progress[0] == self.generation):
            self.status(latest_progress[1])
        # Observe only the precise bound run, never search unrelated task history.
        if (self.idle() and self.active and self.active.get('binding')
                and self.active.get('status') not in {'resume_available', 'blocked'}):
            binding = self.active['binding']
            plan = binding['routed']['plan']
            if self.active.get('run'):
                outcome = Path(self.active['run'])/'outcome.json'
            elif plan is not None:
                suffix = '.capability' if plan['mode'] == 'capability' else '.bundle'
                outcome = Path(binding['session_root'])/'goals'/(plan['sha256']+suffix)/'outcome.json'
            else:
                return
            token = (self.active['id'], self.active['index'], str(outcome))
            if token not in self.observed and outcome.is_file():
                self.observed.add(token)
                self._begin(lambda: core.advance(self.store, token[0]), auto=True, retryable=True)

    def _discard(self, reason):
        if self.pending and not self.pending['discard']:
            self.pending.update(discard=True, discard_reason=reason)
            self.pending['cancelled'].set()
            self.status(reason+'；等待核验结束，当前输入和已保存进度保留。')

    def cancel(self):
        self.resume_epoch += 1
        self.failed_verification = None
        if self.pending:
            self._discard('已停止清单接入')
        self.cleared = None
        self.handoff = None
        if self.resume_owned:
            self.resume_owned['context'] = None
        self.status('已停止清单接入；等待核验结束，记录保留，不自动执行。')

    def close(self):
        self.closed = True
        self.cancel()

    def new_dialog(self):
        if not self.idle():
            return False
        context = deepcopy(self.context())
        batch = context['scope'] == '当前成果批次'
        payload = context['workspace']['payload']
        if context['workspace']['unavailable'] or not (payload['records'] if batch else payload['source']):
            self.status('请先核验第一步的选中资料或当前 Word/Excel 成果批次。')
            return False
        if context['goal']['draft']['text'] or context['goal']['plan']:
            self.status('连续页尚有草稿或计划，请先处理；原内容保留。')
            return False
        from tkinter import ttk
        import tkinter as tk
        from .office_scope_panel import _modal
        win, close = _modal(self.root, '办公任务清单（2至4步）', '760x580')
        ttk.Label(win, text=('第一步使用当前全部成果批次' if batch else '第一步使用当前选中资料')
                  + '；后续每步使用上一步生成的全部 Word/Excel。\n'
                  '每步分别规划和批准；不会自动发送邮件、修改原件或删除文件。', wraplength=720).pack(anchor='w', padx=12, pady=10)
        fields = []
        for number in range(1, core.MAX_STEPS + 1):
            hint = '必填' if number <= 2 else '可选；不用的末尾步骤留空'
            ttk.Label(win, text=f'第{number}步（{hint}，最多4000字）：').pack(anchor='w', padx=12, pady=5)
            field = tk.Text(win, height=3, wrap='word')
            field.pack(fill='x', padx=12)
            fields.append(field)
        fields[0].insert('1.0', context['workspace']['payload']['draft'])
        consumed = False
        def accept():
            nonlocal consumed
            instructions = [field.get('1.0', 'end-1c').strip() for field in fields]
            while instructions and not instructions[-1]:
                instructions.pop()
            try:
                instructions = core._instructions(instructions)
            except ValueError:
                self.status('请连续填写2至4条完整要求，每条1至4000字，不含控制字符；仅末尾可留空。')
                return
            if not consumed and self.create(instructions, context):
                consumed = True
                self.pending['creating'] = True
                close()
        ttk.Button(win, text='保存清单并接入第一步', command=accept).pack(anchor='e', padx=12, pady=12)
        return True

    def history_dialog(self):
        if not self.idle():
            return False
        try:
            items = core.list_chains(self.store)
        except (OSError, ValueError):
            items = []
        if not items:
            self.status('没有可读取的本机清单。')
            return False
        from tkinter import ttk
        import tkinter as tk
        from .office_scope_panel import _modal
        win, close = _modal(self.root, '找回办公任务清单', '780x520')
        ttk.Label(win, text='选择清单后重新核验进度；不重做已完成步骤，不恢复旧批准。').pack(padx=12, pady=10)
        tree = ttk.Treeview(win, columns=('count', 'first'), show='headings', selectmode='browse', height=6)
        for key, title in [('count','总步数'),('first','第一步（下方查看完整要求）')]:
            tree.heading(key,text=title)
            tree.column(key,width=65 if key == 'count' else 665)
        tree.pack(fill='both',expand=True,padx=12)
        for n,item in enumerate(items):
            tree.insert('','end',iid=str(n),values=(len(item['instructions']), item['instructions'][0]))
        details = tk.Text(win, height=8, wrap='word', state='disabled')
        details.pack(fill='both', expand=True, padx=12, pady=8)
        def selected(_event=None):
            chosen = tree.selection()
            text = ('\n\n'.join(f'第{n}步：{value}' for n, value in
                               enumerate(items[int(chosen[0])]['instructions'], 1)) if len(chosen) == 1 else '')
            details.configure(state='normal')
            details.delete('1.0', 'end')
            details.insert('1.0', text)
            details.configure(state='disabled')
        tree.bind('<<TreeviewSelect>>', selected)
        consumed = False
        def accept():
            nonlocal consumed
            chosen=tree.selection()
            if not consumed and len(chosen)==1 and self.restore(items[int(chosen[0])]['id']):
                consumed=True
                close()
        ttk.Button(win,text='核验并接续当前步骤',command=accept).pack(anchor='e',padx=12,pady=10)
        return True
