"""Multi-site comparison through original loop, source proofs and recovery."""
from copy import deepcopy
from pathlib import Path
import json
import time
from itertools import combinations
from urllib.parse import urlsplit

import pytest

from agent_service import computer_agent as engine, research_recovery as recovery
from agent_service.computer import ComputerError, ComputerSession, digest, read_record
from agent_service.computer_agent_tools import AgentEnvironment
from test_computer_agent_tools import ctx as tool_ctx, A, B
from test_computer_agent import Client


def semantic_review(data):
    """Offline model fixture; publisher evidence is captured text, never a host claim."""
    publishers = {s['id']: s['text'].split(';', 1)[0] for s in data['sources']}
    return {
        'sources': [{'id': s['id'],
                     'publisher': {'name': publishers[s['id']], 'quote': publishers[s['id']]},
                     'cells': [{'status': 'relevant', 'reason': 'The selected text states the requested fact.'}
                               for _ in data['dimensions']]} for s in data['sources']],
        'pairs': [{'left': a['id'], 'right': b['id'],
                   'relation': 'same' if publishers[a['id']] == publishers[b['id']] else 'independent',
                   'reason': 'Captured publisher statements identify the source relationship.'}
                  for a, b in combinations(data['sources'], 2)]}


@pytest.fixture
def ctx(tool_ctx):
    c = tool_ctx
    c['session'] = ComputerSession.create(c['run'].parent.parent/('cp-'+digest(str(c['run']))[:8]))
    c['goal'].update(min_sources=4, comparison={'dimensions':['事实'], 'min_domains':2})
    c['search'].results = {'first':[A,A+'two'], 'second':[B,B+'two']}
    c['browser'].text.update({A+'two':'alpha third omega', B+'two':'alpha fourth omega'})
    for url in (A, A+'two', B, B+'two'):
        publisher = 'Publisher Alpha' if urlsplit(url).hostname == urlsplit(A).hostname else 'Publisher Beta'
        c['browser'].text[url] = publisher + '; ' + c['browser'].text[url]
    def select(data):
        if 'selection' in data:
            return semantic_review(data)
        if 'sources' in data and 'dimensions' in data:
            return {'sources':[{'id':s['id'],'spans':[{'quote':s['text']}]} for s in data['sources']]}
        return {'action_id':data['choices'][0]['id']}
    c['client'] = Client(select)
    return c


def run(c, plan, halt=None, **extra):
    root = c['session'].root/'goals'/(plan['sha256']+'.run')
    options = {k:v for k,v in c['options'].items() if k not in {'stop','deadline'}}
    return engine.execute(c['session'], plan, approval=plan['sha256'], client=c['client'],
        stop=lambda: halt is not None and len(list(root.glob('step-*/verification.json'))) >= halt,
        **options, **extra)


def saved_v1_plan(ctx, instruction):
    """Reconstruct a historical stored plan, without weakening new preparation."""
    from agent_service.computer_goals import _save_goal
    return _save_goal(ctx['session'], {'schema': 3, 'mode': 'agent',
        'instruction': instruction, 'goal': deepcopy(ctx['goal']),
        'max_actions': engine.MAX_ACTIONS, 'max_seconds': engine.MAX_SECONDS,
        'prepared_at': time.time()})


def saved_v2_plan(ctx, instruction, goal=None):
    """Keep historical loop/recovery fixtures on their original v2 contract."""
    from agent_service.computer_goals import _save_goal
    objective = deepcopy(ctx['goal'] if goal is None else goal)
    if 'comparison' in objective:
        objective['comparison']['evidence_version'] = 2
    return _save_goal(ctx['session'], {'schema': 3, 'mode': 'agent',
        'instruction': instruction, 'goal': objective,
        'max_actions': engine.MAX_ACTIONS, 'max_seconds': engine.MAX_SECONDS,
        'prepared_at': time.time()})


@pytest.mark.parametrize('halt',[2,6,7,8])
def test_comparison_resumes_without_research_model_or_office_replay(ctx, halt):
    plan = saved_v2_plan(ctx,'对比四份来源',ctx['goal'])
    stopped = run(ctx, plan, halt)
    assert not stopped['ok'] and stopped['state'] in {'stopped','partial'}
    root = Path(stopped['run'])
    original = {p:p.read_bytes() for p in root.rglob('*') if p.is_file()}
    resumed = recovery.prepare(ctx['session'],root)['plan']
    final = run(ctx,resumed)
    assert final['ok'], final['code']
    assert ctx['search'].calls == ['first','second']
    assert len(ctx['browser'].calls)==len(set(ctx['browser'].calls))==4
    assert sum('dimensions' in call and 'selection' not in call for call in ctx['client'].calls)==1
    assert sum('selection' in call for call in ctx['client'].calls)==1
    assert [s['kind'] for s,_ in ctx['effects']]==['spreadsheet','document']
    assert all(p.read_bytes()==content for p,content in original.items())
    sheet,word=[s for s,_ in ctx['effects']]
    assert word['table']=={'headers':sheet['headers'],'rows':sheet['rows']}
    assert len(sheet['rows'])==4 and len(final['artifacts'])==2
    assert '对比' in engine.describe(plan)
    assert '8个不同候选' in engine.describe(plan)


def test_two_continuations_preserve_compare_and_excel(ctx):
    first=run(ctx,saved_v2_plan(ctx,'对比与续接',ctx['goal']),2)
    second=run(ctx,recovery.prepare(ctx['session'],first['run'])['plan'],6)
    assert [a['action']['kind'] for a in second['attempts']][-1]=='spreadsheet'
    third=run(ctx,recovery.prepare(ctx['session'],second['run'])['plan'])
    assert third['ok'] and [a['action']['kind'] for a in third['attempts']]==['document']
    assert len(ctx['effects'])==2 and len(ctx['client'].calls)<=6


def test_distinct_hosts_required_even_with_enough_pages(ctx):
    ctx['goal']['min_sources']=2
    ctx['search'].results['second']=[A+'three',A+'four']
    ctx['browser'].text.update({A+'three':'alpha three omega',A+'four':'alpha four omega'})
    result=run(ctx,saved_v2_plan(ctx,'必须两个站点',ctx['goal']))
    assert not result['ok'] and result['code']=='agent_no_safe_next_action'
    assert not ctx['effects'] and not any('dimensions' in c for c in ctx['client'].calls)


def test_changed_comparison_blocks_recovery(ctx):
    stopped=run(ctx,saved_v2_plan(ctx,'对比后恢复',ctx['goal']),7)
    path=Path(stopped['run'])/'step-07'/'environment-result.json'
    obj=read_record(path)
    obj['data']['comparison']['response']['sources'][0]['spans'][0]={'quote':'invented'}
    obj['sha256']=digest(obj['data'])
    path.write_text(json.dumps(obj),encoding='utf-8')
    assert not recovery.inspect(ctx['session'],stopped['run'])['eligible']
    assert not ctx['effects']


def test_invalid_model_evidence_never_reaches_office(ctx):
    original=ctx['client'].select
    ctx['client'].select=lambda data: {'sources':[]} if 'dimensions' in data else original(data)
    result=run(ctx,saved_v2_plan(ctx,'对比',ctx['goal']))
    assert not result['ok'] and not ctx['effects']
    assert not recovery.inspect(ctx['session'],result['run'])['eligible']


@pytest.mark.parametrize('change',[
    {'min_sources':1}, {'queries':['first']},
    {'comparison':{'dimensions':['a'],'min_domains':True}},
])
def test_infeasible_or_invalid_goal_rejected(ctx,change):
    ctx['goal'].update(change)
    with pytest.raises(ComputerError): engine.validate_goal(ctx['goal'])


def test_duplicate_and_rejected_backup_survive_recovery(ctx):
    # Each query may retain backups; a cross-query duplicate is read once.
    ctx['search'].results = {'first':[A, A+'bad', A+'two'],
                             'second':[A, B, B+'two', B+'unused']}
    ctx['browser'].text[A+'bad'] = 'irrelevant rejected text'
    first = run(ctx, saved_v1_plan(ctx, '备选来源与恢复'), 3)
    assert not first['ok']
    original = {p:p.read_bytes() for p in Path(first['run']).rglob('*') if p.is_file()}
    result = run(ctx, recovery.prepare(ctx['session'], first['run'])['plan'])
    assert result['ok'], result['code']
    assert ctx['search'].calls == ['first', 'second']
    assert len(ctx['browser'].calls) == len(set(ctx['browser'].calls)) == 5
    assert ctx['browser'].calls.count(A+'bad') == 1
    assert B+'unused' not in ctx['browser'].calls
    assert len(result['failures']) == 1 and result['failures'][0]['code'] == 'quality_rejected'
    assert len(ctx['effects'][0][0]['rows']) == 4
    assert all(p.read_bytes() == content for p,content in original.items())


def test_comparison_receipt_cap_is_four_per_query(ctx):
    ctx['search'].results['first'] = [A+str(i) for i in range(5)]
    result = run(ctx, saved_v2_plan(ctx, '拒绝超额候选', ctx['goal']))
    assert not result['ok'] and result['code'] == 'search_receipt_invalid'
    assert not ctx['browser'].calls and not ctx['effects']


def test_ordinary_research_still_rejects_three_candidate_receipt(ctx):
    del ctx['goal']['comparison']
    ctx['goal']['min_sources'] = 2
    ctx['search'].results['first'] = [A, A+'two', A+'three']
    result = run(ctx, saved_v2_plan(ctx, '普通检索上限', ctx['goal']))
    assert not result['ok'] and result['code'] == 'search_receipt_invalid'
    assert not ctx['browser'].calls and not ctx['effects']


def test_research_verified_pause_uses_existing_recovery(ctx):
    plan=saved_v2_plan(ctx,'检索核验后暂停',ctx['goal'])
    prior=run(ctx,plan,pause_after_step=lambda: True)
    assert prior['code']=='agent_paused' and prior['actions']==1
    assert ctx['search'].calls==['first'] and not ctx['browser'].calls
    resumed=recovery.prepare(ctx['session'],prior['run'])['plan']
    final=run(ctx,resumed)
    assert final['ok'] and ctx['search'].calls==['first','second']
    assert len(ctx['browser'].calls)==len(set(ctx['browser'].calls))==4
