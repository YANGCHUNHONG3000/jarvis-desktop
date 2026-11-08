"""Session-bound Office planning questions; append-only answers reuse source snapshots."""
from copy import deepcopy
from pathlib import Path
import re
import uuid

from . import office_intent as intent
from .computer import ComputerError, digest, read_record, safe_root, write_new
from .computer_agent import text
from .office_scope import entries

MAX_ANSWERS = 4
_ID = re.compile('[0-9a-f]{32}')
_META_KEYS = {'schema','id','session','session_sha256','state','issue','question',
              'original_instruction','instruction','answers','round','parent',
              'all_sources','all_refs','proofs','sha256'}
_PROOFS = ('request.json', 'sources.json', 'context.json', 'response.json', 'failure.json', 'outcome.json')
_RECOVERABLE = {
    'office_planning_target_invalid': ('missing_target', '所要求的位置未能在当前范围核验，请明确已有位置或调整所选范围。'),
    'office_planning_scope_too_large_or_empty': ('out_of_scope', '当前范围为空或超过容量，请在原文件中明确缩小范围；公式只读。'),
}


def _folder(session, ident):
    if type(ident) is not str or _ID.fullmatch(ident) is None:
        raise ComputerError('office_clarification_id_invalid')
    return safe_root(session.root / 'office-planning' / ident)


def _identify(session, value):
    expected = None
    if type(value) is dict:
        if 'office_result' in value:
            root = safe_root(value['office_result']['record'])
            if root.parent != session.root / 'office-planning':
                raise ComputerError('office_clarification_session_changed')
            ident = root.name
        else:
            ident, expected = value.get('id'), value.get('sha256')
    else:
        ident = value
    return _folder(session, ident), expected


def _proofs(folder):
    return {name: digest(read_record(folder / name)) if (folder / name).exists() else None for name in _PROOFS}


def _verify_sources(session, sources, refs):
    intent._verified_sources(session, [Path(s['path']) for s in sources], sources)
    if refs is not None:
        from .office_handoff import verify_references
        verify_references([s['path'] for s in sources], refs)


def compose_instruction(pending, answer):
    """Compose the exact visible plan instruction, retaining every previous answer."""
    answer = text(answer, 600).strip()
    original = text(pending['original_instruction'], 1000)
    return text(original + ''.join('；用户补充：' + a for a in [*pending['answers'], answer]), 1000)


def _metadata(session, folder, *, original, answers, all_sources, all_refs, parent=None):
    outcome = read_record(folder / 'outcome.json')
    request = read_record(folder / 'request.json')
    response = read_record(folder / 'response.json') if (folder / 'response.json').exists() else {}
    failure = read_record(folder / 'failure.json') if (folder / 'failure.json').exists() else {}
    issue = response.get('issue', 'ambiguous')
    if failure.get('code') in _RECOVERABLE:
        issue = _RECOVERABLE[failure['code']][0]
    state = 'pending' if outcome['office_result']['kind'] == 'clarify' else 'resolved'
    value = {'schema': 1, 'id': folder.name, 'session': str(session.root),
             'session_sha256': digest(read_record(session.root / 'session.json')),
             'state': state, 'issue': issue if state == 'pending' else None,
             'question': outcome['reason'] if state == 'pending' else None,
             'original_instruction': original, 'instruction': request['instruction'],
             'answers': deepcopy(answers), 'round': len(answers), 'parent': deepcopy(parent),
             'all_sources': deepcopy(all_sources), 'all_refs': deepcopy(all_refs),
             'proofs': _proofs(folder)}
    value['sha256'] = digest(value)
    write_new(folder / 'continuation.json', value)


def _run(session, instruction, files, *, original, answers, artifact_refs, scope,
         all_sources=None, all_refs=None, sources=None, folder=None, parent=None,
         client=None, stop=lambda: False):
    folder = folder or _folder(session, uuid.uuid4().hex)
    guard = (lambda: _verify_sources(session, all_sources, all_refs)) if all_sources is not None else None
    try:
        result = intent.propose(session, instruction, files, client=client, stop=stop,
            artifact_refs=artifact_refs, scope=scope, _record=folder, _sources=sources, _source_guard=guard)
    except ComputerError as exc:
        code = str(exc)
        if code not in _RECOVERABLE or not (folder / 'sources.json').exists():
            raise
        if stop():
            raise ComputerError('office_planning_cancelled') from None
        snapshots = read_record(folder / 'sources.json')
        _verify_sources(session, all_sources if all_sources is not None else snapshots,
                        all_refs if all_sources is not None else artifact_refs)
        result = {'plan': None, 'reason': _RECOVERABLE[code][1],
                  'office_result': {'kind': 'clarify', 'record': str(folder)}}
        write_new(folder / 'outcome.json', result)
    snapshots = read_record(folder / 'sources.json')
    _metadata(session, folder, original=original, answers=answers,
              all_sources=all_sources if all_sources is not None else snapshots,
              all_refs=all_refs if all_sources is not None else artifact_refs, parent=parent)
    return result


def begin(session, instruction, files, *, client=None, stop=lambda: False, artifact_refs=None, scope=None):
    return _run(session, instruction, files, original=instruction, answers=[],
                artifact_refs=artifact_refs, scope=scope, client=client, stop=stop)


def _load(session, value, depth=0):
    if depth > MAX_ANSWERS:
        raise ComputerError('office_clarification_chain_limit')
    folder, expected = _identify(session, value)
    record = read_record(folder / 'continuation.json')
    if type(record) is not dict:
        raise ComputerError('office_clarification_record_changed')
    unsigned = {k: v for k, v in record.items() if k != 'sha256'}
    if (set(record) != _META_KEYS
            or type(record.get('schema')) is not int or record['schema'] != 1
            or record.get('id') != folder.name or record.get('session') != str(session.root)
            or record.get('session_sha256') != digest(read_record(session.root / 'session.json'))
            or digest(unsigned) != record.get('sha256') or expected is not None and expected != record['sha256']
            or record.get('state') not in {'pending', 'resolved'}
            or type(record.get('answers')) is not list or type(record.get('round')) is not int
            or record.get('round') != len(record['answers']) or not 0 <= record['round'] <= MAX_ANSWERS
            or record['state'] == 'pending' and record['issue'] not in {'ambiguous','missing_target','out_of_scope'}
            or record['proofs'] != _proofs(folder)):
        raise ComputerError('office_clarification_record_changed')
    text(record['original_instruction'], 1000)
    instruction = record['original_instruction']
    for answer in record['answers']:
        text(answer, 600)
        instruction += '；用户补充：' + answer
    if text(instruction, 1000) != record['instruction']:
        raise ComputerError('office_clarification_instruction_changed')
    request = read_record(folder / 'request.json')
    sources = read_record(folder / 'sources.json')
    result = read_record(folder / 'outcome.json')
    if type(value) is dict and 'office_result' in value and result != {
            k: value.get(k) for k in ('plan','reason','office_result')}:
        raise ComputerError('office_clarification_result_changed')
    if (set(request) != {'instruction','files','artifact_refs','scope'}
            or request['instruction'] != instruction or request['files'] != [s['path'] for s in sources]
            or set(result) != {'plan','reason','office_result'}
            or result['office_result']['record'] != str(folder)
            or (record['state'] == 'pending' and (result['plan'] is not None
                or result['office_result']['kind'] != 'clarify' or record['question'] != result['reason']))):
        raise ComputerError('office_clarification_record_changed')
    _verify_sources(session, record['all_sources'], record['all_refs'])
    intent._verified_sources(session, [Path(p) for p in request['files']], sources)
    available = {s['path']: s for s in record['all_sources']}
    for source in sources:
        original = available.get(source['path'])
        if original is None or any(source[k] != original[k] for k in ('root','public','path')):
            raise ComputerError('office_clarification_source_changed')
    if record['parent'] is not None:
        parent = _load(session, record['parent'], depth + 1)
        claim = read_record(_folder(session, parent['id']) / 'answer-claim.json')
        if (claim != {'parent_sha256': parent['sha256'], 'child': record['id']}
                or record['answers'][:-1] != parent['answers']
                or record['original_instruction'] != parent['original_instruction']
                or record['all_sources'] != parent['all_sources'] or record['all_refs'] != parent['all_refs']):
            raise ComputerError('office_clarification_parent_changed')
    elif (record['answers'] or record['all_sources'] != sources
          or record['all_refs'] != request['artifact_refs']):
        raise ComputerError('office_clarification_parent_changed')
    record.update(files=request['files'], artifact_refs=request['artifact_refs'], scope=request['scope'], result=result)
    claim_path = folder / 'answer-claim.json'
    if claim_path.exists():
        claim = read_record(claim_path)
        if claim.get('parent_sha256') != record['sha256'] or _ID.fullmatch(str(claim.get('child'))) is None:
            raise ComputerError('office_clarification_claim_changed')
        record.update(state='claimed', child=claim['child'])
    return record


def load(session, pending):
    """Revalidate a local record and every original source without any model call."""
    try:
        return _load(session, pending)
    except (OSError, KeyError, TypeError) as exc:
        raise ComputerError('office_clarification_record_invalid') from None


def catalog(session, pending):
    record = load(session, pending)
    return {'files': [{'path': s['path'], 'sha256': s['public']['sha256'],
                       'kind': s['public']['kind'], 'entries': entries(s['public'])}
                      for s in record['all_sources']], 'artifact_refs': deepcopy(record['all_refs'])}


def _answer_inputs(record, answer, selection):
    if record['state'] != 'pending':
        raise ComputerError('office_clarification_already_claimed_or_resolved')
    if record['round'] >= MAX_ANSWERS:
        raise ComputerError('office_clarification_chain_limit')
    instruction = compose_instruction(record, answer)
    files, refs, scope = record['files'], record['artifact_refs'], record['scope']
    if selection is not None:
        if type(selection) is not dict or set(selection) != {'files','artifact_refs','scope'}:
            raise ComputerError('office_clarification_selection_invalid')
        files, refs, scope = selection['files'], selection['artifact_refs'], selection['scope']
        if type(files) not in (list, tuple) or not files or scope is None:
            raise ComputerError('office_clarification_selection_invalid')
        files = [str(Path(p).absolute()) for p in files]
    available = {s['path']: (i, s) for i, s in enumerate(record['all_sources'])}
    if (len(set(files)) != len(files) or not set(files) <= set(available)
            or refs != ([record['all_refs'][available[p][0]] for p in files] if record['all_refs'] is not None else None)):
        raise ComputerError('office_clarification_scope_widened')
    sources = [{**deepcopy(available[p][1]), 'id': f'e{n}'} for n, p in enumerate(files, 1)]
    # Validate the explicit locations before consuming this question or calling the model.
    checked = deepcopy(sources)
    if scope is not None:
        if type(scope) is not list or len(scope) != len(files):
            raise ComputerError('office_clarification_selection_invalid')
        for source, part in zip(checked, scope):
            source['scope'] = part
    else:
        for source in checked:
            source.pop('scope', None)
    intent._context(instruction, checked)
    return instruction, files, refs, scope, sources


def validate_answer(item, answer, selection=None):
    """Pure UI preflight over a loaded question; continuation still reloads all evidence."""
    return _answer_inputs(item, answer, selection)[0]


def continue_attempt(session, pending, answer, *, selection=None, client=None, stop=lambda: False):
    record = load(session, pending)
    instruction, files, refs, scope, sources = _answer_inputs(record, answer, selection)
    if stop():
        raise ComputerError('office_planning_cancelled')
    child = _folder(session, uuid.uuid4().hex)
    try:
        write_new(_folder(session, record['id']) / 'answer-claim.json',
                  {'parent_sha256': record['sha256'], 'child': child.name})
    except FileExistsError:
        raise ComputerError('office_clarification_already_claimed_or_resolved') from None
    return _run(session, instruction, files, original=record['original_instruction'],
                answers=[*record['answers'], answer.strip()], artifact_refs=refs, scope=scope,
                all_sources=record['all_sources'], all_refs=record['all_refs'], sources=sources,
                folder=child, parent={'id': record['id'], 'sha256': record['sha256']}, client=client, stop=stop)


def recent(session, limit=12):
    if type(limit) is not int or not 1 <= limit <= 32:
        raise ComputerError('office_clarification_limit_invalid')
    root = safe_root(session.root / 'office-planning')
    records = []
    for path in sorted(root.glob('*/continuation.json'), key=lambda p: p.stat().st_mtime_ns, reverse=True):
        item = load(session, path.parent.name)
        if item['state'] == 'pending':
            records.append(item)
        if len(records) >= limit:
            break
    return records
