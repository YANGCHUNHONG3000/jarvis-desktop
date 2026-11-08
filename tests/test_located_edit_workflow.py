"""Bounded workflow/source regressions; no cloud calls or native Office effects."""
from copy import deepcopy
from pathlib import Path

import pytest

from agent_service import located_edit_workflow as flow, computer_interaction as interaction
from agent_service import office_edit_adapter as adapter, computer_agent
from agent_service.computer import ComputerError, ComputerSession, read_record
from test_windows_located_edit import _reference
from test_office_edit import word_fixture


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    create = ComputerSession.create
    monkeypatch.setattr(flow.ComputerSession, 'create', lambda: create(tmp_path / 'sessions'))
    monkeypatch.setattr(adapter, 'DEFAULT_STORE', tmp_path / 'gates')
    path = tmp_path / 'original.docx'
    word_fixture(path)
    parent, ref = _reference(tmp_path, path, '待确认')
    state = flow.prepare(parent, [{'ref': ref, 'after': '已确认'}], '仅替换已命中文字')
    return state, ref, parent


def test_preview_pins_location_and_restores_without_execution(prepared):
    state, ref, _ = prepared
    assert flow.load(state['session_root']) == state
    edit = state['plan']['goal']['edits'][0]
    assert edit['location'] == {'ref': ref, 'after': '已确认'}
    assert edit['spec']['changes'] == [{'paragraph_index': 1, 'before': '预算待确认', 'after': '预算已确认'}]
    again = flow.preview(state['session_root'])
    assert again['plan']['goal'] == state['plan']['goal']
    assert not list(Path(state['session_root']).glob('goals/*.run'))


def test_same_file_second_hit_is_not_silently_merged(prepared):
    _, ref, parent = prepared
    with pytest.raises(ComputerError, match='duplicate|overlap'):
        flow.prepare(parent, [{'ref': ref, 'after': '甲'}, {'ref': ref, 'after': '乙'}], '编辑')


@pytest.mark.parametrize('change', ['range', 'after', 'missing_location', 'other_original'])
def test_bound_plan_cannot_expand_or_drop_scope(prepared, change, tmp_path):
    state, _, _ = prepared
    session = ComputerSession(state['session_root'])
    goal = deepcopy(state['plan']['goal']); item = goal['edits'][0]
    if change == 'range':
        item['spec']['changes'][0]['paragraph_index'] = 0
    elif change == 'after':
        item['spec']['changes'][0]['after'] = '未批准'
    elif change == 'missing_location':
        item.pop('location')
    else:
        path = Path(item['source_root']) / 'source.json'
        record = read_record(path)
        other = tmp_path / 'identical.docx'; other.write_bytes(Path(record['original']).read_bytes())
        record['original'] = str(other)
        import json
        path.write_text(json.dumps(record), encoding='utf8')
    plan = computer_agent.prepare(session, state['plan']['instruction'], goal)
    with pytest.raises((ValueError, ComputerError)):
        flow.execute(state['session_root'], plan)
    assert not list(session.root.glob('goals/*.run'))


def test_no_review_cannot_execute_or_claim_outputs(prepared):
    state, _, _ = prepared
    with pytest.raises(ValueError):
        flow.execute(state['session_root'], state['plan'])
    with pytest.raises(ComputerError, match='outputs_incomplete'):
        flow.outputs(state['session_root'])
    assert not list(Path(state['session_root']).glob('goals/*.run'))


def test_changed_parent_evidence_prevents_refresh(prepared):
    state, _, parent = prepared
    (parent / 'unapproved-extra.json').write_text('{}')
    with pytest.raises(ComputerError, match='source_changed'):
        flow.preview(state['session_root'])


def test_recovery_preview_unwraps_adapter_response(prepared, monkeypatch):
    state, _, _ = prepared
    root = Path(state['session_root'])
    (root / 'goals' / ('a' * 64 + '.run')).mkdir()
    outcome = {'ok': False, 'state': 'partial', 'code': 'agent_paused'}
    monkeypatch.setattr(flow, '_latest', lambda *args: (state['plan'], outcome))
    continuation = deepcopy(state['plan']); continuation['recovery'] = {'test': 'wrapper-shape-only'}
    seen = []
    original = flow._check_plan
    def check(session, manifest, plan, initial):
        if 'recovery' in plan:
            seen.append(plan)
        else:
            original(session, manifest, plan, initial)
    monkeypatch.setattr(flow, '_check_plan', check)
    monkeypatch.setattr(adapter, 'prepare_resume', lambda *args: {'plan': continuation, 'reason': 'Review remaining edits'})
    result = flow.preview(root)
    assert result['plan'] == continuation and seen == [continuation]


@pytest.mark.parametrize('located,expected', [(True, ['e1']), (False, ['e1', 'e2'])])
def test_only_located_office_order_is_deterministic(located, expected):
    env = interaction.InteractionEnvironment.__new__(interaction.InteractionEnvironment)
    env.goal = {'browser': None, 'edits': [{'id': 'e1', 'after_browser': False}, {'id': 'e2', 'after_browser': False}]}
    if located:
        env.goal['edits'][0]['location'] = {}
    obs = {'browser': None, 'started_edits': [], 'checks': {'sources_ready': True}}
    env._check = lambda: None
    env.observe = lambda: deepcopy(obs)
    assert [a['edit_id'] for a in env.actions(obs)] == expected
    obs['started_edits'] = ['e1']
    assert env.actions(obs) == [{'kind': 'office_edit', 'edit_id': 'e2'}]
