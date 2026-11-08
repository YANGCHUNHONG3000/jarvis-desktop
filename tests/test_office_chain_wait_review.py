"""Independent review of final proof stages and queued-result cancellation."""
from copy import deepcopy

import pytest

from agent_service import computer, office_workspace, verification_progress
from agent_service import office_task_chain_panel as module
from test_office_task_chain_panel import harness, Thread


@pytest.mark.parametrize('editing', [False, True])
def test_final_workspace_proof_uses_same_progress_and_cancel_scope(tmp_path, monkeypatch, editing):
    panel, _context, log, state = harness(tmp_path, monkeypatch)
    monkeypatch.setattr(verification_progress.time, 'monotonic', lambda: log.now)
    proof = tmp_path / 'proof.json'
    proof.write_text('{}', encoding='utf-8')
    calls = []

    def workspace_verify(payload, base):
        calls.append('workspace')
        log.now = 130
        assert computer.read_record(proof) == {}
        if not editing:
            for at in (160, 190):
                log.now = at
                assert computer.read_record(proof) == {}
        return {'payload': deepcopy(payload)}

    def edit_verify(payload, base):
        calls.append('edit')
        for at in (160, 190):
            log.now = at
            assert computer.read_record(proof) == {}
        return {'reviewed': True}

    monkeypatch.setattr(module.workspace_state, 'verify', workspace_verify)
    monkeypatch.setattr(office_workspace, 'verify', edit_verify)
    if editing:
        state.update(status='edit_preview', edit_payload={'proof': 'edit'})
    adopted = []
    panel._adopt = lambda state, verified, **kwargs: adopted.append((state, verified))
    assert panel._begin(lambda: deepcopy(state))
    Thread.all[-1].run()
    log.now = 600
    panel.poll()
    assert calls == (['workspace', 'edit'] if editing else ['workspace'])
    assert len(adopted) == 1 and panel.pending is None
    assert verification_progress._CURRENT.get() is None
    if editing:
        assert adopted[0][1]['office_verified'] == {'reviewed': True}


def test_stop_after_success_is_queued_discards_all_progress_and_completion(tmp_path, monkeypatch):
    panel, _context, log, state = harness(tmp_path, monkeypatch)
    monkeypatch.setattr(verification_progress.time, 'monotonic', lambda: log.now)
    proof = tmp_path / 'proof.json'
    proof.write_text('{}', encoding='utf-8')

    def operation():
        for at in (130, 160, 190):
            log.now = at
            assert computer.read_record(proof) == {}
        return deepcopy(state)

    assert panel._begin(operation)
    Thread.all[-1].run()
    assert not panel.messages.empty()
    panel.cancel()
    panel.poll()
    assert not log.applied and not log.prepared
    assert panel.pending is None and log.busy == [True, False]
    assert '已停止清单接入' in log.statuses[-1]
    assert proof.read_text(encoding='utf-8') == '{}'


def test_burst_reads_keep_actual_activity_time_without_stage_retiming(tmp_path, monkeypatch):
    panel, _context, log, state = harness(tmp_path, monkeypatch)
    monkeypatch.setattr(verification_progress.time, 'monotonic', lambda: log.now)
    proof = tmp_path / 'proof.json'
    proof.write_text('{}', encoding='utf-8')
    reached = []

    def operation():
        for at in (130, 130.1):
            log.now = at
            computer.read_record(proof)
        log.now = 169.9
        verification_progress.checkpoint(stage='stage text before silence deadline')
        reached.append('before deadline')
        log.now = 170.2
        verification_progress.checkpoint(stage='stage text after silence deadline')
        reached.append('after deadline')
        return deepcopy(state)

    assert panel._begin(operation)
    Thread.all[-1].run()
    panel.poll()
    assert reached == ['before deadline']
    assert not log.applied and panel.pending is None
    assert '40秒没有新进度' in log.statuses[-1]


def test_observer_delegates_original_cancellation_before_any_counter_increment():
    stopped = [False]
    reported = []
    original = verification_progress._State(lambda: stopped[0], lambda _: None, 'proof')
    observer = module._VerificationObserver(original, reported.append)
    observer.check(records=1, files=1)
    assert (original.records, original.files) == (1, 1)
    assert len(reported) == 1
    stopped[0] = True
    with pytest.raises(verification_progress._Cancelled):
        observer.check(records=9, files=9)
    assert original.cancelled and (original.records, original.files) == (1, 1)
    assert len(reported) == 1 and 'check' not in original.__dict__
