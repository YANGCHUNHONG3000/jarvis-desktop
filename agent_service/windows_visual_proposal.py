"""Evidence-backed visual proposals; persistence is separate from local approval."""
from copy import deepcopy
from pathlib import Path
import time
import uuid

from .computer import ComputerError, digest, safe_root
from . import windows_task as task, windows_visual as visual

BASE = Path(__file__).resolve().parents[1]/'data/windows_visual_proposals'


def reviewed_goal(selector):
    from .windows_winapp_office import is_goal as office_goal
    from .windows_visual_forms import is_goal
    from .windows_reviewed_controls import is_goal as control_goal
    return visual.is_goal(selector) or is_goal(selector) or control_goal(selector) or office_goal(selector)


def verify_candidate(item, *, pending=False):
    kind=item.get('source',{}).get('kind') if type(item) is dict else None
    if kind=='winapp_office':
        from .windows_winapp_office import verify_candidate as verify
        return verify(item,pending=pending)
    if kind=='reviewed_control':
        from .windows_reviewed_controls import verify_candidate as verify
        return verify(item,pending=pending)
    if kind=='form':
        from .windows_visual_forms import verify_candidate as verify
        return verify(item,pending=pending)
    if kind in {'fresh_frame','fresh_selection'}:
        from .windows_visual_discovery import verify
        return verify(item,pending=pending)
    if kind=='rebind':
        from .windows_visual_rebind import verify_candidate as verify
    else:
        from .windows_visual_memory import verify
    return verify(item)


def candidate_condition(item, *, number=1):
    if item.get('source',{}).get('kind')=='winapp_office':
        from .windows_winapp_office import condition
        return condition(item,number=number)
    if item.get('source',{}).get('kind')=='reviewed_control':
        from .windows_reviewed_controls import condition
        return condition(item,number=number)
    if item.get('source',{}).get('kind')=='form':
        from .windows_visual_forms import condition
        return condition(item,number=number)
    source=item['source']
    while source.get('kind')=='rebind':source=source['parent_candidate']['source']
    if source.get('kind')=='fresh_selection':
        return visual.selection_condition(item['window'],item['label'],number=number)
    return visual.condition(item['window'],item['label'],item['expected'],number=number)


def _matches(item, condition):
    if item['window']!=condition['window'] or item['label']!=condition['selector']['name']:
        return False
    if item.get('source',{}).get('kind')=='form' and item['source']['role']=='search_text' and item['expected']==[]:
        return False
    if item.get('source',{}).get('kind')=='reviewed_control' and item['source']['role'] in {'directory','filename','search_text'} and item['expected']==[]:
        return False
    return candidate_condition(item,number=1) == {
        **condition,'id':'target_1'}


def save(goal, candidates, observations, *, model_call=None, parent=BASE,
         document_bindings=None, continuation=None):
    goal=task.validate_goal(goal)
    selected=[]
    for c in goal['conditions']:
        if not reviewed_goal(c['selector']):
            continue
        choices=[]
        for option in candidates:
            item=deepcopy(option)
            if (item.get('source',{}).get('kind')=='reviewed_control' and item['source']['role'] in {'directory','filename','search_text'}
                    and item['window']==c['window'] and item['label']==c['selector']['name']):
                from .windows_reviewed_controls import decode
                data,_=decode(c['selector'],c['value'])
                if item['source']['reference']==data['source']:item['expected']=[data['text']]
            if (item.get('source',{}).get('kind')=='form' and item['source']['role']=='search_text'
                    and item['window']==c['window'] and item['label']==c['selector']['name']):
                from .windows_visual_forms import decode
                data,_=decode(c['selector'],c['value'])
                if item['source']['reference']==data['source']:item['expected']=[data['text']]
            if (item.get('source',{}).get('kind')=='fresh_frame' and item['expected']==[]
                    and item['window']==c['window'] and item['label']==c['selector']['name']):
                item['expected']=visual.decode(c['selector'],c['value'])['all']
            if _matches(item,c):choices.append(item)
        if len(choices)!=1:
            raise ComputerError('windows_visual_proposal_source_missing')
        if choices[0] not in selected:
            selected.append(deepcopy(choices[0]))
    if not selected:
        raise ComputerError('windows_visual_proposal_source_missing')
    for item in selected:verify_candidate(item)
    root=safe_root(parent)/('proposal-'+uuid.uuid4().hex)
    root.mkdir(parents=True,exist_ok=False)
    payload={'schema':1,'goal':goal,'candidates':selected,'observations':deepcopy(observations),
             'created':time.time(),'model_call':deepcopy(model_call),'actions':[]}
    if document_bindings is not None:payload['document_bindings']=deepcopy(document_bindings)
    if continuation is not None:
        payload['continuation']=deepcopy(continuation)
        from .windows_visual_rebind import verify_continuation
        verify_continuation(payload,goal)
    checksum=task._save(root/'proposal.json',payload)
    return {'root':str(root),'sha256':checksum}


def verify(reference, goal):
    if type(reference) is not dict or set(reference)!={'root','sha256'}:
        raise ComputerError('windows_visual_proposal_changed')
    payload,checksum=task._load(safe_root(reference['root'])/'proposal.json')
    if (checksum!=reference['sha256'] or payload.get('schema')!=1
            or payload.get('goal')!=goal or payload.get('actions')!=[]
            or type(payload.get('candidates')) is not list or not 1<=len(payload['candidates'])<=8):
        raise ComputerError('windows_visual_proposal_changed')
    for item in payload['candidates']:verify_candidate(item)
    visual_goals=[c for c in goal['conditions'] if reviewed_goal(c['selector'])]
    if (not visual_goals or any(sum(_matches(i,c) for i in payload['candidates'])!=1 for c in visual_goals)
            or any(not any(_matches(i,c) for c in visual_goals) for i in payload['candidates'])):
        raise ComputerError('windows_visual_proposal_changed')
    if 'continuation' in payload:
        from .windows_visual_rebind import verify_continuation
        verify_continuation(payload,goal)
    return payload


def _approval(scan):
    return {'schema':1,'task_sha256':scan['task_sha256'],
            'proposal':scan['task']['visual_proposal'],'goal_sha256':digest(scan['goal']),
            'approved_via':'local_review_button'}


def approved(root, scan):
    if scan['task']['schema']!=3:return True
    path=safe_root(root)/'visual-approval.json'
    if not path.exists():return False
    payload,_=task._load(path)
    if payload!=_approval(scan):raise ComputerError('windows_visual_approval_changed')
    return True


def approve(root):
    """Only the explicit local execute/confirm callback calls this function."""
    root=safe_root(root)
    with task._own(root):
        scan=task._scan(root)
        if scan['task']['schema']!=3:return
        if scan['uncertain']:raise ComputerError('windows_unknown_requires_review')
        verify(scan['task']['visual_proposal'],scan['goal'])
        if not approved(root,scan):task._save(root/'visual-approval.json',_approval(scan))


def propose(instruction, observations, backend, client, *, stop=lambda:False,
            task_base=None, proposal_base=BASE, intent_mapper=None, task_create_kwargs=None):
    """Reobserve, select bounded candidates in one call, save without acting."""
    from .windows_visual_memory import discover
    from .windows_intent import propose as map_intent
    if type(observations) not in (list,tuple) or not 1<=len(observations)<=2:
        raise ComputerError('windows_intent_observation_invalid')
    fresh=[]
    for old in observations:
        if stop():raise ComputerError('windows_intent_cancelled')
        current=backend.observe(old['window'],stop=stop)
        if current.get('blocked_reason') or current.get('truncated'):
            raise ComputerError(current.get('blocked_reason') or 'windows_observation_truncated')
        fresh.append(current)
    tool_only = getattr(backend, 'provider_binding', None) is not None
    candidates=[] if tool_only else discover([o['window'] for o in fresh if 'form_snapshot' not in o and 'reviewed_snapshot' not in o])
    if getattr(backend,'allow_office_options',False):
        from .windows_winapp_office import candidates_for
        for observed in fresh:
            if observed['application']=='wps' and any(d.get('title') in {'查找','Find'} for d in observed['dialogs']):
                candidates.extend(candidates_for(backend.office_snapshot(observed['window'],stop=stop)))
    from .windows_visual_forms import candidates_for
    from .windows_reviewed_controls import candidates_for as control_candidates
    for observed in fresh:
        if 'form_snapshot' in observed:candidates.extend(candidates_for(observed['form_snapshot']))
        if 'reviewed_snapshot' in observed:candidates.extend(control_candidates(observed['reviewed_snapshot']))
    snapshot_method=None if tool_only else getattr(backend,'visual_snapshot',None)
    if snapshot_method is not None:
        from .windows_visual_discovery import discover as discover_fresh
        for observed in fresh:
            if ('form_snapshot' in observed or 'reviewed_snapshot' in observed
                    or any(c.get('source',{}).get('kind')=='winapp_office' and c['window']==observed['window'] for c in candidates)):continue
            # UIA navigation remains first choice. A text-only provider need
            # not support snapshots; production provides the guarded worker.
            if any(c.get('type') in {'TabItem','RadioButton'} for c in observed['controls']):continue
            snapshot=snapshot_method(observed['window'],stop=stop)
            for item in discover_fresh(snapshot):
                if not any(c['window']==item['window'] and c['label']==item['label'] for c in candidates):
                    candidates.append(item)
    mapper=map_intent if intent_mapper is None else intent_mapper
    mapper_kwargs={'policy':backend} if tool_only and intent_mapper is None else {}
    goal=mapper(instruction,fresh,client,stop=stop,visual_options=candidates,**mapper_kwargs)
    if stop():raise ComputerError('windows_intent_cancelled')
    if not any(reviewed_goal(c['selector']) for c in goal['conditions']):
        return {'goal':goal,'root':None,'visual':False}
    bindings=[]
    identify=getattr(backend,'visual_document_identity',None)
    if identify is not None:
        for observed in fresh:
            if 'form_snapshot' in observed or 'reviewed_snapshot' in observed:continue
            try:
                bindings.append({'window':observed['window'],'document':identify(observed['window'],stop=stop)})
            except ComputerError:
                if stop():raise ComputerError('windows_intent_cancelled')
                # Navigation remains usable; missing document proof prohibits
                # future migration, rather than guessing a path from the title.
                continue
    reference=save(goal,candidates,fresh,model_call=getattr(client,'last_call',None),parent=proposal_base,
                   document_bindings=bindings)
    if stop():raise ComputerError('windows_intent_cancelled')
    parent=task_base or Path(__file__).resolve().parents[1]/'data/windows_tasks'
    root=task.create(parent,goal,visual_proposal=reference,**(task_create_kwargs or {}))
    return {'goal':goal,'root':root,'visual':True}


def rebind(parent, backend, *, stop=lambda:False, task_base=None, proposal_base=BASE):
    from .windows_visual_rebind import prepare
    from .windows_visual_continuation import create_once
    def build():
        result=prepare(parent,backend,stop=stop)
        ref=save(result['goal'],result['candidates'],result['observations'],parent=proposal_base,
                 document_bindings=result['document_bindings'],continuation=result['continuation'])
        return task.create(task_base or Path(__file__).resolve().parents[1]/'data/windows_tasks',
                         result['goal'],visual_proposal=ref)
    root=create_once(parent,build)
    return {'goal':task.load(root)['goal'],'root':root,'visual':True}


def restore_task(root, backend, *, stop=lambda:False, task_base=None, proposal_base=BASE):
    loaded=task.load(root)
    from .windows_visual_continuation import existing
    child=existing(root)
    if child is not None:
        restored=task.load(child)
        restored['migrated_from']=str(root)
        return restored
    presence=getattr(backend,'window_presence',None)
    if presence is None or not loaded['summary'].get('eligible'):
        return loaded
    scan=task._scan(Path(loaded['root']))
    if scan['task']['schema']!=3:return loaded
    payload=verify(scan['task']['visual_proposal'],scan['goal'])
    if 'continuation' in payload:return loaded
    windows=[]
    for condition in scan['goal']['conditions']:
        if condition['id'] in scan['completed']:continue
        if condition['window'] not in windows:windows.append(condition['window'])
    if any(presence(window,stop=stop) is False for window in windows):
        result=rebind(root,backend,stop=stop,task_base=task_base,proposal_base=proposal_base)
        child=task.load(result['root'])
        child['migrated_from']=str(root)
        return child
    return loaded
