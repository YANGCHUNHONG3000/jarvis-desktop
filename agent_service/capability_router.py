"""Approved, single-use bridge to the existing skill/adapter task engine."""
from copy import deepcopy
from pathlib import Path
import re
import threading
import time
import uuid

from .computer import BASE, ComputerError, digest, read_record, safe_root, write_new
from .computer_goal_adapters import TASK_BASE
from .computer_goals import _save_goal


def _source(ref, base):
    if ref is None:
        return
    from .office_handoff import verify_references
    if type(ref) is not dict:
        raise ComputerError('capability_source_invalid')
    verify_references([ref.get('artifact')], [ref], base=base)


def prepare(session, instruction, *, source_ref=None, source_refs=None, task_base=TASK_BASE,
            _clarification=None):
    from .skill_matching import discover_candidates
    if type(instruction) is not str or not 1 <= len(instruction.strip()) <= 4000:
        raise ComputerError('instruction_invalid')
    base = safe_root(task_base)
    if source_refs is not None:
        from .capability_source_choice import verify_refs
        if source_ref is not None:
            raise ComputerError('capability_source_conflict')
        verify_refs(source_refs, base)
    _source(source_ref, base)
    found = discover_candidates(base.parent / 'capabilities', base, query=instruction)
    value = {'schema': 1, 'mode': 'capability',
        'instruction': instruction.strip(), 'source_ref': deepcopy(source_ref),
        'task_base': str(base), 'request_id': uuid.uuid4().hex,
        'candidates': found['candidates']}
    if source_refs is not None:
        value.update(schema=2, source_refs=deepcopy(source_refs))
    if _clarification is not None:
        if source_refs is not None or source_ref is None:
            raise ComputerError('capability_source_conflict')
        value.update(schema=3, clarification=deepcopy(_clarification))
    return _save_goal(session, value)


def prepare_mail(session, instruction, *, source_ref, task_base=TASK_BASE):
    """Local preview; only execution after approval uploads the selected mail."""
    if type(instruction) is not str or not 1 <= len(instruction.strip()) <= 4000:
        raise ComputerError('instruction_invalid')
    base = safe_root(task_base)
    from .mail_understanding import load_mail
    load_mail(source_ref, base)
    value = {'schema': 4, 'mode': 'capability', 'capability': 'mail_understanding',
             'instruction': instruction.strip(), 'source_ref': deepcopy(source_ref),
             'task_base': str(base), 'request_id': uuid.uuid4().hex, 'candidates': []}
    return _save_goal(session, value)


def _mail_plan(plan, base):
    keys = {'schema', 'mode', 'instruction', 'source_ref', 'task_base',
            'request_id', 'candidates', 'sha256', 'capability'}
    if (set(plan) != keys or type(plan['schema']) is not int or plan['schema'] != 4
            or plan['capability'] != 'mail_understanding' or plan['candidates'] != []
            or type(plan['instruction']) is not str or not 1 <= len(plan['instruction'].strip()) <= 4000
            or type(plan['request_id']) is not str
            or re.fullmatch(r'[0-9a-f]{32}', plan['request_id']) is None):
        raise ComputerError('capability_mail_plan_invalid')
    from .mail_understanding import load_mail
    load_mail(plan['source_ref'], base)


def _bind_mail_result(plan, task, result, base):
    """A mail-approved plan cannot be fulfilled by an unrelated Office task."""
    from . import mail_understanding as mail
    if (not (task / 'mail-analysis-claim.json').is_file()
            or (task / 'request.txt').read_text(encoding='utf-8') != plan['instruction']):
        raise ComputerError('capability_mail_result_changed')
    request = task / 'mail-analysis-request.json'
    if request.exists():
        req = read_record(request)
        if (req.get('instruction') != plan['instruction'] or req.get('source_ref') != plan['source_ref']):
            raise ComputerError('capability_mail_result_changed')
    if result['ok']:
        proposal = read_record(task / 'mail-analysis-plan.json')
        mail.verify_task(task, base, proposal['steps'])


def describe(plan):
    if plan.get('schema') == 4:
        return '\n'.join([
            '已选择：所选邮件内容理解。', '任务：' + plan['instruction'],
            '来源：' + plan['source_ref']['artifact'],
            '批准后将这一封邮件的主题和正文上传云端模型；规划时仅使用文件元数据。',
            '按任务指定的人员、主题或日期范围整理，每项附原文行号及引句，独立语义审查后生成所需Word/Excel；明确要求时也可单独拟写未发送回复。',
            '回复仅生成本地.eml，收件人留空；只附加明确要求的新建Office文件，未知完成事实和未经授权的承诺不得写入。',
            '输出注明整理范围；完整正文仍用于上下文审查，范围不明确时会提出问题，等待补充要求。',
            '未说明的信息保持未知；附件内容未读取，邮件头身份未认证。',
            '仅生成本地新文件，不发送邮件、不执行正文中的指令。成果自动接入下一任务。',
            '多封联合、附件内容和超出能力的任务会停止并说明原因；重复批准不重复执行。'])
    source = plan['source_ref']
    lines = ['根据任务匹配已有技能；批准后上传任务说明和所选来源内容。',
             '任务：' + plan['instruction'],
             '来源：' + (source['artifact'] if source else '无来源文件（不会自动读取其他文件）'),
             '先核对全部效果与任务意图，再执行已验证技能；否则尝试现有适配器。',
             '仅生成新的自有文件；不发送、不购买、不删除，不自动执行新下载或生成的代码。',
             '没有合适能力：保留缺口及适配器开发请求，等待测试、审查、实机验证、限次启用。']
    usable = [c for c in plan['candidates'] if c['reason'] == 'source_required']
    lines.append(f'预览时验证有效、待匹配来源的技能 {len(usable)} 个；执行前重新核验，不保证匹配。')
    lines.append('完成后 Office 成果自动接入下一任务；中断或结果未知时不重复已发出的操作。')
    if plan.get('schema') == 2:
        lines[2] = '来源：按任务从以下已核验成果中选择一份输入\n' + '\n'.join(
            f"{i}. {r['artifact']}" for i, r in enumerate(plan['source_refs'], 1))
        lines.append('批准后先用文件名/格式选择输入（单候选零模型、多候选至多一次调用），再上传选中内容。'
                     '任务需要多份联合输入或无法确定时停止，请明确来源；未选正文不上传。')
    elif plan.get('schema') == 3:
        lines.append('已明确原任务的输入来源；本次为新批准，保留旧尝试，不重复调用选源模型。'
                     '请核对这份输入能否满足全部任务；多份联合处理不在本次范围内。')
    return '\n'.join(lines)


class _SessionClient:
    def __init__(self, client, session, stop):
        self.client, self.session, self.stop = client, session, stop

    def __getattr__(self, name):
        return getattr(self.client, name)

    def complete(self, system, user, **kwargs):
        if self.stop():
            raise ComputerError('capability_cancelled')
        claim = self.session.claim_model({'capability_context_sha256': digest([system, user])})
        result = self.client.complete(system, user, **kwargs)
        write_new(claim.with_suffix('.result.json'), {'ok': True})
        if self.stop():
            raise ComputerError('capability_cancelled')
        return result


class _MailSessionClient(_SessionClient):
    def complete(self, system, user, **kwargs):
        try:
            return super().complete(system, user, **kwargs)
        except ComputerError as exc:
            if str(exc) == 'capability_cancelled':
                from .mail_understanding import MailUnderstandingError
                raise MailUnderstandingError('task_cancelled') from exc
            raise


def _inspect(task, base):
    from .artifact_source import prepare_artifact_source
    from .task_history import inspect_task
    raw = read_record(task / 'result.json')
    view = inspect_task(task, base)
    if (raw.get('mail_analysis_schema') in (3, 4) and raw.get('ok') is True
            and view['state'] == 'succeeded'
            and any(Path(item['path']).suffix.lower() == '.eml' for item in view.get('artifacts', []))):
        from . import mail_understanding as mail
        try:
            # Use the workflow history actually verified, rather than trusting
            # a separate self-consistent mail plan to describe the saved draft.
            manifest = read_record(Path(view['workflow_root']) / 'manifest.json')
            mail.verify_task(task, base, manifest['steps'], raw)
        except (ValueError, OSError, KeyError, TypeError):
            view = {**view, 'state': 'needs_review', 'artifacts': []}
            raw = {**raw, 'error': 'mail_analysis_plan_changed'}
    artifacts = []
    for item in view.get('artifacts', []):
        if Path(item['path']).suffix.lower() in {'.docx', '.xlsx'}:
            prepare_artifact_source(item['path'], item['sha256'], base)
            artifacts.append({'path': item['path'], 'sha256': item['sha256']})
        elif (Path(item['path']).suffix.lower() == '.eml'
                and raw.get('mail_analysis_schema') in (3, 4)
                and raw.get('ok') is True and view['state'] == 'succeeded'
                and 'mail_understanding' in raw):
            # History has rechecked the draft receipt and exact bytes. A draft
            # is an output, never an eligible Office source for another task.
            artifacts.append({'path': item['path'], 'sha256': item['sha256']})
    # A prepared notepad payload is not a completed computer operation.
    ok = (raw.get('ok') is True and view['state'] == 'succeeded'
          and (bool(artifacts) or view['kind'] == 'research' and bool(view.get('artifacts'))))
    gap = view.get('capability_gap')
    result = {'ok': ok, 'state': view['state'], 'code': 'capability_verified' if ok else
            'capability_adapter_queued' if gap else raw.get('error', raw.get('reason', 'capability_not_completed')),
            'task_root': str(task), 'artifacts': artifacts,
            'skill': raw.get('skill'), 'gap': gap,
            'result_sha256': digest(raw)}
    if (raw.get('mail_analysis_schema') in (2, 3, 4) and not ok
            and result['code'] in {'mail_analysis_needs_input', 'mail_analysis_unsupported',
                                   'mail_analysis_review_rejected'}):
        if type(raw.get('reason')) is str:
            result['reason'] = _mail_reason(raw['reason'])
        if result['code'] == 'mail_analysis_needs_input' and not artifacts:
            result['state'] = 'needs_input'
    return result


def _mail_reason(value):
    return ' '.join(re.sub(r'[\x00-\x1f\x7f]', ' ', value).split())[:160]


def inspect_saved(run, *, task_base=TASK_BASE, computer_base=BASE):
    """Revalidate a saved capability outcome without running a task or model."""
    base = safe_root(task_base)
    computer = safe_root(computer_base)
    path = safe_root(run)
    session_root = path.parent.parent
    if (path.parent.name != 'goals' or session_root.parent != computer
            or re.fullmatch(r'computer-[0-9a-f]{32}', session_root.name) is None
            or re.fullmatch(r'[0-9a-f]{64}\.capability', path.name) is None
            or not path.is_dir()):
        raise ComputerError('capability_saved_scope_invalid')
    session = read_record(session_root / 'session.json')
    if (type(session) is not dict or session.get('schema') != 1
            or type(session.get('created')) not in (int, float)):
        raise ComputerError('capability_session_invalid')
    approval = path.name.removesuffix('.capability')
    plan = read_record(path.parent / (approval + '.json'))
    auto = type(plan) is dict and plan.get('schema') == 2
    clarified = type(plan) is dict and plan.get('schema') == 3
    mail_route = type(plan) is dict and plan.get('schema') == 4
    keys = {'schema', 'mode', 'instruction', 'source_ref', 'task_base', 'request_id', 'candidates', 'sha256'}
    if (type(plan) is not dict or set(plan) != keys | ({'source_refs'} if auto else {'clarification'} if clarified else {'capability'} if mail_route else set())
            or type(plan['schema']) is not int or plan['schema'] not in (1, 2, 3, 4) or plan['mode'] != 'capability'
            or auto and plan['source_ref'] is not None
            or plan['sha256'] != approval
            or digest({key: value for key, value in plan.items() if key != 'sha256'}) != approval
            or plan['task_base'] != str(base)
            or type(plan['request_id']) is not str
            or re.fullmatch(r'[0-9a-f]{32}', plan['request_id']) is None):
        raise ComputerError('capability_saved_plan_invalid')
    _source(plan['source_ref'], base)
    if mail_route:
        _mail_plan(plan, base)
    if auto:
        from . import capability_source_choice as choice
        choice.verify_refs(plan['source_refs'], base)
    if clarified:
        from .capability_clarification import verify
        verify(plan, path, base, computer, require_claim=True)
    claim = read_record(path / 'claim.json')
    expected_task = base / ('task-' + plan['request_id'])
    if (type(claim) is not dict or claim != {'approval': approval,
            'task_root': str(expected_task)}):
        raise ComputerError('capability_claim_changed')
    outcome = read_record(path / 'outcome.json')
    if type(outcome) is not dict:
        raise ComputerError('capability_result_changed')
    if outcome.get('before_start') is True:
        expected = {'ok': False, 'state': 'cancelled', 'code': 'capability_cancelled',
                    'artifacts': [], 'run': str(path), 'before_start': True}
        if auto and outcome.get('code') == 'capability_source_ambiguous':
            selected, proof = choice.checked(plan, path)
            if selected is not None:
                raise ComputerError('capability_source_choice_changed')
            expected.update(state='needs_input', code='capability_source_ambiguous', source_selection_sha256=proof)
        if outcome != expected or expected_task.exists():
            raise ComputerError('capability_result_changed')
        return outcome
    current = _inspect(expected_task, base)
    if mail_route:
        _bind_mail_result(plan, expected_task, current, base)
    if auto:
        choice.bind_result(plan, path, expected_task, current)
    if clarified:
        from .capability_source_choice import verify_task_source
        verify_task_source(plan['source_ref'], expected_task, current)
    current.update(run=str(path), approval=approval)
    if outcome != current:
        raise ComputerError('capability_result_changed')
    return outcome


def execute(session, plan, *, approval, stop=lambda: False, task_base=TASK_BASE,
            client=None, runner=None, pause_after_step=False):
    if type(pause_after_step) is not bool:
        raise ComputerError('pause_option_invalid')
    base = safe_root(task_base)
    if (type(plan) is not dict or plan.get('mode') != 'capability'
            or plan.get('sha256') != approval
            or digest({k:v for k,v in plan.items() if k != 'sha256'}) != approval
            or plan.get('task_base') != str(base)
            or read_record(session.root / 'goals' / (approval + '.json')) != plan):
        raise ComputerError('capability_approval_invalid')
    _source(plan['source_ref'], base)
    mail_route = plan.get('schema') == 4
    if pause_after_step and not mail_route:
        raise ComputerError('capability_pause_unsupported')
    if mail_route:
        _mail_plan(plan, base)
    auto = plan.get('schema') == 2
    if auto:
        from . import capability_source_choice as choice
        if plan['source_ref'] is not None:
            raise ComputerError('capability_source_conflict')
        choice.verify_refs(plan['source_refs'], base)
    run = safe_root(session.root / 'goals' / (approval + '.capability'))
    if plan.get('schema') == 3:
        from .capability_clarification import verify
        verify(plan, run, base, session.root.parent)
    try:
        run.mkdir()
    except FileExistsError:
        # No replay, including a crash before claim/result publication.
        if not (run / 'outcome.json').exists():
            return {'ok': False, 'state': 'unknown', 'code': 'capability_previous_run_unknown',
                    'artifacts': [], 'run': str(run)}
        return inspect_saved(run, task_base=base, computer_base=session.root.parent)
    task = base / ('task-' + plan['request_id'])
    if pause_after_step:
        write_new(run / 'pause.json', {'schema': 1, 'pause_after_step': True})
    write_new(run / 'claim.json', {'approval': approval, 'task_root': str(task)})
    if plan.get('schema') == 3:
        from .capability_clarification import claim
        claim(plan, run, base, session.root.parent)
    if stop():
        value = {'ok': False, 'state': 'cancelled', 'code': 'capability_cancelled',
                 'artifacts': [], 'run': str(run), 'before_start': True}
        write_new(run / 'outcome.json', value)
        return value
    from .artifact_source import prepare_artifact_task_root
    from .assistant import run_task, TASK_SECONDS
    if mail_route:
        from .mail_understanding import run_task
    from .model import CloudClient
    selected = client if client is not None else CloudClient(
        deadline=time.monotonic() + TASK_SECONDS, should_stop=stop)
    source_ref = plan['source_ref']
    if auto:
        source_ref, proof = choice.choose(plan, _SessionClient(selected, session, stop), run)
        choice.verify_refs(plan['source_refs'], base)
        if source_ref is None or stop():
            value = {'ok': False, 'state': 'needs_input', 'code': 'capability_source_ambiguous',
                     'artifacts': [], 'run': str(run), 'before_start': True, 'source_selection_sha256': proof}
            if stop():
                value = {'ok': False, 'state': 'cancelled', 'code': 'capability_cancelled',
                         'artifacts': [], 'run': str(run), 'before_start': True}
            write_new(run / 'outcome.json', value)
            return value
    prepare_artifact_task_root(task, base)
    with (task / 'request.txt').open('x', encoding='utf-8') as stream:
        stream.write(plan['instruction'])
    done = threading.Event()

    def watch():
        while not done.wait(.1):
            if stop():
                try:
                    (task / 'cancel.flag').touch(exist_ok=True)
                except OSError:
                    pass
                return

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    try:
        _source(source_ref, base)
        if stop():
            (task / 'cancel.flag').touch(exist_ok=True)
        session_client = (_MailSessionClient if mail_route else _SessionClient)(selected, session, stop)
        (runner or run_task)(plan['instruction'], base, task_root=task,
            client=session_client, source_artifact=source_ref,
            **({'pause_after_step': True} if pause_after_step else {}))
        value = _inspect(task, base)
        if mail_route:
            _bind_mail_result(plan, task, value, base)
        if auto:
            choice.bind_result(plan, run, task, value)
        if plan.get('schema') == 3:
            from .capability_source_choice import verify_task_source
            verify_task_source(plan['source_ref'], task, value)
        value.update(run=str(run), approval=approval)
        write_new(run / 'outcome.json', value)
        return value
    finally:
        done.set()
        watcher.join(timeout=1)


def describe_result(result):
    if result.get('ok'):
        if any(Path(item.get('path', '')).suffix.lower() == '.eml'
               for item in result.get('artifacts', [])):
            return '已核验本地文件与邮件草稿；草稿未发送，收件人留空。请核对内容后自行处理。'
        skill = result.get('skill')
        route = ('已匹配技能：' + str(skill.get('id', '已验证技能')) if isinstance(skill, dict)
                 else '已使用现有适配器')
        return route + ('\n已核验生成的原生 Office 成果与引用；请核对任务要求是否全部满足。'
                        if result.get('artifacts') else '\n公开资料报告已核验，记录保存在原任务中。')
    if result.get('gap'):
        return ('没有覆盖完整任务的已启用能力；已保存适配器开发请求。\n'
                '当前阶段：等待开发，不代表新能力已可用。\n'
                '后续：先找成熟技能或库→实现适配器→离线测试→独立审查→实机验证→限次启用。')
    code = str(result.get('code'))
    reasons = {
        'mail_analysis_needs_input': '邮件任务需要补充信息；未生成文件。请在任务框补充完整要求，再点击“按任务规划”并检查新预览；原任务记录保留。',
        'office_paused_after_verified_step': '已在文件保存并核验后暂停；剩余文件尚未开始。请到历史任务选择此任务，点击“继续剩余办公步骤”。恢复复用原邮件分析，不重复生成已完成文件。',
        'mail_analysis_unsupported': '当前邮件能力不能覆盖全部要求。可请求这一封邮件正文的整理、新Word/Excel或本地回复草稿。多封联合、附件和发送仍受限制：不联合多封、不读取原邮件附件内容、不发送；草稿仅可附加本次明确要求的新建Office文件。',
        'mail_analysis_content_limit': '所选邮件正文为空或超过完整处理上限；未截断正文，也未生成不完整报告。请改选范围合适的邮件。',
        'mail_analysis_requires_export': '需要有原文与导出证明的原始邮件Word；普通文档或邮件分析报告不能冒充原邮件。请在历史任务重新选择原邮件导出。',
        'mail_analysis_source_changed': '邮件来源证明与原导出已不一致；请核对原记录，当前任务没有重放。',
        'mail_analysis_review_rejected': '邮件整理未通过独立语义复核，可能缺少依据、遗漏要求或改变原意；未生成办公成果，请查看原任务中的审查原因。',
        'mail_analysis_schema_invalid': '模型未返回可核验的邮件整理结构；结果已保留，未自动重试或执行办公步骤。',
        'mail_analysis_reply_invalid': '模型回复草稿的结构、空收件人或附件引用无法核验；已停止，未生成文件。请查看原任务记录。',
        'mail_analysis_scope_invalid': '模型给出的正文范围无法核验；未生成文件，请明确需要整理的人员、主题或日期。',
        'mail_analysis_schedule_missing': '整理结果遗漏或改变了原文星期日程；已停止，未继续生成文件。请核对原任务记录，不会自动重试。',
        'task_cancelled': '任务已停止；未派发的步骤不会继续，已有成果和模型调用记录保留。',
        'workbook_merged_unsupported': '工作簿包含当前资料读取器不支持的合并结构，不能完整读取；可使用保留版式的编辑入口，或开发该布局的适配器。',
        'workbook_merged_cells_unsupported': '工作簿含不支持的合并单元格，不能静默丢弃；需选择已有编辑能力或补充布局适配器。',
        'edited_table_totals_unsupported': '这份编辑后的表格包含合计或公式，当前复制技能不能完整保留；已停止，未静默丢掉合计行。需使用公式感知的工作簿适配器。',
        'edited_table_scope_invalid': '修改涉及标题、其他工作表或已支持表格范围之外；当前完整表格接力支持既有表头和数据区，需要扩展布局适配器。',
        'capability_previous_run_unknown': '此前执行已开始但没有完整结果回执；先核对原任务记录和文件，不能重新发出同一操作。',
        'capability_source_ambiguous': '无法仅凭文件名和格式确定一份输入，或任务需要多份联合处理。请明确输入成果，或使用批量自然语言编辑入口；尚未执行办公操作。',
        'capability_cancelled': '任务已取消；尚未派发的办公操作不会执行，已发生的模型费用仍计入共享账本。',
        'session_model_call_limit': '本次电脑会话已达到六次模型调用上限；当前任务不再继续调用。',
        'model_budget_exceeded': '共享 API 预算不足；等待预算允许后再建立新任务，不能解除未知费用预留。',
        'skill_gate_invalid': '技能验证已失效；需重新测试、审查和实机验证后才能启用。',
    }
    detail = ''
    if code in {'mail_analysis_needs_input', 'mail_analysis_unsupported', 'mail_analysis_review_rejected'} and type(result.get('reason')) is str:
        detail = '\n模型提示（请结合原文核对）：' + _mail_reason(result['reason'])
    return ('任务尚未完成；已发出的操作不会自动重放。\n原因：' +
            reasons.get(code, '当前能力执行或成果核验未通过；请查看原任务记录，确认已完成内容及缺失回执。') +
            detail + '\n原因代码：' + code)
