"""Keep chain route verification off Tk; retain the original adoption gates."""
from copy import deepcopy

from . import office_task_chain as core
from .computer import ComputerSession
from .office_task_entry import DEFERRED_ROUTE


def _session_root(session):
    return str(session.root) if session is not None else None


class OfficeChainRouteBinding:
    def __init__(self, goal, chain, *, adopt, source_ticket, set_waiting):
        self.goal, self.chain = goal, chain
        self.adopt, self.source_ticket, self.set_waiting = adopt, source_ticket, set_waiting
        self.pending = None

    def _status(self, text):
        self.chain.status(text)
        return False

    def _revoke(self):
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

    def prepare(self, session, instruction, routed, refs, is_current):
        g, c = self.goal, self.chain
        if self.pending is not None:
            return self._status('正在后台核验任务路由，请等待或停止；尚未接入。')
        try:
            ticket = deepcopy(self.source_ticket())
            state = c.active
            if (c.closed or g.closed or g.stopped.is_set() or not is_current()):
                return False
            if (c._restore_route is not None or not state or c.issued != ticket
                    or instruction != state['payload']['draft']):
                if not c.bind(session, instruction, routed, refs, ticket):
                    return False
                return self.adopt(session, instruction, routed, refs, is_current, ticket)
            context = c.context()
            if (not c.idle() or context['goal']['stopped']
                    or context['workspace']['revision'] != ticket
                    or {k: context[k] for k in ('workspace', 'scope')} != c.route_context
                    or context['goal']['draft']['text'].strip() not in {'', instruction}
                    or routed['state'] not in {'ready', 'needs_scope'}):
                return self._status('任务、来源或草稿已变化；原输入保留，请重新核验。')
            request = {'state': deepcopy(state), 'session': _session_root(session),
                       'target_session': _session_root(g.session), 'ticket': ticket,
                       'instruction': instruction, 'routed': deepcopy(routed), 'refs': deepcopy(refs),
                       'epoch': c.resume_epoch}
            g.invalidate()
            request['context'] = deepcopy(c.context())
        except Exception:
            self._revoke()
            return self._status('任务路由缺少完整绑定；原输入和记录保留，未开放执行。')
        store = c.store

        def operation():
            return core.bind_preview(store, request['state']['id'], request['state']['index'],
                ComputerSession(request['session']), request['routed'], request['refs'])

        def settled():
            if self.pending is request:
                self.pending = None
                self.set_waiting(False)

        self.pending = request
        try:
            self.set_waiting(True)
            started = c._begin(operation, complete=lambda state, verified:
                self._complete(request, session, is_current, state, verified), settled=settled)
        except Exception:
            started = False
        if not started:
            settled()
            return self._status('任务路由核验未启动；原输入和记录保留，请稍后重试。')
        self._status('正在后台核验任务路由和既有成果，可停止；完成后再核对范围或批准。')
        return DEFERRED_ROUTE

    def _complete(self, request, session, is_current, state, verified):
        g, c = self.goal, self.chain
        try:
            if (g.closed or c.closed or g.stopped.is_set()
                    or c.resume_epoch != request['epoch'] or c.active != request['state']
                    or c.context() != request['context'] or self.source_ticket() != request['ticket']
                    or _session_root(g.session) != request['target_session']
                    or _session_root(session) != request['session'] or not is_current()
                    or state['id'] != request['state']['id'] or state['index'] != request['state']['index']
                    or state['payload'] != request['state']['payload']
                    or state['status'] not in {'preview', 'needs_scope'} or verified is None
                    or state['binding'] != {'session_root': request['session'],
                                           'routed': request['routed'], 'refs': request['refs']}):
                return self._status('核验期间输入或会话已变化；当前内容保留，已保存进度可从清单找回。')
            c.active = deepcopy(state)
            c.route_context = {k: deepcopy(request['context'][k]) for k in ('workspace', 'scope')}
            c._restore_route = {'state': deepcopy(state), 'context': deepcopy(c.context()),
                                'epoch': c.resume_epoch, 'generation': c.generation}
            try:
                if not c.bind(session, request['instruction'], request['routed'], request['refs'], request['ticket']):
                    return self._status('已保存的路由或计划发生变化；未接入，请从清单重新核验。')
                accepted = self.adopt(session, request['instruction'], request['routed'],
                                      request['refs'], is_current, request['ticket'])
            finally:
                c._restore_route = None
            if accepted is not True:
                self._revoke()
                return self._status('本次路由尚未接入；原输入和记录保留，请核对后继续。')
            return self._status('规划已接入连续任务页；请核对来源和范围后批准。')
        except Exception:
            self._revoke()
            return self._status('路由接入未完成；原输入和已保存清单保留，请从清单找回。')

    def cancel(self):
        if self.pending is not None:
            self.chain.cancel()
