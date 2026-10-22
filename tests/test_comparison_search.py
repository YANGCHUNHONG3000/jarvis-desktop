"""Comparison searches use only the code-owned provider and declared site scope."""
import json
import pytest
from agent_service.research import search_public, ResearchError
from agent_service.computer_search import SearchProcess
from agent_service.computer import ComputerError
from test_research_workflow import Session, Response


def test_bing_scope_filters_unrelated_hosts_and_path_siblings():
    html='<ul>'+''.join(f'<li class="b_algo"><h2><a href="{u}">wireless keyboard</a></h2></li>' for u in (
        'https://example.org/item/a','https://www.example.com/productivity/b',
        'https://www.example.com/product/c','https://example.com/product/d'))+'</ul>'
    session=Session([Response(html)])
    found=search_public('site:example.com/product wireless keyboard',2,provider='bing',session=session)
    assert found==['https://www.example.com/product/c','https://example.com/product/d']
    assert len(session.calls)==1 and session.calls[0][0]=='https://www.bing.com/search'


@pytest.mark.parametrize('scope',['site:example.com site:other.com','site:a.com/../x','site:user@host'])
def test_invalid_scope_rejected_without_dispatch(scope):
    session=Session([])
    with pytest.raises(ResearchError):search_public(scope,provider='bing',session=session)
    assert not session.calls


@pytest.mark.parametrize('value',['https://arbitrary.example/','duckduckgo',True])
def test_arbitrary_provider_rejected(value):
    with pytest.raises(ComputerError):SearchProcess(provider=value)
    with pytest.raises(ResearchError):search_public('x',provider=value,session=Session([]))


def test_supervised_child_receives_fixed_provider(monkeypatch):
    from agent_service import process
    commands=[]
    def child(command,**kwargs):
        commands.append(command)
        return json.dumps({'urls':['https://example.com/a'],'searched_at':'2026-10-20T12:00:00+00:00'})
    monkeypatch.setattr(process,'run_child',child)
    assert SearchProcess(provider='bing').search('topic')['urls']==['https://example.com/a']
    assert json.loads(commands[0][-1])=={'query':'topic','max_sources':2,'provider':'bing'}


def test_site_scope_preserves_path_case_and_normalizes_host():
    html='<ul>'+''.join(f'<li class="b_algo"><h2><a href="{u}">x</a></h2></li>' for u in (
        'https://example.com/products/a','https://example.com/Products/b'))+'</ul>'
    session=Session([Response(html)])
    assert search_public('site:EXAMPLE.COM/Products x',provider='bing',session=session)==[
        'https://example.com/Products/b']
