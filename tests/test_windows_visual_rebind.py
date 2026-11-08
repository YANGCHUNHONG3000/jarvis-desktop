from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_service.computer import ComputerError, digest, write_new
from agent_service import windows_task as task, windows_visual as visual
from agent_service import windows_visual_proposal as proposal, windows_visual_memory as memory
from agent_service import windows_visual_rebind as rebind, windows_visual_binding as binding
from agent_service import windows_visual_continuation as registry


def frame():
    return {'schema': 1, 'sha256': 'a' * 64, 'width': 900, 'height': 240, 'words': [
        {'text': word, 'box': [x, 50, 45, 16]} for word, x in [('开始', 100), ('插入', 160), ('视图', 220)]] + [
        {'text': '冻结窗格', 'box': [300, 100, 60, 16]},
        {'text': '重排窗口', 'box': [400, 100, 60, 16]}]}


def observation(window):
    return {'window': deepcopy(window), 'application': 'wps', 'controls': [],
            'scope': 'visible_controls', 'truncated': False, 'dialogs': []}


def attested(document, window, root):
    root.mkdir(parents=True)
    write_new(root / 'native-request.json', {'op': 'visual_document_identity', 'window': window})
    write_new(root / 'native-outcome.json', {'ok': True, 'document': binding.identity(document)})
    return {**binding.identity(document), 'native_proof': {'root': str(root),
        'request_sha256': sha256((root / 'native-request.json').read_bytes()).hexdigest(),
        'outcome_sha256': sha256((root / 'native-outcome.json').read_bytes()).hexdigest()}}


class Backend:
    def __init__(self, old, new, document):
        self.old, self.new, self.document = old, new, document
        self.windows = [new]
        self.present = False
        self.actions = 0
        self.document_reads = 0
        self.observations = 0
        self.popup = False

    def window_presence(self, window, *, stop):
        assert window == self.old
        return self.present

    def list_windows(self, *, stop):
        return deepcopy(self.windows)

    def visual_document_identity(self, window, *, stop):
        self.document_reads += 1
        base = Path(self.document['native_proof']['root']).parent
        return attested(self.document, window, base / f'observe-{self.document_reads}')

    def observe(self, window, *, stop):
        self.observations += 1
        return {**observation(window), **({'blocked_reason': 'windows_application_blocked'} if self.popup else {})}

    def visual_snapshot(self, window, *, stop):
        return {'window': deepcopy(window), 'application': 'wps', 'frame': frame(), 'rect': [0, 0, 900, 600]}

    def observe_condition(self, condition, *, stop):
        control, details = visual.presentation(frame(), condition['selector'], condition['value'])
        return {**observation(condition['window']), 'scope': 'visual_navigation',
                'controls': [control], 'visual': details}

    def act(self, *args, **kwargs):
        self.actions += 1
        pytest.fail('completed parent step must never be dispatched')

    candidates = staticmethod(visual.candidates)


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, 'BASE', tmp_path / 'registry')
    monkeypatch.setattr(memory, 'verify', lambda item: None)
    monkeypatch.setattr(memory, '_bitmap', lambda image: {'frame_sha256': digest(image)})
    path = tmp_path / 'exact.xlsx'
    path.write_bytes(b'unchanged original document')
    image = tmp_path / 'wps.exe'
    image.write_bytes(b'installed app')
    old = {'hwnd': 100, 'pid': 200, 'created': 'old-process', 'image': str(image.resolve()),
           'class_name': 'XLMAIN', 'title': 'same title', 'profile': 'observe_only'}
    new = {**old, 'hwnd': 101, 'pid': 201, 'created': 'new-process'}
    document = {'schema': 1, **binding._file(path), 'format': 'xlsx', 'application': 'wps',
                'registered_app': str(image.resolve()), 'read_only': False, 'saved': True}
    document = attested(document, old, tmp_path / 'native' / 'original')
    conditions = [visual.condition(old, '视图', ['冻结窗格', '重排窗口'], number=1),
                  visual.condition(old, '开始', ['数字格式', '样式'], number=2)]
    goal = {'schema': 1, 'instruction': '查看显示工具后回到编辑工具', 'conditions': conditions, 'max_steps': 12}
    candidates = [{'window': deepcopy(old), 'label': c['selector']['name'],
                   'expected': visual.decode(c['selector'], c['value'])['all'], 'source': {'kind': 'offline'}}
                  for c in conditions]
    reference = proposal.save(goal, candidates, [observation(old)], parent=tmp_path / 'proposals',
                              document_bindings=[{'window': old, 'document': document}])
    root = task.create(tmp_path / 'tasks', goal, visual_proposal=reference)
    proposal.approve(root)
    backend = Backend(old, new, document)
    result = task.run(root, backend, pause_after_step=True)
    assert result['completed'] == ['target_1'] and result['state'] == 'paused'
    return root, reference, backend


def snapshot_files(root):
    return {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in root.rglob('*') if p.is_file()}


def test_prepare_only_remaining_keeps_original_and_requires_new_approval(prepared, tmp_path):
    root, _, backend = prepared
    original = snapshot_files(root)
    result = rebind.prepare(root, backend)
    assert result['goal']['conditions'] == [visual.condition(backend.new, '开始', ['数字格式', '样式'])]
    assert result['continuation']['completed'] == ['target_1']
    assert result['continuation']['conditions'] == [{'old_id': 'target_2', 'new_id': 'target_1'}]
    assert backend.actions == 0 and snapshot_files(root) == original
    reference = proposal.save(result['goal'], result['candidates'], result['observations'],
                              document_bindings=result['document_bindings'], continuation=result['continuation'],
                              parent=tmp_path / 'child-proposals')
    child = task.create(tmp_path / 'child-tasks', result['goal'], visual_proposal=reference)
    assert task.load(child)['summary']['approval_required']
    with pytest.raises(ComputerError, match='approval_required'):
        task.run(child, backend)
    assert backend.actions == 0 and snapshot_files(root) == original


@pytest.mark.parametrize('present', [True, 1, None, 'missing'])
def test_old_window_presence_or_invalid_existence_never_migrates(prepared, present):
    root, _, backend = prepared
    backend.present = present
    with pytest.raises(ComputerError, match='old_window_present'):
        rebind.prepare(root, backend)
    assert backend.document_reads == backend.observations == backend.actions == 0


@pytest.mark.parametrize('count', [0, 2])
def test_replacement_must_be_unique(prepared, count):
    root, _, backend = prepared
    backend.windows = [{**backend.new, 'hwnd': 101 + i} for i in range(count)]
    with pytest.raises(ComputerError, match='window_not_unique'):
        rebind.prepare(root, backend)
    assert backend.actions == 0


@pytest.mark.parametrize('change', [{'registered_app': '/different/app.exe'}, {'read_only': True}])
def test_native_document_identity_must_be_exact(prepared, change):
    root, _, backend = prepared
    backend.document.update(change)
    with pytest.raises(ComputerError):
        rebind.prepare(root, backend)
    assert backend.actions == 0


def test_same_title_different_full_path_rejected(prepared, tmp_path):
    root, _, backend = prepared
    other = tmp_path / 'other.xlsx'
    other.write_bytes(Path(backend.document['path']).read_bytes())
    backend.document.update(binding._file(other))
    with pytest.raises(ComputerError, match='window_not_unique'):
        rebind.prepare(root, backend)


@pytest.mark.parametrize('field,value', [('image', 'C:/unregistered/wps.exe'), ('class_name', 'OpusApp'), ('profile', 'notepad'), ('title', 'renamed document')])
def test_application_identity_not_title_selects_candidate(prepared, field, value):
    root, _, backend = prepared
    backend.windows = [{**backend.new, field: value}]
    with pytest.raises(ComputerError, match='window_not_unique'):
        rebind.prepare(root, backend)
    assert backend.document_reads == backend.actions == 0


def test_popup_blocks_preparation(prepared):
    root, _, backend = prepared
    backend.popup = True
    with pytest.raises(ComputerError, match='application_blocked'):
        rebind.prepare(root, backend)


def test_parent_append_invalidates_saved_continuation(prepared):
    root, _, backend = prepared
    result = rebind.prepare(root, backend)
    (root / 'external-extra.json').write_text('{}')
    with pytest.raises(ComputerError, match='parent_changed'):
        rebind.verify_continuation(result, result['goal'])


def test_current_document_bytes_change_invalidates_without_ui(prepared):
    root, _, backend = prepared
    result = rebind.prepare(root, backend)
    observed = backend.observations
    Path(backend.document['path']).write_bytes(b'changed')
    with pytest.raises(ComputerError, match='document_changed'):
        rebind.verify_candidate(result['candidates'][0])
    assert backend.observations == observed


@pytest.mark.parametrize('change', ['completed', 'old_id', 'new_id', 'value', 'expected', 'snapshot'])
def test_migration_cannot_change_history_mapping_or_contract(prepared, change):
    root, _, backend = prepared
    result = rebind.prepare(root, backend)
    if change == 'completed':
        result['continuation']['completed'] = []
    elif change in {'old_id', 'new_id'}:
        result['continuation']['conditions'][0][change] = 'invented'
    elif change == 'value':
        result['goal']['conditions'][0]['value'] = '{"all":["invented","another"]}'
    elif change == 'expected':
        result['candidates'][0]['expected'] = ['invented', 'another']
    else:
        result['candidates'][0]['source']['snapshot']['frame']['sha256'] = 'b' * 64
    with pytest.raises(ComputerError):
        rebind.verify_continuation(result, result['goal'])


def test_execution_preflight_rechecks_disappearance_uniqueness_and_document(prepared):
    root, _, backend = prepared
    result = rebind.prepare(root, backend)
    payload = {**result, 'goal': result['goal']}
    assert rebind.preflight(payload, backend)
    backend.present = True
    with pytest.raises(ComputerError, match='old_window_present'):
        rebind.preflight(payload, backend)
    backend.present = False
    backend.windows[0]['created'] = 'reused-process'
    with pytest.raises(ComputerError, match='window_changed'):
        rebind.preflight(payload, backend)


def test_missing_original_document_binding_is_not_reconstructed(prepared):
    root, reference, backend = prepared
    path = Path(reference['root']) / 'proposal.json'
    payload, _ = task._load(path)
    payload.pop('document_bindings')
    path.unlink()
    checksum = task._save(path, payload)
    manifest, _ = task._load(root / 'task.json')
    manifest['visual_proposal']['sha256'] = checksum
    # Call through an isolated manifest so the existing task and events stay intact.
    fresh = task.create(root.parent / 'fresh', manifest['goal'], visual_proposal=manifest['visual_proposal'])
    with pytest.raises(ComputerError, match='document_required'):
        rebind.prepare(fresh, backend)
    assert backend.document_reads == backend.actions == 0


def test_migration_generation_cannot_chain(prepared, tmp_path):
    root, _, backend = prepared
    result = rebind.prepare(root, backend)
    reference = proposal.save(result['goal'], result['candidates'], result['observations'],
                              document_bindings=result['document_bindings'], continuation=result['continuation'],
                              parent=tmp_path / 'child-proposals')
    child = task.create(tmp_path / 'child-tasks', result['goal'], visual_proposal=reference)
    with pytest.raises(ComputerError, match='generation_limit'):
        rebind.prepare(child, backend)


def test_cancel_before_any_native_probe(prepared):
    root, _, backend = prepared
    with pytest.raises(ComputerError, match='cancelled'):
        rebind.prepare(root, backend, stop=lambda: True)
    assert backend.document_reads == backend.observations == 0


def test_unknown_intent_cannot_migrate_or_replay(prepared):
    root, _, backend = prepared
    backend.act = lambda *args, **kwargs: {'ok': False, 'state': 'unknown', 'effects': 'unknown', 'code': 'interrupted'}
    assert task.run(root, backend)['state'] == 'unknown'
    evidence = snapshot_files(root)
    with pytest.raises(ComputerError, match='ineligible'):
        rebind.prepare(root, backend)
    assert snapshot_files(root) == evidence


def test_verified_parent_cannot_migrate(prepared):
    root, _, backend = prepared
    def satisfied(condition, *, stop):
        image = frame()
        words = visual.decode(condition['selector'], condition['value'])['all']
        image['words'][-2]['text'], image['words'][-1]['text'] = words
        control, details = visual.presentation(image, condition['selector'], condition['value'])
        return {**observation(condition['window']), 'scope': 'visual_navigation',
                'controls': [control], 'visual': details}
    backend.observe_condition = satisfied
    assert task.run(root, backend)['state'] == 'verified'
    with pytest.raises(ComputerError, match='ineligible'):
        rebind.prepare(root, backend)


def test_remaining_action_budget_is_not_reset(prepared, monkeypatch):
    root, _, backend = prepared
    original = rebind._parent
    def scanned(parent):
        scan, payload, inventory = original(parent)
        scan['actions'] = 5
        return scan, payload, inventory
    monkeypatch.setattr(rebind, '_parent', scanned)
    result = rebind.prepare(root, backend)
    assert result['goal']['max_steps'] == 7


def test_unreadable_possible_replacement_cannot_be_skipped(prepared):
    root, _, backend = prepared
    def unreadable(*args, **kwargs):
        raise ComputerError('windows_document_rot_unreadable')
    backend.visual_document_identity = unreadable
    with pytest.raises(ComputerError, match='rot_unreadable'):
        rebind.prepare(root, backend)
    assert backend.actions == 0


@pytest.mark.parametrize('suffix', ['docx', 'xlsx'])
def test_native_binding_reads_actual_active_fullname_twice(tmp_path, monkeypatch, suffix):
    from agent_service import computer_uia as uia, windows_access as access
    from agent_service import windows_file_dialog as files, windows_workbook_binding as books
    path = tmp_path / ('actual.' + suffix)
    path.write_bytes(b'actual document')
    image = tmp_path / 'wps.exe'
    window = {'hwnd': 10, 'pid': 20, 'created': 'process', 'image': str(image.resolve()),
              'class_name': 'OpusApp', 'profile': 'observe_only', 'title': 'misleading title.xlsx'}
    monkeypatch.setattr(uia, '_recheck', lambda selected: deepcopy(window))
    monkeypatch.setattr(access, '_kind', lambda selected: 'wps')
    monkeypatch.setattr(access, '_observe', lambda selected, check: (observation(selected), None))
    app = SimpleNamespace(**{('ActiveDocument' if suffix == 'docx' else 'ActiveWorkbook'): SimpleNamespace(FullName=str(path))})
    def unavailable(*args):
        raise ComputerError('windows_document_anchor_unavailable')
    monkeypatch.setattr(files, '_document_application', (lambda *args: (None, app)) if suffix == 'docx' else unavailable)
    def no_workbook(*args):
        raise ComputerError('windows_workbook_anchor_unavailable')
    monkeypatch.setattr(books, '_workbook_application', (lambda *args: (None, app, path)) if suffix == 'xlsx' else no_workbook)
    reads = []
    def status(selected, approved, check):
        reads.append(str(approved))
        return {'active': True, 'saved': True, 'read_only': False}
    monkeypatch.setattr(files, 'document_status', status)
    result = binding.capture(window, lambda: None)
    assert result['path'] == str(path.resolve()) and result['format'] == suffix
    assert reads == [str(path.resolve()), str(path.resolve())]


def test_backend_window_presence_does_not_use_inventory(monkeypatch):
    from agent_service.windows_access import WindowsBackend
    backend = WindowsBackend()
    monkeypatch.setattr(backend, '_call', lambda request, **kwargs: {'ok': True, 'exists': True})
    assert backend.window_presence({'hwnd': 10}) is True
    monkeypatch.setattr(backend, '_call', lambda request, **kwargs: {'ok': True, 'exists': 0})
    with pytest.raises(ComputerError, match='presence_invalid'):
        backend.window_presence({'hwnd': 10})


def test_document_equivalence_ignores_only_native_observation_reference(prepared):
    _, _, backend = prepared
    fresh = backend.visual_document_identity(backend.new, stop=lambda: False)
    assert fresh != backend.document and binding.same_document(fresh, backend.document)
    binding.verify(fresh, window=backend.new)
    with pytest.raises(ComputerError, match='proof_window_changed'):
        binding.verify(fresh, window=backend.old)


@pytest.mark.parametrize('part', ['request', 'outcome'])
def test_original_native_document_proof_cannot_change(prepared, part):
    root, _, backend = prepared
    proof = backend.document['native_proof']
    path = Path(proof['root']) / f'native-{part}.json'
    path.write_text('{}')
    with pytest.raises(ComputerError, match='proof_changed'):
        rebind.prepare(root, backend)
    assert backend.document_reads == 0


def test_original_proof_is_required_not_reconstructed(prepared):
    _, _, backend = prepared
    unproven = binding.identity(backend.document)
    with pytest.raises(ComputerError, match='proof_required'):
        binding.verify(unproven, window=backend.old)


def test_candidate_native_proof_must_match_its_new_window(prepared):
    root, _, backend = prepared
    result = rebind.prepare(root, backend)
    result['document_bindings'][0]['document'] = deepcopy(backend.document)
    with pytest.raises(ComputerError, match='proof_window_changed'):
        rebind.verify_continuation(result, result['goal'])


def test_incomplete_window_inventory_cannot_prove_uniqueness(prepared):
    root, _, backend = prepared
    backend.windows = [backend.new] * 200
    with pytest.raises(ComputerError, match='inventory_incomplete'):
        rebind.prepare(root, backend)


def restore(root, backend, tmp_path):
    return proposal.restore_task(root, backend, task_base=tmp_path / 'migration-tasks',
                                 proposal_base=tmp_path / 'migration-proposals')


def test_repeated_parent_restore_returns_one_unapproved_child_without_actions(prepared, tmp_path):
    parent, _, backend = prepared
    original = snapshot_files(parent)
    first = restore(parent, backend, tmp_path)
    reads = backend.document_reads, backend.observations
    second = restore(parent, backend, tmp_path)
    assert first['root'] == second['root'] != parent
    assert first['summary']['approval_required'] and second['summary']['approval_required']
    assert list((tmp_path / 'migration-tasks').iterdir()) == [first['root']]
    assert registry.existing(parent) == first['root']
    assert reads == (backend.document_reads, backend.observations)
    with pytest.raises(ComputerError, match='approval_required'):
        task.run(first['root'], backend)
    assert backend.actions == 0 and snapshot_files(parent) == original


def test_unknown_child_restore_does_not_create_or_replay_sibling(prepared, tmp_path):
    parent, _, backend = prepared
    child = restore(parent, backend, tmp_path)['root']
    proposal.approve(child)
    calls = []
    def unknown(*args, **kwargs):
        calls.append(args)
        return {'ok': False, 'state': 'unknown', 'effects': 'unknown', 'code': 'interrupted'}
    backend.act = unknown
    assert task.run(child, backend)['state'] == 'unknown' and len(calls) == 1
    original = snapshot_files(parent)
    failed_child = snapshot_files(child)
    restored = restore(parent, backend, tmp_path)
    assert restored['root'] == child and restored['summary']['state'] == 'unknown'
    assert restored['summary']['resumable'] is False
    with pytest.raises(ComputerError, match='unknown_requires_review'):
        task.run(restored['root'], backend)
    assert len(calls) == 1 and snapshot_files(parent) == original and snapshot_files(child) == failed_child
    assert list((tmp_path / 'migration-tasks').iterdir()) == [child]


def test_parent_run_cannot_replay_after_child_publication(prepared, tmp_path):
    parent, _, backend = prepared
    restore(parent, backend, tmp_path)
    original = snapshot_files(parent)
    reads = backend.document_reads, backend.observations
    with pytest.raises(ComputerError, match='continuation_exists'):
        task.run(parent, backend)
    assert snapshot_files(parent) == original and reads == (backend.document_reads, backend.observations)
    assert backend.actions == 0


def test_same_proposal_in_cloned_task_is_not_a_canonical_child(prepared, tmp_path):
    parent, _, backend = prepared
    child = restore(parent, backend, tmp_path)['root']
    manifest, _ = task._load(child / 'task.json')
    clone = task.create(tmp_path / 'clones', manifest['goal'], visual_proposal=manifest['visual_proposal'])
    proposal.approve(clone)
    reads = backend.document_reads, backend.observations
    with pytest.raises(ComputerError, match='superseded'):
        task.run(clone, backend)
    assert reads == (backend.document_reads, backend.observations) and backend.actions == 0


def test_publication_interruption_preserves_nonexecutable_orphan(prepared, tmp_path, monkeypatch):
    parent, _, backend = prepared
    save = task._save
    def interrupted(path, payload):
        if Path(path).name == 'link.json':
            raise OSError('publication interrupted')
        return save(path, payload)
    monkeypatch.setattr(task, '_save', interrupted)
    with pytest.raises(OSError, match='publication interrupted'):
        restore(parent, backend, tmp_path)
    orphan, = (tmp_path / 'migration-tasks').iterdir()
    original = snapshot_files(orphan)
    proposal.approve(orphan)
    with pytest.raises(ComputerError, match='unpublished'):
        task.run(orphan, backend)
    assert registry.existing(parent) is None and backend.actions == 0
    monkeypatch.setattr(task, '_save', save)
    canonical = restore(parent, backend, tmp_path)['root']
    assert canonical != orphan and registry.existing(parent) == canonical
    assert {p: data for p, data in snapshot_files(orphan).items() if not p.endswith('visual-approval.json')} == original
    with pytest.raises(ComputerError, match='superseded'):
        task.run(orphan, backend)


def test_concurrent_claim_rejects_second_builder_then_reuses_published_child(prepared, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    parent, _, backend = prepared
    prepared_result = rebind.prepare(parent, backend)
    reference = proposal.save(prepared_result['goal'], prepared_result['candidates'], prepared_result['observations'],
                              document_bindings=prepared_result['document_bindings'], continuation=prepared_result['continuation'],
                              parent=tmp_path / 'concurrent-proposals')
    child = task.create(tmp_path / 'concurrent-tasks', prepared_result['goal'], visual_proposal=reference)
    entered, release = Event(), Event()
    calls = []
    def first_builder():
        calls.append('first')
        entered.set()
        assert release.wait(10)
        return child
    def second_builder():
        calls.append('second')
        pytest.fail('a concurrent claim must not enter a second builder')
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(registry.create_once, parent, first_builder)
        assert entered.wait(10)
        try:
            with pytest.raises(ComputerError, match='run_owner_active'):
                registry.create_once(parent, second_builder)
        finally:
            release.set()
        assert future.result(timeout=10) == child
    assert registry.create_once(parent, second_builder) == child and calls == ['first']


@pytest.fixture
def prepared_v2(prepared, tmp_path, monkeypatch):
    from agent_service import windows_visual_selection as selection
    _, _, backend = prepared
    monkeypatch.setattr(selection, 'readback', lambda image, row: {
        'selected_labels': ['视图'], 'content_labels': ['冻结窗格', '重排窗口'], 'rule': 'offline-v2'})
    snapshot = backend.visual_snapshot(backend.old, stop=lambda: False)
    candidates = [{'window': deepcopy(backend.old), 'label': label, 'expected': [],
                   'source': {'kind': 'fresh_selection', 'snapshot': snapshot,
                              'binding': memory._bitmap(snapshot['frame'])}} for label in ('视图', '开始')]
    goal = {'schema': 1, 'instruction': '先查看显示工具再回到编辑工具', 'max_steps': 12,
            'conditions': [visual.selection_condition(backend.old, label, number=i)
                           for i, label in enumerate(('视图', '开始'), 1)]}
    ref = proposal.save(goal, candidates, [observation(backend.old)], parent=tmp_path / 'v2-proposals',
                        document_bindings=[{'window': backend.old, 'document': backend.document}])
    root = task.create(tmp_path / 'v2-tasks', goal, visual_proposal=ref)
    proposal.approve(root)
    assert task.run(root, backend, pause_after_step=True)['completed'] == ['target_1']
    return root, backend


def test_v2_migration_keeps_selection_contract_and_no_predicted_terms(prepared_v2):
    root, backend = prepared_v2
    original = snapshot_files(root)
    result = rebind.prepare(root, backend)
    assert result['goal']['conditions'] == [visual.selection_condition(backend.new, '开始')]
    item, = result['candidates']
    assert item['expected'] == [] and item['source']['kind'] == 'rebind'
    assert item['source']['parent_candidate']['source']['kind'] == 'fresh_selection'
    assert proposal.candidate_condition(item) == visual.selection_condition(backend.new, '开始')
    rebind.verify_continuation(result, result['goal'])
    assert snapshot_files(root) == original and backend.actions == 0


def test_v2_repeated_restore_is_pending_and_does_not_replay_completed_step(prepared_v2, tmp_path):
    root, backend = prepared_v2
    child = restore(root, backend, tmp_path)
    again = restore(root, backend, tmp_path)
    assert child['root'] == again['root'] and child['summary']['approval_required']
    assert child['goal']['conditions'] == [visual.selection_condition(backend.new, '开始')]
    with pytest.raises(ComputerError, match='approval_required'):
        task.run(child['root'], backend)
    assert backend.actions == 0


def test_v2_goal_cannot_be_downgraded_to_expected_text(prepared_v2):
    root, backend = prepared_v2
    result = rebind.prepare(root, backend)
    result['goal']['conditions'] = [visual.condition(backend.new, '开始', ['数字格式', '样式'])]
    with pytest.raises(ComputerError, match='goal_changed'):
        rebind.verify_continuation(result, result['goal'])


@pytest.mark.parametrize('labels,terms', [([], ['甲项', '乙项']), (['视图', '开始'], ['甲项', '乙项']), (['视图'], ['甲项'])])
def test_v2_replacement_requires_supported_selection_and_actual_content(prepared_v2, monkeypatch, labels, terms):
    from agent_service import windows_visual_selection as selection
    root, backend = prepared_v2
    original = selection.readback
    # Preserve the original parent snapshot's selection proof. Only replacement
    # pixels represent the unsupported style under review in this fixture.
    old_snapshot = backend.visual_snapshot
    backend.visual_snapshot = lambda window, **kwargs: {
        **old_snapshot(window, **kwargs), 'frame': {**frame(), 'sha256': 'b' * 64}}
    monkeypatch.setattr(selection, 'readback', lambda image, row: (
        {'selected_labels': labels, 'content_labels': terms, 'rule': 'offline-v2'}
        if image['sha256'] == 'b' * 64 else original(image, row)))
    with pytest.raises(ComputerError, match='selection_unavailable'):
        rebind.prepare(root, backend)
    assert backend.actions == 0
