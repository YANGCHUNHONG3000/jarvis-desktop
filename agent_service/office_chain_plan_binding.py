"""Defer only chain-owned edit bindings; original display/execution stay intact."""
from copy import deepcopy

from . import office_task_chain as core
from .computer import ComputerSession


class OfficeChainPlanBinding:
    def __init__(self, goal, chain):
        self.goal, self.chain = goal, chain
        self.render = goal.show_plan
        goal.show_plan = self.show_plan

    def _guard(self):
        provider = getattr(self.goal, 'office_planning_context', None)
        return deepcopy(provider()) if callable(provider) else None

    def _reject(self, text, *, revoke=False):
        if revoke:
            self._clear_failed_display()
        self.goal.status.set(text)
        self.chain.status(text)
        return False

    def _clear_failed_display(self):
        # Rendering itself may have failed. Revocation cannot depend on the
        # failing renderer or on a widget still existing during window close.
        g = self.goal
        g.plan = g.plan_instruction_binding = None
        try:
            g.execute_button.configure(state='disabled')
        except Exception:
            pass
        try:
            g.invalidate()
        except Exception:
            pass

    def show_plan(self, plan, *, instruction_ticket=None, planning_result=None):
        g, c = self.goal, self.chain
        state = c.active
        if (c._restore_display is not None or not state or not state.get('binding')
                or state['binding']['routed']['state'] != 'needs_scope' or plan is None):
            return self.render(plan, instruction_ticket=instruction_ticket,
                               planning_result=planning_result)
        if not c.idle():
            return self._reject('正在核验本次清单编辑预览，请等待或停止；尚未开放执行。')
        ticket = instruction_ticket
        try:
            context = c.context()
            office = c.office_context()['payload']
            continuation = (planning_result or {}).get('_office_continuation')
            answered = (type(continuation) is dict and continuation.get('state') == 'resolved'
                        and continuation.get('answers')
                        and continuation.get('original_instruction') == state['payload']['draft']
                        and ticket and continuation.get('instruction') == ticket['text'].strip()
                        and plan.get('instruction') == continuation['instruction'])
            if (g.closed or g.stopped.is_set() or context['goal']['stopped']
                    or {k: context[k] for k in ('workspace', 'scope')} != c.route_context
                    or c.issued != context['workspace']['revision']
                    or not ticket or ticket != context['goal']['draft']
                    or ticket['text'].strip() != state['payload']['draft'] and not answered
                    or str(g.session.root) != state['binding']['session_root']
                    or planning_result is not None and '_office_guard' in planning_result
                    and self._guard() != planning_result['_office_guard']):
                return self._reject('指令、来源或范围已变化；本次差异未接入，当前输入保留。', revoke=True)
            original = (deepcopy({k: planning_result[k] for k in ('plan', 'reason', 'office_result')})
                        if planning_result and planning_result.get('office_result') else None)
            if original is None and office != state.get('edit_payload'):
                return self._reject('文件或编辑范围已变化，请重新规划；原记录保留。', revoke=True)
            request = {'state': deepcopy(state), 'plan': deepcopy(plan),
                       'ticket': deepcopy(ticket), 'result': deepcopy(planning_result),
                       'office': deepcopy(office), 'session': str(g.session.root),
                       'epoch': c.resume_epoch}
        except Exception:
            self._clear_failed_display()
            return self._reject('本次编辑预览缺少完整绑定；原要求和成果保留。')

        # Drop the former approval before taking our pending snapshot. A local
        # refresh's incoming guard legitimately still contains that former plan.
        try:
            g.invalidate()
            request['guard'] = self._guard()
        except Exception:
            self._clear_failed_display()
            return self._reject('无法准备预览核验；原要求和成果保留，未开放执行。')
        store = c.store

        def operation():
            session = ComputerSession(request['session'])
            if original is not None:
                return core.bind_edit_preview(store, request['state']['id'], request['state']['index'],
                    session, request['plan'], original, request['office'])
            return core.bind_edit_refresh(store, request['state']['id'], request['state']['index'],
                                          session, request['plan'])

        def complete(updated, verified):
            self._complete(request, updated, verified)

        if c._begin(operation, complete=complete):
            self._reject('正在后台核验编辑差异和清单来源，可停止；核验通过后再批准。')
        else:
            self._reject('清单编辑核验未启动；输入和旧记录保留，请重新预览。')
        # GoalPanel.poll must not show its result/reason or call its result hook
        # until the binding worker has completed with this exact UI context.
        return False

    def _complete(self, request, state, verified):
        g, c = self.goal, self.chain
        try:
            if (g.closed or g.stopped.is_set() or c.closed
                    or c.resume_epoch != request['epoch'] or c.active != request['state']
                    or str(g.session.root) != request['session']
                    or self._guard() != request['guard']
                    or g.draft_ticket() != request['ticket']
                    or state['id'] != request['state']['id']
                    or state['index'] != request['state']['index']
                    or state['status'] != 'edit_preview' or state['edit_plan'] != request['plan']
                    or state['edit_payload'] != request['office']
                    or verified is None or not verified.get('office_verified')):
                return self._reject('核验期间输入或会话已变化；未开放执行，已保存进度可从清单找回。')
            c.active = deepcopy(state)
            c._restore_display = {'state': deepcopy(state), 'context': deepcopy(c.context()),
                                  'epoch': c.resume_epoch, 'generation': c.generation}
            try:
                accepted = self.render(deepcopy(request['plan']),
                    instruction_ticket=deepcopy(request['ticket']), planning_result=None)
            finally:
                c._restore_display = None
            if accepted is not True:
                return self._reject('本次差异尚未开放执行；原记录保留，可从清单找回。')
            result = request['result']
            g.status.set(result.get('reason', '编辑差异已核验，请核对并批准。')
                         if result else '编辑差异已核验，请核对并批准。')
            callback = getattr(g, 'on_office_planning_result', None)
            recovery = getattr(g, 'intent_recovery', None)
            if recovery is not None and result is not None and '_office_continuation' in result:
                recovery.on_result(result)
            elif callable(callback) and result is not None and '_office_guard' in result:
                callback(result, request['ticket'], result['_office_guard'])
            return True
        except Exception:
            self._clear_failed_display()
            return self._reject('预览接入未完成；当前输入和已保存清单保留，请从清单找回。')
