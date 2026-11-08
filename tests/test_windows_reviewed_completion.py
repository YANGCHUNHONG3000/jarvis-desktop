from copy import deepcopy
import json

import pytest

from agent_service import windows_task as task, windows_visual_proposal as proposal
from agent_service.computer import ComputerError, digest
from test_windows_reviewed_controls import Backend, context, plan


def _approved(context, tmp_path):
    root, goal = plan(context, tmp_path)
    proposal.approve(root)
    return root, goal


def test_crash_after_atomic_result_skips_completed(context, tmp_path, monkeypatch):
    state, *_ = context
    root, _ = _approved(context, tmp_path)
    original = task._save
    class Crash(BaseException):
        pass
    def save(path, payload):
        value = original(path, payload)
        if payload.get('kind') == 'result' and payload.get('completes'):
            raise Crash()
        return value
    monkeypatch.setattr(task, '_save', save)
    with pytest.raises(Crash):
        task.run(root, Backend())
    assert state['acts'] == ['filename']
    monkeypatch.setattr(task, '_save', original)
    assert task.run(root, Backend())['ok']
    assert state['acts'] == ['filename', 'cancel']


def test_final_read_failure_keeps_completion_and_no_replay(context, tmp_path):
    state, _, options, obs = context
    item = deepcopy(options[0]); item['expected'] = ['C:/existing.xlsx']
    from agent_service import windows_reviewed_controls as reviewed
    goal = {'schema': 1, 'instruction': 'fill filename', 'conditions': [reviewed.condition(item)], 'max_steps': 4}
    ref = proposal.save(goal, options, [obs], parent=tmp_path/'proposals')
    root = task.create(tmp_path/'tasks', goal, visual_proposal=ref); proposal.approve(root)
    class FailFinal(Backend):
        def observe_condition(self, c, *, stop):
            if state['acts']:
                raise ComputerError('read_failed')
            return super().observe_condition(c, stop=stop)
    result = task.run(root, FailFinal())
    assert not result['ok'] and result['completed'] == ['target_1']
    assert task.run(root, Backend())['ok']
    assert state['acts'] == ['filename']


def test_tampered_completion_rejected(context, tmp_path):
    root, _ = _approved(context, tmp_path)
    task.run(root, Backend(), pause_after_step=True)
    events = sorted((root/'attempts'/'0001'/'events').iterdir())
    path = next(p for p in events if json.loads(p.read_text(encoding='utf8'))['payload'].get('completes'))
    record = json.loads(path.read_text(encoding='utf8'))
    record['payload']['result']['observation']['reviewed']['verified'] = False
    record['sha256'] = digest(record['payload'])
    path.write_text(json.dumps(record), encoding='utf8')
    with pytest.raises(ComputerError):
        task.run(root, Backend())


def test_missing_postcondition_is_unknown_and_never_replayed(context, tmp_path):
    state, *_ = context
    root, _ = _approved(context, tmp_path)
    class Missing(Backend):
        def act(self, *args, **kwargs):
            result = super().act(*args, **kwargs)
            result.pop('observation')
            return result
    result = task.run(root, Missing())
    assert result['state'] == 'unknown' and state['acts'] == ['filename']
    with pytest.raises(ComputerError, match='unknown_requires_review'):
        task.run(root, Backend())
    assert state['acts'] == ['filename']
