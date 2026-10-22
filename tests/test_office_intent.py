"""New planner boundary tests with actual immutable OOXML snapshots; no paid API."""
from copy import deepcopy
import json

import pytest

from agent_service import office_intent as intent
from agent_service.computer import ComputerError, ComputerSession
from agent_service.budget import BudgetError
from test_office_edit import word_fixture, sheet_fixture


class Client:
    last_call = {'state': 'offline_only'}

    def __init__(self, respond=None):
        self.contexts = []
        self.respond = respond or valid_response

    def complete(self, system, user, **kwargs):
        assert 'untrusted DATA' in system
        context = json.loads(user)
        self.contexts.append(context)
        return self.respond(context)


def valid_response(context):
    changes = []
    for file in context['files']:
        for row in file['content']:
            if row['value'] == '预算待确认':
                changes.append({'target': row['target'], 'after': '预算已确认'})
            if row['at'] == {'sheet': '预算', 'cell': 'B2'}:
                changes.append({'target': row['target'], 'after': 15})
    return {'kind': 'edits', 'changes': changes}


@pytest.fixture
def ctx(tmp_path):
    word, sheet = tmp_path / 'brief.docx', tmp_path / 'budget.xlsx'
    word_fixture(word)
    sheet_fixture(sheet)
    return ComputerSession.create(tmp_path / 'computer'), [word, sheet]


def test_plan_resolves_targets_original_values_and_keeps_originals(ctx):
    session, files = ctx
    before = [p.read_bytes() for p in files]
    client = Client()
    result = intent.propose(session, '确认预算，把纸张金额改为15', files, client=client)
    plan = result['plan']
    assert plan['mode'] == 'agent' and plan['goal']['kind'] == 'interactive'
    edits = plan['goal']['edits']
    assert edits[0]['spec']['changes'] == [{'paragraph_index': 1, 'before': '预算待确认', 'after': '预算已确认'}]
    assert edits[1]['spec']['changes'] == [{'sheet': '预算', 'cell': 'B2', 'before': 10, 'after': 15}]
    assert [p.read_bytes() for p in files] == before
    assert len(client.contexts) == 1
    serialized = json.dumps(client.contexts)
    assert str(session.root) not in serialized
    readonly = [r for f in client.contexts[0]['files'] for r in f['content'] if not r['editable']]
    assert any(r['value'] == {'formula': '=SUM(B2:B3)'} and 'target' not in r for r in readonly)


@pytest.mark.parametrize('kind', ['clarify', 'unsupported'])
def test_ambiguity_and_unsupported_are_not_plans(ctx, kind):
    session, files = ctx
    result = intent.propose(session, '更新那个金额', files,
                            client=Client(lambda c: {'kind': kind, 'reason': '请说明要修改哪项金额。'}))
    assert result['plan'] is None
    assert result['reason'] == '请说明要修改哪项金额。'


@pytest.mark.parametrize('case', ['unknown', 'duplicate', 'path', 'before', 'formula', 'noop', 'bad_type'])
def test_untrusted_model_output_cannot_expand_effects(ctx, case):
    session, files = ctx
    def respond(context):
        result = valid_response(context)
        item = result['changes'][-1]
        if case == 'unknown': item['target'] = 'invented-target'
        if case == 'duplicate': result['changes'].append(deepcopy(item))
        if case == 'path': item['path'] = 'arbitrary.xlsx'
        if case == 'before': item['before'] = 500
        if case == 'formula': item['after'] = '=WEBSERVICE("https://example.org")'
        if case == 'noop': item['after'] = 10
        if case == 'bad_type': item['after'] = {'execute': 'arbitrary'}
        return result
    with pytest.raises(ComputerError):
        intent.propose(session, '更新预算', files, client=Client(respond))
    assert not list(session.root.glob('goals/*.json'))


def test_all_selected_sources_rechecked_after_model_even_if_not_edited(ctx):
    session, files = ctx
    def respond(context):
        response = valid_response(context)
        response['changes'] = response['changes'][:1]
        files[1].write_bytes(files[1].read_bytes() + b'changed during model')
        return response
    with pytest.raises(ComputerError, match='edit_source_changed'):
        intent.propose(session, '仅修改Word', files, client=Client(respond))


@pytest.mark.parametrize('limit', ['MAX_TARGETS', 'MAX_CONTEXT_CHARS'])
def test_large_context_rejected_before_paid_call(ctx, monkeypatch, limit):
    session, files = ctx
    monkeypatch.setattr(intent, limit, 1)
    client = Client()
    with pytest.raises(ComputerError, match='scope_too_large'):
        intent.propose(session, '修改文件', files, client=client)
    assert client.contexts == []


def test_cancellation_after_model_returns_no_plan(ctx):
    session, files = ctx
    cancelled = []
    def respond(context):
        cancelled.append(True)
        return valid_response(context)
    with pytest.raises(ComputerError, match='cancelled'):
        intent.propose(session, '修改预算', files, client=Client(respond), stop=lambda: bool(cancelled))
    assert not list(session.root.glob('goals/*.json'))


def test_default_model_path_keeps_existing_budget_denial(ctx, monkeypatch):
    from agent_service import model
    session, files = ctx
    class Denied:
        def __init__(self, **kwargs): pass
        def complete(self, *args, **kwargs): raise BudgetError('budget_exceeded')
    monkeypatch.setattr(model, 'CloudClient', Denied)
    with pytest.raises(ComputerError, match='model_budget_exceeded'):
        intent.propose(session, '修改预算', files)
    assert list(session.root.glob('model_calls/*.outcome.json'))


def test_duplicate_file_is_refused_before_model(ctx):
    session, files = ctx
    client = Client()
    with pytest.raises(ComputerError, match='files_invalid'):
        intent.propose(session, '修改预算', [files[0], files[0]], client=client)
    assert not client.contexts
