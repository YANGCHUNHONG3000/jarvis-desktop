from copy import deepcopy
import json
import pytest

from agent_service import windows_stages as stages, windows_task as task
from agent_service.computer import ComputerError, digest
from agent_service.budget import BudgetError

WINDOW = {'hwnd': 100, 'pid': 200, 'created': 'synthetic', 'image': 'C:/Windows/explorer.exe',
          'class_name': 'CabinetWClass', 'title': 'files', 'profile': 'explorer'}

def control(name='Documents'):
    return {'id': name, 'type': 'TreeItem', 'name': name, 'automation_id': '',
            'patterns': ['select', 'expand', 'collapse', 'focus'], 'enabled': True,
            'visible': True, 'selected': False, 'expanded': 0, 'focused': False}

def observation():
    return {'window': deepcopy(WINDOW), 'application': 'explorer', 'scope': 'visible_controls',
            'controls': [control()], 'truncated': False, 'dialogs': []}

class Backend:
    def __init__(self):
        self.state = observation(); self.actions = []; self.observations = 0; self.unknown = False
    def observe(self, window, *, stop):
        self.observations += 1
        return deepcopy(self.state)
    def candidates(self, observed, condition):
        c = task.locate(observed, condition['selector'])
        action = 'expand' if condition['state'] == 'expanded' else 'select'
        return [] if c is None else [{'control_id': c['id'], 'action': action, 'value': ''}]
    def act(self, window, control, action, value, before, operation, *, stop):
        self.actions.append(action)
        if self.unknown: return {'ok': False, 'state': 'unknown', 'effects': 'unknown'}
        self.state['controls'][0]['expanded' if action == 'expand' else 'selected'] = True
        return {'ok': True, 'state': 'verified', 'effects': 'verified'}

class Client:
    def __init__(self, remaining='next', error=None):
        self.remaining = remaining; self.calls = 0; self.error = error
    def complete(self, system, context, max_tokens):
        self.calls += 1
        if self.error: raise self.error
        return {'stage_instruction': 'select Documents', 'remaining_instruction': self.remaining,
            'conditions': [{'id': 'W1C1', 'state': 'selected' if self.calls == 1 else 'expanded', 'value': True, 'retain': False}]}

def setup(tmp_path, remaining='next'):
    root = stages.create('Select Documents then another task', [observation()], parent=tmp_path)
    return root, Backend(), Client(remaining)


def test_provider_session_rejects_wrong_backend_before_observation_or_model(tmp_path):
    from agent_service.windows_winapp import PROVIDER_BINDING
    root = stages.create('Select Documents', [observation()], parent=tmp_path,
                         provider_binding=PROVIDER_BINDING)
    backend, client = Backend(), Client('')
    with pytest.raises(ComputerError, match='windows_provider_binding_mismatch'):
        advance(root, backend, client, tmp_path)
    assert backend.observations == client.calls == 0
    assert not list((root/'attempts').iterdir())


def test_provider_child_binding_survives_restart_without_replaying(tmp_path):
    from agent_service.windows_winapp import PROVIDER_BINDING
    root = stages.create('Select Documents', [observation()], parent=tmp_path,
                         provider_binding=PROVIDER_BINDING)
    backend, client = Backend(), Client('')
    backend.provider_binding = deepcopy(PROVIDER_BINDING)
    backend.state['controls'][0]['bounds'] = [10, 20, 110, 40]
    from agent_service.windows_access import WinappBackend
    backend.allowed = WinappBackend.allowed
    view = advance(root, backend, client, tmp_path)
    assert task.load(view['task_root'])['provider_binding'] == PROVIDER_BINDING
    with pytest.raises(ComputerError, match='windows_provider_binding_mismatch'):
        stages.execute(root, Backend())
    assert not (root/'approval-01.json').exists()
    stages.execute(root, backend)
    assert len(backend.actions) == 1
    assert stages.load(root)['provider_binding'] == PROVIDER_BINDING
    stages.execute(root, backend)
    assert len(backend.actions) == 1 and client.calls == 1


def test_provider_stage_context_excludes_unsupported_focus(tmp_path):
    from agent_service.windows_winapp import PROVIDER_BINDING
    from agent_service.windows_access import WinappBackend
    root = stages.create('Select Documents', [observation()], parent=tmp_path,
                         provider_binding=PROVIDER_BINDING)
    backend = Backend()
    backend.provider_binding = deepcopy(PROVIDER_BINDING)
    backend.state['controls'][0]['bounds'] = [10, 20, 110, 40]
    backend.allowed = WinappBackend.allowed

    class ScopedClient(Client):
        def complete(self, system, context, max_tokens):
            rows = [c for w in json.loads(context)['windows'] for c in w['candidates']]
            assert rows and all('focused' not in c['states'] for c in rows)
            assert all(c['type'] != 'VisualNavigation' for c in rows)
            return super().complete(system, context, max_tokens)

    state = advance(root, backend, ScopedClient(''), tmp_path)
    assert state['stage_number'] == 1 and not backend.actions


@pytest.mark.parametrize('role', ['next_hit','close_find'])
def test_office_completed_stage_cannot_replay_from_new_starting_cell(monkeypatch,role):
    from agent_service import windows_winapp_office as office
    def decode(selector, token):
        return ({'source':{'role':role}}, {'dialog':{'hwnd':101,'pid':200,'created':'same'},
            'query':'纸张' if token=='first' or role=='next_hit' else '新文字',
            'next_proof':{'before':token,'expected':{'address':'A2'}}})
    monkeypatch.setattr(office,'decode',decode)
    first={'id':'target_1','window':deepcopy(WINDOW),
        'selector':{'type':office.TYPE,'automation_id':office.ID,'name':office.LABEL[role]},
        'state':'value','value':'first','retain':False}
    next_condition={**deepcopy(first),'value':'changed-starting-cell'}
    records=[{'scan':{'goal':{'conditions':[first]},'completed':['target_1']}}]
    with pytest.raises(ComputerError,match='completed_target_repeated'):
        stages._reject_repeated(records,{'conditions':[next_condition]})

def advance(root, backend, client, tmp_path, **kw):
    return stages.advance(root, backend, client, task_base=tmp_path/'tasks', proposal_base=tmp_path/'proposals', **kw)

def test_pending_restart_approval_and_verified_no_replay(tmp_path):
    root, backend, client = setup(tmp_path)
    view = advance(root, backend, client, tmp_path)
    assert view['needs_acceptance'] and not view['can_advance'] and not backend.actions
    count = backend.observations
    assert advance(root, backend, client, tmp_path) == stages.load(root)
    assert client.calls == 1 and backend.observations == count
    done = stages.execute(root, backend)
    assert done['summary']['state'] == 'verified' and done['can_advance']
    assert done['completed_stages'] == [{'number':1, 'instruction':'select Documents'}]
    assert len(backend.actions) == 1
    stages.execute(root, backend)
    assert len(backend.actions) == 1
    second = advance(root, backend, client, tmp_path)
    assert second['stage_number'] == 2 and second['needs_acceptance'] and client.calls == 2
    assert second['completed_stages'] == done['completed_stages']
    assert len(backend.actions) == 1
    assert stages.execute(root, backend)['summary']['state'] == 'verified'
    assert len(backend.actions) == 2

def test_empty_remaining_is_not_completion_or_advance(tmp_path):
    root, backend, client = setup(tmp_path, remaining='')
    advance(root, backend, client, tmp_path); view = stages.execute(root, backend)
    assert not view['can_advance'] and view['instruction']
    with pytest.raises(ComputerError, match='no_remaining_work'): advance(root, backend, client, tmp_path)

def test_unknown_never_replayed(tmp_path):
    root, backend, client = setup(tmp_path); advance(root, backend, client, tmp_path)
    backend.unknown = True
    assert stages.execute(root, backend)['summary']['state'] == 'unknown'
    assert advance(root, backend, client, tmp_path)['summary']['state'] == 'unknown'
    with pytest.raises(ComputerError, match='unknown'): stages.execute(root, backend)
    assert len(backend.actions) == 1 and client.calls == 1


def test_close_recheck_overlay_restores_then_requires_fresh_approval(tmp_path, monkeypatch):
    from agent_service import windows_winapp_office as office
    root, backend, client = setup(tmp_path)
    advance(root, backend, client, tmp_path)
    backend.unknown = True
    old = stages.execute(root, backend); child = old['task_root']
    inventory = task._inventory(child)
    # Exercise the stage contract using a synthetic unknown child; native Close
    # eligibility and proof validation have independent provider tests.
    monkeypatch.setattr(stages, '_can_recheck', lambda r: bool(r and r['root'] == child
        and r['approved'] and r['summary']['state'] == 'unknown' and not r.get('close_recheck')))
    calls = []
    def readback(path, selected_backend, *, stop):
        calls.append(path)
        return {'inventory': inventory}
    def verify(proof, path, *, scan, expected_stage_root):
        assert expected_stage_root == root and path == child
        if proof != {'inventory': inventory} or task._inventory(path) != inventory:
            raise ComputerError('windows_office_recheck_source_changed')
    monkeypatch.setattr(office, 'recheck_close', readback)
    monkeypatch.setattr(office, 'verify_close_recheck_locked', verify)
    current = stages.recheck_current_close(root, backend)
    assert current['summary']['state'] == 'unknown' and current['summary']['completed'] == []
    assert current['close_reconciled'] and current['can_advance'] and not current['can_recheck_close']
    assert current['completed_stages'][0]['reconciled']
    assert stages.load(root) == current
    assert stages.execute(root, backend) == current
    assert stages.recheck_current_close(root, backend) == current and len(calls) == 1
    assert task._inventory(child) == inventory and len(backend.actions) == client.calls == 1
    # Same old action is protected despite an empty historical completed list.
    client.calls = 0
    with pytest.raises(ComputerError, match='completed_target_repeated'):
        advance(root, backend, client, tmp_path)
    client.calls = 1
    second = advance(root, backend, client, tmp_path)
    assert second['needs_acceptance'] and second['stage_number'] == 2
    row, _ = task._load(root/'stage-02.json')
    assert row['parent_inventory'] == digest({'task_inventory': inventory,
        'close_recheck':task._load(root/'close-recheck-01.json')[1]})
    assert len(backend.actions) == 1 and stages.load(root) == second
    backend.unknown = False
    assert stages.execute(root, backend)['summary']['state'] == 'verified'
    assert len(backend.actions) == 2 and task._inventory(child) == inventory
    # Validly rehashed proof edits still invalidate the bound descendant.
    wrapper, _ = task._load(root/'close-recheck-01.json')
    wrapper['proof']['inventory'] = {}
    (root/'close-recheck-01.json').write_text(json.dumps({'schema':1,'payload':wrapper,'sha256':digest(wrapper)}))
    with pytest.raises(ComputerError, match='changed'):
        stages.load(root)


def test_generic_unknown_cannot_request_office_recheck(tmp_path):
    root, backend, client = setup(tmp_path); advance(root, backend, client, tmp_path)
    backend.unknown = True
    state = stages.execute(root, backend)
    assert not state['can_recheck_close']
    with pytest.raises(ComputerError, match='recheck_unavailable'):
        stages.recheck_current_close(root, backend)
    assert len(backend.actions) == 1

def test_parent_drift_refuses_second_stage(tmp_path):
    root, backend, client = setup(tmp_path); advance(root, backend, client, tmp_path)
    done = stages.execute(root, backend); advance(root, backend, client, tmp_path)
    (done['task_root'] / 'foreign.txt').write_text('changed')
    with pytest.raises(ComputerError, match='changed'): stages.load(root)
    with pytest.raises(ComputerError, match='changed'): stages.execute(root, backend)
    assert len(backend.actions) == 1

def test_failed_model_retains_attempt_and_no_published_stage(tmp_path):
    root, backend, client = setup(tmp_path); client.error = BudgetError('no')
    with pytest.raises(ComputerError, match='model_budget_exceeded'): advance(root, backend, client, tmp_path)
    assert stages.load(root)['stage_number'] == 0
    assert len(list((root/'attempts').glob('*.json'))) == 1
    assert len(list((root/'attempts').glob('*.failed'))) == 1
    assert not backend.actions

def test_window_drift_and_cancel_do_not_publish(tmp_path):
    root, backend, client = setup(tmp_path)
    backend.state['window']['pid'] += 1
    with pytest.raises(ComputerError, match='window_changed'): advance(root, backend, client, tmp_path)
    assert not client.calls and stages.load(root)['stage_number'] == 0
    with pytest.raises(ComputerError, match='cancelled'): advance(root, backend, client, tmp_path, stop=lambda:True)

def test_unapproved_external_attempt_rejected(tmp_path):
    root, backend, client = setup(tmp_path); view = advance(root, backend, client, tmp_path)
    with pytest.raises(ComputerError, match='approval_missing'): task.run(view['task_root'], backend)
    assert not backend.actions and backend.observations == 1
    assert stages.load(root)['needs_acceptance']

def test_tampered_stage_and_orphan_not_counted(tmp_path):
    root, backend, client = setup(tmp_path)
    (root/'orphan.json').write_text('{}')
    assert stages.load(root)['stage_number'] == 0
    advance(root, backend, client, tmp_path)
    path = root/'stage-01.json'; row, _ = task._load(path)
    row['remaining_instruction'] = 'forged'
    path.write_text(json.dumps({'payload': row, 'sha256': 'bad'}))
    with pytest.raises(ComputerError): stages.load(root)

def test_prefix_drift_during_model_never_publishes(tmp_path):
    root, backend, client = setup(tmp_path)
    advance(root, backend, client, tmp_path)
    done = stages.execute(root, backend)
    class DriftingClient(Client):
        def complete(self, *args, **kwargs):
            result = super().complete(*args, **kwargs)
            (done['task_root'] / 'foreign.txt').write_text('drift')
            return result
    drift = DriftingClient(); drift.calls = 1
    with pytest.raises(ComputerError, match='changed'):
        advance(root, backend, drift, tmp_path)
    assert not (root/'stage-02.json').exists()
    assert len(backend.actions) == 1


def test_model_cancel_and_invalid_response_leave_no_published_stage(tmp_path):
    root, backend, client = setup(tmp_path)
    stop_state = [False]
    class CancelClient(Client):
        def complete(self, *args, **kwargs):
            result = super().complete(*args, **kwargs)
            stop_state[0] = True
            return result
    with pytest.raises(ComputerError, match='cancelled'):
        advance(root, backend, CancelClient(), tmp_path, stop=lambda: stop_state[0])
    assert stages.load(root)['stage_number'] == 0
    class InvalidClient(Client):
        def complete(self, *args, **kwargs):
            return {'stage_instruction': 'pretend', 'remaining_instruction': '', 'conditions': [], 'extra': True}
    with pytest.raises(ComputerError, match='response_invalid'):
        advance(root, backend, InvalidClient(), tmp_path)
    assert stages.load(root)['stage_number'] == 0 and not backend.actions


def test_changed_title_allowed_but_process_identity_bound(tmp_path):
    root, backend, client = setup(tmp_path)
    backend.state['window']['title'] = 'new directory'
    assert advance(root, backend, client, tmp_path)['needs_acceptance']


def test_completed_target_cannot_recur_after_external_state_reversion(tmp_path):
    root, backend, client = setup(tmp_path)
    advance(root, backend, client, tmp_path)
    stages.execute(root, backend)
    backend.state['controls'][0]['selected'] = False
    observations = len(backend.actions)
    with pytest.raises(ComputerError, match='completed_target_repeated'):
        advance(root, backend, Client(), tmp_path)
    assert len(backend.actions) == observations and stages.load(root)['stage_number'] == 1


def test_schema3_visual_approval_alone_cannot_execute_stage(tmp_path, monkeypatch):
    from test_windows_visual_proposal import WINDOW as visual_window, CANDIDATE, Backend as VisualBackend, observation as visual_observation
    from agent_service import windows_visual_memory as memory, windows_visual_proposal as proposal
    monkeypatch.setattr(memory, 'verify', lambda item: None)
    monkeypatch.setattr(memory, 'discover', lambda windows: [deepcopy(CANDIDATE)])
    class VisualClient:
        def complete(self, system, context, *, max_tokens):
            rows = json.loads(context)['windows'][0]['candidates']
            rows = [row for row in rows if row['type'] == 'VisualNavigation']
            return {'stage_instruction':'查看显示工具', 'remaining_instruction':'',
                    'conditions':[{'id':rows[0]['id'],'state':'navigate','value':True,'retain':False}]}
    root = stages.create('查看显示工具', [visual_observation()], parent=tmp_path)
    backend = VisualBackend()
    view = advance(root, backend, VisualClient(), tmp_path)
    before = backend.observations
    proposal.approve(view['task_root'])
    with pytest.raises(ComputerError, match='approval_missing'):
        task.run(view['task_root'], backend)
    assert backend.observations == before and backend.acts == 0
    assert stages.execute(root, backend)['summary']['state'] == 'verified'


def test_visual_role_semantics_ignore_source_reference(monkeypatch):
    from agent_service import windows_reviewed_controls as controls
    monkeypatch.setattr(controls, 'decode', lambda selector, value: (json.loads(value), {}))
    selector = {'type': controls.TYPE, 'automation_id': controls.ID, 'name': '搜索'}
    first = {'window':WINDOW, 'selector':selector, 'state':'value',
             'value':json.dumps({'role':'search_text','text':'foo','source':'one'})}
    second = deepcopy(first)
    second['value'] = json.dumps({'role':'search_text','text':'foo','source':'two'})
    assert stages._semantic(first) == stages._semantic(second)


def test_unpublished_bound_task_has_no_dispatch_authority(tmp_path):
    root, backend, client = setup(tmp_path)
    goal = task.validate_goal({'schema':1, 'instruction':'select Documents', 'max_steps':12,
        'conditions':[{'id':'target_1','window':deepcopy(WINDOW),
            'selector':{'type':'TreeItem','name':'Documents','automation_id':''},
            'state':'selected','value':True,'retain':False}]})
    child = task.create(tmp_path/'orphans', goal, stage_binding={'root':str(root), 'number':1})
    with pytest.raises(ComputerError, match='approval_missing'):
        task.run(child, backend)
    assert not backend.actions and backend.observations == 0
    assert stages.load(root)['stage_number'] == 0


def test_completed_target_selector_ancestor_change_is_still_repeated(tmp_path):
    root, backend, client = setup(tmp_path)
    advance(root, backend, client, tmp_path); stages.execute(root, backend)
    backend.state['controls'][0]['selected'] = False
    parent = {'type':'Tree','name':'Navigation','automation_id':'tree'}
    backend.state['controls'][0]['ancestors'] = [parent]
    # Force ancestor disambiguation while retaining the same core selector.
    duplicate = control(); duplicate['id'] = 'second'; duplicate['ancestors'] = [{**parent, 'name':'Other'}]
    backend.state['controls'].append(duplicate)
    with pytest.raises(ComputerError, match='completed_target_repeated'):
        advance(root, backend, Client(), tmp_path)
    assert len(backend.actions) == 1 and stages.load(root)['stage_number'] == 1


def test_multiple_conditions_rejected_before_stage_published(tmp_path):
    root, backend, client = setup(tmp_path)
    class MultiClient(Client):
        def complete(self, *args, **kwargs):
            response = super().complete(*args, **kwargs)
            response['conditions'].append({'id':'W1C1', 'state':'expanded', 'value':True, 'retain':False})
            return response
    with pytest.raises(ComputerError, match='response_multiple_targets'):
        advance(root, backend, MultiClient(), tmp_path)
    assert stages.load(root)['stage_number'] == 0 and not backend.actions


def test_uia_and_visual_navigation_are_same_target():
    from agent_service import windows_visual as visual
    generic = {'window':WINDOW, 'selector':{'type':'TabItem','name':'视图','automation_id':'view'},
               'state':'selected', 'value':True}
    graphical = visual.condition(WINDOW, '视图', ['冻结窗格','重排窗口'])
    assert stages._semantic(generic) == stages._semantic(graphical)


@pytest.mark.parametrize('special_kind', ['reviewed', 'form'])
@pytest.mark.parametrize('prior_special', [True, False])
def test_search_reversion_across_uia_and_reviewed_forms_rejected(monkeypatch, special_kind, prior_special):
    from agent_service import windows_reviewed_controls as controls, windows_visual_forms as forms
    generic = {'id':'done', 'window':deepcopy(WINDOW),
        'selector':{'type':'Edit','name':'搜索','automation_id':'search'},
        'state':'value','value':'same text','retain':False}
    if special_kind == 'reviewed':
        monkeypatch.setattr(controls, 'decode', lambda selector, value: ({'role':'search_text','text':'same text'}, {}))
        selector = {'type':controls.TYPE,'automation_id':controls.ID,'name':'搜索'}
    else:
        monkeypatch.setattr(forms, 'decode', lambda selector, value: ({'kind':'search_text','text':'same text'}, {}))
        selector = {'type':forms.TYPE,'automation_id':forms.ID,'name':'查找内容'}
    special = {**deepcopy(generic), 'selector':selector, 'value':'new source reference'}
    previous, next_condition = (special, generic) if prior_special else (generic, special)
    records = [{'scan':{'goal':{'conditions':[previous]}, 'completed':['done']}}]
    with pytest.raises(ComputerError, match='completed_target_repeated'):
        stages._reject_repeated(records, {'conditions':[next_condition]})


def test_response_shape_records_only_fixed_types_and_no_values(tmp_path):
    root, backend, client = setup(tmp_path)
    class PrivateClient(Client):
        def complete(self, *args, **kwargs):
            return {'stage_instruction':'SECRET-REQUEST-VALUE', 'remaining_instruction':'SECRET-REMAINDER',
                    'conditions':[{'id':'SECRET-ID','state':'SECRET-STATE','value':'SECRET-VALUE',
                                   'retain':False,'SECRET-CONDITION-FIELD':'secret'}],
                    'SECRET-EXTRA-FIELD':'secret'}
    with pytest.raises(ComputerError, match='response_invalid'):
        advance(root, backend, PrivateClient(), tmp_path)
    path = next((root/'attempts').glob('*.response-shape'))
    data, _ = task._load(path)
    assert data['type'] == 'object' and data['conditions_count'] == 1
    assert data['fields']['stage_instruction'] == {'present':True,'type':'string'}
    assert data['condition_fields'][0]['fields']['retain'] == {'present':True,'type':'boolean'}
    assert 'SECRET' not in path.read_text(encoding='utf-8')
    assert not backend.actions and stages.load(root)['stage_number'] == 0


def test_zero_conditions_reports_unsupported_even_with_empty_stage_instruction(tmp_path):
    root, backend, client = setup(tmp_path)
    class EmptyClient(Client):
        def complete(self, *args, **kwargs):
            return {'stage_instruction':'', 'remaining_instruction':'unfinished', 'conditions':[]}
    with pytest.raises(ComputerError, match='windows_intent_unsupported_or_ambiguous'):
        advance(root, backend, EmptyClient(), tmp_path)
    shape, _ = task._load(next((root/'attempts').glob('*.response-shape')))
    assert shape['conditions_count'] == 0 and shape['condition_fields'] == []
    assert not backend.actions and stages.load(root)['stage_number'] == 0


def test_response_shape_includes_missing_and_wrong_types_only():
    result = stages._response_shape({'stage_instruction':42, 'conditions':[None, {'id':[], 'value':None}],
                                     'private-custom-field':'sensitive'})
    assert result['fields']['stage_instruction'] == {'present':True, 'type':'number'}
    assert result['fields']['remaining_instruction'] == {'present':False, 'type':None}
    assert result['conditions_count'] == 2
    assert result['condition_fields'][0]['type'] == 'null'
    assert result['condition_fields'][1]['fields']['id']['type'] == 'array'
    assert 'private-custom-field' not in json.dumps(result)


def test_staged_system_distinguishes_schema_and_unsupported():
    assert 'fields outside the stated response schema' in stages.SYSTEM
    assert 'return zero conditions if unsupported or ambiguous' in stages.SYSTEM
    assert 'At most 8' not in stages.SYSTEM
