"""Project-specific repair/recovery with offline network and Office doubles."""
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_service import computer_agent as engine, research_recovery as recovery
from agent_service.comparison_completion import CompletionEnvironment
from agent_service.comparison_crosssite import CrossSiteEnvironment
from agent_service.computer import digest, read_record
from agent_service.project_scope import POLICY, project_site, same_project
from agent_service.publisher_evidence import attribution_status, REPOSITORY_POLICY, review_instructions
from agent_service.source_comparison import introduction
from test_computer_agent_tools import ctx as tool_ctx
from test_repository_completion import missing
import test_repository_completion as fixture_module


CASES = [
    ('https://gitlab.com/team/sub/app/-/tree/main',
     'https://gitlab.com/team/sub/app/-/blob/main/AUTHORS.rst',
     'https://gitlab.com/team/other/app/-/blob/main/AUTHORS.rst'),
    ('https://huggingface.co/team/app', 'https://huggingface.co/team/app/blob/main/AUTHORS.rst',
     'https://huggingface.co/datasets/team/app/blob/main/AUTHORS.rst'),
    ('https://huggingface.co/datasets/team/app', 'https://huggingface.co/datasets/team/app/blob/main/AUTHORS.rst',
     'https://huggingface.co/spaces/team/app/blob/main/AUTHORS.rst'),
    ('https://huggingface.co/spaces/team/app', 'https://huggingface.co/spaces/team/app/blob/main/AUTHORS.rst',
     'https://huggingface.co/team/app/blob/main/AUTHORS.rst'),
    ('https://codeberg.org/team/app', 'https://codeberg.org/team/app/src/branch/main/AUTHORS.rst',
     'https://codeberg.org/team/other/src/branch/main/AUTHORS.rst'),
]


@pytest.mark.parametrize('env_type', [CompletionEnvironment, CrossSiteEnvironment])
@pytest.mark.parametrize('source,authors,other', CASES)
def test_missing_attribution_generates_bounded_project_repair(env_type, source, authors, other):
    env, facts, evidence = missing(env_type, policy=POLICY, url=source)
    need, = env._needs(facts, evidence)
    assert need['purpose'] == 'project_attribution' and need['scope_url'] == source
    assert need['source_id'] == 'target'
    assert need['query'].startswith('site:' + project_site(source) + ' ')
    assert env._link_inputs(facts, need)[0] == source
    assert env._supplement_allowed({}, need, [authors])
    assert not env._supplement_allowed({}, need, [other])
    assert not env._supplement_allowed({'link_result': {'discovery': {'urls': [other]}}}, need, [other])
    assert env._matches_need({'url': authors, 'need_id': need['need_id']}, need)
    assert not env._matches_need({'url': other, 'need_id': need['need_id']}, need)


@pytest.mark.parametrize('env_type', [CompletionEnvironment, CrossSiteEnvironment])
@pytest.mark.parametrize('source', ['https://gitlab.com/group/subgroup',
    'https://huggingface.co/team', 'https://codeberg.org/team/app/issues/12',
    'https://bitbucket.org/team/app'])
def test_unknown_project_never_broadens_to_whole_platform(env_type, source):
    env, facts, evidence = missing(env_type, policy=POLICY, url=source)
    assert env._needs(facts, evidence) == []


@pytest.mark.parametrize('env_type', [CompletionEnvironment, CrossSiteEnvironment])
def test_long_project_scope_not_truncated(env_type):
    url = 'https://gitlab.com/' + '/'.join(['n'*90]*8) + '/-/tree/main'
    env, facts, evidence = missing(env_type, policy=POLICY, url=url)
    assert env._needs(facts, evidence) == []


@pytest.mark.parametrize('title', ['GNU GENERAL PUBLIC LICENSE',
    'GNU LESSER GENERAL PUBLIC LICENSE', 'GNU AFFERO GENERAL PUBLIC LICENSE'])
def test_gnu_template_copyright_not_project_publisher_and_v2_unchanged(title):
    quote = 'Copyright (C) 2007 Free Software Foundation, Inc.'
    source = {'text': title + '\n' + quote, 'title': 'LICENSE', 'url': 'https://codeberg.org/team/app/src/branch/main/LICENSE'}
    publisher = {'name': 'Free Software Foundation', 'quote': quote}
    assert attribution_status(source, publisher, policy=POLICY) == 'license_template_credit'
    assert attribution_status(source, publisher, policy=REPOSITORY_POLICY) == 'supported'


def test_project_specific_copyright_remains_valid():
    source = {'text': 'Copyright 2025 Actual Developer.', 'title': 'NOTICE',
              'url': 'https://huggingface.co/team/app/blob/main/NOTICE'}
    assert attribution_status(source, {'name': 'Actual Developer', 'quote': source['text']}, policy=POLICY) == 'supported'


def test_new_prompt_and_report_distinguish_platform_projects():
    goal = {'comparison': {'publisher_policy': POLICY, 'evidence_version': 4}}
    assert 'GitLab' in review_instructions(goal) and 'dataset' in review_instructions(goal)
    assert 'GitLab' in introduction(goal) and 'Space' in introduction(goal)
    goal['comparison']['publisher_policy'] = REPOSITORY_POLICY
    assert 'GitLab' not in review_instructions(goal) and 'Space' not in introduction(goal)


@pytest.mark.parametrize('status,label', [
    ('project_scope_missing', '无法确认项目边界，需本页归属依据'),
    ('license_template_credit', '仅许可证模板署名，未证明项目发布方'),
])
def test_project_failure_explains_missing_evidence_without_rewriting_it(status, label):
    from agent_service.task_diagnostics import explain_failure
    assessment = {'complete_independent_count': 0, 'required_independent_count': 2,
                  'group_details': [{'ids': ['target'], 'publisher_supported': False,
                                     'covered_dimensions': [True], 'complete': False}],
                  'publisher_evidence': {'target': status},
                  'publisher_support_reasons': {'target': status}}
    original = deepcopy(assessment)
    assert label in explain_failure('comparison_coverage_insufficient', assessment=assessment)
    assert assessment == original


@pytest.fixture(params=CASES)
def project_run(request, tool_ctx, monkeypatch):
    source, authors, other = request.param
    monkeypatch.setattr(fixture_module, 'ROOT', source)
    monkeypatch.setattr(fixture_module, 'AUTHORS', authors)
    monkeypatch.setattr(fixture_module, 'OTHER', other)
    c = fixture_module.scoped_run.__wrapped__(tool_ctx, monkeypatch)
    original_model = c['client'].select
    def model(data):
        value = original_model(data)
        if 'selection' in data:
            urls = {s['id']: s['url'] for s in data['sources']}
            for pair in value['pairs']:
                pair['relation'] = ('same' if same_project(urls[pair['left']], urls[pair['right']])
                                    else 'independent')
        return value
    c['client'].select = model
    # Group membership is an explicit test claim; scope verification remains real.
    c['project_urls'] = (source, authors, other)
    return c


def test_project_proof_pause_recovery_does_not_repeat_completed_steps(project_run):
    c = project_run
    source, authors, other = c['project_urls']
    with patch('agent_service.publisher_evidence.POLICY', POLICY):
        plan = engine.prepare(c['session'], 'Find same-project attribution', c['goal'])
    first = fixture_module.execute(c, plan, 2)
    assert not first['ok'] and len(first['attempts']) == 2 and not c['effects'], first
    before = {p: p.read_bytes() for p in Path(first['run']).rglob('*') if p.is_file()}
    record = read_record(Path(first['run'])/'step-02/environment-result.json')['data']
    assert record['action']['purpose'] == 'project_attribution'
    assert record['link_result']['proof']['version'] == 4
    assert record['link_result']['discovery']['urls'] == [authors]
    resumed = recovery.prepare(c['session'], first['run'])['plan']
    result = fixture_module.execute(c, resumed)
    assert result['ok'], result
    assert c['discovery_calls'] == [source] and c['browser'].calls == [authors]
    assert all(p.read_bytes() == old for p, old in before.items())
    assert len(c['effects']) == 2 and len(c['client'].calls) == 4
