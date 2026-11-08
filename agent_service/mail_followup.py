"""Read-only preparation of a new request from a stopped mail analysis.

No execution, model call, claim, or mutation is permitted here. The caller must
still ask for a complete user instruction and explicitly start a new task.
"""
from hashlib import sha256
import json
import re

from . import mail_understanding as mail, office_workflow as workflow
from .office_resume import _task, _linked, _small_bytes


class MailFollowupError(ValueError):
    pass


_BASE_FILES = {'request.txt', 'mail-analysis-claim.json', 'mail-analysis-request.json',
               'mail-analysis-candidate.json', 'result.json', 'owner.lock', 'sources'}
_REVIEW_FILES = {'mail-analysis-plan.json', 'mail-analysis-review.json'}
_SOURCE_FILES = {'source.docx', 'source.json', 'provenance.json'}
_RESULT_KEYS = {'ok', 'state', 'kind', 'root', 'completed', 'artifacts', 'error',
                'reason', 'source', 'source_provenance', 'mail_analysis_schema', 'budget'}


def _json(path):
    value = json.loads(_small_bytes(path, 2_000_000).decode('utf-8'))
    if type(value) is not dict:
        raise MailFollowupError('mail_followup_record_invalid')
    return value


def _inventory(root, rejected):
    expected = _BASE_FILES | (_REVIEW_FILES if rejected else set())
    if {p.name for p in root.iterdir()} != expected:
        raise MailFollowupError('mail_followup_effects_unknown')
    source_dir = root / 'sources'
    if _linked(source_dir) or not source_dir.is_dir() or {p.name for p in source_dir.iterdir()} != _SOURCE_FILES:
        raise MailFollowupError('mail_followup_effects_unknown')
    fingerprints = {}
    paths = [root / name for name in expected - {'sources'}]
    paths += [source_dir / name for name in _SOURCE_FILES]
    for path in paths:
        if _linked(path) or not path.is_file() or path.stat().st_size > 16_000_000:
            raise MailFollowupError('mail_followup_record_invalid')
        raw = path.read_bytes()
        if len(raw) > 16_000_000:
            raise MailFollowupError('mail_followup_record_invalid')
        fingerprints[path.relative_to(root).as_posix()] = sha256(raw).hexdigest()
    return fingerprints


def _snapshot(task_root, base):
    root = _task(task_root, base)
    if not (root / 'mail-analysis-request.json').exists():
        code = 'mail_followup_record_invalid' if (root / 'mail-analysis-claim.json').exists() else 'mail_followup_not_mail'
        raise MailFollowupError(code)
    result_bytes = _small_bytes(root / 'result.json', 2_000_000)
    result = json.loads(result_bytes.decode('utf-8'))
    if type(result) is not dict:
        raise MailFollowupError('mail_followup_record_invalid')
    rejected = result.get('error') == 'mail_analysis_review_rejected'
    expected = ('failed', 'mail_analysis_review_rejected') if rejected else ('needs_input', 'mail_analysis_needs_input')
    if (result.get('ok') is not False or result.get('kind') != 'office'
            or (result.get('state'), result.get('error')) != expected
            or type(result.get('mail_analysis_schema')) is not int or result['mail_analysis_schema'] not in (2, 3, 4)):
        raise MailFollowupError('mail_followup_not_eligible')
    if (set(result) != _RESULT_KEYS or result['root'] != str(root)
            or result['completed'] != [] or result['artifacts'] != []):
        raise MailFollowupError('mail_followup_effects_unknown')
    before = _inventory(root, rejected)
    if before['result.json'] != sha256(result_bytes).hexdigest():
        raise MailFollowupError('mail_followup_changed')
    claim = _json(root / 'mail-analysis-claim.json')
    if set(claim) != {'schema'} or type(claim['schema']) is not int or claim['schema'] != 1:
        raise MailFollowupError('mail_followup_record_invalid')
    req = _json(root / 'mail-analysis-request.json')
    if (set(req) != {'schema', 'instruction', 'source_ref', 'mail_sha256', 'context'}
            or type(req['schema']) is not int or req['schema'] not in (2, 3, 4)
            or req['schema'] != result['mail_analysis_schema']
            or type(req['instruction']) is not str or not 1 <= len(req['instruction'].strip()) <= 4000
            or _small_bytes(root / 'request.txt', 48000).decode('utf-8') != req['instruction']):
        raise MailFollowupError('mail_followup_record_invalid')
    try:
        source = mail.load_mail(req['source_ref'], base)
        data = mail.context(source)
        if req['mail_sha256'] != workflow._hash(source) or req['context'] != data:
            raise ValueError('source changed')
        from .local_skills import _prepared_snapshot
        snapshot = _json(root / 'sources' / 'source.json')
        provenance = {'kind': 'verified_generated_office_artifact', **req['source_ref']}
        if (result['source_provenance'] != provenance
                or _json(root / 'sources' / 'provenance.json') != provenance
                or result['source'] != {k: snapshot.get(k) for k in ('source_id', 'kind', 'sha256', 'bytes', 'display_name', 'coverage')}
                or _prepared_snapshot(root, snapshot, base) != snapshot):
            raise ValueError('source changed')
    except (ValueError, OSError, KeyError, TypeError) as exc:
        raise MailFollowupError('mail_followup_source_changed') from exc
    candidate = _json(root / 'mail-analysis-candidate.json')
    if set(candidate) != {'request_sha256', 'value'} or candidate['request_sha256'] != workflow._hash(req):
        raise MailFollowupError('mail_followup_record_invalid')
    value = mail.validate(candidate['value'], data, schema=req['schema'])
    if rejected:
        plan = _json(root / 'mail-analysis-plan.json')
        review = _json(root / 'mail-analysis-review.json')
        if (value['state'] != 'ready' or set(plan) != {'request_sha256', 'value', 'steps'}
                or plan['request_sha256'] != workflow._hash(req) or plan['value'] != value
                or workflow.validate_steps(plan['steps']) != plan['steps']
                or (mail.compile_steps(value) != plan['steps'] if req['schema'] in (3, 4) else
                    [s['action'] for s in plan['steps']] != value['formats'])
                or set(review) != {'plan_sha256', 'verdict'} or review['plan_sha256'] != workflow._hash(plan)
                or type(review['verdict']) is not dict or set(review['verdict']) != {'approved', 'reason'}
                or review['verdict']['approved'] is not False or result['reason'] != review['verdict']['reason']):
            raise MailFollowupError('mail_followup_record_invalid')
    elif value['state'] != 'needs_input' or result['reason'] != value['reason']:
        raise MailFollowupError('mail_followup_record_invalid')
    mail._text(result['reason'], 500)
    if _inventory(root, rejected) != before:
        raise MailFollowupError('mail_followup_changed')
    return {'task_root': str(root), 'token': workflow._hash({'root': str(root), 'files': before}),
            'source_ref': req['source_ref'], 'instruction': req['instruction'],
            'question': ' '.join(re.sub(r'[\x00-\x1f\x7f]', ' ', result['reason']).split()),
            'code': 'mail_followup_review_rejected' if rejected else 'mail_followup_needs_input'}


def inspect_followup(task_root, base):
    """Show a bounded diagnosis without treating the old task as resumable."""
    try:
        snapshot = _snapshot(task_root, base)
        return {'eligible': True, **{k: snapshot[k] for k in ('task_root', 'token', 'question', 'code')}}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        code = str(exc) if isinstance(exc, MailFollowupError) else 'mail_followup_record_invalid'
        return {'eligible': False, 'code': code}


def prepare_followup(task_root, base, expected_token):
    """Recheck the preview. Return inputs only; never create or replay a task."""
    if type(expected_token) is not str or not re.fullmatch(r'[0-9a-f]{64}', expected_token):
        raise MailFollowupError('mail_followup_changed')
    try:
        snapshot = _snapshot(task_root, base)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        if isinstance(exc, MailFollowupError):
            raise
        raise MailFollowupError('mail_followup_record_invalid') from exc
    if snapshot['token'] != expected_token:
        raise MailFollowupError('mail_followup_changed')
    return snapshot


def describe(preview):
    if preview.get('eligible') is True:
        return ('邮件分析阶段已停止，尚无可续跑的办公步骤。模型提示（请结合原文核对）：'
                + str(preview.get('question', ''))[:500]
                + '\n可点击“补充邮件要求”带回原指令与来源；补全要求后显式开始新任务。旧记录保留。')
    return {
        'mail_followup_not_mail': '',
        'mail_followup_not_eligible': '此邮件任务不符合澄清准备条件；已有执行请使用对应办公恢复入口，不能重放。',
        'mail_followup_effects_unknown': '发现执行记录或无法确认的作用，不能作为未执行邮件重试；请检查原任务记录。',
        'mail_followup_source_changed': '邮件来源或快照已变化；未带入旧来源，请重新读取并核验邮件。',
        'mail_followup_changed': '预览后的记录已变化；未带入，请刷新历史重新核验。',
    }.get(preview.get('code'), '邮件分析记录无法核验；未带入来源，原记录保留。')
