"""Bounded Office chain verification wait, real progress and late UI delivery."""
from copy import deepcopy
import threading

import pytest

from agent_service import computer, verification_progress
from agent_service import office_task_chain_panel as module
from test_office_task_chain_panel import harness, Thread, mutate

REAL_THREAD = threading.Thread


def setup(tmp_path, monkeypatch):
    panel, context, log, state = harness(tmp_path, monkeypatch)
    monkeypatch.setattr(verification_progress.time, 'monotonic', lambda: log.now)
    record = tmp_path / 'record.json'
    record.write_text('{}', encoding='utf-8')
    return panel, context, log, state, record


def test_actual_record_progress_extends_wait_and_late_finished_still_adopts(tmp_path, monkeypatch):
    panel, context, log, state, record = setup(tmp_path, monkeypatch)
    def operation():
        for at in (110, 139, 165, 195):
            log.now = at
            assert computer.read_record(record) == {}
        return deepcopy(state)
    assert panel._begin(operation)
    Thread.all[-1].run()
    # Completion happened at 195 within its live silence window. The UI may
    # receive it much later, even after the absolute computation limit.
    log.now = 600
    panel.poll()
    assert len(log.applied) == 1 and panel.pending is None
    assert verification_progress._CURRENT.get() is None


def test_stage_text_without_record_or_file_progress_cannot_extend_wait(tmp_path, monkeypatch):
    panel, _context, log, state, _record = setup(tmp_path, monkeypatch)
    reached = []
    def operation():
        log.now = 130
        verification_progress.checkpoint(stage='first phase')
        log.now = 141
        verification_progress.checkpoint(stage='second phase')
        reached.append('after timeout')
        return deepcopy(state)
    assert panel._begin(operation)
    Thread.all[-1].run()
    panel.poll()
    assert not reached and not log.applied and panel.pending is None
    assert '40秒没有新进度' in log.statuses[-1]
    assert '核验工作已结束' in log.statuses[-1]


def test_real_progress_cannot_extend_absolute_limit(tmp_path, monkeypatch):
    panel, _context, log, state, record = setup(tmp_path, monkeypatch)
    reached = []
    def operation():
        for at in range(110, 401, 10):
            log.now = at
            computer.read_record(record)
        reached.append('past total limit')
        return deepcopy(state)
    assert panel._begin(operation)
    Thread.all[-1].run()
    panel.poll()
    assert not reached and not log.applied and panel.pending is None
    assert '300秒总上限' in log.statuses[-1]


@pytest.mark.parametrize('kind', ['progress', 'finished'])
def test_worker_message_after_silence_deadline_cannot_revive_wait(tmp_path, monkeypatch, kind):
    panel, _context, log, state, _record = setup(tmp_path, monkeypatch)
    assert panel._begin(lambda: deepcopy(state))
    result = (1, 'late progress') if kind == 'progress' else (state, {'payload': state['payload']})
    panel.messages.put((panel.generation, kind, 141, result, None))
    log.now = 150
    panel.poll()
    assert not log.applied
    if panel.pending:
        assert panel.pending['discard'] and panel.pending['cancelled'].is_set()
        Thread.all[-1].run()
        panel.poll()
    assert '40秒没有新进度' in log.statuses[-1]


def test_duplicate_progress_and_wrong_generation_do_not_extend_wait(tmp_path, monkeypatch):
    panel, _context, log, state, _record = setup(tmp_path, monkeypatch)
    assert panel._begin(lambda: deepcopy(state))
    generation = panel.generation
    panel.messages.put((generation, 'progress', 110, (1, 'actual record'), None))
    panel.messages.put((generation, 'progress', 139, (1, 'duplicate counter'), None))
    panel.messages.put((generation-1, 'progress', 145, (20, 'old generation'), None))
    log.now = 151
    panel.poll()
    assert panel.pending['deadline'] == 150
    assert panel.pending['discard'] and panel.pending['cancelled'].is_set()
    assert 'duplicate counter' not in log.statuses and 'old generation' not in log.statuses


def test_timely_queued_progress_is_consumed_before_ui_silence_check(tmp_path, monkeypatch):
    panel, _context, log, state, _record = setup(tmp_path, monkeypatch)
    assert panel._begin(lambda: deepcopy(state))
    panel.messages.put((panel.generation, 'progress', 139, (1, 'actual record'), None))
    log.now = 150
    panel.poll()
    assert not panel.pending['discard'] and panel.pending['deadline'] == 179
    log.now = 180
    panel.poll()
    assert panel.pending['discard'] and panel.pending['cancelled'].is_set()


def test_many_progress_events_validate_every_time_but_display_once_per_poll(tmp_path, monkeypatch):
    panel, _context, log, state, _record = setup(tmp_path, monkeypatch)
    assert panel._begin(lambda: deepcopy(state))
    before = len(log.statuses)
    for count, at in enumerate((110, 130, 160), 1):
        panel.messages.put((panel.generation, 'progress', at, (count, f'progress {count}'), None))
    log.now = 165
    panel.poll()
    assert not panel.pending['discard'] and panel.pending['deadline'] == 200
    assert panel.pending['progress'] == 3 and panel.pending['progress_at'] == 160
    assert log.statuses[before:] == ['progress 3']


def test_finished_in_same_drain_keeps_final_status_over_progress(tmp_path, monkeypatch):
    panel, _context, log, state, _record = setup(tmp_path, monkeypatch)
    assert panel._begin(lambda: deepcopy(state))
    before = len(log.statuses)
    panel.messages.put((panel.generation, 'progress', 130, (1, 'pending progress'), None))
    panel.messages.put((panel.generation, 'finished', 160,
                        (state, {'payload': deepcopy(state['payload'])}), None))
    log.now = 190
    panel.poll()
    assert len(log.applied) == 1 and panel.pending is None
    assert len(log.statuses[before:]) == 1
    assert log.statuses[-1].startswith('第1/2步已接入')


@pytest.mark.parametrize('reason', ['timeout', 'cancel'])
def test_late_progress_cannot_overwrite_timeout_or_stop_reason(tmp_path, monkeypatch, reason):
    panel, _context, log, state, _record = setup(tmp_path, monkeypatch)
    assert panel._begin(lambda: deepcopy(state))
    panel.messages.put((panel.generation, 'progress', 130, (1, 'valid earlier progress'), None))
    panel.messages.put((panel.generation, 'progress', 171, (2, 'late progress'), None))
    panel.messages.put((panel.generation, 'finished', 172,
                        (state, {'payload': deepcopy(state['payload'])}), None))
    if reason == 'cancel':
        panel.cancel()
    log.now = 180
    panel.poll()
    assert not log.applied and panel.pending is None
    assert ('40秒没有新进度' if reason == 'timeout' else '已停止清单接入') in log.statuses[-1]
    assert not any(text in log.statuses for text in ('valid earlier progress', 'late progress'))


@pytest.mark.parametrize('interruption', ['cancel', 'close', 'context'])
def test_cancel_input_change_and_close_stop_before_next_checkpoint(tmp_path, monkeypatch, interruption):
    panel, context, log, state, record = setup(tmp_path, monkeypatch)
    reached = []
    def operation():
        computer.read_record(record)
        reached.append('operation')
        return deepcopy(state)
    assert panel._begin(operation)
    if interruption == 'context':
        mutate(context, 'goal_revert')
        panel.poll()
    else:
        getattr(panel, interruption)()
    assert panel.pending['cancelled'].is_set()
    Thread.all[-1].run()
    panel.poll()
    assert not reached and not log.applied and panel.pending is None
    assert log.busy == [True, False]


def test_cancellation_inside_reader_never_moves_to_next_proof(tmp_path, monkeypatch):
    panel, _context, log, state, record = setup(tmp_path, monkeypatch)
    reached = []
    original = computer.Path.read_text
    def read_then_cancel(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path == record:
            panel.pending['cancelled'].set()
        return result
    monkeypatch.setattr(computer.Path, 'read_text', read_then_cancel)
    def operation():
        computer.read_record(record)
        reached.append('after reader')
        return deepcopy(state)
    assert panel._begin(operation)
    Thread.all[-1].run()
    panel.poll()
    assert not reached and not log.applied and panel.pending is None
    assert original(record, encoding='utf-8') == '{}'


def test_worker_only_queues_and_never_reads_context_or_calls_ui(tmp_path, monkeypatch):
    panel, _context, log, state, record = setup(tmp_path, monkeypatch)
    original_context, original_status = panel.context, panel.status
    original_busy = panel.on_busy
    def main_only(function):
        def wrapped(*args, **kwargs):
            assert threading.current_thread() is threading.main_thread()
            return function(*args, **kwargs)
        return wrapped
    panel.context = main_only(original_context)
    panel.status = main_only(original_status)
    panel.on_busy = main_only(original_busy)
    threads = []
    def thread_factory(**kwargs):
        worker = REAL_THREAD(**kwargs); threads.append(worker); return worker
    monkeypatch.setattr(module.threading, 'Thread', thread_factory)
    def operation():
        computer.read_record(record)
        return deepcopy(state)
    assert panel._begin(operation)
    threads[-1].join(timeout=2)
    assert not threads[-1].is_alive() and not log.applied
    panel.poll()
    assert len(log.applied) == 1 and panel.pending is None


def test_legacy_completion_envelope_still_obeys_context_guard(tmp_path, monkeypatch):
    panel, context, log, state, _record = setup(tmp_path, monkeypatch)
    assert panel._begin(lambda: deepcopy(state))
    panel.messages.put((panel.generation, (state, {'payload': state['payload']}), None))
    mutate(context, 'main_revert')
    panel.poll()
    assert not log.applied and panel.pending is None
    assert '输入已变化' in log.statuses[-1]


@pytest.mark.parametrize('failure', [False, True])
def test_local_observer_restores_nested_and_outer_contexts(tmp_path, monkeypatch, failure):
    panel, _context, log, state, record = setup(tmp_path, monkeypatch)
    seen = []
    def operation():
        observer = verification_progress._CURRENT.get()
        assert isinstance(observer, module._VerificationObserver)
        try:
            with verification_progress.verification_scope(lambda: False, lambda _: None, 'nested'):
                assert verification_progress._CURRENT.get() is not observer
                verification_progress.checkpoint(records=1)
                raise ValueError('inner failure')
        except ValueError:
            pass
        assert verification_progress._CURRENT.get() is observer
        log.now = 130
        computer.read_record(record)
        seen.append(observer.records)
        if failure:
            raise ValueError('outer operation failed')
        return deepcopy(state)
    with verification_progress.verification_scope(lambda: False, lambda _: None, 'caller'):
        caller = verification_progress._CURRENT.get()
        assert panel._begin(operation)
        Thread.all[-1].run()
        assert verification_progress._CURRENT.get() is caller
        panel.poll()
        assert verification_progress._CURRENT.get() is caller
    assert verification_progress._CURRENT.get() is None
    assert seen == [1] and len(log.applied) == (0 if failure else 1)
