"""Offline, read-only navigation proofs; no live API or Office calls."""
from copy import deepcopy
import os
from pathlib import Path
import subprocess
import uuid

import pytest

from agent_service import mail_review_navigation as navigation
from agent_service import mail_review_resume as resume, office_workflow as workflow
from test_mail_review_resume import _stopped, _read, _write, _stamp, _paused_child
from test_mail_understanding_resume import _interrupted


def _association(tmp_path):
    parent, base, _, view = _stopped(tmp_path)
    payload = resume.prepare_resume(parent, base, view['token'])
    child = base / ('task-' + uuid.uuid4().hex)
    child.mkdir()
    marker = resume.claim_resume(payload, child, base)
    _write(child / 'mail-analysis-claim.json', {'schema': 1, 'review_resume': workflow._hash(marker)})
    return parent, child, base, marker


def _paths(parent, child, base):
    registry = base / '.mail-review-resumes'
    return [registry / (parent.name + '.json'), registry / 'children' / (child.name + '.json'),
            child / 'mail-review-resume.json', child / 'mail-analysis-claim.json']


def _reject(parent, base, code):
    with pytest.raises(navigation.MailReviewNavigationError) as caught:
        navigation.resolve_continuation(parent, base)
    assert caught.value.safe_code == 'mail_review_navigation_' + code
    assert str(caught.value) == caught.value.safe_code
    return caught.value


def test_exact_association_is_readonly_and_does_not_list_history(tmp_path, monkeypatch):
    parent, child, base, marker = _association(tmp_path)
    before = _stamp(base)
    real_iterdir = Path.iterdir
    registry = base / '.mail-review-resumes'
    def limited(path):
        if path in (base, registry, registry / 'children'):
            pytest.fail('navigation scanned global task or claim directory')
        return real_iterdir(path)
    monkeypatch.setattr(Path, 'iterdir', limited)
    resolved = navigation.resolve_continuation(parent, base)
    assert resolved['parent_root'] == str(parent)
    assert resolved['child_root'] == str(child) and resolved['token'] == marker['token']
    monkeypatch.setattr(resume, '_snapshot', lambda *_: pytest.fail('UI guard reread full parent'))
    assert navigation.link_unchanged(resolved, base)
    assert not navigation.link_unchanged({**resolved, 'token': '0' * 64}, base)
    assert not navigation.link_unchanged({**resolved, 'child_root': str(parent)}, base)
    assert _stamp(base) == before


def test_unclaimed_parent_has_explicit_missing_message(tmp_path):
    parent, base, _, _ = _stopped(tmp_path)
    error = _reject(parent, base, 'missing')
    assert '尚无' in navigation.describe_error(error)


@pytest.mark.parametrize('index', [1, 2, 3])
def test_partial_claim_is_incomplete_and_never_released(tmp_path, index):
    parent, child, base, _ = _association(tmp_path)
    _paths(parent, child, base)[index].unlink()
    before = _stamp(base)
    _reject(parent, base, 'incomplete')
    assert _stamp(base) == before
    assert resume.inspect_resume(parent, base)['code'] == 'mail_review_resume_already_claimed'


def test_missing_child_is_unavailable(tmp_path):
    parent, child, base, _ = _association(tmp_path)
    child.rename(base / 'retained-child')
    _reject(parent, base, 'unavailable')


@pytest.mark.parametrize('index', [0, 1, 2, 3])
@pytest.mark.parametrize('change', ['extra', 'schema_bool', 'bad_json'])
def test_each_small_claim_is_strict(tmp_path, index, change):
    parent, child, base, _ = _association(tmp_path)
    path = _paths(parent, child, base)[index]
    if change == 'bad_json':
        path.write_text('{', encoding='utf-8')
    else:
        value = _read(path)
        value['extra' if change == 'extra' else 'schema'] = True
        _write(path, value)
    _reject(parent, base, 'changed')


@pytest.mark.parametrize('field,value', [
    ('parent_root', 'unrelated'), ('token', 'f' * 64), ('token', True),
    ('candidate_sha256', 'f' * 64), ('plan_sha256', 'f' * 64),
    ('child_root', 'relative'), ('schema', 2),
])
def test_consistent_marker_tamper_cannot_change_association(tmp_path, field, value):
    parent, child, base, marker = _association(tmp_path)
    marker[field] = value
    paths = _paths(parent, child, base)
    for path in paths[:3]:
        _write(path, marker)
    _write(paths[3], {'schema': 1, 'review_resume': workflow._hash(marker)})
    _reject(parent, base, 'changed')


@pytest.mark.parametrize('target', ['self', 'outside', 'other_child'])
def test_child_scope_and_exact_index_cannot_be_substituted(tmp_path, target):
    parent, child, base, marker = _association(tmp_path)
    other = parent if target == 'self' else ((tmp_path if target == 'outside' else base) / ('task-' + uuid.uuid4().hex))
    if target != 'self':
        other.mkdir()
    changed = deepcopy(marker); changed['child_root'] = str(other)
    _write(_paths(parent, child, base)[0], changed)
    _reject(parent, base, 'incomplete' if target == 'other_child' else 'changed')


@pytest.mark.parametrize('relative', ['result.json', 'mail-analysis-candidate.json',
                                      'mail-analysis-plan.json', 'sources/source.docx'])
def test_parent_evidence_tamper_rejected(tmp_path, relative):
    parent, _, base, _ = _association(tmp_path)
    path = parent / relative
    path.write_bytes(path.read_bytes() + b' ')
    _reject(parent, base, 'changed')


def test_unrelated_broken_claim_does_not_affect_lookup(tmp_path):
    parent, child, base, _ = _association(tmp_path)
    registry = base / '.mail-review-resumes'
    for folder in (registry, registry / 'children'):
        (folder / ('task-' + 'f' * 32 + '.json')).write_text('not json', encoding='utf-8')
    assert navigation.resolve_continuation(parent, base)['child_root'] == str(child)


@pytest.mark.parametrize('state,ok', [('failed', False), ('unknown', False), ('succeeded', True)])
def test_association_does_not_claim_result_or_artifact_success(tmp_path, state, ok):
    parent, child, base, marker = _association(tmp_path)
    result = {'root': str(child), 'state': state, 'ok': ok, 'kind': 'office',
              'completed': [], 'artifacts': [], 'mail_review_resume': marker}
    _write(child / 'result.json', result)
    if ok:
        _reject(parent, base, 'changed')
        return
    resolved = navigation.resolve_continuation(parent, base)
    assert not {'ok', 'state', 'artifacts', 'completed'} & resolved.keys()
    assert _read(child / 'result.json') == result


@pytest.mark.parametrize('which', ['child', 'children', 'parent_claim', 'child_marker'])
def test_real_symlink_rejected(tmp_path, which):
    parent, child, base, _ = _association(tmp_path)
    paths = _paths(parent, child, base)
    path = {'child': child, 'children': paths[1].parent,
            'parent_claim': paths[0], 'child_marker': paths[2]}[which]
    retained = path.with_name(path.name + '-retained')
    is_dir = path.is_dir()
    path.rename(retained)
    try:
        path.symlink_to(retained, target_is_directory=is_dir)
    except OSError as exc:
        pytest.skip('local symlink privilege unavailable: ' + str(exc.winerror))
    _reject(parent, base, 'changed')


@pytest.mark.skipif(os.name != 'nt', reason='Windows junction contract')
def test_real_junction_child_rejected(tmp_path):
    parent, child, base, _ = _association(tmp_path)
    retained = child.with_name(child.name + '-retained')
    child.rename(retained)
    made = subprocess.run(['cmd', '/c', 'mklink', '/J', str(child), str(retained)],
                          capture_output=True, text=True)
    assert made.returncode == 0, made.stderr
    _reject(parent, base, 'changed')


@pytest.mark.parametrize('which', ['parent_claim', 'index', 'marker', 'execution', 'parent_file', 'child_directory'])
def test_fast_guard_rejects_late_changes_without_full_parent_revalidation(tmp_path, monkeypatch, which):
    parent, child, base, _ = _association(tmp_path)
    resolved = navigation.resolve_continuation(parent, base)
    monkeypatch.setattr(resume, '_snapshot', lambda *_: pytest.fail('UI guard reread full parent'))
    if which == 'child_directory':
        child.rename(base / 'retained-child')
        child.mkdir()
    else:
        paths = _paths(parent, child, base)
        path = parent / 'request.txt' if which == 'parent_file' else paths[
            {'parent_claim': 0, 'index': 1, 'marker': 2, 'execution': 3}[which]]
        path.write_bytes(path.read_bytes() + b' ')
    assert not navigation.link_unchanged(resolved, base)


def test_initial_child_can_prepare_but_new_result_requires_fresh_proof(tmp_path):
    parent, child, base, _ = _association(tmp_path)
    resolved = navigation.resolve_continuation(parent, base)
    (child / 'request.txt').write_text('将所选邮件整理成Word和Excel', encoding='utf-8')
    assert navigation.link_unchanged(resolved, base)
    _write(child / 'result.json', {'state': 'unknown'})
    assert not navigation.link_unchanged(resolved, base)
    assert not navigation.link_unchanged(resolved, tmp_path)


@pytest.mark.parametrize('copy_result_marker', [False, True])
def test_consistent_claim_redirection_to_unrelated_successful_office_is_rejected(tmp_path, monkeypatch, copy_result_marker):
    parent, original_child, base, marker = _association(tmp_path)
    unrelated, other_base, _, _, execute, _, result = _interrupted(tmp_path, monkeypatch, requested_pause=True)
    assert other_base == base
    steps = _read(unrelated / 'mail-analysis-plan.json')['steps']
    work = unrelated / ('workflow-' + uuid.uuid4().hex)
    run = workflow.run_office_workflow(steps, work, execute=execute)
    assert run['ok']
    result.update(ok=True, state='succeeded', completed=run['completed'], workflow_root=str(work),
                  artifacts=[{'step': key, 'path': value['artifact'], 'sha256': value['sha256']}
                             for key, value in run['receipts'].items()])
    marker['child_root'] = str(unrelated)
    if copy_result_marker:
        result['mail_review_resume'] = marker
    _write(unrelated / 'result.json', result)
    paths = _paths(parent, unrelated, base)
    for path in paths[:3]:
        _write(path, marker)
    _write(paths[3], {'schema': 1, 'review_resume': workflow._hash(marker)})
    # Preserve the complete old parent proof so refusal tests child binding.
    assert resume._snapshot(parent, base)['token'] == marker['token']
    _reject(parent, base, 'changed')


def test_real_offline_review_child_with_office_proof_can_navigate(tmp_path, monkeypatch):
    parent, child, base, _, _, _, _ = _paused_child(tmp_path, monkeypatch)
    resolved = navigation.resolve_continuation(parent, base)
    assert resolved['child_root'] == str(child)
    assert navigation.link_unchanged(resolved, base)
    path = child / 'mail-analysis-review.json'
    path.write_bytes(path.read_bytes() + b' ')
    assert not navigation.link_unchanged(resolved, base)


@pytest.mark.parametrize('name', ['request.txt', 'mail-analysis-request.json',
                                'mail-analysis-candidate.json', 'mail-analysis-plan.json'])
def test_partial_child_payload_must_match_parent(tmp_path, name):
    parent, child, base, _ = _association(tmp_path)
    path = child / name
    path.write_text('unrelated' if name == 'request.txt' else '{}', encoding='utf-8')
    _reject(parent, base, 'changed')


def test_new_effect_directory_requires_fresh_worker_proof(tmp_path):
    parent, child, base, _ = _association(tmp_path)
    resolved = navigation.resolve_continuation(parent, base)
    (child / 'edit').mkdir()
    assert not navigation.link_unchanged(resolved, base)
    _reject(parent, base, 'incomplete')


@pytest.mark.parametrize('tamper', ['manifest', 'alternate_edit'])
def test_review_proof_cannot_be_combined_with_another_effect(tmp_path, monkeypatch, tamper):
    parent, child, base, _, _, result, _ = _paused_child(tmp_path, monkeypatch)
    if tamper == 'alternate_edit':
        (child / 'edit').mkdir()
    else:
        path = Path(result['workflow_root']) / 'manifest.json'
        value = _read(path)
        value['steps'][0]['data']['title'] = 'unrelated workflow'
        value['steps_sha256'] = workflow._hash(value['steps'])
        _write(path, value)
    _reject(parent, base, 'changed')


def test_child_cannot_switch_to_another_history_owner(tmp_path):
    parent, child, base, marker = _association(tmp_path)
    _write(child / 'result.json', {'root': str(child), 'kind': 'office', 'state': 'failed',
        'ok': False, 'completed': [], 'artifacts': [], 'mail_review_resume': marker,
        'skill': {'id': 'unrelated'}})
    _reject(parent, base, 'changed')


def test_duplicate_json_keys_rejected_and_error_text_is_fixed(tmp_path):
    parent, child, base, _ = _association(tmp_path)
    path = _paths(parent, child, base)[0]
    raw = path.read_text(encoding='utf-8')
    path.write_text(raw[:-1] + ', "schema": 1}', encoding='utf-8')
    error = _reject(parent, base, 'changed')
    assert navigation.describe_error(error) == navigation.describe_error(error.safe_code)
    assert str(tmp_path) not in navigation.describe_error(error)
