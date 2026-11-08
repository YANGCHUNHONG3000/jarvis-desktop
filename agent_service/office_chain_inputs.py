"""Explicit input proofs owned by one task-chain load, never a shared cache."""
from copy import deepcopy

from . import workspace_state as workspace
from .verification_progress import checkpoint


class InputVerifications:
    def __init__(self):
        self._entries = []
        self._rechecked = False

    def verify(self, payload, base):
        # Validate the current draft and record shape even when the exact input
        # files were already checked earlier in this load. No subset inference.
        self._rechecked = False
        checkpoint()
        checked = workspace._payload(payload)
        root = str(workspace._store_root(base))
        identity = {key: checked[key] for key in ('records', 'source')}
        for entry in self._entries:
            if entry['base'] == root and entry['identity'] == identity:
                return {**deepcopy(entry['proof']), 'payload': checked}
        verified = workspace.verify(checked, root)
        proof = {key: deepcopy(verified[key]) for key in ('refs', 'source_ref')}
        self._entries.append({'base': root, 'identity': deepcopy(identity),
                              'payload': checked, 'proof': proof})
        return deepcopy(verified)

    def recheck(self):
        """Replay the complete production validator before publishing a load."""
        self._rechecked = False
        for entry in self._entries:
            checkpoint()
            current = workspace.verify(deepcopy(entry['payload']), entry['base'])
            checkpoint()
            if current != {**entry['proof'], 'payload': entry['payload']}:
                return False
        self._rechecked = True
        return True

    def completed_proof(self, payload, base):
        """Return only an exact input group's proof after the final full recheck."""
        checkpoint()
        if not self._rechecked:
            return None
        checked = workspace._payload(payload)
        root = str(workspace._store_root(base))
        identity = {key: checked[key] for key in ('records', 'source')}
        for entry in self._entries:
            if entry['base'] == root and entry['identity'] == identity:
                return {**deepcopy(entry['proof']), 'payload': checked}
        return None
