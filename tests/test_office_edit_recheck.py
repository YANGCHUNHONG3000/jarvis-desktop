"""Read-only recovery binds real failed-run records; native reads are simulated."""
from pathlib import Path
import subprocess
import sys
import tempfile

import pytest
from openpyxl import Workbook

from agent_service import computer_agent, computer_interaction as interaction
from agent_service import office_edit as edit, office_edit_host as host, office_edit_recheck as recheck
from agent_service import run_lifecycle
from agent_service.computer import ComputerSession, ComputerError, read_record, write_new
from agent_service.computer_agent_tools import _load
from agent_service.external_document_source_host import _child_environment


@pytest.fixture
def short_root():
    # Keep generated run/step evidence within legacy Windows path limits.
    with tempfile.TemporaryDirectory(prefix='jr-') as folder:
        yield Path(folder).resolve()


@pytest.fixture
def failed(short_root):
    tmp_path = short_root
    source = tmp_path / 'original.xlsx'
    book = Workbook()
    book.active['A1'], book.active['A2'] = 2, '=SUM(A1:A1)'
    book.save(source)
    book.close()
    session = ComputerSession.create(tmp_path / 'sessions')
    plan = interaction.prepare(session, '修改一个单元格', edits=[{'path': str(source), 'changes': [
        {'sheet': 'Sheet', 'cell': 'A1', 'before': 2, 'after': 3}]}])
    base = tmp_path / 'tasks'
    def initial_failure(spec, output, source_root, **kwargs):
        _, observed = edit._bind(spec, source_root)
        artifact = Path(output) / 'edited-spreadsheet.xlsx'
        edit._make_copy(spec, Path(source_root) / 'source.xlsx', artifact)
        edit.verify_preservation(Path(source_root) / 'source.xlsx', artifact, spec)
        value = {'ok': False, 'state': 'unknown', 'stage': 'native_readback',
            'code': 'edit_application_mismatch', 'file_verified': True, 'preservation_verified': True,
            'application_verified': False, 'artifact': str(artifact), 'path': str(artifact),
            'sha256': edit._hash(artifact), 'source_sha256': observed['sha256']}
        write_new(Path(output) / 'worker-request.json', {'spec': spec, 'source_root': str(source_root)})
        write_new(Path(output) / 'worker-outcome.json', value)
        return value
    outcome = computer_agent.execute(session, plan, approval=plan['sha256'],
        office_execute=initial_failure, task_base=base)
    assert outcome['state'] == 'unknown'
    assert list(base.glob('task-*/edit/edited-spreadsheet.xlsx')), outcome
    artifact = next(base.glob('task-*/edit/edited-spreadsheet.xlsx'))
    return artifact, edit._hash(artifact), base, session, plan


def supervised_readback(monkeypatch):
    calls = []
    def readback(path, spec, observed, check):
        check()
        calls.append(str(path))
        return {'progid': 'test-only', 'name': 'simulated native contract',
                'version': 'test', 'changes': spec['changes']}
    monkeypatch.setattr(edit, '_native_readback', readback)
    def supervised(payload, root, *, stop):
        write_new(root / 'worker-request.json', payload)
        write_new(root / 'worker-ready.json', {'ready': True})
        write_new(root / 'worker-gated.json', {'gated': True})
        native = recheck._worker_readback(payload, root, stop)
        write_new(root / 'worker-outcome.json', native)
        write_new(root / 'supervision.json', {'exit_code': 0, 'read_only': True})
        return native
    monkeypatch.setattr(host, 'execute_recheck_supervised', supervised)
    return calls


def test_inspect_is_passive_and_success_rechecks_existing_copy_without_replay(failed, monkeypatch):
    artifact, sha, base, session, plan = failed
    before = recheck._files(artifact.parent, output=True)
    old_run = recheck._files(session.root / 'goals' / (plan['sha256'] + '.run'))
    preview = recheck.inspect(artifact, sha, base)
    assert not (artifact.parent / 'native-rechecks').exists()
    assert preview['observed']['sheets'][0]['cells'][0]['value'] == 3
    calls = supervised_readback(monkeypatch)
    monkeypatch.setattr(edit, '_make_copy', lambda *a, **k: pytest.fail('copy write replayed'))
    result = recheck.execute(artifact, sha, base=base, approval=preview['sha256'])
    assert result['ok'] and calls == [str(artifact)]
    verified = recheck.verify(artifact, sha, base=base)
    assert verified == {'native': result['native'], 'proof_sha256': result['proof_sha256']}
    assert edit.verify_edit_result(result['native'], preview['source_root'], preview['spec']) == result['native']
    assert recheck._files(artifact.parent, output=True) == before
    assert recheck._files(session.root / 'goals' / (plan['sha256'] + '.run')) == old_run
    assert read_record(Path(preview['run']) / 'outcome.json')['state'] == 'unknown'
    with pytest.raises(ComputerError, match='already_verified'):
        recheck.execute(artifact, sha, base=base, approval=preview['sha256'])
    assert len(calls) == 1


def test_changed_approval_cannot_start_worker(failed, monkeypatch):
    artifact, sha, base, *_ = failed
    monkeypatch.setattr(host, 'execute_recheck_supervised', lambda *a, **k: pytest.fail('started'))
    with pytest.raises(ComputerError, match='approval_invalid'):
        recheck.execute(artifact, sha, base=base, approval='0' * 64)
    assert not (artifact.parent / 'native-rechecks').exists()


def test_native_failure_preserves_new_attempt_and_does_not_publish_pointer(failed, monkeypatch):
    artifact, sha, base, *_ = failed
    preview = recheck.inspect(artifact, sha, base)
    monkeypatch.setattr(host, 'execute_recheck_supervised', lambda *a, **k:
        {'ok': False, 'state': 'unknown', 'code': 'edit_application_mismatch'})
    result = recheck.execute(artifact, sha, base=base, approval=preview['sha256'])
    assert result['ok'] is False
    assert read_record(Path(result['recheck']) / 'result.json') == result
    assert not (artifact.parent / 'native-recheck.json').exists()
    assert not (artifact.parent / 'native-edit-receipt.json').exists()


def test_unbound_native_receipt_cannot_be_adopted(failed):
    artifact, sha, base, *_ = failed
    write_new(artifact.parent / 'native-edit-receipt.json', {'fake': True})
    with pytest.raises(ComputerError, match='receipt_conflict'):
        recheck.inspect(artifact, sha, base)


def test_active_original_run_is_not_recheckable(failed):
    artifact, sha, base, session, plan = failed
    run = session.root / 'goals' / (plan['sha256'] + '.run')
    with (run / 'owner.lease').open('r+b', buffering=0) as stream:
        run_lifecycle._lock(stream)
        try:
            with pytest.raises(ComputerError, match='run_owner_active'):
                recheck.inspect(artifact, sha, base)
        finally:
            run_lifecycle._unlock(stream)


@pytest.mark.parametrize('target', ['artifact', 'source', 'proof', 'worker', 'old_outcome', 'receipt', 'result'])
def test_changed_bound_bytes_block_published_verification(failed, monkeypatch, target):
    artifact, sha, base, *_ = failed
    preview = recheck.inspect(artifact, sha, base)
    supervised_readback(monkeypatch)
    result = recheck.execute(artifact, sha, base=base, approval=preview['sha256'])
    assert result['ok']
    paths = {'artifact': artifact, 'source': Path(preview['source_root']) / 'source.xlsx',
        'proof': Path(result['recheck']) / 'proof.json', 'worker': Path(result['recheck']) / 'worker-outcome.json',
        'old_outcome': Path(preview['run']) / 'outcome.json',
        'result': Path(result['recheck']) / 'result.json',
        'receipt': artifact.parent / 'native-edit-receipt.json'}
    with paths[target].open('ab') as stream:
        stream.write(b'changed')
    with pytest.raises((ComputerError, ValueError, OSError)):
        recheck.verify(artifact, sha, base=base)


def test_original_failure_must_have_preservation_proof(failed):
    artifact, sha, base, *_ = failed
    from agent_service.computer_agent_tools import _save
    manifest, _ = _load(artifact.parent / 'edit-manifest.json')
    path = Path(manifest['step']) / 'environment-result.json'
    record, _ = _load(path)
    record['native']['preservation_verified'] = False
    # Simulate a different historical record, not a valid later recheck.
    path.unlink()
    _save(path, record)
    with pytest.raises(ComputerError, match='native_failure_invalid'):
        recheck.inspect(artifact, sha, base)


@pytest.mark.parametrize('field,value', [('engine', 'other_engine'), ('code', 'other_failure'),
    ('recovery', {'parent_run': 'unbound'})])
def test_original_outcome_contract_cannot_change(failed, field, value):
    artifact, sha, base, session, plan = failed
    path = session.root / 'goals' / (plan['sha256'] + '.run') / 'outcome.json'
    outcome = read_record(path)
    outcome[field] = value
    path.unlink()
    write_new(path, outcome)
    with pytest.raises(ComputerError, match='outcome_invalid'):
        recheck.inspect(artifact, sha, base)


def test_original_claim_approval_is_required(failed):
    artifact, sha, base, session, plan = failed
    path = session.root / 'goals' / (plan['sha256'] + '.run') / 'claim.json'
    claim = read_record(path)
    claim['approval'] = '0' * 64
    path.unlink()
    write_new(path, claim)
    with pytest.raises(ComputerError, match='run_owner_invalid|approval_invalid'):
        recheck.inspect(artifact, sha, base)


def test_location_guard_is_rechecked_during_passive_inspection(failed, monkeypatch):
    artifact, sha, base, *_ = failed
    def changed(item):
        raise ComputerError('interaction_location_changed')
    monkeypatch.setattr(interaction, 'verify_location', changed)
    with pytest.raises(ComputerError, match='interaction_location_changed'):
        recheck.inspect(artifact, sha, base)


def test_cancelled_worker_cannot_publish_receipt(failed, monkeypatch):
    artifact, sha, base, *_ = failed
    preview = recheck.inspect(artifact, sha, base)
    calls = supervised_readback(monkeypatch)
    result = recheck.execute(artifact, sha, base=base, approval=preview['sha256'], stop=lambda: True)
    assert result['ok'] is False and not calls
    assert not (artifact.parent / 'native-edit-receipt.json').exists()
    assert not (artifact.parent / 'native-recheck.json').exists()


def test_artifact_change_during_native_readback_is_rejected_before_receipt(failed, monkeypatch):
    artifact, sha, base, *_ = failed
    preview = recheck.inspect(artifact, sha, base)
    supervised_readback(monkeypatch)
    def changed(*args):
        with artifact.open('ab') as stream:
            stream.write(b'changed')
        return {'progid': 'test-only'}
    monkeypatch.setattr(edit, '_native_readback', changed)
    result = recheck.execute(artifact, sha, base=base, approval=preview['sha256'])
    assert result['ok'] is False
    assert not (artifact.parent / 'native-edit-receipt.json').exists()
    assert not (artifact.parent / 'native-recheck.json').exists()


def test_worker_request_must_be_owned_by_the_artifact_recheck_directory(failed, tmp_path):
    artifact, sha, base, *_ = failed
    preview = recheck.inspect(artifact, sha, base)
    payload = {'preview': preview, 'approval': preview['sha256']}
    write_new(tmp_path / 'request.json', payload)
    with pytest.raises(ComputerError, match='worker_owner_invalid'):
        recheck._worker_readback(payload, tmp_path, lambda: False)
    assert not (artifact.parent / 'native-edit-receipt.json').exists()


def test_failed_pointer_publication_leaves_an_explicit_failed_attempt(failed, monkeypatch):
    artifact, sha, base, *_ = failed
    preview = recheck.inspect(artifact, sha, base)
    supervised_readback(monkeypatch)
    original = recheck.write_new
    def fail_pointer(path, value):
        if Path(path).name == 'native-recheck.json':
            raise ValueError('controlled_pointer_failure')
        return original(path, value)
    monkeypatch.setattr(recheck, 'write_new', fail_pointer)
    result = recheck.execute(artifact, sha, base=base, approval=preview['sha256'])
    assert result['ok'] is False and not (artifact.parent / 'native-recheck.json').exists()
    assert read_record(Path(result['recheck']) / 'failure.json') == result


def test_recheck_worker_exits_with_parent_pipe_still_open(tmp_path):
    write_new(tmp_path / 'worker-request.json', {})
    script = ('import sys; from agent_service import office_edit_recheck as r,office_edit_host as h; '
        'r._worker_readback=lambda *a: {"ok":True,"controlled":True}; '
        'raise SystemExit(h.worker(sys.argv[1],10,recheck=True))')
    process = subprocess.Popen([sys.executable, '-B', '-c', script, str(tmp_path)],
        cwd=host.ROOT, env=_child_environment(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        process.stdin.write(b'1')
        process.stdin.flush()
        process.wait(timeout=15)
        assert process.returncode == 0, process.stderr.read().decode('utf-8', 'replace')
        assert read_record(tmp_path / 'worker-outcome.json') == {'ok': True, 'controlled': True}
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        for pipe in (process.stdin, process.stdout, process.stderr):
            pipe.close()


def test_nonzero_recheck_worker_cannot_publish_saved_success(tmp_path, monkeypatch):
    class Pipe:
        def write(self, value): pass
        def flush(self): pass
        def close(self): pass
    class Process:
        stdin = Pipe()
        stdout = Pipe()
        returncode = 2
        def poll(self): return self.returncode
    write_new(tmp_path / 'worker-outcome.json', {'ok': True})
    monkeypatch.setattr(host.subprocess, 'Popen', lambda *a, **k: Process())
    monkeypatch.setattr('run_jarvis_desktop.desktop_python', lambda: sys.executable)
    monkeypatch.setattr(recheck, '_native', lambda *a: pytest.fail('validated nonzero result'))
    result = host.execute_recheck_supervised({'preview': {}}, tmp_path)
    assert result == {'ok': False, 'state': 'unknown', 'code': 'edit_recheck_worker_exited'}
    assert not (tmp_path / 'supervision.json').exists()
