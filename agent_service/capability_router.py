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


def prepare(session, instruction, *, source_ref=None, task_base=TASK_BASE):
    from .skill_matching import discover_candidates
    if type(instruction) is not str or not 1 <= len(instruction.strip()) <= 4000:
        raise ComputerError('instruction_invalid')
    base = safe_root(task_base)
    _source(source_ref, base)
    found = discover_candidates(base.parent / 'capabilities', base, query=instruction)
    return _save_goal(session, {'schema': 1, 'mode': 'capability',
        'instruction': instruction.strip(), 'source_ref': deepcopy(source_ref),
        'task_base': str(base), 'request_id': uuid.uuid4().hex,
        'candidates': found['candidates']})


def describe(plan):
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


def _inspect(task, base):
    from .artifact_source import prepare_artifact_source
    from .task_history import inspect_task
    raw = read_record(task / 'result.json')
    view = inspect_task(task, base)
    artifacts = []
    for item in view.get('artifacts', []):
        if Path(item['path']).suffix.lower() in {'.docx', '.xlsx'}:
            prepare_artifact_source(item['path'], item['sha256'], base)
            artifacts.append({'path': item['path'], 'sha256': item['sha256']})
    # A prepared notepad payload is not a completed computer operation.
    ok = (raw.get('ok') is True and view['state'] == 'succeeded'
          and (bool(artifacts) or view['kind'] == 'research' and bool(view.get('artifacts'))))
    gap = view.get('capability_gap')
    return {'ok': ok, 'state': view['state'], 'code': 'capability_verified' if ok else
            'capability_adapter_queued' if gap else raw.get('error', raw.get('reason', 'capability_not_completed')),
            'task_root': str(task), 'artifacts': artifacts,
            'skill': raw.get('skill'), 'gap': gap,
            'result_sha256': digest(raw)}


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
    if (type(plan) is not dict or set(plan) != {'schema', 'mode', 'instruction',
            'source_ref', 'task_base', 'request_id', 'candidates', 'sha256'}
            or plan['schema'] != 1 or plan['mode'] != 'capability'
            or plan['sha256'] != approval
            or digest({key: value for key, value in plan.items() if key != 'sha256'}) != approval
            or plan['task_base'] != str(base)
            or type(plan['request_id']) is not str
            or re.fullmatch(r'[0-9a-f]{32}', plan['request_id']) is None):
        raise ComputerError('capability_saved_plan_invalid')
    _source(plan['source_ref'], base)
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
        if outcome != expected or expected_task.exists():
            raise ComputerError('capability_result_changed')
        return outcome
    current = _inspect(expected_task, base)
    current.update(run=str(path), approval=approval)
    if outcome != current:
        raise ComputerError('capability_result_changed')
    return outcome


def execute(session, plan, *, approval, stop=lambda: False, task_base=TASK_BASE,
            client=None, runner=None):
    base = safe_root(task_base)
    if (type(plan) is not dict or plan.get('mode') != 'capability'
            or plan.get('sha256') != approval
            or digest({k:v for k,v in plan.items() if k != 'sha256'}) != approval
            or plan.get('task_base') != str(base)
            or read_record(session.root / 'goals' / (approval + '.json')) != plan):
        raise ComputerError('capability_approval_invalid')
    _source(plan['source_ref'], base)
    run = safe_root(session.root / 'goals' / (approval + '.capability'))
    try:
        run.mkdir()
    except FileExistsError:
        # No replay, including a crash before claim/result publication.
        if not (run / 'outcome.json').exists():
            return {'ok': False, 'state': 'unknown', 'code': 'capability_previous_run_unknown',
                    'artifacts': [], 'run': str(run)}
        return inspect_saved(run, task_base=base, computer_base=session.root.parent)
    task = base / ('task-' + plan['request_id'])
    write_new(run / 'claim.json', {'approval': approval, 'task_root': str(task)})
    if stop():
        value = {'ok': False, 'state': 'cancelled', 'code': 'capability_cancelled',
                 'artifacts': [], 'run': str(run), 'before_start': True}
        write_new(run / 'outcome.json', value)
        return value
    from .artifact_source import prepare_artifact_task_root
    from .assistant import run_task, TASK_SECONDS
    from .model import CloudClient
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
        selected = client if client is not None else CloudClient(
            deadline=time.monotonic() + TASK_SECONDS, should_stop=stop)
        _source(plan['source_ref'], base)
        if stop():
            (task / 'cancel.flag').touch(exist_ok=True)
        (runner or run_task)(plan['instruction'], base, task_root=task,
            client=_SessionClient(selected, session, stop), source_artifact=plan['source_ref'])
        value = _inspect(task, base)
        value.update(run=str(run), approval=approval)
        write_new(run / 'outcome.json', value)
        return value
    finally:
        done.set()
        watcher.join(timeout=1)


def describe_result(result):
    if result.get('ok'):
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
        'workbook_merged_unsupported': '工作簿包含当前资料读取器不支持的合并结构，不能完整读取；可使用保留版式的编辑入口，或开发该布局的适配器。',
        'workbook_merged_cells_unsupported': '工作簿含不支持的合并单元格，不能静默丢弃；需选择已有编辑能力或补充布局适配器。',
        'edited_table_totals_unsupported': '这份编辑后的表格包含合计或公式，当前复制技能不能完整保留；已停止，未静默丢掉合计行。需使用公式感知的工作簿适配器。',
        'edited_table_scope_invalid': '修改涉及标题、表头或已支持表格范围之外；当前完整表格接力只支持数据区，需要扩展布局适配器。',
        'capability_previous_run_unknown': '此前执行已开始但没有完整结果回执；先核对原任务记录和文件，不能重新发出同一操作。',
        'capability_cancelled': '任务已取消；开始前取消不会调用模型或操作文件。',
        'session_model_call_limit': '本次电脑会话已达到六次模型调用上限；当前任务不再继续调用。',
        'model_budget_exceeded': '共享 API 预算不足；等待预算允许后再建立新任务，不能解除未知费用预留。',
        'skill_gate_invalid': '技能验证已失效；需重新测试、审查和实机验证后才能启用。',
    }
    return ('任务尚未完成；已发出的操作不会自动重放。\n原因：' +
            reasons.get(code, '当前能力执行或成果核验未通过；请查看原任务记录，确认已完成内容及缺失回执。') +
            '\n原因代码：' + code)
