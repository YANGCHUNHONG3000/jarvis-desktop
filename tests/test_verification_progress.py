"""Cooperative read-only cancellation; no network, model, GUI or Office."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
import json

import pytest

from agent_service import verification_progress as progress
from agent_service import comparison_seed as seed, comparison_history as history
from agent_service import comparison_crosssite as crosssite, computer
from agent_service.computer_agent_tools import _load


def test_scope_is_opt_in_and_restores_on_success_and_failure():
    progress.checkpoint()
    with progress.verification_scope(lambda: False, lambda _: None, '核验'):
        assert progress._CURRENT.get() is not None
    assert progress._CURRENT.get() is None
    with pytest.raises(RuntimeError, match='unchanged'):
        with progress.verification_scope(lambda: False, lambda _: None, '核验'):
            raise RuntimeError('unchanged')
    assert progress._CURRENT.get() is None


def test_progress_throttles_counts_and_stage_changes(monkeypatch):
    now, messages = [10.0], []
    monkeypatch.setattr(progress.time, 'monotonic', lambda: now[0])
    with progress.verification_scope(lambda: False, messages.append, '核验来源'):
        for _ in range(100):
            progress.checkpoint(records=1)
        assert len(messages) == 1
        now[0] += .25
        progress.checkpoint(files=1)
        assert '记录 100 次、校验文件 1 次' in messages[-1]
        progress.checkpoint(stage='准备目标')
        assert len(messages) == 3 and messages[-1].startswith('准备目标')
    assert len(messages) == 3


def test_cancel_after_progress_does_not_start_work():
    stopped = Event()
    with pytest.raises(computer.ComputerError, match='^comparison_selection_cancelled$'):
        with progress.verification_scope(stopped.is_set, lambda _: stopped.set(), '核验'):
            pytest.fail('stop request must precede work')
    assert progress._CURRENT.get() is None


def test_scope_isolated_between_threads_and_nested_callers():
    stopped = Event()
    with progress.verification_scope(stopped.is_set, lambda _: None, '外层'):
        outer = progress._CURRENT.get()
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(progress._CURRENT.get).result() is None
        with progress.verification_scope(lambda: False, lambda _: None, '内层'):
            assert progress._CURRENT.get() is not outer
        assert progress._CURRENT.get() is outer


def test_record_cancel_preserves_code_across_receipt_error_translation(tmp_path, monkeypatch):
    path, stopped = tmp_path / 'receipt.json', Event()
    path.write_text(json.dumps({'data': {}, 'sha256': computer.digest({})}), encoding='utf8')
    original = Path.read_text
    def read_then_stop(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if self == path:
            stopped.set()
        return result
    monkeypatch.setattr(Path, 'read_text', read_then_stop)
    with pytest.raises(computer.ComputerError, match='^comparison_selection_cancelled$'):
        with progress.verification_scope(stopped.is_set, lambda _: None, '核验'):
            _load(path)
    # The same reader still works normally outside the optional scope.
    assert _load(path)[0] == {}


def test_hash_cancel_between_blocks_preserves_original(tmp_path, monkeypatch):
    path, stopped, hashed_sizes = tmp_path / 'large.bin', Event(), []
    original_bytes = b'x' * (3 * 1024 * 1024)
    path.write_bytes(original_bytes)
    real_sha = seed.sha256
    class Hash:
        def __init__(self):
            self.inner = real_sha()
        def update(self, block):
            hashed_sizes.append(len(block))
            self.inner.update(block)
            stopped.set()
        def hexdigest(self):
            return self.inner.hexdigest()
    monkeypatch.setattr(seed, 'sha256', Hash)
    with pytest.raises(computer.ComputerError, match='comparison_selection_cancelled'):
        with progress.verification_scope(stopped.is_set, lambda _: None, '核验'):
            seed._hash(path)
    assert hashed_sizes == [1024 * 1024]
    assert path.read_bytes() == original_bytes


def test_inventory_stops_before_next_file(tmp_path, monkeypatch):
    for i in range(3):
        (tmp_path / f'{i}.txt').write_text('kept', encoding='utf8')
    stopped, calls = Event(), []
    original = seed._hash
    def hash_then_stop(path):
        result = original(path)
        calls.append(path)
        stopped.set()
        return result
    monkeypatch.setattr(seed, '_hash', hash_then_stop)
    with pytest.raises(computer.ComputerError, match='comparison_selection_cancelled'):
        with progress.verification_scope(stopped.is_set, lambda _: None, '核验'):
            seed._inventory(tmp_path)
    assert len(calls) == 1 and len(list(tmp_path.iterdir())) == 3


def test_cancel_does_not_enter_compact_fallback(monkeypatch):
    from agent_service import comparison_compact_seed as compact
    stopped = Event()
    def rejected(*args):
        stopped.set()
        raise computer.ComputerError('comparison_failed_seed_invalid')
    monkeypatch.setattr(seed, 'create_manifest', rejected)
    monkeypatch.setattr(compact, 'create_manifest', lambda *a: pytest.fail('cancel must not fallback'))
    with pytest.raises(computer.ComputerError, match='comparison_selection_cancelled'):
        history.select_source('old', stop=stopped.is_set)


def test_cancel_inside_dependency_never_returns_partial_selection(tmp_path, monkeypatch):
    stopped = Event()
    path = tmp_path / 'dependency.json'
    path.write_text('{}', encoding='utf8')
    def dependency(*args):
        stopped.set()
        computer.read_record(path)
        pytest.fail('must not build a source manifest')
    monkeypatch.setattr(crosssite, 'followup_manifest', dependency)
    monkeypatch.setattr(seed, 'load_seed', lambda *a: pytest.fail('must not publish sources'))
    with pytest.raises(computer.ComputerError, match='comparison_selection_cancelled'):
        history.select_source('old', stop=stopped.is_set)


def test_cancellation_latches_even_if_inner_code_translates_exception():
    stopped = Event()
    with pytest.raises(computer.ComputerError, match='comparison_selection_cancelled'):
        with progress.verification_scope(stopped.is_set, lambda _: None, '核验'):
            stopped.set()
            try:
                progress.checkpoint()
            except Exception:
                stopped.clear()
                raise ValueError('translated')
    assert progress._CURRENT.get() is None


def test_completed_progress_counts_real_record_and_hash(tmp_path):
    path, messages = tmp_path / 'record.json', []
    path.write_text('{}', encoding='utf8')
    with progress.verification_scope(lambda: False, messages.append, '核验'):
        assert computer.read_record(path) == {}
        assert seed._hash(path) == seed.sha256(path.read_bytes()).hexdigest()
        progress.checkpoint(stage='就绪')
    assert '记录 1 次、校验文件 1 次' in messages[-1]
    assert str(path) not in ''.join(messages)
