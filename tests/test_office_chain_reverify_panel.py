"""Read-only retry ownership; no API, native execution, or task replay."""
from copy import deepcopy

import pytest

from agent_service import office_task_chain_panel as module
from test_office_task_chain_panel import Thread, automatic, finish, harness, mutate
from test_office_chain_drafts_panel import prepared
from test_office_chain_resume_panel import resume_harness, open_resume, next_stage


SECRET = r'C:\private\客户邮件.docx token=hidden-123'


def fail_once(monkeypatch, name, result, *, error=None):
    calls = []
    def operation(*args, **kwargs):
        calls.append((deepcopy(args), deepcopy(kwargs)))
        if len(calls) == 1:
            raise error if error is not None else PermissionError(SECRET)
        return deepcopy(result)
    monkeypatch.setattr(module.core, name, operation)
    return calls


def failed_restore(tmp_path, monkeypatch):
    panel, context, log, state = harness(tmp_path, monkeypatch)
    calls = fail_once(monkeypatch, 'advance', state)
    assert panel.restore(state['id'])
    finish(panel)
    assert panel.failed_verification is not None and panel.pending is None
    return panel, context, log, state, calls


def test_restore_permission_failure_can_reverify_same_request_after_release(tmp_path, monkeypatch):
    panel, context, log, state, calls = failed_restore(tmp_path, monkeypatch)
    before = deepcopy(context)
    assert '访问受阻或被占用' in log.statuses[-1] and SECRET not in log.statuses[-1]
    assert context == before and not log.applied and log.busy == [True, False]
    assert panel.retry_verification()
    assert panel.failed_verification is None
    finish(panel)
    assert calls[0] == calls[1] and len(calls) == 2
    assert panel.active == state and len(log.applied) == 1
    assert context['workspace']['payload'] == state['payload']
    assert not panel.retry_verification() and len(Thread.all) == 2


def test_failure_diagnosis_never_echoes_arbitrary_exception_text(tmp_path, monkeypatch):
    panel, _context, log, state = harness(tmp_path, monkeypatch)
    fail_once(monkeypatch, 'advance', state, error=ValueError(SECRET))
    assert panel.restore(state['id']); finish(panel)
    assert '无法安全确认具体原因' in log.statuses[-1]
    assert all('hidden-123' not in text and SECRET not in text for text in log.statuses)


def test_draft_reverification_keeps_exact_selected_record_and_opens_only_once(tmp_path, monkeypatch):
    panel, context, log, state, draft, _calls, opened = prepared(tmp_path, monkeypatch)
    another = {'path': str(tmp_path / 'another.eml'), 'sha256': 'c' * 64}
    state['deliveries'].append({'index': 1, 'task_root': 'other-task', 'records': [another]})
    calls = fail_once(monkeypatch, 'inspect', state)
    selection = deepcopy(draft); original = deepcopy(draft); before = deepcopy(context)
    assert panel.preview_draft(state['id'], 0, selection); finish(panel)
    assert not opened
    selection.clear(); selection.update(another)
    assert panel.retry_verification(); finish(panel)
    assert opened == [original] and calls[0] == calls[1]
    assert not panel.retry_verification() and opened == [original]
    assert context == before and not log.applied and not log.prepared and not log.advanced


def test_draft_list_failure_retries_read_only_dialog_request(tmp_path, monkeypatch):
    panel, context, log, state, _draft, _calls, opened = prepared(tmp_path, monkeypatch)
    calls = fail_once(monkeypatch, 'inspect', state); dialogs = []
    monkeypatch.setattr(panel, '_drafts_dialog', lambda value: dialogs.append(value))
    before = deepcopy(context)
    assert panel.view_drafts(); finish(panel)
    assert not dialogs and panel.retry_verification(); finish(panel)
    assert dialogs == [state] and len(calls) == 2 and context == before
    assert not log.applied and not log.advanced and not opened


@pytest.mark.parametrize('change', ['main_new', 'main_revert', 'goal_new', 'goal_revert',
                                  'source', 'source_revert', 'records', 'selected',
                                  'goal_source_revert', 'stop', 'scope', 'plan'])
def test_failed_request_invalidates_on_new_input_source_selection_or_stop_generation(tmp_path, monkeypatch, change):
    panel, context, log, _state, calls = failed_restore(tmp_path, monkeypatch)
    mutate(context, change); before = deepcopy(context)
    assert not panel.retry_verification()
    assert panel.failed_verification is None and len(calls) == len(Thread.all) == 1
    assert context == before and not log.applied and not log.prepared


@pytest.mark.parametrize('change', ['active', 'generation', 'epoch'])
def test_failed_request_is_bound_to_exact_active_chain_and_panel_generations(tmp_path, monkeypatch, change):
    panel, _context, _log, state, calls = failed_restore(tmp_path, monkeypatch)
    if change == 'active':
        panel.active = {**deepcopy(state), 'id': 'b' * 32}
    elif change == 'generation':
        panel.generation += 1
    else:
        panel.resume_epoch += 1
    assert not panel.retry_verification()
    assert panel.failed_verification is None and len(calls) == len(Thread.all) == 1


def test_external_busy_preserves_failed_request_until_verification_is_idle(tmp_path, monkeypatch):
    panel, _context, _log, _state, calls = failed_restore(tmp_path, monkeypatch)
    log = _log; failed = panel.failed_verification
    log.external = True
    assert not panel.retry_verification() and panel.failed_verification is failed
    assert len(calls) == len(Thread.all) == 1
    log.external = False
    assert panel.retry_verification(); finish(panel)
    assert len(calls) == 2 and panel.failed_verification is None


@pytest.mark.parametrize('seconds', [41, 301])
def test_timeout_can_retry_only_after_the_original_worker_has_exited(tmp_path, monkeypatch, seconds):
    panel, _context, log, state = harness(tmp_path, monkeypatch)
    assert panel.restore(state['id'])
    log.now += seconds; panel.poll()
    assert panel.pending['discard'] and panel.failed_verification is None
    assert not panel.retry_verification() and log.busy == [True]
    finish(panel)
    assert panel.pending is None and panel.failed_verification is not None
    assert log.busy == [True, False] and not log.applied
    assert panel.retry_verification(); finish(panel)
    assert panel.active == state and len(log.applied) == 1


@pytest.mark.parametrize('action', ['cancel', 'close'])
@pytest.mark.parametrize('phase', ['pending', 'failed'])
def test_cancel_and_close_remove_retry_authority_without_late_adoption(tmp_path, monkeypatch, action, phase):
    panel, context, log, state = harness(tmp_path, monkeypatch)
    fail_once(monkeypatch, 'advance', state)
    assert panel.restore(state['id'])
    if phase == 'failed':
        finish(panel)
        assert panel.failed_verification is not None
    getattr(panel, action)()
    if phase == 'pending':
        assert not panel.retry_verification() and log.busy == [True]
        finish(panel)
    before = deepcopy(context)
    assert panel.failed_verification is None and not panel.retry_verification()
    assert context == before and not log.applied and not log.prepared


def test_new_verification_supersedes_old_failure_and_success_consumes_authority(tmp_path, monkeypatch):
    panel, _context, _log, state, calls = failed_restore(tmp_path, monkeypatch)
    assert panel.restore('b' * 32)
    assert panel.failed_verification is None and not panel.retry_verification()
    finish(panel)
    assert calls[-1][0][1] == 'b' * 32 and len(calls) == 2
    assert not panel.retry_verification() and len(Thread.all) == 2


def test_create_failure_is_not_replayable_by_verification_retry(tmp_path, monkeypatch):
    panel, context, log, state = harness(tmp_path, monkeypatch)
    created = []
    def create(*args, **kwargs):
        created.append((args, kwargs)); raise PermissionError(SECRET)
    monkeypatch.setattr(module.core, 'create', create)
    assert panel.create(['first', 'second'], deepcopy(context)); finish(panel)
    assert len(created) == 1 and panel.failed_verification is None
    assert not panel.retry_verification() and len(created) == 1 and not log.applied


def test_failed_write_binding_cannot_be_replayed_by_verification_retry(tmp_path, monkeypatch):
    panel, _context, _log, state = harness(tmp_path, monkeypatch)
    panel.active = deepcopy(state); panel.issued = {'revision': 1, 'text': state['payload']['draft']}
    bound = []
    def bind(*args):
        bound.append(args); raise PermissionError(SECRET)
    monkeypatch.setattr(module.core, 'bind_preview', bind)
    assert not panel.bind(object(), state['payload']['draft'], {}, [], deepcopy(panel.issued))
    assert len(bound) == 1 and panel.failed_verification is None
    assert not panel.retry_verification() and len(bound) == 1 and not Thread.all


@pytest.mark.parametrize('kind', ['office_resume', 'bundle_resume'])
def test_resume_verification_retry_preserves_fixed_attempt_and_copied_adoption_binding(tmp_path, monkeypatch, kind):
    panel, context, log, state = resume_harness(tmp_path, monkeypatch, kind)
    open_resume(panel, log, state)()
    owned = deepcopy(panel.resume_owned); before_apply = len(log.applied)
    next_stage(state)
    calls = fail_once(monkeypatch, 'advance', state)
    assert panel.resume_finished(owned['task_root'], owned['attempt'])
    assert panel.pending['resuming'] == owned and panel.resume_owned is None
    finish(panel)
    assert panel.failed_verification['pending']['resuming'] == owned
    failed_binding = panel.failed_verification['pending']['resuming']
    assert not log.cleared and len(log.applied) == before_apply
    assert panel.retry_verification()
    copied = panel.pending['resuming']
    assert copied == owned and copied is not failed_binding
    assert copied['context'] is not failed_binding['context']
    finish(panel)
    assert calls[0] == calls[1] and calls[1][1] == {
        'resume_attempt': owned['attempt'], 'resume_index': owned['index']}
    assert len(log.started) == len(log.cleared) == 1
    assert len(log.applied) == before_apply + 1 and context['workspace']['payload'] == state['payload']
    assert context['goal']['draft']['text'] == '' and panel.failed_verification is None
    assert not panel.resume_finished(owned['task_root'], owned['attempt'])


def test_observed_outcome_verification_failure_can_retry_without_reexecuting_task(tmp_path, monkeypatch):
    panel, context, log, state, outcome = automatic(tmp_path, monkeypatch)
    calls = fail_once(monkeypatch, 'advance', state)
    panel.poll()
    token = (panel.active['id'], panel.active['index'], str(outcome))
    assert token in panel.observed and panel.pending['auto'] is True
    finish(panel)
    assert panel.pending is None and panel.failed_verification is not None and not log.applied
    panel.poll()
    assert len(Thread.all) == len(calls) == 1
    assert panel.retry_verification() and panel.pending['auto'] is True
    finish(panel)
    assert calls[0] == calls[1] and len(calls) == 2
    assert len(log.applied) == 1 and context['workspace']['payload'] == state['payload']
    assert token in panel.observed and not panel.retry_verification()
    assert not log.prepared


def test_draft_open_false_retries_complete_verification_before_same_open_callback(tmp_path, monkeypatch):
    panel, context, log, state, draft, calls, _opened = prepared(tmp_path, monkeypatch)
    before = deepcopy(context); attempts = []
    def open_draft(record):
        attempts.append(deepcopy(record))
        if len(attempts) == 1:
            log.statuses.append('草稿暂时无法读取；请解除占用。')
        return len(attempts) > 1
    panel.open_draft = open_draft
    assert panel.preview_draft(state['id'], 0, draft); finish(panel)
    assert panel.failed_verification is not None and attempts == [draft] and len(calls) == 1
    assert log.statuses[-1] == '草稿暂时无法读取；请解除占用。'
    assert panel.retry_verification() and len(calls) == 1
    finish(panel)
    assert calls == [state['id'], state['id']] and attempts == [draft, draft]
    assert panel.failed_verification is None and not panel.retry_verification()
    assert context == before and not log.applied and not log.advanced


def test_draft_callback_exception_hides_sensitive_message_and_releases_busy(tmp_path, monkeypatch):
    panel, context, log, state, draft, calls, _opened = prepared(tmp_path, monkeypatch)
    before = deepcopy(context); attempts = []
    def open_draft(record):
        attempts.append(deepcopy(record))
        if len(attempts) == 1:
            raise PermissionError(SECRET)
        return True
    panel.open_draft = open_draft
    assert panel.preview_draft(state['id'], 0, draft); finish(panel)
    assert panel.pending is None and panel.failed_verification is not None
    assert log.busy == [True, False] and all('hidden-123' not in text for text in log.statuses)
    assert '访问受阻或被占用' in log.statuses[-1] and '重新核验' in log.statuses[-1]
    assert panel.retry_verification(); finish(panel)
    assert len(calls) == len(attempts) == 2 and log.busy == [True, False, True, False]
    assert context == before and not log.applied and not log.advanced


def test_new_instruction_invalidates_retry_after_unsuccessful_draft_open(tmp_path, monkeypatch):
    panel, context, log, state, draft, calls, _opened = prepared(tmp_path, monkeypatch)
    attempts = []
    panel.open_draft = lambda record: attempts.append(record) or False
    assert panel.preview_draft(state['id'], 0, draft); finish(panel)
    assert panel.failed_verification is not None
    mutate(context, 'main_new'); before = deepcopy(context)
    assert not panel.retry_verification() and panel.failed_verification is None
    assert calls == [state['id']] and attempts == [draft] and context == before
    assert not log.applied and not log.advanced
