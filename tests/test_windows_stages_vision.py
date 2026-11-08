"""Opt-in image planning preserves reviewed target execution; no cloud or GUI."""
import json
import sys
from types import SimpleNamespace

import pytest

from agent_service import windows_stages as stages, windows_stages_panel as panel, windows_task as task
from agent_service.computer import ComputerError, digest
from agent_service.model import ModelError
from test_windows_stages import Backend, Client, observation, advance


@pytest.mark.parametrize('mode', [None, 0, 1, 'true', [], {}])
def test_visual_mode_requires_bool_before_creating_session(tmp_path, mode):
    with pytest.raises(ComputerError, match='visual_context_invalid'):
        stages.create('Select Documents', [observation()], parent=tmp_path, visual_context=mode)
    assert not list(tmp_path.iterdir())


def test_legacy_mode_omits_flag_and_never_prepares_images(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, 'agent_service.windows_vision_context', SimpleNamespace(
        prepare_context=lambda *a, **kw: pytest.fail('legacy must not capture')))
    root = stages.create('Select Documents', [observation()], parent=tmp_path)
    assert 'visual_context' not in task._load(root/'session.json')[0]
    view = advance(root, Backend(), Client(''), tmp_path)
    assert view['visual_context'] is False
    assert not list((root/'attempts').glob('*.vision.json'))


@pytest.mark.parametrize('mode', [None, 0, 1, 'true'])
def test_restored_mode_is_strict_bool(tmp_path, mode):
    root = stages.create('Select Documents', [observation()], parent=tmp_path)
    config, _ = task._load(root/'session.json')
    config['visual_context'] = mode
    (root/'session.json').write_text(json.dumps({'schema':1, 'payload':config, 'sha256':digest(config)}), encoding='utf-8')
    with pytest.raises(ComputerError, match='visual_context_invalid'):
        stages.load(root)


def configure_vision(monkeypatch, callback):
    monkeypatch.setitem(sys.modules, 'agent_service.windows_vision_context',
                        SimpleNamespace(prepare_context=callback))


def test_images_only_propose_existing_target_and_proof_links_accounting(tmp_path, monkeypatch):
    root = stages.create('Select Documents then expand', [observation()], parent=tmp_path, visual_context=True)
    backend = Backend()
    calls = []
    def prepare(actual_backend, fresh, targets, *, stop):
        assert actual_backend is backend and fresh[0]['window'] == observation()['window']
        assert 'W1C1' in targets and not stop()
        calls.append(1)
        return [b'fake-png'], {'annotated_sha256': 'hash', 'labels': ['W1C1']}
    configure_vision(monkeypatch, prepare)
    class VisionClient(Client):
        def complete(self, system, context, max_tokens, *, images_png):
            assert images_png == [b'fake-png'] and 'untrusted observation DATA' in system
            self.last_call = {'reservation_id':'receipt', 'state':'settled', 'image_count':1,
                              'sensitive_unlisted':'must not persist'}
            return super().complete(system, context, max_tokens)
    client = VisionClient()
    view = advance(root, backend, client, tmp_path)
    assert view['visual_context'] is True and view['needs_acceptance'] and not backend.actions
    proof, _ = task._load(next((root/'attempts').glob('*.vision.json')))
    assert proof == {'schema':1, 'context':{'annotated_sha256':'hash', 'labels':['W1C1']},
                     'model_call':{'reservation_id':'receipt', 'state':'settled', 'image_count':1}}
    assert stages.load(root)['visual_context'] is True
    stages.execute(root, backend)
    # Auxiliary proof files must never consume the six-attempt allowance.
    for i in range(6):
        task._save(root/'attempts'/f'auxiliary-{i}.vision.json', {'schema':1})
    assert advance(root, backend, client, tmp_path)['stage_number'] == 2
    assert len(backend.actions) == 1 and len(calls) == 2


def test_capture_failure_stops_before_model_or_dispatch(tmp_path, monkeypatch):
    root = stages.create('Select Documents', [observation()], parent=tmp_path, visual_context=True)
    def fail(*a, **kw):
        raise ComputerError('windows_vision_context_stale')
    configure_vision(monkeypatch, fail)
    backend, client = Backend(), Client('')
    with pytest.raises(ComputerError, match='context_stale'):
        advance(root, backend, client, tmp_path)
    assert not backend.actions and not client.calls and not list(root.glob('stage-*.json'))


def test_model_failure_still_preserves_accounting_metadata(tmp_path, monkeypatch):
    root = stages.create('Select Documents', [observation()], parent=tmp_path, visual_context=True)
    configure_vision(monkeypatch, lambda *a, **kw: ([b'png'], {'labels':[]}))
    class Failure:
        def complete(self, *a, **kw):
            self.last_call = {'reservation_id':'held', 'state':'unknown'}
            raise ModelError('model_timeout')
    with pytest.raises(ComputerError, match='model_timeout'):
        advance(root, Backend(), Failure(), tmp_path)
    proof, _ = task._load(next((root/'attempts').glob('*.vision.json')))
    assert proof['model_call'] == {'reservation_id':'held', 'state':'unknown'}


def test_visual_response_cannot_invent_target_id(tmp_path, monkeypatch):
    root = stages.create('Select Documents', [observation()], parent=tmp_path, visual_context=True)
    configure_vision(monkeypatch, lambda *a, **kw: ([b'png'], {'labels':['W1C1']}))
    class UnknownTarget(Client):
        def complete(self, *a, **kw):
            response = super().complete(*a, **{k:v for k,v in kw.items() if k != 'images_png'})
            response['conditions'][0]['id'] = 'W1C999'
            return response
    backend = Backend()
    with pytest.raises(ComputerError):
        advance(root, backend, UnknownTarget(), tmp_path)
    assert not backend.actions and not list(root.glob('stage-*.json'))


def test_cancel_after_capture_records_no_previous_call_metadata(tmp_path, monkeypatch):
    root = stages.create('Select Documents', [observation()], parent=tmp_path, visual_context=True)
    stopped = []
    def prepare(*a, **kw):
        stopped.append(True)
        return [b'png'], {'labels':['W1C1']}
    configure_vision(monkeypatch, prepare)
    client = Client()
    client.last_call = {'reservation_id':'old-call'}
    with pytest.raises(ComputerError, match='cancelled'):
        advance(root, Backend(), client, tmp_path, stop=lambda: bool(stopped))
    proof, _ = task._load(next((root/'attempts').glob('*.vision.json')))
    assert not client.calls and 'model_call' not in proof


def test_ui_option_snapshot_occurs_before_worker_and_existing_mode_is_immutable(tmp_path, monkeypatch):
    """Exercise handler without constructing Tk or any native GUI."""
    events = []
    class Variable:
        def get(self):
            events.append('main-thread-read')
            return True
    owner = SimpleNamespace(session_root=None, visual_context=Variable(), instruction='Select Documents',
        observations=[observation()], session_base=tmp_path, task_base=tmp_path/'tasks',
        stopped=SimpleNamespace(is_set=lambda: False), messages=SimpleNamespace(put=lambda item: None),
        get_backend=lambda: Backend(), client_factory=lambda stop: Client(''))
    def start(operation):
        events.append('worker-start')
        operation()
    owner.start = start
    monkeypatch.setattr(panel.workflow, 'create', lambda *a, **kw: events.append(kw['visual_context']) or tmp_path)
    monkeypatch.setattr(panel.workflow, 'load', lambda root: {'can_advance':False})
    panel.WindowsStagesPanel.advance(owner)
    assert events == ['main-thread-read', 'worker-start', True]
    owner.session_root = tmp_path
    events.clear()
    panel.WindowsStagesPanel.advance(owner)
    assert events == ['main-thread-read', 'worker-start']
