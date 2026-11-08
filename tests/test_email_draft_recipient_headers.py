"""Independent MIME recipient verification; no mailbox or Office execution."""
from email.parser import BytesParser
from email.policy import SMTP
from pathlib import Path

import pytest

from agent_service import email_draft


def draft(tmp_path, to):
    step = tmp_path / 's1'
    step.mkdir()
    spec = {'kind': 'email_draft', 'subject': 'Draft', 'body': 'Please review.', 'to': to}
    result = email_draft.execute_draft(spec, step)
    assert result['ok']
    return Path(result['artifact']), {key: spec[key] for key in ('subject', 'body', 'to')}


@pytest.mark.parametrize('header', ['Cc', 'Bcc', 'Resent-To', 'Resent-Cc', 'Resent-Bcc'])
@pytest.mark.parametrize('address', ['', 'hidden@example.org'])
def test_additional_recipient_header_is_rejected_even_when_empty(tmp_path, header, address):
    path, expected = draft(tmp_path, [])
    message = BytesParser(policy=SMTP).parsebytes(path.read_bytes())
    message[header] = address
    path.write_bytes(message.as_bytes(policy=SMTP))
    with pytest.raises(email_draft.DraftError, match='draft_recipient_mismatch'):
        email_draft._verify_eml(path, expected, [])


@pytest.mark.parametrize('to', [[], ['reader@example.org'], ['one@example.org', 'two@example.org']])
def test_explicit_normal_to_verifies_without_extra_recipient_headers(tmp_path, to):
    path, expected = draft(tmp_path, to)
    assert email_draft._verify_eml(path, expected, [])['to'] == to


def test_existing_to_mismatch_rejection_is_preserved(tmp_path):
    path, expected = draft(tmp_path, ['reader@example.org'])
    message = BytesParser(policy=SMTP).parsebytes(path.read_bytes())
    message.replace_header('To', 'other@example.org')
    path.write_bytes(message.as_bytes(policy=SMTP))
    with pytest.raises(email_draft.DraftError, match='draft_recipient_mismatch'):
        email_draft._verify_eml(path, expected, [])
