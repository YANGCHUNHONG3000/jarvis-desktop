"""Policy 2 routing of a single provenance-verified mail export."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_service import daily_task_routing as routing
from agent_service.computer import ComputerError, digest, write_new


class Client:
    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def complete(self, system, user, **kwargs):
        self.calls.append(json.loads(user))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.fixture
def setup(tmp_path, monkeypatch):
    root = tmp_path / 'computer'
    (root / 'goals').mkdir(parents=True)
    refs = [
        {'artifact': str(tmp_path / 'mail.docx'), 'sha256': '1' * 64,
         'task_root': str(tmp_path / 'mail-task')},
        {'artifact': str(tmp_path / 'ordinary.docx'), 'sha256': '2' * 64,
         'task_root': str(tmp_path / 'ordinary-task')},
        {'artifact': str(tmp_path / 'source.xlsx'), 'sha256': '3' * 64,
         'task_root': str(tmp_path / 'sheet-task')},
    ]
    verified, created, claims = [], [], []
    for ref in refs[:2]:
        parent = Path(ref['task_root'])
        parent.mkdir()
        write_new(parent / 'result.json', {'mail_export': {}} if ref == refs[0] else {})
    monkeypatch.setattr(routing, '_verify', lambda r, b: verified.append((r, b)))
    valid_mail = {refs[0]['artifact']}

    def load_mail(ref, _base):
        if ref['artifact'] not in valid_mail:
            raise ValueError('mail_analysis_requires_export')
        return {'subject': 'PRIVATE SUBJECT', 'body': 'PRIVATE BODY'}

    monkeypatch.setattr('agent_service.mail_understanding.load_mail', load_mail)

    def claim(context):
        path = root / f'claim-{len(claims)}.json'
        write_new(path, context)
        claims.append(context)
        return path

    session = SimpleNamespace(root=root, claim_model=claim)
    original_child = routing._child

    def child(_session, req, decision):
        source = routing._selected(decision, req['source_refs'])
        plan = {'schema': 4, 'mode': 'capability', 'capability': 'mail_understanding',
                'instruction': req['instruction'], 'source_ref': source,
                'task_base': req['task_base'], 'request_id': 'a' * 32, 'candidates': []}
        plan['sha256'] = digest(plan)
        write_new(root / 'goals' / (plan['sha256'] + '.json'), plan)
        created.append(plan)
        return plan

    monkeypatch.setattr(routing, '_child', child)
    return SimpleNamespace(session=session, refs=refs, base=tmp_path, created=created,
                           verified=verified, claims=claims, valid_mail=valid_mail,
                           original_child=original_child)


def call(s, answer, refs=None, **kwargs):
    client = Client(answer)
    result = routing.propose(s.session, '整理这封邮件并提取待办',
                             s.refs if refs is None else refs, task_base=s.base,
                             client=client, **kwargs)
    return result, client


def test_single_verified_mail_routes_with_metadata_only_and_bound_schema4_child(setup):
    mail_ref = setup.refs[0]
    result, client = call(setup, {'route': 'mail_understanding', 'source_ids': ['S1'],
                                  'reason': '只处理已核验邮件'})
    assert result['state'] == 'ready'
    assert result['plan']['schema'] == 4
    assert result['plan']['capability'] == 'mail_understanding'
    assert result['plan']['source_ref'] == mail_ref
    assert len(setup.created) == len(setup.claims) == len(client.calls) == 1
    payload = client.calls[0]
    assert payload['sources'][0]['mail_export'] is True
    assert 'PRIVATE SUBJECT' not in json.dumps(payload)
    assert 'PRIVATE BODY' not in json.dumps(payload)
    assert setup.session.root.joinpath('task_routes', digest({
        'schema': routing.POLICY, 'routing_rules_sha256': digest(routing.SYSTEM),
        'instruction': '整理这封邮件并提取待办',
        'source_refs': setup.refs, 'mail_export': [True, False, False],
        'task_base': str(setup.base)}), 'request.json').exists()


def test_mail_route_can_select_one_verified_mail_among_other_refs(setup):
    result, client = call(setup, {'route': 'mail_understanding', 'source_ids': ['S1'],
                                  'reason': '请求仅针对邮件'})
    assert result['plan']['source_ref'] == setup.refs[0]
    assert [item.get('mail_export', False) for item in client.calls[0]['sources']] == [True, False, False]


def test_ordinary_word_cannot_take_mail_route_and_failure_is_cached(setup):
    result, client = call(setup, {'route': 'mail_understanding', 'source_ids': ['S2'],
                                  'reason': '普通文件'})
    assert result['state'] == 'blocked' and not setup.created
    again, second = call(setup, RuntimeError('must not retry'))
    assert again['state'] == 'blocked' and not second.calls


def test_nonmail_sources_cannot_claim_mail_route_even_when_filenames_look_like_mail(setup):
    ordinary = dict(setup.refs[1], artifact=str(setup.base / 'mail-export.docx'))
    result, _ = call(setup, {'route': 'mail_understanding', 'source_ids': ['S1'],
                             'reason': 'filename is not proof'}, refs=[ordinary])
    assert result['state'] == 'blocked' and not setup.created


def test_child_uses_existing_mail_capability_preview(setup, monkeypatch):
    from agent_service import capability_router

    seen = []
    monkeypatch.setattr(capability_router, 'prepare_mail',
                        lambda session, instruction, *, source_ref, task_base:
                        seen.append((session, instruction, source_ref, task_base)) or {'schema': 4})
    req = {'instruction': '理解这封邮件', 'task_base': str(setup.base),
           'source_refs': setup.refs, 'mail_export': [True, False, False]}
    decision = {'route': 'mail_understanding', 'source_ids': ['S1'], 'reason': '选择原邮件'}
    assert setup.original_child(setup.session, req, decision) == {'schema': 4}
    assert seen == [(setup.session, req['instruction'], setup.refs[0], setup.base)]


def test_cached_success_rechecks_provenance_without_second_model_call(setup):
    answer = {'route': 'mail_understanding', 'source_ids': ['S1'], 'reason': '核验邮件来源'}
    first, _ = call(setup, answer)
    second, client = call(setup, RuntimeError('must not be called'))
    assert second == first and not client.calls
    assert len(setup.created) == 1 and len(setup.claims) == 1


def test_revoked_source_after_model_blocks_child(setup, monkeypatch):
    calls = []

    def verify(refs, base):
        calls.append(True)
        if len(calls) > 1:
            raise ComputerError('artifact_source_reference_stale')

    monkeypatch.setattr(routing, '_verify', verify)
    result, _ = call(setup, {'route': 'mail_understanding', 'source_ids': ['S1'],
                             'reason': '核验邮件来源'})
    assert result['state'] == 'blocked' and not setup.created


def test_broken_mail_proof_never_downgrades_or_creates_paid_request(setup):
    answer = {'route': 'mail_understanding', 'source_ids': ['S1'], 'reason': '核验邮件来源'}
    first, _ = call(setup, answer)
    setup.valid_mail.clear()
    with pytest.raises(ValueError):
        call(setup, RuntimeError('must not retry model'))
    assert first['state'] == 'ready'
    assert len(setup.claims) == 1


def test_real_mail_request_corruption_blocks_before_new_model_claim(tmp_path):
    from test_mail_understanding import _mail_source
    from agent_service.computer import ComputerSession
    _, original, base, _, ref = _mail_source(tmp_path)
    session = ComputerSession.create(tmp_path / 'sessions')
    client = Client({'route': 'mail_understanding', 'source_ids': ['S1'], 'reason': '整理邮件'})
    first = routing.propose(session, '提取邮件待办并生成Word', [ref], task_base=base, client=client)
    assert first['state'] == 'ready'
    request = original / 'mail-export-request.json'
    data = json.loads(request.read_text(encoding='utf-8'))
    data['snapshot']['body'] = 'changed'
    request.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(ValueError):
        routing.propose(session, '提取邮件待办并生成Word', [ref], task_base=base, client=client)
    assert len(client.calls) == 1
    assert len([path for path in (session.root / 'task_routes').iterdir() if path.is_dir()]) == 1


def test_edited_word_is_not_misclassified_as_mail(tmp_path):
    assert routing._mail_exports([{'kind': 'office_edit', 'artifact': str(tmp_path / 'edit.docx')}], tmp_path) == [False]


def test_cancel_before_route_model_makes_no_claim(setup):
    with pytest.raises(ComputerError, match='daily_route_cancelled'):
        call(setup, {}, stop=lambda: True)
    assert not setup.claims


def test_mail_child_schema_capability_and_exact_source_are_checked(setup, monkeypatch):
    def bad_child(_session, req, decision):
        plan = {'schema': 4, 'mode': 'capability', 'capability': 'mail_understanding',
                'instruction': req['instruction'], 'source_ref': setup.refs[1],
                'task_base': req['task_base'], 'request_id': 'b' * 32, 'candidates': []}
        plan['sha256'] = digest(plan)
        write_new(setup.session.root / 'goals' / (plan['sha256'] + '.json'), plan)
        return plan

    monkeypatch.setattr(routing, '_child', bad_child)
    result, _ = call(setup, {'route': 'mail_understanding', 'source_ids': ['S1'],
                             'reason': '邮件任务'})
    assert result['state'] == 'blocked'


def test_current_policy_uses_separate_cache_namespace_from_policy_one():
    assert routing.POLICY == 3
    old = {'schema': 1, 'instruction': 'same', 'source_refs': [], 'task_base': '/tmp/base'}
    current = {'schema': routing.POLICY, 'instruction': 'same', 'source_refs': [],
               'task_base': '/tmp/base'}
    assert digest(old) != digest(current)
