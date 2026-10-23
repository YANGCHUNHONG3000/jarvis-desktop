"""Worker termination is separate evidence from a saved edit receipt."""
import json
from pathlib import Path
import subprocess
import sys

from agent_service import office_edit_host as host
from agent_service.external_document_source_host import _child_environment


def test_edit_child_exits_with_parent_pipe_open(tmp_path):
    # No Office automation: controlled result exercises the real worker lifetime.
    (tmp_path / 'worker-request.json').write_text(json.dumps({'spec': {}, 'source_root': str(tmp_path)}))
    script = ("import sys; from agent_service import office_edit as e,office_edit_host as h; "
              "e.execute_edit=lambda *a: {'ok':True,'controlled':True}; "
              "raise SystemExit(h.worker(sys.argv[1],10))")
    process = subprocess.Popen([sys.executable, '-B', '-c', script, str(tmp_path)],
                               cwd=host.ROOT, env=_child_environment(), stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        process.stdin.write(b'1');process.stdin.flush()
        process.wait(timeout=15)
        assert process.returncode == 0, process.stderr.read().decode('utf-8','replace')
        assert json.loads((tmp_path/'worker-outcome.json').read_text()) == {'ok': True, 'controlled': True}
    finally:
        if process.poll() is None:
            process.terminate();process.wait(timeout=5)
        for pipe in (process.stdin,process.stdout,process.stderr):pipe.close()


def test_nonzero_exit_cannot_pass_saved_success(tmp_path, monkeypatch):
    class Pipe:
        def write(self, value): pass
        def flush(self): pass
        def close(self): pass
    class Process:
        stdin=Pipe();stdout=Pipe();returncode=2
        def poll(self):return self.returncode
    monkeypatch.setattr(host, '_bind', lambda *a: ({},{}))
    monkeypatch.setattr(host, '_read', lambda p: {'ok':True})
    monkeypatch.setattr(host.subprocess, 'Popen', lambda *a,**k: Process())
    monkeypatch.setattr('run_jarvis_desktop.desktop_python',lambda:sys.executable)
    verified=[]
    monkeypatch.setattr(host,'verify_edit_result',lambda *a:verified.append(True))
    result=host.execute_edit_supervised({},tmp_path/'output',tmp_path)
    assert result == {'ok':False,'state':'unknown','code':'edit_worker_exited','effects':'unknown'}
    assert not verified
