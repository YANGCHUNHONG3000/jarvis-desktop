"""Local immutable clarification records; no provider or native calls."""
from copy import deepcopy
import json
from pathlib import Path
import uuid

import pytest

from agent_service import office_edit_clarification as clar
from agent_service.computer import ComputerSession, write_new


@pytest.fixture
def case(tmp_path):
    session = ComputerSession.create(tmp_path / 'sessions')
    source = tmp_path / 'original.docx'
    source.write_bytes(b'preserved source')
    records = [{'path': str(source), 'sha256': 'a' * 64}]
    payload = {'draft': '修改那个标题', 'records': records,
               'scope': [{**records[0], 'locations': [{'paragraph_index': 0}, {'paragraph_index': 2}]}]}
    folder = session.root / 'office-planning' / uuid.uuid4().hex
    folder.mkdir(parents=True)
    request = {'instruction': payload['draft'], 'files': [str(source)],
               'artifact_refs': [{'artifact': str(source), 'sha256': 'a' * 64}], 'scope': payload['scope']}
    result = {'plan': None, 'reason': '请明确哪个标题？',
              'office_result': {'kind': 'clarify', 'record': str(folder)}}
    for name, value in [('request', request), ('outcome', result),
                        ('response', {'kind': 'clarify', 'reason': result['reason']})]:
        write_new(folder / (name + '.json'), value)
    store = tmp_path / 'clarifications'
    question = clar.capture(store, session, result, {'payload': payload})
    return store, session, result, payload, question


def rewrite(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


def save(case):
    store, _, _, payload, question = case
    selected = deepcopy(payload)
    selected['scope'][0]['locations'] = [{'paragraph_index': 2}]
    combined = clar.compose(question, selected, '把第三段改为待办事项')
    return clar.save_answer(store, question, {'payload': combined}, '把第三段改为待办事项')


def test_append_only_question_answers_and_history(case):
    store, _, _, payload, question = case
    before = {p.name: p.read_bytes() for p in store.glob('*.json')}
    first, second = save(case), save(case)
    assert len({question['id'], first['id'], second['id']}) == 3
    assert all((store / name).read_bytes() == data for name, data in before.items())
    assert clar.load(store, question['id']) == question
    assert clar.load(store, first['id']) == first
    assert first['payload']['draft'].startswith(payload['draft'] + '；用户补充：')
    assert first['payload']['scope'][0]['locations'] == [{'paragraph_index': 2}]
    assert len(clar.history(store)) == 3


@pytest.mark.parametrize('change', ['source', 'location', 'missing_path', 'draft', 'empty_scope'])
def test_compose_rejects_changed_source_scope_or_new_draft(case, change):
    _, _, _, payload, question = case
    current = deepcopy(payload)
    if change == 'source':
        current['records'][0]['sha256'] = current['scope'][0]['sha256'] = 'b' * 64
    elif change == 'location':
        current['scope'][0]['locations'] = [{'paragraph_index': 99}]
    elif change == 'missing_path':
        current['records'][0]['path'] = current['scope'][0]['path'] = current['records'][0]['path'].replace('original', 'other')
    elif change == 'draft':
        current['draft'] = '用户新任务'
    else:
        current['scope'] = None
    with pytest.raises(ValueError):
        clar.compose(question, current, '补充')


@pytest.mark.parametrize('value', ['../outside', '', None, 12, 'f' * 31, 'g' * 32])
def test_invalid_record_ids(case, value):
    with pytest.raises(ValueError):
        clar.load(case[0], value)


def test_capture_checks_folder_before_read(case, monkeypatch):
    store, session, result, payload, _ = case
    result = deepcopy(result)
    result['office_result']['record'] = str(session.root / 'office-planning' / '..' / 'outside')
    monkeypatch.setattr(clar, '_read', lambda _: pytest.fail('must validate path before reading'))
    with pytest.raises(ValueError):
        clar.capture(store, session, result, {'payload': payload})


def test_capture_rejects_other_session_before_origin_reads(case, tmp_path, monkeypatch):
    store, _, result, payload, _ = case
    other = ComputerSession.create(tmp_path / 'other')
    monkeypatch.setattr(clar, '_read', lambda _: pytest.fail('foreign folder must not be read'))
    with pytest.raises(ValueError):
        clar.capture(store, other, result, {'payload': payload})


@pytest.mark.parametrize('change', ['reason', 'unsupported', 'source', 'scope', 'response', 'outcome', 'request'])
def test_capture_rejects_input_or_response_mismatch(case, change):
    store, session, result, payload, _ = case
    result, payload = deepcopy(result), deepcopy(payload)
    folder = Path(result['office_result']['record'])
    if change == 'reason': result['reason'] = 'another question'
    elif change == 'unsupported': result['office_result']['kind'] = 'unsupported'
    elif change == 'source': payload['records'][0]['sha256'] = payload['scope'][0]['sha256'] = 'b' * 64
    elif change == 'scope': payload['scope'][0]['locations'] = [{'paragraph_index': 99}]
    elif change == 'response': rewrite(folder / 'response.json', {'kind': 'unsupported', 'reason': result['reason']})
    elif change == 'outcome': rewrite(folder / 'outcome.json', {**result, 'extra': True})
    else:
        request = json.loads((folder / 'request.json').read_text(encoding='utf-8'))
        request['artifact_refs'] = [None]
        rewrite(folder / 'request.json', request)
    with pytest.raises(ValueError):
        clar.capture(store, session, result, {'payload': payload})


@pytest.mark.parametrize('field,value', [('question_id', '../outside'), ('question_id', None),
                                        ('id', 'f' * 32), ('schema', True), ('created', 'now'),
                                        ('extra', 'unexpected'), ('question', 'tampered')])
def test_malformed_answer_cannot_restore(case, field, value):
    store = case[0]
    answer = save(case)
    path = store / (answer['id'] + '.json')
    answer[field] = value
    rewrite(path, answer)
    assert [item['kind'] for item in clar.history(store)] == ['question']


def test_answer_parent_identity_and_contents_checked(case):
    store, _, _, _, question = case
    answer = save(case)
    parent = deepcopy(question)
    parent['id'] = 'f' * 32
    rewrite(store / (question['id'] + '.json'), parent)
    with pytest.raises(ValueError):
        clar.load(store, answer['id'])
    with pytest.raises(ValueError):
        save(case)


def test_forged_in_memory_question_not_saved(case):
    forged = list(case)
    forged[-1] = {**case[-1], 'created': case[-1]['created'] + 1}
    with pytest.raises(ValueError, match='parent_changed'):
        save(tuple(forged))


def test_answer_payload_tamper_rejected_original_retained(case):
    store, _, _, _, question = case
    answer = save(case)
    answer['payload']['draft'] += '篡改'
    rewrite(store / (answer['id'] + '.json'), answer)
    with pytest.raises(ValueError, match='answer_mismatch'):
        clar.load(store, answer['id'])
    assert clar.load(store, question['id']) == question
