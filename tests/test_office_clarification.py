"""Bound Office questions and durable continuation, offline real OOXML sources."""
import json

import pytest

from agent_service import office_clarification as core, office_intent as intent
from agent_service.computer import ComputerSession, ComputerError, read_record
from test_office_intent import Client, valid_response
from test_office_edit import word_fixture, sheet_fixture
from test_office_handoff import selected


@pytest.fixture
def ctx(tmp_path):
    word, sheet = tmp_path / 'brief.docx', tmp_path / 'budget.xlsx'
    word_fixture(word)
    sheet_fixture(sheet)
    return ComputerSession.create(tmp_path / 'computer'), [word, sheet]


def question(ctx, issue='ambiguous'):
    session, files = ctx
    return core.begin(session, '确认预算', files, client=Client(lambda _: {
        'kind': 'clarify', 'issue': issue, 'reason': '请明确哪一项。'}))


@pytest.mark.parametrize('issue', ['ambiguous','missing_target','out_of_scope'])
def test_three_questions_are_durable_non_executable(ctx, issue):
    session, files = ctx
    result = question(ctx, issue)
    assert set(result) == {'plan','reason','office_result'} and result['plan'] is None
    pending = core.load(ComputerSession(session.root), result)
    assert pending['state'] == 'pending' and pending['issue'] == issue
    assert pending['original_instruction'] == '确认预算' and pending['files'] == [str(p) for p in files]
    assert core.recent(session)[0]['id'] == pending['id']


def test_old_kind_reason_response_still_works(ctx):
    session, files = ctx
    result = core.begin(session, '确认预算', files, client=Client(lambda _: {'kind':'clarify','reason':'哪项？'}))
    assert core.load(session, result)['issue'] == 'ambiguous'


def test_continue_reuses_snapshots_and_composes_instruction(ctx, monkeypatch):
    session, files = ctx
    pending = core.load(session, question(ctx))
    snapshots = set(session.root.glob('edit-inputs/*/source.json'))
    monkeypatch.setattr(intent, 'snapshot_edit_source', lambda *a: pytest.fail('must reuse original snapshots'))
    client = Client()
    expected = core.compose_instruction(pending, '纸张金额15，文字已确认')
    result = core.continue_attempt(ComputerSession(session.root), pending, '纸张金额15，文字已确认', client=client)
    assert result['plan']['instruction'] == expected
    assert client.contexts[0]['instruction'] == expected
    assert set(session.root.glob('edit-inputs/*/source.json')) == snapshots
    child = core.load(session, result)
    assert child['state'] == 'resolved' and child['parent']['id'] == pending['id']
    assert core.load(session, pending)['state'] == 'claimed'
    with pytest.raises(ComputerError, match='already_claimed'):
        core.continue_attempt(session, pending, '重复', client=Client())
    assert len(list((session.root / 'model_calls').glob('*.json'))) == 4


def test_target_validation_failure_is_preserved_and_can_clarify(ctx):
    session, files = ctx
    result = core.begin(session, '修改找不到的目标', files,
        client=Client(lambda _: {'kind':'edits','changes':[{'target':'invented','after':'x'}]}))
    record = core.load(session, result)
    assert result['plan'] is None and record['issue'] == 'missing_target'
    folder = session.root / 'office-planning' / record['id']
    assert read_record(folder / 'failure.json')['code'] == 'office_planning_target_invalid'
    assert not list(session.root.glob('goals/*.json'))
    assert core.continue_attempt(session, record, '确认预算文字并把纸张改15', client=Client())['plan']


def test_capacity_can_be_locally_narrowed_without_initial_model(ctx, monkeypatch):
    session, files = ctx
    monkeypatch.setattr(intent, 'MAX_TARGETS', 1)
    client = Client()
    result = core.begin(session, '纸张改15', files, client=client)
    record = core.load(session, result)
    assert record['issue'] == 'out_of_scope' and not client.contexts
    from agent_service.office_scope import selection_from_indices
    cat = core.catalog(session, record)
    chosen = [(i,j) for i,f in enumerate(cat['files']) for j,r in enumerate(f['entries'])
              if r['at'] == {'sheet':'预算','cell':'B2'}]
    selection = selection_from_indices(cat, chosen)
    second = core.continue_attempt(session, record, '仅改纸张金额', selection=selection, client=client)
    assert second['plan']['goal']['edits'][0]['spec']['changes'][0]['after'] == 15
    assert len(client.contexts) == 1


def test_explicit_range_change_can_use_other_location_in_original_files(ctx):
    session, files = ctx
    from agent_service.office_scope import selection_from_indices
    result = question(ctx, 'out_of_scope')
    record = core.load(session, result)
    cat = core.catalog(session, record)
    index = [(i,j) for i,f in enumerate(cat['files']) for j,r in enumerate(f['entries'])
             if r['at'] == {'sheet':'预算','cell':'B2'}]
    selected = selection_from_indices(cat, index)
    narrowed = core.continue_attempt(session, record, '先确认纸张', selection=selected,
        client=Client(lambda _: {'kind':'clarify','issue':'out_of_scope','reason':'文字也要改吗？'}))
    next_record = core.load(session, narrowed)
    expanded = selection_from_indices(cat, [(i,j) for i,f in enumerate(cat['files'])
        for j,r in enumerate(f['entries']) if r['editable']])
    final = core.continue_attempt(session, next_record, '文字也确认', selection=expanded, client=Client())
    assert len(final['plan']['goal']['edits']) == 2


def test_scope_never_adds_external_file_and_invalid_locations_do_not_consume(ctx, tmp_path):
    session, files = ctx
    pending = core.load(session, question(ctx))
    external = tmp_path / 'other.docx'
    word_fixture(external)
    with pytest.raises(ComputerError, match='scope_widened'):
        core.continue_attempt(session, pending, '修改其他文件', selection={
            'files':[str(external)], 'artifact_refs':None, 'scope':[]}, client=Client())
    part = {'path':str(files[0]), 'sha256':pending['all_sources'][0]['public']['sha256'],
            'locations':[{'paragraph_index':999}]}
    with pytest.raises(ComputerError, match='location_invalid'):
        core.continue_attempt(session, pending, '修改', selection={'files':[str(files[0])],
            'artifact_refs':None,'scope':[part]}, client=Client())
    assert core.load(session, pending)['state'] == 'pending'


@pytest.mark.parametrize('which', ['original','snapshot','source_record','request','continuation','forged','foreign','traversal'])
def test_changed_and_forged_state_is_rejected_before_model(ctx, tmp_path, which):
    session, files = ctx
    pending = core.load(session, question(ctx))
    folder = session.root / 'office-planning' / pending['id']
    if which == 'original': files[1].write_bytes(files[1].read_bytes()+b'changed')
    elif which == 'snapshot':
        p = next(session.root.glob('edit-inputs/*/source.docx'))
        p.write_bytes(p.read_bytes()+b'changed')
    elif which == 'source_record':
        p = next(session.root.glob('edit-inputs/*/source.json'))
        value = read_record(p); value['original'] = str(tmp_path / 'alien.docx')
        p.write_text(json.dumps(value), encoding='utf-8')
    elif which == 'request':
        p = folder / 'request.json'; value = read_record(p); value['instruction'] = 'changed'
        p.write_text(json.dumps(value), encoding='utf-8')
    elif which == 'continuation':
        p = folder / 'continuation.json'; value = read_record(p); value['original_instruction'] = 'changed'
        p.write_text(json.dumps(value), encoding='utf-8')
    elif which == 'forged': pending['sha256'] = '0'*64
    elif which == 'foreign': session = ComputerSession.create(tmp_path / 'foreign')
    elif which == 'traversal': pending = '../elsewhere'
    client = Client()
    with pytest.raises((ComputerError, ValueError, OSError)):
        core.continue_attempt(session, pending, '明确要求', client=client)
    assert not client.contexts


def test_all_originals_rechecked_even_after_narrowing_to_one_file(ctx):
    session, files = ctx
    pending = core.load(session, question(ctx))
    from agent_service.office_scope import selection_from_indices
    cat = core.catalog(session, pending)
    selection = selection_from_indices(cat, [(0,j) for j,row in enumerate(cat['files'][0]['entries']) if row['editable']])
    def respond(context):
        files[1].write_bytes(files[1].read_bytes()+b'changed')
        return valid_response(context)
    with pytest.raises(ComputerError, match='source_changed'):
        core.continue_attempt(session, pending, '仅改文字', selection=selection, client=Client(respond))
    assert not list(session.root.glob('goals/*.json'))


@pytest.mark.parametrize('when', ['before','during'])
def test_original_receipts_rechecked_on_continuation(selected, when):
    session, files, records, receipts, _ = selected
    from agent_service.office_handoff import select_outputs
    refs = select_outputs(records)
    initial = core.begin(session, '确认预算', files, artifact_refs=refs,
        client=Client(lambda _: {'kind':'clarify','reason':'哪项？'}))
    pending = core.load(session, initial)
    def change(): receipts[str(files[1])]['receipt_sha256'] = 'b'*64
    if when == 'before': change()
    def respond(context):
        change()
        return valid_response(context)
    client = Client(respond)
    with pytest.raises(ComputerError, match='reference_changed'):
        core.continue_attempt(session, pending, '确认文字及纸张15', client=client)
    assert len(client.contexts) == (0 if when == 'before' else 1)
    assert not list(session.root.glob('goals/*.json'))


def test_shared_session_model_limit_cannot_be_reset_by_answers(ctx):
    session, files = ctx
    pending = core.load(session, question(ctx))
    for _ in range(5): session.claim_model({'other':'preexisting'})
    client = Client()
    with pytest.raises(ComputerError, match='model_call_limit'):
        core.continue_attempt(session, pending, '确认预算', client=client)
    assert not client.contexts
    assert core.load(session, pending)['state'] == 'claimed'


def test_cancel_does_not_claim_or_model(ctx):
    session, files = ctx
    pending = core.load(session, question(ctx))
    with pytest.raises(ComputerError, match='cancelled'):
        core.continue_attempt(session, pending, '确认预算', client=Client(), stop=lambda: True)
    assert core.load(session, pending)['state'] == 'pending'


def test_budget_denial_remains_denial_and_does_not_create_plan(ctx, monkeypatch):
    session, _ = ctx
    pending = core.load(session, question(ctx))
    from agent_service import model
    from agent_service.budget import BudgetError
    class Denied:
        def __init__(self, **kwargs): pass
        def complete(self, *args, **kwargs): raise BudgetError('budget_exceeded')
    monkeypatch.setattr(model, 'CloudClient', Denied)
    with pytest.raises(ComputerError, match='model_budget_exceeded'):
        core.continue_attempt(session, pending, '确认预算')
    assert not list(session.root.glob('goals/*.json'))


def test_formula_is_readonly_in_reused_catalog_and_cannot_be_selected_alone(ctx):
    session, _ = ctx
    pending = core.load(session, question(ctx))
    cat = core.catalog(session, pending)
    from agent_service.office_scope import selection_from_indices
    positions = [(i,j) for i,f in enumerate(cat['files']) for j,row in enumerate(f['entries'])
                 if isinstance(row['value'], dict) and 'formula' in row['value']]
    assert positions and all(not cat['files'][i]['entries'][j]['editable'] for i,j in positions)
    with pytest.raises(ComputerError): selection_from_indices(cat, positions)


def test_cancel_after_model_returns_no_plan(ctx):
    session, _ = ctx
    pending = core.load(session, question(ctx))
    stopped = []
    def respond(context):
        stopped.append(True)
        return valid_response(context)
    with pytest.raises(ComputerError, match='cancelled'):
        core.continue_attempt(session, pending, '确认预算', client=Client(respond), stop=lambda: bool(stopped))
    assert not list(session.root.glob('goals/*.json'))


def test_chain_limit_and_original_instruction_preserved(ctx):
    session, files = ctx
    result = question(ctx)
    for n in range(core.MAX_ANSWERS):
        pending = core.load(session, result)
        result = core.continue_attempt(session, pending, str(n),
            client=Client(lambda _: {'kind':'clarify','reason':'请再明确。'}))
    last = core.load(session, result)
    assert last['original_instruction'] == '确认预算'
    assert last['round'] == core.MAX_ANSWERS
    with pytest.raises(ComputerError, match='chain_limit'):
        core.continue_attempt(session, last, 'more', client=Client())


def test_answer_limits_do_not_consume_pending(ctx):
    session, _ = ctx
    pending = core.load(session, question(ctx))
    for answer in ('', 'a\nb', 'x'*601):
        with pytest.raises(ComputerError): core.continue_attempt(session, pending, answer, client=Client())
    assert core.load(session, pending)['state'] == 'pending'


def test_preflight_rejects_bad_scope_without_io_or_claim(ctx, monkeypatch):
    session, files = ctx
    pending = core.load(session, question(ctx))
    monkeypatch.setattr(core, 'load', lambda *_: pytest.fail('preflight must be pure'))
    assert core.validate_answer(pending, '明确') == core.compose_instruction(pending, '明确')
    with pytest.raises(ComputerError, match='scope_widened'):
        core.validate_answer(pending, '明确', {'files':['other.docx'],'scope':[], 'artifact_refs':None})


def test_changed_parent_claim_prevents_child_restore(ctx):
    session, _ = ctx
    pending = core.load(session, question(ctx))
    result = core.continue_attempt(session, pending, '确认预算', client=Client())
    path = session.root / 'office-planning' / pending['id'] / 'answer-claim.json'
    path.write_text(json.dumps({'parent_sha256':pending['sha256'],'child':'0'*32}), encoding='utf-8')
    with pytest.raises(ComputerError, match='parent_changed'): core.load(session, result)
