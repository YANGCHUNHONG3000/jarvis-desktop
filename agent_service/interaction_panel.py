"""Local effect-envelope editor; no model, browser or file write on opening."""
import json
from pathlib import Path
from urllib.parse import urlsplit

from .public_site_profiles import PUBLIC_SITES


def scalar(value):
    try:
        result = json.loads(value)
    except ValueError:
        return value
    if type(result) not in (str, int, float, bool, type(None)):
        raise ValueError('单元格只支持文字、数字、布尔值或 null')
    return result


def build_request(values):
    browser, edits = None, []
    profile = PUBLIC_SITES.get(values.get('site_mode'))
    if values['url'].strip() or profile:
        from .browser_interaction import validate_config
        url = values['url'].strip()
        if profile:
            site, start = profile
            browser = validate_config({'mode':'public_search','site':site,
                'start_url':url or start, 'query':values['query'],
                'target_url':values.get('target_url','').strip(),
                'required_text':[values['expected'].strip() or values['query'].strip()]})
        elif values.get('site_mode', '').startswith('GitHub'):
            parts = urlsplit(url).path.strip('/').split('/')
            if len(parts) != 2:
                raise ValueError('请填写完整 GitHub 仓库网址，例如 https://github.com/owner/repo')
            owner, repo = parts
            browser = validate_config({'mode':'github_repo','start_url':url,
                'query':f'{repo} user:{owner}', 'required_text':[values['expected'].strip() or repo],
                'human_login': values['site_mode'] == 'GitHub仓库（人工登录）'})
        else:
            browser = validate_config({'mode':'discover_query','start_url':url,'query':values['query'],
                'javascript':True,
                'required_text':[values['expected']],
                'allow_loopback':urlsplit(url).hostname in {'127.0.0.1','localhost','::1'}})
    if values['word'].strip():
        edits.append({'path':values['word'], 'changes':[{'paragraph_index':int(values['paragraph'])-1,
                      'before':values['word_before'],'after':values['word_after']}]})
    if values['excel'].strip():
        edits.append({'path':values['excel'], 'changes':[{'sheet':values['sheet'],'cell':values['cell'].upper(),
                      'before':scalar(values['excel_before']),'after':scalar(values['excel_after'])}]})
    if browser is None and not edits:
        raise ValueError('至少选择一个网页或文件')
    request = {'browser':browser,'edits':edits}
    if browser and browser.get('mode') == 'github_repo' and values.get('repository_document', True):
        request['repository_document'] = True
    if browser and browser.get('mode') == 'public_search' and values.get('page_office', True):
        request['page_office'] = True
    return request


def open_editor(parent, on_save, initial=None):
    import tkinter as tk
    from tkinter import ttk, filedialog
    top = tk.Toplevel(parent)
    top.title('网页交互与已有文件编辑范围')
    top.geometry('800x780')
    tabs = ttk.Notebook(top)
    tabs.pack(fill='both',expand=True,padx=10,pady=8)
    fields = {}
    groups = [
        ('网页查询', [('url','查询页面网址（支持 JavaScript；自动识别查询输入框和按钮）',''),
                    ('query','查询文字（GitHub按仓库网址自动生成，可留空）',''),
                    ('target_url','公开检索：目标页面网址（可留空，按主界面的任务说明选择结果）',''),
                    ('expected','结果页面必须出现的文字（公开检索可留空使用查询文字）','')]),
        ('Word 编辑', [('word','已有 .docx 文件（留空跳过）',''),('paragraph','段落序号（从 1 开始）','1'),
                     ('word_before','该段当前完整文字',''),('word_after','修改后的完整文字','')]),
        ('Excel 编辑', [('excel','已有 .xlsx 文件（留空跳过）',''),('sheet','工作表名称','Sheet1'),
                      ('cell','单元格位置','B2'),('excel_before','当前值（纯数字按数字处理，文字数字请加双引号）',''),
                      ('excel_after','修改后的值','')])]
    for label, entries in groups:
        page = ttk.Frame(tabs,padding=12)
        tabs.add(page,text=label)
        if label == '网页查询':
            ttk.Label(page,text='网站类型').pack(anchor='w')
            fields['site_mode'] = tk.StringVar(value=(initial or {}).get('site_mode','普通网页'))
            site_selector = ttk.Combobox(page,textvariable=fields['site_mode'],state='readonly',values=(
                '普通网页','GitHub仓库（人工登录）','GitHub公开仓库（免登录）',*PUBLIC_SITES))
            site_selector.pack(fill='x')
            def site_changed(_=None):
                selected = PUBLIC_SITES.get(fields['site_mode'].get())
                if selected:
                    fields['url'].set(selected[1])
            site_selector.bind('<<ComboboxSelected>>', site_changed)
            fields['repository_document'] = tk.BooleanVar(value=(initial or {}).get('repository_document', True))
            ttk.Checkbutton(page, text='GitHub 读取后生成 Word 并接到下一任务（本机整理，不耗 API）',
                            variable=fields['repository_document']).pack(anchor='w', pady=6)
            fields['page_office'] = tk.BooleanVar(value=(initial or {}).get('page_office', True))
            ttk.Checkbutton(page, text='公开检索读取后生成 Word＋Excel 并接到下一任务（本机整理）',
                            variable=fields['page_office']).pack(anchor='w', pady=2)
        for key, label, default in entries:
            ttk.Label(page,text=label,wraplength=690).pack(anchor='w',pady=(8,2))
            row = ttk.Frame(page)
            row.pack(fill='x')
            variable = tk.StringVar(value=(initial or {}).get(key,default))
            fields[key] = variable
            ttk.Entry(row,textvariable=variable).pack(side='left',fill='x',expand=True)
            if key in {'word','excel'}:
                suffix = '.docx' if key=='word' else '.xlsx'
                def choose(var=variable,suffix=suffix):
                    path = filedialog.askopenfilename(parent=top,filetypes=[('Office 文件','*'+suffix)])
                    if path:
                        var.set(str(Path(path)))
                ttk.Button(row,text='选择文件…',command=choose).pack(side='left')
    ttk.Label(top,text='在副本中编辑并保留原件；只支持普通段落和普通单元格值。若填写网页条件，办公编辑在网页核验后执行。',
              wraplength=730).pack(anchor='w',padx=14,pady=8)
    status = tk.StringVar(value='保存范围后，返回连续任务预览并批准。')
    ttk.Label(top,textvariable=status,wraplength=730).pack(anchor='w',padx=14)
    def save():
        try:
            values = {k:v.get() for k,v in fields.items()}
            request = build_request(values)
            on_save(request,values)
            top.destroy()
        except (ValueError,TypeError) as exc:
            status.set('范围未保存：'+str(exc))
    ttk.Button(top,text='保存操作范围',command=save).pack(pady=12)
    return top
