"""One-operation final proof handoff; real small core proofs, no model/Office."""
from contextvars import copy_context
from copy import deepcopy
import os
from pathlib import Path
from threading import Event, Thread

import pytest

from agent_service import office_chain_payload as payloads, verification_progress, workspace_state
from agent_service import office_task_chain as chain
from agent_service.office_chain_inputs import InputVerifications
from agent_service.computer import ComputerError
from test_office_task_chain import local, route, succeed_capability


def sample(tmp_path):
    records = [{'path': str(tmp_path / 'one.xlsx'), 'sha256': 'a' * 64},
               {'path': str(tmp_path / 'two.docx'), 'sha256': 'b' * 64}]
    payload = {'draft': 'original', 'records': records, 'source': None}
    state = {'id': 'a' * 32, 'index': 0, 'total': 2, 'status': 'ready', 'payload': payload}
    proof = {'payload': deepcopy(payload), 'refs': [
        {'artifact': r['path'], 'sha256': r['sha256'], 'nested': {'values': [1]}} for r in records],
        'source_ref': None}
    return state, proof


def test_publish_and_take_copy_all_nested_values_and_consume_entire_scope(tmp_path):
    state, proof = sample(tmp_path)
    expected_state, expected_proof = deepcopy(state), deepcopy(proof)
    with payloads.capture() as handoff:
        payloads.publish(state, tmp_path, proof)
        state['payload']['draft'] = 'caller changed state'
        proof['refs'][0]['nested']['values'].append(9)
        taken = handoff.take(expected_state, str(tmp_path))
        assert taken == expected_proof and not payloads.collecting()
        taken['refs'][0]['nested']['values'].append(8)
        payloads.publish(expected_state, tmp_path, expected_proof)
        assert handoff.take(expected_state, tmp_path) is None
    assert expected_proof['refs'][0]['nested']['values'] == [1]


@pytest.mark.parametrize('change', ['index', 'total', 'status', 'draft', 'record', 'base'])
def test_mismatch_discards_handoff_without_partial_state_or_subset_matching(tmp_path, change):
    state, proof = sample(tmp_path)
    changed, base = deepcopy(state), tmp_path
    if change in {'index', 'total'}:
        changed[change] += 1
    elif change == 'status':
        changed['status'] = 'complete'
    elif change == 'draft':
        changed['payload']['draft'] = 'other draft'
    elif change == 'record':
        changed['payload']['records'] = changed['payload']['records'][:1]
    else:
        base = tmp_path / 'another'; base.mkdir()
    with payloads.capture() as handoff:
        payloads.publish(state, tmp_path, proof)
        assert handoff.take(changed, base) is None
        payloads.publish(state, tmp_path, proof)
        assert handoff.take(state, tmp_path) is None


def test_latest_publication_wins_before_the_single_consumption(tmp_path):
    state, proof = sample(tmp_path)
    next_state, next_proof = deepcopy(state), deepcopy(proof)
    next_state['payload']['draft'] = next_proof['payload']['draft'] = 'last load'
    with payloads.capture() as handoff:
        payloads.publish(state, tmp_path, proof)
        payloads.publish(next_state, tmp_path, next_proof)
        assert handoff.take(next_state, tmp_path) == next_proof


def test_no_scope_no_state_marker_and_no_cross_operation_cache(tmp_path):
    state, proof = sample(tmp_path)
    assert not payloads.collecting()
    payloads.publish(state, tmp_path, proof)
    with payloads.capture() as first:
        state['verified'] = proof
        assert first.take(state, tmp_path) is None
    with payloads.capture() as second:
        assert second.take(state, tmp_path) is None


def test_nested_scope_discards_parent_proof_on_entry_and_exit(tmp_path):
    state, proof = sample(tmp_path)
    with payloads.capture() as parent:
        payloads.publish(state, tmp_path, proof)
        with payloads.capture() as child:
            assert parent.take(state, tmp_path) is None
            payloads.publish(state, tmp_path, proof)
            assert child.take(state, tmp_path) == proof
        assert parent.take(state, tmp_path) is None
    assert not payloads.collecting()


def test_copied_context_cannot_collect_publish_or_consume_parent_proof(tmp_path):
    state, proof = sample(tmp_path)
    with payloads.capture() as handoff:
        payloads.publish(state, tmp_path, proof)
        copied = copy_context()
        assert copied.run(payloads.collecting) is False
        assert copied.run(handoff.take, state, tmp_path) is None
        copied.run(payloads.invalidate)
        copied.run(payloads.publish, {**state, 'index': 1}, tmp_path, proof)
        assert handoff.take(state, tmp_path) == proof


def test_foreign_thread_even_with_copied_context_cannot_consume_proof(tmp_path):
    state, proof = sample(tmp_path)
    with payloads.capture() as handoff:
        payloads.publish(state, tmp_path, proof)
        copied, results = copy_context(), []
        worker = Thread(target=lambda: results.append(copied.run(handoff.take, state, tmp_path)))
        worker.start(); worker.join(3)
        assert not worker.is_alive() and results == [None]
        assert handoff.take(state, tmp_path) == proof


def test_scope_exception_closes_published_proof(tmp_path):
    state, proof = sample(tmp_path)
    with pytest.raises(RuntimeError, match='operation failed'):
        with payloads.capture() as handoff:
            payloads.publish(state, tmp_path, proof)
            raise RuntimeError('operation failed')
    assert handoff.take(state, tmp_path) is None and not payloads.collecting()


@pytest.mark.parametrize('stage', ['publish', 'take'])
def test_cancellation_propagates_instead_of_falling_back_to_verification(tmp_path, stage):
    state, proof = sample(tmp_path)
    cancelled = Event()
    with pytest.raises(ComputerError, match='comparison_selection_cancelled'):
        with verification_progress.verification_scope(cancelled.is_set, lambda _message: None, 'test'):
            with payloads.capture() as handoff:
                if stage == 'take':
                    payloads.publish(state, tmp_path, proof)
                cancelled.set()
                if stage == 'publish':
                    payloads.publish(state, tmp_path, proof)
                else:
                    handoff.take(state, tmp_path)
    assert not payloads.collecting()


def simple_inputs(tmp_path, monkeypatch):
    state, proof = sample(tmp_path)
    calls = []
    def verify(payload, _base):
        calls.append(deepcopy(payload))
        return {**deepcopy(proof), 'payload': deepcopy(payload)}
    monkeypatch.setattr(workspace_state, 'verify', verify)
    return InputVerifications(), state['payload'], proof, calls


def test_completed_proof_requires_successful_recheck_and_rebinds_only_valid_draft(tmp_path, monkeypatch):
    checks, payload, proof, calls = simple_inputs(tmp_path, monkeypatch)
    checks.verify(payload, tmp_path)
    assert checks.completed_proof(payload, tmp_path) is None
    assert checks.recheck()
    changed = {**deepcopy(payload), 'draft': 'next authored instruction'}
    completed = checks.completed_proof(changed, tmp_path)
    assert completed == {**proof, 'payload': changed} and len(calls) == 2
    completed['refs'].clear()
    assert len(checks.completed_proof(changed, tmp_path)['refs']) == 2
    with pytest.raises(workspace_state.WorkspaceError):
        checks.completed_proof({**payload, 'draft': None}, tmp_path)
    checks.verify(changed, tmp_path)
    assert checks.completed_proof(changed, tmp_path) is None


@pytest.mark.parametrize('change', ['subset', 'order', 'source', 'base'])
def test_completed_proof_never_derives_another_input_group(tmp_path, monkeypatch, change):
    checks, payload, _proof, _calls = simple_inputs(tmp_path, monkeypatch)
    checks.verify(payload, tmp_path)
    assert checks.recheck()
    other, base = deepcopy(payload), tmp_path
    if change == 'subset':
        other['records'] = other['records'][:1]
    elif change == 'order':
        other['records'].reverse()
    elif change == 'source':
        other['source'] = deepcopy(other['records'][0])
    else:
        base = tmp_path / 'different'; base.mkdir()
    assert checks.completed_proof(other, base) is None


def test_later_failed_recheck_revokes_former_completed_proof(tmp_path, monkeypatch):
    checks, payload, proof, _calls = simple_inputs(tmp_path, monkeypatch)
    checks.verify(payload, tmp_path)
    assert checks.recheck()
    monkeypatch.setattr(workspace_state, 'verify', lambda *_args:
                        {**deepcopy(proof), 'payload': {**payload, 'draft': 'changed'}})
    assert checks.recheck() is False
    assert checks.completed_proof(payload, tmp_path) is None


def unselected(s):
    initial = {'draft': 'original source draft', 'records': [deepcopy(s.payload['source'])], 'source': None}
    s.ident = chain.create(s.store, ['first authored task', 'second authored task'],
                           initial, s.base, s.session.root.parent)


def test_real_core_inspection_hands_off_only_after_recheck_and_requests_remain_independent(local, monkeypatch):
    s = local; unselected(s)
    calls, original = [], workspace_state.verify
    def verify(*args):
        calls.append(deepcopy(args[0]))
        return original(*args)
    monkeypatch.setattr(workspace_state, 'verify', verify)
    for count in (2, 4):
        with payloads.capture() as handoff:
            state = chain.inspect(s.store, s.ident)
            proof = handoff.take(state, s.base)
        assert proof['payload'] == state['payload'] and proof['refs']
        assert proof['payload']['draft'] == 'first authored task'
        assert len(calls) == count


def test_advance_publishes_final_postwrite_load_not_its_prewrite_state(local, monkeypatch):
    s = local; unselected(s)
    view, refs = route(s)
    chain.bind_preview(s.store, s.ident, 0, s.session, view, refs)
    succeed_capability(s, view, count=1)
    publications, original = [], payloads.publish
    def publish(state, base, proof):
        publications.append(deepcopy(state))
        original(state, base, proof)
    monkeypatch.setattr(payloads, 'publish', publish)
    with payloads.capture() as handoff:
        state = chain.advance(s.store, s.ident)
        proof = handoff.take(state, s.base)
    assert [p['index'] for p in publications] == [0, 1]
    assert publications[-1] == state and state['status'] == 'ready'
    assert proof['payload'] == state['payload']
    assert proof['refs'][0]['artifact'] == state['payload']['records'][0]['path']
    assert (s.store / ('chain-' + s.ident) / 'step-0-complete.json').is_file()


@pytest.mark.parametrize('target', ['artifact', 'receipt'])
def test_failed_new_load_revokes_old_proof_and_same_mtime_bytes_fail_real_recheck(local, monkeypatch, target):
    s = local; unselected(s)
    artifact = Path(s.payload['source']['path'])
    path = artifact if target == 'artifact' else artifact.parent / 'receipt.json'
    original = chain._load_checked
    def altered(*args, **kwargs):
        result = original(*args, **kwargs)
        saved = path.stat()
        path.write_bytes(b'{}')
        os.utime(path, ns=(saved.st_atime_ns, saved.st_mtime_ns))
        assert path.stat().st_mtime_ns == saved.st_mtime_ns
        return result
    with payloads.capture() as handoff:
        state = chain.inspect(s.store, s.ident)
        monkeypatch.setattr(chain, '_load_checked', altered)
        with pytest.raises(chain.OfficeTaskChainError):
            chain.inspect(s.store, s.ident)
        assert handoff.take(state, s.base) is None


def test_failed_write_leaves_no_proof_outside_its_failed_operation(local, monkeypatch):
    s = local; unselected(s)
    view, refs = route(s)
    chain.bind_preview(s.store, s.ident, 0, s.session, view, refs)
    succeed_capability(s, view, count=1)
    def fail(*_args):
        raise ValueError('write failed')
    monkeypatch.setattr(chain, '_append', fail)
    with pytest.raises(chain.OfficeTaskChainError):
        with payloads.capture() as handoff:
            chain.advance(s.store, s.ident)
    assert not payloads.collecting()
    assert handoff.take({}, s.base) is None
    assert not (s.store / ('chain-' + s.ident) / 'step-0-complete.json').exists()


def test_take_rejects_state_proof_drift_between_publish_check_and_deepcopy(tmp_path, monkeypatch):
    state, proof = sample(tmp_path)
    expected = deepcopy(state)
    original_copy = payloads.deepcopy
    copies = []
    def racing_copy(value):
        copied = original_copy(value)
        if value is state:
            copies.append('state copied')
            # Deterministically simulate a concurrent input mutation in this gap.
            proof['payload']['draft'] = 'changed after publication comparison'
        return copied
    monkeypatch.setattr(payloads, 'deepcopy', racing_copy)
    with payloads.capture() as handoff:
        payloads.publish(state, tmp_path, proof)
        assert copies == ['state copied']
        assert handoff.take(expected, tmp_path) is None


def test_existing_preview_binding_early_return_falls_back_after_additional_record_read(local):
    s = local; unselected(s)
    view, refs = route(s)
    expected = chain.bind_preview(s.store, s.ident, 0, s.session, view, refs)
    with payloads.capture() as handoff:
        returned = chain.bind_preview(s.store, s.ident, 0, s.session, view, refs)
        assert returned == expected
        assert handoff.take(returned, s.base) is None
