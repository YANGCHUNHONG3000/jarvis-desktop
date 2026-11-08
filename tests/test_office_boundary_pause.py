"""A deliberate pause is only recorded after a durable verified receipt."""
from copy import deepcopy
from pathlib import Path
import pytest
from agent_service.office_workflow import run_office_workflow, WorkflowError
from test_office_workflow import root, sheet_step, report_step, fake_execute


def test_pause_then_resume_pauses_only_after_new_step(tmp_path):
    workspace = root(tmp_path)
    third = deepcopy(sheet_step()); third['id'] = 's3'
    steps = [sheet_step(), report_step(), third]
    calls = []
    first = run_office_workflow(steps, workspace, execute=fake_execute(calls), pause_after_step=True)
    assert first['state'] == 'cancelled' and first['error'] == 'office_paused_after_verified_step'
    assert first['completed'] == ['s1'] and not (workspace / 's2').exists()
    protected = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (workspace / 's1').iterdir()}
    second = run_office_workflow(steps, workspace, execute=fake_execute(calls), resume=True, pause_after_step=True)
    assert second['completed'] == ['s1', 's2'] and second['state'] == 'cancelled'
    assert len(calls) == 2 and not (workspace / 's3').exists()
    final = run_office_workflow(steps, workspace, execute=fake_execute(calls), resume=True, pause_after_step=True)
    assert final['ok'] and final['completed'] == ['s1', 's2', 's3'] and len(calls) == 3
    assert all((p.read_bytes(), p.stat().st_mtime_ns) == v for p, v in protected.items())


def test_single_final_step_is_success_not_paused(tmp_path):
    result = run_office_workflow([sheet_step()], root(tmp_path), execute=fake_execute([]), pause_after_step=True)
    assert result['state'] == 'succeeded' and 'error' not in result


@pytest.mark.parametrize('option', [1, None, 'true', []])
def test_invalid_option_has_no_workflow_effect(tmp_path, option):
    workspace = root(tmp_path)
    with pytest.raises(WorkflowError, match='pause_option_invalid'):
        run_office_workflow([sheet_step()], workspace, pause_after_step=option)
    assert not workspace.exists()


@pytest.mark.parametrize('failure', ['unknown', 'bad_receipt'])
def test_unknown_or_failed_readback_is_never_reported_as_pause(tmp_path, failure):
    workspace = root(tmp_path)
    def execute(spec, step_root, stop):
        if failure == 'unknown':
            raise TimeoutError('unknown Office effect')
        result = fake_execute([])(spec, step_root, stop)
        result['file_verified'] = False
        return result
    result = run_office_workflow([sheet_step(), report_step()], workspace, execute=execute, pause_after_step=True)
    assert result['state'] == 'unknown' and result['completed'] == []
    assert not (workspace / 's1' / 'receipt.json').exists() and not (workspace / 's2').exists()


def test_cancel_before_first_step_does_not_become_boundary_pause(tmp_path):
    result = run_office_workflow([sheet_step(), report_step()], root(tmp_path),
        execute=lambda *_: pytest.fail('dispatched'), pause_after_step=True, cancel_requested=lambda: True)
    assert result['state'] == 'cancelled' and result['completed'] == []
    assert result.get('error') != 'office_paused_after_verified_step'
