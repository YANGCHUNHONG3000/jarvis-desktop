"""Disposable, bounded UIA worker. Never kills or closes the target app."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from .computer import ComputerError, check_snapshot, comparable, prepare, read_record, same_identity, safe_root, write_new

ROOT = Path(__file__).resolve().parents[1]


def grant_foreground(pid):
    """Pass the approving GUI's foreground permission to this worker only."""
    import ctypes
    return bool(ctypes.windll.user32.AllowSetForegroundWindow(pid))


def perform_request(request, root, backend, check):
    check()
    op = request.get('op')
    if op == 'list' and set(request) == {'op'}:
        return {'ok': True, 'windows': backend.list_windows()}
    if op == 'observe' and set(request) == {'op', 'window'}:
        return {'ok': True, 'observation': backend.observe(request['window'])}
    if op != 'act' or set(request) not in ({'op', 'snapshot', 'plan'}, {'op', 'snapshot', 'plan', 'input_epoch'}):
        raise ComputerError('request_invalid')
    if 'input_epoch' in request and (type(request['input_epoch']) is not int or not 0 <= request['input_epoch'] <= 0xffffffff):
        raise ComputerError('input_epoch_invalid')
    before = check_snapshot(request['snapshot'])
    plan = request['plan']
    expected = prepare(request['snapshot'], plan['control_id'], plan['action'], plan['value'])
    if plan != expected or not expected['allowed']:
        raise ComputerError('action_not_allowed')
    original_check = check
    def check():
        original_check()
        if not 0 <= time.time() - request['snapshot']['at'] <= 120:
            raise ComputerError('snapshot_expired')
    current = backend.observe(before['window'])
    if not same_identity(before['window'], current['window']) or before['window']['title'] != current['window']['title'] or comparable(before) != comparable(current) or before.get('truncated') != current.get('truncated'):
        raise ComputerError('screen_changed_reobserve')
    control = next(c for c in current['controls'] if c['id'] == plan['control_id'])
    check()
    # Exclusive intent prevents replay even when this worker was interrupted.
    write_new(root / 'intent.json', {'plan': plan, 'at': time.time()})
    backend.perform(current['window'], control, plan['action'], plan['value'], check)
    check()
    after = backend.observe(current['window'])
    if not same_identity(current['window'], after['window']):
        raise ComputerError('window_changed_after_effect')
    matches = [c for c in after['controls'] if c['id'] == control['id']]
    actual = matches[0] if len(matches) == 1 else None
    action = plan['action']
    if plan['policy'] == 'notepad_new_tab':
        old_tabs = {c['id'] for c in current['controls'] if c['type'] == 'TabItem'}
        new_tabs = [c for c in after['controls'] if c['type'] == 'TabItem' and c['id'] not in old_tabs and c['selected'] is True]
        editors = [c for c in after['controls'] if c['type'] in {'Document', 'Edit'} and 'set_value' in c['patterns'] and c['visible']]
        if not current.get('truncated') and not after.get('truncated') and len(new_tabs) == 1 and len(editors) == 1 and editors[0]['value'] in {'', '\n', '\r\n'}:
            return {'ok': True, 'state': 'verified', 'effects': 'verified', 'observation': after}
        return {'ok': False, 'state': 'unknown', 'code': 'new_tab_not_verified', 'effects': 'unknown', 'observation': after}
    if action == 'invoke':
        return {'ok': True, 'state': 'executed_unverified', 'effects': 'observed', 'observation': after}
    verified = actual is not None and (
        (action == 'set_value' and isinstance(actual['value'], str) and actual['value'].replace('\r\n', '\n').replace('\r', '\n') == plan['value'].replace('\r\n', '\n').replace('\r', '\n'))
        or (action == 'focus' and actual['focused'])
        or (action == 'select' and actual['selected'] is True)
        or (action == 'expand' and actual['expanded'] == 1)
        or (action == 'collapse' and actual['expanded'] == 0))
    if not verified:
        return {'ok': False, 'state': 'unknown', 'code': 'postcondition_mismatch', 'effects': 'unknown', 'observation': after}
    return {'ok': True, 'state': 'verified', 'effects': 'verified', 'observation': after}


def run_supervised(request, root, *, stop=lambda: False, seconds=25, foreground_grant=None):
    from run_jarvis_desktop import desktop_python
    if type(seconds) not in (float, int) or not 0 < seconds <= 30:
        raise ComputerError('deadline_invalid')
    root = safe_root(root)
    root.mkdir(parents=True, exist_ok=False)
    write_new(root / 'request.json', request)
    if stop():
        return {'ok': False, 'state': 'cancelled', 'code': 'cancelled_before_start', 'effects': 'none'}
    options = {'cwd': ROOT, 'stdin': subprocess.PIPE, 'stdout': subprocess.DEVNULL,
               'stderr': subprocess.DEVNULL, 'env': dict(os.environ, PYTHONIOENCODING='utf-8')}
    if os.name == 'nt':
        options['creationflags'] = subprocess.CREATE_NO_WINDOW
    process = subprocess.Popen([desktop_python(), '-B', '-m', 'agent_service.computer_host',
                                str(root), '--seconds', str(seconds)], **options)
    try:
        if request.get('op') == 'act' and request.get('plan', {}).get('action') == 'focus' and 'input_epoch' in request:
            # A UI approval gives this parent last-input eligibility. New child
            # processes need an explicit grant after the first app switch.
            if stop() or not (foreground_grant or grant_foreground)(process.pid) or stop():
                result = {'ok': False, 'state': 'failed', 'effects': 'none',
                          'code': 'foreground_permission_denied'}
                write_new(root / 'supervisor-outcome.json', result)
                return result
        process.stdin.write(b'1')
        process.stdin.flush()
        deadline = time.monotonic() + seconds
        while process.poll() is None and not stop() and time.monotonic() < deadline:
            time.sleep(.05)
        if process.poll() is None:
            (root / 'cancel.flag').touch(exist_ok=True)
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=3)
        if (root / 'outcome.json').is_file():
            try:
                outcome = read_record(root / 'outcome.json')
                if type(outcome) is dict and type(outcome.get('ok')) is bool:
                    return outcome
            except (OSError, ValueError, TypeError):
                pass  # A torn receipt never overrides the durable intent.
        effect = (root / 'intent.json').exists()
        result = {'ok': False, 'state': 'unknown' if effect else 'failed',
                  'effects': 'unknown' if effect else 'none', 'code': 'worker_interrupted'}
        write_new(root / 'supervisor-outcome.json', result)
        return result
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
        process.stdin.close()


def worker(root, seconds):
    root = safe_root(root)
    if sys.stdin.buffer.read(1) != b'1':
        return 2
    cancelled = threading.Event()
    finished = threading.Event()
    deadline = time.monotonic() + seconds
    request = read_record(root / 'request.json')
    input_epoch = None
    if request.get('op') == 'act':
        import win32api
        input_epoch = request.get('input_epoch', win32api.GetLastInputInfo())
    def parent_watch():
        sys.stdin.buffer.read(1)
        if not finished.is_set():
            cancelled.set()
    def watchdog():
        while not finished.wait(.1):
            if time.monotonic() > deadline + 2 or cancelled.is_set():
                os._exit(3)
    threading.Thread(target=parent_watch, daemon=True).start()
    threading.Thread(target=watchdog, daemon=True).start()
    def check():
        if cancelled.is_set() or (root / 'cancel.flag').exists() or time.monotonic() >= deadline:
            raise ComputerError('cancelled_or_deadline')
        if input_epoch is not None and win32api.GetLastInputInfo() != input_epoch:
            raise ComputerError('user_takeover')
    try:
        from . import computer_uia
        result = perform_request(request, root, computer_uia, check)
    except Exception as exc:
        from .computer_uia import UIAError
        code = str(exc) if isinstance(exc, (ComputerError, UIAError)) else 'native_operation_failed'
        effect = (root / 'intent.json').exists()
        result = {'ok': False, 'state': 'unknown' if effect else 'failed',
                  'effects': 'unknown' if effect else 'none', 'code': code}
    write_new(root / 'outcome.json', result)
    finished.set()
    # Avoid unbounded provider/COM teardown after durable receipt.
    os._exit(0 if result['ok'] else 1)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('root')
    parser.add_argument('--seconds', type=float, default=25)
    args = parser.parse_args()
    worker(args.root, args.seconds)
