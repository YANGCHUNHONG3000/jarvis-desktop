"""Offline goal runner contracts using real sessions and worker request checks."""
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest

from agent_service.computer import ComputerError, ComputerSession, read_record, write_new
from agent_service.computer_goals import (
    arithmetic, display_number, execute_goal, prepare_goal, propose_goal,
)
from agent_service.computer_host import perform_request


def control(identifier, kind, automation='', value=None, patterns=(), **extra):
    return dict(id=identifier, runtime_id=[len(identifier), sum(map(ord, identifier))],
                type=kind, name=identifier, automation_id=automation, value=value,
                patterns=list(patterns), enabled=True, visible=True, focused=False,
                selected=None, expanded=None, **extra)


def observed(profile, controls, hwnd):
    controls = controls + [control('root-' + profile, 'Window', patterns=['focus'])]
    return dict(window=dict(hwnd=hwnd, pid=hwnd + 100, created='fixture',
                            image='trusted.exe', class_name='fixture',
                            title=profile, profile=profile),
                controls=controls, truncated=False)


class Desktop:
    def __init__(self):
        buttons = [f'num{i}Button' for i in range(10)] + [
            'clearButton', 'plusButton', 'minusButton', 'multiplyButton',
            'divideButton', 'equalButton', 'decimalSeparatorButton', 'negateButton']
        self.windows = {
            'calc': observed('calculator', [control(k, 'Button', k, patterns=['invoke'])
                                          for k in buttons] + [
                control('display', 'Text', 'CalculatorResults', '0')], 10),
            'note': observed('notepad', [control('add', 'Button', 'AddButton', patterns=['invoke']),
                control('old-tab', 'TabItem', patterns=['select']),
                control('old-editor', 'Edit', value='USER ORIGINAL', patterns=['set_value'])], 20),
            'files': observed('explorer', [control('item', 'ListItem', patterns=['select'])], 30),
        }
        self.windows['note']['controls'][1]['selected'] = True
        self.actions = []
        self.entry, self.left, self.operator = '', None, None
        self.wrong_result = False
        self.new_tab_value = ''
        self.new_tab_selected = True
        self.extra_editor = False
        self.unknown_at = None
        self.after_action = lambda: None
        self.after_tab_observations = 0
        self.change_after_new_tab = lambda obs: None

    def observe(self, window):
        obs = next(v for v in self.windows.values() if v['window']['hwnd'] == window['hwnd'])
        if any(c['id'] == 'new-tab' for c in obs['controls']):
            self.after_tab_observations += 1
            if self.after_tab_observations == 2:
                self.change_after_new_tab(obs)
        return deepcopy(obs)

    def perform(self, window, target, action, value, check):
        check()
        self.actions.append((window['profile'], target['automation_id'] or target['id'], action, value))
        obs = next(v for v in self.windows.values() if v['window']['hwnd'] == window['hwnd'])
        if action == 'focus':
            for current in self.windows.values():
                for c in current['controls']:
                    if c['type'] == 'Window':
                        c['focused'] = current is obs
        elif window['profile'] == 'calculator':
            key = target['automation_id']
            if key == 'clearButton':
                self.entry, self.left, self.operator = '', None, None
            elif key.startswith('num'):
                self.entry += key[3]
            elif key == 'decimalSeparatorButton':
                self.entry += '.'
            elif key == 'negateButton':
                self.entry = self.entry[1:] if self.entry.startswith('-') else '-' + self.entry
            elif key == 'equalButton':
                right = Decimal(self.entry)
                a = self.left
                result = {'plusButton': lambda: a + right, 'minusButton': lambda: a - right,
                          'multiplyButton': lambda: a * right, 'divideButton': lambda: a / right}[self.operator]()
                self.entry = str(result + (1 if self.wrong_result else 0))
            else:
                self.left, self.operator, self.entry = Decimal(self.entry), key, ''
            next(c for c in obs['controls'] if c['id'] == 'display')['value'] = self.entry or '0'
        elif target['automation_id'] == 'AddButton':
            for c in obs['controls']:
                if c['type'] == 'TabItem':
                    c['selected'] = False
                if c['type'] == 'Edit':
                    c['visible'] = False
            tab = control('new-tab', 'TabItem', patterns=['select'])
            tab['selected'] = self.new_tab_selected
            obs['controls'] += [tab, control('new-editor', 'Edit', value=self.new_tab_value, patterns=['set_value'])]
            if self.extra_editor:
                obs['controls'].append(control('extra-editor', 'Edit', value='', patterns=['set_value']))
        elif action == 'set_value':
            next(c for c in obs['controls'] if c['id'] == target['id'])['value'] = value
        elif action == 'select':
            next(c for c in obs['controls'] if c['id'] == target['id'])['selected'] = True
        self.after_action()

    def host(self, request, root, stop):
        root.mkdir(parents=True)
        if request['op'] == 'act':
            assert request['input_epoch'] == 7
        if request['op'] == 'act' and self.unknown_at == len(self.actions) + 1:
            return dict(ok=False, state='unknown', code='injected_unknown', effects='unknown')
        def check():
            if stop():
                raise ComputerError('cancelled_or_takeover')
        try:
            return perform_request(request, root, self, check)
        except ComputerError as exc:
            return dict(ok=False, state='unknown' if (root / 'intent.json').exists() else 'failed', code=str(exc))


@pytest.fixture
def setup(tmp_path):
    desktop = Desktop()
    session = ComputerSession.create(tmp_path, host=desktop.host)
    snapshots = {name: session._snapshot(obs) for name, obs in desktop.windows.items()}
    return desktop, session, snapshots


def calculate(expression='2+3', output='answer'):
    return dict(op='calculate', window='calc', expression=expression, **{'as': output})


def note(text='实际结果 {{answer}} / 😀 {+^%}'):
    return dict(op='new_note', window='note', text=text)


def plan(setup, steps):
    return prepare_goal(setup[1], 'Explicit fixture goal', setup[2], steps)


def run(setup, prepared, **kwargs):
    return execute_goal(setup[1], prepared, approval=prepared['sha256'], epoch=7,
                        get_epoch=kwargs.pop('get_epoch', lambda: 7), **kwargs)


def test_calculation_to_new_note_uses_actual_output_and_preserves_original(setup):
    prepared = plan(setup, [calculate('12.5*4'), note()])
    outcome = run(setup, prepared)
    assert outcome['ok'] and outcome['state'] == 'verified'
    assert outcome['outputs'] == {'answer': '50.0'}
    assert len(outcome['completed']) == 2
    assert outcome['completed'][1]['saved'] is False
    controls = setup[0].windows['note']['controls']
    assert next(c['value'] for c in controls if c['id'] == 'new-editor') == '实际结果 50.0 / 😀 {+^%}'
    assert next(c['value'] for c in controls if c['id'] == 'old-editor') == 'USER ORIGINAL'
    assert read_record(Path(outcome['run']) / 'outcome.json') == outcome


def test_wrong_arithmetic_never_runs_next_step(setup):
    setup[0].wrong_result = True
    outcome = run(setup, plan(setup, [calculate(), note()]))
    assert outcome['state'] == 'unknown'
    assert outcome['code'] == 'calculation_readback_mismatch'
    assert not outcome['completed'] and not outcome['outputs']
    assert all(a[0] == 'calculator' for a in setup[0].actions)


def test_unknown_action_never_continues_or_replays(setup):
    setup[0].unknown_at = 2
    prepared = plan(setup, [calculate(), note()])
    outcome = run(setup, prepared)
    assert outcome['state'] == 'unknown' and outcome['code'] == 'injected_unknown'
    assert len(setup[0].actions) == 1
    reopened = ComputerSession(setup[1].root, host=setup[0].host)
    with pytest.raises(ComputerError, match='goal_already_claimed'):
        execute_goal(reopened, prepared, approval=prepared['sha256'], epoch=7, get_epoch=lambda: 7)


def test_tampered_plan_cannot_execute(setup):
    prepared = plan(setup, [calculate()])
    prepared['steps'][0]['expression'] = '3+4'
    with pytest.raises(ComputerError, match='goal_plan_changed'):
        run(setup, prepared)
    assert not setup[0].actions


@pytest.mark.parametrize('expression,code', [
    ('1/0', 'division_by_zero'), ('1/3', 'result_precision_unsupported'),
    ('0.0001*0.0001', None), ('100000000+1', 'expression_unsupported'),
    ('01+2', 'expression_unsupported'), ('1e2+1', 'expression_unsupported'),
    ('1+2+3', 'expression_unsupported'), ('1.00001+2', 'expression_unsupported'),
    ('99999999*99999999', 'result_precision_unsupported'),
])
def test_arithmetic_bounds(expression, code):
    if code:
        with pytest.raises(ComputerError, match=code):
            arithmetic(expression)
    else:
        assert arithmetic(expression)[0] == Decimal('.00000001')


@pytest.mark.parametrize('text', ['{{future}}', '{{answer', '{{ answer }}', '{{answer}}\x00'])
def test_invalid_reference_or_text_rejected_before_effect(setup, text):
    with pytest.raises(ComputerError, match='goal_reference_invalid|goal_text_invalid'):
        plan(setup, [calculate(), note(text)])
    assert not setup[0].actions


@pytest.mark.parametrize('steps', [
    [note()], [calculate(), calculate()],
    [dict(op='invoke', window='calc', control_id='num2Button')],
    [dict(op='calculate', window='calc', expression='2+3', result='5', **{'as': 'answer'})],
    [dict(op='select_item', window='note', control_id='old-tab')],
])
def test_schema_output_and_profile_rejections(setup, steps):
    with pytest.raises(ComputerError):
        plan(setup, steps)
    assert not setup[0].actions


def test_takeover_between_steps_stops_before_next_effect(setup):
    epoch = [7]
    def progress(value):
        if value['step'] == 2:
            epoch[0] = 8
    outcome = run(setup, plan(setup, [calculate(), note()]), get_epoch=lambda: epoch[0], progress=progress)
    assert outcome['code'] == 'user_takeover' and outcome['state'] == 'partial'
    assert len(outcome['completed']) == 1
    assert all(a[0] == 'calculator' for a in setup[0].actions)


@pytest.mark.parametrize('attribute,value', [('new_tab_value', 'NONEMPTY'),
                                           ('new_tab_selected', False), ('extra_editor', True)])
def test_blank_tab_contract_prevents_writing(setup, attribute, value):
    setattr(setup[0], attribute, value)
    outcome = run(setup, plan(setup, [note('safe literal')]))
    assert not outcome['ok'] and outcome['state'] == 'unknown'
    assert [a[2] for a in setup[0].actions] == ['focus', 'invoke']
    assert setup[0].actions[-1][1] == 'AddButton'


def test_cancel_after_verified_new_tab_records_partial_and_never_writes(setup):
    cancelled = [False]
    setup[0].after_action = lambda: cancelled.__setitem__(0, setup[0].actions[-1][1] == 'AddButton')
    outcome = run(setup, plan(setup, [note('safe literal')]), stop=lambda: cancelled[0])
    assert not outcome['ok'] and outcome['state'] in {'partial', 'unknown'}
    assert len(setup[0].actions) == 2
    assert not outcome['completed']


def test_read_reference_is_single_substitution_and_explorer_selection(setup):
    setup[0].windows['note']['controls'][2]['value'] = '{{answer}} literal'
    setup[2]['note'] = setup[1]._snapshot(setup[0].windows['note'])
    steps = [dict(op='read_text', window='note', control_id='old-editor', **{'as': 'source'}),
             note('{{source}}'), dict(op='select_item', window='files', control_id='item')]
    outcome = run(setup, plan(setup, steps))
    assert outcome['ok'] and outcome['completed'][1]['text'] == '{{answer}} literal'
    assert setup[0].windows['files']['controls'][0]['selected'] is True


@pytest.mark.parametrize('text', ['Display is 1,234.50', '显示为 −2.5'])
def test_supported_display_labels(text):
    assert display_number({'name': text})[0] in {Decimal('1234.50'), Decimal('-2.5')}


@pytest.mark.parametrize('text', ['Display is 1,23', '1e5', 'NaN', '结果为 5'])
def test_ambiguous_display_rejected(text):
    with pytest.raises(ComputerError, match='calculator_display_unsupported'):
        display_number({'name': text})


def test_proposal_uses_one_model_call_without_execution(setup):
    class Client:
        last_call = {'state': 'offline'}
        calls = 0
        def complete(self, system, user, max_tokens):
            self.calls += 1
            return {'steps': [calculate(), note()], 'reason': 'requested sequence'}
    client = Client()
    proposed = propose_goal(setup[1], 'calculate and write', setup[2], client=client)
    assert proposed['plan'] and client.calls == 1
    assert not setup[0].actions
    assert len(list((setup[1].root / 'model_calls').glob('[0-9].json'))) == 1


@pytest.mark.parametrize('change', ['pid', 'truncated', 'value'])
def test_starting_state_changes_fail_without_effect(setup, change):
    prepared = plan(setup, [calculate()])
    if change == 'pid':
        setup[0].windows['calc']['window']['pid'] += 1
    elif change == 'truncated':
        setup[0].windows['calc']['truncated'] = True
    else:
        next(c for c in setup[0].windows['calc']['controls'] if c['id'] == 'display')['value'] = '99'
    outcome = run(setup, prepared)
    assert not outcome['ok'] and outcome['state'] == 'stopped'
    assert not setup[0].actions


def test_deadline_before_first_effect_stops_and_consumes_claim(setup):
    ticks = iter([0, 240])
    prepared = plan(setup, [calculate()])
    outcome = run(setup, prepared, clock=lambda: next(ticks))
    assert outcome['code'] == 'goal_deadline' and not setup[0].actions
    with pytest.raises(ComputerError, match='goal_already_claimed'):
        run(setup, prepared)


def test_reference_expansion_limit_rejects_before_creating_tab(setup):
    setup[0].windows['note']['controls'][2]['value'] = 'x' * 3000
    setup[2]['note'] = setup[1]._snapshot(setup[0].windows['note'])
    steps = [dict(op='read_text', window='note', control_id='old-editor', **{'as': 'source'}),
             note('{{source}}{{source}}')]
    outcome = run(setup, plan(setup, steps))
    assert outcome['code'] == 'goal_text_invalid'
    assert len(outcome['completed']) == 1
    assert all(a[2] == 'focus' for a in setup[0].actions)


def test_missing_matching_approval_never_claims_or_executes(setup):
    prepared = plan(setup, [calculate()])
    with pytest.raises(ComputerError, match='explicit_goal_approval_required'):
        execute_goal(setup[1], prepared, approval='yes', epoch=7, get_epoch=lambda: 7)
    assert not list((setup[1].root / 'goals').glob('*.run'))
    assert not setup[0].actions


def test_selection_rechecks_reviewed_item_description_after_previous_steps(setup):
    desktop = setup[0]
    def rename_during_calculation():
        if desktop.actions[-1][1] == 'equalButton':
            desktop.windows['files']['controls'][0]['name'] = 'DIFFERENT ITEM'
    desktop.after_action = rename_during_calculation
    prepared = plan(setup, [calculate(), dict(op='select_item', window='files', control_id='item')])
    outcome = run(setup, prepared)
    assert not outcome['ok']
    assert len(outcome['completed']) == 1
    assert not any(action[0] == 'explorer' and action[2] == 'select' for action in desktop.actions)


@pytest.mark.parametrize('change', ['tab_runtime', 'editor_runtime', 'editor_value', 'selected_tab'])
def test_new_note_binding_rejects_replacement_before_write(setup, change):
    def replace(obs):
        tab = next(c for c in obs['controls'] if c['id'] == 'new-tab')
        editor = next(c for c in obs['controls'] if c['id'] == 'new-editor')
        if change == 'tab_runtime':
            tab['runtime_id'] = [999]
        elif change == 'editor_runtime':
            editor['runtime_id'] = [999]
        elif change == 'editor_value':
            editor['value'] = 'SOMEONE ELSE WROTE THIS'
        else:
            tab['selected'] = False
            next(c for c in obs['controls'] if c['id'] == 'old-tab')['selected'] = True
    setup[0].change_after_new_tab = replace
    outcome = run(setup, plan(setup, [note('approved text')]))
    assert not outcome['ok']
    assert not any(a[2] == 'set_value' for a in setup[0].actions)


def test_unrelated_notepad_tooltip_change_does_not_invalidate_blank_binding(setup):
    setup[0].change_after_new_tab = lambda obs: obs['controls'].append(control('tooltip', 'Text', value='tip'))
    outcome = run(setup, plan(setup, [note('approved text')]))
    assert outcome['ok']
    assert outcome['completed'][0]['text'] == 'approved text'


def test_each_mutating_step_focuses_root_within_approved_action_budget(setup):
    prepared = plan(setup, [calculate(), note(), dict(op='select_item', window='files', control_id='item')])
    outcome = run(setup, prepared)
    assert outcome['ok']
    assert prepared['max_actions'] == len(arithmetic('2+3')[1]) + 2 + 1 + 3
    assert outcome['actions'] == prepared['max_actions']
    assert [a[0] for a in setup[0].actions if a[2] == 'focus'] == ['calculator', 'notepad', 'explorer']


@pytest.fixture
def supervised_process(tmp_path, monkeypatch):
    from agent_service import computer_host
    import run_jarvis_desktop
    events = []
    class Pipe:
        def __init__(self, process):
            self.process = process
            self.writes = []
            self.closed = False
        def write(self, value):
            events.append(('write', value))
            self.writes.append(value)
            self.process.running = False
            write_new(tmp_path / 'worker' / 'outcome.json',
                      {'ok': True, 'state': 'verified', 'effects': 'verified'})
        def flush(self):
            events.append(('flush',))
        def close(self):
            self.closed = True
    class Process:
        pid = 43210
        def __init__(self):
            self.running = True
            self.killed = False
            self.stdin = Pipe(self)
        def poll(self):
            return None if self.running else 0
        def kill(self):
            events.append(('kill', self.pid))
            self.killed = True
            self.running = False
        def wait(self, timeout):
            events.append(('wait', timeout))
            assert not self.running
            return 0
    process = Process()
    def popen(args, **kwargs):
        events.append(('popen', process.pid))
        assert args[0] == 'offline-python'
        return process
    monkeypatch.setattr(run_jarvis_desktop, 'desktop_python', lambda: 'offline-python')
    monkeypatch.setattr(computer_host.subprocess, 'Popen', popen)
    return computer_host, process, events, tmp_path / 'worker'


def test_focus_worker_granted_exact_pid_before_start_gate(supervised_process, monkeypatch):
    host, process, events, root = supervised_process
    def grant(pid):
        events.append(('grant', pid))
        assert process.running and not process.stdin.writes
        return True
    monkeypatch.setattr(host, 'grant_foreground', grant)
    result = host.run_supervised({'op': 'act', 'plan': {'action': 'focus'}, 'input_epoch': 7}, root)
    assert result['ok']
    assert events[:3] == [('popen', 43210), ('grant', 43210), ('write', b'1')]
    assert process.stdin.closed and not process.killed


def test_denied_focus_grant_never_releases_worker_and_is_durable(supervised_process, monkeypatch):
    host, process, events, root = supervised_process
    monkeypatch.setattr(host, 'grant_foreground', lambda pid: events.append(('grant', pid)) or False)
    result = host.run_supervised({'op': 'act', 'plan': {'action': 'focus'}, 'input_epoch': 7}, root)
    assert result == {'ok': False, 'state': 'failed', 'effects': 'none', 'code': 'foreground_permission_denied'}
    assert read_record(root / 'supervisor-outcome.json') == result
    assert not process.stdin.writes and not (root / 'intent.json').exists()
    assert process.killed and process.stdin.closed
    assert events == [('popen', 43210), ('grant', 43210), ('kill', 43210), ('wait', 3)]


@pytest.mark.parametrize('operation', [
    {'op': 'observe', 'window': {}}, {'op': 'list'},
    {'op': 'act', 'plan': {'action': 'invoke'}, 'input_epoch': 7},
    {'op': 'act', 'plan': {'action': 'focus'}},
])
def test_non_goal_focus_requests_receive_no_foreground_grant(supervised_process, monkeypatch, operation):
    host, process, events, root = supervised_process
    def unexpected_grant(pid):
        pytest.fail('non-focus or non-goal operation must not receive foreground grant')
    monkeypatch.setattr(host, 'grant_foreground', unexpected_grant)
    assert host.run_supervised(operation, root)['ok']
    assert process.stdin.writes == [b'1'] and process.stdin.closed


def test_stop_after_spawn_never_grants_or_releases_focus_worker(supervised_process, monkeypatch):
    host, process, events, root = supervised_process
    monkeypatch.setattr(host, 'grant_foreground', lambda pid: pytest.fail('stopped request must not grant'))
    result = host.run_supervised({'op': 'act', 'plan': {'action': 'focus'}, 'input_epoch': 7}, root,
                                 stop=lambda: bool(events))
    assert not result['ok'] and result['effects'] == 'none'
    assert not process.stdin.writes and process.killed and process.stdin.closed


def test_read_text_cannot_follow_reused_editor_into_another_note(setup):
    desktop = setup[0]
    def switch_note():
        if desktop.actions[-1][1] == 'equalButton':
            next(c for c in desktop.windows['note']['controls'] if c['id'] == 'old-tab')['selected'] = False
    desktop.after_action = switch_note
    steps = [calculate(), dict(op='read_text', window='note', control_id='old-editor', **{'as':'source'})]
    outcome = run(setup, plan(setup, steps))
    assert not outcome['ok'] and outcome['code'] == 'source_note_changed'
    assert 'source' not in outcome['outputs'] and len(outcome['completed']) == 1


@pytest.mark.parametrize('steps', ['', None, {}, False])
def test_falsey_non_list_model_steps_are_rejected(setup, steps):
    class Client:
        last_call = {}
        def complete(self, *args, **kwargs):
            return {'steps':steps, 'reason':'unsupported'}
    with pytest.raises(ComputerError, match='model_goal_invalid'):
        propose_goal(setup[1], 'do a task', setup[2], client=Client())
    assert not setup[0].actions
