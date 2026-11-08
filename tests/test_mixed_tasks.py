"""Parent contract tests; no GUI, paid calls or native Office execution."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path

import pytest

from agent_service import mixed_tasks as mixed, windows_task as task
from agent_service.computer import ComputerError, digest
from agent_service.windows_auto import PROVIDER_BINDING
from test_windows_task import WINDOW, Backend, condition, control, goal


STEPS = [
    {'kind': 'windows', 'instruction': 'Select Documents', 'source_ids': [], 'window_ids': ['w1']},
    {'kind': 'office_edit', 'instruction': 'Set A1 to 3', 'source_ids': ['s1'], 'window_ids': []},
]


class Client:
    def __init__(self, response=None, error=False):
        self.response = {'steps': deepcopy(STEPS)} if response is None else response
        self.calls = 0
        self.error = error

    def complete(self, *args, **kwargs):
        self.calls += 1
        if self.error:
            raise RuntimeError('simulated interrupted call')
        return deepcopy(self.response)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(mixed, 'ROOT', tmp_path)
    source = tmp_path / 'old-success.xlsx'
    source.write_bytes(b'preserved-successful-artifact')
    records = [{'path': str(source), 'sha256': sha256(source.read_bytes()).hexdigest()}]
    def refs(rows, *, base):
        for row in rows:
            if sha256(Path(row['path']).read_bytes()).hexdigest() != row['sha256']:
                raise ComputerError('changed_source')
        return [{'artifact': row['path'], 'sha256': row['sha256'], 'receipt_sha256': 'a'*64} for row in rows]
    monkeypatch.setattr(mixed, 'select_outputs', refs)
    root = mixed.create('先选择Documents，然后把Excel A1改成3', records, [WINDOW],
                        parent=tmp_path/'mixed', task_base=tmp_path)
    return root, records


def ready(setup):
    root, _ = setup
    view = mixed.split(root, Client())
    return root, mixed.approve(root, view['plan_sha256'])


def windows_plan(setup, monkeypatch, *, conditions=None):
    from agent_service import windows_access, windows_intent, windows_auto
    root, _ = ready(setup)
    backend = Backend(control(), control('Downloads'))
    backend.provider_binding = deepcopy(PROVIDER_BINDING)
    monkeypatch.setattr(windows_access, 'AutoWindowsBackend', lambda: backend)
    monkeypatch.setattr(windows_access, 'backend_for_binding', lambda binding: backend)
    # This fixture exercises the parent and real task journal, not a native auto
    # worker receipt; that verifier has its own provider integration coverage.
    monkeypatch.setattr(windows_auto, 'verify_result', lambda *args: None)
    planned = goal(*(conditions or [condition()]))
    planned['instruction'] = STEPS[0]['instruction']
    monkeypatch.setattr(windows_intent, 'propose', lambda *a, **k: deepcopy(planned))
    view = mixed.prepare(root, Client())
    return root, view, backend


def test_split_claim_and_complete_response_are_reused(setup):
    root, records = setup
    assert mixed.inspect(root)['state'] == 'split_required'
    client = Client()
    view = mixed.split(root, client)
    assert view['state'] == 'review' and view['records'] == records
    assert mixed.split(root, client) == view and client.calls == 1
    intent = mixed._load(root/'split-intent.json')
    assert intent['context']['sources'][0]['id'] == 's1'
    assert 'path' not in intent['context']['sources'][0]


def test_missing_split_response_never_calls_again(setup):
    root, _ = setup
    client = Client(error=True)
    with pytest.raises(RuntimeError):
        mixed.split(root, client)
    assert mixed.inspect(root)['state'] == 'split_pending'
    with pytest.raises(ComputerError, match='mixed_split_pending_no_retry'):
        mixed.split(root, client)
    assert client.calls == 1


@pytest.mark.parametrize('response', [
    {'steps': STEPS[:1]}, {'steps': STEPS*3}, {'steps': [STEPS[0], STEPS[0]]},
    {'steps': [{**STEPS[0], 'window_ids': ['w9']}, STEPS[1]]},
    {'steps': [STEPS[0], {**STEPS[1], 'source_ids': ['s2']}]},
    {'steps': [STEPS[0], {**STEPS[1], 'path': 'C:/invented.xlsx'}]},
    {'steps': [STEPS[0], {**STEPS[1], 'window_ids': ['w1']}]},
    {'unsupported': 'cannot preserve the complete instruction'},
])
def test_invalid_or_incomplete_split_is_not_approved_or_retried(setup, response):
    root, _ = setup
    client = Client(response)
    with pytest.raises(ComputerError):
        mixed.split(root, client)
    assert not (root/'split.json').exists()
    with pytest.raises(ComputerError, match='pending_no_retry'):
        mixed.split(root, client)
    assert client.calls == 1


def test_split_approval_and_sources_are_exact(setup):
    root, records = setup
    view = mixed.split(root, Client())
    with pytest.raises(ComputerError, match='approval_required'):
        mixed.approve(root, 'f'*64)
    Path(records[0]['path']).write_bytes(b'changed')
    with pytest.raises(ComputerError, match='changed_source'):
        mixed.approve(root, view['plan_sha256'])


def test_unapproved_checklist_does_not_prepare(setup):
    root, _ = setup
    mixed.split(root, Client())
    with pytest.raises(ComputerError, match='prepare_unavailable'):
        mixed.prepare(root, Client())
    assert not list(root.glob('step-*-intent.json'))


def test_windows_child_success_passes_batch_and_restart_does_not_replay(setup, monkeypatch):
    root, view, backend = windows_plan(setup, monkeypatch)
    token = view['current']['plan_sha256']
    assert view['state'] == 'preview'
    with pytest.raises(ComputerError, match='child_approval_required'):
        mixed.execute(root, 'b'*64)
    done = mixed.execute(root, token)
    assert done['current_index'] == 1 and done['state'] == 'ready'
    assert done['records'] == setup[1] and len(backend.calls) == 1
    assert mixed.inspect(root) == done
    assert len(backend.calls) == 1


def test_child_success_parent_gap_is_reconciled_without_dispatch(setup, monkeypatch):
    root, view, backend = windows_plan(setup, monkeypatch)
    binding = {k: v for k, v in view['current'].items() if k != 'summary'}
    token = binding['plan_sha256']
    mixed._save(root/f'step-0-dispatch-{token}.json', {'binding_sha256': digest(binding)})
    task.run(binding['task_root'], backend)
    assert mixed.inspect(root)['state'] == 'child_verified'
    done = mixed.execute(root, token)
    assert done['current_index'] == 1 and len(backend.calls) == 1


def test_claim_without_any_child_effect_is_not_replayed(setup, monkeypatch):
    root, view, backend = windows_plan(setup, monkeypatch)
    binding = {k: v for k, v in view['current'].items() if k != 'summary'}
    token = binding['plan_sha256']
    mixed._save(root/f'step-0-dispatch-{token}.json', {'binding_sha256': digest(binding)})
    with pytest.raises(ComputerError, match='unknown_no_replay'):
        mixed.execute(root, token)
    assert backend.calls == []


def test_unknown_windows_effect_never_creates_or_replays_child(setup, monkeypatch):
    root, view, backend = windows_plan(setup, monkeypatch)
    token = view['current']['plan_sha256']
    backend.result = {'ok': False, 'state': 'unknown', 'effects': 'unknown'}
    assert mixed.execute(root, token)['state'] == 'unknown'
    for operation in [lambda: mixed.execute(root, token), lambda: mixed.resume_preview(root),
                      lambda: mixed.prepare(root, Client())]:
        with pytest.raises(ComputerError):
            operation()
    assert len(backend.calls) == 1 and not (root/'step-1-intent.json').exists()


def test_safe_windows_pause_resumes_original_task_remaining_only(setup, monkeypatch):
    root, view, backend = windows_plan(setup, monkeypatch,
        conditions=[condition(), condition('Downloads')])
    token = view['current']['plan_sha256']
    child = view['current']['task_root']
    paused = mixed.execute(root, token, pause_after_step=True)
    assert paused['state'] == 'paused' and len(backend.calls) == 1
    restored = mixed.resume_preview(root)
    assert restored['current']['task_root'] == child
    finished = mixed.execute(root, token)
    assert finished['current_index'] == 1 and len(backend.calls) == 2


def test_changed_completed_child_receipt_blocks_restore(setup, monkeypatch):
    root, view, backend = windows_plan(setup, monkeypatch)
    mixed.execute(root, view['current']['plan_sha256'])
    receipt = next(Path(view['current']['task_root']).rglob('receipt.json'))
    receipt.write_text('{"changed":true}', encoding='utf-8')
    with pytest.raises(ComputerError):
        mixed.inspect(root)
    assert len(backend.calls) == 1


def test_planning_interrupt_is_durable_without_repeated_call(setup, monkeypatch):
    from agent_service import windows_access, windows_intent
    root, _ = ready(setup)
    backend = Backend()
    backend.provider_binding = deepcopy(PROVIDER_BINDING)
    monkeypatch.setattr(windows_access, 'AutoWindowsBackend', lambda: backend)
    calls = []
    def failure(*args, **kwargs):
        calls.append(True)
        raise RuntimeError('model response lost')
    monkeypatch.setattr(windows_intent, 'propose', failure)
    with pytest.raises(RuntimeError):
        mixed.prepare(root, Client())
    assert mixed.inspect(root)['state'] == 'planning_pending'
    with pytest.raises(ComputerError, match='prepare_unavailable'):
        mixed.prepare(root, Client())
    assert calls == [True]


def test_final_outputs_require_all_steps(setup):
    with pytest.raises(ComputerError, match='not_complete'):
        mixed.outputs(setup[0])


@pytest.fixture
def office_setup(tmp_path, monkeypatch):
    from openpyxl import Workbook
    from agent_service import office_handoff
    monkeypatch.setattr(mixed, 'ROOT', tmp_path)
    records = []
    for index in range(2):
        path = tmp_path / f'preserved-{index}.xlsx'
        book = Workbook()
        book.active['A1'] = index + 1
        book.active['A2'] = '=SUM(A1:A1)'
        book.save(path)
        book.close()
        records.append({'path': str(path), 'sha256': sha256(path.read_bytes()).hexdigest()})
    def refs(rows, *, base):
        assert all(sha256(Path(r['path']).read_bytes()).hexdigest() == r['sha256'] for r in rows)
        return [{'artifact': r['path'], 'sha256': r['sha256']} for r in rows]
    monkeypatch.setattr(mixed, 'select_outputs', refs)
    monkeypatch.setattr(office_handoff, 'verify_references', lambda *a, **k: None)
    original_create = mixed.ComputerSession.create
    monkeypatch.setattr(mixed.ComputerSession, 'create', lambda: original_create(tmp_path/'sessions'))
    monkeypatch.setattr(mixed.adapter, 'current_candidate', lambda: 'c'*64)
    monkeypatch.setattr(mixed.adapter, 'status', lambda *a: {'remaining': 2, 'trial_passed': True})
    rows = [{**STEPS[1], 'source_ids': ['s2', 's1']}, STEPS[0]]
    root = mixed.create('先修改原第一份Excel，再选Documents', records, [WINDOW],
                        parent=tmp_path/'mixed', task_base=tmp_path)
    view = mixed.split(root, Client({'steps': rows}))
    mixed.approve(root, view['plan_sha256'])
    return root, records


class OfficeClient:
    calls = 0

    def complete(self, system, context, **kwargs):
        self.calls += 1
        value = json.loads(context)
        # source_ids are deliberately reversed: e2 is logical slot s1.
        target = next(row['target'] for row in value['files'][1]['content'] if row.get('editable'))
        return {'kind': 'edits', 'changes': [{'target': target, 'after': 3}]}


def test_office_qualification_blocks_before_model(office_setup, monkeypatch):
    root, _ = office_setup
    monkeypatch.setattr(mixed.adapter, 'status', lambda *a: {'remaining': 0, 'trial_passed': True})
    client = OfficeClient()
    with pytest.raises(ComputerError, match='qualification_unavailable'):
        mixed.prepare(root, client)
    assert client.calls == 0
    assert mixed.inspect(root)['state'] == 'ready'
    assert not (root/'step-0-intent.json').exists()
    assert not list((mixed.ROOT/'sessions').glob('computer-*'))
    # Once the ordinary reviewed gate permits it, continue this same step;
    # no replacement root, old unknown claim or duplicate model request.
    monkeypatch.setattr(mixed.adapter, 'status', lambda *a: {'remaining': 1, 'trial_passed': True})
    assert mixed.prepare(root, client)['state'] == 'preview'
    assert client.calls == 1


def test_office_real_scope_and_intent_bind_sparse_edit_to_reversed_slot(office_setup, monkeypatch, tmp_path):
    root, records = office_setup
    client = OfficeClient()
    view = mixed.prepare(root, client)
    assert view['state'] == 'preview' and client.calls == 1
    current = view['current']
    assert [e['id'] for e in current['plan']['goal']['edits']] == ['e2']
    output = tmp_path/'edited'/'edit'
    output.mkdir(parents=True)
    artifact = output/'edited-spreadsheet.xlsx'
    artifact.write_bytes(b'native-output-placeholder-for-parent-mapping-test')
    native_artifact = {'path': str(artifact), 'sha256': sha256(artifact.read_bytes()).hexdigest()}
    from agent_service.computer_agent_tools import _save as save_tool
    save_tool(output/'edit-manifest.json', {'source_root': current['plan']['goal']['edits'][0]['source_root']})
    monkeypatch.setattr(mixed, '_child', lambda binding: {'state': 'verified'})
    monkeypatch.setattr(mixed.adapter, '_verified', lambda *a: {'artifacts': [native_artifact], 'evidence_sha256': 'a'*64})
    session = Path(current['session_root'])
    run = session/'goals'/(current['plan_sha256']+'.run')
    run.mkdir()
    (run/'outcome.json').write_text('{"ok":true}', encoding='utf-8')
    binding = {key: val for key, val in current.items() if key != 'summary'}
    proof = mixed._proof(binding, records, mixed._config(root)[1])
    assert proof['records'] == [native_artifact, records[1]]


def test_office_preview_reuses_paid_plan_and_exact_adapter_execution(office_setup, monkeypatch):
    root, _ = office_setup
    client = OfficeClient()
    view = mixed.prepare(root, client)
    assert mixed.prepare(root, client) == view and client.calls == 1
    calls = []
    def failed_execution(store, candidate, session, plan, **kwargs):
        assert (root/f'step-0-dispatch-{plan["sha256"]}.json').exists()
        calls.append((candidate, str(session.root), plan['sha256'], kwargs['approval']))
        raise RuntimeError('simulated process exit before child result')
    monkeypatch.setattr(mixed.adapter, 'execute', failed_execution)
    token = view['current']['plan_sha256']
    with pytest.raises(RuntimeError):
        mixed.execute(root, token)
    with pytest.raises(ComputerError, match='unknown_no_replay'):
        mixed.execute(root, token)
    assert calls == [('c'*64, view['current']['session_root'], token, token)]


def test_office_resume_preserves_session_candidate_and_uses_adapter_resume(office_setup, monkeypatch):
    root, _ = office_setup
    view = mixed.prepare(root, OfficeClient())
    original = view['current']
    binding = {k: v for k, v in original.items() if k != 'summary'}
    mixed._save(root/f'step-0-dispatch-{original["plan_sha256"]}.json', {'binding_sha256': digest(binding)})
    monkeypatch.setattr(mixed, '_child', lambda child: {'state': 'paused', 'resumable': True})
    monkeypatch.setattr(mixed.adapter, 'owns_recovery', lambda *a: {'candidate': 'c'*64, 'index': 2})
    recovery = {**original['plan'], 'recovery': {'parent_run': 'retained-original-run'}, 'sha256': 'd'*64}
    monkeypatch.setattr(mixed.adapter, 'prepare_resume', lambda *a, **k: {'plan': recovery})
    restored = mixed.resume_preview(root)
    assert restored['current']['session_root'] == original['session_root']
    assert restored['current']['candidate'] == original['candidate']
    assert restored['current']['plan'] == recovery and restored['state'] == 'preview'
    calls = []
    def resumed(store, candidate, session, plan, **kwargs):
        calls.append((candidate, str(session.root), plan, kwargs['approval']))
    monkeypatch.setattr(mixed.adapter, 'resume', resumed)
    mixed.execute(root, 'd'*64)
    assert calls == [('c'*64, original['session_root'], recovery, 'd'*64)]


def test_changed_parent_step_intent_cannot_bind_another_child(setup, monkeypatch):
    root, _, backend = windows_plan(setup, monkeypatch)
    path = root/'step-0-intent.json'
    value = task._load(path)[0]
    value['records'] = []
    path.write_text(json.dumps({'schema': 1, 'payload': value, 'sha256': digest(value)}), encoding='utf-8')
    with pytest.raises(ComputerError, match='evidence_changed'):
        mixed.inspect(root)
    assert backend.calls == []
