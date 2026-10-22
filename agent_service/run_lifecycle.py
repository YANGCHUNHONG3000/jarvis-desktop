"""Immutable run identity and an OS lease; never infer liveness from a PID."""
from contextlib import contextmanager
from hashlib import sha256
import errno
import os
import re
import uuid

from .computer import ComputerError, digest, read_record, safe_root, write_new

OWNER_FILE = 'owner.json'
LEASE_FILE = 'owner.lease'
_SHA = re.compile(r'[0-9a-f]{64}\Z')


def _file_id(stat):
    return {'device':stat.st_dev, 'inode':stat.st_ino}


def _identity(run, lease_bytes, lease_stat):
    run = safe_root(run)
    session = run.parent.parent
    if (run.parent.name != 'goals' or not run.is_dir()
            or not run.name.endswith('.run') or not _SHA.fullmatch(run.name[:-4])):
        raise ComputerError('run_owner_invalid')
    plan = read_record(run.with_suffix('.json'))
    if (type(plan) is not dict or plan.get('schema') != 3 or plan.get('mode') != 'agent'
            or plan.get('sha256') != run.name[:-4]
            or digest({k:v for k,v in plan.items() if k != 'sha256'}) != plan['sha256']
            or type(lease_bytes) is not bytes or not re.fullmatch(rb'[0-9a-f]{32}', lease_bytes)):
        raise ComputerError('run_owner_invalid')
    return {'schema':1, 'run':str(run), 'session':str(session),
            'plan_sha256':plan['sha256'], 'goal_sha256':digest(plan.get('goal')),
            'session_sha256':digest(read_record(session/'session.json')),
            'lease_sha256':sha256(b'!' + lease_bytes).hexdigest(),
            'lease_file_id':_file_id(lease_stat)}


def _lock(stream):
    try:
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        code = ('run_owner_active' if exc.errno in
                {errno.EACCES, errno.EAGAIN, errno.EDEADLK} else 'run_owner_invalid')
        raise ComputerError(code) from None


def _unlock(stream):
    stream.seek(0)
    if os.name == 'nt':
        import msvcrt
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@contextmanager
def own_run(session, plan):
    """Create a run once and hold its lease through all adapter cleanup."""
    run = safe_root(session.root/'goals'/(plan['sha256']+'.run'))
    try:
        run.mkdir(exist_ok=False)
    except FileExistsError:
        raise ComputerError('goal_already_claimed') from None
    # A failed/abrupt initialization remains unusable; never repair or reuse it.
    with (run/LEASE_FILE).open('x+b', buffering=0) as stream:
        token = uuid.uuid4().hex.encode('ascii')
        # Byte zero is locked. Identity bytes remain readable during a live
        # Windows byte-range lock so a probe can validate before testing it.
        stream.write(b'!' + token)
        os.fsync(stream.fileno())
        _lock(stream)
        try:
            owner = _identity(run, token, os.fstat(stream.fileno()))
            write_new(run/OWNER_FILE, owner)
            yield run
        finally:
            _unlock(stream)


def assert_inactive(run):
    """Return validated ownership only if the existing OS lease is free.

    No record or lease is created, truncated, repaired or rewritten by a probe.
    A released lease is evidence of inactivity, never permission to restart it.
    """
    try:
        run = safe_root(run)
        owner_path = safe_root(run/OWNER_FILE)
        if not owner_path.is_file():
            raise ComputerError('run_owner_missing')
        owner = read_record(owner_path)
        lease_path = safe_root(run/LEASE_FILE)
        # Windows _locking needs an existing writable descriptor; no writes occur.
        with lease_path.open('r+b', buffering=0) as stream:
            stream.seek(1)
            if (type(owner) is not dict
                    or digest(owner) != digest(_identity(run, stream.read(33), os.fstat(stream.fileno())))
                    or _file_id(os.stat(lease_path)) != _file_id(os.fstat(stream.fileno()))):
                raise ComputerError('run_owner_invalid')
            claim_path = run/'claim.json'
            if claim_path.exists():
                claim = read_record(claim_path)
                if (type(claim) is not dict or claim.get('approval') != owner['plan_sha256']
                        or digest(claim.get('lifecycle')) != digest(
                            {'version':1,'owner_sha256':digest(owner)})):
                    raise ComputerError('run_owner_invalid')
            _lock(stream)
            try:
                if stream.read(1) != b'!':
                    raise ComputerError('run_owner_invalid')
                stream.seek(1)
                if (digest(owner) != digest(read_record(owner_path))
                        or digest(owner) != digest(_identity(run, stream.read(33), os.fstat(stream.fileno())))
                        or _file_id(os.stat(lease_path)) != _file_id(os.fstat(stream.fileno()))):
                    raise ComputerError('run_owner_invalid')
                return owner
            finally:
                _unlock(stream)
    except ComputerError as exc:
        if str(exc) in {'run_owner_missing','run_owner_invalid','run_owner_active'}:
            raise
        raise ComputerError('run_owner_invalid') from None
    except (OSError, ValueError, TypeError, KeyError):
        raise ComputerError('run_owner_invalid') from None
