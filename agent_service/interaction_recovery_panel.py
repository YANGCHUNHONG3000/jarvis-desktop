"""Local saved-task discovery and explicit continuation selection."""
from copy import deepcopy
from pathlib import Path

from .computer import BASE, ComputerError, ComputerSession, safe_root


def recent_candidates(*, base=BASE, stop=lambda:False):
    from .interaction_recovery import list_candidates
    base = safe_root(base)
    if not base.exists():
        return []
    sessions = sorted(base.glob('computer-*'), key=lambda p:p.stat().st_mtime_ns, reverse=True)[:20]
    rows = []
    for root in sessions:
        if stop():
            raise ComputerError('recovery_cancelled')
        try:
            if safe_root(root).parent != base:
                continue
            session = ComputerSession(root)
            candidates = list_candidates(session)
        except (OSError,ValueError):
            continue
        rows.extend({**row,'session_root':str(session.root)} for row in candidates)
        if len(rows) >= 30:
            break
    return rows[:30]


def prepare_selected(row, *, host=None, stop=lambda:False, base=BASE):
    from .interaction_recovery import prepare
    if stop():
        raise ComputerError('recovery_cancelled')
    root = safe_root(row['session_root'])
    if root.parent != safe_root(base) or not root.name.startswith('computer-'):
        raise ComputerError('recovery_session_scope_invalid')
    session = ComputerSession(root,host=host)
    result = prepare(session,row['run'])
    if stop():
        raise ComputerError('recovery_cancelled')
    return {**result,'session_root':str(session.root)}


def candidate_label(row):
    state = '可恢复' if row['eligible'] else '已阻止'
    if row.get('interrupted_without_outcome'):
        state = '异常退出 · '+state
    return (f"{state} · 已完成{len(row.get('completed_edits',[]))}/待完成{len(row.get('remaining_edits',[]))}"
            f" · {row.get('instruction','')[:70]}")


def candidate_detail(row):
    if row is None:
        return '选择任务后核验来源和原生回执。'
    from .task_diagnostics import explain_recovery

    detail = ('原任务未写最终结果；已确认原执行已停止，已完成步骤的回执已核验。\n'
              if row.get('eligible') and row.get('interrupted_without_outcome') else '')
    detail += explain_recovery(row)
    code = row.get('code')
    if code:
        detail += f'\n原始代码：{code}'
    return f"任务：{row.get('instruction','')}\n记录：{row['run']}\n"+detail


def open_selector(root, rows, on_selected):
    import tkinter as tk
    from tkinter import ttk
    from .office_scope_panel import _modal
    rows = deepcopy(rows)
    win,close = _modal(root,'恢复连续任务','820x420')
    ttk.Label(win,text='只续做核验安全的未完成步骤；已完成成果保留。未知执行结果不会重跑。',
              wraplength=780).pack(fill='x',padx=12,pady=8)
    box = tk.Listbox(win,exportselection=False)
    box.pack(fill='both',expand=True,padx=12)
    for row in rows:
        box.insert(tk.END,candidate_label(row))
    detail = tk.Text(win,height=8,wrap='word',state='disabled')
    detail.pack(fill='x',padx=12,pady=6)
    def show(_event=None):
        indices = box.curselection()
        row = rows[indices[0]] if indices else None
        text = candidate_detail(row)
        detail.configure(state='normal'); detail.delete('1.0','end'); detail.insert('1.0',text)
        detail.configure(state='disabled')
        accept.configure(state='normal' if row and row['eligible'] else 'disabled')
    def choose():
        indices = box.curselection()
        if indices and rows[indices[0]]['eligible']:
            on_selected(deepcopy(rows[indices[0]]))
            close()
    buttons=ttk.Frame(win); buttons.pack(fill='x',padx=12,pady=8)
    accept=ttk.Button(buttons,text='预览剩余步骤',command=choose,state='disabled'); accept.pack(side='right')
    ttk.Button(buttons,text='取消',command=close).pack(side='right',padx=8)
    box.bind('<<ListboxSelect>>',show)
    show()
    return win
