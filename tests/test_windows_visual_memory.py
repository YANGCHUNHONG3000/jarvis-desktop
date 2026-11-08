"""Native-history provenance discovery, without UI/OCR/network calls."""
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import struct

import pytest

from agent_service.computer import ComputerError, digest, write_new
from agent_service import windows_access as access
from agent_service import windows_task as task
from agent_service import windows_visual as visual
from agent_service import windows_visual_recovery as recovery
from agent_service import windows_visual_memory as memory


WINDOW = {'hwnd': 100, 'pid': 200, 'created': 'offline-start', 'image': 'C:/wps.exe',
          'class_name': 'OpusApp', 'title': 'Offline workbook', 'profile': 'observe_only'}


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, 'OBSERVATION_BASE', tmp_path / 'observations')
    return tmp_path


def observation(condition, met):
    label = condition['selector']['name']
    expected = json.loads(condition['value'])['all']
    names = [label, *[x for x in ('开始', '插入', '视图', '数据') if x != label]][:3]
    words = [{'text': text, 'box': [100+i*80, 50, 28, 16]} for i, text in enumerate(names)]
    if met:
        words += [{'text': text, 'box': [100+i*150, 100, 80, 16]} for i, text in enumerate(expected)]
    width, height = 900, 240
    color = sha256((label+str(expected)+str(met)).encode()).digest()[:4]
    pixels = color * (width*height)
    raw = struct.pack('<2sIHHI', b'BM', 54+len(pixels), 0, 0, 54)
    raw += struct.pack('<IiiHHIIiiII', 40, width, -height, 1, 32, 0, len(pixels), 0, 0, 0, 0)+pixels
    key = sha256(raw).hexdigest()
    path = memory.OBSERVATION_BASE / 'ocr-cache/test-v1' / (key+'.bmp')
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(raw)
    frame = {'schema': 1, 'sha256': key, 'width': width, 'height': height,
             'recipe': 'test-v1', 'words': words}
    control, details = visual.presentation(frame, condition['selector'], condition['value'])
    details['rect'] = [0, 0, width, 800]
    value = {'window': deepcopy(condition['window']), 'application': 'wps', 'controls': [control],
             'scope': 'visual_navigation', 'dialogs': [], 'truncated': False, 'visual': details}
    # Reproduce the native worker's JSON boundary, including row tuple conversion.
    return json.loads(json.dumps(value))


class NativeHistory:
    def __init__(self, conditions, fail_last=False):
        self.conditions = conditions
        self.met = {c['id']: False for c in conditions}
        self.fail_last = fail_last

    def observe_condition(self, condition, *, stop):
        return observation(condition, self.met[condition['id']])

    candidates = staticmethod(visual.candidates)

    def act(self, window, control, action, value, before_sha, operation_root, *, stop):
        condition = next(c for c in self.conditions if c['selector']['name'] == control['name'])
        failed = self.fail_last and condition is self.conditions[-1]
        self.met[condition['id']] = not failed
        result = {'ok': not failed, 'state': 'unknown' if failed else 'verified',
                  'effects': 'unknown' if failed else 'verified',
                  'observation': observation(condition, not failed)}
        if failed:
            result['code'] = 'windows_visual_postcondition_failed'
        request = {'op': 'act', 'window': window, 'control': control, 'action': action,
                   'value': value, 'before_sha': before_sha, 'selector': condition['selector'], 'input_epoch': 10}
        write_new(operation_root/'native-request.json', request)
        write_new(operation_root/'native-intent.json', {'request_sha': digest(request)})
        write_new(operation_root/'native-outcome.json', {**result, 'input_epoch': 11})
        return result


def history(base, *, label='开始', expected=('数字格式', '样式'), window=None, unknown=False):
    window = deepcopy(window or WINDOW)
    conditions = [visual.condition(window, label, list(expected), number=1)]
    if unknown:
        conditions.append(visual.condition(window, '视图', ['冻结窗格', '重排窗口'], number=2))
    root = task.create(base/'tasks', {'schema': 1, 'instruction': 'Offline history',
                       'conditions': conditions, 'max_steps': 12})
    result = task.run(root, NativeHistory(conditions, fail_last=unknown))
    assert result['state'] == ('unknown' if unknown else 'verified')
    return root, conditions


def discover(base, **kwargs):
    return memory.discover([WINDOW], task_base=base/'tasks', recheck_base=base/'rechecks', **kwargs)


def snapshot(root):
    return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in root.rglob('*') if p.is_file()}


def test_completed_step_export_does_not_upgrade_unknown_parent(environment):
    base = environment
    original, _ = history(base, unknown=True)
    before = snapshot(original)
    candidates = discover(base)
    assert [(c['label'], c['expected']) for c in candidates] == [('开始', ['数字格式', '样式'])]
    assert candidates[0]['source']['kind'] == 'completed_visual_step'
    assert memory.verify(candidates[0]) is None
    assert task.inspect(original)['state'] == 'unknown'
    assert snapshot(original) == before


@pytest.mark.parametrize('key,value', [('hwnd', 101), ('pid', 201), ('created', 'new-start'),
                                     ('image', 'C:/other.exe'), ('class_name', 'Other'),
                                     ('profile', 'other'), ('title', 'Other workbook')])
def test_window_identity_and_title_cannot_migrate(environment, key, value):
    base = environment
    history(base)
    report = memory.discover_report([{**WINDOW, key: value}], task_base=base/'tasks', recheck_base=base/'rechecks')
    assert report['candidates'] == [] and report['empty_reason'] == 'no_verified_same_window_history'


def test_duplicate_exact_contract_is_deduplicated(environment):
    base = environment
    history(base)
    history(base)
    assert len(discover(base)) == 1


def test_same_label_different_verified_contracts_are_not_arbitrarily_selected(environment):
    base = environment
    history(base)
    history(base, expected=('字体', '对齐方式'))
    report = memory.discover_report([WINDOW], task_base=base/'tasks', recheck_base=base/'rechecks')
    assert not report['candidates'] and report['empty_reason'] == 'ambiguous_visual_history'
    assert any(d['code'] == 'windows_visual_memory_contract_ambiguous' for d in report['diagnostics'])


def test_selected_mapping_and_entire_source_inventory_are_mandatory(environment):
    base = environment
    original, _ = history(base)
    candidate = discover(base)[0]
    forged = deepcopy(candidate)
    forged['expected'] = ['字体', '对齐方式']
    with pytest.raises(ComputerError, match='source_changed'):
        memory.verify(forged)
    (original/'added.json').write_text('{}')
    with pytest.raises(ComputerError, match='source_changed'):
        memory.verify(candidate)


def test_source_bitmap_is_verified_not_a_free_standing_ocr_cache_entry(environment):
    base = environment
    history(base)
    candidate = discover(base)[0]
    path = Path(candidate['source']['bitmap'])
    path.write_bytes(path.read_bytes()+b'changed')
    assert discover(base) == []
    with pytest.raises(ComputerError, match='source_changed'):
        memory.verify(candidate)


def test_bitmap_dimensions_must_match_the_bound_frame(environment):
    condition = visual.condition(WINDOW, '开始', ['数字格式', '样式'])
    frame = observation(condition, True)['visual']['frame']
    frame['width'] = 901
    with pytest.raises(ComputerError, match='bitmap_invalid'):
        memory._bitmap(frame)


def test_schema3_and_nonvisual_manifests_are_skipped_before_scan(environment, monkeypatch):
    base = environment
    for name, schema, conditions in [('new-plan', 3, [visual.condition(WINDOW, '开始', ['数字格式', '样式'])]),
                                      ('nonvisual', 1, [])]:
        folder = base/'tasks'/name
        folder.mkdir(parents=True)
        task._save(folder/'task.json', {'schema': schema, 'goal': {'conditions': conditions}})
    monkeypatch.setattr(task, '_scan', lambda root: pytest.fail('manifest should have been skipped'))
    report = memory.discover_report([WINDOW], task_base=base/'tasks', recheck_base=base/'rechecks')
    assert report['candidates'] == [] and len(report['diagnostics']) == 2


def test_recent_limit_is_applied_before_evidence_scan(environment):
    base = environment
    old, _ = history(base)
    recent, _ = history(base, label='视图', expected=('冻结窗格', '重排窗口'))
    os.utime(old, ns=(1_000_000_000, 1_000_000_000))
    os.utime(recent, ns=(2_000_000_000, 2_000_000_000))
    candidates = discover(base, limit=1)
    assert len(candidates) == 1 and candidates[0]['label'] == '视图'


def test_native_recheck_only_and_original_attempts_remain_unknown(environment, monkeypatch):
    base = environment
    original, conditions = history(base, unknown=True)
    before = snapshot(original)
    class FreshWorker:
        def __init__(self, parent):
            self.parent = parent
        def observe_condition(self, condition, *, stop):
            op = self.parent/('observe-'+'0'*32)
            op.mkdir(parents=True)
            raw = observation(condition, True)
            write_new(op/'native-request.json', {'op': 'observe', 'window': condition['window'],
                      'selector': condition['selector'], 'document_token': condition['value']})
            write_new(op/'native-outcome.json', {'ok': True, 'observation': raw})
            return raw
    monkeypatch.setattr(access, 'WindowsBackend', FreshWorker)
    receipt = recovery.run(original, parent=base/'rechecks')
    assert receipt['ok'] and receipt['native_required']
    candidates = discover(base)
    assert {c['label'] for c in candidates} == {'开始', '视图'}
    selected = next(c for c in candidates if c['label'] == '视图')
    assert selected['source']['kind'] == 'native_visual_recheck'
    memory.verify(selected)
    assert snapshot(original) == before and task.inspect(original)['state'] == 'unknown'


def test_earlier_recheck_contract_without_native_required_cannot_supply_candidates(environment):
    base = environment
    original, _ = history(base, unknown=True)
    class TestBackend:
        def observe_condition(self, condition, *, stop):
            return observation(condition, True)
    receipt = recovery.run(original, TestBackend(), parent=base/'rechecks')
    assert receipt['ok'] and not receipt['native_required']
    assert [c['label'] for c in discover(base)] == ['开始']


def test_no_window_and_no_history_have_clear_reasons(environment):
    base = environment
    assert memory.discover_report([], task_base=base/'tasks', recheck_base=base/'rechecks')['empty_reason'] == 'no_selected_windows'
    assert memory.discover_report([WINDOW], task_base=base/'tasks', recheck_base=base/'rechecks')['empty_reason'] == 'no_verified_same_window_history'


def test_recheck_of_schema3_is_filtered_before_recursive_source_loading(environment, monkeypatch):
    base = environment
    origin = base/'schema3-source'
    origin.mkdir()
    task._save(origin/'task.json', {'schema': 3})
    recheck = base/'rechecks'/'new-plan-readback'
    recheck.mkdir(parents=True)
    task._save(recheck/'receipt.json', {'source': {'task_root': str(origin)}})
    monkeypatch.setattr(recovery, 'load', lambda root: pytest.fail('recursive schema3 source was loaded'))
    report = memory.discover_report([WINDOW], task_base=base/'tasks', recheck_base=base/'rechecks')
    assert report['candidates'] == []
    assert report['diagnostics'][0]['code'] == 'windows_visual_memory_schema_skipped'


def test_output_is_bounded_to_eight_verified_contracts(environment):
    base = environment
    for index, label in enumerate(('开始', '插入', '页面', '公式', '数据', '审阅', '视图', '工具', '帮助')):
        history(base, label=label, expected=(f'功能{index}甲', f'功能{index}乙'))
    candidates = discover(base)
    assert len(candidates) == 8
    assert len({c['label'] for c in candidates}) == 8
