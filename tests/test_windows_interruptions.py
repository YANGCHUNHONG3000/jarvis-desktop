from copy import deepcopy
import pytest
from agent_service import windows_task as task
from agent_service.computer import ComputerError
from test_windows_task import Backend, WINDOW, condition, control, goal


def blocked(window=WINDOW):
    return {'window':deepcopy(window),'controls':[],'dialogs':[],
            'blocked_reason':'owned_window_requires_manual_attention',
            'blocked_windows':[{'hwnd':123,'class_name':'TestDialog'}]}


class DialogBackend(Backend):
    def observe(self, window, *, stop):
        if self.blocked:
            self.observations += 1
            return blocked(window)
        return super().observe(window, stop=stop)


def test_preaction_pause_restores_without_observing_then_continues(tmp_path):
    root=task.create(tmp_path,goal()); backend=DialogBackend();backend.blocked=True
    paused=task.run(root,backend)
    assert paused['state']=='paused' and paused['manual_attention']['after_action'] is False
    assert paused['resumable'] and backend.observations==1 and not backend.calls
    saved=task._inventory(root/'attempts'/'0001')
    assert task.load(root)['summary']==paused and task.inspect(root)==paused
    assert backend.observations==1
    backend.blocked=False
    assert task.run(root,backend)['state']=='verified'
    assert len(backend.calls)==1 and task._inventory(root/'attempts'/'0001')==saved


def test_completed_prefix_is_not_replayed_after_popup(tmp_path):
    root=task.create(tmp_path,goal(condition(),condition('Downloads')))
    backend=DialogBackend(control(),control('Downloads'))
    first=task.run(root,backend,pause_after_step=True)
    assert first['completed']==['documents'] and len(backend.calls)==1
    backend.blocked=True
    paused=task.run(root,backend)
    assert paused['state']=='paused' and paused['completed']==['documents']
    assert paused['manual_attention']['condition_id']=='documents'
    backend.blocked=False
    assert task.run(root,backend)['state']=='verified'
    assert [call[0]['name'] for call in backend.calls]==['Documents','Downloads']


@pytest.mark.parametrize('after',[False,True])
def test_native_popup_result_pauses_or_stays_unknown(tmp_path,after):
    root=task.create(tmp_path,goal());backend=DialogBackend()
    backend.result={'ok':False,'state':'unknown' if after else 'failed',
        'effects':'unknown' if after else 'none','code':'windows_owned_dialog_after_action' if after else 'windows_owned_dialog_before_action',
        'observation':blocked()}
    result=task.run(root,backend)
    assert result['state']==('unknown' if after else 'paused')
    assert result['manual_attention']['after_action'] is after
    backend.result=None
    if after:
        with pytest.raises(ComputerError,match='unknown_requires_review'): task.run(root,backend)
        assert len(backend.calls)==1
    else:
        assert task.run(root,backend)['state']=='verified'


def test_popup_after_successful_call_before_completion_remains_unknown(tmp_path):
    root=task.create(tmp_path,goal());backend=DialogBackend()
    def action(value, operation):
        value.controls[0]['selected']=True;value.blocked=True
        return {'ok':True,'state':'verified','effects':'verified'}
    backend.on_act=action
    result=task.run(root,backend)
    assert result['state']=='unknown' and result['completed']==[]
    assert result['manual_attention']['after_action'] is True
    backend.blocked=False
    with pytest.raises(ComputerError,match='unknown_requires_review'): task.run(root,backend)
    assert len(backend.calls)==1


@pytest.mark.parametrize('failure',['metadata','observation'])
def test_unsealed_effect_remains_unknown_even_when_popup_read_fails(tmp_path,failure):
    root=task.create(tmp_path,goal());backend=DialogBackend()
    def action(value,operation):
        value.controls[0]['selected']=True
        def fail(window,*,stop):
            if failure=='observation': raise ComputerError('windows_blocked_metadata_invalid')
            raw=blocked(window);raw['blocked_windows'][0]['class_name']=''
            return raw
        value.observe=fail
        return {'ok':True,'state':'verified','effects':'verified'}
    backend.on_act=action
    assert task.run(root,backend)['state']=='unknown'
    with pytest.raises(ComputerError,match='unknown_requires_review'):task.run(root,backend)
    assert len(backend.calls)==1


def test_crash_after_success_result_without_complete_never_replays(tmp_path,monkeypatch):
    root=task.create(tmp_path,goal());backend=DialogBackend();save=task._save
    class Crash(BaseException):pass
    def crash(path,payload):
        result=save(path,payload)
        if payload.get('kind')=='result' and payload['result']['ok']:
            raise Crash()
        return result
    monkeypatch.setattr(task,'_save',crash)
    with pytest.raises(Crash):task.run(root,backend)
    monkeypatch.setattr(task,'_save',save)
    assert task.inspect(root)['state']=='unknown'
    with pytest.raises(ComputerError,match='unknown_requires_review'):task.run(root,backend)
    assert len(backend.calls)==1


@pytest.mark.parametrize('after',[False,True])
def test_interruption_without_outcome_is_still_authoritative(tmp_path,monkeypatch,after):
    root=task.create(tmp_path,goal());backend=DialogBackend()
    if after:
        backend.result={'ok':False,'state':'unknown','effects':'unknown','code':'windows_owned_dialog_after_action','observation':blocked()}
    else: backend.blocked=True
    save=task._save
    def crash(path,payload):
        if path.name=='outcome.json': raise OSError('synthetic crash')
        return save(path,payload)
    monkeypatch.setattr(task,'_save',crash)
    with pytest.raises(OSError,match='synthetic crash'): task.run(root,backend)
    monkeypatch.setattr(task,'_save',save)
    restored=task.inspect(root)
    assert restored['state']==('unknown' if after else 'paused')
    assert restored['manual_attention']['after_action'] is after
    assert restored['resumable'] is not after


@pytest.mark.parametrize('reason',['owned_window_requires_manual_attention','unrecognized_reason'])
def test_uncropped_backend_result_is_not_persisted(tmp_path,reason):
    root=task.create(tmp_path,goal());backend=DialogBackend();raw=blocked()
    raw['blocked_reason']=reason
    raw['blocked_windows'][0]['title']='PRIVATE_SECRET_MARKER_152'
    raw['controls']=[{'text':'PRIVATE_SECRET_MARKER_152'}]
    backend.result={'ok':False,'state':'unknown','effects':'unknown','observation':raw}
    result=task.run(root,backend)
    assert result['state']=='unknown'
    for path in root.rglob('*.json'):
        assert 'PRIVATE_SECRET_MARKER_152' not in path.read_text(encoding='utf-8')


@pytest.mark.parametrize('change',[{'title':'secret'},{'class_name':'bad\nclass'},{'class_name':''},{'hwnd':True},{'hwnd':2**65}])
def test_attention_metadata_rejects_unbounded_or_extra_data(change):
    raw=blocked();raw['blocked_windows'][0].update(change)
    with pytest.raises(ComputerError,match='interruption_invalid'): task._attention(raw,WINDOW)


def test_interruption_must_be_last_event_and_cannot_be_verified(tmp_path):
    root=task.create(tmp_path,goal());backend=DialogBackend();backend.blocked=True
    task.run(root,backend)
    attempt=root/'attempts'/'0001';path=attempt/'outcome.json'
    payload,_=task._load(path);payload.update(state='verified',ok=True)
    # Rewrite only a disposable synthetic test fixture; genuine receipts remain immutable.
    from agent_service.computer import digest
    import json
    path.write_text(json.dumps({'schema':1,'payload':payload,'sha256':digest(payload)}))
    with pytest.raises(ComputerError,match='interruption_invalid'):task.inspect(root)


def legacy_attempt(attempt, *, failed=False):
    """Rebuild only a synthetic fixture with the historical attempt contract."""
    from agent_service.computer import digest
    import json
    def replace(path,payload):
        path.write_text(json.dumps({'schema':1,'payload':payload,'sha256':digest(payload)}))
        return digest(payload)
    header,_=task._load(attempt/'attempt.json');del header['interruption_contract']
    tail=replace(attempt/'attempt.json',header)
    intent_sha=None
    for path in sorted((attempt/'events').iterdir()):
        event,_=task._load(path);event['previous']=tail
        if event['kind']=='result':event['intent_sha256']=intent_sha
        tail=replace(path,event)
        if event['kind']=='intent':intent_sha=tail
    outcome,_=task._load(attempt/'outcome.json');outcome['events_sha256']=tail
    if failed:outcome.update(state='failed',ok=False,code='windows_action_limit')
    replace(attempt/'outcome.json',outcome)


def test_legacy_unsealed_failure_keeps_history_but_cannot_replay(tmp_path):
    root=task.create(tmp_path,goal());backend=DialogBackend()
    backend.result={'ok':True,'state':'verified','effects':'verified'}
    task.run(root,backend);legacy_attempt(root/'attempts'/'0001',failed=True)
    inventory=task._inventory(root);state=task.inspect(root)
    assert state['state']=='failed' and state['code']=='windows_action_limit'
    assert state['unsealed_effect'] and not state['eligible'] and not state['resumable']
    with pytest.raises(ComputerError,match='unknown_requires_review'):task.run(root,backend)
    assert task._inventory(root)==inventory and len(backend.calls)==1


def test_legacy_header_cannot_claim_new_interruption_event(tmp_path):
    root=task.create(tmp_path,goal());backend=DialogBackend();backend.blocked=True
    task.run(root,backend);legacy_attempt(root/'attempts'/'0001')
    with pytest.raises(ComputerError,match='interruption_invalid'):task.inspect(root)


def test_no_events_may_follow_interruption(tmp_path):
    root=task.create(tmp_path,goal());backend=DialogBackend();backend.blocked=True
    task.run(root,backend);events=root/'attempts'/'0001'/'events'
    first,tail=task._load(events/'0001.json');extra=deepcopy(first);extra['previous']=tail
    task._save(events/'0002.json',extra)
    with pytest.raises(ComputerError,match='interruption_invalid'):task.inspect(root)
