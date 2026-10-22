"""Offline typed goals: actual OOXML and existing receipt/source verification."""
from copy import deepcopy
import json
from pathlib import Path
import time
import uuid

import pytest

from agent_service import computer_goal_adapters as adapters
from agent_service.artifact_source import (prepare_artifact_source,
    prepare_artifact_task_root, snapshot_artifact_source)
from agent_service.computer import ComputerError, ComputerSession, read_record
from agent_service.computer_goals import execute_goal, prepare_goal
from agent_service.contracts import Observation
from agent_service.task_history import inspect_task, list_tasks
from test_artifact_source import fake as native_fake


URL = 'https://example.com/'
TITLE = '真实公开资料 😀'
BODY = '正文不是模型结果。 literal {+^%} / {{not_a_template}} ' + '证据' * 700


class Browser:
    def __init__(self):
        self.calls = []
    def read(self, url, should_stop):
        assert not should_stop()
        self.calls.append(url)
        return Observation(url, url, TITLE, BODY, '2026-10-17T12:00:00+00:00')


@pytest.fixture
def context(tmp_path):
    session = ComputerSession.create(tmp_path / 'sessions', host=lambda *a, **kw: pytest.fail('UIA forbidden'))
    browser, effects = Browser(), []
    def office_execute(spec, root, stop):
        effects.append((deepcopy(spec), root))
        return native_fake(spec, root, stop)
    return dict(session=session, browser=browser, effects=effects,
                options=dict(browser=browser, office_execute=office_execute,
                             task_base=tmp_path / 'assistant_tasks'))


def prepared(context, steps=None):
    return prepare_goal(context['session'], '读取公开网页并生成Excel和Word', {},
                        adapters.template([URL], '资料摘录') if steps is None else steps)


def execute(context, plan, **kwargs):
    return execute_goal(context['session'], plan, approval=plan['sha256'],
                        adapter_options=context['options'], **kwargs)


def test_browser_excel_word_saved_bytes_provenance_and_next_handoff(context):
    outcome = execute(context, prepared(context))
    assert outcome['ok'] and outcome['state'] == 'verified'
    assert context['browser'].calls == [URL]
    assert [s['kind'] for s, _ in context['effects']] == ['spreadsheet', 'document']
    sheet, document = [s for s, _ in context['effects']]
    assert sheet['headers'] == adapters.HEADERS
    assert sheet['rows'][0][:4] == [TITLE, URL, '2026-10-17T12:00:00+00:00', BODY[:1200]]
    assert len(sheet['rows'][0][3]) == 1200 and '不代表完整网页' in sheet['rows'][0][4]
    assert document['table'] == {'headers': sheet['headers'], 'rows': sheet['rows']}
    base = context['options']['task_base']
    assert len(list_tasks(base)) == 2
    for artifact in outcome['artifacts']:
        task = Path(artifact['task_root'])
        inspection = inspect_task(task, base)
        assert inspection['state'] == 'succeeded' and len(inspection['artifacts']) == 1
        assert read_record(task / 'goal-origin.json')['approval'] == read_record(Path(outcome['run']) / 'claim.json')['approval']
        assert prepare_artifact_source(artifact['path'], artifact['sha256'], base)
    sheet_ref = outcome['outputs']['sheet']['ref']
    word_task = Path(outcome['outputs']['document']['ref']['task_root'])
    provenance = read_record(word_task / 'sources' / 'provenance.json')
    assert provenance == {'kind': 'verified_generated_office_artifact', **sheet_ref}
    next_task = base / ('task-' + uuid.uuid4().hex)
    prepare_artifact_task_root(next_task, base)
    actual = snapshot_artifact_source(outcome['outputs']['document']['ref'], next_task, base)
    assert actual['kind'] == 'document'
    assert actual['observed']['table'] == document['table']
    assert read_record(next_task / 'sources' / 'provenance.json')['artifact'] == outcome['outputs']['document']['ref']['artifact']


@pytest.mark.parametrize('change', ['forward', 'wrong_kind', 'duplicate_output', 'extra', 'mixed', 'bad_url', 'duplicate_url', 'too_many_urls'])
def test_schema_and_reference_rejections_before_any_effect(context, change):
    steps = adapters.template([URL], '资料摘录')
    if change == 'forward':
        steps[1]['from'] = 'document'
    elif change == 'wrong_kind':
        steps[2]['from'] = 'sources'
    elif change == 'duplicate_output':
        steps[1]['as'] = 'sources'
    elif change == 'extra':
        steps[1]['text'] = 'forged content'
    elif change == 'mixed':
        steps.append({'op': 'new_note', 'window': 'note', 'text': 'text'})
    elif change == 'bad_url':
        steps[0]['urls'] = ['file:///C:/secret.txt']
    elif change == 'duplicate_url':
        steps[0]['urls'] = [URL, URL]
    else:
        steps[0]['urls'] = [URL, 'https://example.org/', 'https://example.net/']
    with pytest.raises(ComputerError):
        prepared(context, steps)
    assert not context['browser'].calls and not context['effects']


def test_adapter_plan_refuses_uia_windows(context):
    with pytest.raises(ComputerError, match='adapter_windows_not_allowed'):
        prepare_goal(context['session'], 'goal', {'window': {}}, adapters.template([URL], 'title'))


@pytest.mark.parametrize('change', ['file', 'receipt', 'source_observation'])
def test_changed_evidence_blocks_subsequent_office_effect(context, change):
    def progress(value):
        target = 2 if change == 'source_observation' else 3
        if value['step'] != target:
            return
        run = next((context['session'].root / 'goals').glob('*.run'))
        if change == 'source_observation':
            source = run / 'sources-1.json'
            content = read_record(source)
            content['pages'][0]['text'] = 'FORGED BODY'
            source.write_text(json.dumps(content), encoding='utf-8')
        else:
            sheet_root = context['effects'][0][1]
            if change == 'file':
                with (sheet_root / 'spreadsheet.xlsx').open('ab') as f:
                    f.write(b'changed')
            else:
                receipt = sheet_root / 'receipt.json'
                content = read_record(receipt)
                content['observed']['rows'][0][0] = 'FORGED TITLE'
                receipt.write_text(json.dumps(content), encoding='utf-8')
    outcome = execute(context, prepared(context), progress=progress)
    assert not outcome['ok']
    assert len(context['effects']) == (0 if change == 'source_observation' else 1)
    assert len(outcome['completed']) == (1 if change == 'source_observation' else 2)


def test_cancel_after_excel_preserves_verified_child_and_never_runs_word(context):
    stopped = [False]
    def progress(value):
        if value['step'] == 3:
            stopped[0] = True
    outcome = execute(context, prepared(context), progress=progress, stop=lambda: stopped[0])
    assert outcome['state'] == 'partial' and outcome['code'] == 'goal_cancelled'
    assert len(context['effects']) == 1 and len(outcome['completed']) == 2
    assert read_record(Path(outcome['artifacts'][0]['task_root']) / 'result.json')['state'] == 'succeeded'


def test_deadline_after_reading_blocks_native_effect(context):
    tick = [time.monotonic()]
    def progress(value):
        if value['step'] == 2:
            tick[0] += adapters.SECONDS
    outcome = execute(context, prepared(context), progress=progress, clock=lambda: tick[0])
    assert outcome['code'] == 'goal_deadline' and outcome['state'] == 'partial'
    assert not context['effects']


def test_unknown_native_effect_has_no_word_and_is_never_replayed(context):
    def interrupted(spec, root, stop):
        context['effects'].append((deepcopy(spec), root))
        native_fake(spec, root, stop)
        raise RuntimeError('lost reply after save')
    context['options']['office_execute'] = interrupted
    plan = prepared(context)
    outcome = execute(context, plan)
    assert outcome['state'] == 'unknown' and len(context['effects']) == 1
    assert len(outcome['completed']) == 1
    task = context['effects'][0][1].parent.parent
    assert read_record(task / 'result.json')['state'] == 'unknown'
    assert (context['effects'][0][1] / 'intent.json').is_file()
    assert not (context['effects'][0][1] / 'receipt.json').exists()
    context['session'] = ComputerSession(context['session'].root, host=lambda *a, **kw: pytest.fail('UIA forbidden'))
    with pytest.raises(ComputerError, match='goal_already_claimed'):
        execute(context, plan)
    assert len(context['effects']) == 1


def test_expired_preview_is_rejected_before_claim(context, monkeypatch):
    real_time = time.time
    with monkeypatch.context() as patch:
        patch.setattr(adapters.time, 'time', lambda: real_time() - 121)
        plan = prepared(context)
    with pytest.raises(ComputerError, match='goal_preview_expired'):
        execute(context, plan)
    assert not list((context['session'].root / 'goals').glob('*.run'))
    assert not context['browser'].calls and not context['effects']


def test_tampered_approval_cannot_read_or_create_files(context):
    plan = prepared(context)
    plan['steps'][0]['urls'] = ['https://example.org/']
    with pytest.raises(ComputerError, match='goal_plan_changed'):
        execute(context, plan)
    assert not context['browser'].calls and not context['effects']


def test_multiline_title_and_body_normalize_only_office_cells_preserving_evidence(context):
    original_title = '真实\r\n标题\t😀'
    original_text = '第一行\r\n第二行\r第三行\n制表\t原文 ' + '证据' * 700
    def read(url, should_stop):
        assert not should_stop()
        return Observation(url, url, original_title, original_text, '2026-10-17T12:00:00+00:00')
    context['browser'].read = read
    outcome = execute(context, prepared(context))
    assert outcome['ok']
    observed = read_record(Path(outcome['outputs']['sources']['path']))['pages'][0]
    assert observed['title'] == original_title and observed['text'] == original_text
    flat = lambda value: value.replace('\r', ' ').replace('\n', ' ').replace('\t', ' ')
    sheet, document = [spec for spec, _ in context['effects']]
    row = sheet['rows'][0]
    assert row[0] == flat(original_title)
    assert row[3] == flat(original_text[:1200]) and len(row[3]) == 1200
    assert '换行/制表符转空格' in row[4]
    assert document['table']['rows'] == sheet['rows']


@pytest.mark.parametrize('title', ['有\n换行', '有\r回车', '有\t制表', '有\x00控制符'])
def test_invalid_office_title_rejected_during_preview(context, title):
    steps = adapters.template([URL], '正常标题')
    steps[1]['title'] = title
    with pytest.raises(ComputerError, match='adapter_title_invalid|adapter_text_invalid'):
        prepared(context, steps)
    assert not context['browser'].calls and not context['effects']


def test_invalid_source_cell_rejected_before_native_intent_and_not_unknown(context):
    def read(url, should_stop):
        return Observation(url, url, TITLE, '正文含不支持的C1字符\u0085', '2026-10-17T12:00:00+00:00')
    context['browser'].read = read
    outcome = execute(context, prepared(context))
    assert not outcome['ok'] and outcome['state'] == 'partial'
    assert len(outcome['completed']) == 1 and not context['effects']
    assert not list(context['options']['task_base'].rglob('intent.json'))


def test_stop_after_native_receipt_preserves_child_success_and_prevents_word(context):
    stopped = [False]
    def execute_then_stop(spec, root, stop):
        context['effects'].append((deepcopy(spec), root))
        result = native_fake(spec, root, stop)
        stopped[0] = True
        return result
    context['options']['office_execute'] = execute_then_stop
    outcome = execute(context, prepared(context), stop=lambda: stopped[0])
    assert outcome['state'] == 'partial' and outcome['code'] == 'goal_cancelled'
    assert len(context['effects']) == 1 and len(outcome['artifacts']) == 1
    artifact = outcome['artifacts'][0]
    assert read_record(Path(artifact['task_root']) / 'result.json')['state'] == 'succeeded'
    assert prepare_artifact_source(artifact['path'], artifact['sha256'], context['options']['task_base'])
