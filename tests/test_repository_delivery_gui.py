"""No-network delivery/GUI checks; native Office is covered by separate acceptance."""
from copy import deepcopy

import pytest

from agent_service.computer import ComputerError, ComputerSession, read_record
from agent_service import computer_goal_panel as ui, computer_interaction, computer_goals
from agent_service.interaction_panel import build_request
from test_computer_goal_panel import panel, hidden_root


CONFIG = {'mode':'github_repo', 'start_url':'https://github.com/example/project',
          'query':'project user:example', 'required_text':['project'], 'human_login':False}


def test_scope_option_bound_and_previewed(tmp_path):
    session = ComputerSession.create(tmp_path / 'sessions')
    plan = computer_interaction.prepare(session, 'Read repository', browser=CONFIG, repository_document=True)
    assert plan['goal']['repository_document'] is True
    assert '生成' in computer_interaction.describe(plan) and '下一任务' in ui.describe(plan)
    for value in ('yes', 1, None):
        with pytest.raises(ComputerError, match='repository_document_scope_invalid'):
            computer_interaction.prepare(session, 'Read', browser=CONFIG, repository_document=value)


def test_editor_github_defaults_to_report_and_allows_read_only():
    values = dict(url=CONFIG['start_url'],site_mode='GitHub公开仓库（免登录）',expected='',word='',excel='')
    assert build_request(values)['repository_document'] is True
    assert 'repository_document' not in build_request(dict(values,repository_document=False))


def test_no_report_flag_for_generic_browser():
    generic = {'kind':'interactive','browser':None,'edits':[], 'repository_document':True}
    with pytest.raises(ComputerError):
        computer_interaction.validate_goal(generic)


def test_automatic_followup_only_after_verified_approved_goal(tmp_path,monkeypatch):
    from agent_service import computer_agent, repository_delivery
    session = ComputerSession.create(tmp_path / 'sessions')
    plan = computer_interaction.prepare(session, 'Read', browser=CONFIG, repository_document=True)
    events=[]
    monkeypatch.setattr(computer_agent,'execute',lambda *a,**k:{'ok':False,'code':'browser_failed'})
    monkeypatch.setattr(repository_delivery,'deliver_repository_document',lambda *a,**k:events.append('called'))
    assert computer_goals.execute_goal(session,plan,approval=plan['sha256'])['code']=='browser_failed'
    assert not events
    monkeypatch.setattr(computer_agent,'execute',lambda *a,**k:{'ok':True})
    computer_goals.execute_goal(session,plan,approval=plan['sha256'])
    assert events==['called']
    events.clear()
    read_only=computer_interaction.prepare(session,'Read only',browser=CONFIG)
    computer_goals.execute_goal(session,read_only,approval=read_only['sha256'])
    assert not events


@pytest.mark.parametrize('ok', [True,False])
def test_report_stage_preserves_browser_outcome_and_discloses_failure(tmp_path,monkeypatch,ok):
    from agent_service import repository_delivery, repository_document
    report={'ok':ok,'state':'verified' if ok else 'unknown','code':'report_verified' if ok else 'native_unknown',
            'artifacts':[{'kind':'document','path':'local.docx','sha256':'a'*64}] if ok else []}
    monkeypatch.setattr(repository_document,'prepare_repository_document',lambda *a,**k:{'sha256':'a'*64})
    monkeypatch.setattr(repository_document,'execute_repository_document',lambda *a,**k:deepcopy(report))
    reading={'ok':True,'state':'verified','code':'objective_verified','artifacts':[], 'run':str(tmp_path)}
    original=deepcopy(reading)
    result=repository_delivery.deliver_repository_document(reading)
    assert reading==original and result['ok']==ok
    assert result['artifacts']==report['artifacts']
    assert read_record(tmp_path/'repository-delivery.json')['repository_report']==report
    assert (result['state']=='unknown') is (not ok)


def test_cancel_does_not_start_document(tmp_path,monkeypatch):
    from agent_service import repository_delivery, repository_document
    monkeypatch.setattr(repository_document,'prepare_repository_document',lambda *a,**k:pytest.fail('cancelled'))
    result=repository_delivery.deliver_repository_document({'ok':True,'artifacts':[],'run':str(tmp_path)}, stop=lambda:True)
    assert not result['ok'] and result['code']=='repository_document_cancelled'


def test_gui_recent_readings_select_without_file_dialog(panel,monkeypatch):
    from agent_service import repository_document
    p,_=panel
    records=[{'run':'owned.run','url':CONFIG['start_url'],'body_chars':123}]
    monkeypatch.setattr(repository_document,'list_repository_readings',lambda:records)
    monkeypatch.setattr(p,'start',lambda kind,op:p.messages.put((kind,op(None),None)))
    p.mode.set(ui.MODES[8]);p.mode_changed();p.poll()
    assert p.repository_readings==records and p.control.current()==0
    assert str(p.propose_button['state'])=='disabled'
    assert str(p.control['state'])=='readonly' and '不需要找文件' in str(p.text_label['text'])


def test_gui_repository_delivery_handoff_is_automatic(panel):
    p,_=panel
    calls=[]
    p.on_artifacts=lambda records:calls.append(records)
    records=[{'path':'local.docx','kind':'document','sha256':'a'*64}]
    p.messages.put(('repository_document',{'ok':True,'artifacts':records},None));p.poll()
    assert calls==[records] and '下一任务' in p.status.get()


def test_gui_report_failure_never_hands_off(panel):
    p,_=panel
    p.on_artifacts=lambda records:pytest.fail('unverified')
    p.messages.put(('repository_document',{'ok':False,'code':'native_unknown'},None));p.poll()
    assert 'native_unknown' in p.status.get() and '仓库读取记录仍保留' in p.preview_box.get('1.0','end')


def test_gui_handoff_failure_explains_same_reading_recovery(panel):
    p,_=panel
    def fail(records): raise ValueError('busy')
    p.on_artifacts=fail
    p.messages.put(('repository_document',{'ok':True,'artifacts':[{'path':'local.docx'}]},None));p.poll()
    assert '无需重新登录' in p.status.get() and 'Word 已保存' in p.status.get()
