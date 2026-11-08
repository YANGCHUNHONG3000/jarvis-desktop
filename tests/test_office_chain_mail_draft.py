"""Mixed mail-chain proofs with offline Office receipts and real local drafts."""
from copy import deepcopy
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

from agent_service import (capability_router, daily_task_routing, office_bundle,
                           office_resume, office_task_chain as chain)
from agent_service.computer import ComputerSession, read_record
from test_mail_understanding import _mail_source
from test_mail_reply_draft import reply, draft_workflow, PositionalClient
from test_office_task_chain import route, succeed_capability


@pytest.fixture
def local(tmp_path, monkeypatch):
    _, _, base, _, ref = _mail_source(tmp_path)
    session = ComputerSession.create(tmp_path / 'sessions')
    monkeypatch.setattr(office_bundle, 'BASE', session.root.parent)
    effects, execute = draft_workflow(monkeypatch)
    record = {'path': ref['artifact'], 'sha256': ref['sha256']}
    return SimpleNamespace(base=base, session=session, ref=ref, record=record,
                           store=tmp_path / 'chains', effects=effects, execute=execute)


def bind(s, *, total=2, office=True):
    instruction = '生成Word并拟写未发送回复，附加本次Word' if office else '拟写未发送回复'
    instructions = [instruction] + [f'第{i}步依据上一步办公成果生成表格' for i in range(2, total + 1)]
    s.ident = chain.create(s.store, instructions,
                          {'draft': '', 'records': [s.record], 'source': s.record},
                          s.base, s.session.root.parent)
    class RoutingClient:
        def complete(self, *args, **kwargs):
            return {'route': 'mail_understanding', 'source_ids': ['S1'], 'reason': '所选邮件'}
    routed = daily_task_routing.propose(s.session, instruction, [s.ref], task_base=s.base, client=RoutingClient())
    chain.bind_preview(s.store, s.ident, 0, s.session, routed, [s.ref])
    s.plan = routed['plan']
    s.client = PositionalClient(reply(office=office, schema=4), {'approved': True, 'reason': '离线独立审查通过'})


def run(s, *, pause=False):
    return capability_router.execute(s.session, s.plan, approval=s.plan['sha256'],
                                     task_base=s.base, client=s.client, pause_after_step=pause)


def completion(s):
    return s.store / ('chain-' + s.ident) / 'step-0-complete.json'


def test_mixed_success_retains_full_delivery_and_projects_only_office(local):
    s = local; bind(s); result = run(s)
    assert result['ok'] and s.effects == ['document', 'email_draft']
    state = chain.advance(s.store, s.ident)
    assert state['index'] == 1 and state['status'] == 'ready'
    assert state['payload']['records'] == result['artifacts'][:1]
    assert state['payload']['source'] is None
    assert chain._refs(state['payload'], s.base)[0]['artifact'].endswith('.docx')
    assert state['deliveries'] == [{'index': 0, 'task_root': result['task_root'], 'records': result['artifacts']}]
    saved = read_record(completion(s))
    assert saved['schema'] == 2 and saved['outcome'] == result
    assert saved['records'] == result['artifacts'][:1]
    assert set(saved['mail_proof']) == {'original_sha256', 'manifest_sha256', 'token'}
    before = completion(s).read_bytes()
    assert importlib.reload(chain).inspect(s.store, s.ident) == state
    assert chain.advance(s.store, s.ident) == state
    assert completion(s).read_bytes() == before and len(s.client.calls) == 2


def test_office_edit_projection_does_not_require_capability_task_metadata(local):
    s = local; bind(s); result = run(s)
    records = result['artifacts'][:1]
    # Native editing has run/proof ownership, rather than a capability task_root.
    assert chain._outputs({'ok': True, 'state': 'succeeded', 'artifacts': records}, s.base) == (records, None)


@pytest.mark.parametrize('total', [2, 3, 4])
def test_following_steps_and_restarts_keep_mail_delivery_without_eml_sources(local, total):
    s = local; bind(s, total=total); result = run(s)
    state = chain.advance(s.store, s.ident)
    delivery = deepcopy(state['deliveries'])
    for index in range(1, total):
        view, refs = route(s, index)
        assert all(Path(ref['artifact']).suffix.lower() in {'.docx', '.xlsx'} for ref in refs)
        chain.bind_preview(s.store, s.ident, index, s.session, view, refs)
        succeed_capability(s, view, count=1)
        state = chain.advance(s.store, s.ident)
        assert state['deliveries'] == delivery
        assert len(state['completed_office']) == index + 1
        assert all(Path(r['path']).suffix in {'.docx', '.xlsx'}
                   for entry in state['completed_office'] for r in entry['records'])
        assert importlib.reload(chain).inspect(s.store, s.ident) == state
    assert state['status'] == 'complete' and state['index'] == total
    assert delivery[0]['records'] == result['artifacts']
    assert all(Path(item['path']).suffix != '.eml' for item in state['payload']['records'])


def test_paused_mixed_task_resumes_only_draft_and_pins_exact_attempt(local):
    s = local; bind(s); original = run(s, pause=True)
    task = Path(original['task_root'])
    protected = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in task.rglob('*') if p.is_file()}
    state = chain.inspect(s.store, s.ident)
    assert state['status'] == 'resume_available' and state['deliveries'] == []
    assert s.effects == ['document'] and state['resume_preview']['remaining'] == 1
    attempt = uuid.uuid4().hex
    result = office_resume.resume_task(task, s.base, state['resume_preview']['token'], attempt, execute=s.execute)
    assert result['ok'] and s.effects == ['document', 'email_draft']
    state = chain.advance(s.store, s.ident, resume_attempt=attempt)
    assert state['index'] == 1 and state['payload']['records'] == original['artifacts']
    saved = read_record(completion(s))
    assert saved['schema'] == 2 and saved['outcome']['mail_resume']['attempt_id'] == attempt
    assert saved['outcome']['artifacts'] == state['deliveries'][0]['records']
    assert len(saved['outcome']['artifacts']) == 2 and len(s.client.calls) == 2
    assert all((p.read_bytes(), p.stat().st_mtime_ns) == value for p, value in protected.items())
    assert read_record(task / 'result.json')['ok'] is False
    assert chain.advance(s.store, s.ident) == state


def test_reply_only_chain_item_is_explicitly_blocked_and_original_draft_remains(local):
    s = local; bind(s, office=False); result = run(s)
    assert result['ok'] and s.effects == ['email_draft']
    state = chain.inspect(s.store, s.ident)
    assert state['status'] == 'blocked' and state['blocked_code'] == 'office_chain_mail_reply_only'
    assert state['index'] == 0 and state['deliveries'] == []
    assert chain.advance(s.store, s.ident) == state and not completion(s).exists()
    assert Path(result['artifacts'][0]['path']).is_file()


@pytest.mark.parametrize('change', ['draft', 'source', 'review', 'manifest', 'receipt', 'candidate'])
def test_completed_mixed_mail_proof_rejects_mutation(local, change):
    s = local; bind(s); result = run(s); chain.advance(s.store, s.ident)
    saved = completion(s).read_bytes()
    task = Path(result['task_root'])
    draft = Path(result['artifacts'][-1]['path'])
    if change == 'draft':
        draft.write_bytes(draft.read_bytes() + b'tamper')
    elif change == 'source':
        Path(s.ref['artifact']).write_bytes(b'tamper')
    elif change == 'review':
        path = task / 'mail-analysis-review.json'
        value = json.loads(path.read_text(encoding='utf-8'))
        value['verdict']['reason'] = '另一个自洽但未绑定的审查理由'
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    else:
        path = {'manifest': draft.parent.parent / 'manifest.json',
                'receipt': draft.parent / 'receipt.json',
                'candidate': task / 'mail-analysis-candidate.json'}[change]
        path.write_bytes(b'{}')
    with pytest.raises(chain.OfficeTaskChainError):
        chain.inspect(s.store, s.ident)
    assert completion(s).read_bytes() == saved


@pytest.mark.parametrize('change', ['drop_office', 'drop_draft', 'duplicate', 'foreign', 'extra_field', 'oversize'])
def test_projection_never_silently_drops_unverified_outputs(local, change):
    s = local; bind(s); result = run(s); changed = deepcopy(result)
    if change == 'drop_office':
        # A draft alone cannot hide the workflow's completed Office output.
        changed['artifacts'] = changed['artifacts'][-1:]
    elif change == 'drop_draft':
        changed['artifacts'] = changed['artifacts'][:1]
    elif change == 'duplicate':
        changed['artifacts'].append(deepcopy(changed['artifacts'][-1]))
    elif change == 'foreign':
        changed['artifacts'].append(s.record)
    elif change == 'extra_field':
        changed['artifacts'][-1]['unverified'] = True
    else:
        changed['artifacts'] *= 3
    with pytest.raises(chain.OfficeTaskChainError):
        chain._outputs(changed, s.base)


@pytest.mark.parametrize('change', ['proof', 'downgrade', 'records'])
def test_resealed_mixed_completion_cannot_change_projection_or_proof(local, change):
    s = local; bind(s); run(s); chain.advance(s.store, s.ident)
    value = read_record(completion(s)); value.pop('sha256')
    if change == 'proof':
        value['mail_proof']['token'] = 'a' * 64
    elif change == 'downgrade':
        value['schema'] = 1; value.pop('mail_proof')
    else:
        value['records'] = value['outcome']['artifacts']
    completion(s).write_bytes(chain.workspace._encode(chain._sealed(value)))
    with pytest.raises(chain.OfficeTaskChainError):
        chain.inspect(s.store, s.ident)
