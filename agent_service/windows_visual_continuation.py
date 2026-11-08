"""One executable visual continuation per parent; separate from old evidence."""
from contextlib import contextmanager
from pathlib import Path

from .computer import ComputerError, digest, safe_root
from . import windows_task as task

BASE=Path(__file__).resolve().parents[1]/'data/windows_visual_continuations'


def _directory(parent):
    root=safe_root(parent)
    _,checksum=task._load(root/'task.json')
    return BASE/digest({'parent':str(root),'task_sha256':checksum}),checksum


@contextmanager
def _lock(root):
    root.mkdir(parents=True,exist_ok=True)
    path=safe_root(root/'claim.lease')
    try:
        with path.open('xb') as stream:stream.write(b'!')
    except FileExistsError:
        pass
    with path.open('r+b',buffering=0) as stream:
        task._lock(stream)
        try:yield
        finally:task._unlock(stream)


def existing(parent):
    directory,checksum=_directory(parent)
    path=directory/'link.json'
    if not path.exists():return None
    link,_=task._load(path)
    if link.get('parent')!=str(safe_root(parent)) or link.get('parent_task_sha256')!=checksum:
        raise ComputerError('windows_visual_continuation_changed')
    child=safe_root(link['child'])
    payload,child_sha=task._load(child/'task.json')
    if child_sha!=link.get('child_task_sha256') or payload.get('visual_proposal')!=link.get('proposal'):
        raise ComputerError('windows_visual_continuation_changed')
    return child


def create_once(parent,builder):
    directory,checksum=_directory(parent)
    with _lock(directory):
        child=existing(parent)
        if child is not None:return child
        child=safe_root(builder())
        payload,child_sha=task._load(child/'task.json')
        if payload.get('schema')!=3:raise ComputerError('windows_visual_continuation_invalid')
        task._save(directory/'link.json',{'schema':1,'parent':str(safe_root(parent)),
                   'parent_task_sha256':checksum,'child':str(child),'child_task_sha256':child_sha,
                   'proposal':payload['visual_proposal']})
        return child


def assert_registered(payload, root):
    """Orphan proposals from an interrupted publication cannot execute."""
    parent=payload['continuation']['parent_root']
    child=existing(parent)
    if child is None:raise ComputerError('windows_visual_continuation_unpublished')
    if safe_root(root)!=child:raise ComputerError('windows_visual_continuation_superseded')
    child_task,_=task._load(child/'task.json')
    if child_task['visual_proposal']['sha256']!=digest(payload):
        raise ComputerError('windows_visual_continuation_superseded')
    return True
