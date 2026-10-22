"""Small modal selectors for choosing Office files and editable locations."""
from copy import deepcopy
from pathlib import Path
import tkinter as tk
from tkinter import ttk


def _modal(root, title, size):
    win = tk.Toplevel(root)
    win.title(title)
    win.transient(root)
    win.geometry(size)
    win.grab_set()
    def close():
        try:
            win.grab_release()
        except tk.TclError:
            pass
        win.destroy()
    win.protocol("WM_DELETE_WINDOW", close)
    return win, close


def _range_nodes(visible_nodes, anchor, target):
    """Return the inclusive visible-row range between two tree item IDs."""
    try:
        start, end = visible_nodes.index(anchor), visible_nodes.index(target)
    except ValueError:
        return (target,)
    lo, hi = sorted((start, end))
    return tuple(visible_nodes[lo:hi + 1])


def _set_visible_range(tree, visible_nodes, anchor, target):
    nodes = _range_nodes(visible_nodes, anchor, target)
    tree.selection_set(*nodes)
    tree.focus(target)


def _visible_tree_nodes(tree):
    result = []
    def visit(parent):
        for node in tree.get_children(parent):
            result.append(node)
            if tree.item(node, "open"):
                visit(node)
    visit("")
    return result


def _bind_shift_range(tree):
    anchor = {"item": None}
    def clicked(event):
        # Ctrl-click adds/removes a row; preserve the previous range anchor.
        if not (event.state & (0x0004 | 0x0001)):
            item = tree.identify_row(event.y)
            if item:
                anchor["item"] = item
    tree.bind("<Button-1>", clicked, add="+")

    def shift(key):
        visible = _visible_tree_nodes(tree)
        if not visible:
            return "break"
        current = tree.focus() or (tree.selection()[-1] if tree.selection() else visible[0])
        try:
            position = visible.index(current)
        except ValueError:
            position = 0
        target_position = {"Up": max(0, position - 1), "Down": min(len(visible) - 1, position + 1),
                           "Home": 0, "End": len(visible) - 1}[key]
        target = visible[target_position]
        if anchor["item"] not in visible:
            anchor["item"] = current if current in visible else target
        _set_visible_range(tree, visible, anchor["item"], target)
        return "break"
    for key in ("Up", "Down", "Home", "End"):
        tree.bind(f"<Shift-{key}>", lambda event, key=key: shift(key), add="+")
    return anchor


def open_file_selector(root, records, on_selected):
    records = deepcopy(list(records))
    if not records:
        raise ValueError("没有可接入的 Office 文件。")
    if len(records) > 32:
        raise ValueError("一次最多选择 32 个 Office 文件。")
    win, close = _modal(root, "选择 Office 文件", "760x440")
    ttk.Label(win, text="选择最多 4 个文件接入编辑。", anchor="w").pack(fill="x", padx=12, pady=(12, 6))
    files_area = ttk.Frame(win)
    files_area.pack(fill="both", expand=True, padx=12, pady=4)
    box = tk.Listbox(files_area, selectmode=tk.EXTENDED, exportselection=False)
    xscroll = ttk.Scrollbar(files_area, orient="horizontal", command=box.xview)
    box.configure(xscrollcommand=xscroll.set)
    box.pack(fill="both", expand=True)
    xscroll.pack(fill="x")
    for i, rec in enumerate(records, 1):
        path = str(rec.get("path", ""))
        kind = "Word" if Path(path).suffix.lower() in (".doc", ".docx", ".rtf") else "Excel"
        box.insert(tk.END, f"{i}. {kind}  {Path(path).name}  —  {path}")
    if len(records) <= 4:
        for i in range(len(records)):
            box.selection_set(i)
    status = ttk.Label(win, text="", anchor="w")
    status.pack(fill="x", padx=12, pady=4)
    def accept():
        indices = box.curselection()
        if not indices:
            status.configure(text="请至少选择一个文件。")
            return
        if len(indices) > 4:
            status.configure(text="最多选择 4 个文件。")
            return
        on_selected(deepcopy([records[i] for i in indices]))
        close()
    buttons = ttk.Frame(win)
    buttons.pack(fill="x", padx=12, pady=(4, 12))
    ttk.Button(buttons, text="接入选中文件", command=accept).pack(side="right")
    ttk.Button(buttons, text="取消", command=close).pack(side="right", padx=8)
    return win


def _group(entry, kind):
    at = entry.get("at", {})
    if kind == "spreadsheet":
        return f"工作表 {at['sheet']}"
    table = at.get("table_index")
    return f"表格 {int(table) + 1}" if table is not None else "Word 正文"


def _label(entry, kind):
    at, value = entry.get("at", {}), entry.get("value", "")
    if kind == "spreadsheet":
        loc = at["cell"]
    else:
        row, col = at.get("row_index"), at.get("column_index")
        loc = (f"表行{int(row)+1}列{int(col)+1} 第{at['paragraph_index']+1}段" if row is not None and col is not None
               else f"第{int(at['paragraph_index'])+1}段")
    return loc + ("  [只读]" if not entry.get("editable", False) else "")


def _detail_text(path, entry=None, heading=""):
    lines = [f"文件：{path}"]
    if heading:
        lines.append(heading)
    if entry is not None:
        lines.append("原值：" + str(entry.get("value", "")))
    return "\n".join(lines)


def open_scope_selector(root, catalog, on_selected):
    catalog = deepcopy(catalog)
    files = catalog.get("files", [])
    win, close = _modal(root, "选择编辑范围", "850x560")
    ttk.Label(win, text="仅选中范围进入云端；公式只读。Ctrl 多选；选中文件或分组即选择其全部范围。", anchor="w").pack(fill="x", padx=12, pady=(12, 6))
    contents = ttk.Frame(win)
    contents.pack(fill='both',expand=True,padx=12,pady=4)
    tree = ttk.Treeview(contents, columns=("detail",), show="tree headings", selectmode="extended")
    tree.heading("#0", text="文件 / 分组 / 条目")
    tree.heading("detail", text="内容")
    tree.column("#0", width=370, stretch=True)
    tree.column("detail", width=430, stretch=True)
    scroll = ttk.Scrollbar(contents, orient="vertical", command=tree.yview)
    xscroll = ttk.Scrollbar(contents, orient="horizontal", command=tree.xview)
    tree.configure(yscrollcommand=scroll.set, xscrollcommand=xscroll.set)
    tree.grid(row=0, column=0, sticky="nsew")
    scroll.grid(row=0, column=1, sticky="ns")
    xscroll.grid(row=1, column=0, sticky="ew")
    contents.grid_rowconfigure(0, weight=1)
    contents.grid_columnconfigure(0, weight=1)
    file_nodes = {}
    group_nodes = {}
    entry_nodes = {}
    node_details = {}
    for fi, file in enumerate(files):
        path = str(file.get("path", ""))
        fnode = f"f{fi}"
        file_nodes[fnode] = fi
        tree.insert("", "end", iid=fnode, text=Path(path).name or path, values=(path,))
        node_details[fnode] = _detail_text(path)
        groups = {}
        for ei, entry in enumerate(file.get("entries", [])):
            group = _group(entry, file.get("kind"))
            if group not in groups:
                gnode = f"g{fi}_{len(groups)}"
                groups[group] = gnode
                group_nodes[gnode] = fi
                tree.insert(fnode, "end", iid=gnode, text=group, values=("",))
                node_details[gnode] = _detail_text(path, heading=group)
            inode = f"e{fi}_{ei}"
            entry_nodes[inode] = (fi, ei)
            tree.insert(groups[group], "end", iid=inode, text=_label(entry, file.get("kind")),
                        values=(" ".join(str(entry.get("value", "")).split()),))
            node_details[inode] = _detail_text(path, entry, _label(entry, file.get("kind")))
        tree.item(fnode, open=True)
    for node in file_nodes:
        tree.selection_add(node)
    _bind_shift_range(tree)
    detail_frame = ttk.Frame(win)
    detail_frame.pack(fill="x", padx=12, pady=(0, 4))
    ttk.Label(detail_frame, text="选中内容详情（完整路径与原值）", anchor="w").pack(fill="x")
    detail_body = ttk.Frame(detail_frame)
    detail_body.pack(fill="x")
    detail = tk.Text(detail_body, height=4, wrap="word", state="disabled")
    detail_scroll = ttk.Scrollbar(detail_body, orient="vertical", command=detail.yview)
    detail.configure(yscrollcommand=detail_scroll.set)
    detail.pack(side="left", fill="x", expand=True)
    detail_scroll.pack(side="right", fill="y")
    def show_detail(_event=None):
        node = tree.focus()
        text = node_details.get(node, "")
        detail.configure(state="normal")
        detail.delete("1.0", "end")
        detail.insert("1.0", text)
        detail.configure(state="disabled")
    tree.bind("<<TreeviewSelect>>", show_detail, add="+")
    tree.bind("<ButtonRelease-1>", show_detail, add="+")
    if file_nodes:
        tree.focus(next(iter(file_nodes)))
        show_detail()
    status = ttk.Label(win, text="", anchor="w")
    status.pack(fill="x", padx=12, pady=4)
    def accept():
        selected = set(tree.selection())
        indices = []
        for node in selected:
            if node in file_nodes:
                fi = file_nodes[node]
                indices.extend((fi, ei) for ei in range(len(files[fi].get("entries", []))))
            elif node in group_nodes:
                fi = group_nodes[node]
                group = tree.item(node, "text")
                indices.extend((fi, ei) for ei, entry in enumerate(files[fi].get("entries", []))
                               if _group(entry, files[fi].get("kind")) == group)
            elif node in entry_nodes:
                indices.append(entry_nodes[node])
        from .office_scope import selection_from_indices
        try:
            result = selection_from_indices(catalog, list(dict.fromkeys(indices)))
        except ValueError as exc:
            status.configure(text=str(exc))
            return
        on_selected(result)
        close()
    buttons = ttk.Frame(win)
    buttons.pack(fill="x", padx=12, pady=(4, 12))
    ttk.Button(buttons, text="使用选中范围", command=accept).pack(side="right")
    ttk.Button(buttons, text="取消", command=close).pack(side="right", padx=8)
    return win
