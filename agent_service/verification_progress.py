"""Opt-in, context-local progress for read-only historical verification."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import time


_CURRENT = ContextVar('history_verification_progress', default=None)
_INTERVAL = 0.25


class _Cancelled(Exception):
    # Deliberately not ValueError: receipt validators often translate ValueError.
    pass


@dataclass
class _State:
    stop: object
    progress: object
    stage: str
    records: int = 0
    files: int = 0
    cancelled: bool = False
    last_report: float = float('-inf')

    def check(self, *, stage=None, records=0, files=0):
        if self.cancelled or self.stop():
            self.cancelled = True
            raise _Cancelled()
        self.records += records
        self.files += files
        force = stage is not None and stage != self.stage
        if stage is not None:
            self.stage = stage
        now = time.monotonic()
        if force or now - self.last_report >= _INTERVAL:
            self.last_report = now
            self.progress(f'{self.stage}；已读取记录 {self.records} 次、校验文件 {self.files} 次。')
            # A callback may coincide with a stop request. Do not proceed into
            # a new file/fallback after sending the progress message.
            if self.cancelled or self.stop():
                self.cancelled = True
                raise _Cancelled()


def checkpoint(*, stage=None, records=0, files=0):
    state = _CURRENT.get()
    if state is not None:
        state.check(stage=stage, records=records, files=files)


@contextmanager
def verification_scope(stop, progress, stage):
    """Cancel only this caller; restore context even on malformed evidence."""
    state = _State(stop, progress, stage)
    token = _CURRENT.set(state)
    try:
        state.check()
        yield
        state.check()
    except Exception:
        if state.cancelled:
            from .computer import ComputerError
            raise ComputerError('comparison_selection_cancelled') from None
        raise
    finally:
        _CURRENT.reset(token)
