from copy import deepcopy
import json

import pytest

from agent_service import windows_task as wt
from agent_service.computer import ComputerError, digest


WINDOW = {'hwnd': 100, 'pid': 200, 'created': 'process-start', 'image': 'C:/Windows/explorer.exe',
          'class_name': 'CabinetWClass', 'title': 'Synthetic files', 'profile': 'explorer'}


def control(name='Documents', **changes):
    return {'id': name.lower(), 'runtime_id': [1, 2], 'type': 'TreeItem', 'name': name,
            'automation_id': '', 'patterns': ['select', 'expand', 'collapse', 'focus'],
            'enabled': True, 'visible': True, 'selected': False, 'expanded': 0,
            'focused': False, 'value': None, **changes}


def condition(name='Documents', state='selected', value=True, retain=True):
    return {'id': name.lower(), 'window': deepcopy(WINDOW),
            'selector': {'type': 'TreeItem', 'name': name, 'automation_id': ''},
            'state': state, 'value': value, 'retain': retain}


def goal(*conditions, max_steps=12):
    return {'schema': 1, 'instruction': 'Select synthetic navigation controls',
            'conditions': list(conditions or [condition()]), 'max_steps': max_steps}


class Backend:
    def __init__(self, *controls):
        self.controls = list(deepcopy(controls or [control()]))
        self.calls = []
        self.observations = 0
        self.blocked = False
        self.candidate_override = None
        self.result = None
        self.on_observe = None
        self.on_act = None

    def observe(self, window, *, stop):
        self.observations += 1
        if self.on_observe:
            self.on_observe(self)
        return {'window': deepcopy(window), 'controls': deepcopy(self.controls),
                'truncated': False, **({'blocked_reason': 'synthetic modal'} if self.blocked else {})}

    def candidates(self, observation, target):
        if self.candidate_override is not None:
            return deepcopy(self.candidate_override)
        found = wt.locate(observation, target['selector'])
        if found is None:
            return []
        action = {'selected': 'select', 'expanded': 'expand', 'collapsed': 'collapse',
                  'focused': 'focus', 'value': 'set_value'}[target['state']]
        return [{'control_id': found['id'], 'action': action,
                 'value': target['value'] if action == 'set_value' else ''}]

    def act(self, window, observed_control, action, value, before_sha, operation_root, *, stop):
        self.calls.append((observed_control, action, value))
        assert before_sha == digest(self.observe(window, stop=stop))
        if self.on_act:
            return self.on_act(self, operation_root)
        if self.result is not None:
            return deepcopy(self.result)
        actual = next(c for c in self.controls if c['id'] == observed_control['id'])
        actual[{'select': 'selected', 'expand': 'expanded', 'collapse': 'expanded',
                'focus': 'focused', 'set_value': 'value'}[action]] = {
                    'select': True, 'expand': 1, 'collapse': 0, 'focus': True, 'set_value': value}[action]
        (operation_root / 'receipt.json').write_text('{"synthetic":true}', encoding='utf-8')
        return {'ok': True, 'state': 'verified', 'effects': 'verified'}


@pytest.fixture
def root(tmp_path):
    return wt.create(tmp_path, goal())


def test_provider_binding_enforced_before_any_observation(tmp_path):
    from agent_service.windows_winapp import PROVIDER_BINDING
    from agent_service.windows_access import backend_for_task, WinappBackend, WindowsBackend
    bound = wt.create(tmp_path, goal(), provider_binding=PROVIDER_BINDING)
    backend = Backend()
    with pytest.raises(ComputerError, match='windows_provider_binding_mismatch'):
        wt.run(bound, backend)
    assert backend.observations == 0 and backend.calls == []
    assert isinstance(backend_for_task(bound), WinappBackend)
    legacy = wt.create(tmp_path, goal())
    assert type(backend_for_task(legacy)) is WindowsBackend
    backend.provider_binding = PROVIDER_BINDING
    with pytest.raises(ComputerError, match='windows_provider_binding_mismatch'):
        wt.run(legacy, backend)
    assert wt.load(bound)['provider_binding'] == PROVIDER_BINDING


def test_bound_provider_interruption_is_unknown_and_never_replayed(tmp_path):
    from agent_service.windows_winapp import PROVIDER_BINDING
    root = wt.create(tmp_path, goal(), provider_binding=PROVIDER_BINDING)
    backend = Backend()
    backend.provider_binding = PROVIDER_BINDING
    def interrupted(*args):
        raise RuntimeError('synthetic interruption after dispatch')
    backend.on_act = interrupted
    result = wt.run(root, backend)
    assert result['state'] == 'unknown' and len(backend.calls) == 1
    with pytest.raises(ComputerError, match='windows_unknown_requires_review'):
        wt.run(root, backend)
    assert len(backend.calls) == 1


def test_navigation_is_observed_before_and_after_and_never_repeated(root):
    backend = Backend()
    result = wt.run(root, backend)
    assert result['ok'] and result['completed'] == ['documents']
    assert backend.observations >= 4 and len(backend.calls) == 1
    before = (root / 'attempts' / '0001' / 'outcome.json').read_bytes()
    assert wt.run(root, backend)['ok']
    assert len(backend.calls) == 1
    assert (root / 'attempts' / '0001' / 'outcome.json').read_bytes() == before


@pytest.mark.parametrize('state,value,initial', [('expanded', 1, 0), ('collapsed', 0, 1),
                                               ('focused', True, False), ('value', 'new', 'old')])
def test_condition_states_use_independent_readback(tmp_path, state, value, initial):
    target = condition(state=state, value=value, retain=state != 'focused')
    key = 'expanded' if state in {'expanded', 'collapsed'} else state
    backend = Backend(control(**{key: initial}))
    root = wt.create(tmp_path, goal(target))
    assert wt.run(root, backend)['ok']
    assert len(backend.calls) == 1


def test_unique_semantic_target_survives_new_runtime_id(root):
    backend = Backend(control(id='new-runtime', runtime_id=[7, 8]))
    assert wt.run(root, backend)['ok']
    assert backend.calls[0][0]['id'] == 'new-runtime'


def test_ancestor_disambiguates_identical_names(tmp_path):
    target = condition()
    target['selector']['ancestor'] = {'type': 'Tree', 'name': 'Navigation', 'automation_id': 'nav'}
    backend = Backend(control(id='first', ancestors=[target['selector']['ancestor']]),
                      control(id='second', ancestors=[{'type': 'Tree', 'name': 'Other', 'automation_id': ''}]))
    assert wt.run(wt.create(tmp_path, goal(target)), backend)['ok']
    assert backend.calls[0][0]['id'] == 'first'


def test_ambiguous_target_refused_before_intent(root):
    backend = Backend(control(id='a'), control(id='b'))
    result = wt.run(root, backend)
    assert result['code'] == 'windows_target_ambiguous' and not backend.calls
    assert all(json.loads(p.read_text())['payload']['kind'] != 'intent'
               for p in (root / 'attempts' / '0001' / 'events').iterdir())


@pytest.mark.parametrize('candidates,code', [([], 'windows_no_safe_action'),
    ([{'control_id': 'missing', 'action': 'select', 'value': ''}], 'windows_action_unobserved'),
    ([{'control_id': 'documents', 'action': 'select', 'value': ''}] * 2, 'windows_action_ambiguous')])
def test_missing_or_ambiguous_candidates_never_dispatch(root, candidates, code):
    backend = Backend()
    backend.candidate_override = candidates
    assert wt.run(root, backend)['code'] == code
    assert not backend.calls


def test_blocked_application_does_not_read_or_dispatch_other_steps(root):
    backend = Backend()
    backend.blocked = True
    assert wt.run(root, backend)['code'] == 'windows_application_blocked'
    assert not backend.calls
    backend.blocked = False
    assert wt.run(root, backend)['ok']


def test_changed_observation_is_refreshed_before_dispatch(root):
    backend = Backend()
    def change_once(b):
        if b.observations == 2:
            b.controls[0]['runtime_id'] = [4, 5]
            b.controls[0]['id'] = 'replacement'
    backend.on_observe = change_once
    assert wt.run(root, backend)['ok']
    assert len(backend.calls) == 1 and backend.calls[0][0]['id'] == 'replacement'


def test_unstable_observation_stops_after_two_refreshes(root):
    backend = Backend()
    backend.on_observe = lambda b: b.controls[0].update(runtime_id=[b.observations])
    result = wt.run(root, backend)
    assert result['code'] == 'windows_reobservation_limit'
    assert backend.observations == 6 and not backend.calls


def test_pause_restart_skips_completed_and_preserves_receipt(tmp_path):
    root = wt.create(tmp_path, goal(condition(), condition('Downloads')))
    backend = Backend(control(), control('Downloads'))
    first = wt.run(root, backend, pause_after_step=True)
    assert first['state'] == 'paused' and first['completed'] == ['documents']
    evidence = {str(p): p.read_bytes() for p in (root / 'attempts' / '0001').rglob('*') if p.is_file()}
    assert wt.run(root, backend)['ok']
    assert [call[0]['name'] for call in backend.calls] == ['Documents', 'Downloads']
    assert all(__import__('pathlib').Path(path).read_bytes() == value for path, value in evidence.items())


def test_retained_completed_state_change_blocks_resume(tmp_path):
    root = wt.create(tmp_path, goal(condition(), condition('Downloads')))
    backend = Backend(control(), control('Downloads'))
    wt.run(root, backend, pause_after_step=True)
    backend.controls[0]['selected'] = False
    result = wt.run(root, backend)
    assert result['code'] == 'windows_completed_state_changed'
    assert len(backend.calls) == 1


def test_focus_is_not_retained_across_resume(tmp_path):
    root = wt.create(tmp_path, goal(condition(state='focused', retain=False), condition('Downloads')))
    backend = Backend(control(), control('Downloads'))
    wt.run(root, backend, pause_after_step=True)
    backend.controls[0]['focused'] = False
    assert wt.run(root, backend)['ok']
    assert len(backend.calls) == 2


def test_successful_native_call_without_condition_does_not_claim_success(root):
    backend = Backend()
    backend.result = {'ok': True, 'state': 'verified', 'effects': 'verified'}
    result = wt.run(root, backend)
    assert result['state'] == 'unknown' and result['cause_code'] == 'windows_postcondition_changed'
    assert len(backend.calls) == 1
    with pytest.raises(ComputerError,match='unknown_requires_review'):
        wt.run(root,backend)
    assert len(backend.calls) == 1


@pytest.mark.parametrize('result', [
    {'ok': False, 'state': 'unknown', 'effects': 'unknown'},
    {'ok': True, 'state': 'verified', 'effects': 'none'},
    {'ok': False, 'state': 'failed', 'effects': 'observed'},
])
def test_unknown_or_invalid_native_result_is_never_replayed(root, result):
    backend = Backend()
    backend.result = result
    assert wt.run(root, backend)['state'] == 'unknown'
    assert not wt.inspect(root)['resumable']
    with pytest.raises(ComputerError, match='windows_unknown_requires_review'):
        wt.run(root, backend)
    assert len(backend.calls) == 1


def test_crash_after_intent_is_unknown_even_without_outcome(root):
    backend = Backend()
    def crash(*args):
        raise KeyboardInterrupt()
    backend.on_act = crash
    with pytest.raises(KeyboardInterrupt):
        wt.run(root, backend)
    assert wt.inspect(root)['state'] == 'unknown'
    with pytest.raises(ComputerError, match='windows_unknown_requires_review'):
        wt.run(root, backend)


def test_crash_before_intent_can_resume(root):
    backend = Backend()
    def crash(*args):
        raise KeyboardInterrupt()
    backend.on_observe = crash
    with pytest.raises(KeyboardInterrupt):
        wt.run(root, backend)
    assert wt.inspect(root)['resumable']
    backend.on_observe = None
    assert wt.run(root, backend)['ok']


def test_backend_explicit_no_effect_stale_can_refresh(root):
    backend = Backend()
    def stale_once(b, operation):
        b.on_act = None
        return {'ok': False, 'state': 'failed', 'effects': 'none', 'code': 'screen_changed_reobserve'}
    backend.on_act = stale_once
    assert wt.run(root, backend)['ok']
    assert len(backend.calls) == 2


def test_native_receipt_tamper_blocks_inspect_and_resume(root):
    backend = Backend()
    wt.run(root, backend)
    (root / 'attempts' / '0001' / 'operations' / '0001' / 'receipt.json').write_text('changed')
    with pytest.raises(ComputerError, match='windows_evidence_changed'):
        wt.inspect(root)
    with pytest.raises(ComputerError, match='windows_evidence_changed'):
        wt.run(root, backend)
    assert len(backend.calls) == 1


def test_final_retained_condition_is_checked_again(tmp_path):
    root = wt.create(tmp_path, goal(condition(), condition('Downloads')))
    backend = Backend(control(), control('Downloads'))
    def clear_first(b):
        if b.controls[1]['selected']:
            b.controls[0]['selected'] = False
    backend.on_observe = clear_first
    result = wt.run(root, backend)
    assert result['code'] == 'windows_final_state_changed' and not result['ok']


def test_active_task_cannot_be_inspected_or_resumed(root):
    with wt._own(root):
        with pytest.raises(ComputerError, match='windows_task_active'):
            wt.inspect(root)
        with pytest.raises(ComputerError, match='windows_task_active'):
            wt.run(root, Backend())


def test_cancel_before_effect_can_resume(root):
    backend = Backend()
    assert wt.run(root, backend, stop=lambda: True)['state'] == 'paused'
    assert not backend.calls
    assert wt.run(root, backend)['ok']


def test_each_explicit_run_starts_new_input_baseline_once(root):
    backend = Backend()
    starts = []
    backend.begin_run = lambda: starts.append('approved')
    assert wt.run(root, backend)['ok']
    assert starts == ['approved']
    assert wt.run(root, backend)['ok']
    assert starts == ['approved', 'approved']


def test_end_run_releases_input_baseline_on_success_and_interruption(root):
    backend = Backend()
    ends = []
    backend.end_run = lambda: ends.append('end')
    assert wt.run(root, backend)['ok']
    assert ends == ['end']
    def crash(*args):
        raise KeyboardInterrupt()
    backend.on_observe = crash
    with pytest.raises(KeyboardInterrupt):
        wt.run(root, backend)
    assert ends == ['end', 'end']


def test_cleanup_failure_does_not_replace_verified_receipt(root):
    backend = Backend()
    def fail_cleanup():
        raise RuntimeError('synthetic cleanup failure')
    backend.end_run = fail_cleanup
    result = wt.run(root, backend)
    assert result['ok']
    assert wt.inspect(root)['ok']


def test_verified_task_is_not_advertised_as_resumable(root):
    assert wt.run(root, Backend())['ok']
    result = wt.inspect(root)
    assert not result['eligible'] and not result['resumable']


def test_explicit_no_effect_failure_preserves_safe_cause_code(root):
    backend = Backend()
    backend.result = {'ok': False, 'state': 'failed', 'effects': 'none', 'code': 'foreground_permission_denied'}
    result = wt.run(root, backend)
    assert result['code'] == 'windows_action_failed'
    assert result['cause_code'] == 'foreground_permission_denied'
    assert wt.inspect(root)['cause_code'] == 'foreground_permission_denied'


@pytest.mark.parametrize('mutate', [
    lambda g: g.update(schema=True),
    lambda g: g.update(max_steps=13),
    lambda g: g['conditions'][0].update(retain='yes'),
    lambda g: g['conditions'][0].update(state='invoke'),
    lambda g: g['conditions'][0].update(state='focused', retain=True),
    lambda g: g['conditions'][0]['selector'].update(name='', automation_id=''),
])
def test_invalid_goal_rejected_without_creating_task(tmp_path, mutate):
    value = goal()
    mutate(value)
    with pytest.raises(ComputerError):
        wt.create(tmp_path, value)
    assert list(tmp_path.iterdir()) == []


class TargetBackend(Backend):
    """A read-only exact offscreen lookup plus a separately receipted reveal."""
    def observe_target(self, window, selector, *, stop):
        self.selector = deepcopy(selector)
        observed = self.observe(window, stop=stop)
        observed['controls'] = [c for c in observed['controls'] if c['visible']
                                or all(c[k] == selector[k] for k in ('type', 'name', 'automation_id'))]
        return observed

    def candidates(self, observation, target):
        found = wt.locate(observation, target['selector'])
        if self.candidate_override is None and found is not None and not found['visible']:
            return [{'control_id': found['id'], 'action': 'reveal', 'value': ''}]
        return super().candidates(observation, target)

    def act(self, window, observed_control, action, value, before_sha, operation_root, *, stop):
        self.calls.append((deepcopy(observed_control), action, value))
        assert before_sha == digest(self.observe_target(window, self.selector, stop=stop))
        # The core must persist intent before even a viewport-only effect.
        events = operation_root.parent.parent / 'events'
        latest = json.loads(sorted(events.iterdir())[-1].read_text())['payload']
        assert latest['kind'] == 'intent' and latest['candidate']['action'] == action
        if self.on_act:
            return self.on_act(self, operation_root)
        if self.result is not None:
            return deepcopy(self.result)
        actual = next(c for c in self.controls if c['id'] == observed_control['id'])
        if action == 'reveal':
            actual['visible'] = True
        else:
            actual[{'select': 'selected', 'expand': 'expanded', 'collapse': 'expanded',
                    'focus': 'focused', 'set_value': 'value'}[action]] = {
                        'select': True, 'expand': 1, 'collapse': 0, 'focus': True, 'set_value': value}[action]
        (operation_root / 'receipt.json').write_text('{"synthetic":true}', encoding='utf-8')
        return {'ok': True, 'state': 'verified', 'effects': 'verified'}


def test_offscreen_target_reveals_before_business_action(root):
    backend = TargetBackend(control(visible=False), control('Unrelated secret', visible=False))
    assert wt.run(root, backend)['ok']
    assert [c[1] for c in backend.calls] == ['reveal', 'select']
    assert wt.inspect(root)['completed'] == ['documents']
    observations = [json.loads(p.read_text())['payload'] for p in
                    (root / 'attempts' / '0001' / 'events').iterdir()]
    assert all(all(c['name'] != 'Unrelated secret' for c in e['observation']['controls'])
               for e in observations if e['kind'] == 'observation')


def test_resume_reveals_completed_target_without_repeating_business_action(tmp_path):
    root = wt.create(tmp_path, goal(condition(), condition('Downloads')))
    backend = TargetBackend(control(), control('Downloads'))
    assert wt.run(root, backend, pause_after_step=True)['completed'] == ['documents']
    evidence = {p: p.read_bytes() for p in (root / 'attempts' / '0001').rglob('*') if p.is_file()}
    backend.controls[0]['visible'] = False
    assert wt.run(root, backend)['ok']
    assert [(c[0]['name'], c[1]) for c in backend.calls] == [
        ('Documents', 'select'), ('Documents', 'reveal'), ('Downloads', 'select')]
    assert all(p.read_bytes() == contents for p, contents in evidence.items())
    events = [json.loads(p.read_text())['payload'] for p in
              (root / 'attempts' / '0002' / 'events').iterdir()]
    reveal = next(e for e in events if e['kind'] == 'intent')
    assert reveal['purpose'] == 'recheck' and reveal['condition_id'] == 'documents'
    assert [e['condition_id'] for e in events if e['kind'] == 'complete'] == ['downloads']


def test_final_reveals_retained_target_without_repeating_completion(tmp_path):
    root = wt.create(tmp_path, goal(condition(), condition('Downloads')))
    backend = TargetBackend(control(), control('Downloads'))
    hidden = []
    def hide_once(b):
        if b.controls[1]['selected'] and not hidden:
            hidden.append(True)
            b.controls[0]['visible'] = False
    backend.on_observe = hide_once
    assert wt.run(root, backend)['ok']
    assert [c[1] for c in backend.calls] == ['select', 'select', 'reveal']
    assert wt.inspect(root)['completed'] == ['documents', 'downloads']


@pytest.mark.parametrize('visible', [False, True])
def test_changed_completed_state_stops_before_reveal_or_business(tmp_path, visible):
    root = wt.create(tmp_path, goal(condition(), condition('Downloads')))
    backend = TargetBackend(control(), control('Downloads'))
    wt.run(root, backend, pause_after_step=True)
    backend.controls[0].update(visible=visible, selected=False)
    assert wt.run(root, backend)['code'] == 'windows_completed_state_changed'
    assert [c[1] for c in backend.calls] == ['select']


def test_reveal_cannot_repair_changed_completed_state(tmp_path):
    root = wt.create(tmp_path, goal(condition(), condition('Downloads')))
    backend = TargetBackend(control(), control('Downloads'))
    wt.run(root, backend, pause_after_step=True)
    backend.controls[0]['visible'] = False
    def reveal_and_change(b, operation):
        b.controls[0].update(visible=True, selected=False)
        return {'ok': True, 'state': 'verified', 'effects': 'verified'}
    backend.on_act = reveal_and_change
    assert wt.run(root, backend)['code'] == 'windows_completed_state_changed'
    assert [c[1] for c in backend.calls] == ['select', 'reveal']


def test_completed_offscreen_backend_business_candidate_is_refused(tmp_path):
    root = wt.create(tmp_path, goal(condition(), condition('Downloads')))
    backend = TargetBackend(control(), control('Downloads'))
    wt.run(root, backend, pause_after_step=True)
    backend.controls[0]['visible'] = False
    backend.candidate_override = [{'control_id': 'documents', 'action': 'select', 'value': ''}]
    assert wt.run(root, backend)['code'] == 'windows_completed_action_refused'
    assert len(backend.calls) == 1


def test_reveal_without_visible_readback_stops_after_one_call(root):
    backend = TargetBackend(control(visible=False))
    backend.result = {'ok': True, 'state': 'verified', 'effects': 'verified'}
    assert wt.run(root, backend)['code'] == 'windows_reveal_not_visible'
    assert [c[1] for c in backend.calls] == ['reveal']


def test_unknown_reveal_is_never_replayed(root):
    backend = TargetBackend(control(visible=False))
    backend.result = {'ok': False, 'state': 'unknown', 'effects': 'unknown'}
    assert wt.run(root, backend)['state'] == 'unknown'
    with pytest.raises(ComputerError, match='windows_unknown_requires_review'):
        wt.run(root, backend)
    assert [c[1] for c in backend.calls] == ['reveal']


def test_reveal_crash_after_intent_stays_unknown(root):
    backend = TargetBackend(control(visible=False))
    def crash(*args):
        raise KeyboardInterrupt()
    backend.on_act = crash
    with pytest.raises(KeyboardInterrupt):
        wt.run(root, backend)
    assert wt.inspect(root)['state'] == 'unknown'
    with pytest.raises(ComputerError, match='windows_unknown_requires_review'):
        wt.run(root, backend)


def test_completed_reveal_uses_same_persistent_action_limit(tmp_path):
    root = wt.create(tmp_path, goal(condition(), condition('Downloads'), max_steps=1))
    backend = TargetBackend(control(), control('Downloads'))
    wt.run(root, backend, pause_after_step=True)
    backend.controls[0]['visible'] = False
    assert wt.run(root, backend)['code'] == 'windows_action_limit'
    assert len(backend.calls) == 1


def test_reveal_explicit_no_effect_refresh_is_bounded(root):
    backend = TargetBackend(control(visible=False))
    backend.result = {'ok': False, 'state': 'failed', 'effects': 'none', 'code': 'windows_observation_changed'}
    assert wt.run(root, backend)['code'] == 'windows_reobservation_limit'
    assert [c[1] for c in backend.calls] == ['reveal'] * 3


def test_legacy_backend_keeps_completed_offscreen_failure(tmp_path):
    root = wt.create(tmp_path, goal(condition(), condition('Downloads')))
    backend = Backend(control(), control('Downloads'))
    wt.run(root, backend, pause_after_step=True)
    backend.controls[0]['visible'] = False
    assert wt.run(root, backend)['code'] == 'windows_completed_state_changed'
    assert len(backend.calls) == 1


def test_completed_reveal_unknown_preserves_completed_and_blocks_restart(tmp_path):
    root = wt.create(tmp_path, goal(condition(), condition('Downloads')))
    backend = TargetBackend(control(), control('Downloads'))
    wt.run(root, backend, pause_after_step=True)
    backend.controls[0]['visible'] = False
    backend.result = {'ok': False, 'state': 'unknown', 'effects': 'unknown'}
    result = wt.run(root, backend)
    assert result['state'] == 'unknown' and result['completed'] == ['documents']
    with pytest.raises(ComputerError, match='windows_unknown_requires_review'):
        wt.run(root, backend)
    assert [c[1] for c in backend.calls] == ['select', 'reveal']


def test_cancel_after_verified_reveal_resumes_without_repeating_it(tmp_path):
    root = wt.create(tmp_path, goal(condition(), condition('Downloads')))
    backend = TargetBackend(control(), control('Downloads'))
    wt.run(root, backend, pause_after_step=True)
    backend.controls[0]['visible'] = False
    cancelled = []
    def reveal_and_cancel(b, operation):
        b.controls[0]['visible'] = True
        cancelled.append(True)
        return {'ok': True, 'state': 'verified', 'effects': 'verified'}
    backend.on_act = reveal_and_cancel
    result = wt.run(root, backend, stop=lambda: bool(cancelled))
    assert result['state'] == 'paused' and result['completed'] == ['documents']
    backend.on_act = None
    assert wt.run(root, backend)['ok']
    assert [c[1] for c in backend.calls] == ['select', 'reveal', 'select']


def test_offscreen_business_action_without_reveal_is_refused(root):
    backend = TargetBackend(control(visible=False))
    backend.candidate_override = [{'control_id': 'documents', 'action': 'select', 'value': ''}]
    assert wt.run(root, backend)['code'] == 'windows_action_unobserved'
    assert not backend.calls


def test_ambiguous_offscreen_target_is_never_revealed(root):
    backend = TargetBackend(control(id='first', visible=False), control(id='second', visible=False))
    assert wt.run(root, backend)['code'] == 'windows_target_ambiguous'
    assert not backend.calls


def test_condition_observer_has_priority_and_receives_isolated_exact_condition(tmp_path):
    target = condition(state='value', value='{"path":"approved.xlsx","sha256":"pinned"}')
    backend = Backend(control(value=target['value']))
    received = []
    stop = lambda: False
    def observe_condition(given, *, stop):
        assert stop is stop_callback
        received.append(deepcopy(given))
        given['window']['title'] = 'mutated backend copy'
        given['selector']['name'] = 'mutated backend copy'
        given['value'] = 'mutated backend copy'
        return {'window': deepcopy(target['window']), 'controls': deepcopy(backend.controls)}
    def unexpected(*args, **kwargs):
        raise AssertionError('condition-aware backend must not fall back to broader observation')
    stop_callback = stop
    backend.observe_condition = observe_condition
    backend.observe_target = unexpected
    backend.observe = unexpected
    task = wt.create(tmp_path, goal(target))
    assert wt.run(task, backend, stop=stop)['ok']
    assert received == [target, target]
    assert wt.load_goal(task)['conditions'] == [target]
    assert not backend.calls


def test_condition_observer_failure_does_not_fall_back(root):
    backend = Backend(control(selected=True))
    def denied(condition, *, stop):
        raise ComputerError('approved_file_changed')
    backend.observe_condition = denied
    assert wt.run(root, backend)['code'] == 'approved_file_changed'
    assert backend.observations == 0 and not backend.calls


@pytest.mark.parametrize('targeted', [False, True])
def test_observer_fallback_remains_compatible_without_condition_interface(root, targeted):
    backend = Backend(control(selected=True))
    calls = []
    def observe(window, *, stop):
        calls.append('window')
        return {'window': deepcopy(window), 'controls': deepcopy(backend.controls)}
    def observe_target(window, selector, *, stop):
        calls.append('target')
        assert selector == condition()['selector']
        return {'window': deepcopy(window), 'controls': deepcopy(backend.controls)}
    backend.observe_condition = None
    backend.observe = observe
    backend.observe_target = observe_target if targeted else None
    assert wt.run(root, backend)['ok']
    assert calls == ['target' if targeted else 'window'] * 2
