"""Real planning/routing/recovery proofs, with only native Office I/O faked."""
from copy import deepcopy
from hashlib import sha256
import importlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import uuid
import zipfile

from openpyxl import Workbook
import pytest

from agent_service import (office_task_chain as chain, office_task_chain_edit as editing,
                           office_intent, office_scope, office_edit, office_handoff,
                           office_workflow, daily_task_routing, office_bundle, computer_agent)
from agent_service.computer import ComputerSession, digest, write_new, read_record
from test_artifact_source import fake
from test_computer_interaction import CurrentChoiceClient

TABLE = {'headers': ['项目', '值'], 'rows': [['甲', '三']]}
SHEET = {'kind': 'spreadsheet', 'title': '表', **TABLE, 'sum_columns': []}
DOC = {'kind': 'document', 'title': '报告', 'paragraphs': ['范围'], 'table': TABLE}
INSTRUCTION = '将两份文件表头“值”都改成“内容”，其他不变。'


def rich_fake(spec, root, stop):
    if spec['kind'] == 'document':
        result = fake(spec, root, stop)
        path = Path(result['artifact'])
        with zipfile.ZipFile(path, 'a') as package:
            package.writestr('[Content_Types].xml', '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/></Types>')
        result['sha256'] = sha256(path.read_bytes()).hexdigest()
        return result
    from agent_service import office
    path = root / 'spreadsheet.xlsx'
    book = Workbook(); sheet = book.active; sheet.title = 'Data'
    sheet.append([spec['title']]); sheet.merge_cells(start_row=1, start_column=1, end_row=1,
                                                   end_column=len(spec['headers']))
    sheet.append(spec['headers'])
    for row in spec['rows']:
        sheet.append(row)
    book.save(path); book.close()
    actual = office.verify_office_file(path, spec)
    return {'ok': True, 'state': 'succeeded', 'artifact': str(path),
            'sha256': sha256(path.read_bytes()).hexdigest(), 'observed': office._public_observation(spec, actual),
            'app': {'progid': 'offline-only', 'name': 'Offline', 'version': '1'},
            'application_verified': True, 'file_verified': True}


@pytest.fixture
def local(monkeypatch):
    with tempfile.TemporaryDirectory(prefix='jce-') as temporary:
        root = Path(temporary).resolve(); base = root / 't'; base.mkdir()
        task = base / ('task-' + uuid.uuid4().hex); task.mkdir()
        workflow = task / ('workflow-' + uuid.uuid4().hex)
        receipt = office_workflow.run_office_workflow([
            {'id': 's1', 'action': 'spreadsheet', 'data': SHEET},
            {'id': 's2', 'action': 'document', 'data': DOC}], workflow, execute=rich_fake)
        assert receipt['ok']
        artifacts = [{'step': key, 'path': r['artifact'], 'sha256': r['sha256']}
                     for key, r in receipt['receipts'].items()]
        write_new(task / 'result.json', {'ok': True, 'state': 'succeeded', 'kind': 'office',
            'root': str(task), 'workflow_root': str(workflow), 'completed': receipt['completed'], 'artifacts': artifacts})
        records = [{k: a[k] for k in ('path', 'sha256')} for a in artifacts]
        session = ComputerSession.create(root / 'c')
        original_select, original_verify = office_handoff.select_outputs, office_handoff.verify_references
        monkeypatch.setattr(office_handoff, 'select_outputs', lambda records, *, base=base:
                            original_select(records, base=base))
        monkeypatch.setattr(office_handoff, 'verify_references', lambda files, refs, *, base=base:
                            original_verify(files, refs, base=base))
        monkeypatch.setattr(office_bundle, 'BASE', session.root.parent)
        store = root / 'chains'
        payload = {'draft': '', 'records': records, 'source': None}
        ident = chain.create(store, [INSTRUCTION, '逐格核对全部新成果并生成Excel和Word'], payload, base, session.root.parent)
        refs = chain._refs(payload, base)
        class RouteClient:
            def complete(self, *args, **kwargs):
                return {'route': 'office_edit', 'source_ids': ['S1', 'S2'], 'reason': '两份都编辑'}
        routed = daily_task_routing.propose(session, INSTRUCTION, refs, task_base=base, client=RouteClient())
        state = chain.bind_preview(store, ident, 0, session, routed, refs)
        assert state['status'] == 'needs_scope'
        catalog = office_scope.inspect_files(session, [r['path'] for r in records], artifact_refs=refs)
        selected = office_scope.selection_from_indices(catalog, [
            (i, j) for i, f in enumerate(catalog['files']) for j, row in enumerate(f['entries']) if row['value'] == '值'])
        office_payload = {'draft': INSTRUCTION, 'records': records, 'scope': selected['scope']}
        yield SimpleNamespace(root=root, base=base, session=session, store=store, ident=ident,
                              refs=refs, records=records, routed=routed, office_payload=office_payload)


class Planner:
    last_call = {'state': 'offline_only'}
    def __init__(self, *, kind='edits', first_only=False):
        self.kind, self.first_only = kind, first_only
    def complete(self, system, user, **kwargs):
        if self.kind != 'edits':
            return {'kind': self.kind, 'reason': '请澄清'}
        context = json.loads(user)
        changes = [{'target': row['target'], 'after': '内容'} for f in context['files']
                   for row in f['content'] if row.get('target') and row['value'] == '值']
        return {'kind': 'edits', 'changes': changes[:1] if self.first_only else changes}


def planning(s, *, payload=None, instruction=None, client=None, session=None):
    payload = payload or s.office_payload
    return office_intent.propose(session or s.session, instruction or payload['draft'],
        [r['path'] for r in payload['records']], client=client or Planner(),
        artifact_refs=s.refs if len(payload['records']) == len(s.refs) else s.refs[:len(payload['records'])],
        scope=payload['scope'])


def bind(s, result=None, **kwargs):
    result = result or planning(s)
    state = chain.bind_edit_preview(s.store, s.ident, 0, s.session, result['plan'], result,
                                    kwargs.get('payload', s.office_payload))
    return state, result


def execute(s, plan, monkeypatch):
    def native(path, spec, observed, check):
        check()
        return {'progid': 'KWPS.Application' if spec['kind'] == 'document' else 'KET.Application',
                'name': 'OFFLINE FAKE', 'version': '1'}
    monkeypatch.setattr(office_edit, '_native_readback', native)
    def offline(spec, output, source_root, *, stop, deadline):
        result = office_edit.execute_edit(spec, output, source_root, stop)
        for name, value in {'worker-request.json': {'spec': spec, 'source_root': source_root},
                            'worker-outcome.json': result, 'worker-ready.json': {'ready': True},
                            'worker-gated.json': {'gated': True}}.items():
            write_new(output / name, value)
        return result
    return computer_agent.execute(s.session, plan, approval=plan['sha256'], client=CurrentChoiceClient(),
                                   task_base=s.base, office_execute=offline)


def test_needs_scope_route_remains_bound_without_dispatch(local):
    s = local
    state = chain.inspect(s.store, s.ident)
    assert state['status'] == 'needs_scope' and state['run'] is None
    assert state['binding']['routed'] == s.routed and state['binding']['refs'] == s.refs
    assert chain.advance(s.store, s.ident) == state


def clarified_planning(s, *, original=INSTRUCTION, selection=None):
    from agent_service import office_clarification as clarification
    initial = clarification.begin(s.session, original, [r['path'] for r in s.records],
        artifact_refs=s.refs, scope=s.office_payload['scope'], client=Planner(kind='clarify'))
    pending = clarification.load(s.session, initial)
    result = clarification.continue_attempt(s.session, pending, '把值表头明确改为内容',
                                           selection=selection, client=Planner())
    item = clarification.load(s.session, result)
    payload = {'draft':item['instruction'], 'scope':item['scope'], 'records':[
        {'path':p,'sha256':part['sha256']} for p,part in zip(item['files'],item['scope'])]}
    return result, payload


def test_clarified_chain_binds_composed_draft_and_keeps_original_manifest(local):
    s = local
    result, payload = clarified_planning(s)
    folder = s.store / ('chain-' + s.ident)
    before = (folder / 'manifest.json').read_bytes()
    state, _ = bind(s, result, payload=payload)
    assert state['status'] == 'edit_preview' and state['payload']['draft'] == INSTRUCTION
    assert state['edit_payload'] == payload and state['edit_plan']['instruction'] == payload['draft']
    saved = read_record(folder / 'step-0-edit.json')
    assert 'continuation.json' in saved['proofs']
    assert (folder / 'manifest.json').read_bytes() == before
    assert importlib.reload(chain).inspect(s.store, s.ident) == state


def test_clarified_chain_can_explicitly_narrow_original_files(local):
    s = local
    selection = {'files':[s.records[0]['path']], 'artifact_refs':s.refs[:1],
                 'scope':s.office_payload['scope'][:1]}
    result, payload = clarified_planning(s, selection=selection)
    state, _ = bind(s, result, payload=payload)
    assert state['edit_payload']['records'] == s.records[:1]
    assert len(state['edit_plan']['goal']['edits']) == 1


def test_clarified_chain_executes_and_advances_only_this_stage(local, monkeypatch):
    s = local
    result, payload = clarified_planning(s)
    state, _ = bind(s, result, payload=payload)
    outcome = execute(s, result['plan'], monkeypatch)
    assert outcome['ok']
    advanced = chain.advance(s.store, s.ident)
    assert advanced['index'] == 1 and advanced['status'] == 'ready'
    assert advanced['payload']['draft'] == '逐格核对全部新成果并生成Excel和Word'
    assert chain.advance(s.store, s.ident) == advanced


def test_clarification_for_other_original_instruction_cannot_bind_chain(local):
    s = local
    result, payload = clarified_planning(s, original='另一条不同任务')
    with pytest.raises(chain.OfficeTaskChainError, match='clarification_changed'):
        bind(s, result, payload=payload)
    assert chain.inspect(s.store, s.ident)['status'] == 'needs_scope'


def test_changed_clarification_proof_blocks_chain_restart(local):
    s = local
    result, payload = clarified_planning(s)
    bind(s, result, payload=payload)
    path = Path(result['office_result']['record']) / 'continuation.json'
    value = read_record(path); value['answers'] = ['伪造的新答案']
    path.write_text(json.dumps(value), encoding='utf-8')
    with pytest.raises(chain.OfficeTaskChainError): chain.inspect(s.store, s.ident)


def test_clarified_plan_cannot_replace_started_stage(local):
    s = local
    state, _ = bind(s)
    Path(state['run']).mkdir()
    result, payload = clarified_planning(s)
    with pytest.raises(chain.OfficeTaskChainError, match='preview_conflict'):
        bind(s, result, payload=payload)


def test_real_edit_planning_proof_and_cross_restart(local):
    s = local
    state, result = bind(s)
    assert state['status'] == 'edit_preview' and state['edit_plan'] == result['plan']
    assert state['edit_payload'] == s.office_payload and state['edit_revision'] == 0
    assert state['run'].endswith(result['plan']['sha256'] + '.run')
    assert importlib.reload(chain).inspect(s.store, s.ident) == state
    assert chain.bind_edit_preview(s.store, s.ident, 0, s.session, result['plan'], result, s.office_payload) == state


def test_edit_success_normalizes_exact_native_outputs_and_does_not_replay(local, monkeypatch):
    s = local
    state, result = bind(s)
    outcome = execute(s, result['plan'], monkeypatch)
    assert outcome['ok'] is True
    assert chain.inspect(s.store, s.ident)['status'] == 'waiting_result'
    advanced = chain.advance(s.store, s.ident)
    assert advanced['index'] == 1 and advanced['status'] == 'ready'
    assert advanced['payload']['records'] == [{'path': a['path'], 'sha256': a['sha256']} for a in outcome['artifacts']]
    assert advanced['payload']['source'] is None and advanced['edit_plan'] is None
    assert advanced['completed_office'] == [{'index': 0, 'records': advanced['payload']['records']}]
    assert chain.advance(s.store, s.ident) == advanced
    assert chain.bind_edit_preview(s.store, s.ident, 0, s.session, result['plan'], result, s.office_payload) == advanced


def test_saved_context_includes_unchanged_selected_file(local):
    s = local
    state, result = bind(s, planning(s, client=Planner(first_only=True)))
    assert len(result['plan']['goal']['edits']) == 1
    assert len(state['edit_payload']['records']) == 2
    assert len(read_record(Path(result['office_result']['record']) / 'context.json')['files']) == 2


@pytest.mark.parametrize('case', ['draft', 'scope', 'files', 'plan', 'request', 'context', 'response', 'outcome',
                                  'references', 'selection', 'source_snapshot'])
def test_wrong_task_scope_or_planning_proof_cannot_bind(local, case):
    s = local; result = planning(s); payload = deepcopy(s.office_payload)
    folder = Path(result['office_result']['record'])
    if case == 'draft': payload['draft'] = '另一个要求'
    elif case == 'scope': payload['scope'][0]['locations'][0] = {'sheet': 'Data', 'cell': 'A2'}
    elif case == 'files': payload['records'].reverse(); payload['scope'].reverse()
    elif case == 'plan': result['plan']['instruction'] = '另一个要求'
    else:
        path = {'request': folder / 'request.json', 'context': folder / 'context.json',
                'response': folder / 'response.json', 'outcome': folder / 'outcome.json',
                'references': folder / 'artifact-references.json', 'selection': folder / 'selection.json',
                'source_snapshot': Path(result['plan']['goal']['edits'][0]['source_root']) / 'source.xlsx'}[case]
        path.write_text('{}', encoding='utf-8')
    with pytest.raises(chain.OfficeTaskChainError):
        chain.bind_edit_preview(s.store, s.ident, 0, s.session, result['plan'], result, payload)
    assert chain.inspect(s.store, s.ident)['status'] == 'needs_scope'


@pytest.mark.parametrize('kind', ['clarify', 'unsupported'])
def test_clarification_does_not_rewrite_original_chain(local, kind):
    s = local; result = planning(s, client=Planner(kind=kind))
    with pytest.raises(chain.OfficeTaskChainError):
        chain.bind_edit_preview(s.store, s.ident, 0, s.session, None, result, s.office_payload)
    assert chain.inspect(s.store, s.ident)['payload']['draft'] == INSTRUCTION


def test_same_sources_wrong_authored_instruction_cannot_claim_stage(local):
    s = local; result = planning(s, instruction='把同一表头改成内容')
    with pytest.raises(chain.OfficeTaskChainError):
        bind(s, result)


def test_cross_session_same_instruction_and_sources_cannot_claim_route(local):
    s = local; other = ComputerSession.create(s.session.root.parent)
    result = planning(s, session=other)
    with pytest.raises(chain.OfficeTaskChainError, match='session_changed'):
        chain.bind_edit_preview(s.store, s.ident, 0, other, result['plan'], result, s.office_payload)


def test_refresh_cannot_reuse_a_started_exact_plan(local):
    s = local; state, result = bind(s)
    Path(state['run']).mkdir()
    with pytest.raises(chain.OfficeTaskChainError, match='already_started'):
        chain.bind_edit_refresh(s.store, s.ident, 0, s.session, result['plan'])


def test_edit_payload_cannot_drop_a_routed_file(local):
    s = local; payload = deepcopy(s.office_payload)
    payload['records'] = payload['records'][:1]; payload['scope'] = payload['scope'][:1]
    result = planning(s, payload=payload)
    with pytest.raises(chain.OfficeTaskChainError):
        bind(s, result, payload=payload)


def test_two_previews_cannot_replace_the_first_binding(local):
    s = local
    state, _ = bind(s)
    other = planning(s)
    with pytest.raises(chain.OfficeTaskChainError, match='preview_conflict'):
        bind(s, other)
    assert chain.inspect(s.store, s.ident) == state


def test_refresh_local_same_goal_retains_original_proof_and_revision(local):
    s = local; state, result = bind(s)
    from agent_service.office_edit_adapter import refresh_plan
    fresh = refresh_plan(s.session, result['plan'])
    refreshed = chain.bind_edit_refresh(s.store, s.ident, 0, s.session, fresh)
    assert refreshed['edit_revision'] == 1 and refreshed['edit_plan'] == fresh
    assert refreshed['edit_payload'] == state['edit_payload']
    assert chain.bind_edit_refresh(s.store, s.ident, 0, s.session, fresh) == refreshed
    newer = refresh_plan(s.session, fresh)
    assert chain.bind_edit_refresh(s.store, s.ident, 0, s.session, newer)['edit_revision'] == 2
    folder = s.store / ('chain-' + s.ident)
    assert (folder / 'step-0-edit.json').is_file()
    assert len(list(folder.glob('step-0-edit-refresh-*.json'))) == 2
    assert chain.inspect(s.store, s.ident)['edit_plan'] == newer


@pytest.mark.parametrize('case', ['instruction', 'goal', 'session', 'old_started', 'new_started'])
def test_refresh_rejects_semantic_changes_and_unknown_run(local, case):
    s = local; _, result = bind(s)
    from agent_service.computer_goals import _save_goal
    value = {k: deepcopy(v) for k, v in result['plan'].items() if k != 'sha256'}
    value['prepared_at'] += 1
    session = s.session
    if case == 'instruction': value['instruction'] = '新要求'
    elif case == 'goal': value['goal']['edits'][0]['spec']['changes'][0]['after'] = '另一个值'
    elif case == 'session': session = ComputerSession.create(s.session.root.parent)
    fresh = _save_goal(session, value)
    if case in {'old_started', 'new_started'}:
        old = result['plan'] if case == 'old_started' else fresh
        (s.session.root / 'goals' / (old['sha256'] + '.run')).mkdir()
    with pytest.raises(chain.OfficeTaskChainError):
        chain.bind_edit_refresh(s.store, s.ident, 0, session, fresh)
    assert not list((s.store / ('chain-' + s.ident)).glob('*refresh*.json'))


def test_old_run_created_after_refresh_blocks_inspection(local):
    s = local; _, result = bind(s)
    from agent_service.office_edit_adapter import refresh_plan
    fresh = refresh_plan(s.session, result['plan'])
    chain.bind_edit_refresh(s.store, s.ident, 0, s.session, fresh)
    (s.session.root / 'goals' / (result['plan']['sha256'] + '.run')).mkdir()
    with pytest.raises(chain.OfficeTaskChainError, match='refresh_changed'):
        chain.inspect(s.store, s.ident)


@pytest.mark.parametrize('outcome', [None, {'ok': False, 'state': 'unknown'}, {'ok': False, 'state': 'failed'}])
def test_started_or_failed_edit_never_advances_or_replays(local, outcome):
    s = local; before, _ = bind(s); run = Path(before['run']); run.mkdir()
    if outcome is not None:
        write_new(run / 'outcome.json', outcome)
    state = chain.advance(s.store, s.ident)
    assert state['index'] == 0 and state['edit_payload'] == s.office_payload
    assert state['status'] == ('waiting_result' if outcome is None else 'blocked')
    assert chain.advance(s.store, s.ident) == state


def test_claimed_success_without_real_native_receipts_is_rejected(local):
    s = local; state, _ = bind(s); run = Path(state['run']); run.mkdir()
    write_new(run / 'outcome.json', {'ok': True, 'state': 'verified', 'artifacts': []})
    with pytest.raises(chain.OfficeTaskChainError):
        chain.advance(s.store, s.ident)


def test_completed_edit_revalidates_original_model_response(local, monkeypatch):
    s = local; _, result = bind(s)
    assert execute(s, result['plan'], monkeypatch)['ok']
    chain.advance(s.store, s.ident)
    (Path(result['office_result']['record']) / 'response.json').write_text('{}')
    with pytest.raises(chain.OfficeTaskChainError):
        chain.inspect(s.store, s.ident)


def test_bind_preview_accepts_only_the_exact_edit_subset(local):
    s = local
    changed = deepcopy(s.routed); changed['edit']['source_refs'].reverse()
    with pytest.raises(chain.OfficeTaskChainError):
        chain.bind_preview(s.store, s.ident, 0, s.session, changed, s.refs)


def test_route_may_explicitly_select_an_ordered_subset_before_scope(local):
    s = local; instruction = '只将Word表头“值”改成“内容”，其他不变。'
    ident = chain.create(s.store, [instruction, '继续使用新成果'],
                         {'draft': '', 'records': s.records, 'source': None}, s.base, s.session.root.parent)
    class SelectWord:
        def complete(self, *args, **kwargs):
            return {'route': 'office_edit', 'source_ids': ['S2'], 'reason': '明确仅Word'}
    routed = daily_task_routing.propose(s.session, instruction, s.refs, task_base=s.base, client=SelectWord())
    state = chain.bind_preview(s.store, ident, 0, s.session, routed, s.refs)
    assert state['binding']['refs'] == s.refs
    payload = {'draft': instruction, 'records': s.records[1:], 'scope': s.office_payload['scope'][1:]}
    result = office_intent.propose(s.session, instruction, [s.records[1]['path']],
        client=Planner(), artifact_refs=s.refs[1:], scope=payload['scope'])
    bound = chain.bind_edit_preview(s.store, ident, 0, s.session, result['plan'], result, payload)
    assert bound['status'] == 'edit_preview' and bound['edit_payload']['records'] == s.records[1:]


def test_refresh_does_not_reuse_an_older_preview_revision(local):
    s = local; _, result = bind(s)
    from agent_service.office_edit_adapter import refresh_plan
    fresh = refresh_plan(s.session, result['plan'])
    chain.bind_edit_refresh(s.store, s.ident, 0, s.session, fresh)
    with pytest.raises(chain.OfficeTaskChainError, match='refresh_changed'):
        chain.bind_edit_refresh(s.store, s.ident, 0, s.session, result['plan'])
