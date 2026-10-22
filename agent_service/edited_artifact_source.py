"""Separate provenance route for preserved Office edits, never creation receipts."""
from pathlib import Path
import re

from .computer import digest, read_record, safe_root
from .computer_agent_tools import _load


def prepare_reference(artifact, expected_sha256, base):
    from .office_edit import verify_edit_result
    artifact, base = safe_root(artifact), safe_root(base)
    output, task = artifact.parent, artifact.parent.parent
    if (output.name != 'edit' or task.parent != base
            or not re.fullmatch(r'task-[0-9a-f]{32}',task.name)
            or artifact.name not in {'edited-document.docx','edited-spreadsheet.xlsx'}):
        raise ValueError('edited_artifact_owner_invalid')
    manifest, manifest_hash = _load(output/'edit-manifest.json')
    run, step = safe_root(manifest['run']), safe_root(manifest['step'])
    if step.parent != run or not re.fullmatch(r'step-\d{2}',step.name) or not re.fullmatch(r'[0-9a-f]{64}\.run',run.name):
        raise ValueError('edited_artifact_run_invalid')
    plan = read_record(run.with_suffix('.json'))
    if plan.get('sha256') != run.stem or digest({k:v for k,v in plan.items() if k!='sha256'}) != run.stem:
        raise ValueError('edited_artifact_plan_invalid')
    goal, _ = _load(run/'environment-goal.json')
    if goal != plan['goal'] or goal.get('kind') != 'interactive':
        raise ValueError('edited_artifact_goal_invalid')
    record, record_hash = _load(step/'environment-result.json')
    intent, intent_hash = _load(step/'environment-intent.json')
    if (record['intent_sha256'] != intent_hash or record['action'] != intent['action']
            or record['action']['kind'] != 'office_edit' or record['result'].get('ok') is not True):
        raise ValueError('edited_artifact_receipt_invalid')
    item = next((e for e in goal['edits'] if e['id']==record['action']['edit_id']),None)
    if item is None or item['source_root'] != manifest['source_root'] or item['spec'] != manifest['spec']:
        raise ValueError('edited_artifact_spec_invalid')
    native = verify_edit_result(record['native'],manifest['source_root'],manifest['spec'])
    if native['artifact'] != str(artifact) or native['sha256'] != expected_sha256:
        raise ValueError('edited_artifact_changed')
    return {'version':1,'kind':'office_edit','artifact':str(artifact),'sha256':expected_sha256,
            'manifest_sha256':manifest_hash,'receipt_sha256':record_hash}


def snapshot_reference(ref, task_root, base, *, stop=lambda:False, deadline=None):
    from . import office_source, task_history
    if type(ref) is not dict or set(ref) != {'version','kind','artifact','sha256','manifest_sha256','receipt_sha256'}:
        raise ValueError('edited_artifact_ref_invalid')
    if prepare_reference(ref['artifact'],ref['sha256'],base) != ref:
        raise ValueError('edited_artifact_ref_changed')
    root = task_history._task(task_root,task_history._base(base))
    from .source_intake import snapshot_selected_source
    result = snapshot_selected_source(ref['artifact'],root,stop=stop,deadline=deadline)
    if result['sha256'] != ref['sha256'] or prepare_reference(ref['artifact'],ref['sha256'],base) != ref:
        raise ValueError('edited_artifact_changed')
    office_source._write_json_new(root/'sources'/'provenance.json',ref)
    return result
