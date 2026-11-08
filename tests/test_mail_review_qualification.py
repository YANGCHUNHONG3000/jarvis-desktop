"""Mail source-proof changes must invalidate both dependent skill approvals."""
from pathlib import Path

import pytest

from agent_service import local_skills, office_edit_adapter


@pytest.mark.parametrize('module', [local_skills, office_edit_adapter])
@pytest.mark.parametrize('source', ['mail_understanding.py', 'mail_review_resume.py'])
def test_mail_proof_change_changes_implementation_fingerprint(monkeypatch, module, source):
    before = module.implementation_sha256()
    real_read = Path.read_bytes
    target = Path(module.__file__).resolve().parent / source
    def changed(path):
        raw = real_read(path)
        return raw + b'\n# synthetic changed proof implementation\n' if path.resolve() == target else raw
    monkeypatch.setattr(Path, 'read_bytes', changed)
    assert module.implementation_sha256() != before
