"""Append-only, locally bound Office questions and user answer drafts."""
from copy import deepcopy
from pathlib import Path
import math
import time
import uuid

from . import office_workspace, workspace_state
from .computer import ROOT, digest
from .office_input_review import validate_instruction

DEFAULT_STORE = ROOT / 'data' / 'office_clarifications'


def _read(path):
    return workspace_state._read(Path(path), 200_000)[0]


def _write(store, value):
    root = workspace_state._store_root(store, create=True)
    value = {**deepcopy(value), 'created': time.time(), 'id': uuid.uuid4().hex}
    data = workspace_state._encode(value)
    if len(data) > 100_000:
        raise ValueError('clarification_record_too_large')
    workspace_state._write_new(root / (value['id'] + '.json'), data)
    return value


def _folder(session, origin):
    if type(session) is not str or type(origin) is not str:
        raise ValueError('clarification_origin_invalid')
    session = workspace_state._store_root(session)
    folder = workspace_state._store_root(origin)
    if folder.parent != session / 'office-planning' or not workspace_state._ID.fullmatch(folder.name):
        raise ValueError('clarification_origin_invalid')
    # Sessions may live in the production data directory or an explicit test root.
    metadata = _read(session / 'session.json')
    if metadata.get('schema') != 1:
        raise ValueError('clarification_session_invalid')
    return folder


def _origin(record):
    if type(record) is not dict or type(record.get('question')) is not str:
        raise ValueError('clarification_record_invalid')
    folder = _folder(record['session'], record['origin'])
    request, outcome = _read(folder / 'request.json'), _read(folder / 'outcome.json')
    response = _read(folder / 'response.json')
    if (set(request) != {'instruction', 'files', 'artifact_refs', 'scope'}
            or set(outcome) != {'plan', 'reason', 'office_result'}
            or digest(request) != record['request_sha256'] or digest(outcome) != record['outcome_sha256']
            or response != {'kind': 'clarify', 'reason': record['question']}
            or outcome.get('plan') is not None or outcome.get('reason') != record['question']
            or outcome.get('office_result') != {'kind': 'clarify', 'record': str(folder)}):
        raise ValueError('clarification_origin_changed')
    payload = office_workspace._payload(record['original'])
    if (request['instruction'] != payload['draft'].strip()
            or request['files'] != [r['path'] for r in payload['records']]
            or request['scope'] != payload['scope']):
        raise ValueError('clarification_input_mismatch')
    refs = request['artifact_refs']
    if (type(refs) is not list or len(refs) != len(payload['records'])
            or any(type(ref) is not dict or ref.get('artifact') != r['path'] or ref.get('sha256') != r['sha256']
                   for r, ref in zip(payload['records'], refs))):
        raise ValueError('clarification_source_proof_required')
    validate_instruction(payload['draft'])
    validate_instruction(record['question'])
    return payload


def capture(store, session, result, context):
    if type(result) is not dict:
        raise ValueError('clarification_required')
    meta = result.get('office_result') or {}
    if (type(meta) is not dict or set(meta) != {'kind', 'record'}
            or meta.get('kind') != 'clarify' or result.get('plan') is not None):
        raise ValueError('clarification_required')
    folder = _folder(str(session.root), meta['record'])
    request, outcome = _read(folder / 'request.json'), _read(folder / 'outcome.json')
    if outcome != {key: result.get(key) for key in ('plan', 'reason', 'office_result')}:
        raise ValueError('clarification_result_mismatch')
    record = {'schema': 1, 'kind': 'question', 'origin': str(folder), 'session': str(session.root),
              'question': result['reason'], 'original': deepcopy(context['payload']),
              'request_sha256': digest(request), 'outcome_sha256': digest(outcome)}
    _origin(record)
    return _write(store, record)


def subset(original, selected):
    original, selected = office_workspace._payload(original), office_workspace._payload(selected)
    if not original['scope'] or not selected['scope']:
        raise ValueError('clarification_scope_required')
    available = {r['path']: (r['sha256'], {digest(at) for at in r['locations']}) for r in original['scope']}
    for part in selected['scope']:
        if part['path'] not in available:
            raise ValueError('clarification_scope_widened')
        sha, locations = available[part['path']]
        if part['sha256'] != sha or not {digest(at) for at in part['locations']} <= locations:
            raise ValueError('clarification_scope_widened')
    return selected


def compose(question, current, answer):
    original = _origin(question)
    current = subset(original, current)
    if current['draft'] != original['draft']:
        raise ValueError('clarification_new_draft')
    if type(answer) is not str:
        raise ValueError('clarification_answer_invalid')
    answer = validate_instruction(answer)
    if len(answer) > 600:
        raise ValueError('clarification_answer_too_long')
    combined = validate_instruction(original['draft'].strip() + '；用户补充：' + answer)
    return {**current, 'draft': combined}


def save_answer(store, question, verified, answer):
    if type(question) is not dict:
        raise ValueError('clarification_record_invalid')
    parent = load(store, question.get('id'))
    if parent['kind'] != 'question' or parent != question:
        raise ValueError('clarification_parent_changed')
    _origin(question)
    payload = subset(question['original'], verified['payload'])
    expected = compose(question, {**payload, 'draft': question['original']['draft']}, answer)
    if expected != payload:
        raise ValueError('clarification_answer_mismatch')
    return _write(store, {k: deepcopy(v) for k, v in question.items() if k not in {'id', 'created', 'kind'}} | {
        'kind': 'answer', 'question_id': question['id'], 'answer': validate_instruction(answer), 'payload': payload})


def load(store, record_id):
    if type(record_id) is not str or not workspace_state._ID.fullmatch(record_id):
        raise ValueError('clarification_id_invalid')
    root = workspace_state._store_root(store)
    record = _read(root / (record_id + '.json'))
    keys = {'schema', 'kind', 'origin', 'session', 'question', 'original',
            'request_sha256', 'outcome_sha256', 'created', 'id'}
    if record.get('kind') == 'answer':
        keys |= {'question_id', 'answer', 'payload'}
    if (set(record) != keys or type(record.get('schema')) is not int or record['schema'] != 1
            or record.get('id') != record_id or record.get('kind') not in {'question', 'answer'}
            or type(record.get('created')) not in (int, float) or not math.isfinite(record['created'])
            or any(type(record.get(key)) is not str or not workspace_state._SHA.fullmatch(record[key])
                   for key in ('request_sha256', 'outcome_sha256'))):
        raise ValueError('clarification_record_invalid')
    original = _origin(record)
    if record['kind'] == 'answer':
        ident = record['question_id']
        if type(ident) is not str or not workspace_state._ID.fullmatch(ident):
            raise ValueError('clarification_id_invalid')
        parent = _read(root / (ident + '.json'))
        if parent.get('kind') != 'question' or any(record.get(k) != parent.get(k) for k in (
                'origin', 'session', 'question', 'original', 'request_sha256', 'outcome_sha256')):
            raise ValueError('clarification_parent_changed')
        parent = load(store, ident)
        expected = compose(parent, {**record['payload'], 'draft': original['draft']}, record['answer'])
        if expected != record['payload']:
            raise ValueError('clarification_answer_mismatch')
    return record


def history(store, limit=12):
    root = workspace_state._store_root(store)
    if not root.exists():
        return []
    result = []
    for path in sorted(root.glob('*.json'), key=lambda p: p.stat().st_mtime_ns, reverse=True)[:40]:
        if workspace_state._ID.fullmatch(path.stem):
            try:
                result.append(load(root, path.stem))
            except (OSError, ValueError, KeyError, TypeError):
                continue
        if len(result) >= limit:
            break
    return result
