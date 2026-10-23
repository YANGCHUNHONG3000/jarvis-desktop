"""Version-four policy: supplement/read interruption, proof replay and unchanged effects."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from agent_service import computer_agent as engine, research_recovery as recovery
from agent_service import comparison_crosssite_discovery as links
from agent_service.comparison_crosssite import CrossSiteEnvironment
from agent_service.comparison_completion import CompletionEnvironment
from agent_service.computer import ComputerError, read_record
from agent_service.project_documents import POLICY,PURPOSE
from agent_service.publisher_evidence import POLICY as DEFAULT_POLICY
from agent_service.project_reader import capture,validate_read
from test_computer_agent_tools import ctx as tool_ctx,STAMP
from test_dynamic_project_documents import gitlab_fixture,offline_fetch,prohibit_network
import test_repository_completion as old


@pytest.fixture
def dynamic_run(tool_ctx,monkeypatch):
    f=gitlab_fixture('team/nested/project')
    authors=f['blob'];root=f['tree']
    monkeypatch.setattr(old,'ROOT',root)
    monkeypatch.setattr(old,'AUTHORS',authors)
    monkeypatch.setattr(old,'OTHER',root.replace('/project/','/other/'))
    c=old.scoped_run.__wrapped__(tool_ctx,monkeypatch)
    data=json.loads(f['pages'][f['endpoint']][1])
    data['html']='<p>This project was started by Alice Smith.</p>'
    data.pop('raw_path')
    f['pages'][f['endpoint']]=('application/json',json.dumps(data).encode())
    c['network_calls']=[]
    def discover(self,url,query,excluded_urls=(),**kwargs):
        c['discovery_calls'].append(url)
        assert kwargs['purpose']==PURPOSE
        return links._discover(links._request(url,query,excluded_urls,purpose=PURPOSE),
                               offline_fetch(f['pages'],c['network_calls']),STAMP)
    monkeypatch.setattr(links.CrossSiteDiscoveryProcess,'discover',discover)
    class Reader:
        calls=[]
        last_document_proof=None
        def read(self,url,should_stop):
            self.calls.append(url)
            # Production uses the derived need query, fixed and bound in the proof.
            query='site:gitlab.com/team/nested/project authors maintainers copyright publisher'
            value=capture(url,query,offline_fetch(f['pages'],c['network_calls']),STAMP)
            self.last_document_proof=value['document_proof']
            return validate_read(value,url,query)
    c['browser']=Reader();c['options']['browser']=c['browser']
    original=c['client'].select
    def model(data):
        value=original(data)
        if 'selection' in data:
            from agent_service.project_scope import same_project
            urls={s['id']:s['url'] for s in data['sources']}
            sources={s['id']:s for s in data['sources']}
            for row in value['sources']:
                choices=sources[row['id']].get('project_role_candidates',[])
                if choices and row['publisher'] is not None:
                    index=next(i for i,p in enumerate(choices) if p['name']==row['publisher']['name'])
                    row['publisher']={'candidate_index':index}
            for pair in value['pairs']:
                pair['relation']='same' if same_project(urls[pair['left']],urls[pair['right']]) else 'independent'
        return value
    c['client'].select=model
    return c


@pytest.mark.parametrize('halt',[2,3])
def test_dynamic_supplement_and_read_resume_without_repeating_completed_steps(dynamic_run,halt):
    c=dynamic_run
    plan=engine.prepare(c['session'],'Find missing authors in this project',c['goal'])
    assert plan['goal']['comparison']['publisher_policy']==DEFAULT_POLICY
    first=old.execute(c,plan,halt)
    assert not first['ok'] and len(first['attempts'])==halt and not c['effects'],first
    before={p:p.read_bytes() for p in Path(first['run']).rglob('*') if p.is_file()}
    supplement=read_record(Path(first['run'])/'step-02/environment-result.json')['data']
    assert supplement['action']['purpose']==PURPOSE
    assert supplement['link_result']['proof']['version']==5
    resumed=recovery.prepare(c['session'],first['run'])['plan']
    result=old.execute(c,resumed)
    assert result['ok'],result
    assert len(c['discovery_calls'])==1 and len(c['browser'].calls)==1
    assert all(p.read_bytes()==raw for p,raw in before.items())
    assert len(c['effects'])==2


@pytest.mark.parametrize('env_type',[CompletionEnvironment,CrossSiteEnvironment])
@pytest.mark.parametrize('damage',['missing','text','resource','url','query'])
def test_dynamic_read_cannot_reuse_missing_changed_or_other_project_proof(env_type,damage):
    f=gitlab_fixture();q='authors'
    value=capture(f['blob'],q,offline_fetch(f['pages'],[]),STAMP)
    obs=validate_read(value,f['blob'],q).to_dict()
    record={'observation':obs,'document_proof':value['document_proof']}
    env=object.__new__(env_type);env.goal={'comparison':{'publisher_policy':POLICY}}
    env._validate_project_document(record,f['blob'],q)
    original=deepcopy(record)
    if damage=='missing':record.pop('document_proof')
    elif damage=='text':record['observation']['text']='Changed authors'
    elif damage=='resource':record['document_proof']['resources'][0]['sha256']='0'*64
    elif damage=='url':record['document_proof']['url']=f['blob'].replace('/project/','/other/')
    else:record['document_proof']['query']='unapproved'
    with pytest.raises(ComputerError,match='agent_evidence_changed'):
        env._validate_project_document(record,f['blob'],q)
    env.goal['comparison']['publisher_policy']='project_attribution_v3'
    env._validate_project_document({'observation':original['observation']},f['blob'],q)
