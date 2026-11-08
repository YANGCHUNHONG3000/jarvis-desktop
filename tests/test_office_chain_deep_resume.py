"""Deep-step exact bundle recovery with real saved proofs and offline Office I/O."""
import importlib
from pathlib import Path

import pytest

from agent_service import office_task_chain as chain, office_bundle
from agent_service.computer import read_record
from test_artifact_source import fake
from test_office_task_chain import local, route, succeed_capability
from test_office_task_chain_bundle_resume import execute, resume


def snapshot(*roots):
    return {p: (p.read_bytes(), p.stat().st_mtime_ns)
            for root in roots for p in root.rglob('*') if p.is_file()}


def unchanged(protected):
    assert all(p.is_file() and (p.read_bytes(), p.stat().st_mtime_ns) == value
               for p, value in protected.items())


def prepare_deep(s, total):
    instructions = ['从选中资料生成明确的新表格', '依据上一步表格生成Excel与Word副本']
    instructions.extend(f'第{i + 1}步逐格核对当前两份表格并生成Excel和Word核对报告'
                        for i in range(2, total))
    s.ident = chain.create(s.store, instructions, s.payload, s.base, s.session.root.parent)
    for index in range(2):
        view, refs = route(s, index)
        chain.bind_preview(s.store, s.ident, index, s.session, view, refs)
        succeed_capability(s, view, count=1 if index == 0 else 2)
        state = chain.advance(s.store, s.ident)
        assert state['index'] == index + 1 and state['total'] == total
    return state


@pytest.mark.parametrize('total', [3, 4])
def test_third_and_fourth_internal_resume_only_completes_missing_report_and_binds_exact_attempt(local, total):
    s = local
    state = prepare_deep(s, total)
    chain_folder = s.store / ('chain-' + s.ident)
    old_attempt = None
    for index in range(2, total):
        view, refs = route(s, index)
        s.plan, s.refs = view['plan'], refs
        state = chain.bind_preview(s.store, s.ident, index, s.session, view, refs)
        assert state['status'] == 'preview'
        original, client = execute(s)
        paused = chain.inspect(s.store, s.ident)
        assert original['ok'] is False and original['state'] == 'cancelled'
        assert paused['index'] == index and paused['total'] == total
        assert paused['status'] == 'resume_available'
        assert paused['resume_preview']['kind'] == 'bundle_resume'
        assert (paused['resume_preview']['completed'], paused['resume_preview']['remaining']) == (1, 1)
        current_completion = chain_folder / f'step-{index}-complete.json'
        before_wrong_attempt = snapshot(chain_folder)

        if old_attempt is not None:
            # The successful third-stage event must not claim the paused fourth stage.
            with pytest.raises(chain.OfficeTaskChainError, match='resume_unverified'):
                chain.advance(s.store, s.ident, resume_attempt=old_attempt)
            assert not current_completion.exists()
            unchanged(before_wrong_attempt)

        if total == 3:
            # Same instruction and sources still produce a distinct task owner.
            foreign_plan = office_bundle.prepare(s.session, s.plan['instruction'], refs, task_base=s.base)
            execute(s, plan=foreign_plan)
            foreign_task = s.base / ('task-' + foreign_plan['request_id'])
            foreign_attempt, foreign_result = resume(s, task=foreign_task)
            assert foreign_result['task_root'] == str(foreign_task)
            with pytest.raises(chain.OfficeTaskChainError, match='resume_unverified'):
                chain.advance(s.store, s.ident, resume_attempt=foreign_attempt)
            assert not current_completion.exists()
            unchanged(before_wrong_attempt)

        task, run = Path(paused['task_root']), Path(original['run'])
        # Protect all previously written task, receipt, prefix artifact and chain bytes.
        protected = snapshot(s.base, s.session.root, chain_folder)
        native_calls = []
        def missing_only(spec, output, stop):
            native_calls.append(spec['kind'])
            return fake(spec, output, stop)
        attempt, result = resume(s, native=missing_only)
        state = importlib.reload(chain).advance(s.store, s.ident, resume_attempt=attempt)
        assert state['index'] == index + 1 and state['total'] == total
        assert state['status'] == ('complete' if index + 1 == total else 'ready')
        assert state['payload']['source'] is None
        assert state['payload']['records'] == [
            {'path': a['path'], 'sha256': a['sha256']} for a in result['artifacts']]
        assert native_calls == ['document'] and len(client.calls) == 1
        unchanged(protected)
        assert read_record(task / 'result.json')['ok'] is False
        completion = read_record(current_completion)
        assert completion['outcome']['bundle_resume']['attempt_id'] == attempt
        assert completion['outcome']['bundle_resume']['task_root'] == str(task)
        assert office_bundle.inspect_saved(run, task_base=s.base,
            computer_base=s.session.root.parent) == original
        old_attempt = attempt
        if index + 1 < total:
            # A duplicated late completion event cannot advance an unplanned next step.
            with pytest.raises(chain.OfficeTaskChainError, match='resume_unverified'):
                chain.advance(s.store, s.ident, resume_attempt=attempt)
            assert not (chain_folder / f'step-{index + 1}-preview.json').exists()
    final_proofs = snapshot(chain_folder)
    assert chain.advance(s.store, s.ident) == state
    unchanged(final_proofs)
    assert len(list(chain_folder.glob('step-*-complete.json'))) == total
