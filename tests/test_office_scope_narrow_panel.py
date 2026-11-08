"""Local scope narrowing uses verified inputs and rejects stale UI results."""
from copy import deepcopy

import pytest

from agent_service import computer_goal_panel, office_scope_narrow, office_workspace
from test_office_workspace_panel import _Goal, _Thread, _make_panel, _material


def _setup(tmp_path, monkeypatch, draft='  keep exact draft\r\n'):
    records, scope, refs = _material(tmp_path)
    goal = _Goal(files=[item['path'] for item in records], refs=refs,
                 scope=scope, draft=draft)
    goal.stopped.clear()
    goal.proposals = []
    goal.propose = lambda: goal.proposals.append(True)
    panel = _make_panel(goal, tmp_path / 'store', tmp_path, monkeypatch)
    panel.controller.expected_id = 'previous-snapshot'
    calls, dialogs = [], []

    def verify(payload, _base):
        calls.append(deepcopy(payload))
        selected_refs = [deepcopy(ref) for ref in refs
                         if ref['artifact'] in [row['path'] for row in payload['records']]]
        return {'payload': deepcopy(payload), 'refs': selected_refs, 'source_ref': None,
                'review': [{'path': part['path'], 'rows': [
                    {'at': deepcopy(at), 'value': 'duplicate', 'editable': True}
                    for at in part['locations']]} for part in payload['scope']]}

    monkeypatch.setattr(office_workspace, 'verify', verify)
    monkeypatch.setattr(office_scope_narrow, 'open_selector',
                        lambda root, verified, callback: dialogs.append((verified, callback)))
    return panel, goal, calls, dialogs


def _complete(panel):
    _Thread.all[-1].run()
    panel.poll()


def _selection(verified):
    part = deepcopy(verified['payload']['scope'][0])
    return {'files': (part['path'],), 'artifact_refs': [deepcopy(verified['refs'][0])],
            'scope': [part]}


@pytest.mark.parametrize('draft', ['', '  keep exact draft\r\n'])
def test_narrowing_reverifies_subset_retains_draft_and_cas_without_planning_or_saving(
        tmp_path, monkeypatch, draft):
    panel, goal, calls, dialogs = _setup(tmp_path, monkeypatch, draft)
    before = deepcopy(panel.context())
    assert panel.narrow_inputs()
    assert panel.preparation.pending['ticket']['purpose'] == 'scope_narrow'
    assert not dialogs and not calls
    _complete(panel)
    assert calls == [before['payload']]
    assert panel.context() == before
    assert len(dialogs) == 1
    selection = _selection(dialogs[0][0])
    dialogs[0][1](selection)
    assert panel.preparation.pending['ticket']['purpose'] == 'scope_narrow_apply'
    assert goal.office_scope == before['payload']['scope']
    _complete(panel)
    assert len(calls) == 2
    assert calls[1] == {'draft': draft,
                        'records': [deepcopy(before['payload']['records'][0])],
                        'scope': selection['scope']}
    assert goal.office_scope == selection['scope']
    assert goal.office_planning_files == selection['files']
    assert goal.instruction_draft.snapshot() == before['revision']
    assert panel.controller.expected_id == 'previous-snapshot'
    assert not goal.proposals and goal.executions == 0 and goal.plan is None
    assert not (tmp_path / 'store').exists()


def test_cancel_narrowing_modal_preserves_inputs_and_cas(tmp_path, monkeypatch):
    panel, goal, calls, dialogs = _setup(tmp_path, monkeypatch)
    before = deepcopy(panel.context())
    assert panel.narrow_inputs()
    _complete(panel)
    # Closing the selector has no callback, so there is no second verification.
    assert len(dialogs) == 1 and len(calls) == 1
    assert panel.preparation.pending is None
    assert panel.context() == before
    assert panel.controller.expected_id == 'previous-snapshot'
    assert not goal.proposals and goal.executions == 0


def _change(panel, goal, change):
    if change == 'draft_revert':
        original = goal.instruction_draft.text
        goal.instruction_draft.adopt('new draft')
        goal.instruction_draft.adopt(original)
    elif change == 'scope':
        goal.office_scope[0]['locations'] = [{'paragraph_index': 19}]
    elif change == 'refs':
        goal.office_artifact_refs[0]['sha256'] = 'f' * 64
    elif change == 'mode':
        goal.mode.set(computer_goal_panel.MODES[0])
    elif change == 'plan':
        goal.plan = {'new': 'preview'}
    elif change == 'stop':
        goal.stopped.set()
    elif change == 'generation_revert':
        goal.capability_source_generation += 2
    elif change == 'busy':
        goal.running = True
    elif change == 'external_busy':
        panel.controller.busy = lambda: True
    elif change == 'closed':
        goal.closed = True


@pytest.mark.parametrize('change', ['draft_revert', 'scope', 'refs', 'mode', 'plan',
                                    'stop', 'generation_revert', 'busy',
                                    'external_busy', 'closed'])
def test_stale_selector_callback_cannot_replace_current_inputs(tmp_path, monkeypatch, change):
    panel, goal, calls, dialogs = _setup(tmp_path, monkeypatch)
    assert panel.narrow_inputs()
    _complete(panel)
    selection = _selection(dialogs[0][0])
    _change(panel, goal, change)
    before = deepcopy(panel.context())
    dialogs[0][1](selection)
    assert panel.preparation.pending is None
    assert len(calls) == 1 and len(_Thread.all) == 1
    assert panel.context() == before
    assert panel.controller.expected_id == 'previous-snapshot'
    assert not goal.proposals and goal.executions == 0


@pytest.mark.parametrize('change', ['draft_revert', 'scope', 'stop', 'busy', 'closed'])
def test_late_subset_verification_cannot_replace_new_input(tmp_path, monkeypatch, change):
    panel, goal, calls, dialogs = _setup(tmp_path, monkeypatch)
    assert panel.narrow_inputs()
    _complete(panel)
    dialogs[0][1](_selection(dialogs[0][0]))
    _change(panel, goal, change)
    before = deepcopy(panel.context())
    _complete(panel)
    assert panel.context() == before
    assert panel.controller.expected_id == 'previous-snapshot'
    assert not goal.proposals and goal.executions == 0


def test_selector_callback_is_single_use_even_after_worker_finishes(tmp_path, monkeypatch):
    panel, goal, calls, dialogs = _setup(tmp_path, monkeypatch)
    assert panel.narrow_inputs()
    _complete(panel)
    selection = _selection(dialogs[0][0])
    dialogs[0][1](selection)
    dialogs[0][1](selection)
    assert len(_Thread.all) == 2
    _complete(panel)
    after = deepcopy(panel.context())
    dialogs[0][1](selection)
    assert len(calls) == 2 and len(_Thread.all) == 2
    assert panel.context() == after
    assert not goal.proposals and goal.executions == 0


@pytest.mark.parametrize('change', ['scope', 'mode', 'plan', 'busy', 'closed', 'pending'])
def test_invalid_narrowing_start_has_no_worker(tmp_path, monkeypatch, change):
    panel, goal, calls, dialogs = _setup(tmp_path, monkeypatch, '')
    if change == 'scope':
        goal.office_scope = None
    elif change == 'pending':
        panel.controller.pending = True
    else:
        _change(panel, goal, change)
    before = deepcopy(panel.context())
    assert panel.narrow_inputs() is False
    assert not _Thread.all and not calls and not dialogs
    assert panel.context() == before


def test_initial_verification_after_draft_revert_opens_no_selector(tmp_path, monkeypatch):
    panel, goal, calls, dialogs = _setup(tmp_path, monkeypatch)
    assert panel.narrow_inputs()
    _change(panel, goal, 'draft_revert')
    before = deepcopy(panel.context())
    _complete(panel)
    assert not dialogs and panel.context() == before
    assert not goal.proposals and goal.executions == 0


@pytest.mark.parametrize('phase', ['initial', 'subset'])
def test_timeout_discards_late_verification_without_installing_scope(tmp_path, monkeypatch, phase):
    panel, goal, calls, dialogs = _setup(tmp_path, monkeypatch)
    assert panel.narrow_inputs()
    if phase == 'subset':
        _complete(panel)
        dialogs[0][1](_selection(dialogs[0][0]))
    before = deepcopy(panel.context())
    deadline = panel.preparation.pending['deadline']
    panel.preparation.clock = lambda: deadline + 1
    panel.poll()
    assert panel.preparation.pending['discard'] is True
    assert panel.preparation.pending['stop'].is_set()
    _complete(panel)
    assert panel.preparation.pending is None
    assert panel.context() == before
    assert len(dialogs) == (1 if phase == 'subset' else 0)
    assert panel.controller.expected_id == 'previous-snapshot'
    assert not goal.proposals and goal.executions == 0


@pytest.mark.parametrize('phase', ['initial', 'subset'])
def test_stop_discards_late_verification_without_installing_scope(tmp_path, monkeypatch, phase):
    panel, goal, calls, dialogs = _setup(tmp_path, monkeypatch)
    assert panel.narrow_inputs()
    if phase == 'subset':
        _complete(panel)
        dialogs[0][1](_selection(dialogs[0][0]))
    goal.stopped.set()
    before = deepcopy(panel.context())
    panel.poll()
    assert panel.preparation.pending['discard'] is True
    _complete(panel)
    assert panel.context() == before
    assert len(dialogs) == (1 if phase == 'subset' else 0)
    assert panel.controller.expected_id == 'previous-snapshot'
    assert not goal.proposals and goal.executions == 0


def test_subset_verification_failure_preserves_original_scope_and_exact_draft(tmp_path, monkeypatch):
    panel, goal, calls, dialogs = _setup(tmp_path, monkeypatch)
    assert panel.narrow_inputs()
    _complete(panel)
    before = deepcopy(panel.context())
    dialogs[0][1](_selection(dialogs[0][0]))

    def failed_verify(*_args):
        raise office_workspace.WorkspaceVerificationError('changed', 0, 'range')

    monkeypatch.setattr(office_workspace, 'verify', failed_verify)
    _complete(panel)
    assert panel.preparation.pending is None
    assert panel.context() == before
    assert panel.controller.expected_id == 'previous-snapshot'
    assert not goal.proposals and goal.executions == 0
    assert not (tmp_path / 'store').exists()


def test_callback_cannot_widen_the_previously_verified_scope(tmp_path, monkeypatch):
    panel, goal, calls, dialogs = _setup(tmp_path, monkeypatch)
    assert panel.narrow_inputs()
    _complete(panel)
    before = deepcopy(panel.context())
    forged = _selection(dialogs[0][0])
    forged['scope'][0]['locations'].append({'paragraph_index': 999})
    assert dialogs[0][1](forged) is False
    assert panel.preparation.pending is None
    assert len(_Thread.all) == 1 and len(calls) == 1
    assert panel.context() == before
    assert not goal.proposals and goal.executions == 0


def test_narrowing_does_not_restore_a_stale_snapshot_cas_id(tmp_path, monkeypatch):
    panel, goal, calls, dialogs = _setup(tmp_path, monkeypatch)
    assert panel.narrow_inputs()
    _complete(panel)
    # A concurrent successful save can advance the expected snapshot pointer.
    # Narrowing changes no disk snapshot and must retain the current CAS value.
    panel.controller.expected_id = 'newly-saved-snapshot'
    dialogs[0][1](_selection(dialogs[0][0]))
    _complete(panel)
    assert panel.controller.expected_id == 'newly-saved-snapshot'
    assert not goal.proposals and goal.executions == 0
