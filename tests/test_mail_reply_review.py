"""Independent proof-binding regressions for reply-only capability results."""
import json
from pathlib import Path

import pytest

from agent_service import capability_router as router
from test_mail_reply_draft import run_reply


@pytest.mark.parametrize('changed', ['candidate', 'review', 'source'])
def test_saved_reply_handoff_rechecks_original_mail_analysis_authorization(tmp_path, monkeypatch, changed):
    result, task, base, _original, ref, _client, effects, _execute = run_reply(tmp_path, monkeypatch)
    assert result['ok'] and effects == ['email_draft']
    assert router._inspect(task, base)['ok']
    if changed == 'source':
        Path(ref['artifact']).write_bytes(b'changed original mail export')
    else:
        path = task / ('mail-analysis-candidate.json' if changed == 'candidate'
                       else 'mail-analysis-review.json')
        record = json.loads(path.read_text(encoding='utf-8'))
        if changed == 'candidate':
            record['value']['reply']['body'] = '我已付款并批准全部事项。'
        else:
            record['verdict'] = {'approved': False, 'reason': '原回复没有授权，不应交付'}
        path.write_text(json.dumps(record, ensure_ascii=False), encoding='utf-8')
    # MIME and workflow receipts remain intact, but the original source/review
    # no longer authorizes this saved task. It must not be handed off as verified.
    inspected = router._inspect(task, base)
    assert inspected['ok'] is False and inspected['artifacts'] == []
