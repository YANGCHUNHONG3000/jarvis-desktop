"""Local, hash-bound Office file/range selection before any model call."""
from copy import deepcopy
import json
from pathlib import Path
import uuid

from .computer import ComputerError, digest, safe_root, write_new
from .office_edit import snapshot_edit_source, verify_edit_source

MAX_EDIT_FILES = 4
MAX_BATCH_FILES = 32
MAX_TARGETS = 120
MAX_CONTEXT_CHARS = 18_000


def entries(public):
    observed = public['observed']
    if public['kind'] == 'document':
        result = [{'at':{'paragraph_index':p['index']},'value':p['text'],'editable':p['editable']}
                  for p in observed['paragraphs']]
        result += [{'at':{key:cell[key] for key in ('table_index','row_index','column_index','paragraph_index')},
                    'value':cell['text'],'editable':cell['editable']}
                   for cell in observed.get('table_cells',[])]
        return result
    return [{'at':{'sheet':sheet['name'],'cell':cell['cell']},'value':cell['value'],'editable':cell['editable']}
            for sheet in observed['sheets'] for cell in sheet['cells']]


def inspect_files(session, files, *, artifact_refs=None, stop=lambda:False):
    from .office_handoff import verify_references
    if type(files) not in (list,tuple) or not 1 <= len(files) <= MAX_EDIT_FILES:
        raise ComputerError('office_scope_file_count_invalid')
    files = [str(Path(p).absolute()) for p in files]
    if len({Path(p).resolve() for p in files}) != len(files):
        raise ComputerError('office_scope_duplicate_file')
    if artifact_refs is not None:
        verify_references(files, artifact_refs)
    catalog = {'files':[], 'artifact_refs':deepcopy(artifact_refs)}
    snapshots = []
    for n,path in enumerate(files):
        if stop():
            raise ComputerError('office_scope_cancelled')
        root = safe_root(session.root/'edit-inputs'/uuid.uuid4().hex)
        root.mkdir(parents=True,exist_ok=False)
        public = snapshot_edit_source(path,root)
        if artifact_refs is not None and public['sha256'] != artifact_refs[n]['sha256']:
            raise ComputerError('office_scope_source_changed')
        snapshots.append((root,public))
        catalog['files'].append({'path':path,'sha256':public['sha256'],'kind':public['kind'],
                                 'entries':entries(public)})
    if sum(len(f['entries']) for f in catalog['files']) > 2000:
        raise ComputerError('office_scope_catalog_too_large')
    for root, public in snapshots:
        if verify_edit_source(root) != public:
            raise ComputerError('office_scope_source_changed')
    if artifact_refs is not None:
        verify_references(files,artifact_refs)
    if stop():
        raise ComputerError('office_scope_cancelled')
    folder = safe_root(session.root/'office-scopes'/uuid.uuid4().hex)
    folder.mkdir(parents=True,exist_ok=False)
    write_new(folder/'catalog.json',catalog)
    return catalog


def selection_from_indices(catalog, indices):
    if type(indices) not in (list,tuple) or not indices:
        raise ComputerError('请选择至少一个编辑范围')
    selected = set()
    for pair in indices:
        if (type(pair) not in (tuple,list) or len(pair)!=2 or any(type(v) is not int for v in pair)
                or not 0 <= pair[0] < len(catalog['files'])
                or not 0 <= pair[1] < len(catalog['files'][pair[0]]['entries'])):
            raise ComputerError('office_scope_selection_invalid')
        selected.add(tuple(pair))
    scope, files, refs, context, editable = [], [], [], [], 0
    for i,file in enumerate(catalog['files']):
        rows = [row for j,row in enumerate(file['entries']) if (i,j) in selected]
        if not rows:
            continue
        files.append(file['path'])
        scope.append({'path':file['path'],'sha256':file['sha256'], 'locations':[deepcopy(r['at']) for r in rows]})
        context.append(rows)
        editable += sum(r['editable'] is True for r in rows)
        if catalog['artifact_refs'] is not None:
            refs.append(deepcopy(catalog['artifact_refs'][i]))
    if not 1 <= len(files) <= MAX_EDIT_FILES or not 1 <= editable <= MAX_TARGETS:
        raise ComputerError('请选择1至4份文件、1至120个可编辑位置；公式等只读内容不能单独编辑')
    if len(json.dumps(context,ensure_ascii=False)) > MAX_CONTEXT_CHARS:
        raise ComputerError('选中内容过长，请缩小范围')
    return {'files':tuple(files),'artifact_refs':refs if catalog['artifact_refs'] is not None else None,'scope':scope}


def scoped_entries(public, path, selection):
    if (type(selection) is not dict or set(selection)!={'path','sha256','locations'}
            or selection['path']!=str(Path(path).absolute()) or selection['sha256']!=public['sha256']
            or type(selection['locations']) is not list or not selection['locations']):
        raise ComputerError('office_scope_source_changed')
    wanted = [digest(at) for at in selection['locations']]
    all_rows = entries(public)
    available = {digest(row['at']):row for row in all_rows}
    if len(set(wanted))!=len(wanted) or not set(wanted)<=set(available):
        raise ComputerError('office_scope_location_invalid')
    return [row for row in all_rows if digest(row['at']) in set(wanted)]
