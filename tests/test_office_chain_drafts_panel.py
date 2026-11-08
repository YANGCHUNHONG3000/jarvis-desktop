"""Read-only draft discovery uses fresh chain proofs and preserves task inputs."""
import ast
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_service import office_task_chain_panel as module
from test_office_task_chain_panel import finish, harness, mutate


def prepared(tmp_path, monkeypatch):
    panel, context, log, state = harness(tmp_path, monkeypatch)
    draft = {'path': str(tmp_path / 'draft.eml'), 'sha256': 'b' * 64}
    office = deepcopy(state['payload']['records'][0])
    state['deliveries'] = [{'index': 0, 'task_root': str(tmp_path / 'task'), 'records': [office, draft]}]
    panel.active = deepcopy(state)
    calls, opened = [], []
    def inspect(store, ident):
        calls.append(ident)
        return deepcopy(state)
    monkeypatch.setattr(module.core, 'inspect', inspect)
    panel.open_draft = lambda record: opened.append(record) or True
    return panel, context, log, state, draft, calls, opened


def test_view_rechecks_without_adopting_or_advancing(tmp_path, monkeypatch):
    panel, context, log, state, draft, calls, opened = prepared(tmp_path, monkeypatch)
    before = deepcopy(context)
    dialogs = []
    monkeypatch.setattr(panel, '_drafts_dialog', lambda state: dialogs.append(state))
    assert panel.view_drafts()
    finish(panel)
    assert calls == [state['id']] and dialogs == [state]
    assert panel._draft_records(state) == [(0, draft)]
    assert context == before and not log.applied and not log.prepared and not log.advanced and not opened


def test_open_rechecks_again_and_only_previews_selected_draft(tmp_path, monkeypatch):
    panel, context, log, state, draft, calls, opened = prepared(tmp_path, monkeypatch)
    before = deepcopy(context)
    assert panel.preview_draft(state['id'], 0, draft)
    finish(panel)
    assert calls == [state['id']] and opened == [draft]
    assert context == before and not log.applied and not log.advanced


@pytest.mark.parametrize('change', ['main_new', 'main_revert', 'goal_new', 'source', 'records', 'stop', 'scope'])
def test_changed_context_discards_late_preview(tmp_path, monkeypatch, change):
    panel, context, log, state, draft, _calls, opened = prepared(tmp_path, monkeypatch)
    assert panel.preview_draft(state['id'], 0, draft)
    mutate(context, change)
    before = deepcopy(context)
    finish(panel)
    assert not opened and not log.applied and context == before


@pytest.mark.parametrize('change', ['removed', 'hash', 'index', 'different_chain', 'failed_verification', 'cancel'])
def test_stale_or_failed_selection_is_never_opened(tmp_path, monkeypatch, change):
    panel, _context, _log, state, draft, _calls, opened = prepared(tmp_path, monkeypatch)
    assert panel.preview_draft(state['id'], 0, draft)
    if change == 'removed': state['deliveries'] = []
    elif change == 'hash': state['deliveries'][0]['records'][1]['sha256'] = 'c' * 64
    elif change == 'index': state['deliveries'][0]['index'] = 1
    elif change == 'different_chain': panel.active['id'] = 'c' * 32
    elif change == 'failed_verification':
        monkeypatch.setattr(module.core, 'inspect', lambda *_: (_ for _ in ()).throw(ValueError('changed')))
    elif change == 'cancel': panel.cancel()
    finish(panel)
    assert not opened


def test_no_active_chain_or_office_selection_never_starts_preview(tmp_path, monkeypatch):
    panel, _context, _log, state, _draft, calls, opened = prepared(tmp_path, monkeypatch)
    assert not panel.preview_draft(state['id'], 0, state['payload']['records'][0])
    panel.active = None
    assert not panel.view_drafts()
    assert not calls and not opened


def main_function(name, environment):
    tree = ast.parse((Path(__file__).parents[1] / 'run_jarvis_assistant.py').read_text('utf-8'))
    main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'main')
    fn = next(node for node in main.body if isinstance(node, ast.FunctionDef) and node.name == name)
    exec(compile(ast.Module(body=[fn], type_ignores=[]), '<main-function>', 'exec'), environment)
    return environment[name]


@pytest.mark.parametrize('verified,opened,expected', [(None, True, False), ('document.docx', True, False),
                                                   ('draft.eml', False, False), ('draft.eml', True, True)])
def test_main_callback_only_opens_verified_draft_and_reports_actual_result(verified, opened, expected):
    calls, statuses = [], []
    env = {'TASK_ROOT': Path('tasks'), 'task_status': object(), 'office_chain_status': statuses.append,
           '_verified_history_path': lambda *_: Path(verified) if verified else None,
           'open_artifact': lambda path, **_: calls.append(path) or opened}
    assert main_function('office_chain_open_draft', env)({'path': 'draft.eml', 'sha256': 'a'*64}) is expected
    assert len(calls) == (1 if verified == 'draft.eml' else 0)


def test_preview_parse_failure_does_not_report_open_success(tmp_path):
    path = tmp_path / 'draft.eml'
    path.write_text('invalid', 'utf-8')
    statuses = []
    env = {'task_status': SimpleNamespace(set=statuses.append),
           '_read_draft_preview': lambda _: (_ for _ in ()).throw(ValueError('invalid'))}
    assert main_function('open_artifact', env)(path) is False
    assert statuses and '无法打开' in statuses[-1]
