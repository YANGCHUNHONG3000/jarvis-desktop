"""Local public-comparison objective editor; opening and previewing use no API."""


def build_goal(values):
    from .computer_agent import validate_goal
    queries = [values['query1'].strip()]
    if values.get('query2', '').strip():
        queries.append(values['query2'].strip())
    return validate_goal({'queries': queries, 'title': values['title'].strip(),
        'min_sources': int(values['min_sources']), 'required_terms': [],
        'outputs': ['spreadsheet', 'document'],
        'comparison': {'dimensions': [part.strip() for part in values['dimensions'].split('|')],
                       'min_domains': int(values['min_domains']), 'evidence_version': 3}})


def fields_from_goal(goal):
    return {'query1': goal['queries'][0],
        'query2': goal['queries'][1] if len(goal['queries']) > 1 else '',
        'title': goal['title'], 'dimensions': ' | '.join(goal['comparison']['dimensions']),
        'min_sources': str(goal['min_sources']),
        'min_domains': str(goal['comparison']['min_domains'])}


def open_editor(parent, on_save, initial=None):
    import tkinter as tk
    from tkinter import ttk
    top = tk.Toplevel(parent)
    top.title('多结果对比范围')
    top.geometry('740x570')
    top.transient(parent)
    top.grab_set()
    body = ttk.Frame(top, padding=14)
    body.pack(fill='both', expand=True)
    ttk.Label(body, text='批准后检索实际结果并读取来源，再按维度选取原文，生成 Excel 和 Word。',
              wraplength=690).pack(anchor='w')
    fields = {}
    entries = [('query1', '检索主题一（可使用 site: 限定网站）', ''),
        ('query2', '检索主题二（可留空；跨网站对比建议填写）', ''),
        ('title', '文档标题（最多 200 字）', ''),
        ('dimensions', '对比维度（1～3 个，以 | 分隔；每个最多 60 字）', ''),
        ('min_sources', '至少读取页面数（2～4；含同源页面）', '2'),
        ('min_domains', '至少核验独立来源组数（1～2；同源译文只算一组）', '2')]
    for key, label, default in entries:
        ttk.Label(body, text=label).pack(anchor='w', pady=(9, 2))
        variable = tk.StringVar(value=(initial or {}).get(key, default))
        fields[key] = variable
        if key in {'min_sources', 'min_domains'}:
            widget = ttk.Combobox(body, textvariable=variable, state='readonly',
                                 values=('2', '3', '4') if key == 'min_sources' else ('1', '2'))
        else:
            widget = ttk.Entry(body, textvariable=variable)
        widget.pack(fill='x')
    ttk.Label(body, text='缺证时自动补查和替换候选，最多两轮复核；每个独立来源组须覆盖全部维度后才交付。仍受现有预算和次数限制，模型判断需人工确认。',
              wraplength=690).pack(anchor='w', pady=10)
    status = tk.StringVar(value='保存后返回本地预览，批准才开始执行。')
    ttk.Label(body, textvariable=status, wraplength=690).pack(anchor='w')
    def save():
        try:
            values = {key: var.get() for key, var in fields.items()}
            goal = build_goal(values)
            on_save(goal, values)
            top.destroy()
        except (ValueError, TypeError, KeyError) as exc:
            status.set('范围未保存：' + str(exc))
    ttk.Button(body, text='保存对比范围', command=save).pack(pady=10)
    return top
