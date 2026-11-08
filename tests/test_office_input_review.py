"""Local selected-input review and explicit, guarded planning handoff."""
from copy import deepcopy
from hashlib import sha256
import json
from types import ModuleType
import zipfile

import pytest

from agent_service import computer_goal_panel, office_input_review, office_workspace
from test_office_workspace import prepared
from test_office_workspace_panel import _Goal, _Thread, _make_panel, _material


def _review(tmp_path, monkeypatch):
    records, scope, refs = _material(tmp_path)
    goal = _Goal(files=[r['path'] for r in records], refs=refs, scope=scope,
                 draft='  exact instruction\r\n')
    goal.stopped.clear()
    goal.proposals = []
    goal.propose = lambda: goal.proposals.append(deepcopy(panel.context()))
    panel = _make_panel(goal, tmp_path / 'store', tmp_path, monkeypatch)
    captured, dialogs = [], []
    def verify(payload, _base):
        captured.append(deepcopy(payload))
        return {'payload': deepcopy(payload), 'refs': deepcopy(refs),
                'source_ref': None, 'review': []}
    monkeypatch.setattr(office_workspace, 'verify', verify)
    # These UI tests isolate modal context guards; real preflight is exercised below.
    monkeypatch.setattr(office_input_review, 'validate_for_planning', lambda verified: None)
    monkeypatch.setattr(office_input_review, 'open_review',
                        lambda root, verified, callback: dialogs.append((verified, callback)))
    return panel, goal, captured, dialogs


def _complete(panel):
    _Thread.all[-1].run()
    panel.poll()


def test_local_review_preserves_exact_inputs_and_only_explicit_callback_plans(tmp_path, monkeypatch):
    panel, goal, captured, dialogs = _review(tmp_path, monkeypatch)
    before = deepcopy(panel.context())
    panel.controller.expected_id = 'saved-snapshot'
    assert panel.review_inputs()
    assert not goal.proposals and not dialogs
    assert panel.preparation.pending['ticket']['purpose'] == 'input_review'
    _complete(panel)
    assert captured == [before['payload']]
    assert dialogs[0][0]['payload'] == before['payload']
    assert panel.context() == before
    assert panel.controller.expected_id == 'saved-snapshot'
    assert not goal.proposals and goal.executions == 0
    # Closing/returning from a read-only dialog invokes no callback.
    assert panel.context() == before and not (tmp_path / 'store').exists()
    assert dialogs[0][1]() is True
    assert len(goal.proposals) == 1
    assert dialogs[0][1]() is False
    assert len(goal.proposals) == 1


@pytest.mark.parametrize('change', ['draft_revert', 'refs', 'scope', 'mode', 'stop',
                                     'plan', 'busy', 'external_busy', 'pending', 'closed'])
def test_late_review_confirmation_rejects_changed_or_busy_context(tmp_path, monkeypatch, change):
    panel, goal, _, dialogs = _review(tmp_path, monkeypatch)
    assert panel.review_inputs()
    _complete(panel)
    if change == 'draft_revert':
        original = goal.instruction_draft.text
        goal.instruction_draft.adopt('new text')
        goal.instruction_draft.adopt(original)
    elif change == 'refs':
        goal.office_artifact_refs[0]['sha256'] = 'f' * 64
    elif change == 'scope':
        goal.office_scope[0]['locations'] = [{'paragraph_index': 9}]
    elif change == 'mode':
        goal.mode.set(computer_goal_panel.MODES[0])
    elif change == 'stop':
        goal.stopped.set()
        goal.capability_source_generation += 1
        goal.daily_task_route_generation += 1
    elif change == 'plan':
        goal.plan = {'preview': 'new'}
    elif change == 'busy':
        goal.running = True
    elif change == 'external_busy':
        panel.controller.busy = lambda: True
    elif change == 'pending':
        panel.controller.pending = True
    else:
        goal.closed = True
    changed = deepcopy(panel.context())
    assert dialogs[0][1]() is False
    assert not goal.proposals and goal.executions == 0
    assert panel.context() == changed
    assert '未调用模型' in goal.status.get()


@pytest.mark.parametrize('change,message', [
    ('empty', '1至1000'), ('long', '1至1000'), ('scope', '先选择编辑范围'),
    ('mode', '先接入'), ('busy', '正忙'), ('restore', '正忙'),
])
def test_review_start_validation_does_not_start_worker(tmp_path, monkeypatch, change, message):
    panel, goal, captured, dialogs = _review(tmp_path, monkeypatch)
    if change == 'empty':
        goal.instruction_draft.adopt(' \r\n')
    elif change == 'long':
        goal.instruction_draft.adopt('x' * 1001)
    elif change == 'scope':
        goal.office_scope = None
    elif change == 'mode':
        goal.mode.set(computer_goal_panel.MODES[0])
        goal.office_planning_files = ()
        goal.office_artifact_refs = []
        goal.office_scope = None
        goal.instruction_draft.adopt('')
    elif change == 'busy':
        goal.running = True
    else:
        panel.controller.pending = True
    before = deepcopy(panel.context())
    assert panel.review_inputs() is False
    assert message in goal.status.get()
    assert not _Thread.all and not captured and not dialogs and not goal.proposals
    assert panel.context() == before


def test_review_result_after_stop_never_opens_dialog(tmp_path, monkeypatch):
    panel, goal, captured, dialogs = _review(tmp_path, monkeypatch)
    assert panel.review_inputs()
    goal.stopped.set()
    panel.poll()
    assert panel.preparation.pending['discard'] is True
    _complete(panel)
    assert panel.preparation.pending is None
    assert not dialogs and not goal.proposals


def test_real_local_verification_review_contains_only_selected_original_rows(prepared, tmp_path):
    payload, files, _ = prepared
    before = [path.read_bytes() for path in files]
    verified = office_workspace.verify(payload, tmp_path / 'tasks')
    office_input_review.validate_for_planning(verified)
    assert verified['payload'] == payload
    assert verified['review'] == [
        {'path': payload['records'][0]['path'], 'rows': [
            {'at': {'paragraph_index': 1}, 'value': '预算待确认', 'editable': True}]},
        {'path': payload['records'][1]['path'], 'rows': [
            {'at': {'sheet': '预算', 'cell': 'A2'}, 'value': '纸张', 'editable': True}]},
    ]
    text = office_input_review.describe(verified)
    assert payload['draft'] in text and '正文段落 2' in text
    assert '预算待确认' in text and '纸张' in text
    assert '保留这一段' not in text and '此页原样保留' not in text and '原值："笔"' not in text
    assert [path.read_bytes() for path in files] == before


@pytest.mark.parametrize('draft', ['line one\nline two', 'line one\rline two',
                                  'embedded\x00control', 'embedded\ud800surrogate'])
def test_invalid_planner_instruction_rejected_before_worker(tmp_path, monkeypatch, draft):
    panel, goal, captured, dialogs = _review(tmp_path, monkeypatch)
    goal.instruction_draft.adopt(draft)
    before = deepcopy(panel.context())
    assert panel.review_inputs() is False
    assert '单行修改说明' in goal.status.get()
    assert not _Thread.all and not captured and not dialogs and not goal.proposals
    assert panel.context() == before


def test_full_planning_context_over_limit_rejects_modal_before_model(prepared, tmp_path, monkeypatch):
    payload, files, _ = prepared
    with zipfile.ZipFile(files[0]) as archive:
        contents = {name: archive.read(name) for name in archive.namelist()}
    xml = contents['word/document.xml'].decode()
    xml = xml.replace('预算待确认', 'y' * 2800)
    extra = '<w:p><w:r><w:t>' + 'y' * 2800 + '</w:t></w:r></w:p>'
    contents['word/document.xml'] = xml.replace('<w:sectPr>', extra * 5 + '<w:sectPr>').encode()
    with zipfile.ZipFile(files[0], 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in contents.items():
            archive.writestr(name, data)
    digest = sha256(files[0].read_bytes()).hexdigest()
    payload['records'][0]['sha256'] = payload['scope'][0]['sha256'] = digest
    payload['scope'][0]['locations'] = [{'paragraph_index': n} for n in [1, 3, 4, 5, 6, 7]]
    payload['draft'] = 'x' * 1000
    verified = office_workspace.verify(payload, tmp_path / 'tasks')
    selected_context = [item['rows'] for item in verified['review']]
    assert len(json.dumps(selected_context, ensure_ascii=False)) < 18_000
    with pytest.raises(ValueError, match='office_planning_scope_too_large_or_empty'):
        office_input_review.validate_for_planning(verified)
    goal = _Goal(files=[r['path'] for r in payload['records']], refs=verified['refs'],
                 scope=payload['scope'], draft=payload['draft'])
    goal.stopped.clear()
    goal.proposals = []
    goal.propose = lambda: goal.proposals.append(True)
    panel = _make_panel(goal, tmp_path / 'store', tmp_path, monkeypatch)
    dialogs = []
    monkeypatch.setattr(office_input_review, 'open_review', lambda *args: dialogs.append(args))
    monkeypatch.setattr(office_workspace, 'verify', lambda *_args: deepcopy(verified))
    before = deepcopy(panel.context())
    assert panel.review_inputs()
    _complete(panel)
    assert not dialogs and not goal.proposals
    assert '无法满足规划限制' in goal.status.get()
    assert panel.context() == before


@pytest.mark.parametrize('value,formatted', [(None, 'null'), (0, '0'), (False, 'false'), ('', '""')])
def test_review_formats_original_json_values_and_word_positions(value, formatted):
    verified = {'payload': {'draft': 'instruction', 'records': [{'path': 'word.docx'}]},
                'review': [{'path': 'word.docx', 'rows': [{
                    'at': {'table_index': 0, 'row_index': 1, 'column_index': 2, 'paragraph_index': 0},
                    'value': value, 'editable': False}]}]}
    text = office_input_review.describe(verified)
    assert '表格 1 · 行 2 · 列 3 · 段落 1 [只读]' in text
    assert '  原值：' + formatted + '\n' in text


def test_review_window_return_closes_without_continuing_and_text_is_readonly(monkeypatch):
    from agent_service import office_scope_panel
    import sys
    widgets, closed, continued = [], [], []
    class Widget:
        def __init__(self, *args, **options):
            self.options = options
            self.content = ''
            widgets.append(self)
        def pack(self, **kwargs):
            pass
        def configure(self, **options):
            self.options.update(options)
        def insert(self, where, content):
            self.content = content
        def yview(self, *args):
            pass
        def set(self, *args):
            pass
    tk, ttk = ModuleType('tkinter'), ModuleType('tkinter.ttk')
    tk.Text = ttk.Frame = ttk.Scrollbar = ttk.Button = Widget
    tk.ttk = ttk
    monkeypatch.setitem(sys.modules, 'tkinter', tk)
    monkeypatch.setitem(sys.modules, 'tkinter.ttk', ttk)
    monkeypatch.setattr(office_scope_panel, '_modal',
                        lambda *args: (object(), lambda: closed.append(True)))
    verified = {'payload': {'draft': 'instruction', 'records': [{'path': 'word.docx'}]},
                'review': [{'path': 'word.docx', 'rows': [{
                    'at': {'paragraph_index': 0}, 'value': 'selected', 'editable': True}]}]}
    office_input_review.open_review(object(), verified, lambda: continued.append(True))
    text = next(item for item in widgets if item.content)
    assert text.options['state'] == 'disabled' and 'selected' in text.content
    buttons = {item.options['text']: item for item in widgets if 'text' in item.options}
    assert not continued and not closed
    buttons['返回修改说明/范围'].options['command']()
    assert closed == [True] and not continued
