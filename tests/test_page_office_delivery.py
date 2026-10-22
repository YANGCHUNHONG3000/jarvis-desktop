"""Offline saved-browser evidence and standard Office artifact handoff.

The Office executor creates actual OOXML; it does not stand in for live acceptance.
"""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path

import pytest

from agent_service import page_office_delivery as page
from agent_service.computer import ComputerError, ComputerSession, digest, read_record, write_new
from agent_service.computer_agent import choices_for
from agent_service.computer_agent_tools import _save, _load
from agent_service.artifact_source import prepare_artifact_source, snapshot_artifact_source
from test_artifact_source import fake as office_fake


def put(path, value):
    path.write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8')


@pytest.fixture
def reading(tmp_path_factory):
    folder = tmp_path_factory.mktemp('p')
    base = folder/'c'
    session = ComputerSession.create(base)
    goals = session.root/'goals'
    goals.mkdir()
    config = {'mode':'public_search','site':'python_docs',
        'start_url':'https://docs.python.org/3/','query':'pathlib',
        'target_url':'https://docs.python.org/3/library/pathlib.html',
        'required_text':['pathlib'],'headless':True}
    goal = {'kind':'interactive','browser':config,'edits':[],'page_office':True}
    plan = {'schema':3,'mode':'agent','instruction':'Read public page and deliver Excel and Word',
        'goal':goal,'max_actions':12,'max_seconds':360,'prepared_at':10.0}
    plan['sha256'] = digest(plan)
    write_new(goals/(plan['sha256']+'.json'),plan)
    run = goals/(plan['sha256']+'.run')
    run.mkdir()
    write_new(run/'claim.json',{'mode':'agent','approval':plan['sha256'],'at':11.0})
    _save(run/'environment-goal.json',goal)
    body = 'Python pathlib tutorial. '+('Saved page text 中文。\n' * 220)
    attempts, hashes = [], []
    codes = ['browser_fill_verified','browser_public_search_submitted','browser_public_target_opened']
    def browser(phase):
        complete = phase == 3
        value = {'session_id':'offline-public-page','title':'Python tutorial',
            'url':config['target_url'] if complete else config['start_url'],
            'text':body if complete else 'Search public Python documents',
            'locked':False,'human_required':False,'failures':[],
            'checks':{'goal_met':complete},'discovery':{
                'mode':'public_search','site':'python_docs','phase':'verified' if complete else 'pending',
                'query_verified':phase >= 2,'content_verified':complete,
                'search_status':200 if phase >= 2 else None,'content_status':200 if complete else None,
                'target_url':config['target_url'],'main_present':True,
                'search_finished':phase >= 2,'content_finished':complete,
                'search_submitted':phase >= 2,'target_clicked':complete,
                'ambiguous_controls':False,'controls_overflow':False}}
        value['state_id'] = digest(value)
        value['actions'] = [] if complete else [{
            'kind':'browser_fill' if phase == 0 else 'browser_click',
            'rule_id':('query','submit','content')[phase],
            'control_id':f'actual-{phase}','state_id':value['state_id']}]
        return value
    for index in range(1,4):
        before = {'browser':browser(index-1),'checks':{'sources_ready':False,'goal_met':False}}
        write_new(run/f'observation-{index:02}.json',before)
        action = before['browser']['actions'][0]
        state, choices = choices_for(run,before,[action])
        step = run/f'step-{index:02}'
        step.mkdir()
        write_new(step/'choices.json',{'state_sha256':state,'choices':choices})
        write_new(step/'decision.json',{'method':'single_legal_action','choice':choices[0],
                                          'state_sha256':state})
        write_new(step/'intent.json',choices[0])
        intent = {'action':action}
        _save(step/'environment-intent.json',intent)
        result = {'ok':True,'state':'verified','code':codes[index-1],
                  'observation':browser(index),'verification':{'independent_read':True}}
        record = {'action':action,'intent_sha256':digest(intent),'result':result}
        _save(step/'environment-result.json',record)
        write_new(step/'verification.json',result)
        attempts.append({'step':index,'action':action,'result':result})
        hashes.append(digest(record))
    final = {'browser':browser(3),'checks':{'sources_ready':True,'goal_met':True},
        'evidence_sha256':digest(hashes),'artifacts':[],'failures':[],'source_reads':0}
    write_new(run/'observation-04.json',final)
    _save(run/'browser-checkpoint.json',{'version':1,'run':str(run),'config_sha256':digest(config),
        'captured_at':20.0,'browser':final['browser'],
        'content_sha256':sha256(body.encode('utf-8')).hexdigest()})
    outcome = {'ok':True,'state':'verified','code':'objective_verified','engine':'observed_agent',
        'run':str(run),'goal_kind':'interactive','failures':[],'artifacts':[],
        'attempts':attempts,'completed':attempts,'actions':3}
    write_new(run/'outcome.json',outcome)
    return {'base':base,'tasks':folder/'t','session':session,'run':run,'body':body,
            'outcome':outcome,'final':final,'plan':plan}


def deliver(reading, **options):
    return page.deliver_page_office(reading['outcome'],computer_base=reading['base'],
        task_base=reading['tasks'],office_execute=options.pop('office_execute',office_fake),**options)


def inventory(root):
    return {str(path.relative_to(root)):path.read_bytes() for path in root.rglob('*') if path.is_file()}


def test_metadata_binds_complete_saved_source_and_all_bounded_content(reading):
    source = page.verify_page_source(reading['run'],computer_base=reading['base'])
    assert source['source_sha256'] == digest(source['metadata'])
    assert source['metadata']['url'] == reading['plan']['goal']['browser']['target_url']
    assert source['metadata']['config'] == reading['plan']['goal']['browser']
    assert source['metadata']['evidence_sha256']
    steps = page._steps(source)
    assert [step['action'] for step in steps] == ['spreadsheet','document']
    rows = steps[0]['data']['rows'][4:]
    flat = reading['body'].replace('\n',' ').replace('\r',' ').replace('\t',' ')
    assert ''.join(row[3] for row in rows) == flat
    assert rows[0][1] == 0 and rows[-1][2] == len(reading['body'])
    assert all(a[2] == b[1] for a,b in zip(rows,rows[1:]))
    assert ''.join(steps[1]['data']['paragraphs'][5:]) == flat
    assert 'SHA' not in ''.join(steps[1]['data']['paragraphs'])


def test_standard_two_outputs_reuse_and_next_sources_preserve_run(reading):
    before = inventory(reading['run'])
    calls = []
    def execute(spec, root, stop):
        calls.append(spec['kind'])
        return office_fake(spec,root,stop)
    first = deliver(reading,office_execute=execute)
    assert first['ok'], first
    assert first['code'] == 'page_office_verified'
    assert [item['kind'] for item in first['artifacts']] == ['spreadsheet','document']
    report = first['page_office_report']
    for index,(artifact,ref) in enumerate(zip(report['artifacts'],report['refs'])):
        assert prepare_artifact_source(artifact['path'],artifact['sha256'],reading['tasks']) == ref
        target = reading['tasks']/('task-'+str(index+1)*32)
        target.mkdir()
        source = snapshot_artifact_source(ref,target,reading['tasks'])
        assert source['kind'] == ('table' if artifact['kind']=='spreadsheet' else 'document')
    assert deliver(reading,office_execute=execute) == first
    assert calls == ['spreadsheet','document']
    assert inventory(reading['run']) == before
    record = reading['session'].root/'page-deliveries'/reading['run'].stem/'claim.json'
    assert record.is_file()


@pytest.mark.parametrize('target',['checkpoint','step','claim','plan','outcome','observation','session'])
def test_source_byte_changes_refuse_reuse(reading,target):
    first = deliver(reading)
    assert first['ok'],first
    paths = {'checkpoint':reading['run']/'browser-checkpoint.json',
        'step':reading['run']/'step-03'/'verification.json','claim':reading['run']/'claim.json',
        'plan':reading['run'].with_suffix('.json'),'outcome':reading['run']/'outcome.json',
        'observation':reading['run']/'observation-04.json',
        'session':reading['session'].root/'session.json'}
    path = paths[target]
    path.write_bytes(path.read_bytes()+b' ')
    second = deliver(reading,office_execute=lambda *args:pytest.fail('source changed'))
    assert not second['ok'] and second['artifacts'] == []
    assert len(list(reading['tasks'].glob('task-*'))) == 1


@pytest.mark.parametrize('target',['artifact','receipt','report'])
def test_output_changes_refuse_reuse(reading,target):
    first = deliver(reading)
    assert first['ok'],first
    artifact = Path(first['artifacts'][0]['path'])
    path = artifact if target=='artifact' else artifact.parent/'receipt.json'
    if target=='report':
        path = reading['session'].root/'page-deliveries'/reading['run'].stem/'result.json'
    if target=='report':
        value,_ = _load(path)
        value['artifacts'] = []
        _save_value = {'data':value,'sha256':digest(value)}
        put(path,_save_value)
    else:
        path.write_bytes(path.read_bytes()+b' ')
    second = deliver(reading,office_execute=lambda *args:pytest.fail('output changed'))
    assert not second['ok'] and second['artifacts'] == []


def test_unknown_is_terminal_and_preserves_browser_success(reading):
    calls = []
    def unknown(spec,root,stop):
        calls.append(spec['kind'])
        raise RuntimeError('private contents')
    before = inventory(reading['run'])
    first = deliver(reading,office_execute=unknown)
    second = deliver(reading,office_execute=unknown)
    assert first == second and first['state'] == 'unknown' and len(calls)==1
    assert first['artifacts'] == [] and 'private contents' not in json.dumps(first)
    assert inventory(reading['run']) == before
    assert read_record(reading['run']/'outcome.json')['ok'] is True


def test_partial_keeps_verified_excel_and_never_starts_word_on_repeat(reading):
    stopped, calls = [False], []
    def execute(spec,root,stop):
        calls.append(spec['kind'])
        result = office_fake(spec,root,stop)
        stopped[0] = True
        return result
    first = deliver(reading,office_execute=execute,stop=lambda:stopped[0])
    assert first['state']=='partial' and not first['ok']
    assert [item['kind'] for item in first['artifacts']] == ['spreadsheet']
    stopped[0] = False
    second = deliver(reading,office_execute=execute)
    assert first == second and calls==['spreadsheet']
    task = Path(first['page_office_report']['task_root'])
    assert not next(task.glob('workflow-*')).joinpath('s2').exists()


def test_false_input_success_and_missing_checkpoint_cannot_deliver(reading):
    bad = deepcopy(reading['outcome'])
    bad['ok'] = False
    result = page.deliver_page_office(bad,computer_base=reading['base'],task_base=reading['tasks'])
    assert not result['ok'] and not reading['tasks'].exists()
    (reading['run']/'browser-checkpoint.json').unlink()
    result = deliver(reading)
    assert not result['ok'] and result['artifacts']==[] and not reading['tasks'].exists()


def test_claim_without_result_is_unknown_never_replayed(reading):
    first = deliver(reading)
    assert first['ok'],first
    result_path = reading['session'].root/'page-deliveries'/reading['run'].stem/'result.json'
    result_path.unlink()
    repeat = deliver(reading,office_execute=lambda *args:pytest.fail('claimed'))
    assert repeat['state']=='unknown' and repeat['artifacts']==[]


@pytest.mark.parametrize('key,value',[('search_status',503),('content_status',True),
    ('query_verified',False),('content_verified',False),('target_url','https://docs.python.org/3/')])
def test_source_metadata_rejects_unverified_search_or_target(reading,key,value):
    path = reading['run']/'browser-checkpoint.json'
    data,_ = _load(path)
    data['browser']['discovery'][key] = value
    data['browser']['state_id'] = digest({k:v for k,v in data['browser'].items()
                                         if k not in {'state_id','actions'}})
    put(path,{'data':data,'sha256':digest(data)})
    result = deliver(reading)
    assert not result['ok'] and not reading['tasks'].exists()


def test_successful_reading_does_not_require_recovery_metadata(reading):
    from agent_service.interaction_recovery import verify_reading, verify_completed
    value = verify_reading(reading['session'],reading['run'])
    assert value['browser_checkpoint']['browser']['text'] == reading['body']
    with pytest.raises(ComputerError,match='recovery_browser_checkpoint_missing'):
        verify_completed(reading['session'],reading['run'])


def test_cancel_before_claim_has_no_office_effect_or_delivery_claim(reading):
    result = deliver(reading,stop=lambda:True,
        office_execute=lambda *args:pytest.fail('cancelled'))
    assert result['state']=='partial' and result['artifacts']==[]
    assert not reading['tasks'].exists()
    assert not (reading['session'].root/'page-deliveries').exists()


def test_source_change_during_first_step_stops_second_step_and_all_replay(reading):
    calls = []
    def execute(spec,root,stop):
        calls.append(spec['kind'])
        result = office_fake(spec,root,stop)
        path = reading['run']/'observation-04.json'
        path.write_bytes(path.read_bytes()+b' ')
        return result
    first = deliver(reading,office_execute=execute)
    assert not first['ok'] and first['state']=='unknown' and first['artifacts']==[]
    second = deliver(reading,office_execute=execute)
    assert not second['ok'] and calls==['spreadsheet']
    assert read_record(reading['run']/'outcome.json')['ok'] is True


def test_second_unknown_keeps_original_receipt_and_never_replays(reading):
    calls = []
    def execute(spec,root,stop):
        calls.append(spec['kind'])
        if spec['kind']=='document':
            raise RuntimeError('unknown Office response')
        return office_fake(spec,root,stop)
    first = deliver(reading,office_execute=execute)
    assert first['state']=='unknown' and first['artifacts']==[]
    task = Path(first['page_office_report']['task_root'])
    receipt = next(task.glob('workflow-*'))/'s1'/'receipt.json'
    before = receipt.read_bytes()
    assert deliver(reading,office_execute=execute)==first
    assert calls==['spreadsheet','document'] and receipt.read_bytes()==before
