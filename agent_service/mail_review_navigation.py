"""Read-only, exact parent-to-child navigation for claimed mail reviews.

A verified association says nothing about the child's execution or artifacts.
Call task_history.inspect_task separately before displaying those results.
"""
from hashlib import sha256
import json
from pathlib import Path
import re

from . import mail_review_resume as resume, mail_understanding as mail, office_workflow as workflow
from .office_resume import _linked, _task


class MailReviewNavigationError(ValueError):
    def __init__(self, code):
        self.safe_code = 'mail_review_navigation_' + code
        super().__init__(self.safe_code)


_MARKER = {'schema', 'parent_root', 'child_root', 'token', 'candidate_sha256', 'plan_sha256'}
_DIGEST = re.compile(r'[0-9a-f]{64}\Z')
_MAX_CLAIM = 16_000
_CHILD_ALLOWED = {'request.txt', 'owner.lock', 'cancel.flag', 'mail-review-resume.json',
                  'mail-analysis-claim.json', 'sources', 'result.json',
                  'mail-analysis-request.json', 'mail-analysis-candidate.json',
                  'mail-analysis-plan.json', 'mail-analysis-review.json'}


def _fail(code):
    raise MailReviewNavigationError(code)


def _identity(path, directory=False):
    if _linked(path) or (not path.is_dir() if directory else not path.is_file()):
        _fail('changed')
    stat = path.stat()
    identity = (stat.st_dev, stat.st_ino)
    return identity if directory else (*identity, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            _fail('changed')
        result[key] = value
    return result


def _read_record(path, absent, maximum=_MAX_CLAIM, *, text=False):
    if _linked(path):
        _fail('changed')
    if not path.exists():
        _fail(absent)
    before = _identity(path)
    if not 0 < before[2] <= maximum:
        _fail('changed')
    raw = path.read_bytes()
    if _identity(path) != before or not 0 < len(raw) <= maximum:
        _fail('changed')
    value = raw.decode('utf-8') if text else json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs)
    if not text and type(value) is not dict:
        _fail('changed')
    return value, {'identity': before, 'sha256': sha256(raw).hexdigest()}


def _read_claim(path, absent):
    return _read_record(path, absent)


def _child_evidence(child, base, marker, original):
    """Prove the child's own payload, rather than trusting claim agreement."""
    allowed = _CHILD_ALLOWED
    names = set()
    effectful = False
    for path in child.iterdir():
        if len(names) >= 64 or _linked(path):
            _fail('changed')
        names.add(path.name)
        if path.name not in allowed or path.is_dir() and path.name != 'sources':
            effectful = True
    stamps = {}
    expected = {'request.txt': original['instruction'],
                'mail-analysis-request.json': original['request'],
                'mail-analysis-candidate.json': original['candidate'],
                'mail-analysis-plan.json': original['plan']}
    for name, value in expected.items():
        if name in names:
            actual, stamp = _read_record(child / name, 'incomplete', 2_000_000,
                                         text=name == 'request.txt')
            if actual != value or workflow._hash(actual) != workflow._hash(value):
                _fail('changed')
            stamps[str(child / name)] = stamp['identity']
    source_dir = child / 'sources'
    if 'sources' in names:
        _identity(source_dir, directory=True)
        source_names = set()
        for path in source_dir.iterdir():
            if len(source_names) >= 3 or path.name not in resume._SOURCES:
                _fail('changed')
            source_names.add(path.name)
            before = _identity(path)
            raw = path.read_bytes() if before[2] <= 16_000_000 else b''
            proof = original['files']['sources/' + path.name]
            if (_identity(path) != before or len(raw) != proof['bytes']
                    or sha256(raw).hexdigest() != proof['sha256']):
                _fail('changed')
            stamps[str(path)] = before
    if 'mail-analysis-review.json' in names:
        review, stamp = _read_record(child / 'mail-analysis-review.json', 'incomplete', 2_000_000)
        if (set(review) != {'plan_sha256', 'verdict'}
                or review['plan_sha256'] != workflow._hash(original['plan'])):
            _fail('changed')
        stamps[str(child / 'mail-analysis-review.json')] = stamp['identity']
    result = None
    if 'result.json' in names:
        result, stamp = _read_record(child / 'result.json', 'incomplete', 2_000_000)
        result_keys = resume._RESULT | {'mail_review_resume', 'mail_understanding', 'workflow_root',
                                        'artifact', 'readback_sha256', 'reason'}
        if (set(result) - result_keys or result.get('root') != str(child) or result.get('kind') != 'office'
                or type(result.get('ok')) is not bool
                or result.get('state') not in {'failed', 'unknown', 'succeeded', 'cancelled', 'timed_out'}
                or (result['state'] == 'succeeded') != result['ok']
                or type(result.get('completed')) is not list or type(result.get('artifacts')) is not list
                or result.get('mail_review_resume') != marker
                or workflow._hash(result.get('mail_review_resume')) != workflow._hash(marker)):
            _fail('changed')
        stamps[str(child / 'result.json')] = stamp['identity']
        effectful = (effectful or result['ok'] or bool(result['completed']) or bool(result['artifacts'])
                     or any(key in result for key in ('workflow_root', 'artifact', 'mail_understanding')))
        if 'mail_analysis_schema' in result and (type(result['mail_analysis_schema']) is not int
                                                  or result['mail_analysis_schema'] != 4):
            _fail('changed')
        source = resume._json(Path(original['task_root']) / 'sources' / 'source.json')
        expected_result = {
            'source_provenance': {'kind': 'verified_generated_office_artifact', **original['source_ref']},
            'source': {key: source.get(key) for key in
                       ('source_id', 'kind', 'sha256', 'bytes', 'display_name', 'coverage')},
        }
        if any(key in result and result[key] != value for key, value in expected_result.items()):
            _fail('changed')
    if effectful:
        if result is None:
            _fail('incomplete')
        mail.verify_task(child, base, original['plan']['steps'], parent=result)
        # verify_task proves the reviewed plan. The actual workflow must use it.
        named = result.get('workflow_root')
        if named is not None:
            if type(named) is not str:
                _fail('changed')
            work = Path(named)
            if (not work.is_absolute() or work.parent != child
                    or not re.fullmatch(r'workflow-[0-9a-f]{32}', work.name)):
                _fail('changed')
            if any(name != work.name and name != 'resume-latest.json'
                   and not re.fullmatch(r'resume-[0-9a-f]{32}', name)
                   for name in names - allowed):
                _fail('changed')
            _identity(work, directory=True)
            manifest, stamp = _read_record(work / 'manifest.json', 'incomplete', 2_000_000)
            if type(manifest.get('version')) is not int or manifest != {'version': 1, 'steps': original['plan']['steps'],
                            'steps_sha256': workflow._hash(original['plan']['steps'])}:
                _fail('changed')
            stamps[str(work / 'manifest.json')] = stamp['identity']
        elif result['ok'] or result['completed'] or result['artifacts'] or names - allowed:
            _fail('changed')
    return stamps, names


def _resolve(parent_root, base):
    parent = _task(parent_root, base)
    registry = resume._registry(base)
    claim_path = registry / (parent.name + '.json')
    marker, parent_stamp = _read_claim(claim_path, 'missing')
    if (set(marker) != _MARKER or type(marker['schema']) is not int or marker['schema'] != 1
            or marker['parent_root'] != str(parent)
            or any(type(marker[k]) is not str or not _DIGEST.fullmatch(marker[k])
                   for k in ('token', 'candidate_sha256', 'plan_sha256'))
            or type(marker['child_root']) is not str):
        _fail('changed')
    candidate = Path(marker['child_root'])
    # Validate scope even when the directory is missing, without following links.
    if (not candidate.is_absolute() or candidate.parent != parent.parent
            or not re.fullmatch(r'task-[0-9a-f]{32}', candidate.name)
            or candidate == parent or _linked(candidate)):
        _fail('changed')
    if not candidate.exists():
        _fail('unavailable')
    child = _task(candidate, base)
    if str(child) != marker['child_root']:
        _fail('changed')
    children = registry / 'children'
    if _linked(children) or children.exists() and not children.is_dir():
        _fail('changed')
    paths = [children / (child.name + '.json'), child / 'mail-review-resume.json',
             child / 'mail-analysis-claim.json']
    records = [_read_claim(path, 'incomplete') for path in paths]
    if (records[0][0] != marker or records[1][0] != marker
            or any(type(record[0].get('schema')) is not int for record in records)
            or records[2][0] != {'schema': 1, 'review_resume': workflow._hash(marker)}):
        _fail('changed')
    original = resume._snapshot(parent, base)
    if (original['token'] != marker['token']
            or workflow._hash(original['candidate']) != marker['candidate_sha256']
            or workflow._hash(original['plan']) != marker['plan_sha256']):
        _fail('changed')
    child_files, child_entries = _child_evidence(child, base, marker, original)
    # The full parent proof runs in the worker. The receiving UI only checks
    # these fixed paths, metadata, and four small immutable claim files.
    parent_files = {}
    for name, proof in original['files'].items():
        stamp = _identity(parent / name)
        if stamp[2:4] != (proof['bytes'], proof['mtime_ns']):
            _fail('changed')
        parent_files[name] = stamp
    guard = {
        'base': str(parent.parent),
        'parent_root': str(parent), 'child_root': str(child), 'token': marker['token'],
        'directories': {str(p): _identity(p, directory=True)
                        for p in (parent.parent, parent, parent / 'sources', registry, children, child)},
        'claims': {str(claim_path): parent_stamp,
                   **{str(path): record[1] for path, record in zip(paths, records)}},
        'parent_files': parent_files,
        'child_files': child_files,
        'child_entries': child_entries,
    }
    resolved = {'parent_root': str(parent), 'child_root': str(child), 'token': marker['token'],
                '_fast_guard': guard}
    if not link_unchanged(resolved, base):
        _fail('changed')
    return resolved


def resolve_continuation(parent_root, base):
    """Verify a single exact association; never list, write, execute, or retry."""
    try:
        return _resolve(parent_root, base)
    except MailReviewNavigationError:
        raise
    except (OSError, ValueError, UnicodeError, KeyError, TypeError) as exc:
        raise MailReviewNavigationError('changed') from exc


def link_unchanged(resolved, base):
    """Bounded UI acceptance guard; artifacts still require normal inspection."""
    try:
        guard = resolved['_fast_guard']
        if str(Path(base).absolute().resolve()) != guard['base'] or _linked(Path(base).absolute()):
            return False
        if any(resolved[key] != guard[key] for key in ('parent_root', 'child_root', 'token')):
            return False
        parent = Path(resolved['parent_root'])
        child = Path(resolved['child_root'])
        if parent.parent != Path(guard['base']) or child.parent != parent.parent or child == parent:
            return False
        for named, expected in guard['directories'].items():
            if _identity(Path(named), directory=True) != expected:
                return False
        for name, expected in guard['parent_files'].items():
            if _identity(parent / name) != expected:
                return False
        for named, expected in guard['child_files'].items():
            if _identity(Path(named)) != expected:
                return False
        # New preparatory records are normal. New results or effect directories
        # require a fresh worker proof before the history inspector can show them.
        current_names = set()
        for path in child.iterdir():
            if len(current_names) >= 64 or _linked(path):
                return False
            current_names.add(path.name)
        if ('result.json' in current_names) != ('result.json' in guard['child_entries']):
            return False
        if current_names - guard['child_entries'] - (_CHILD_ALLOWED - {'result.json'}):
            return False
        for named, expected in guard['claims'].items():
            _, actual = _read_claim(Path(named), 'incomplete')
            if actual != expected:
                return False
        return True
    except (OSError, ValueError, UnicodeError, KeyError, TypeError):
        return False


def describe_error(error):
    """Fixed UI text; no exception details or record contents are displayed."""
    code = getattr(error, 'safe_code', error)
    return {
        'mail_review_navigation_missing': '此任务尚无邮件复核续接记录；原失败记录保留。',
        'mail_review_navigation_unavailable': '已认领的续接任务目前不可用；不会重新执行，请保留原记录。',
        'mail_review_navigation_incomplete': '续接认领尚未完整写入；原记录保留，不会重新执行。',
        'mail_review_navigation_changed': '邮件复核续接关联或原证据已变化，无法核验；原记录保留。',
    }.get(code, '邮件复核续接关联无法核验；原记录保留，未启动任务。')
