"""Explicit one-shot REVIEW continuation of a provably undispatched mail plan."""
from hashlib import sha256
import json
from pathlib import Path
import re

from . import mail_understanding as mail, office_workflow as workflow
from .office_resume import _task, _linked, _small_bytes


class MailReviewResumeError(ValueError):
    def __init__(self, code):
        self.safe_code = code
        super().__init__(code)


_FILES = {'request.txt', 'mail-analysis-claim.json', 'mail-analysis-request.json',
          'mail-analysis-candidate.json', 'mail-analysis-plan.json', 'result.json', 'owner.lock', 'sources'}
_SOURCES = {'source.docx', 'source.json', 'provenance.json'}
_RESULT = {'ok', 'state', 'kind', 'root', 'completed', 'artifacts', 'error',
           'source', 'source_provenance', 'mail_analysis_schema', 'budget'}
_ERRORS = {'model_input_invalid', 'model_network_failed', 'model_response_incomplete'}


def _fail(code):
    raise MailReviewResumeError('mail_review_resume_' + code)


def _json(path):
    value = json.loads(_small_bytes(path, 2_000_000).decode('utf-8'))
    if type(value) is not dict:
        _fail('record_invalid')
    return value


def _inventory(root):
    if {p.name for p in root.iterdir()} != _FILES:
        _fail('effects_unknown')
    source = root / 'sources'
    if _linked(source) or not source.is_dir() or {p.name for p in source.iterdir()} != _SOURCES:
        _fail('effects_unknown')
    files = [root / n for n in _FILES - {'sources'}] + [source / n for n in _SOURCES]
    result = {}
    for path in files:
        if _linked(path) or not path.is_file() or path.stat().st_size > 16_000_000:
            _fail('record_invalid')
        before = path.stat()
        raw = path.read_bytes()
        after = path.stat()
        if len(raw) > 16_000_000 or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            _fail('changed')
        result[path.relative_to(root).as_posix()] = {
            'sha256': sha256(raw).hexdigest(), 'bytes': len(raw), 'mtime_ns': after.st_mtime_ns}
    return result


def _registry(base):
    path = Path(base).absolute() / '.mail-review-resumes'
    if _linked(path) or path.exists() and not path.is_dir():
        _fail('claim_invalid')
    return path


def _claims_for_child(task, base):
    """Look up this child only; unrelated claims never affect task verification."""
    directory = _registry(base) / 'children'
    path = directory / (Path(task).name + '.json')
    if _linked(path):
        _fail('claim_invalid')
    if not path.exists():
        return []
    return [_json(path)]


def _snapshot(task_root, base):
    root = _task(task_root, base)
    if not (root / 'mail-analysis-request.json').exists():
        _fail('not_mail')
    if (root / 'mail-review-resume.json').exists() or _claims_for_child(root, base):
        _fail('nested')
    raw = _small_bytes(root / 'result.json', 2_000_000)
    result = json.loads(raw.decode('utf-8'))
    if (type(result) is not dict or result.get('ok') is not False
            or result.get('state') != 'failed' or result.get('kind') != 'office'
            or result.get('error') not in _ERRORS
            or type(result.get('mail_analysis_schema')) is not int or result['mail_analysis_schema'] != 4):
        _fail('not_eligible')
    if set(result) != _RESULT or result['root'] != str(root) or result['completed'] != [] or result['artifacts'] != []:
        _fail('effects_unknown')
    before = _inventory(root)
    if before['result.json']['sha256'] != sha256(raw).hexdigest():
        _fail('changed')
    claim = _json(root / 'mail-analysis-claim.json')
    if set(claim) != {'schema'} or type(claim['schema']) is not int or claim['schema'] != 1:
        _fail('record_invalid')
    req = _json(root / 'mail-analysis-request.json')
    if (set(req) != {'schema', 'instruction', 'source_ref', 'mail_sha256', 'context'}
            or type(req['schema']) is not int or req['schema'] != 4
            or type(req['instruction']) is not str or not 1 <= len(req['instruction'].strip()) <= 4000
            or _small_bytes(root / 'request.txt', 48000).decode('utf-8') != req['instruction']):
        _fail('record_invalid')
    try:
        source = mail.load_mail(req['source_ref'], base)
        data = mail.context(source)
        from .local_skills import _prepared_snapshot
        snapshot = _json(root / 'sources' / 'source.json')
        provenance = {'kind': 'verified_generated_office_artifact', **req['source_ref']}
        if (req['mail_sha256'] != workflow._hash(source) or req['context'] != data
                or result['source_provenance'] != provenance
                or _json(root / 'sources' / 'provenance.json') != provenance
                or result['source'] != {k: snapshot.get(k) for k in ('source_id', 'kind', 'sha256', 'bytes', 'display_name', 'coverage')}
                or _prepared_snapshot(root, snapshot, base) != snapshot):
            raise ValueError('source changed')
    except (ValueError, OSError, KeyError, TypeError) as exc:
        raise MailReviewResumeError('mail_review_resume_source_changed') from exc
    candidate = _json(root / 'mail-analysis-candidate.json')
    if set(candidate) != {'request_sha256', 'value'} or candidate['request_sha256'] != workflow._hash(req):
        _fail('record_invalid')
    value = mail.validate(candidate['value'], data, schema=4)
    plan = _json(root / 'mail-analysis-plan.json')
    if value['state'] != 'ready' or plan != {'request_sha256': workflow._hash(req), 'value': value, 'steps': mail.compile_steps(value)}:
        _fail('plan_changed')
    if _inventory(root) != before:
        _fail('changed')
    return {'task_root': str(root), 'token': workflow._hash({'root': str(root), 'files': before}),
            'instruction': req['instruction'], 'source_ref': req['source_ref'],
            'request': req, 'candidate': candidate, 'plan': plan, 'files': before}


def inspect_resume(task_root, base):
    """Read-only preview; never claim or execute."""
    try:
        value = _snapshot(task_root, base)
        if (_registry(base) / (Path(value['task_root']).name + '.json')).exists():
            _fail('already_claimed')
        return {'eligible': True, 'code': 'mail_review_resume_ready',
                **{k: value[k] for k in ('task_root', 'token', 'instruction', 'source_ref')}}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {'eligible': False, 'code': str(exc) if isinstance(exc, MailReviewResumeError) else 'mail_review_resume_record_invalid'}


def prepare_resume(task_root, base, expected_token):
    """Revalidate all parent evidence without writing any file."""
    if type(expected_token) is not str or not re.fullmatch(r'[0-9a-f]{64}', expected_token):
        _fail('changed')
    try:
        value = _snapshot(task_root, base)
        if value['token'] != expected_token:
            _fail('changed')
        if (_registry(base) / (Path(value['task_root']).name + '.json')).exists():
            _fail('already_claimed')
        return value
    except (OSError, ValueError, KeyError, TypeError) as exc:
        if isinstance(exc, MailReviewResumeError):
            raise
        raise MailReviewResumeError('mail_review_resume_record_invalid') from exc


def claim_resume(payload, child, base):
    """Persist a one-shot claim before any cloud call. Never release it."""
    checked = prepare_resume(payload['task_root'], base, payload['token'])
    if checked != payload or str(child) == payload['task_root']:
        _fail('changed')
    directory = _registry(base)
    directory.mkdir(exist_ok=True)
    marker = {'schema': 1, 'parent_root': payload['task_root'], 'token': payload['token'],
              'child_root': str(child), 'candidate_sha256': workflow._hash(payload['candidate']),
              'plan_sha256': workflow._hash(payload['plan'])}
    try:
        workflow._write_new(directory / (Path(payload['task_root']).name + '.json'), marker)
    except FileExistsError as exc:
        raise MailReviewResumeError('mail_review_resume_already_claimed') from exc
    children = directory / 'children'
    if _linked(children) or children.exists() and not children.is_dir():
        _fail('claim_invalid')
    children.mkdir(exist_ok=True)
    workflow._write_new(children / (Path(child).name + '.json'), marker)
    workflow._write_new(child / 'mail-review-resume.json', marker)
    return marker


def verify_binding(task, base, parent, req, candidate, plan):
    """Detect removed markers via both the execution claim and registry."""
    marker_path = task / 'mail-review-resume.json'
    claim = _json(task / 'mail-analysis-claim.json')
    claims = _claims_for_child(task, base)
    marked = marker_path.exists() or 'mail_review_resume' in parent or 'review_resume' in claim or bool(claims)
    if not marked:
        return None
    marker = _json(marker_path)
    if (set(marker) != {'schema', 'parent_root', 'token', 'child_root', 'candidate_sha256', 'plan_sha256'}
            or type(marker['schema']) is not int or marker['schema'] != 1
            or marker['child_root'] != str(task) or parent.get('mail_review_resume') != marker
            or claim != {'schema': 1, 'review_resume': workflow._hash(marker)} or claims != [marker]):
        _fail('binding_changed')
    original = _snapshot(marker['parent_root'], base)
    parent_claim = _json(_registry(base) / (Path(original['task_root']).name + '.json'))
    if (parent_claim != marker or original['token'] != marker['token'] or req != original['request']
            or candidate != original['candidate'] or plan != original['plan']
            or marker['candidate_sha256'] != workflow._hash(candidate)
            or marker['plan_sha256'] != workflow._hash(plan)):
        _fail('binding_changed')
    return workflow._hash(marker)


def describe(preview):
    if preview.get('eligible') is True:
        return '原候选与执行计划已核验且尚未执行。只重新复核，不重新生成；创建一次新任务，复核通过后才运行办公步骤，旧失败记录保留。'
    return {
        'mail_review_resume_not_mail': '',
        'mail_review_resume_already_claimed': '此邮件已认领一次复核续接；不能重复付费执行，请检查新任务记录。',
        'mail_review_resume_effects_unknown': '发现执行记录或未知作用，不能重新复核续接。',
        'mail_review_resume_source_changed': '邮件来源或快照已变化，不能续接旧计划。',
        'mail_review_resume_changed': '预览后记录已变化，请刷新历史重新核验。',
        'mail_review_resume_not_eligible': '此任务不符合未执行计划的复核续接条件。',
        'mail_review_resume_nested': '续接任务不能再次重新复核；已有办公步骤请使用办公恢复入口。',
    }.get(preview.get('code'), '邮件复核续接证据无法核验；原记录保留，未启动任务。')
