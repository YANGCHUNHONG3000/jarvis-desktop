"""Automatic defaults must not migrate approved tasks or overwrite drafts."""
from types import SimpleNamespace
import pytest
from agent_service.windows_panel import WindowsPanel
from agent_service.windows_stages_panel import WindowsStagesPanel
from agent_service.windows_access import AutoWindowsBackend, WindowsBackend, WindowsMcpBackend
from agent_service.windows_mcp import PROVIDER_BINDING


def owner(**changes):
    messages, deleted = [], []
    value = SimpleNamespace(running=False, external_busy=lambda: False,
        conditions=[], sources=[], task_root=None, proposal_task=None, saved_task=None,
        backend=None, observation={'old': True}, approved_observations=[{'old': True}],
        status=SimpleNamespace(set=messages.append),
        tree=SimpleNamespace(get_children=lambda: ['old-control'], delete=lambda *ids: deleted.extend(ids)))
    value.__dict__.update(changes)
    return value, messages, deleted


@pytest.mark.parametrize('binding', ['conditions', 'sources', 'task_root', 'proposal_task', 'saved_task'])
def test_switch_keeps_existing_approved_or_artifact_bound_work(binding):
    original = WindowsMcpBackend()
    panel, messages, deleted = owner(backend=original, **{binding: ['bound']})
    WindowsPanel.use_auto(panel)
    assert panel.backend is original and panel.observation == {'old': True}
    assert not deleted and '保留原执行方式' in messages[-1]


def test_new_draft_auto_switch_requires_fresh_observation():
    panel, _, deleted = owner()
    WindowsPanel.use_auto(panel)
    assert type(panel.backend) is AutoWindowsBackend
    assert panel.observation is None and panel.approved_observations == []
    assert deleted == ['old-control']


def test_stage_new_default_and_legacy_restore_are_distinct():
    panel = SimpleNamespace(state=None, backend=None)
    assert type(WindowsStagesPanel.get_backend(panel)) is AutoWindowsBackend
    panel.state = {'provider_binding': None}
    assert type(WindowsStagesPanel.get_backend(panel)) is WindowsBackend
    panel.state = {'provider_binding': PROVIDER_BINDING}
    assert type(WindowsStagesPanel.get_backend(panel)) is WindowsMcpBackend


def test_new_stage_respects_explicit_selected_backend():
    backend = WindowsMcpBackend()
    panel = SimpleNamespace(state=None, backend=backend)
    assert WindowsStagesPanel.get_backend(panel) is backend
