"""Bounded, local prerequisite diagnostics; never a task or permission grant."""
import importlib
import json
import os
from pathlib import Path
import platform
import re
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
PROBES = ('platform', 'main_dependencies', 'desktop_dependencies', 'office_registration',
          'office_windows', 'model_configuration', 'budget', 'edit_adapter')
STATUSES = frozenset(('ok', 'blocked', 'unknown'))


def result(status, detail, next_step='', **facts):
    return dict(status=status, detail=detail, next_step=next_step, facts=facts)


def check_platform():
    supported = sys.platform == 'win32' and sys.version_info >= (3, 12)
    return result('ok' if supported else 'blocked',
                  '桌面能力要求 Windows 和 Python 3.12 或以上。',
                  '' if supported else '使用 Windows 上的 Python 3.12+ 启动桌面入口。',
                  system=platform.system(), release=platform.release(), python=platform.python_version(),
                  bits=64 if sys.maxsize > 2**32 else 32)


def check_imports(desktop=False):
    names = ('pywinauto', 'comtypes', 'pythoncom', 'win32com.client') if desktop else (
        'tkinter', 'requests', 'bs4', 'playwright.sync_api', 'openpyxl')
    missing = []
    for name in names:
        try:
            importlib.import_module(name)
        except Exception:
            missing.append(name)
    return result('blocked' if missing else 'ok', '模块导入检查；不启动应用或运行任务。',
                  '按 requirements-desktop.txt 检查对应解释器依赖。' if missing else '',
                  modules=list(names), failed=missing)


def check_office_registration():
    if sys.platform != 'win32':
        return result('blocked', '当前生产Office执行器需要 Windows WPS COM。')
    import winreg
    registered = {}
    views = {}
    for name in ('KWPS.Application', 'KET.Application'):
        views[name] = []
        # Out-of-process COM permits a 64-bit Python to use a 32-bit WPS server.
        for label, view in (('64', winreg.KEY_WOW64_64KEY), ('32', winreg.KEY_WOW64_32KEY)):
            try:
                with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, name + r'\CLSID', 0, winreg.KEY_READ | view) as key:
                    clsid, kind = winreg.QueryValueEx(key, '')
                if kind != winreg.REG_SZ or not isinstance(clsid, str) or not re.fullmatch(
                        r'\{[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\}', clsid):
                    continue
                with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, 'CLSID\\' + clsid + r'\LocalServer32',
                                    0, winreg.KEY_READ | view) as key:
                    command, kind = winreg.QueryValueEx(key, '')
                if kind not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) or not isinstance(command, str):
                    continue
                match = re.match(r'^(?:"([^"]+\.exe)"|(.+?\.exe))(?:\s|$)', os.path.expandvars(command.strip()), re.I)
                path = Path(match[1] or match[2]) if match else None
                if path is not None and path.is_absolute() and not str(path).startswith(('\\\\', '//')):
                    resolved = path.resolve()
                    if not str(resolved).startswith(('\\\\', '//')) and resolved.is_file():
                        views[name].append(label)
            except (OSError, ValueError, RuntimeError):
                continue
        registered[name] = bool(views[name])
    ok = all(registered.values())
    return result('ok' if ok else 'blocked',
                  '仅检查WPS的32/64位注册及程序存在；不证明COM启动、文件编辑或公式重算成功。',
                  '' if ok else '检查WPS安装及COM注册；只装Microsoft Office不满足当前执行器。',
                  registered=registered, registry_views=views)


def check_office_windows():
    from .office_readiness import preflight_office_window
    code = preflight_office_window()
    status = 'ok' if code is None else 'blocked' if code == 'office_ui_blocked' else 'unknown'
    return result(status, '瞬时Office窗口阻塞观察；没有窗口也不代表Office已安装或可执行。',
                  '' if status == 'ok' else '检查Office弹窗、未响应窗口；不要自动关闭用户文档。',
                  code=code if code in (None, 'office_ui_blocked') else 'office_ui_unavailable')


def check_model():
    from .model import CloudClient, configured_model_label
    # Inject a non-ledger sentinel: construction must not initialize SQLite.
    client = CloudClient(ledger=object())
    try:
        profile, key_path = client._profile(client._permission())
        available = key_path.is_file() and key_path.stat().st_size > 0
    except (OSError, ValueError):
        return result('blocked', '模型本机授权、供应商或凭据文件配置不可用。',
                      '在本机检查已授权模型配置；不要把密钥发到聊天或日志。')
    return result('ok' if available else 'blocked',
                  '仅校验本机配置和凭据文件存在；未读取密钥、验证云端认证、价格或网络。',
                  '' if available else '在本机恢复已授权凭据文件；不要发送密钥。',
                  model=configured_model_label(), provider=profile['provider'], credential_file_present=available)


def check_budget(path=None, now=None):
    from datetime import datetime
    from .budget import BudgetLedger, BudgetError, DEFAULT_LEDGER, _DAY_ZONE, effective_limit
    path = Path(DEFAULT_LEDGER if path is None else path)
    now = time.time() if now is None else now
    if not path.is_file():
        return result('blocked', '原预算账本不存在；诊断不会创建替代账本。', '恢复原共享账本后再检查。')
    try:
        limit = effective_limit(path)
    except BudgetError:
        return result('blocked', '本机模型费用授权配置不可用。', '检查本地授权配置；不创建替代账本。')
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        day = datetime.fromtimestamp(now, _DAY_ZONE).date().isoformat()
        held, daily, rolling = BudgetLedger._totals(db, now, day)
        faulted = db.execute('SELECT 1 FROM budget_faults LIMIT 1').fetchone() is not None
    if any(type(value) is not int or value < 0 for value in (held, daily, rolling)):
        return result('blocked', '预算汇总数据无效。', '检查原账本；不可清空预留或换账本。')
    if limit is None:
        return result('blocked' if faulted else 'ok',
                      '本地金额上限已取消；原账本仍只读核对费用，不代表云端账号余额或配额。',
                      '记账故障需核验；原记录和未知费用预留保留。' if faulted else '',
                      limit_micro=None, charged_micro=daily, rolling_charged_micro=rolling,
                      held_micro=held, remaining_micro=None, faulted=faulted)
    remaining = 0 if faulted else max(0, min(limit-held-daily, limit-held-rolling))
    return result('ok' if remaining else 'blocked',
                  '原账本只读汇总；有余额不代表足以覆盖下一次请求预留。',
                  '' if remaining else '等待额度窗口恢复；故障需核验，未知费用预留不得解除。',
                  limit_micro=limit, charged_micro=daily, rolling_charged_micro=rolling,
                  held_micro=held, remaining_micro=remaining, faulted=faulted)


def check_adapter():
    from . import office_edit_adapter as adapter
    candidate = adapter.current_candidate()
    try:
        state = adapter.status(adapter.DEFAULT_STORE, candidate)
    except (OSError, ValueError):
        return result('blocked', '当前编辑实现缺少可核验资格；旧版本资格不自动继承。',
                      '按离线检查、独立审查、实机试运行流程验收当前候选。', candidate=candidate)
    available = state['offline'] and state['reviewed'] and state['trial_passed'] and state['remaining'] > 0
    return result('ok' if available else 'blocked', '当前候选资格只读核验；不消耗或新增执行名额。',
                  '' if available else '检查候选资格、未完成记录或剩余限用次数；不要手改门禁。', **state)


def check_browser():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel='msedge', headless=True, timeout=12000, chromium_sandbox=True)
        try:
            context = browser.new_context(service_workers='block', accept_downloads=False)
            context.route('**/*', lambda route: route.abort())
            page = context.new_page()
            passed = page.evaluate('() => 6 * 7') == 42 and page.url == 'about:blank'
        finally:
            browser.close()
    return result('ok' if passed else 'blocked',
                  '独立Edge空白页启动及JavaScript检查；未使用用户登录资料或访问真实网站。',
                  '' if passed else '检查本机Edge和Playwright兼容性。')


def probe(name):
    checks = dict(platform=check_platform, main_dependencies=check_imports,
                  desktop_dependencies=lambda: check_imports(True), office_registration=check_office_registration,
                  office_windows=check_office_windows, model_configuration=check_model, budget=check_budget,
                  edit_adapter=check_adapter, browser_runtime=check_browser)
    try:
        return checks[name]()
    except Exception:
        # Never expose raw exception text, credentials, local paths or provider messages.
        return result('unknown', '本项检查未完成；未把异常当作通过。', '检查本机依赖或配置后重新运行本项诊断。')


def run_probe(name, *, executable=None, timeout=35):
    from .process import run_child
    executable = sys.executable if executable is None else executable
    try:
        output = run_child([str(executable), '-B', str(ROOT/'run_jarvis_check.py'), '--_probe', name],
                           should_stop=lambda: False, timeout=timeout)
        value = json.loads(output) if len(output) < 32000 else None
        if (not isinstance(value, dict) or set(value) != {'status', 'detail', 'next_step', 'facts'}
                or not isinstance(value.get('status'), str) or value['status'] not in STATUSES
                or not isinstance(value['facts'], dict)
                or any(not isinstance(value[k], str) or len(value[k]) > 2000 for k in ('detail', 'next_step'))):
            raise ValueError('invalid_probe_output')
        return dict(id=name, **value)
    except TimeoutError:
        return dict(id=name, **result('unknown', '本项检查超时；没有执行任务。', '检查本机进程状态后重试。'))
    except (OSError, ValueError, RuntimeError):
        return dict(id=name, **result('unknown', '诊断进程不可用或结果无效。', '检查实际生产解释器的依赖。'))


def collect(*, browser=False):
    checks = []
    desktop = None
    try:
        from run_jarvis_desktop import desktop_python
        desktop = desktop_python()
    except Exception:
        pass
    for name in PROBES:
        if name in ('desktop_dependencies', 'office_registration') and desktop is None:
            checks.append(dict(id=name, **result('blocked', '未找到生产桌面解释器。',
                                                '检查桌面依赖或JARVIS_DESKTOP_PYTHON本机设置。')))
        else:
            checks.append(run_probe(name, executable=desktop if name in (
                'desktop_dependencies', 'office_registration') else None))
    if browser:
        checks.append(run_probe('browser_runtime'))
    return dict(schema=1, scope='local_prerequisites_only', checked_at_unix=time.time(),
                business_acceptance=False, cloud_request_sent=False, checks=checks,
                blocked=[c['id'] for c in checks if c['status']=='blocked'],
                unknown=[c['id'] for c in checks if c['status']=='unknown'])


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description='Jarvis本机环境诊断；不运行任务、不调用云模型。')
    parser.add_argument('--json', action='store_true', help='输出脱敏JSON报告')
    parser.add_argument('--probe-browser', action='store_true', help='启动独立Edge空白页检查，不访问网站')
    parser.add_argument('--_probe', choices=(*PROBES, 'browser_runtime'), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args._probe:
        # All child probes wait until the parent has attached its process-tree owner.
        if sys.stdin.buffer.read(1) != b'1':
            return 2
        sys.stdout.reconfigure(encoding='utf-8')
        print(json.dumps(probe(args._probe), ensure_ascii=False))
        return 0
    report = collect(browser=args.probe_browser)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print('Jarvis本机环境检查（前置条件，不等于业务验收）')
        for row in report['checks']:
            print(f"[{row['status']}] {row['id']}: {row['detail']}")
            if row['next_step']:
                print('  下一步：'+row['next_step'])
            if row['facts']:
                print('  '+json.dumps(row['facts'], ensure_ascii=False))
        print('未发送模型请求、启用调度或改动技能名额。')
    return 1 if report['blocked'] else 2 if report['unknown'] else 0
