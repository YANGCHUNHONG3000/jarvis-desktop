"""Fresh OCR navigation proposals with measured selection and live toolbar text."""
from copy import deepcopy

from .computer import ComputerError
from . import windows_visual as visual
from . import windows_visual_memory as memory


def capture_snapshot(window, check):
    """Native worker only; stable selected-window pixels, no navigation input."""
    visual.prepare(window, check)
    for attempt in range(3):
        pixels, identity, kind, rect = visual.capture(window, check)
        frame = visual.recognize(pixels, None, check)
        latest, current, _, current_rect = visual.capture(window, check)
        if latest == pixels and current == identity and current_rect == rect:
            return {'window': identity, 'application': kind, 'frame': frame, 'rect': rect}
    raise ComputerError('screen_changed_reobserve')


def _page(snapshot, label):
    if (type(snapshot) is not dict or set(snapshot) != {'window','application','frame','rect'}
            or snapshot['application'] not in {'wps','explorer'}):
        raise ComputerError('windows_visual_discovery_invalid')
    memory._window(snapshot['window'])
    selector = {'type':visual.TYPE,'automation_id':visual.ID,'name':label}
    # Placeholder terms only locate the actual navigation row, never prove a page.
    _, details = visual.presentation(snapshot['frame'], selector, '{"all":["__probe_a__","__probe_b__"]}')
    if len(details['label_boxes']) != 1:
        raise ComputerError('windows_visual_discovery_target_missing')
    return details


def discover(snapshot):
    binding = memory._bitmap(snapshot['frame'])
    from .windows_visual_selection import readback
    options = []
    for label in sorted(visual.LABELS):
        try:
            page=_page(snapshot,label)
        except ComputerError:
            continue
        selected=readback(snapshot['frame'],page['navigation_row'])
        if len(selected['selected_labels'])!=1 or len(selected['content_labels'])<2:
            continue
        options.append({'window':deepcopy(snapshot['window']), 'label':label, 'expected':[],
                        'source':{'kind':'fresh_selection','snapshot':deepcopy(snapshot),'binding':binding}})
    return options


def verify(item, *, pending=False):
    if type(item) is not dict or set(item) != {'window','label','expected','source'}:
        raise ComputerError('windows_visual_discovery_invalid')
    source = item['source']
    if type(source) is not dict or set(source) != {'kind','snapshot','binding'} or source['kind'] not in {'fresh_frame','fresh_selection'}:
        raise ComputerError('windows_visual_discovery_invalid')
    snapshot = source['snapshot']
    details = _page(snapshot,item['label'])
    if (item['window'] != snapshot['window']
            or memory._bitmap(snapshot['frame']) != source['binding']):
        raise ComputerError('windows_visual_discovery_changed')
    if source['kind']=='fresh_selection':
        from .windows_visual_selection import readback
        selection=readback(snapshot['frame'],details['navigation_row'])
        if item['expected']!=[] or len(selection['selected_labels'])!=1 or len(selection['content_labels'])<2:
            raise ComputerError('windows_visual_selection_unavailable')
        visual.selection_condition(item['window'],item['label'])
        return item
    if pending and item['expected'] == []:
        return item
    visual.condition(item['window'],item['label'],item['expected'])
    # A prediction is not prior evidence. Existing content cannot serve as a
    # novel destination contract, even if the model chooses a different tab.
    box = details['label_boxes'][0]
    low = box[1]+box[3]+6
    if any(any(low <= b[1] and b[1]+b[3] <= min(180,low+110)
               for b in visual.spans(snapshot['frame'],word)) for word in item['expected']):
        raise ComputerError('windows_visual_prediction_already_visible')
    return item
