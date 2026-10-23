"""Bounded local capture selection. Candidates are not evidence until verified."""
from copy import deepcopy

from .computer import BASE, ComputerError, read_record, safe_root


def recent_candidates(*, base=BASE, stop=lambda: False):
    base = safe_root(base)
    if not base.exists():
        return []
    # Later Office-only sessions must not hide the last useful browser capture.
    # Metadata scanning is bounded independently of the 30 displayed choices.
    sessions = sorted(base.glob('computer-*'), key=lambda p: p.stat().st_mtime_ns,
                      reverse=True)[:200]
    paths = []
    for session in sessions:
        if stop():
            raise ComputerError('comparison_selection_cancelled')
        if safe_root(session).parent != base:
            continue
        paths.extend(sorted((session / 'goals').glob('*.run'),
                            key=lambda p: p.stat().st_mtime_ns, reverse=True)[:30])
    rows = []
    for run in sorted(paths, key=lambda p: p.stat().st_mtime_ns, reverse=True)[:300]:
        if stop():
            raise ComputerError('comparison_selection_cancelled')
        try:
            run = safe_root(run)
            if run.parent.parent.parent != base:
                continue
            plan, outcome = read_record(run.with_suffix('.json')), read_record(run / 'outcome.json')
            goal = plan.get('goal', {})
            failed = (outcome.get('ok') is False and 'comparison' in goal
                      and outcome.get('code') == 'agent_no_safe_next_action'
                      and outcome.get('state') in {'partial', 'stopped'}
                      and outcome.get('engine') == 'observed_agent'
                      and outcome.get('mode') == 'adapters' and outcome.get('artifacts') == [])
            if outcome.get('ok') is not True and not failed:
                continue
            if 'recovery' in plan:
                recovery = plan['recovery']
                if (('comparison' in goal and not failed) or type(recovery) is not dict
                        or recovery.get('kind') != 'research' or recovery.get('version') != 2
                        or type(recovery.get('depth')) is not int or recovery['depth'] != 1):
                    continue
            if 'comparison' not in goal:
                reads = outcome.get('source_reads')
                if (outcome.get('engine') != 'observed_agent'
                        or type(reads) is not int or not 1 <= reads <= 2
                        or type(goal.get('queries')) is not list
                        or not 1 <= len(goal['queries']) <= 2
                        or reads == 1 and len(goal['queries']) == 1):
                    continue
            elif goal['comparison'].get('evidence_version') not in {3, 4}:
                continue
            if failed and (goal['comparison'].get('evidence_version') == 4
                           and goal['comparison'].get('seed', {}).get('version') not in {2, 4, 5}
                           or 'recovery' in plan and goal['comparison'].get('evidence_version') != 4):
                continue
            rows.append({'run': str(run), 'title': str(goal.get('title', ''))[:200],
                         'kind': 'comparison_failed' if failed else 'research' if 'comparison' not in goal else 'comparison'})
        except (OSError, ValueError, TypeError, AttributeError):
            continue
        if len(rows) == 30:
            break
    return rows


def select_source(run, *, stop=lambda: False):
    from .comparison_seed import create_manifest, load_seed
    from .comparison_panel import fields_from_goal, build_goal
    if stop():
        raise ComputerError('comparison_selection_cancelled')
    from .comparison_crosssite import followup_manifest
    manifest = followup_manifest(run)
    captures = load_seed(manifest)
    goal = read_record(safe_root(run).with_suffix('.json'))['goal']
    # Rebuild only editable fields; a historical seed/recovery is never reused as a plan.
    fields = (fields_from_goal(goal) if 'comparison' in goal else {
        'title': goal['title'], 'dimensions': '主要能力',
        'min_sources': '2', 'min_domains': '2'})
    fields.update(query1=manifest['queries'][0], query2=(manifest['queries'][1]
                  if len(manifest['queries']) > 1 else ''))
    if stop():
        raise ComputerError('comparison_selection_cancelled')
    return {'manifest': manifest, 'goal': build_goal(fields), 'fields': fields,
            'pages': [{'url': item['url'], 'query_index': item['query_index'],
                       **({'captured_at': record['observation']['captured_at']}
                          if manifest.get('version') in {2, 4, 5} else {})}
                      for item, record in captures]}


def prepare_selected(session, instruction, goal, selection, *, stop=lambda: False):
    from .comparison_seed import load_seed
    from .comparison_crosssite import prepare_followup
    if stop():
        raise ComputerError('comparison_selection_cancelled')
    manifest = selection['manifest']
    load_seed(manifest)  # Selection-to-preview changes must fail, not bless new bytes.
    plan = prepare_followup(session, instruction, goal, manifest['run'], manifest['source_ids'])
    if plan['goal']['comparison']['seed'] != manifest:
        raise ComputerError('comparison_seed_changed')
    if stop():
        raise ComputerError('comparison_selection_cancelled')
    return plan


def source_description(selection):
    kind = ('未完成对比的已核验捕获' if selection['manifest'].get('version') in {4, 5} else
            '普通资料检索' if selection['manifest'].get('version') == 2 else '历史对比')
    return (kind + '来源：' + selection['manifest']['run'] + '\n' +
            '\n'.join(f"{i}. {p['url']}" +
                      (f"（采集时间：{p['captured_at']}）" if 'captured_at' in p else '')
                      for i, p in enumerate(selection['pages'], 1)) +
            '\n历史捕获不会重新读取，旧结论不沿用；批准后重新复核，缺证时有界补查。')


def open_selector(parent, rows, on_select):
    import tkinter as tk
    from tkinter import ttk
    from .office_scope_panel import _modal
    rows = deepcopy(rows)
    win, close = _modal(parent, '选择历史对比来源', '820x420')
    ttk.Label(win, text='历史来源候选（待核验，含受支持的未完成对比）。选择后检查原文件与回执，不调用 API。',
              wraplength=780).pack(fill='x', padx=12, pady=8)
    box = tk.Listbox(win, exportselection=False)
    box.pack(fill='both', expand=True, padx=12)
    for row in rows:
        prefix = '未完成对比·待核验' if row.get('kind') == 'comparison_failed' else '资料检索' if row.get('kind') == 'research' else '对比'
        box.insert(tk.END, f"[{prefix}] {row['title']}")
    status = tk.StringVar(value='支持原有成功来源及受限的未完成对比捕获。失败来源须完整核验、无 Office 派发或未知作用；原查询保留，新任务须另行预览批准。列表不代表已通过核验。')
    ttk.Label(win, textvariable=status, wraplength=780).pack(fill='x', padx=12, pady=8)
    def choose():
        selected = box.curselection()
        if selected:
            on_select(rows[selected[0]]['run'])
            close()
    def changed(_=None):
        selected = box.curselection()
        accept.configure(state='normal' if selected else 'disabled')
        if selected:
            status.set(rows[selected[0]]['run'])
    accept = ttk.Button(win, text='核验并使用这些来源', command=choose, state='disabled')
    accept.pack(pady=10)
    box.bind('<<ListboxSelect>>', changed)
    return win
