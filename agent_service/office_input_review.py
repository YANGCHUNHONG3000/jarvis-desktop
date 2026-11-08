"""Read-only local review of verified Office task inputs, before cloud planning."""
import json


def validate_instruction(draft):
    from .office_intent import text
    return text(draft.strip(), 1000)


def validate_for_planning(verified):
    from .office_intent import _context
    instruction = validate_instruction(verified['payload']['draft'])
    # Same target IDs, metadata and context limit as the existing planner.
    _context(instruction, verified['review_sources'])


def location_label(at):
    if 'sheet' in at:
        return f"工作表 {json.dumps(at['sheet'], ensure_ascii=False)} · {at['cell']}"
    if 'table_index' in at:
        return (f"表格 {at['table_index'] + 1} · 行 {at['row_index'] + 1} · "
                f"列 {at['column_index'] + 1} · 段落 {at['paragraph_index'] + 1}")
    return f"正文段落 {at['paragraph_index'] + 1}"


def describe(verified):
    payload, review = verified['payload'], verified['review']
    if not review or len(review) != len(payload['records']):
        raise ValueError('office_review_missing')
    lines = ['任务说明（原文）：', payload['draft'], '',
             '以下为本机核验得到的选中范围与原值。Word序号从1开始。',
             '原值按JSON表示：null为空值，字符串有引号；公式等只读位置不会成为修改目标。', '']
    for index, (record, item) in enumerate(zip(payload['records'], review), 1):
        if record['path'] != item['path'] or not item['rows']:
            raise ValueError('office_review_mismatch')
        lines.append(f"文件 {index}：{record['path']}")
        for row in item['rows']:
            tag = '可编辑' if row['editable'] is True else '只读'
            lines.append(f"  {location_label(row['at'])} [{tag}]")
            lines.append('  原值：' + json.dumps(row['value'], ensure_ascii=False))
        lines.append('')
    lines.append('尚未调用模型或修改文件。继续规划会上传任务说明与所选范围内容，仍需另行批准修改差异。')
    return '\n'.join(lines)


def open_review(root, verified, on_continue):
    import tkinter as tk
    from tkinter import ttk
    from .office_scope_panel import _modal
    content = describe(verified)
    win, close = _modal(root, '核对办公任务输入', '880x600')
    area = ttk.Frame(win)
    area.pack(fill='both', expand=True, padx=12, pady=12)
    box = tk.Text(area, wrap='word')
    scroll = ttk.Scrollbar(area, orient='vertical', command=box.yview)
    box.configure(yscrollcommand=scroll.set)
    scroll.pack(side='right', fill='y')
    box.pack(side='left', fill='both', expand=True)
    box.insert('1.0', content)
    box.configure(state='disabled')
    actions = ttk.Frame(win)
    actions.pack(fill='x', padx=12, pady=(0, 12))
    def proceed():
        close()
        on_continue()
    ttk.Button(actions, text='返回修改说明/范围', command=close).pack(side='left')
    ttk.Button(actions, text='继续云端规划', command=proceed).pack(side='right')
