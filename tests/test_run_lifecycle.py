"""Real OS lease contracts; subprocesses are owned by these offline tests."""
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from agent_service import computer_agent as agent
from agent_service.computer import ComputerError, ComputerSession, read_record
from agent_service.run_lifecycle import assert_inactive, own_run


@pytest.fixture
def context(tmp_path):
    session = ComputerSession.create(tmp_path)
    plan = agent.prepare(session, 'lease test', {
        'queries':['topic'], 'title':'source', 'min_sources':1,
        'required_terms':[], 'outputs':['document']})
    return session, plan, session.root/'goals'/(plan['sha256']+'.run')


def snapshot(root):
    def contents(path):
        if path.name == 'owner.lease':
            # The Windows OS denies reads over the live locked byte zero.
            with path.open('rb') as stream:
                stream.seek(1)
                return stream.read()
        return path.read_bytes()
    return {p.relative_to(root).as_posix():(contents(p), p.stat().st_size, p.stat().st_mtime_ns)
            for p in root.rglob('*') if p.is_file()}


def test_active_and_inactive_probes_never_mutate(context):
    session, plan, run = context
    with own_run(session, plan):
        before = snapshot(session.root)
        with pytest.raises(ComputerError, match='^run_owner_active$'):
            assert_inactive(run)
        assert snapshot(session.root) == before
    assert assert_inactive(run) == read_record(run/'owner.json')
    assert snapshot(session.root) == before
    with pytest.raises(ComputerError, match='^goal_already_claimed$'):
        with own_run(session, plan):
            pytest.fail('dead runs must never be reused')
    assert snapshot(session.root) == before


@pytest.mark.parametrize('finish', ['normal', 'kill'])
def test_subprocess_lease_blocks_and_process_exit_releases(context, finish):
    session, plan, run = context
    script = '''
import json, sys
from pathlib import Path
from agent_service.computer import ComputerSession
from agent_service.run_lifecycle import own_run
session = ComputerSession(sys.argv[1])
plan = json.loads(Path(sys.argv[2]).read_text(encoding='utf-8'))
with own_run(session, plan):
    print('LEASE_READY', flush=True)
    sys.stdin.readline()
'''
    process = subprocess.Popen([sys.executable, '-c', script, str(session.root),
        str(run.with_suffix('.json'))], cwd=Path(__file__).resolve().parents[1],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == 'LEASE_READY'
        before = snapshot(session.root)
        with pytest.raises(ComputerError, match='^run_owner_active$'):
            assert_inactive(run)
        assert snapshot(session.root) == before
        if finish == 'kill':
            process.kill()
            process.communicate(timeout=10)
            assert process.returncode != 0
        else:
            _, errors = process.communicate('finish\n', timeout=10)
            assert process.returncode == 0, errors
        assert assert_inactive(run)['plan_sha256'] == plan['sha256']
        assert snapshot(session.root) == before
        assert not (run/'outcome.json').exists()
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)


@pytest.mark.parametrize('damage', ['owner_missing','owner_json','owner_fields',
    'owner_bool_schema','lease_missing','lease_token','lease_sentinel','lease_replaced',
    'claim_binding','plan_changed','session_changed'])
def test_missing_or_invalid_identity_is_refused_without_repair(context, damage):
    session, plan, run = context
    with own_run(session, plan):
        pass
    if damage == 'owner_missing':
        (run/'owner.json').unlink()
    elif damage == 'owner_json':
        (run/'owner.json').write_text('{', encoding='utf-8')
    elif damage in {'owner_fields', 'owner_bool_schema'}:
        owner = read_record(run/'owner.json')
        owner['schema'] = True if damage == 'owner_bool_schema' else 2
        (run/'owner.json').write_text(json.dumps(owner), encoding='utf-8')
    elif damage == 'lease_missing':
        (run/'owner.lease').unlink()
    elif damage == 'lease_token':
        (run/'owner.lease').write_bytes(b'!' + b'f'*32)
    elif damage == 'lease_sentinel':
        path = run/'owner.lease'
        path.write_bytes(b'?' + path.read_bytes()[1:])
    elif damage == 'lease_replaced':
        path = run/'owner.lease'
        replacement = run/'replacement.lease'
        replacement.write_bytes(path.read_bytes())
        replacement.replace(path)
    elif damage == 'claim_binding':
        (run/'claim.json').write_text(json.dumps({'approval':plan['sha256'],
            'lifecycle':{'version':1,'owner_sha256':'f'*64}}), encoding='utf-8')
    elif damage == 'plan_changed':
        data = read_record(run.with_suffix('.json'))
        data['goal']['title'] = 'different configuration'
        run.with_suffix('.json').write_text(json.dumps(data), encoding='utf-8')
    elif damage == 'session_changed':
        (session.root/'session.json').write_text('{"schema":2}', encoding='utf-8')
    before = snapshot(session.root)
    expected = 'run_owner_missing' if damage == 'owner_missing' else 'run_owner_invalid'
    with pytest.raises(ComputerError, match='^' + expected + '$'):
        assert_inactive(run)
    assert snapshot(session.root) == before


def test_execute_keeps_lease_during_adapter_and_cleanup(context):
    session, plan, run = context
    events = []
    def assert_live():
        with pytest.raises(ComputerError, match='^run_owner_active$'):
            assert_inactive(run)
    class Environment:
        def __init__(self, goal, root, **kwargs):
            assert root == run and (root/'claim.json').exists()
            assert_live()
            events.append('init')
        def observe(self):
            assert_live()
            return {'checks':{'sources_ready':True,'goal_met':True},
                    'artifacts':[], 'source_reads':1, 'failures':[]}
        def close(self):
            assert read_record(run/'outcome.json')['ok'] is True
            assert_live()
            events.append('cleanup')
    result = agent.execute(session, plan, approval=plan['sha256'], environment_factory=Environment)
    assert result['ok'] and events == ['init','cleanup']
    assert assert_inactive(run)['plan_sha256'] == plan['sha256']


def test_independent_adapter_worker_observes_parent_lease_active(context):
    session, plan, run = context
    script = '''
import sys
from agent_service.computer import ComputerError
from agent_service.run_lifecycle import assert_inactive
try:
    assert_inactive(sys.argv[1])
except ComputerError as exc:
    assert str(exc) == 'run_owner_active', str(exc)
else:
    raise AssertionError('adapter worker observed a released owner')
'''
    class Environment:
        def __init__(self, *args, **kwargs):
            self.done = False
        def observe(self):
            return {'checks':{'sources_ready':self.done,'goal_met':self.done},
                    'artifacts':[], 'source_reads':int(self.done), 'failures':[]}
        def actions(self, observation):
            return [{'kind':'search','query_index':0}]
        def execute(self, action, root):
            worker = subprocess.run([sys.executable,'-c',script,str(run)],
                cwd=Path(__file__).resolve().parents[1], capture_output=True,
                text=True, timeout=10)
            assert worker.returncode == 0, worker.stderr
            self.done = True
            return {'ok':True,'state':'verified','code':'verified'}
    result = agent.execute(session, plan, approval=plan['sha256'], environment_factory=Environment)
    assert result['ok'] and result['actions'] == 1
    assert_inactive(run)


def test_initialization_failure_releases_but_does_not_reset_run(context):
    session, plan, run = context
    with pytest.raises(RuntimeError):
        with own_run(session, plan):
            raise RuntimeError('controlled failure')
    assert_inactive(run)
    with pytest.raises(ComputerError, match='^goal_already_claimed$'):
        with own_run(session, plan):
            pass


@pytest.mark.parametrize('signal', [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize('cleanup_fails', [False, True])
def test_base_exception_cleans_up_under_lease_without_terminal_outcome(context, signal, cleanup_fails):
    session, plan, run = context
    closed = []
    class Environment:
        def __init__(self, *args, **kwargs):
            pass
        def observe(self):
            raise signal('controlled interruption')
        def close(self):
            with pytest.raises(ComputerError, match='^run_owner_active$'):
                assert_inactive(run)
            closed.append(True)
            if cleanup_fails:
                raise RuntimeError('controlled cleanup failure')
    with pytest.raises(signal, match='controlled interruption'):
        agent.execute(session, plan, approval=plan['sha256'], environment_factory=Environment)
    assert closed == [True] and not (run/'outcome.json').exists()
    assert (run/'cleanup.json').exists() == cleanup_fails
    assert_inactive(run)


def test_linked_root_is_refused_before_new_files(context, tmp_path):
    session, plan, run = context
    alias = tmp_path/'alias'
    try:
        alias.symlink_to(session.root, target_is_directory=True)
    except OSError:
        pytest.skip('symlink privilege unavailable')
    with pytest.raises(ComputerError, match='^linked_root$'):
        with own_run(SimpleNamespace(root=alias), plan):
            pass
    assert not run.exists()
    with own_run(session, plan):
        pass
    before = snapshot(session.root)
    with pytest.raises(ComputerError, match='^run_owner_invalid$'):
        assert_inactive(alias/'goals'/run.name)
    assert snapshot(session.root) == before
