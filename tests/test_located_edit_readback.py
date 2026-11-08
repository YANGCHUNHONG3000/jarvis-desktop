"""Failure reassessment routing without editing or native application effects."""
from copy import deepcopy
from pathlib import Path
import json

import pytest

from agent_service import located_edit_workflow as flow, office_edit_recheck as recheck
from agent_service import edited_artifact_source as source
from agent_service.computer import ComputerError, read_record, digest
from agent_service.computer_agent_tools import _load
from test_office_edit_recheck import short_root, failed, supervised_readback
from test_office_edit_recheck import failed as make_failed


def scope(failed):
    artifact,sha,base,session,plan=failed
    outcome=read_record(session.root/'goals'/(plan['sha256']+'.run')/'outcome.json')
    return flow._readback_scope(session,plan,outcome)


def test_exact_final_failed_copy_can_be_reviewed_without_dispatch(failed):
    artifact,sha,*_=failed
    value=scope(failed)
    assert value['failed']=={'path':str(artifact),'sha256':sha}
    assert value['records']==[value['failed']]
    assert not (artifact.parent/'native-rechecks').exists()


def test_foreign_valid_failed_copy_cannot_replace_current_failed_step(failed,short_root):
    foreign=short_root/'foreign'; foreign.mkdir()
    other=make_failed.__wrapped__(foreign)
    artifact,sha,base,session,plan=failed
    manifest,_=_load(artifact.parent/'edit-manifest.json')
    result_path=Path(manifest['step'])/'environment-result.json'
    record,_=_load(result_path)
    record['native']['artifact']=str(other[0]); record['native']['sha256']=other[1]
    result_path.write_text(json.dumps({'data':record,'sha256':digest(record)}),encoding='utf8')
    with pytest.raises(ComputerError,match='readback_source_changed'):
        scope(failed)


def test_unstarted_file_cannot_be_claimed_complete(failed):
    *_,session,plan=failed
    value=deepcopy(plan)
    value['goal']['edits'].append(deepcopy(value['goal']['edits'][0]))
    outcome=read_record(session.root/'goals'/(plan['sha256']+'.run')/'outcome.json')
    with pytest.raises(ComputerError,match='readback_not_eligible'):
        flow._readback_scope(session,value,outcome)


def test_rechecked_copy_uses_composite_provenance_without_rewriting_old_failure(failed,monkeypatch):
    artifact,sha,base,session,plan=failed
    with pytest.raises(ValueError,match='receipt_invalid'):
        source.prepare_reference(artifact,sha,base)
    old=(session.root/'goals'/(plan['sha256']+'.run')/'outcome.json').read_bytes()
    preview=recheck.inspect(artifact,sha,base)
    calls=supervised_readback(monkeypatch)
    result=recheck.execute(artifact,sha,base=base,approval=preview['sha256'])
    assert result['ok'] and len(calls)==1
    ref=source.prepare_reference(artifact,sha,base)
    assert ref['kind']=='office_edit' and ref['sha256']==sha
    manifest,_=_load(artifact.parent/'edit-manifest.json')
    _,old_hash=_load(Path(manifest['step'])/'environment-result.json')
    assert ref['receipt_sha256']!=old_hash
    assert (session.root/'goals'/(plan['sha256']+'.run')/'outcome.json').read_bytes()==old
