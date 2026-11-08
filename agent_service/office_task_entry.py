"""Main-page routing coordinator. Workers never touch widgets or execute plans."""
from copy import deepcopy
from pathlib import Path
import queue
import threading
import time


DEFERRED_ROUTE = object()


class OfficeTaskEntry:
    def __init__(self, *, context, busy, on_busy, prepare, status, session_factory,
                 task_base, clock=time.monotonic):
        self.context, self.busy, self.on_busy = context, busy, on_busy
        self.prepare, self.status, self.session_factory = prepare, status, session_factory
        self.task_base, self.clock = task_base, clock
        self.session = None
        self.pending = None
        self.messages = queue.Queue()
        self.closed = False
        self.generation = 0

    def _current(self, snapshot, generation, *, include_draft=False):
        if self.closed or generation != self.generation:
            return False
        try:
            current = deepcopy(self.context())
            if not include_draft:
                # Target adoption/completion deliberately changes its revision;
                # the source instruction remains bound for the plan's lifetime.
                current.pop('draft', None)
                current.pop('draft_revision', None)
                snapshot = {key: value for key, value in snapshot.items()
                            if key not in {'draft', 'draft_revision'}}
            return current == snapshot
        except Exception:
            return False

    def start(self):
        if self.closed or self.pending or self.busy():
            return False
        try:
            context = deepcopy(self.context())
            instruction, records = context['instruction'], context['records']
            if (type(instruction) is not str or not 1 <= len(instruction) <= 4000
                    or instruction != instruction.strip()):
                raise ValueError('请填写 1 至 4000 字的完整任务要求。')
            if context.get('unavailable'):
                raise ValueError('资料绑定尚未就绪；请先完成来源核验。')
            if context.get('draft') and context['draft'].strip() != instruction:
                raise ValueError('连续任务页已有不同草稿，请先自行处理；原草稿已保留。')
            if type(records) is not list or not 1 <= len(records) <= 4:
                raise ValueError('请选择一份已核验的办公成果，或当前批次的 1 至 4 份成果。')
            paths = []
            for record in records:
                if (type(record) is not dict or set(record) != {'path', 'sha256'}
                        or type(record['path']) is not str or not Path(record['path']).is_absolute()
                        or Path(record['path']).suffix.lower() not in {'.docx', '.xlsx'}
                        or type(record['sha256']) is not str or len(record['sha256']) != 64
                        or any(c not in '0123456789abcdef' for c in record['sha256'])):
                    raise ValueError('所选成果缺少有效证明，请重新核验。')
                paths.append(str(Path(record['path']).resolve()))
            if len(set(paths)) != len(paths):
                raise ValueError('来源重复，请重新选择。')
        except (ValueError, KeyError, TypeError) as exc:
            self.status(str(exc) if type(exc) is ValueError else '任务或来源格式无效。')
            return False
        self.generation += 1
        generation = self.generation
        stop = threading.Event()
        self.pending = {'context': context, 'generation': generation, 'stop': stop,
                        'deadline': self.clock() + 95, 'discard': False}
        self.on_busy(True)
        self.status('正在核验所选成果并选择能力；这一步只上传指令、文件名和格式。')

        def work():
            result = None
            error = None
            try:
                from .office_handoff import select_outputs
                from .daily_task_routing import propose
                refs = select_outputs(records, base=self.task_base)
                expected = context.get('expected_refs')
                if expected is not None and refs != expected:
                    raise ValueError('task_entry_reference_changed')
                if not stop.is_set():
                    if self.session is None:
                        self.session = self.session_factory()
                    routed = propose(self.session, instruction, refs,
                                     task_base=self.task_base, stop=stop.is_set)
                    result = (self.session, routed, refs)
            except Exception:
                # Never surface provider details, credentials or document text.
                error = '规划未完成：来源核验、预算或模型调用未通过；原记录已保留，未执行操作。'
            self.messages.put((generation, result, error))

        try:
            threading.Thread(target=work, name='office-task-planning', daemon=True).start()
        except Exception:
            self.pending = None
            self.on_busy(False)
            self.status('未能启动任务规划，请稍后再试。')
            return False
        return True

    def cancel(self):
        if self.pending:
            self.pending['discard'] = True
            self.pending['stop'].set()
            self.status('已请求停止规划；等待当前调用结束，不自动重试。')

    def close(self):
        self.closed = True
        self.cancel()

    def poll(self):
        pending = self.pending
        if pending and not pending['discard']:
            if (self.clock() >= pending['deadline'] or not self._current(
                    pending['context'], pending['generation'], include_draft=True)):
                pending['discard'] = True
                pending['stop'].set()
                self.status('任务、来源或草稿已变化，或规划超时；旧结果不会接入，请待调用结束。')
        while True:
            try:
                generation, result, error = self.messages.get_nowait()
            except queue.Empty:
                break
            pending = self.pending
            if not pending or generation != pending['generation']:
                continue
            self.pending = None
            self.on_busy(False)
            context = pending['context']
            if (pending['discard'] or pending['stop'].is_set()
                    or not self._current(context, generation, include_draft=True)):
                self.status('旧规划已结束并保留记录，未接入；可核对当前要求后重新规划。')
                continue
            if error or result is None:
                self.status(error or '规划已停止，未执行操作。')
                continue
            session, routed, refs = result
            if routed.get('state') not in {'ready', 'needs_scope'}:
                self.status(str(routed.get('reason') or '任务需要补充要求或当前能力不支持，未执行操作。'))
                continue
            try:
                accepted = self.prepare(session, context['instruction'], routed, refs,
                                        lambda: self._current(context, generation))
            except Exception:
                accepted = False
            if accepted is DEFERRED_ROUTE:
                continue
            self.status(('规划已接入连续任务页；请核对来源和范围后批准。' if accepted else
                         '未接入规划：来源、草稿或计划已失效。原记录已保留，请重新核验。'))
