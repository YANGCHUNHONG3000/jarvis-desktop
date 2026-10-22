"""Offline shared-loop integration with actual OOXML edits and durable references.

Only browser/native application boundaries are faked. Sources, preservation,
receipts, claims, candidate IDs and next-task snapshots use production code.
"""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import uuid

from openpyxl import load_workbook
import pytest

from agent_service import computer_agent as engine, computer_interaction as interaction
from agent_service import office_edit, artifact_source, office_source, task_history
from agent_service.computer import ComputerError, ComputerSession, digest, read_record
from agent_service.interaction_panel import build_request
from test_office_edit import word_fixture, sheet_fixture


BROWSER = {'start_url':'https://example.org/', 'allowed_paths':['/', '/result'],
    'headless':True, 'required_text':['Verified browser result'], 'rules':[
        {'id':'alternative','kind':'click','selector':'#alternative','effect':'navigate',
         'expected_text':'Verified browser result'},
        {'id':'query','kind':'fill','selector':'#query','value':'approved public query'},
        {'id':'submit','kind':'click','selector':'#submit','effect':'query',
         'expected_text':'Verified browser result'}]}


class FakeBrowser:
    def __init__(self, config, *, ready=False, unknown=False, **options):
        self.config, self.options = deepcopy(config), options
        self.phase, self.unknown = 2 if ready else 0, unknown
        self.failed, self.locked, self.closed = False, False, 0
        self.effects = []

    def observe(self):
        snap = {'session_id':'offline-owned-browser', 'phase':self.phase,
                'url':'https://example.org/result' if self.phase == 2 else 'https://example.org/',
                'text':'Verified browser result' if self.phase == 2 else 'Search form',
                'locked':self.locked, 'failures':['alternative'] if self.failed else [],
                'checks':{'goal_met':self.phase == 2 and not self.locked}}
        state = digest(snap)
        candidates = []
        if self.phase == 0:
            if not self.failed:
                candidates.append(('browser_click','alternative'))
            candidates.append(('browser_fill','query'))
        elif self.phase == 1:
            candidates.append(('browser_click','submit'))
        snap['state_id'] = state
        snap['actions'] = [] if self.locked else [
            {'kind':kind, 'rule_id':rule, 'control_id':rule+'-actual-dom', 'state_id':state}
            for kind, rule in candidates]
        return snap

    def execute(self, action):
        assert action in self.observe()['actions']
        self.effects.append(deepcopy(action))
        if self.unknown:
            self.locked = True
            result = {'ok':False,'state':'unknown','code':'browser_effect_unknown'}
        elif action['rule_id'] == 'alternative':
            self.failed = True
            result = {'ok':False,'state':'failed','code':'browser_target_mismatch'}
        else:
            self.phase += 1
            result = {'ok':True,'state':'verified','code':'browser_fixture_verified'}
        return dict(result, observation=self.observe())

    def close(self):
        self.closed += 1


class CurrentChoiceClient:
    last_call = {'state':'offline_test_only'}
    def __init__(self):
        self.calls = []
    def complete(self, system, user, **options):
        value = json.loads(user)
        self.calls.append(value)
        # Return a freshly observed production candidate ID, never a fake ID.
        return {'action_id':value['choices'][0]['id']}


@pytest.fixture
def ctx(monkeypatch):
    # Keep nested hash-named run paths below the Windows 260-character limit.
    with tempfile.TemporaryDirectory(prefix='jci-') as folder:
        root = Path(folder)
        word, sheet = root/'original.docx', root/'original.xlsx'
        word_fixture(word)
        sheet_fixture(sheet)
        session = ComputerSession.create(root/'c')
        calls, browsers, events = [], [], []
        stopped = [False]
        def native(path, spec, observed, check):
            check()
            return {'progid':'offline-test-only', 'changes':deepcopy(spec['changes'])}
        monkeypatch.setattr(office_edit, '_native_readback', native)
        def execute(spec, output, source_root, *, stop, deadline):
            calls.append(deepcopy(spec))
            events.append(('office',spec['kind']))
            return office_edit.execute_edit(spec, output, source_root, stop)
        def browser_factory(config, **options):
            value = FakeBrowser(config, **options)
            browsers.append(value)
            return value
        yield {'root':root,'word':word,'sheet':sheet,'session':session,
               'calls':calls,'browsers':browsers,'events':events,'stopped':stopped,
               'execute':execute,'browser_factory':browser_factory,'task_base':root/'t'}


def edits(ctx, kinds=('document','spreadsheet')):
    return [({'path':str(ctx['word']), 'changes':[
        {'paragraph_index':1,'before':'预算待确认','after':'预算已确认'}]}
        if kind == 'document' else {'path':str(ctx['sheet']), 'changes':[
        {'sheet':'预算','cell':'B2','before':10,'after':15}]}) for kind in kinds]


def prepare(ctx, *, browser=BROWSER, kinds=('document','spreadsheet')):
    return interaction.prepare(ctx['session'], '浏览器查询后核准现有文件副本',
                               browser=deepcopy(browser), edits=edits(ctx,kinds))


def run(ctx, plan, **options):
    defaults = {'client':CurrentChoiceClient(), 'browser_factory':ctx['browser_factory'],
                'office_execute':ctx['execute'], 'task_base':ctx['task_base'],
                'stop':lambda:ctx['stopped'][0]}
    defaults.update(options)
    return engine.execute(ctx['session'], plan, approval=plan['sha256'], **defaults)


def next_root(ctx):
    return artifact_source.prepare_artifact_task_root(
        ctx['task_base']/('task-'+uuid.uuid4().hex), ctx['task_base'])


def test_same_observed_engine_browser_before_office_real_xml_refs_and_next_word_snapshot(ctx):
    originals = [ctx['word'].read_bytes(), ctx['sheet'].read_bytes()]
    plan = prepare(ctx)
    client = CurrentChoiceClient()
    result = run(ctx,plan,client=client)
    assert result['ok'] and result['engine'] == 'observed_agent'
    assert result['code'] == 'objective_verified'
    assert [a['action']['kind'] for a in result['attempts']] == [
        'browser_click','browser_fill','browser_click','office_edit','office_edit']
    assert result['attempts'][0]['result']['state'] == 'failed'
    assert len(client.calls) == 2  # alternative selection and choice between two edits
    assert all(not any(c['action']['kind']=='office_edit' for c in data['choices'])
               for data in client.calls if not data['observation']['checks']['sources_ready'])
    assert len(ctx['calls']) == 2 and ctx['browsers'][0].closed == 1
    assert [ctx['word'].read_bytes(),ctx['sheet'].read_bytes()] == originals
    assert len(result['artifacts']) == 2
    assert read_record(Path(result['run'])/'step-01'/'decision.json')['method'] == 'model_choice'
    for item in result['artifacts']:
        ref = artifact_source.prepare_artifact_source(item['path'],item['sha256'],ctx['task_base'])
        assert ref['kind'] == 'office_edit'
        if item['kind'] == 'document':
            target = next_root(ctx)
            source = artifact_source.snapshot_artifact_source(ref,target,ctx['task_base'])
            assert source['sha256'] == item['sha256']
            assert source['observed']['paragraphs'][1] == '预算已确认'
            assert read_record(target/'sources'/'provenance.json') == ref
        else:
            book = load_workbook(item['path'],data_only=False)
            try:
                assert book['预算']['B2'].value == 15
                assert book['预算']['B4'].value == '=SUM(B2:B3)'
                assert book['预算']['B2'].font.bold
                assert book['保留页']['B2'].value == 123
            finally:
                book.close()


def test_multi_sheet_formula_edit_is_verified_and_next_snapshot_keeps_all_sheets(ctx,monkeypatch):
    from agent_service import workbook_source as ws, workbook_source_host
    from test_workbook_source import receipt
    def native(path,directory,stop,deadline):
        value = receipt(path)
        office_source._write_json_new(directory/'source-native.json',value)
        return value
    monkeypatch.setattr(workbook_source_host,'native_snapshot_supervised',native)
    result = run(ctx,prepare(ctx,browser=None,kinds=('spreadsheet',)))
    assert result['ok']
    item = result['artifacts'][0]
    ref = artifact_source.prepare_artifact_source(item['path'],item['sha256'],ctx['task_base'])
    target = next_root(ctx)
    public = artifact_source.snapshot_artifact_source(ref,target,ctx['task_base'])
    assert public['kind'] == 'workbook' and public['coverage']['sheet_count'] == 2
    assert public['observed']['sheets'][0]['values'][3][1] == 35
    assert public['observed']['sheets'][0]['formulas'][0]['expression'] == '=SUM(B2:B3)'
    assert public['observed']['sheets'][1]['values'][1][1] == 123
    assert ws.verify_workbook_source(target) == public


@pytest.mark.parametrize('target',['original','snapshot'])
def test_source_modified_after_approval_refused_before_any_edit(ctx,target):
    plan = prepare(ctx,browser=None,kinds=('document',))
    path = ctx['word'] if target == 'original' else Path(plan['goal']['edits'][0]['source_root'])/'source.docx'
    path.write_bytes(path.read_bytes()+b'changed')
    result = run(ctx,plan)
    assert not result['ok'] and result['actions'] == 0
    assert not result['artifacts'] and not ctx['calls']


@pytest.mark.parametrize('kind',['document','spreadsheet'])
def test_before_mismatch_refused_during_prepare_with_no_native_effect(ctx,kind):
    request = edits(ctx,(kind,))
    request[0]['changes'][0]['before'] = 'incorrect' if kind=='document' else 999
    with pytest.raises(office_edit.EditError,match='edit_before_mismatch'):
        interaction.prepare(ctx['session'],'拒绝旧值不匹配',browser=None,edits=request)
    assert not ctx['calls'] and not list((ctx['session'].root/'goals').glob('*.json'))


@pytest.mark.parametrize('target',['artifact','receipt','manifest'])
def test_completed_artifact_or_evidence_tamper_refused_on_next_handoff(ctx,target):
    result = run(ctx,prepare(ctx,browser=None,kinds=('document',)))
    assert result['ok']
    item = result['artifacts'][0]
    ref = artifact_source.prepare_artifact_source(item['path'],item['sha256'],ctx['task_base'])
    artifact = Path(item['path'])
    paths = {'artifact':artifact,'receipt':artifact.parent/'native-edit-receipt.json',
             'manifest':artifact.parent/'edit-manifest.json'}
    path = paths[target]
    if target == 'manifest':
        changed = read_record(path)
        changed['data']['spec']['changes'][0]['after'] = 'tampered approval'
        path.write_text(json.dumps(changed,ensure_ascii=False),encoding='utf-8')
    else:
        path.write_bytes(path.read_bytes()+b' ')
    with pytest.raises(ValueError):
        artifact_source.snapshot_artifact_source(ref,next_root(ctx),ctx['task_base'])
    assert len(ctx['calls']) == 1  # revalidation never replays native edit


def test_browser_unknown_stops_shared_loop_no_edit_or_replay_and_closes(ctx):
    def factory(config, **options):
        value = FakeBrowser(config,unknown=True,**options)
        ctx['browsers'].append(value)
        return value
    result = run(ctx,prepare(ctx),browser_factory=factory)
    assert result['state'] == 'unknown' and not result['ok']
    assert len(result['attempts']) == 1
    assert len(ctx['browsers'][0].effects) == 1 and ctx['browsers'][0].closed == 1
    assert not ctx['calls'] and not result['artifacts']


def test_artifact_tamper_between_cycles_stops_next_edit_and_suppresses_stale_handoff(ctx):
    plan = prepare(ctx,browser=None)
    run_root = ctx['session'].root/'goals'/(plan['sha256']+'.run')
    tampered = [False]
    def tamper_after_persisted_result():
        if not tampered[0] and (run_root/'step-01'/'environment-result.json').is_file():
            artifact = next(ctx['task_base'].glob('task-*/edit/edited-document.docx'))
            artifact.write_bytes(artifact.read_bytes()+b'changed after verified step')
            tampered[0] = True
        return False
    result = run(ctx,plan,stop=tamper_after_persisted_result)
    assert tampered[0] and not result['ok']
    assert len(ctx['calls']) == len(result['attempts']) == 1
    assert not result['artifacts']


def test_native_unknown_not_replayed_and_own_browser_closed(ctx):
    def factory(config, **options):
        value = FakeBrowser(config,ready=True,**options)
        ctx['browsers'].append(value)
        return value
    calls = []
    def unknown(spec, output, source_root, **options):
        calls.append(deepcopy(spec))
        return {'ok':False,'state':'unknown','code':'offline_native_unknown'}
    result = run(ctx,prepare(ctx),browser_factory=factory,office_execute=unknown)
    assert result['state'] == 'unknown' and len(calls)==1
    assert not result['artifacts'] and ctx['browsers'][0].closed == 1


def test_cancel_after_verified_first_edit_keeps_partial_reference_and_closes_browser(ctx):
    def factory(config, **options):
        value = FakeBrowser(config,ready=True,**options)
        ctx['browsers'].append(value)
        return value
    def execute(spec, output, source_root, **options):
        value = ctx['execute'](spec,output,source_root,**options)
        assert value['ok']
        ctx['stopped'][0] = True
        return value
    result = run(ctx,prepare(ctx),browser_factory=factory,office_execute=execute)
    assert result['state'] == 'partial' and result['code'] == 'goal_cancelled'
    assert len(result['completed']) == len(result['artifacts']) == len(ctx['calls']) == 1
    assert ctx['browsers'][0].closed == 1
    item = result['artifacts'][0]
    assert item['kind'] == 'document'
    ref = artifact_source.prepare_artifact_source(item['path'],item['sha256'],ctx['task_base'])
    source = artifact_source.snapshot_artifact_source(ref,next_root(ctx),ctx['task_base'])
    assert source['observed']['paragraphs'][1] == '预算已确认'


def gui_values(**changes):
    value = {k:'' for k in ('url','paths','query','expected','input','button','word','paragraph',
                            'word_before','word_after','excel','sheet','cell','excel_before','excel_after')}
    value.update(changes)
    return value


@pytest.mark.parametrize('values',[
    gui_values(), gui_values(url='file:///secret',query='x',expected='x',input='#q',button='#s'),
    gui_values(url='https://example.org/',query='',expected='x',input='#q',button='#s'),
    gui_values(excel='selected.xlsx',sheet='预算',cell='B2',excel_before='[]',excel_after='2'),
    gui_values(word='selected.docx',paragraph='not a number',word_before='before',word_after='after'),
])
def test_gui_build_request_invalid_inputs_refused_without_effect(values):
    with pytest.raises((ValueError,TypeError)):
        build_request(values)


@pytest.mark.parametrize('kind',['delete','shell',None,[]])
def test_unsupported_goal_mode_never_becomes_interaction_plan(kind):
    with pytest.raises(ComputerError):
        interaction.validate_goal({'kind':kind,'browser':None,'edits':[]})


def test_gui_zero_paragraph_is_refused_before_prepare_or_execution(ctx):
    request = build_request(gui_values(word=str(ctx['word']),paragraph='0',
                                      word_before='预算待确认',word_after='预算已确认'))
    with pytest.raises(office_edit.EditError):
        interaction.prepare(ctx['session'],'无效段落序号',**request)
    assert not ctx['calls']


def test_edited_history_inspects_without_worker_metadata_and_supplies_next_task_reference(ctx):
    result = run(ctx,prepare(ctx,browser=None,kinds=('document',)))
    assert result['ok']
    item = result['artifacts'][0]
    output = Path(item['path']).parent
    task = output.parent
    assert not (output/'worker-outcome.json').exists()
    before = {str(p):p.read_bytes() for p in task.rglob('*') if p.is_file()}
    listed = task_history.list_tasks(ctx['task_base'])
    assert listed[0]['kind'] == 'office_edit'
    inspected = task_history.inspect_task(task,ctx['task_base'])
    assert inspected['state'] == 'succeeded' and inspected['kind'] == 'office_edit'
    assert inspected['resume']['eligible'] is False
    assert inspected['artifacts'][0]['path'] == item['path']
    assert before == {str(p):p.read_bytes() for p in task.rglob('*') if p.is_file()}
    selected = inspected['artifacts'][0]
    ref = artifact_source.prepare_artifact_source(selected['path'],selected['sha256'],ctx['task_base'])
    source = artifact_source.snapshot_artifact_source(ref,next_root(ctx),ctx['task_base'])
    assert source['observed']['paragraphs'][1] == '预算已确认'
    assert len(ctx['calls']) == 1


@pytest.mark.parametrize('target',['receipt','manifest'])
def test_edited_history_tamper_needs_review_and_never_exposes_artifacts(ctx,target):
    result = run(ctx,prepare(ctx,browser=None,kinds=('document',)))
    output = Path(result['artifacts'][0]['path']).parent
    if target == 'receipt':
        path = output/'native-edit-receipt.json'
        path.write_bytes(path.read_bytes()+b' ')
    else:
        path = output/'edit-manifest.json'
        changed = read_record(path)
        changed['data']['spec']['changes'][0]['after'] = 'tampered history'
        path.write_text(json.dumps(changed,ensure_ascii=False),encoding='utf-8')
    inspected = task_history.inspect_task(output.parent,ctx['task_base'])
    assert inspected['state'] == 'needs_review'
    assert inspected['artifacts'] == [] and inspected['resume']['eligible'] is False
    assert len(ctx['calls']) == 1


def test_prior_base_edited_layout_cannot_bypass_verified_source_route(ctx):
    result = run(ctx,prepare(ctx,browser=None,kinds=('document',)))
    item = result['artifacts'][0]
    other_base = ctx['root']/'different-task-base'
    other_base.mkdir()
    assert not Path(item['path']).is_relative_to(other_base)
    with pytest.raises(artifact_source.ArtifactSourceError,match='source_generated_requires_reference'):
        artifact_source.require_external_source_file(item['path'],other_base)
