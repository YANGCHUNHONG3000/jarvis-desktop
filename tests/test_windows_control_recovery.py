"""Offline bounded Cancel proof; none of these tests access desktop or cloud."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_service.computer import ComputerError, digest, write_new
from agent_service import windows_task as task, windows_reviewed_controls as reviewed
from agent_service import windows_visual_proposal as proposal, windows_control_recovery as recovery

WINDOW = {'hwnd': 100, 'pid': 200, 'created': 'offline', 'image': 'C:/wps.exe',
          'class_name': 'XLMAIN', 'title': 'offline workbook', 'profile': 'observe_only'}
PREFIX = {'id': 'first', 'window': WINDOW, 'selector': {'type': 'TreeItem', 'name': 'items', 'automation_id': 'i'},
          'state': 'selected', 'value': True, 'retain': False}


@pytest.fixture
def context(tmp_path, monkeypatch, request):
    # Native-format source receipts still cross the production source verifier;
    # only native platform facts are replaced in this offline fixture.
    monkeypatch.setattr(reviewed, 'native', lambda kind: SimpleNamespace(validate=lambda s: s))
    source = {'window': WINDOW, 'application': 'wps', 'kind': 'open_dialog',
              'binding': {'scope': 'dialog_controls', 'dialog': {'hwnd': 300}},
              'controls': [{'role': 'cancel', 'label': '取消'}]}
    variant = getattr(request, 'param', None)
    if variant == 'document':
        source['binding'] = {'document': {'path': 'C:/old.xlsx'}, 'dialog': {'hwnd': 300}}
    elif variant == 'other_control':
        source.update(kind='explorer_view', application='explorer', controls=[{'role': 'preview_pane', 'label': 'Preview'}])
    folder = tmp_path / 'source-native'; folder.mkdir()
    write_new(folder / 'native-request.json', {'op': 'reviewed_snapshot', 'window': WINDOW, 'kind': source['kind']})
    write_new(folder / 'native-outcome.json', {'ok': True, 'snapshot': source})
    source['native_proof'] = {'root': str(folder), **{
        key: sha256((folder / name).read_bytes()).hexdigest() for key, name in
        [('request_sha256', 'native-request.json'), ('outcome_sha256', 'native-outcome.json')]}}
    options = reviewed.candidates_for(source, parent=tmp_path / 'sources')
    condition = reviewed.condition(options[0], number=2)
    return source, options, condition


def observation(condition, presence='destroyed'):
    control = {'id': digest({'selector': condition['selector'],
                            'source': json.loads(condition['value'])['source']})[:24],
               **condition['selector'], 'visible': True, 'enabled': True,
               'value': condition['value'] if presence == 'destroyed' else '', 'patterns': [reviewed.ACTION]}
    return {'window': deepcopy(WINDOW), 'application': 'wps', 'controls': [control], 'dialogs': [],
            'truncated': False, 'scope': 'reviewed_control',
            'reviewed': {'selector': deepcopy(condition['selector']), 'value': condition['value'],
                         'verified': presence == 'destroyed',
                         'facts': {'dialog_presence': presence, 'binding_scope': 'dialog_controls',
                                   'document_state': 'not_claimed', 'control': None, 'parent': None, 'field_value': None}}}


class OriginalBackend:
    candidates = staticmethod(reviewed.actions)
    def __init__(self, condition, code='window_unavailable', missing=None, mutate=None):
        self.condition, self.code, self.missing, self.mutate = condition, code, missing, mutate

    def observe_condition(self, condition, *, stop):
        if condition['id'] == 'first':
            return {'window': WINDOW, 'controls': [{'id': 'i', **PREFIX['selector'],
                    'visible': True, 'enabled': True, 'selected': True}], 'truncated': False}
        return observation(condition, 'visible')

    def act(self, window, control, action, value, before_sha, operation_root, *, stop):
        result = {'ok': False, 'state': 'unknown', 'effects': 'unknown', 'code': self.code}
        request = {'op': 'act', 'window': window, 'control': control, 'action': action,
                   'value': value, 'before_sha': before_sha, 'selector': self.condition['selector'],
                   'document_token': value, 'input_epoch': 12}
        data = json.loads(value)
        records = {'native-request.json': request,
                   'native-intent.json': {'request_sha': digest(request), 'source': data['source'], 'role': data['role']},
                   'native-outcome.json': {**result, 'input_epoch': 12}}
        if self.mutate:
            self.mutate(records)
        for name, payload in records.items():
            if name != self.missing:
                write_new(operation_root / name, payload)
        return result


def unknown(tmp_path, context, *, code='window_unavailable', missing=None, prefix=True, trailing=False, mutate=None):
    _, options, condition = context
    conditions = ([deepcopy(PREFIX)] if prefix else []) + [deepcopy(condition)]
    if trailing:
        conditions.append({**deepcopy(PREFIX), 'id': 'last'})
    goal = {'schema': 1, 'instruction': 'offline cancel', 'conditions': conditions, 'max_steps': 4}
    ref = proposal.save(goal, options, [], parent=tmp_path / 'proposals')
    root = task.create(tmp_path / 'tasks', goal, visual_proposal=ref)
    proposal.approve(root)
    result = task.run(root, OriginalBackend(condition, code, missing, mutate))
    assert result['state'] == 'unknown'
    return root


class ReadOnly:
    def __init__(self, condition, presence='destroyed', change=None):
        self.value, self.change, self.calls = observation(condition, presence), change, []
    def observe_condition(self, condition, *, stop):
        self.calls.append(deepcopy(condition))
        if self.change:
            self.change()
        return deepcopy(self.value)
    def act(self, *a, **kw):
        pytest.fail('readback dispatched input')
    candidates = begin_run = end_run = act


def snapshot(root):
    return {p.relative_to(root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in root.rglob('*') if p.is_file()}


def test_zero_action_readback_retains_original_unknown_and_every_byte(tmp_path, context):
    root = unknown(tmp_path, context); before = snapshot(root)
    backend = ReadOnly(context[2])
    result = recovery.run(root, backend, parent=tmp_path / 'rechecks')
    assert result['ok'] and result['actions'] == [] and result['original_state'] == 'unknown'
    assert result['source']['completed'] == ['first']
    assert result['resumable'] is False and result['fresh_permission'] is False
    assert backend.calls == [context[2]] and snapshot(root) == before
    assert recovery.load(result['root'])['historical'] is True
    assert task.inspect(root)['state'] == 'unknown'
    with pytest.raises(ComputerError, match='unknown_requires_review'):
        task.run(root, OriginalBackend(context[2]))
    with pytest.raises(ComputerError, match='production_proof_required'):
        recovery.verified_window(result['root'])
    assert recovery.latest(root, parent=tmp_path / 'rechecks') is None


@pytest.mark.parametrize('code', ['user_takeover', 'windows_worker_interrupted', 'windows_reviewed_completion_invalid'])
def test_other_unknown_causes_rejected_without_observation(tmp_path, context, code):
    root = unknown(tmp_path, context, code=code); backend = ReadOnly(context[2])
    with pytest.raises(ComputerError, match='ineligible'):
        recovery.run(root, backend, parent=tmp_path / 'rechecks')
    assert not backend.calls and not (tmp_path / 'rechecks').exists()


@pytest.mark.parametrize('missing', ['native-request.json', 'native-intent.json', 'native-outcome.json'])
def test_missing_native_unknown_binding_rejected(tmp_path, context, missing):
    root = unknown(tmp_path, context, missing=missing)
    with pytest.raises(ComputerError, match='ineligible'):
        recovery.inspect(root)


def test_unstarted_later_condition_is_ineligible(tmp_path, context):
    with pytest.raises(ComputerError, match='ineligible'):
        recovery.inspect(unknown(tmp_path, context, trailing=True))


def test_missing_approval_refused(tmp_path, context):
    root = unknown(tmp_path, context)
    (root / 'visual-approval.json').unlink()
    with pytest.raises(ComputerError, match='ineligible'):
        recovery.inspect(root)


@pytest.mark.parametrize('context', ['document', 'other_control'], indirect=True)
def test_old_document_scope_and_other_reviewed_controls_are_ineligible(tmp_path, context):
    root = unknown(tmp_path, context)
    with pytest.raises(ComputerError, match='ineligible'):
        recovery.inspect(root)


@pytest.mark.parametrize('presence', ['visible', 'hidden'])
def test_hidden_or_visible_original_is_not_closed(tmp_path, context, presence):
    root = unknown(tmp_path, context)
    result = recovery.run(root, ReadOnly(context[2], presence), parent=tmp_path / 'rechecks')
    assert not result['ok'] and result['code'] == 'windows_control_current_cancel_unverified'
    assert recovery.load(result['root'])['ok'] is False


@pytest.mark.parametrize('field', ['window', 'document', 'verified', 'value', 'control', 'selector'])
def test_forged_success_or_wrong_identity_is_rejected(tmp_path, context, field):
    root = unknown(tmp_path, context); backend = ReadOnly(context[2]); raw = backend.value
    if field == 'window': raw['window']['hwnd'] += 1
    elif field == 'document': raw['reviewed']['facts']['document'] = {'saved': True}
    elif field == 'verified': raw['reviewed']['facts']['dialog_presence'] = 'hidden'
    elif field == 'value': raw['controls'][0]['value'] = 'inherited'
    elif field == 'control': raw['reviewed']['facts']['control'] = {'id': 'new-window'}
    else: raw['reviewed']['selector']['name'] = 'another button'
    assert not recovery.run(root, backend, parent=tmp_path / 'rechecks')['ok']


def native_worker(monkeypatch, *, mutate=lambda request, outcome, receipt: None):
    from agent_service import windows_access as access
    calls = []
    class Worker:
        def __init__(self, parent): self.parent = parent
        def observe_condition(self, condition, *, stop):
            calls.append(deepcopy(condition))
            folder = self.parent / ('observe-' + '0' * 32); folder.mkdir(parents=True)
            current = observation(condition)
            request = {'op': 'observe', 'window': deepcopy(WINDOW), 'selector': condition['selector'],
                       'document_token': condition['value']}
            outcome = {'ok': True, 'observation': deepcopy(current)}
            receipt = {'schema': 1, 'observation': deepcopy(current)}
            mutate(request, outcome, receipt)
            write_new(folder / 'native-request.json', request)
            write_new(folder / 'native-outcome.json', outcome)
            write_new(folder / 'reviewed-observe.json', receipt)
            return current
        def act(self, *args, **kwargs): pytest.fail('production readback dispatched input')
    monkeypatch.setattr(access, 'WindowsBackend', Worker)
    return calls


def test_production_proof_latest_reuses_saved_history_without_backend(tmp_path, context, monkeypatch):
    root = unknown(tmp_path, context); before = snapshot(root)
    calls = native_worker(monkeypatch)
    result = recovery.run(root, parent=tmp_path / 'rechecks')
    assert result['ok'] and result['native_required'] is True
    assert len(result['native_proof']['files']) == 3 and len(calls) == 1
    history = snapshot(tmp_path / 'rechecks')
    assert recovery.verified_window(result['root']) == WINDOW
    loaded = recovery.latest(root, parent=tmp_path / 'rechecks')
    assert loaded['root'] == result['root'] and loaded['historical'] and not loaded['fresh_permission']
    assert snapshot(tmp_path / 'rechecks') == history and snapshot(root) == before and len(calls) == 1


@pytest.mark.parametrize('field', ['request', 'outcome', 'receipt'])
def test_default_worker_native_chain_must_match_returned_observation(tmp_path, context, monkeypatch, field):
    root = unknown(tmp_path, context)
    def mutate(request, outcome, receipt):
        if field == 'request': request['document_token'] = 'wrong-source'
        elif field == 'outcome': outcome['observation']['reviewed']['verified'] = False
        else: receipt['schema'] = 2
    native_worker(monkeypatch, mutate=mutate)
    result = recovery.run(root, parent=tmp_path / 'rechecks')
    assert not result['ok'] and result['code'] == 'windows_control_recheck_native_evidence_invalid'


@pytest.mark.parametrize('name', ['native-request.json', 'native-outcome.json', 'reviewed-observe.json', 'extra.json'])
def test_proof_tamper_or_added_evidence_breaks_load(tmp_path, context, monkeypatch, name):
    root = unknown(tmp_path, context); native_worker(monkeypatch)
    result = recovery.run(root, parent=tmp_path / 'rechecks'); folder = Path(result['root'])
    operation = next((folder / 'native').iterdir())
    (operation / name).write_text('{}', encoding='utf8')
    with pytest.raises(ComputerError, match='evidence_changed'):
        recovery.load(folder)
    assert recovery.latest(root, parent=tmp_path / 'rechecks') is None


def test_unchanged_old_prefix_and_native_source_proof_required_on_load(tmp_path, context):
    root = unknown(tmp_path, context)
    result = recovery.run(root, ReadOnly(context[2]), parent=tmp_path / 'rechecks')
    source_native = Path(context[0]['native_proof']['root'])
    (source_native / 'native-outcome.json').write_text('{}', encoding='utf8')
    with pytest.raises(ComputerError, match='source_changed'):
        recovery.load(result['root'])


def test_original_evidence_drift_during_readback_never_succeeds(tmp_path, context):
    root = unknown(tmp_path, context)
    backend = ReadOnly(context[2], change=lambda: (root / 'new-evidence.json').write_text('{}'))
    result = recovery.run(root, backend, parent=tmp_path / 'rechecks')
    assert not result['ok'] and result['code'] == 'windows_control_recheck_source_changed'


def test_destination_inside_original_and_cancel_before_read_have_no_writes(tmp_path, context):
    root = unknown(tmp_path, context); before = snapshot(root); backend = ReadOnly(context[2])
    with pytest.raises(ComputerError, match='destination_invalid'):
        recovery.run(root, backend, parent=root / 'rechecks')
    with pytest.raises(ComputerError, match='cancelled'):
        recovery.run(root, backend, parent=tmp_path / 'rechecks', stop=lambda: True)
    assert not backend.calls and snapshot(root) == before and not (tmp_path / 'rechecks').exists()


@pytest.mark.parametrize('binding', ['request_sha', 'role', 'source', 'window', 'control', 'document_token', 'native_result'])
def test_original_native_binding_is_checked_even_with_valid_event_inventory(tmp_path, context, binding):
    def mutate(records):
        request = records['native-request.json']; intent = records['native-intent.json']
        if binding in {'request_sha', 'role', 'source'}:
            intent[binding] = 'wrong'
        elif binding == 'window': request['window'] = {**WINDOW, 'pid': 999}
        elif binding == 'control': request['control'] = {**request['control'], 'id': 'wrong'}
        elif binding == 'document_token': request['document_token'] = 'wrong'
        else: records['native-outcome.json']['code'] = 'different'
        if binding in {'window', 'control', 'document_token'}:
            intent['request_sha'] = digest(request)
    root = unknown(tmp_path, context, mutate=mutate)
    with pytest.raises(ComputerError, match='ineligible'):
        recovery.inspect(root)


def test_prior_completed_prefix_bytes_are_required_on_history_load(tmp_path, context):
    root = unknown(tmp_path, context)
    result = recovery.run(root, ReadOnly(context[2]), parent=tmp_path / 'rechecks')
    event = root / 'attempts/0001/events/0001.json'
    event.write_text('{}', encoding='utf8')
    with pytest.raises(ComputerError, match='evidence_changed'):
        recovery.load(result['root'])


def test_source_change_during_native_history_validation_is_caught_at_end(tmp_path, context, monkeypatch):
    root = unknown(tmp_path, context); native_worker(monkeypatch)
    result = recovery.run(root, parent=tmp_path / 'rechecks')
    original = recovery._native_read
    def read(*args):
        proof = original(*args)
        (Path(context[0]['native_proof']['root']) / 'native-outcome.json').write_text('{}', encoding='utf8')
        return proof
    monkeypatch.setattr(recovery, '_native_read', read)
    with pytest.raises(ComputerError, match='source_changed'):
        recovery.load(result['root'])


def test_extra_native_intent_refused_even_if_inventory_is_recomputed(tmp_path, context, monkeypatch):
    root = unknown(tmp_path, context); native_worker(monkeypatch)
    result = recovery.run(root, parent=tmp_path / 'rechecks'); folder = Path(result['root'])
    receipt, _ = task._load(folder / 'receipt.json')
    (folder / 'receipt.json').unlink()
    write_new(next((folder / 'native').iterdir()) / 'native-intent.json', {'forbidden': True})
    receipt['evidence_sha256'] = task._inventory(folder)
    task._save(folder / 'receipt.json', receipt)
    with pytest.raises(ComputerError, match='native_evidence_invalid'):
        recovery.load(folder)


def test_observe_exception_creates_failure_only_receipt(tmp_path, context):
    root = unknown(tmp_path, context)
    def popup(): raise ComputerError('windows_open_dialog_input_busy')
    backend = ReadOnly(context[2], change=popup)
    result = recovery.run(root, backend, parent=tmp_path / 'rechecks')
    assert not result['ok'] and result['code'] == 'windows_open_dialog_input_busy'
    assert not recovery.load(result['root'])['ok']
