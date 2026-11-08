"""Prepare replacement Office inputs without installing them or executing work."""
from copy import deepcopy
import queue
import threading
import time
import uuid

from . import office_workspace, workspace_state


def record_failure(store, saved_id, error):
    """Keep a small immutable receipt; never store provider errors or file text."""
    reason = getattr(error, 'reason', 'unknown')
    if reason not in {'missing', 'changed', 'source_proof', 'range_invalid', 'readonly',
                      'limit', 'unsupported'}:
        reason = 'unknown'
    index = getattr(error, 'file_index', None)
    index = index if type(index) is int and 0 <= index < 4 else None
    root = workspace_state._store_root(store, create=True)
    path = root / ('failure-' + uuid.uuid4().hex + '.json')
    workspace_state._write_new(path, workspace_state._encode(
        {'schema': 1, 'snapshot_id': saved_id, 'reason': reason, 'file_index': index}))


class RepairPreparation:
    def __init__(self, *, context, busy, on_busy, status, ready, task_base, clock=time.monotonic):
        self.context, self.busy, self.on_busy = context, busy, on_busy
        self.status, self.ready, self.task_base, self.clock = status, ready, task_base, clock
        self.messages = queue.Queue()
        self.pending = None
        self.generation = 0
        self.closed = False

    def current(self, expected):
        return not self.closed and deepcopy(self.context()) == expected

    def start(self, records, ticket, *, scope=None, select_scope=True):
        if self.closed or self.pending or self.busy() or not self.current(ticket['context']):
            self.status('当前草稿或来源已变化，未开始修正；请重新核验。')
            return False
        payload = {'draft': ticket['saved']['payload']['draft'],
                   'records': deepcopy(records), 'scope': deepcopy(scope)}
        try:
            payload = office_workspace._payload(payload)
        except ValueError:
            self.status('请选择1至4份有完整记录的 Word/Excel 成果。')
            return False
        self.generation += 1
        generation = self.generation
        stop = threading.Event()
        self.pending = {'context': deepcopy(ticket['context']), 'ticket': deepcopy(ticket),
                        'deadline': self.clock() + 30, 'stop': stop, 'discard': False}
        self.on_busy(True)
        self.status('正在核验所选文件并准备编辑范围；原草稿尚未替换。')

        def work():
            result, error = None, None
            try:
                from .computer import ComputerSession
                from .office_scope import inspect_files
                verified = office_workspace.verify(payload, self.task_base)
                if not stop.is_set():
                    if select_scope:
                        session = ComputerSession.create(parent=self.task_base.parent / 'computer_sessions')
                        result = inspect_files(session, [r['path'] for r in payload['records']],
                                               artifact_refs=verified['refs'], stop=stop.is_set)
                    else:
                        result = verified
            except Exception as exc:
                error = exc
            self.messages.put((generation, result, error))

        try:
            threading.Thread(target=work, name='office-workspace-repair', daemon=True).start()
        except Exception:
            self.pending = None
            self.on_busy(False)
            self.status('未能开始核验，当前输入保持不变。')
            return False
        return True

    def poll(self):
        pending = self.pending
        if pending and not pending['discard']:
            if self.clock() >= pending['deadline'] or not self.current(pending['context']):
                pending['discard'] = True
                pending['stop'].set()
                self.status('修正核验超时或输入已变化；等待核验结束，旧结果不会接入。')
        while True:
            try:
                generation, catalog, error = self.messages.get_nowait()
            except queue.Empty:
                break
            pending = self.pending
            if not pending or generation != self.generation:
                continue
            self.pending = None
            self.on_busy(False)
            if pending['discard'] or not self.current(pending['context']):
                self.status('修正已结束，旧结果未接入；当前草稿保留。')
            elif error is not None or catalog is None:
                self.status(office_workspace.describe_error(error))
            else:
                self.ready(catalog, deepcopy(pending['ticket']))

    def close(self):
        self.closed = True
        if self.pending:
            self.pending['discard'] = True
            self.pending['stop'].set()
