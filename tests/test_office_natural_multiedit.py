"""Natural Office grouping and ordered dispatch; offline native/model boundaries."""
from copy import deepcopy
import json
from pathlib import Path

from openpyxl import load_workbook
import pytest

from agent_service import computer_interaction as interaction, office_edit, office_intent
from agent_service.computer import ComputerError, read_record
from test_computer_interaction import BROWSER, CurrentChoiceClient, ctx, run


INSTRUCTION = ('Word同一段把预算改为年度预算、待确认改为已确认，标题改为编辑复核；'
               'Excel预算页B2改19、B3改23，保留页B2改456，保留样式和公式。')


class PlanningClient:
    last_call = {'state': 'offline_test_only'}

    def __init__(self, duplicate=False):
        self.contexts = []
        self.duplicate = duplicate

    def complete(self, system, user, **options):
        context = json.loads(user)
        self.contexts.append(context)
        changes = []
        for file in context['files']:
            for row in file['content']:
                after = None
                if file['kind'] == 'document':
                    after = {'编辑验收': '编辑复核', '预算待确认': '年度预算已确认'}.get(row['value'])
                elif row['at'] in ({'sheet': '预算', 'cell': 'B2'},
                                   {'sheet': '预算', 'cell': 'B3'},
                                   {'sheet': '保留页', 'cell': 'B2'}):
                    after = 456 if row['at']['sheet'] == '保留页' else (19 if row['at']['cell'] == 'B2' else 23)
                if after is not None:
                    changes.append({'target': row['target'], 'after': after})
        if self.duplicate:
            changes.append(deepcopy(changes[1]))
        return {'kind': 'edits', 'changes': changes}


def natural_plan(ctx, client=None):
    return office_intent.propose(ctx['session'], INSTRUCTION, [ctx['word'], ctx['sheet']],
                                 client=client or PlanningClient())['plan']


def test_natural_same_paragraph_and_multi_sheet_edits_need_no_action_selection(ctx):
    originals = [ctx['word'].read_bytes(), ctx['sheet'].read_bytes()]
    planner = PlanningClient()
    plan = natural_plan(ctx, planner)
    assert len(planner.contexts) == 1
    assert planner.contexts[0]['instruction'] == INSTRUCTION
    edits = plan['goal']['edits']
    assert [edit['id'] for edit in edits] == ['e1', 'e2']
    assert all('location' not in edit and 'locations' not in edit for edit in edits)
    assert edits[0]['spec']['changes'] == [
        {'paragraph_index': 0, 'before': '编辑验收', 'after': '编辑复核'},
        {'paragraph_index': 1, 'before': '预算待确认', 'after': '年度预算已确认'}]
    assert edits[1]['spec']['changes'] == [
        {'sheet': '预算', 'cell': 'B2', 'before': 10, 'after': 19},
        {'sheet': '预算', 'cell': 'B3', 'before': 20, 'after': 23},
        {'sheet': '保留页', 'cell': 'B2', 'before': 123, 'after': 456}]
    readonly = [row for file in planner.contexts[0]['files'] for row in file['content']
                if row['value'] == {'formula': '=SUM(B2:B3)'}]
    assert len(readonly) == 1 and readonly[0]['editable'] is False and 'target' not in readonly[0]
    selector = CurrentChoiceClient()
    outcome = run(ctx, plan, client=selector)
    assert outcome['ok'] and not selector.calls
    assert [attempt['action'] for attempt in outcome['attempts']] == [
        {'kind': 'office_edit', 'edit_id': 'e1'}, {'kind': 'office_edit', 'edit_id': 'e2'}]
    assert len(ctx['calls']) == 2 and len(outcome['artifacts']) == 2
    assert [ctx['word'].read_bytes(), ctx['sheet'].read_bytes()] == originals
    for number in (1, 2):
        decision = read_record(Path(outcome['run']) / f'step-{number:02d}' / 'decision.json')
        assert decision['method'] == 'single_legal_action'
    word_output = next(item['path'] for item in outcome['artifacts'] if item['kind'] == 'document')
    paragraphs = office_edit._observe(word_output, 'document', 2)['paragraphs']
    assert [item['text'] for item in paragraphs] == [
        '编辑复核', '年度预算已确认', '保留这一段和原有样式。']
    output = next(item['path'] for item in outcome['artifacts'] if item['kind'] == 'spreadsheet')
    book = load_workbook(output, data_only=False)
    try:
        assert [book['预算'][cell].value for cell in ('B2', 'B3', 'B4')] == [19, 23, '=SUM(B2:B3)']
        assert book['保留页']['B2'].value == 456
        assert book['预算']['B2'].font.bold and book['预算']['B2'].number_format == '0.00'
    finally:
        book.close()


def test_natural_response_cannot_repeat_same_paragraph_target(ctx):
    with pytest.raises(ComputerError, match='office_planning_target_invalid'):
        natural_plan(ctx, PlanningClient(duplicate=True))
    assert not list(ctx['session'].root.glob('goals/*.json')) and not ctx['calls']


def action_environment(goal, observation):
    env = interaction.InteractionEnvironment.__new__(interaction.InteractionEnvironment)
    env.goal = deepcopy(goal)
    env._check = lambda: None
    env.observe = lambda: deepcopy(observation)
    return env


def test_office_only_order_follows_plan_and_skips_started_files(ctx):
    goal = natural_plan(ctx)['goal']
    goal['edits'].reverse()
    observation = {'browser': None, 'started_edits': [], 'checks': {'sources_ready': True}}
    env = action_environment(goal, observation)
    assert env.actions(observation) == [{'kind': 'office_edit', 'edit_id': 'e2'}]
    observation['started_edits'] = ['e2']
    assert env.actions(observation) == [{'kind': 'office_edit', 'edit_id': 'e1'}]
    observation['started_edits'] = ['e2', 'e1']
    assert env.actions(observation) == []


@pytest.mark.parametrize('ready', [False, True])
def test_browser_tasks_keep_existing_action_choices(ctx, ready):
    goal = natural_plan(ctx)['goal']
    goal['browser'] = deepcopy(BROWSER)
    browser_action = {'kind': 'browser_fill', 'rule_id': 'query', 'state_id': 'fresh'}
    observation = {'browser': {'checks': {'goal_met': ready}, 'locked': False,
                               'actions': [browser_action]},
                   'started_edits': [], 'checks': {'sources_ready': ready}}
    actions = action_environment(goal, observation).actions(observation)
    assert actions == ([] if ready else [browser_action]) + [
        {'kind': 'office_edit', 'edit_id': 'e1'}, {'kind': 'office_edit', 'edit_id': 'e2'}]
