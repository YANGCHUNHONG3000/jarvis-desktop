"""Existing-file edits and stateful browser tools in the shared observed loop."""
from copy import deepcopy
from pathlib import Path
import re
import time
import uuid

from .computer import ComputerError, digest, read_record, safe_root, write_new
from .computer_agent_tools import _save, _load
from .computer_goal_adapters import TASK_BASE
from .office_scope import MAX_EDIT_FILES


def validate_goal(goal):
    from .browser_interaction import validate_config
    from .office_edit import validate_edit_spec
    if (type(goal) is not dict or set(goal) not in ({'kind','browser','edits'},
            {'kind','browser','edits','repository_document'}, {'kind','browser','edits','page_office'})
            or goal['kind'] != 'interactive' or type(goal['edits']) is not list
            or len(goal['edits']) > MAX_EDIT_FILES or (goal['browser'] is None and not goal['edits'])):
        raise ComputerError('interaction_goal_invalid')
    value = deepcopy(goal)
    if value['browser'] is not None:
        value['browser'] = validate_config(value['browser'])
    if 'repository_document' in value:
        if (type(value['repository_document']) is not bool or value['browser'] is None
                or value['browser'].get('mode') != 'github_repo'):
            raise ComputerError('repository_document_scope_invalid')
    if 'page_office' in value:
        if (type(value['page_office']) is not bool or value['browser'] is None
                or value['browser'].get('mode') != 'public_search'):
            raise ComputerError('page_office_scope_invalid')
    seen = set()
    for item in value['edits']:
        if (type(item) is not dict or set(item) != {'id','source_root','spec','after_browser'}
                or type(item['id']) is not str or item['id'] not in {f'e{n}' for n in range(1,MAX_EDIT_FILES+1)}
                or item['id'] in seen or type(item['after_browser']) is not bool
                or (item['after_browser'] and value['browser'] is None)
                or type(item['source_root']) is not str or len(item['source_root']) > 1000
                or not Path(item['source_root']).is_absolute()):
            raise ComputerError('interaction_edit_invalid')
        seen.add(item['id'])
        item['spec'] = validate_edit_spec(item['spec'])
    return value


def prepare(session, instruction, *, browser=None, edits=(), repository_document=False, page_office=False):
    """Snapshot explicit local file selections before showing the effect preview."""
    from .computer_agent import prepare as prepare_agent
    from .office_edit import snapshot_edit_source, _bind
    if type(edits) not in (list, tuple) or len(edits) > MAX_EDIT_FILES:
        raise ComputerError('interaction_edit_invalid')
    records = []
    for n, item in enumerate(edits, 1):
        if type(item) is not dict or set(item) - {'path','changes','after_browser'} or not {'path','changes'} <= item.keys():
            raise ComputerError('interaction_edit_invalid')
        root = safe_root(session.root/'edit-inputs'/uuid.uuid4().hex)
        root.mkdir(parents=True, exist_ok=False)
        source = snapshot_edit_source(item['path'], root)
        spec = {'kind':source['kind'], 'source_id':'selected-edit',
                'source_sha256':source['sha256'], 'changes':deepcopy(item['changes'])}
        spec, _ = _bind(spec, root)
        records.append({'id':f'e{n}', 'source_root':str(root), 'spec':spec,
                        'after_browser':item.get('after_browser', browser is not None)})
    goal = {'kind':'interactive','browser':browser,'edits':records}
    if type(repository_document) is not bool:
        raise ComputerError('repository_document_scope_invalid')
    if repository_document:
        goal['repository_document'] = True
    if type(page_office) is not bool:
        raise ComputerError('page_office_scope_invalid')
    if page_office:
        goal['page_office'] = True
    return prepare_agent(session, instruction, goal)


def describe(plan):
    goal = validate_goal(plan['goal'])
    lines = ['本次批准网页交互与已有文件编辑（根据实际状态选择下一步）：',
             '目标：' + plan['instruction']]
    if 'recovery' in plan:
        from .interaction_recovery import describe as describe_recovery
        lines.insert(0, describe_recovery(plan))
    if goal['browser'] and plan.get('recovery', {}).get('kind') != 'interactive_browser':
        config = goal['browser']
        lines += ['独立浏览器：' + config['start_url'],
                  '页面完成条件：'+' / '.join(config['required_text'])]
        if config.get('mode') == 'github_repo':
            lines += ['GitHub：搜索批准仓库，打开真实搜索结果中的精确仓库链接并读取。',
                      '登录与验证码由你在独立浏览器完成，再点“已完成登录／验证，继续任务”。',
                      '仅本机保存核验结果，不把登录页或私有正文提供给模型；歧义会停止。',
                      '每次人工等待最多15分钟，共最多3次；等待不计入360秒活动期限。']
            if goal.get('repository_document'):
                lines += ['读取通过后自动整理为 Word：仓库概况、来源与完整可见页面内容。',
                          '文档在本机生成并经 WPS 回读核验，随后自动选为下一任务资料。',
                          '这份文档记录已读取页面，不包含未读取代码的分析；文档阶段另有120秒上限。']
        elif config.get('mode') == 'public_search':
            lines += ['公开网站查询：'+config['query'],
                      ('从实际搜索结果中打开批准页面：'+config['target_url']
                       if config['target_url'] else
                       '按本次任务说明，从最多8个当前可见的站内结果中选择合适页面。'),
                      '自动识别输入框和结果链接；未知作用会停止，页面变化后重新观察。']
            if not config['target_url']:
                lines += ['多结果时仅任务说明、公开标题/网址/简短摘录提供给云模型；单结果无需模型。',
                          '未找到合适结果会停止；不能让网页扩大任务范围。']
            if goal.get('page_office'):
                lines += ['读取后本机生成来源/摘录 Excel 与完整有界页面资料 Word，',
                          '经 WPS 回读与文件核验后，两份成果自动接入下一任务；文档生成不调用模型。']
        elif config.get('mode') == 'discover_query':
            lines += ['自动识别页面的查询输入框和按钮；输入 '+repr(config['query']),
                      '仅执行已识别的同源 GET 查询；控件变化会重新观察，多候选按任务选择。']
            if config.get('javascript'):
                lines += ['支持 JavaScript 动态控件及异步查询；等待页面变化后重新核验。',
                          '明确只读失败可有限重试；页面变化最多重新选择3次，未知效果立即停止。']
        else:
            lines.append('允许同源路径：'+'、'.join(config['allowed_paths']))
            for rule in config['rules']:
                lines.append(('输入 '+repr(rule['value']) if rule['kind']=='fill' else '点击并核对 '+repr(rule['expected_text']))
                             +'；控件 '+rule['selector'])
    for edit in goal['edits']:
        from .office_edit import verify_edit_source
        source = verify_edit_source(edit['source_root'])
        if 'recovery' in plan:
            status = ('已完成，仅复核并带入成果' if edit['id'] in plan['recovery']['completed_edits']
                      else '尚未开始，本次执行')
            lines.append(status+'（'+edit['id']+'）：'+source['display_name'])
        lines += [f"编辑副本：{source['display_name']}（原件保留）；SHA {source['sha256'][:16]}"]
        original = read_record(Path(edit['source_root']) / 'source.json')['original']
        lines.append('本地原件：' + str(original))
        for change in edit['spec']['changes']:
            where = (f"表格{change['table_index']+1} 行{change['row_index']+1} 列{change['column_index']+1} 第{change['paragraph_index']+1}段"
                     if 'table_index' in change else ('第 '+str(change['paragraph_index']+1)+' 段')
                     if 'paragraph_index' in change else change['sheet']+'!'+change['cell'])
            lines.append(f"{where}：{change['before']!r} → {change['after']!r}")
        if edit['after_browser']:
            lines.append('此编辑仅在网页完成条件通过后执行。')
    return '\n'.join(lines + ['未改内容、格式和公式逐项复核；副本通过原生 WPS 回读才交付。',
        ('此站点任务不调用云模型；多候选歧义会停止，页面正文仅留本机。'
         if goal['browser'] and (goal['browser'].get('mode') == 'github_repo' or
             goal['browser'].get('mode') == 'public_search' and goal['browser'].get('target_url')) else
         '公开搜索仅向模型提供任务与候选元数据，办公原文不随结果选择上传。'
         if goal['browser'] and goal['browser'].get('mode') == 'public_search' else
         '多候选时任务、批准变更及有限页面文字会提供给云模型；单候选不调用模型。'),
        '12 动作 / 360 秒 / 会话最多 6 次模型调用；原共享每日和滚动 24 小时各 ¥5。',
        '未知效果立即停止；成果自动接到下一任务。登录仅人工接管；不支持发送、付款或任意页面控件。'])


class InteractionEnvironment:
    def __init__(self, goal, run, *, stop, deadline, clock=time.monotonic,
                 browser_factory=None, office_execute=None, task_base=TASK_BASE, human_handoff=None,
                 recovery_validator=None):
        self.goal, self.run = validate_goal(goal), safe_root(run)
        self.stop, self.deadline, self.clock = stop, deadline, clock
        self.task_base = safe_root(task_base)
        self.office_execute = office_execute
        self.recovery_validator = recovery_validator
        self.browser = None
        self.last_browser = None
        seed = recovery_validator() if recovery_validator is not None else {}
        self.browser_checkpoint = deepcopy(seed.get('browser_checkpoint'))
        self.browser_resume = deepcopy(seed.get('browser_resume'))
        self.handoff = None
        if (self.goal['browser'] and not self.browser_checkpoint
                and self.goal['browser'].get('mode') == 'github_repo'):
            from .browser_handoff import HumanHandoff
            self.handoff = HumanHandoff(self.run, self.goal['browser'], stop=stop,
                                       clock=clock, callback=human_handoff)
        for edit in self.goal['edits']:
            source = safe_root(edit['source_root'])
            if source.parent != self.run.parent.parent/'edit-inputs':
                raise ComputerError('interaction_source_owner_invalid')
        _save(self.run/'environment-goal.json', self.goal)
        if self.goal['browser'] is not None and not self.browser_checkpoint:
            if browser_factory is None:
                from .browser_interaction import BrowserInteraction
                browser_factory = BrowserInteraction
            options = {'stop':stop,'deadline':deadline,'clock':clock}
            if self.browser_resume is not None:
                options['resume'] = deepcopy(self.browser_resume)
            self.browser = browser_factory(self.goal['browser'], **options)

    def _check(self):
        if self.stop():
            raise ComputerError('goal_cancelled')
        if self.clock() >= self.deadline:
            raise ComputerError('goal_deadline')

    def _facts(self):
        from .office_edit import verify_edit_source, verify_edit_result
        seed = self.recovery_validator() if self.recovery_validator is not None else None
        if seed is not None and seed.get('browser_checkpoint') != self.browser_checkpoint:
            raise ComputerError('recovery_browser_checkpoint_changed')
        if seed is not None and seed.get('browser_resume') != self.browser_resume:
            raise ComputerError('recovery_browser_resume_changed')
        if _load(self.run/'environment-goal.json')[0] != self.goal:
            raise ComputerError('interaction_goal_changed')
        edits = {e['id']:e for e in self.goal['edits']}
        for item in edits.values():
            if verify_edit_source(item['source_root'])['sha256'] != item['spec']['source_sha256']:
                raise ComputerError('interaction_source_changed')
        done, started, failures, artifacts, hashes = [], [], [], [], []
        if seed is not None:
            done, started = list(seed['done']), list(seed['done'])
            artifacts, hashes = deepcopy(seed['artifacts']), list(seed['hashes'])
        for step in sorted(self.run.glob('step-*')):
            if not (step/'environment-intent.json').exists():
                continue
            intent, intent_hash = _load(step/'environment-intent.json')
            if not (step/'environment-result.json').exists():
                raise ComputerError('agent_interrupted_action')
            record, record_hash = _load(step/'environment-result.json')
            hashes.append(record_hash)
            if record['intent_sha256'] != intent_hash or record['action'] != intent['action']:
                raise ComputerError('agent_evidence_changed')
            action, result = record['action'], record['result']
            if action['kind'] == 'office_edit':
                item = edits[action['edit_id']]
                if item['id'] in started:
                    raise ComputerError('interaction_duplicate_edit')
                started.append(item['id'])
                if result['ok']:
                    actual = verify_edit_result(record['native'], item['source_root'], item['spec'])
                    done.append(item['id'])
                    artifacts.append({'kind':item['spec']['kind'], 'path':actual['artifact'],
                                      'sha256':actual['sha256'], 'provenance':'office_edit'})
            if not result['ok']:
                failures.append({'kind':action['kind'],'code':result.get('code','failed')})
        return done, started, failures, artifacts, hashes

    def _observation(self, *, final=False):
        done, started, failures, artifacts, hashes = self._facts()
        if self.browser_checkpoint is not None:
            from .browser_checkpoint import frozen
            browser, browser_ready = frozen(self.browser_checkpoint), True
        elif self.browser is not None:
            if not final:
                current = self.browser.observe()
                while current.get('human_required'):
                    if self.handoff is None:
                        raise ComputerError('browser_human_required')
                    self.handoff.perform(self.browser)
                    current = self.browser.observe()
                self.last_browser = current
            # A stopped/closed browser cannot establish live completion. Files
            # are still revalidated, allowing truthful partial handoff.
            browser = deepcopy(self.last_browser)
            browser_ready = bool(not final and browser and browser['checks']['goal_met'])
            if browser_ready:
                from .browser_checkpoint import capture
                capture(self.run, self.goal['browser'], browser)
        else:
            browser, browser_ready = None, True
        return {'browser':browser, 'done_edits':done, 'started_edits':started,
                'failures':failures, 'artifacts':sorted(artifacts,key=lambda a:a['kind']=='document'), 'source_reads':0,
                'evidence_sha256':digest(hashes),
                'checks':{'sources_ready':browser_ready,
                          'goal_met':browser_ready and len(done)==len(self.goal['edits'])}}

    def observe(self):
        self._check()
        return self._observation()

    def observe_final(self):
        return self._observation(final=True)

    def actions(self, observation):
        self._check()
        if self.observe() != observation:
            raise ComputerError('agent_observation_changed')
        actions = []
        browser = observation['browser']
        if browser and not browser['checks']['goal_met'] and not browser['locked']:
            actions.extend(deepcopy(browser['actions']))
        for item in self.goal['edits']:
            if item['id'] in observation['started_edits']:
                continue
            if not item['after_browser'] or observation['checks']['sources_ready']:
                actions.append({'kind':'office_edit','edit_id':item['id']})
        if (self.goal['browser'] and self.goal['browser'].get('mode') in {'github_repo','public_search'}
                and observation['checks']['sources_ready']):
            # Approved edits have a fixed order; keep private page captures local.
            return actions[:1]
        if len(actions) > 8:
            # Bound model context without changing previously observed IDs.
            actions = actions[:8]
        return actions

    def execute(self, action, step_root):
        self._check()
        observed = self.observe()
        step = safe_root(step_root)
        if step.parent != self.run or not step.is_dir():
            raise ComputerError('interaction_step_invalid')
        dynamic_browser = bool(self.goal['browser'] and (self.goal['browser'].get('javascript')
                               or self.goal['browser'].get('mode') in {'github_repo','public_search'}))
        stale = False
        try:
            stale = action not in self.actions(observed)
        except ComputerError as exc:
            if not dynamic_browser or str(exc) != 'agent_observation_changed':
                raise
            stale = True
        if stale and (not dynamic_browser or action.get('kind') not in {'browser_fill','browser_click','browser_wait'}):
            raise ComputerError('interaction_action_stale')
        intent = {'action':deepcopy(action)}
        _save(step/'environment-intent.json', intent)
        record = {'action':deepcopy(action),'intent_sha256':digest(intent)}
        if stale:
            result = {'ok':False,'state':'failed','code':'browser_action_stale',
                      'verification':{'effect_dispatched':False},'observation':observed['browser']}
        elif action['kind'] in {'browser_fill','browser_click','browser_wait','browser_search_open','browser_read'}:
            result = self.browser.execute(action)
            self.last_browser = deepcopy(result.get('observation', self.last_browser))
        else:
            item = next(e for e in self.goal['edits'] if e['id']==action['edit_id'])
            output = self.task_base/('task-'+uuid.uuid4().hex)/'edit'
            output.mkdir(parents=True, exist_ok=False)
            try:
                from .office_edit import execute_edit_supervised, verify_edit_result
                native = (self.office_execute or execute_edit_supervised)(item['spec'], output,
                         item['source_root'], stop=self.stop,
                         deadline=self.clock.wall_deadline(self.deadline) if hasattr(self.clock,'wall_deadline') else self.deadline)
                if native.get('ok') is True:
                    native = verify_edit_result(native, item['source_root'], item['spec'])
                    result = {'ok':True,'state':'verified','code':'existing_file_edit_verified'}
                else:
                    result = {'ok':False,'state':'unknown' if native.get('state')=='unknown' else 'failed',
                              'code':native.get('code','office_edit_failed')}
                record['native'] = native
            except Exception:
                result = {'ok':False,'state':'unknown','code':'office_edit_unknown'}
            _save(output/'edit-manifest.json', {'source_root':item['source_root'],'spec':item['spec'],
                                               'run':str(self.run),'step':str(step)})
        record['result'] = deepcopy(result)
        _save(step/'environment-result.json', record)
        return result

    def close(self):
        if self.browser is not None:
            self.browser.close()
