"""Selected-window images supplement, never widen, reviewed native targets."""
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import time
import math

from .computer import ComputerError, same_identity
from . import windows_task as task


def capture_frame(window, root, check):
    """Native worker: guarded PrintWindow, never capture the whole desktop."""
    from . import windows_visual as visual
    from .windows_access import _tree
    visual.prepare(window, check)
    observed, _ = _tree(window, check, reject_password=True)
    if observed['truncated']:
        raise ComputerError('windows_vision_sensitive_check_incomplete')
    with visual._physical_pixels():
        pixels, identity, kind, rect = visual._capture_physical(window, check, full_window=True)
        check()
        latest, current, _, current_rect = visual._capture_physical(window, check, full_window=True)
    if pixels != latest or identity != current or rect != current_rect:
        raise ComputerError('windows_vision_screen_changed')
    observed, _ = _tree(window, check, reject_password=True)
    if observed['truncated']:
        raise ComputerError('windows_vision_sensitive_check_incomplete')
    # Keep the existing native worker dependency set. Host Pillow handles PNG.
    if len(pixels) > 48 * 1024 * 1024:
        raise ComputerError('windows_vision_frame_limit')
    path = Path(root) / 'selected-window.bmp'
    with path.open('xb') as stream:
        stream.write(pixels)
    return {'window': identity, 'application': kind, 'rect': rect,
            'path': str(path.resolve()), 'sha256': sha256(pixels).hexdigest(),
            'captured_at': time.time()}


def _bounds(control, rect):
    bounds = control.get('bounds')
    if (type(bounds) is not list or len(bounds) != 4 or any(type(v) is not int for v in bounds)
            or not rect[0] <= bounds[0] < bounds[2] <= rect[2]
            or not rect[1] <= bounds[1] < bounds[3] <= rect[3]):
        raise ComputerError('windows_vision_target_geometry')
    return [bounds[0]-rect[0], bounds[1]-rect[1], bounds[2]-rect[0], bounds[3]-rect[1]]


def prepare_context(backend, fresh, targets, *, stop=lambda:False):
    """Recheck labels after capture; output images only to the authorized client."""
    from PIL import Image
    from .community_annotations import annotate
    if type(fresh) is not list or not 1 <= len(fresh) <= 2:
        raise ComputerError('windows_vision_observation_invalid')
    images, proof = [], {'schema':1, 'sources':[]}
    for number, observed in enumerate(fresh,1):
        if stop(): raise ComputerError('windows_intent_cancelled')
        if observed.get('blocked_reason') or observed.get('dialogs') or observed.get('truncated') is not False:
            raise ComputerError('windows_vision_manual_attention')
        started = time.time()
        frame = backend.vision_frame(observed['window'],stop=stop)
        captured = frame.get('captured_at')
        if (type(captured) not in (int,float) or not math.isfinite(captured)
                or not started-1 <= captured <= time.time()+1):
            raise ComputerError('windows_vision_frame_stale')
        rect = frame.get('rect')
        if (not same_identity(frame.get('window',{}),observed['window'])
                or frame.get('window',{}).get('title') != observed['window'].get('title')
                or frame.get('application') != observed.get('application')
                or type(rect) is not list or len(rect)!=4 or any(type(v) is not int for v in rect)
                or not 0 < rect[2]-rect[0] <=4096 or not 0 < rect[3]-rect[1] <=4096):
            raise ComputerError('windows_vision_frame_changed')
        path = Path(frame['path']).resolve()
        parent = Path(backend.parent).resolve()
        if not path.is_relative_to(parent) or path.name != 'selected-window.bmp' or path.stat().st_size > 48*1024*1024:
            raise ComputerError('windows_vision_frame_invalid')
        png = path.read_bytes()
        if sha256(png).hexdigest() != frame.get('sha256'):
            raise ComputerError('windows_vision_frame_changed')
        with Image.open(BytesIO(png)) as image:
            if image.format != 'BMP' or image.size != (rect[2]-rect[0],rect[3]-rect[1]):
                raise ComputerError('windows_vision_frame_invalid')
            image.load()
            if len(image.convert('RGB').getcolors(1) or []) == 1:
                raise ComputerError('windows_vision_blank_frame')
        current = backend.observe(observed['window'],stop=stop)
        if (not same_identity(current.get('window',{}),observed['window'])
                or current.get('window',{}).get('title') != observed['window'].get('title')
                or current.get('blocked_reason') or current.get('dialogs') or current.get('truncated') is not False):
            raise ComputerError('windows_vision_frame_changed')
        labels=[]
        for target_id, target in targets.items():
            original, selector = target[:2]
            if original['window'] != observed['window'] or selector is None:
                continue  # Existing specialized proof remains separate, never invented.
            control = task.locate(observed, selector)
            if control is None: continue  # Future Find fields have no current pixels.
            live = task.locate(current,selector)
            keys=('id','type','name','automation_id','bounds','enabled','visible','selected','expanded','checked','value')
            if live is None or any(control.get(k)!=live.get(k) for k in keys):
                raise ComputerError('windows_vision_target_changed')
            if control.get('password') or control.get('is_password'):
                raise ComputerError('windows_vision_target_changed')
            labels.append({'id':target_id,'bounds':_bounds(control,rect)})
        if not labels or len(labels)>100:
            raise ComputerError('windows_vision_target_limit')
        annotated=annotate(png,labels)
        images.append(annotated)
        proof['sources'].append({**frame,'image_number':number,'labels':labels,
                                'annotated_sha256':sha256(annotated).hexdigest()})
    if stop(): raise ComputerError('windows_intent_cancelled')
    return images, proof
