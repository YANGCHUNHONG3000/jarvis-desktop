import io
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from agent_service import environment_check as check


def test_non_windows_is_not_ready(monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'linux')
    assert check.check_platform()['status'] == 'blocked'


@pytest.mark.parametrize('desktop,missing', [(False, 'tkinter'), (True, 'pythoncom')])
def test_dependency_import_failure_is_scoped_and_redacted(monkeypatch, desktop, missing):
    def load(name):
        if name == missing:
            raise ImportError('private-path-and-credential')
    monkeypatch.setattr(check.importlib, 'import_module', load)
    outcome = check.check_imports(desktop)
    assert outcome['status'] == 'blocked' and outcome['facts']['failed'] == [missing]
    assert 'private-path' not in json.dumps(outcome)


def fake_registry(monkeypatch, tmp_path, *, exists=True, clsid='{000209FF-0000-4b30-A977-D214852036FF}'):
    exe = tmp_path / 'WPS Office' / 'wps.exe'
    if exists:
        exe.parent.mkdir()
        exe.write_bytes(b'fixture only')
    class Key:
        def __init__(self, path): self.path = path
        def __enter__(self): return self
        def __exit__(self, *args): pass
    def open_key(hive, path, reserved=0, access=0):
        if not access & 512:
            raise FileNotFoundError('only 32-bit COM registration')
        return Key(path)
    def query(key, name):
        return (clsid if key.path.endswith('CLSID') else f'"{exe}" /automation', 1)
    monkeypatch.setitem(sys.modules, 'winreg', SimpleNamespace(HKEY_CLASSES_ROOT=1, KEY_READ=1,
        KEY_WOW64_64KEY=256, KEY_WOW64_32KEY=512, REG_SZ=1, REG_EXPAND_SZ=2,
        OpenKey=open_key, QueryValueEx=query))
    monkeypatch.setattr(sys, 'platform', 'win32')


def test_64bit_python_can_find_32bit_out_of_process_wps(monkeypatch, tmp_path):
    fake_registry(monkeypatch, tmp_path)
    outcome = check.check_office_registration()
    assert outcome['status'] == 'ok'
    assert outcome['facts']['registry_views'] == {'KWPS.Application':['32'], 'KET.Application':['32']}
    assert str(tmp_path) not in json.dumps(outcome)


@pytest.mark.parametrize('exists,clsid', [(False, '{000209FF-0000-4b30-A977-D214852036FF}'),
                                        (True, 'untrusted-invalid-registry-value')])
def test_invalid_or_stale_office_registration_not_ok(monkeypatch, tmp_path, exists, clsid):
    fake_registry(monkeypatch, tmp_path, exists=exists, clsid=clsid)
    assert check.check_office_registration()['status'] == 'blocked'


@pytest.mark.parametrize('code,status', [(None,'ok'),('office_ui_blocked','blocked'),
                                        ('office_ui_unavailable','unknown'),('private-error','unknown')])
def test_window_snapshot_does_not_claim_business_acceptance(monkeypatch, code, status):
    from agent_service import office_readiness
    monkeypatch.setattr(office_readiness, 'preflight_office_window', lambda:code)
    value = check.check_office_windows()
    assert value['status'] == status and '不代表Office已安装' in value['detail']
    assert 'private-error' not in json.dumps(value)


@pytest.mark.parametrize('authorized', [True, False])
def test_model_check_never_reads_key_or_initializes_ledger(monkeypatch, tmp_path, authorized):
    from agent_service import model
    key = tmp_path/'key.txt'; key.write_text('synthetic-sensitive-value')
    config = tmp_path/'permission.json'
    config.write_text(json.dumps(dict(authorized=authorized, daily_limit_cny='5.00',
        timezone='America/New_York', provider='openai', model=model.openai_profile.MODEL, key_path=str(key))))
    original = model.CloudClient
    monkeypatch.setattr(model, 'CloudClient', lambda **kw: original(permission_path=config, **kw))
    monkeypatch.setattr(model, 'configured_model_label', lambda:'configured GPT')
    def forbidden(*a, **kw): pytest.fail('credential read, ledger initialization or network forbidden')
    monkeypatch.setattr(model.BudgetLedger, '__init__', forbidden)
    monkeypatch.setattr(model.requests, 'Session', forbidden)
    original_read = Path.read_text
    def guarded_read(path, *a, **kw):
        if path == key: forbidden()
        return original_read(path, *a, **kw)
    monkeypatch.setattr(Path, 'read_text', guarded_read)
    value = check.check_model()
    assert value['status'] == ('ok' if authorized else 'blocked')
    assert 'synthetic-sensitive' not in json.dumps(value) and str(key) not in json.dumps(value)


def test_missing_budget_does_not_create_parent_or_database(tmp_path):
    path=tmp_path/'absent'/'original.sqlite3'
    assert check.check_budget(path)['status'] == 'blocked'
    assert not path.parent.exists()


def test_budget_uses_both_windows_keeps_unknown_hold_and_does_not_write(tmp_path):
    from agent_service.budget import BudgetLedger
    path=tmp_path/'original.sqlite3'; now=2_000_000_000
    ledger=BudgetLedger(path, clock=lambda:now-3600)
    held=ledger.reserve(2_000_000,'hold')
    paid=ledger.reserve(1_000_000,'paid'); ledger.settle(paid,500_000,{})
    before=path.read_bytes(); mtime=path.stat().st_mtime_ns
    value=check.check_budget(path, now=now)
    assert value['status']=='ok' and value['facts']['held_micro']==2_000_000
    assert value['facts']['rolling_charged_micro']==500_000
    assert value['facts']['remaining_micro']==2_500_000
    assert path.read_bytes()==before and path.stat().st_mtime_ns==mtime
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT charge_micro FROM reservations WHERE id=?',(held,)).fetchone()==(None,)


def test_budget_fault_blocks_even_with_balance(tmp_path):
    from agent_service.budget import BudgetLedger
    path=tmp_path/'original.sqlite3'; ledger=BudgetLedger(path,clock=lambda:2_000_000_000)
    token=ledger.reserve(100,'request'); ledger.settle_overrun(token,101,{})
    value=check.check_budget(path,now=2_000_000_000)
    assert value['status']=='blocked' and value['facts']['remaining_micro']==0


@pytest.mark.parametrize('faulted', [False, True])
def test_unlimited_budget_diagnostic_is_read_only_and_preserves_faults(monkeypatch, tmp_path, faulted):
    from agent_service import budget, model_policy
    path = tmp_path/'original.sqlite3'
    ledger = budget.BudgetLedger(path, clock=lambda:2_000_000_000)
    ledger.reserve(2_364_618, 'prior-unknown')
    if faulted:
        token = ledger.reserve(100, 'request')
        ledger.settle_overrun(token, 101, {})
    permission = tmp_path/'permission.json'
    permission.write_text(json.dumps(dict(authorized=True, timezone='America/New_York',
        budget_mode='unlimited', daily_limit_cny=None, rolling_limit_cny=None)))
    monkeypatch.setattr(budget, 'DEFAULT_LEDGER', path)
    monkeypatch.setattr(model_policy, 'PERMISSION', permission)
    before, mtime = path.read_bytes(), path.stat().st_mtime_ns
    value = check.check_budget(path, now=2_000_000_000)
    assert value['status'] == ('blocked' if faulted else 'ok')
    assert value['facts']['limit_micro'] is None and value['facts']['remaining_micro'] is None
    assert value['facts']['faulted'] is faulted and value['facts']['held_micro'] >= 2_364_618
    assert path.read_bytes() == before and path.stat().st_mtime_ns == mtime


def test_unlimited_budget_diagnostic_rejects_missing_authorization(monkeypatch, tmp_path):
    from agent_service import budget, model_policy
    path = tmp_path/'original.sqlite3'; budget.BudgetLedger(path)
    permission = tmp_path/'permission.json'
    permission.write_text(json.dumps(dict(budget_mode='unlimited')))
    monkeypatch.setattr(budget, 'DEFAULT_LEDGER', path)
    monkeypatch.setattr(model_policy, 'PERMISSION', permission)
    before = path.read_bytes()
    assert check.check_budget(path)['status'] == 'blocked'
    assert path.read_bytes() == before


@pytest.mark.parametrize('remaining,pending,status', [(1,False,'ok'),(0,False,'blocked'),(0,True,'blocked')])
def test_edit_gate_is_current_and_never_consumed(monkeypatch, remaining, pending, status):
    from agent_service import office_edit_adapter as adapter
    monkeypatch.setattr(adapter,'current_candidate',lambda:'a'*64)
    observed=[]
    def read(store,candidate):
        observed.append(candidate)
        return dict(offline=True,reviewed=True,trial_passed=True,remaining=remaining,pending=pending)
    monkeypatch.setattr(adapter,'status',read)
    monkeypatch.setattr(adapter,'execute',lambda *a,**kw:pytest.fail('must not execute'))
    monkeypatch.setattr(adapter,'activate',lambda *a,**kw:pytest.fail('must not activate'))
    assert check.check_adapter()['status']==status and observed==['a'*64]


def test_probe_exception_never_leaks_message(monkeypatch):
    def bad(): raise ValueError('private-secret-and-path')
    monkeypatch.setattr(check,'check_model',bad)
    value=check.probe('model_configuration')
    assert value['status']=='unknown' and 'private-secret' not in json.dumps(value)


@pytest.mark.parametrize('failure,status', [(TimeoutError('sensitive'),'unknown'),
                                          (RuntimeError('sensitive'),'unknown'),(None,'ok')])
def test_probes_use_owned_child_with_exact_interpreter(monkeypatch, failure, status):
    from agent_service import process
    seen=[]
    def child(command,should_stop,timeout):
        seen.append(command)
        assert timeout==35 and not should_stop()
        if failure: raise failure
        return json.dumps(check.result('ok','bounded'))
    monkeypatch.setattr(process,'run_child',child)
    value=check.run_probe('browser_runtime',executable='chosen-python')
    assert seen==[['chosen-python','-B',str(check.ROOT/'run_jarvis_check.py'),'--_probe','browser_runtime']]
    assert value['status']==status and 'sensitive' not in json.dumps(value)


def test_child_without_owner_handshake_does_not_start_probe(monkeypatch):
    monkeypatch.setattr(sys,'stdin',SimpleNamespace(buffer=io.BytesIO(b'')))
    monkeypatch.setattr(check,'probe',lambda _:pytest.fail('probe started before ownership'))
    assert check.main(['--_probe','browser_runtime'])==2


@pytest.mark.parametrize('response', ['{"status":"ok"}', '{"status":"ok","detail":"x","next_step":"", "facts":[],"id":"injected"}', 'not json',
                                    '{"status":[],"detail":"x","next_step":"","facts":{}}',
                                    '{"status":{},"detail":"x","next_step":"","facts":{}}'])
def test_malformed_child_output_is_unknown(monkeypatch, response):
    from agent_service import process
    monkeypatch.setattr(process,'run_child',lambda *a,**kw:response)
    assert check.run_probe('platform')['status']=='unknown'


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows process-tree containment')
def test_probe_timeout_reaps_owned_descendant(tmp_path):
    import ctypes
    from ctypes import wintypes
    from agent_service.process import run_child
    marker=tmp_path/'owned-child-pid.txt'
    code=("import sys,subprocess,time; from pathlib import Path; "
          "assert sys.stdin.buffer.read(1)==b'1'; "
          "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],creationflags=subprocess.CREATE_NO_WINDOW); "
          f"Path({str(marker)!r}).write_text(str(p.pid)); time.sleep(30)")
    with pytest.raises(TimeoutError):
        run_child([sys.executable,'-B','-c',code],should_stop=lambda:False,timeout=2)
    pid=int(marker.read_text())
    api=ctypes.WinDLL('kernel32',use_last_error=True)
    api.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
    api.OpenProcess.restype=wintypes.HANDLE
    api.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
    api.CloseHandle.argtypes=[wintypes.HANDLE]
    handle=api.OpenProcess(0x00100000,False,pid)
    if handle:
        try: assert api.WaitForSingleObject(handle,5000)==0
        finally: api.CloseHandle(handle)


def test_collect_does_not_start_browser_by_default_or_hide_missing_desktop(monkeypatch):
    import run_jarvis_desktop
    monkeypatch.setattr(run_jarvis_desktop,'desktop_python',lambda: (_ for _ in ()).throw(RuntimeError()))
    seen=[]
    def run(name,**kw):
        seen.append(name)
        return dict(id=name,**check.result('ok','local'))
    monkeypatch.setattr(check,'run_probe',run)
    value=check.collect()
    assert 'browser_runtime' not in seen and not value['business_acceptance'] and not value['cloud_request_sent']
    assert value['blocked']==['desktop_dependencies','office_registration']
