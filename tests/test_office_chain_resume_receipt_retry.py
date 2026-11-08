"""Retry a fixed completed continuation without advancing any later step."""
from copy import deepcopy
import uuid

import pytest

from agent_service import office_task_chain as chain
from agent_service.computer import read_record
from test_office_task_chain import route
from test_office_task_chain_mail import (local as mail_local, paused as mail_paused,
                                         resume as mail_resume)
from test_office_task_chain_bundle_resume import (local as bundle_local, bundle_setup,
    paused as bundle_paused, resume as bundle_resume, execute as bundle_execute)


@pytest.fixture(params=['mail', 'bundle'])
def recovered(request):
    kind = request.param
    s = request.getfixturevalue(kind + '_local')
    (mail_paused if kind == 'mail' else bundle_paused)(s)
    attempt, _ = (mail_resume if kind == 'mail' else bundle_resume)(s)
    return s, attempt, kind + '_resume'


def complete_path(s, index=0):
    return s.store / ('chain-' + s.ident) / f'step-{index}-complete.json'


def snapshot(root):
    return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in root.rglob('*') if p.is_file()}


def test_append_then_final_load_access_failure_retries_only_the_exact_receipt(recovered, monkeypatch):
    s, attempt, proof = recovered
    original_load = chain._load; failed = []
    protected = snapshot(s.base)
    def load(*args, **kwargs):
        if complete_path(s).exists() and not failed:
            failed.append(True)
            raise PermissionError('private-original-path')
        return original_load(*args, **kwargs)
    monkeypatch.setattr(chain, '_load', load)
    with pytest.raises(chain.OfficeTaskChainError):
        chain.advance(s.store, s.ident, resume_attempt=attempt, resume_index=0)
    saved = snapshot(complete_path(s).parent)
    assert read_record(complete_path(s))['outcome'][proof]['attempt_id'] == attempt
    monkeypatch.setattr(chain, '_append', lambda *args: pytest.fail('receipt retry appended another step'))
    state = chain.advance(s.store, s.ident, resume_attempt=attempt, resume_index=0)
    assert state['index'] == 1 and state['status'] == 'ready'
    assert snapshot(complete_path(s).parent) == saved and snapshot(s.base) == protected
    # The former API deliberately retains its unconsumed-attempt rejection.
    with pytest.raises(chain.OfficeTaskChainError, match='resume_unverified'):
        chain.advance(s.store, s.ident, resume_attempt=attempt)


def test_indexed_attempt_cannot_claim_another_incomplete_step(recovered):
    s, attempt, _proof = recovered
    with pytest.raises(chain.OfficeTaskChainError, match='resume_index_invalid'):
        chain.advance(s.store, s.ident, resume_attempt=attempt, resume_index=1)
    assert not complete_path(s).exists() and not complete_path(s, 1).exists()


def test_completed_receipt_rejects_wrong_attempt_or_step(recovered):
    s, attempt, _proof = recovered
    chain.advance(s.store, s.ident, resume_attempt=attempt, resume_index=0)
    before = snapshot(complete_path(s).parent)
    for wrong_attempt, wrong_index in [(uuid.uuid4().hex, 0), (attempt, 1)]:
        with pytest.raises(chain.OfficeTaskChainError):
            chain.advance(s.store, s.ident, resume_attempt=wrong_attempt, resume_index=wrong_index)
    assert snapshot(complete_path(s).parent) == before


@pytest.mark.parametrize('change', ['attempt_proof', 'both_proofs', 'during_load_bytes'])
def test_resealed_or_midverification_receipt_changes_are_rejected(recovered, monkeypatch, change):
    s, attempt, proof = recovered
    chain.advance(s.store, s.ident, resume_attempt=attempt, resume_index=0)
    path = complete_path(s)
    if change == 'during_load_bytes':
        original_load = chain._load
        def load(*args, **kwargs):
            result = original_load(*args, **kwargs)
            path.write_bytes(path.read_bytes() + b' ')
            return result
        monkeypatch.setattr(chain, '_load', load)
    else:
        saved = read_record(path); saved.pop('sha256')
        if change == 'attempt_proof':
            saved['outcome'][proof]['attempt_id'] = uuid.uuid4().hex
        else:
            other = 'bundle_resume' if proof == 'mail_resume' else 'mail_resume'
            saved['outcome'][other] = deepcopy(saved['outcome'][proof])
        path.write_bytes(chain.workspace._encode(chain._sealed(saved)))
    with pytest.raises(chain.OfficeTaskChainError):
        chain.advance(s.store, s.ident, resume_attempt=attempt, resume_index=0)
    assert not complete_path(s, 1).exists()


def test_receipt_retry_verifies_later_success_without_appending_its_completion(recovered, monkeypatch):
    s, attempt, _proof = recovered
    chain.advance(s.store, s.ident, resume_attempt=attempt, resume_index=0)
    view, refs = route(s, 1)
    chain.bind_preview(s.store, s.ident, 1, s.session, view, refs)
    result, _ = bundle_execute(s, plan=view['plan'], pause=False)
    assert result['ok']
    before = snapshot(complete_path(s).parent)
    monkeypatch.setattr(chain, '_append', lambda *args: pytest.fail('later success was appended'))
    state = chain.advance(s.store, s.ident, resume_attempt=attempt, resume_index=0)
    assert state['index'] == 1 and state['status'] == 'waiting_result'
    assert not complete_path(s, 1).exists() and snapshot(complete_path(s).parent) == before


@pytest.mark.parametrize(('attempt', 'index'), [(None, 0), ('a' * 32, True), ('a' * 32, -1),
    ('a' * 32, 4), ('a' * 32, '0'), ('a' * 32, 0.0)])
def test_resume_index_requires_a_fixed_attempt_and_bounded_integer(tmp_path, attempt, index):
    with pytest.raises(chain.OfficeTaskChainError, match='resume_index_invalid'):
        chain.advance(tmp_path / 'chains', 'a' * 32, resume_attempt=attempt, resume_index=index)
