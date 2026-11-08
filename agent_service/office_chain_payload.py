"""One synchronous worker's final chain proof, never a persistent input cache."""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from threading import get_ident

from . import workspace_state as workspace
from .verification_progress import checkpoint


_CURRENT = ContextVar('office_chain_final_payload', default=None)


class _Handoff:
    def __init__(self):
        self.thread = get_ident()
        self.token = None
        self.entry = None
        self._consumed = False

    def _owns_context(self):
        if self.token is None or get_ident() != self.thread or _CURRENT.get() is not self:
            return False
        # A copied Context carries the same object but cannot reset this token.
        # Replace it in-place only in its originating synchronous Context.
        try:
            _CURRENT.reset(self.token)
        except (ValueError, RuntimeError):
            return False
        self.token = _CURRENT.set(self)
        return True

    def clear(self):
        if self._owns_context():
            self.entry = None

    def publish(self, state, base, proof):
        if not self._owns_context() or self._consumed:
            return
        self.entry = None
        checkpoint()
        if (type(proof) is not dict or set(proof) != {'payload', 'refs', 'source_ref'}
                or type(proof['refs']) is not list or proof['payload'] != state['payload']
                or len(proof['refs']) != len(state['payload']['records'])
                or any(type(ref) is not dict for ref in proof['refs'])
                or (proof['source_ref'] is not None and type(proof['source_ref']) is not dict)
                or (proof['source_ref'] is None) != (state['payload']['source'] is None)):
            return
        self.entry = (deepcopy(state), str(workspace._store_root(base)), deepcopy(proof))

    def take(self, state, base):
        checkpoint()
        if not self._owns_context() or self._consumed:
            return None
        self._consumed = True
        entry, self.entry = self.entry, None
        if entry is None:
            return None
        saved, root, proof = entry
        if (saved != state or root != str(workspace._store_root(base))
                or proof['payload'] != state['payload']):
            return None
        checkpoint()
        return deepcopy(proof)

    def close(self):
        if self._owns_context():
            self.entry = None
            _CURRENT.reset(self.token)
            self.token = None


@contextmanager
def capture():
    parent = _CURRENT.get()
    if parent is not None:
        parent.clear()
    handoff = _Handoff()
    handoff.token = _CURRENT.set(handoff)
    try:
        yield handoff
    finally:
        handoff.close()
        if parent is not None:
            parent.clear()


def invalidate():
    handoff = _CURRENT.get()
    if handoff is not None:
        handoff.clear()


def publish(state, base, proof):
    handoff = _CURRENT.get()
    if handoff is not None:
        handoff.publish(state, base, proof)


def collecting():
    handoff = _CURRENT.get()
    return handoff is not None and handoff._owns_context() and not handoff._consumed
