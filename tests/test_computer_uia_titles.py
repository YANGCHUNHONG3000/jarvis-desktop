"""Identity policy tests use fake Win32 calls; no live UI is touched."""
import ctypes
import sys
from types import SimpleNamespace

import pytest
from agent_service import computer_uia as uia


class FakeQuery:
    def __init__(self, image): self.image = image
    def __call__(self, handle, flags, buffer, length):
        buffer.value = self.image
        return 1


def setup_identity(monkeypatch, *, title='jarvis_stage_149', image='C:\\Windows\\explorer.exe', cls='CabinetWClass'):
    monkeypatch.setenv('SystemRoot', 'C:\\Windows')
    class Handle:
        def __int__(self): return 7
        def Close(self): pass
    monkeypatch.setitem(sys.modules, 'win32api', SimpleNamespace(OpenProcess=lambda *a: Handle()))
    monkeypatch.setitem(sys.modules, 'win32gui', SimpleNamespace(IsWindow=lambda h:True,
        IsWindowVisible=lambda h:True, GetWindowText=lambda h:title, GetClassName=lambda h:cls))
    monkeypatch.setitem(sys.modules, 'win32process', SimpleNamespace(GetWindowThreadProcessId=lambda h:(1,2),
        GetProcessTimes=lambda h:{'CreationTime':'start'}))
    monkeypatch.setattr(ctypes, 'windll', SimpleNamespace(dwmapi=SimpleNamespace(DwmGetWindowAttribute=lambda *a:1)))
    monkeypatch.setattr(ctypes, 'WinDLL', lambda *a,**kw: SimpleNamespace(QueryFullProcessImageNameW=FakeQuery(image)))


@pytest.mark.parametrize('brand', ['jarvis_stage_149','CODEX results','ChatGPT文件','贾维斯'])
def test_explorer_only_accepts_brand_title(monkeypatch, brand):
    setup_identity(monkeypatch, title=brand)
    assert uia._identity(10)['profile'] == 'explorer'
    setup_identity(monkeypatch, title=brand, cls='OtherClass')
    with pytest.raises(uia.UIAError, match='window_denied'): uia._identity(10)
    setup_identity(monkeypatch, title=brand, image='C:\\Untrusted\\explorer.exe')
    with pytest.raises(uia.UIAError, match='window_denied'): uia._identity(10)


@pytest.mark.parametrize('title', ['jarvis password','Codex 登录','ChatGPT credential','贾维斯 终端',
                                  'powershell', '', 'x'*301])
def test_sensitive_or_invalid_title_remains_denied(monkeypatch, title):
    setup_identity(monkeypatch, title=title)
    with pytest.raises(uia.UIAError, match='window_denied'): uia._identity(10)


@pytest.mark.parametrize('process', ['python.exe','pythonw.exe','codex.exe','chatgpt.exe','powershell.exe'])
def test_forbidden_process_never_gets_title_exception(monkeypatch, process):
    setup_identity(monkeypatch, title='jarvis', image='C:\\Windows\\'+process)
    with pytest.raises(uia.UIAError, match='window_denied'): uia._identity(10)
