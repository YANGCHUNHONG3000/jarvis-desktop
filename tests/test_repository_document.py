"""Offline repository evidence chain and real OOXML/standard Office handoff."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path

import pytest

from agent_service.computer import ComputerError, digest, read_record, write_new
from agent_service.computer_agent import choices_for
from agent_service.computer_agent_tools import _load, _save
from agent_service import repository_document as repo
from agent_service.artifact_source import prepare_artifact_source, snapshot_artifact_source
from test_artifact_source import fake as native_fake


def put(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


@pytest.fixture
def reading(tmp_path_factory):
    # Canonical computer/run names already consume 119 characters on Windows.
    tmp_path = tmp_path_factory.mktemp('r')
    base = tmp_path / 'computers'
    session = base / ('computer-' + 'a' * 32)
    goals = session / 'goals'
    goals.mkdir(parents=True)
    write_new(session / 'session.json', {'schema': 1, 'created': 10.0})
    config = {'mode': 'github_repo', 'start_url': 'https://github.com/example/project',
              'query': 'project user:example', 'required_text': ['actual project'],
              'headless': False, 'human_login': True}
    goal = {'kind': 'interactive', 'browser': config, 'edits': []}
    plan = {'schema': 3, 'mode': 'agent', 'goal': goal, 'instruction': 'Read approved repository',
            'prepared_at': 10.0, 'max_actions': 12, 'max_seconds': 360}
    plan['sha256'] = digest(plan)
    write_new(goals / (plan['sha256'] + '.json'), plan)
    run = goals / (plan['sha256'] + '.run')
    run.mkdir()
    write_new(run / 'claim.json', {'approval': plan['sha256'], 'mode': 'agent', 'at': 11.0})
    _save(run / 'environment-goal.json', goal)
    body = 'actual project. ' + 'Verified source text 中文句子。 ' * 250
    browser = {'url': config['start_url'], 'text': body, 'title': 'GitHub repository',
        'locked': False, 'human_required': False, 'checks': {'goal_met': True},
        'discovery': {'mode': 'github_repo', 'query_verified': True, 'repository_verified': True,
            'authenticated': True, 'repository_identity_matches': True, 'main_present': True,
            'repository_search_status': 200, 'repository_status': 200,
            'ambiguous_controls': False, 'controls_overflow': False}}
    browser['state_id'] = digest(browser)
    browser['actions'] = []
    attempts, hashes = [], []
    for n, code in enumerate(('browser_github_search_submitted', 'browser_github_repository_opened'), 1):
        before = {'browser': None, 'checks': {'sources_ready': False, 'goal_met': False}}
        write_new(run / f'observation-{n:02}.json', before)
        action = {'kind': 'browser_click', 'rule_id': 'approved-' + str(n), 'control_id': str(n),
                  'state_id': 'b' * 64, 'label': 'approved action'}
        state_hash, choices = choices_for(run, before, [action])
        step = run / f'step-{n:02}'
        step.mkdir()
        write_new(step / 'choices.json', {'state_sha256': state_hash, 'choices': choices})
        write_new(step / 'decision.json', {'method': 'single_legal_action', 'choice': choices[0],
                                          'state_sha256': state_hash})
        write_new(step / 'intent.json', choices[0])
        intent = {'action': action}
        _save(step / 'environment-intent.json', intent)
        result = {'ok': True, 'state': 'verified', 'code': code, 'observation': browser,
                  'verification': {'independent_read': True}}
        record = {'action': action, 'intent_sha256': digest(intent), 'result': result}
        _save(step / 'environment-result.json', record)
        write_new(step / 'verification.json', result)
        hashes.append(digest(record))
        attempts.append({'step': n, 'action': action, 'result': result})
    final = {'browser': browser, 'checks': {'sources_ready': True, 'goal_met': True},
             'evidence_sha256': digest(hashes), 'artifacts': [], 'failures': [], 'source_reads': 0}
    write_new(run / 'observation-03.json', final)
    outcome = {'ok': True, 'state': 'verified', 'code': 'objective_verified', 'engine': 'observed_agent',
        'goal_kind': 'interactive', 'run': str(run), 'failures': [], 'artifacts': [],
        'attempts': attempts, 'completed': attempts, 'actions': 2,
        'repository_read': {'url': config['start_url'], 'text': body, 'authenticated': True}}
    write_new(run / 'outcome.json', outcome)
    return {'run': run, 'base': base, 'tasks': tmp_path / 'tasks', 'body': body,
            'final': final, 'outcome': outcome}


def prepare(reading):
    return repo.prepare_repository_document(reading['run'], computer_base=reading['base'])


def execute(reading, plan, **kwargs):
    return repo.execute_repository_document(plan, approval=plan['sha256'],
        computer_base=reading['base'], task_base=reading['tasks'], office_execute=native_fake, **kwargs)


def test_plan_local_and_body_free_complete_natural_structure(reading, monkeypatch):
    monkeypatch.setattr('agent_service.model.CloudClient', lambda *a, **k: pytest.fail('no model'))
    plan = prepare(reading)
    assert plan == prepare(reading)
    assert plan['mode'] == 'repository_document' and reading['body'] not in json.dumps(plan)
    source = repo.verify_repository_source(reading['run'], computer_base=reading['base'])
    spec = repo._spec(source, plan['title'])
    parts = repo._parts(reading['body'])
    assert ''.join(p['text'] for p in parts) == reading['body']
    assert parts[0]['start'] == 0 and parts[-1]['end'] == len(reading['body'])
    assert all(a['end'] == b['start'] for a, b in zip(parts, parts[1:]))
    assert len(spec['paragraphs']) <= 40 and max(map(len, spec['paragraphs'])) <= 1300
    assert '仓库概况' in spec['paragraphs'] and '来源与阅读范围' in spec['paragraphs']
    assert not any('SHA' in p or str(reading['run']) in p for p in spec['paragraphs'])
    assert '零 API' in repo.describe_repository_document(plan)
    listed = repo.list_repository_readings(computer_base=reading['base'])
    assert listed[0]['body_chars'] == len(reading['body'])
    assert 'text' not in listed[0] and listed[0]['authenticated'] is True


def test_office_standard_artifact_source_and_success_reuse(reading):
    plan = prepare(reading)
    result = execute(reading, plan)
    assert result['ok'] and result['state'] == 'verified'
    artifact = result['artifact']
    assert prepare_artifact_source(artifact['path'], artifact['sha256'], reading['tasks']) == result['ref']
    task = Path(artifact['task_root'])
    assert read_record(task / 'result.json')['state'] == 'succeeded'
    assert _load(task / 'repository-source.json')[0]['text'] == reading['body']
    assert execute(reading, plan) == result
    assert len(list(reading['tasks'].glob('task-*'))) == 1
    next_task = reading['tasks'] / ('task-' + 'b' * 32)
    next_task.mkdir()
    source = snapshot_artifact_source(result['ref'], next_task, reading['tasks'])
    assert source['kind'] == 'document'


@pytest.mark.parametrize('change', ['auth', 'search', 'repo', 'url', 'text', 'final', 'claim',
                                  'plan', 'environment', 'receipt', 'decision', 'missing', 'fake_success'])
def test_rejects_broken_evidence_not_bare_success(reading, change):
    run = reading['run']
    if change in {'auth', 'search', 'repo', 'url', 'text', 'final'}:
        final = deepcopy(reading['final'])
        browser = final['browser']
        if change == 'auth': browser['discovery']['authenticated'] = False
        if change == 'search': browser['discovery']['repository_search_status'] = 503
        if change == 'repo': browser['discovery']['repository_status'] = True
        if change == 'url': browser['url'] = 'https://github.com/other/repository'
        if change == 'text': browser['text'] += 'tampered'
        if change == 'final': final['evidence_sha256'] = '0' * 64
        put(run / 'observation-03.json', final)
    elif change == 'claim': put(run / 'claim.json', {'approval': '0' * 64, 'mode': 'agent'})
    elif change == 'plan':
        plan = read_record(run.with_suffix('.json'))
        plan['instruction'] = 'tampered'
        put(run.with_suffix('.json'), plan)
    elif change == 'environment':
        put(run / 'environment-goal.json', {})
    elif change == 'receipt': put(run / 'step-02' / 'verification.json', {'ok': True})
    elif change == 'decision': put(run / 'step-02' / 'decision.json', {'method': 'model_choice'})
    elif change == 'missing': (run / 'step-02' / 'environment-result.json').unlink()
    elif change == 'fake_success':
        out = deepcopy(reading['outcome'])
        out['attempts'], out['completed'], out['actions'] = [], [], 0
        put(run / 'outcome.json', out)
    with pytest.raises(ComputerError, match='repository_source_invalid'):
        prepare(reading)
    assert not reading['tasks'].exists()
    assert repo.list_repository_readings(computer_base=reading['base']) == []


def test_unknown_never_replays_and_no_generic_resume_claim(reading):
    plan = prepare(reading)
    calls = []
    def failing(spec, root, stop):
        calls.append(root)
        raise RuntimeError('private contents must not appear')
    result = repo.execute_repository_document(plan, approval=plan['sha256'],
        computer_base=reading['base'], task_base=reading['tasks'], office_execute=failing)
    assert not result['ok'] and result['state'] == 'unknown'
    assert 'private contents' not in json.dumps(result)
    assert execute(reading, plan) == result and len(calls) == 1
    changed = repo.prepare_repository_document(reading['run'], computer_base=reading['base'], title='Other')
    with pytest.raises(ComputerError, match='already_claimed'):
        execute(reading, changed)


def test_interrupted_claim_is_unknown_without_rerun(reading):
    plan = prepare(reading)
    write_new(reading['run'] / repo._CLAIM, {'approval': plan['sha256'],
        'task_base': str(reading['tasks']), 'task_root': str(reading['tasks'] / ('task-' + 'd' * 32))})
    result = execute(reading, plan)
    assert result['state'] == 'unknown' and not reading['tasks'].exists()


def test_approval_source_and_output_changes_stop_before_replay(reading):
    plan = prepare(reading)
    with pytest.raises(ComputerError, match='approval_invalid'):
        repo.execute_repository_document(plan, approval='0' * 64)
    assert not (reading['run'] / repo._CLAIM).exists()
    result = execute(reading, plan)
    Path(result['artifact']['path']).write_bytes(b'tampered')
    with pytest.raises(ComputerError, match='result_changed'):
        execute(reading, plan)
    assert len(list(reading['tasks'].glob('task-*'))) == 1


def test_cancel_before_claim_and_whitespace_transparency(reading):
    plan = prepare(reading)
    result = execute(reading, plan, stop=lambda: True)
    assert result['state'] == 'cancelled' and not (reading['run'] / repo._CLAIM).exists()
    text = 'first\r\nsecond\t第三段'
    assert ''.join(p['text'] for p in repo._parts(text)) == 'first  second 第三段'
    with pytest.raises(ComputerError, match='source_invalid'):
        repo.prepare_repository_document('example/project', computer_base=reading['base'])


@pytest.mark.parametrize('change', ['artifact_list', 'empty_list', 'duplicate_list', 'ok_type',
                                   'false_ok', 'state', 'code', 'kind'])
def test_saved_result_rejects_unverified_gui_artifacts_and_success_fields(reading, change):
    plan = prepare(reading)
    result = execute(reading, plan)
    assert result['ok'] is True
    value = deepcopy(result)
    if change == 'artifact_list':
        value['artifacts'][0]['path'] = str(reading['tasks'] / 'other.docx')
        # Keep the individually verified artifact unchanged.
        value['artifact'] = deepcopy(result['artifact'])
    elif change == 'empty_list': value['artifacts'] = []
    elif change == 'duplicate_list': value['artifacts'].append(deepcopy(value['artifact']))
    elif change == 'ok_type': value['ok'] = 1
    elif change == 'false_ok': value['ok'] = False
    elif change == 'state': value['state'] = 'unknown'
    elif change == 'code': value['code'] = 'unverified_success'
    elif change == 'kind':
        value['artifact']['kind'] = 'spreadsheet'
        value['artifacts'] = [value['artifact']]
    put(reading['run'] / repo._RESULT, {'data': value, 'sha256': digest(value)})
    with pytest.raises(ComputerError, match='repository_document_result_changed'):
        execute(reading, plan)
    assert len(list(reading['tasks'].glob('task-*'))) == 1


def test_max_source_size_fits_office_without_truncating(reading):
    source = repo.verify_repository_source(reading['run'], computer_base=reading['base'])
    source['text'] = '甲' * 24000
    source['metadata']['body_chars'] = 24000
    spec = repo._spec(source, 'Maximum')
    assert len(spec['paragraphs']) <= 40
    assert sum(p.count('甲') for p in spec['paragraphs']) == 24000
    assert all(len(p) <= 3000 for p in spec['paragraphs'])


def test_public_reading_preserves_actual_false_authentication(reading):
    run = reading['run']
    old_plan = read_record(run.with_suffix('.json'))
    old_plan['goal']['browser']['human_login'] = False
    old_plan['sha256'] = digest({k: v for k, v in old_plan.items() if k != 'sha256'})
    new_run = run.parent / (old_plan['sha256'] + '.run')
    run.rename(new_run)
    run.with_suffix('.json').unlink()
    write_new(new_run.with_suffix('.json'), old_plan)
    reading['run'] = new_run
    put(new_run / 'claim.json', {'approval': old_plan['sha256'], 'mode': 'agent', 'at': 11.0})
    put(new_run / 'environment-goal.json', {'data': old_plan['goal'], 'sha256': digest(old_plan['goal'])})
    outcome = read_record(new_run / 'outcome.json')
    outcome['run'] = str(new_run)
    outcome['repository_read']['authenticated'] = False
    hashes = []
    final_browser = None
    for step in sorted(new_run.glob('step-*')):
        n = int(step.name[-2:])
        record = _load(step / 'environment-result.json')[0]
        browser = record['result']['observation']
        browser['discovery']['authenticated'] = False
        browser['state_id'] = digest({k: v for k, v in browser.items() if k not in {'state_id', 'actions'}})
        put(step / 'environment-result.json', {'data': record, 'sha256': digest(record)})
        put(step / 'verification.json', record['result'])
        outcome['attempts'][n - 1]['result'] = deepcopy(record['result'])
        outcome['completed'][n - 1]['result'] = deepcopy(record['result'])
        before = read_record(new_run / f'observation-{n:02}.json')
        state_hash, choices = choices_for(new_run, before, [record['action']])
        put(step / 'choices.json', {'state_sha256': state_hash, 'choices': choices})
        put(step / 'intent.json', choices[0])
        put(step / 'decision.json', {'method': 'single_legal_action', 'choice': choices[0], 'state_sha256': state_hash})
        hashes.append(digest(record))
        final_browser = browser
    final = read_record(new_run / 'observation-03.json')
    final['browser'], final['evidence_sha256'] = final_browser, digest(hashes)
    put(new_run / 'observation-03.json', final)
    put(new_run / 'outcome.json', outcome)
    plan = prepare(reading)
    assert plan['source']['authenticated'] is False
    assert '公开读取' in repo.describe_repository_document(plan)
    assert repo.list_repository_readings(computer_base=reading['base'])[0]['authenticated'] is False
    assert execute(reading, plan)['ok']


@pytest.mark.parametrize('last_code', ['browser_wait_observed', 'browser_fill_verified'])
def test_repository_navigation_may_finish_on_verified_wait_only(reading, last_code):
    run = reading['run']
    final = deepcopy(reading['final'])
    pending = deepcopy(final)
    pending['browser']['checks']['goal_met'] = False
    pending['browser']['text'] = ''
    pending['browser']['discovery']['repository_status'] = None
    pending['browser']['discovery']['repository_verified'] = False
    pending['browser']['state_id'] = digest({k: v for k, v in pending['browser'].items()
                                           if k not in {'state_id', 'actions'}})
    pending['checks'] = {'sources_ready': False, 'goal_met': False}
    prior = _load(run / 'step-02' / 'environment-result.json')[0]
    prior['result']['observation'] = deepcopy(pending['browser'])
    put(run / 'step-02' / 'environment-result.json', {'data': prior, 'sha256': digest(prior)})
    put(run / 'step-02' / 'verification.json', prior['result'])
    put(run / 'observation-03.json', pending)
    action = {'kind': 'browser_wait', 'rule_id': 'approved-wait', 'control_id': 'wait',
              'state_id': pending['browser']['state_id'], 'label': 'Wait for completed body'}
    state_hash, choices = choices_for(run, pending, [action])
    step = run / 'step-03'
    step.mkdir()
    write_new(step / 'choices.json', {'state_sha256': state_hash, 'choices': choices})
    write_new(step / 'intent.json', choices[0])
    write_new(step / 'decision.json', {'method': 'single_legal_action', 'choice': choices[0],
                                      'state_sha256': state_hash})
    intent = {'action': action}
    _save(step / 'environment-intent.json', intent)
    result = {'ok': True, 'state': 'verified', 'code': last_code,
              'observation': final['browser'], 'verification': {'independent_read': True}}
    record = {'action': action, 'intent_sha256': digest(intent), 'result': result}
    _save(step / 'environment-result.json', record)
    write_new(step / 'verification.json', result)
    hashes = [_load(p / 'environment-result.json')[1] for p in sorted(run.glob('step-*'))]
    final['evidence_sha256'] = digest(hashes)
    write_new(run / 'observation-04.json', final)
    outcome = deepcopy(reading['outcome'])
    outcome['attempts'][1]['result'] = deepcopy(prior['result'])
    outcome['completed'][1]['result'] = deepcopy(prior['result'])
    attempt = {'step': 3, 'action': action, 'result': result}
    outcome['attempts'].append(deepcopy(attempt))
    outcome['completed'] = deepcopy(outcome['attempts'])
    outcome['actions'] = 3
    put(run / 'outcome.json', outcome)
    if last_code == 'browser_wait_observed':
        assert execute(reading, prepare(reading))['ok'] is True
    else:
        with pytest.raises(ComputerError, match='repository_source_invalid'):
            prepare(reading)
