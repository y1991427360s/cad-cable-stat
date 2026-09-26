"""桌面工作台。所有 Tk 操作都留在主线程，工作线程只向队列投递数据。"""

from __future__ import annotations

import os
import queue
import threading
import time
import traceback
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, font as tkfont, messagebox, ttk
from typing import Any, Callable

import cable_stat_app as app
from cable_stat import __version__
from cable_stat.loaders import (
    apply_aliases, apply_case_insensitive_matches, load_aliases,
    load_cabinets, load_workbook_rows,
)
from cable_stat.params import PARAM_SPECS, format_param_value, load_params_with_warnings, save_params
from cable_stat.pipeline import RunResult
from cable_stat.project import (
    RESULT_HTML, RESULT_WORKBOOK, ProjectStatus, input_signature,
    save_aliases, scan_project,
)
from cable_stat.report import classify_issues, make_cabinet_check_rows, suggest_cabinet_names
from cable_stat.text import normalize_cabinet_name, parse_float

BG = "#f3f6fa"
INK = "#152c45"
MUTED = "#61748a"
ACCENT = "#087e8b"
SIDEBAR = "#162c45"
LEVEL_COLORS = {"错误": "#b42332", "警告": "#975b0c", "提示": "#236b9b", "信息": "#43716a"}


def enable_high_dpi() -> None:
    """必须在创建 Tk 前调用；旧版 Windows 逐级回退。"""
    if os.name != "nt":
        return
    import ctypes
    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


@dataclass
class ProjectSnapshot:
    status: ProjectStatus
    params: dict[str, float]
    issues: list[tuple[str, str]]
    cabinets: list[dict[str, Any]]
    cad_names: list[str]
    aliases: dict[str, str]
    signature: tuple


def inspect_project(project: Path) -> ProjectSnapshot:
    """可在工作线程运行，读取输入并预检名称，不改写原清册。"""
    signature = input_signature(project)
    status = scan_project(project)
    params, warnings = load_params_with_warnings(project / "data")
    cabinets, cabinet_warnings = load_cabinets(project / "data")
    names = sorted(cabinets)
    aliases, alias_warnings = load_aliases(project / "data")
    warnings += cabinet_warnings + alias_warnings + apply_aliases(cabinets, aliases)
    checks: list[dict[str, Any]] = []
    if status.workbook:
        wb, _, _, rows, required = load_workbook_rows(status.workbook)
        try:
            warnings += apply_case_insensitive_matches(cabinets, required)
            checks = make_cabinet_check_rows(rows, cabinets)
        finally:
            wb.close()
    issues = classify_issues(warnings)
    if status.workbook_error:
        issues.insert(0, ("错误", status.workbook_error))
    issues += [("错误", f"缺少有效输入：{name}") for name in status.missing_required]
    missing = sum(row["已提供坐标"] == "否" for row in checks)
    if missing:
        issues.append(("错误", f"{missing} 个清册柜名没有匹配到 CAD 坐标，请在“柜名匹配”中处理。"))
    return ProjectSnapshot(status, params, issues, checks, names, aliases, signature)


def validate_parameter_values(raw: dict[str, str]) -> dict[str, float]:
    values: dict[str, float] = {}
    for spec in PARAM_SPECS:
        value = parse_float(raw.get(spec.name))
        if value is None:
            raise ValueError(f"{spec.name}：请输入有限数字")
        problem = spec.check(value)
        if problem:
            raise ValueError(f"{spec.name}：{problem}")
        values[spec.name] = value
    return values


class CableStatApp:
    def __init__(self, root: tk.Tk, project: Path | None = None, *, auto_run: bool = False) -> None:
        self.root = root
        self.cfg = app.load_config()
        self.project: Path | None = None
        self.snapshot: ProjectSnapshot | None = None
        self.result: RunResult | None = None
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.busy = False
        self.closed = False
        self.close_when_done = False
        self.pending_handoffs: list[dict[str, Any]] = []
        self.started = 0.0
        self.target_progress = 0.0
        self.param_baseline: dict[str, str] = {}
        self.pending_aliases: dict[str, str] = {}
        self.issues: list[tuple[str, str]] = []
        self.cabinet_rows: list[dict[str, Any]] = []
        self.cad_names: list[str] = []
        self.signature: tuple = ()
        self.mutation_controls: list[tuple[Any, str]] = []
        self.param_vars: dict[str, tk.StringVar] = {}
        self._timer_ids: set[str] = set()
        self._style()
        self._build()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.bind("<Control-o>", lambda _e: self.choose_project())
        root.bind("<F5>", lambda _e: self.refresh())
        root.bind("<Control-Return>", lambda _e: self.start_calculation())
        self._later(60, self._pump)
        self._later(800, self._poll_handoff)
        self._later(4000, self._poll_inputs)
        if project is None:
            history = app.recent_projects(self.cfg)
            project = Path(history[0]) if history else None
        if project:
            self._later(50, lambda: self.select_project(project, auto_run=auto_run))

    def _later(self, delay: int, callback: Callable[[], None]) -> None:
        if self.closed:
            return
        def invoke() -> None:
            self._timer_ids.discard(timer)
            if not self.closed:
                callback()
        timer = self.root.after(delay, invoke)
        self._timer_ids.add(timer)

    def _style(self) -> None:
        self.root.title(f"{app.APP_TITLE} · {__version__}")
        self.root.configure(bg=BG)
        scale = max(1.0, self.root.winfo_fpixels("1i") / 96)
        self.scale = scale
        width = min(round(1220 * scale), self.root.winfo_screenwidth() - 60)
        height = min(round(820 * scale), self.root.winfo_screenheight() - 100)
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(min(width, round(940 * scale)), min(height, round(640 * scale)))
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont"):
            tkfont.nametofont(name).configure(family="Microsoft YaHei UI", size=10)
        try:
            self.root.iconbitmap(str(app.resource_dir() / app.APP_ICON_FILE))
        except tk.TclError:
            pass
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", font=("Microsoft YaHei UI", 10), foreground=INK, background=BG)
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background="white")
        style.configure("TLabel", background=BG)
        style.configure("Muted.TLabel", foreground=MUTED)
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 21, "bold"))
        style.configure("Card.TLabel", background="white")
        style.configure("Metric.TLabel", background="white", foreground=ACCENT, font=("Microsoft YaHei UI", 24, "bold"))
        style.configure("TButton", padding=(12, 7), borderwidth=0)
        style.map("TButton", background=[("active", "#dfeaf0"), ("disabled", "#ecf0f4")])
        style.configure("Primary.TButton", background=ACCENT, foreground="white", font=("Microsoft YaHei UI", 10, "bold"))
        style.map("Primary.TButton", background=[("disabled", "#a6bec3"), ("active", "#056571")], foreground=[("disabled", "#f6fafb")])
        style.configure("TEntry", padding=6, fieldbackground="white")
        style.configure("TCombobox", padding=5, fieldbackground="white")
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", padding=(18, 10), background="#e6edf4")
        style.map("TNotebook.Tab", background=[("selected", "white")], foreground=[("selected", ACCENT)])
        style.configure("Treeview", background="white", fieldbackground="white", rowheight=round(33 * scale), borderwidth=0)
        style.configure("Treeview.Heading", background="#eaf0f6", padding=(8, 9), font=("Microsoft YaHei UI", 10, "bold"))
        style.map("Treeview", background=[("selected", "#d8edf0")], foreground=[("selected", INK)])
        style.configure("TProgressbar", background=ACCENT, troughcolor="#e1e9f0", borderwidth=0)

    def _button(self, parent: Any, text: str, command: Callable, *, primary: bool = False, mutate: bool = False) -> ttk.Button:
        button = ttk.Button(parent, text=text, command=command, style="Primary.TButton" if primary else "TButton")
        if mutate:
            self.mutation_controls.append((button, "normal"))
        return button

    def _build(self) -> None:
        sidebar = tk.Frame(self.root, bg=SIDEBAR, width=round(230 * self.scale), padx=18, pady=24)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        tk.Label(sidebar, text="电缆统计", fg="white", bg=SIDEBAR, font=("Microsoft YaHei UI", 22, "bold")).pack(anchor="w")
        tk.Label(sidebar, text="工程计算工作台", fg="#9eb9cf", bg=SIDEBAR).pack(anchor="w", pady=(6, 28))
        for text, command in (("打开工程   Ctrl+O", self.choose_project), ("新建工程", self.new_project), ("导出 CAD 向导", self.export_cad)):
            self._button(sidebar, text, command, mutate=True).pack(fill="x", pady=4)
        tk.Label(sidebar, text="最近工程", fg="#9eb9cf", bg=SIDEBAR).pack(anchor="w", pady=(28, 10))
        self.history_list = tk.Listbox(sidebar, bg=SIDEBAR, fg="#edf5fa", selectbackground="#305776", selectforeground="white",
                                       activestyle="none", borderwidth=0, highlightthickness=0, exportselection=False, height=12)
        self.history_list.pack(fill="both", expand=True)
        self.history_list.bind("<<ListboxSelect>>", self._history_selected)
        self._update_history()
        tk.Label(sidebar, text=f"v{__version__}\nCAD → 清册 → 统计结果", fg="#9eb9cf", bg=SIDEBAR, justify="left").pack(anchor="w", pady=(20, 0))

        main = ttk.Frame(self.root, padding=(24, 20))
        main.pack(side="left", fill="both", expand=True)
        header = ttk.Frame(main)
        header.pack(fill="x")
        titles = ttk.Frame(header)
        titles.pack(side="left", fill="x", expand=True)
        self.project_title = tk.StringVar(value="打开一个工程，开始统计")
        ttk.Label(titles, textvariable=self.project_title, style="Title.TLabel").pack(anchor="w")
        self.project_path = tk.StringVar(value="选择包含清册和 data 文件夹的工程目录")
        path_label = ttk.Label(titles, textvariable=self.project_path, style="Muted.TLabel", wraplength=620)
        path_label.pack(anchor="w", pady=(5, 0))
        titles.bind("<Configure>", lambda e: path_label.configure(wraplength=max(200, e.width - 10)))
        self.run_button = self._button(header, "开始计算", self.start_calculation, primary=True, mutate=True)
        self.run_button.pack(side="right", padx=(16, 0))
        self.state_text = tk.StringVar(value="尚未选择工程")
        ttk.Label(main, textvariable=self.state_text, foreground=ACCENT).pack(anchor="w", pady=(15, 13))

        metrics = ttk.Frame(main)
        metrics.pack(fill="x", pady=(0, 18))
        self.metrics: dict[str, tk.StringVar] = {}
        for i, (key, title) in enumerate((("total", "电缆总数"), ("ok", "已完成计算"), ("failed", "未计算"), ("length", "自动统计合计 / 米"))):
            metrics.columnconfigure(i, weight=1, uniform="metric")
            card = ttk.Frame(metrics, padding=(16, 12), style="Card.TFrame")
            card.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 8, 0))
            self.metrics[key] = tk.StringVar(value="—")
            ttk.Label(card, text=title, style="Card.TLabel", foreground=MUTED).pack(anchor="w")
            ttk.Label(card, textvariable=self.metrics[key], style="Metric.TLabel").pack(anchor="w", pady=(6, 0))

        self.notebook = ttk.Notebook(main)
        self.notebook.pack(fill="both", expand=True)
        self.tabs: dict[str, ttk.Frame] = {}
        for name in ("工程概览", "计算参数", "柜名匹配", "问题清单", "运行日志"):
            page = ttk.Frame(self.notebook, padding=16, style="Card.TFrame")
            self.notebook.add(page, text=name)
            self.tabs[name] = page
        self._build_overview()
        self._build_params()
        self._build_aliases()
        self._build_issues()
        self._build_log()
        bottom = ttk.Frame(main)
        bottom.pack(fill="x", pady=(15, 0))
        self.phase = tk.StringVar(value="就绪")
        self.elapsed = tk.StringVar(value="Ctrl+Enter 开始计算  ·  F5 刷新")
        ttk.Label(bottom, textvariable=self.phase).pack(side="left")
        ttk.Label(bottom, textvariable=self.elapsed, style="Muted.TLabel").pack(side="right")
        self.progress = ttk.Progressbar(main, maximum=100, mode="determinate")
        self.progress.pack(fill="x", pady=(8, 0))
        self._update_enabled()

    def _table(self, parent: Any, columns: list[tuple[str, str, int]], *, height: int = 8) -> ttk.Treeview:
        frame = ttk.Frame(parent, style="Card.TFrame")
        frame.pack(fill="both", expand=True, pady=(10, 0))
        tree = ttk.Treeview(frame, columns=[c[0] for c in columns], show="headings", height=height, selectmode="browse")
        for key, title, width in columns:
            tree.heading(key, text=title)
            tree.column(key, width=round(width * self.scale), minwidth=60, stretch=True)
        vertical = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        horizontal = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        for level, color in LEVEL_COLORS.items():
            tree.tag_configure(level, foreground=color)
        tree.tag_configure("pending", foreground=ACCENT)
        return tree

    def _build_overview(self) -> None:
        page = self.tabs["工程概览"]
        toolbar = ttk.Frame(page, style="Card.TFrame")
        toolbar.pack(fill="x")
        for label, command in (("刷新输入", self.refresh), ("工程文件夹", lambda: self.open_artifact("project")),
                               ("结果工作簿", lambda: self.open_artifact("xlsx")), ("路径可视化", lambda: self.open_artifact("html"))):
            self._button(toolbar, label, command, mutate=label == "刷新输入").pack(side="left", padx=(0, 8))
        self.file_tree = self._table(page, [("name", "输入文件", 240), ("count", "有效行数", 90), ("state", "状态", 170), ("time", "修改时间", 130)])
        self.file_tree.bind("<Double-1>", self._open_input)
        ttk.Label(page, text="使用顺序：在 CAD 中导出数据 → 放入电缆清册 → 检查参数与柜名 → 开始计算。\n双击文件可打开；结果存放在工程的 outputs 文件夹。",
                  style="Card.TLabel", foreground=MUTED, wraplength=820, justify="left").pack(anchor="w", pady=(14, 0))

    def _build_params(self) -> None:
        page = self.tabs["计算参数"]
        toolbar = ttk.Frame(page, style="Card.TFrame")
        toolbar.pack(fill="x")
        self._button(toolbar, "保存参数", self.save_parameters, primary=True, mutate=True).pack(side="left")
        self._button(toolbar, "填入默认值", self.reset_parameters, mutate=True).pack(side="left", padx=8)
        self.param_notice = tk.StringVar(value="仅对当前工程生效")
        ttk.Label(toolbar, textvariable=self.param_notice, style="Card.TLabel", foreground=MUTED).pack(side="left", padx=8)
        frame = ttk.Frame(page, style="Card.TFrame")
        frame.pack(fill="both", expand=True, pady=(14, 0))
        canvas = tk.Canvas(frame, bg="white", highlightthickness=0)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        inner = ttk.Frame(canvas, style="Card.TFrame")
        item = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(item, width=e.width))
        def wheel(event: Any) -> str | None:
            if self.notebook.select() == str(page) and canvas.winfo_rootx() <= event.x_root <= canvas.winfo_rootx() + canvas.winfo_width():
                canvas.yview_scroll(-int(event.delta / 120), "units")
                return "break"
            return None
        self.root.bind("<MouseWheel>", wheel, add="+")
        inner.columnconfigure(2, weight=1)
        for i, spec in enumerate(PARAM_SPECS):
            var = tk.StringVar(value=format_param_value(spec.default))
            self.param_vars[spec.name] = var
            var.trace_add("write", lambda *_: self._param_changed())
            ttk.Label(inner, text=spec.name, style="Card.TLabel").grid(row=i, column=0, sticky="w", padx=(0, 15), pady=12)
            entry = ttk.Entry(inner, textvariable=var, width=13)
            entry.grid(row=i, column=1, sticky="w", padx=(0, 18))
            self.mutation_controls.append((entry, "normal"))
            note = ttk.Label(inner, text=spec.note, style="Card.TLabel", foreground=MUTED, wraplength=450)
            note.grid(row=i, column=2, sticky="ew", pady=8)
            note.bind("<Configure>", lambda e, label=note: label.configure(wraplength=max(180, inner.winfo_width() - round(295 * self.scale))))

    def _build_aliases(self) -> None:
        page = self.tabs["柜名匹配"]
        bar = ttk.Frame(page, style="Card.TFrame")
        bar.pack(fill="x")
        self.only_missing = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="只看未匹配", variable=self.only_missing, command=self._render_cabinets).pack(side="left")
        self.alias_filter = tk.StringVar()
        ttk.Entry(bar, textvariable=self.alias_filter, width=24).pack(side="left", padx=12)
        self.alias_filter.trace_add("write", lambda *_: self._render_cabinets())
        ttk.Label(bar, text="按清册柜名搜索", style="Card.TLabel", foreground=MUTED).pack(side="left")
        self.alias_notice = tk.StringVar(value="相似名称仅作建议，请选择实际对应的 CAD 柜子。")
        ttk.Label(page, textvariable=self.alias_notice, style="Card.TLabel", foreground=MUTED).pack(anchor="w", pady=(10, 0))
        self.alias_tree = self._table(page, [("name", "清册柜名", 200), ("count", "出现次数", 80), ("state", "匹配状态", 100),
                                             ("target", "CAD 名称 / 建议", 330)], height=6)
        self.alias_tree.bind("<<TreeviewSelect>>", self._alias_selected)
        editor = ttk.Frame(page, style="Card.TFrame")
        editor.pack(fill="x", pady=(12, 0))
        self.alias_source = tk.StringVar(value="先选一条清册柜名")
        ttk.Label(editor, textvariable=self.alias_source, style="Card.TLabel").pack(anchor="w", pady=(0, 8))
        row = ttk.Frame(editor, style="Card.TFrame")
        row.pack(fill="x")
        self.alias_target = tk.StringVar()
        self.alias_combo = ttk.Combobox(row, textvariable=self.alias_target, width=30)
        self.alias_combo.pack(side="left", fill="x", expand=True)
        self.mutation_controls.append((self.alias_combo, "normal"))
        self._button(row, "加入映射", self.stage_alias, mutate=True).pack(side="left", padx=8)
        self._button(row, "撤销待存", self.undo_alias, mutate=True).pack(side="left")
        self._button(row, "保存映射", self.save_alias_mappings, primary=True, mutate=True).pack(side="left", padx=(8, 0))

    def _build_issues(self) -> None:
        page = self.tabs["问题清单"]
        bar = ttk.Frame(page, style="Card.TFrame")
        bar.pack(fill="x")
        self.issue_level = tk.StringVar(value="全部级别")
        selector = ttk.Combobox(bar, textvariable=self.issue_level, values=["全部级别", *LEVEL_COLORS], state="readonly", width=12)
        selector.pack(side="left")
        selector.bind("<<ComboboxSelected>>", lambda _e: self._render_issues())
        self.issue_search = tk.StringVar()
        ttk.Entry(bar, textvariable=self.issue_search, width=27).pack(side="left", padx=10)
        self.issue_search.trace_add("write", lambda *_: self._render_issues())
        self.issue_count = tk.StringVar(value="尚无检查结果")
        ttk.Label(bar, textvariable=self.issue_count, style="Card.TLabel", foreground=MUTED).pack(side="left")
        self.issue_tree = self._table(page, [("level", "级别", 80), ("message", "问题说明", 650)], height=6)
        self.issue_tree.bind("<<TreeviewSelect>>", self._issue_selected)
        self.issue_detail = tk.Text(page, height=4, wrap="word", bg="#f5f8fb", fg=INK, relief="flat", padx=12, pady=10)
        self.issue_detail.pack(fill="x", pady=(12, 0))
        self.issue_detail.configure(state="disabled")

    def _build_log(self) -> None:
        page = self.tabs["运行日志"]
        toolbar = ttk.Frame(page, style="Card.TFrame")
        toolbar.pack(fill="x", pady=(0, 10))
        self._button(toolbar, "保存日志…", self.save_log).pack(side="left")
        self._button(toolbar, "清空显示", self.clear_log).pack(side="left", padx=8)
        self.log_text = tk.Text(page, bg="#14283e", fg="#d9e7f0", insertbackground="white", relief="flat",
                                wrap="word", padx=14, pady=12, font=("Microsoft YaHei UI", 10), state="disabled")
        scroll = ttk.Scrollbar(page, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log_text.pack(fill="both", expand=True)
        for name, color in (("error", "#ff9b9b"), ("warn", "#ffcf88"), ("success", "#75d7be"), ("info", "#d9e7f0")):
            self.log_text.tag_configure(name, foreground=color)

    def _update_history(self) -> None:
        self.history_paths = app.recent_projects(self.cfg)
        self.history_list.delete(0, "end")
        for item in self.history_paths:
            self.history_list.insert("end", f"  {Path(item).name}")

    def _history_selected(self, _event: Any = None) -> None:
        selected = self.history_list.curselection()
        if selected and not self.busy:
            self.select_project(Path(self.history_paths[selected[0]]))

    def choose_project(self) -> None:
        if self.busy:
            return
        name = filedialog.askdirectory(parent=self.root, title="选择工程文件夹", initialdir=str(self.project or app.tool_dir() / "项目"))
        if name:
            self.select_project(Path(name))

    def new_project(self) -> None:
        if self.busy or not self._resolve_edits():
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("新建工程")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        panel = ttk.Frame(dialog, padding=24)
        panel.pack(fill="both", expand=True)
        parent_dir = app.tool_dir() / "项目"
        parent_dir.mkdir(exist_ok=True)
        name, folder = tk.StringVar(), tk.StringVar(value=str(parent_dir))
        ttk.Label(panel, text="工程名称").grid(row=0, column=0, sticky="w", pady=8)
        name_entry = ttk.Entry(panel, textvariable=name, width=40)
        name_entry.grid(row=0, column=1, columnspan=2, sticky="ew", padx=(12, 0))
        ttk.Label(panel, text="存放位置").grid(row=1, column=0, sticky="w", pady=8)
        ttk.Entry(panel, textvariable=folder, width=40).grid(row=1, column=1, sticky="ew", padx=12)
        def browse() -> None:
            value = filedialog.askdirectory(parent=dialog, initialdir=folder.get())
            if value:
                folder.set(value)
        self._button(panel, "浏览…", browse).grid(row=1, column=2)
        error = tk.StringVar()
        ttk.Label(panel, textvariable=error, foreground=LEVEL_COLORS["错误"], wraplength=440).grid(row=2, column=0, columnspan=3, sticky="w", pady=12)
        def create() -> None:
            problem = app.check_new_project(folder.get(), name.get())
            if problem:
                error.set(problem)
                return
            try:
                project = app.create_project(Path(folder.get()), name.get())
            except Exception as exc:
                error.set(str(exc))
                return
            dialog.destroy()
            self.select_project(project)
        self._button(panel, "创建工程", create, primary=True).grid(row=3, column=1, columnspan=2, sticky="e")
        dialog.bind("<Return>", lambda _e: create())
        dialog.bind("<Escape>", lambda _e: dialog.destroy())
        dialog.grab_set()
        name_entry.focus_set()

    def export_cad(self) -> None:
        if self.busy:
            return
        target = filedialog.askdirectory(parent=self.root, title="导出 CAD 完整向导", initialdir=str(self.project or app.tool_dir()))
        if not target:
            return
        try:
            files = app.export_lsp(Path(target))
            if not files:
                raise FileNotFoundError("未找到内嵌 CAD 向导资源")
            messagebox.showinfo("已导出 CAD 向导", f"位置：{target}\n\n在 CAD 中 APPLOAD 加载 cad_cable_wizard.lsp，\n再运行 DDFD_CABLE_WIZARD。", parent=self.root)
        except Exception as exc:
            self._error("导出失败", exc)

    def select_project(self, project: Path, *, auto_run: bool = False) -> None:
        if self.busy or not self._resolve_edits():
            return
        project = project.resolve()
        if not project.is_dir() or app.same_path(project, app.tool_dir()):
            self._error("无法打开工程", "请选择具体工程文件夹，不要选择工具根目录。")
            return
        self.project = project
        self.snapshot = None
        self.result = None
        self.pending_aliases.clear()
        self.param_baseline = {}
        for var in self.param_vars.values():
            var.set("")
        self.param_notice.set("正在读取工程参数…")
        self.issues = []
        self.cabinet_rows = []
        self.cad_names = []
        self.alias_target.set("")
        self.alias_source.set("先选一条清册柜名")
        self.signature = ()
        self.project_title.set(project.name)
        self.project_path.set(str(project))
        for var in self.metrics.values():
            var.set("—")
        self.file_tree.delete(*self.file_tree.get_children())
        self._render_cabinets()
        self._render_issues()
        app.remember_project(self.cfg, project)
        app.save_config(self.cfg)
        self._update_history()
        self.refresh(auto_run=auto_run, resolve=False)

    def refresh(self, *, auto_run: bool = False, resolve: bool = True) -> None:
        if self.busy or not self.project or (resolve and not self._resolve_edits()):
            return
        project = self.project
        self._launch("读取工程输入", lambda: inspect_project(project), "scanned", auto_run)

    def _launch(self, title: str, work: Callable[[], Any], event: str, extra: Any = None) -> None:
        self.busy = True
        self.started = time.monotonic()
        self.target_progress = 0
        self.progress["value"] = 0
        self.phase.set(title)
        self.state_text.set(title + "…")
        self._update_enabled()
        def worker() -> None:
            try:
                self.events.put((event, (work(), extra)))
            except Exception as exc:
                self.events.put(("error", (title, str(exc) or type(exc).__name__, traceback.format_exc())))
        threading.Thread(target=worker, name="cable-stat-worker", daemon=False).start()

    def _pump(self) -> None:
        try:
            self._process_events()
        finally:
            self._later(60, self._pump)

    def _process_events(self) -> None:
        for _ in range(120):
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self._append_log(payload)
            elif kind == "progress":
                value, title = payload
                self.target_progress = max(self.target_progress, value)
                self.phase.set(title)
            elif kind == "scanned":
                snapshot, auto_run = payload
                self._finish_busy()
                self._apply_snapshot(snapshot)
                if auto_run and not self.close_when_done:
                    self._later(80, self.start_calculation)
            elif kind == "calculated":
                result, _ = payload
                self._finish_busy()
                self._apply_result(result)
            elif kind == "error":
                title, reason, trace = payload
                self._finish_busy(success=False)
                self.state_text.set(f"{title}失败：{(reason or '未知异常').splitlines()[0]}")
                self._append_log(trace + "\n", "error")
                self.issues.insert(0, ("错误", reason))
                self._render_issues()
                self.notebook.select(self.tabs["运行日志"])
                self._error(f"{title}失败", reason)
        if self.busy:
            self.elapsed.set(f"已用时 {time.monotonic() - self.started:.1f} 秒")
            current = float(self.progress["value"])
            self.progress["value"] = min(self.target_progress, current + max(0.5, (self.target_progress - current) * 0.22))
        elif self.close_when_done:
            self._destroy()
            return

    def _finish_busy(self, *, success: bool = True) -> None:
        self.busy = False
        self.progress["value"] = 100 if success else 0
        self.phase.set("完成" if success else "任务失败")
        self.elapsed.set(f"用时 {time.monotonic() - self.started:.1f} 秒")
        self._update_enabled()

    def _update_enabled(self) -> None:
        for widget, normal in self.mutation_controls:
            # 打开、新建和导出按钮在无工程时仍可用。
            available = not self.busy and (self.project is not None or widget.master == self.history_list.master)
            if any(str(widget).startswith(str(self.tabs[tab]) + ".") for tab in ("计算参数", "柜名匹配")):
                available = available and self.snapshot is not None
            widget.configure(state=normal if available else "disabled")
        self.history_list.configure(state="disabled" if self.busy else "normal")

    def _apply_snapshot(self, snapshot: ProjectSnapshot) -> None:
        self.snapshot = snapshot
        self.signature = snapshot.signature
        self.issues = snapshot.issues
        self.cabinet_rows = snapshot.cabinets
        self.cad_names = snapshot.cad_names
        for spec in PARAM_SPECS:
            self.param_vars[spec.name].set(format_param_value(snapshot.params[spec.name]))
        self.param_baseline = {name: var.get() for name, var in self.param_vars.items()}
        self.param_notice.set("已载入当前工程参数；保存后重新计算生效")
        status = snapshot.status
        self.file_tree.delete(*self.file_tree.get_children())
        self.file_tree.insert("", "end", iid="workbook", values=(status.workbook.name if status.workbook else "电缆清册 .xlsx", "—", "已找到" if status.workbook else "未找到有效清册", ""), tags=() if status.workbook else ("错误",))
        for i, file in enumerate(status.files):
            valid = file.exists and file.rows > 0
            state = "已就绪" if valid else ("缺少有效数据" if file.required else "可选 · 未填写")
            self.file_tree.insert("", "end", iid=f"file{i}", values=(file.name, file.rows if file.exists else "—", state, file.mtime_text), tags=("错误",) if file.required and not valid else ())
        if status.workbook_error or status.missing_required:
            self.state_text.set("输入尚未齐全 · 请查看工程概览与问题清单")
        elif status.result_outdated:
            self.state_text.set("输入已更新 · 上次结果需要重新计算")
        else:
            self.state_text.set("输入已就绪 · 可检查参数、处理柜名后开始计算")
        self._render_cabinets()
        self._render_issues()
        self._append_log(f"\n已打开工程：{status.project_dir}\n预检发现 {len(self.issues)} 条提示或问题。\n")
        self._update_enabled()

    def start_calculation(self) -> None:
        if self.busy or not self.project or not self._resolve_edits():
            return
        self.result = None
        for var in self.metrics.values():
            var.set("—")
        project = self.project
        self.signature = input_signature(project)
        self._append_log(f"\n{'─' * 36}\n开始计算：{project.name}\n", "info")
        self._launch("正在统计", lambda: app.calculate_project(project, lambda text: self.events.put(("log", text)),
                                                               lambda value, text: self.events.put(("progress", (value, text)))), "calculated")

    def _apply_result(self, result: RunResult) -> None:
        self.result = result
        self.issues = result.issues + [("警告", note) for note in result.notes]
        self.cabinet_rows = result.cabinet_check_rows
        self.cad_names = result.cad_cabinet_names
        for key, value in (("total", result.total), ("ok", result.ok), ("failed", result.failed), ("length", f"{result.summary['auto_sum']:,.1f}")):
            self.metrics[key].set(str(value))
        if self.project and input_signature(self.project) != self.signature:
            self.state_text.set("统计已完成，但输入在运行期间发生变化 · 请刷新后重算")
        elif result.failed:
            self.state_text.set(f"已完成 · {result.failed} 条电缆未计算，请处理问题后重算")
        else:
            self.state_text.set(f"统计完成 · 共 {result.ok} 条电缆，结果已写入 outputs")
        self._render_cabinets()
        self._render_issues()
        self._append_log("统计结束，可打开结果工作簿或路径可视化。\n", "warn" if result.failed else "success")
        if result.failed or any(level == "错误" for level, _ in self.issues):
            self.notebook.select(self.tabs["问题清单"])
        else:
            self.notebook.select(self.tabs["工程概览"])

    def _param_changed(self) -> None:
        if hasattr(self, "param_notice"):
            self.param_notice.set("有未保存修改" if self.parameters_dirty() else "当前工程参数")

    def parameters_dirty(self) -> bool:
        return bool(self.param_baseline) and any(var.get() != self.param_baseline.get(name) for name, var in self.param_vars.items())

    def reset_parameters(self) -> None:
        if self.busy:
            return
        for spec in PARAM_SPECS:
            self.param_vars[spec.name].set(format_param_value(spec.default))

    def save_parameters(self) -> bool:
        if self.busy or not self.project or self.snapshot is None:
            return False
        try:
            values = validate_parameter_values({name: var.get() for name, var in self.param_vars.items()})
            save_params(self.project / "data", values)
        except Exception as exc:
            self._error("参数未保存", exc)
            return False
        self.param_baseline = {name: var.get() for name, var in self.param_vars.items()}
        self.param_notice.set("已保存 · 重新计算后生效")
        self.state_text.set("参数已更新 · 请重新计算")
        self._append_log("已保存工程参数。\n", "success")
        return True

    def _render_cabinets(self) -> None:
        if not hasattr(self, "alias_tree"):
            return
        self.alias_tree.delete(*self.alias_tree.get_children())
        query = self.alias_filter.get().strip().casefold()
        missing = 0
        for i, row in enumerate(self.cabinet_rows):
            name = row["柜子名称"]
            unmatched = row["已提供坐标"] == "否"
            missing += unmatched
            if (self.only_missing.get() and not unmatched and name not in self.pending_aliases) or query not in name.casefold():
                continue
            pending = self.pending_aliases.get(name)
            existing = self.snapshot.aliases.get(name, "") if self.snapshot else ""
            target = pending or existing or (name if not unmatched else row.get("近似CAD柜名建议", ""))
            state = "待保存" if pending else ("未匹配" if unmatched else "已匹配")
            self.alias_tree.insert("", "end", iid=str(i), values=(name, row["清册出现次数"], state, target), tags=("pending",) if pending else (("错误",) if unmatched else ()))
        self.alias_notice.set(f"{missing} 个未匹配 · {len(self.pending_aliases)} 条待保存。相似名称仅作建议，请人工核对。")

    def _alias_selected(self, _event: Any = None) -> None:
        selected = self.alias_tree.selection()
        if not selected:
            return
        name = self.cabinet_rows[int(selected[0])]["柜子名称"]
        self.alias_source.set(name)
        suggestions = suggest_cabinet_names(name, self.cad_names, limit=5)
        self.alias_combo["values"] = suggestions + [n for n in self.cad_names if n not in suggestions]
        self.alias_target.set(self.pending_aliases.get(name, ""))

    def stage_alias(self) -> None:
        if self.busy:
            return
        selected = self.alias_tree.selection()
        if not selected:
            self._error("尚未选择柜名", "先在列表中选择清册柜名，再选择对应的 CAD 柜子。")
            return
        name = self.cabinet_rows[int(selected[0])]["柜子名称"]
        target = normalize_cabinet_name(self.alias_target.get())
        if target not in self.cad_names:
            self._error("CAD 柜名无效", "请选择列表中的 CAD 柜名，或输入完全一致的名称。")
            return
        if name in self.cad_names and name != target:
            self._error("无法覆盖 CAD 柜名", "该清册柜名已有同名 CAD 坐标，别名不能覆盖它。请先核对 CAD 命名。")
            return
        self.pending_aliases[name] = target
        self._render_cabinets()

    def undo_alias(self) -> None:
        if self.busy:
            return
        selected = self.alias_tree.selection()
        if selected:
            self.pending_aliases.pop(self.cabinet_rows[int(selected[0])]["柜子名称"], None)
            self._render_cabinets()

    def save_alias_mappings(self, *, refresh: bool = True) -> bool:
        if self.busy or not self.project:
            return False
        if not self.pending_aliases:
            return True
        try:
            count = save_aliases(self.project / "data", self.pending_aliases)
        except Exception as exc:
            self._error("映射未保存", exc)
            return False
        if self.snapshot:
            self.snapshot.aliases.update(self.pending_aliases)
        self.pending_aliases.clear()
        self.state_text.set("柜名映射已更新 · 请重新计算")
        self._append_log(f"已保存 {count} 条柜名映射。\n", "success")
        if refresh:
            self.refresh()
        return True

    def _resolve_edits(self) -> bool:
        if not self.parameters_dirty() and not self.pending_aliases:
            return True
        answer = messagebox.askyesnocancel("保存工程修改", "当前工程有未保存的参数或柜名映射。\n\n是否保存后继续？选择“否”将放弃未保存修改。", parent=self.root)
        if answer is None:
            return False
        if answer:
            if self.parameters_dirty() and not self.save_parameters():
                return False
            if self.pending_aliases and not self.save_alias_mappings(refresh=False):
                return False
        else:
            for name, text in self.param_baseline.items():
                self.param_vars[name].set(text)
            self.pending_aliases.clear()
            self._render_cabinets()
        return True

    def _render_issues(self) -> None:
        if not hasattr(self, "issue_tree"):
            return
        self.issue_tree.delete(*self.issue_tree.get_children())
        query = self.issue_search.get().strip().casefold()
        shown = 0
        for i, (level, message) in enumerate(self.issues):
            if self.issue_level.get() not in ("全部级别", level) or query not in message.casefold():
                continue
            self.issue_tree.insert("", "end", iid=str(i), values=(level, message.replace("\n", " ")), tags=(level,))
            shown += 1
        errors = sum(level == "错误" for level, _ in self.issues)
        self.issue_count.set(f"显示 {shown} / {len(self.issues)} 条 · {errors} 条错误")
        self.issue_detail.configure(state="normal")
        self.issue_detail.delete("1.0", "end")
        self.issue_detail.insert("end", "选中问题以查看完整说明。" if self.issues else "当前没有检查问题。")
        self.issue_detail.configure(state="disabled")

    def _issue_selected(self, _event: Any = None) -> None:
        selected = self.issue_tree.selection()
        if selected:
            level, message = self.issues[int(selected[0])]
            self.issue_detail.configure(state="normal")
            self.issue_detail.delete("1.0", "end")
            self.issue_detail.insert("end", f"[{level}] {message}")
            self.issue_detail.configure(state="disabled")

    def _append_log(self, text: str, tag: str | None = None) -> None:
        self.log_text.configure(state="normal")
        following = self.log_text.yview()[1] >= 0.98
        for line in text.splitlines(keepends=True):
            color = tag or ("error" if any(word in line for word in ("错误", "失败", "Traceback")) else
                            "warn" if any(word in line for word in ("注意", "警告", "未计算", "参数提示")) else "info")
            self.log_text.insert("end", line, color)
        if int(self.log_text.index("end-1c").split(".")[0]) > 12000:
            self.log_text.delete("1.0", "1001.0")
        if following:
            self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def save_log(self) -> None:
        path = filedialog.asksaveasfilename(parent=self.root, title="保存当前日志", initialdir=str(self.project or app.tool_dir()), initialfile="电缆统计_运行日志.txt", defaultextension=".txt")
        if path:
            try:
                Path(path).write_text(self.log_text.get("1.0", "end-1c"), encoding="utf-8-sig")
            except OSError as exc:
                self._error("日志未保存", exc)

    def _open_input(self, _event: Any = None) -> None:
        if not self.project or not self.snapshot:
            return
        selected = self.file_tree.selection()
        if selected:
            name = selected[0]
            path = self.snapshot.status.workbook if name == "workbook" else self.project / "data" / self.snapshot.status.files[int(name[4:])].name
            if path:
                self._open_path(path)

    def open_artifact(self, kind: str) -> None:
        if not self.project:
            return
        if kind == "project":
            path = self.project
        elif kind == "xlsx":
            path = self.result.output_workbook if self.result else self.project / "outputs" / RESULT_WORKBOOK
        else:
            path = self.result.html_path if self.result else self.project / "outputs" / RESULT_HTML
        self._open_path(path)

    def _open_path(self, path: Path) -> None:
        try:
            if not path.exists():
                raise FileNotFoundError(f"文件尚未生成或已移走：{path}")
            os.startfile(str(path))
        except Exception as exc:
            self._error("无法打开", exc)

    def _poll_handoff(self) -> None:
        # 模态编辑/文件选择期间不切工程，避免保存到错误的工程目录。
        if self.root.grab_current() is None:
            request = app.claim_handoff_request()
            if request:
                self.pending_handoffs.append(request)
            if not self.busy and self.pending_handoffs:
                request = self.pending_handoffs.pop(0)
                self.root.deiconify()
                self.root.lift()
                self.select_project(Path(request["project"]), auto_run=bool(request.get("run", True)))
        self._later(800, self._poll_handoff)

    def _poll_inputs(self) -> None:
        if self.project and not self.busy and self.signature:
            try:
                if input_signature(self.project) != self.signature:
                    self.state_text.set("检测到工程输入变化 · 按 F5 刷新，重新计算后更新结果")
            except OSError:
                pass
        self._later(4000, self._poll_inputs)

    def _error(self, title: str, error: Any) -> None:
        messagebox.showerror(title, str(error), parent=self.root)

    def on_close(self) -> None:
        if self.busy:
            if messagebox.askyesno("任务仍在运行", "任务正在处理工程文件。是否在任务完成后自动关闭窗口？", parent=self.root):
                self.close_when_done = True
                self.state_text.set("正在完成当前任务 · 完成后自动关闭")
            return
        if self._resolve_edits():
            self._destroy()

    def _destroy(self) -> None:
        self.closed = True
        for timer in self._timer_ids:
            self.root.after_cancel(timer)
        self._timer_ids.clear()
        self.root.destroy()


def run_gui(project: Path | None = None, *, auto_run: bool = False) -> int:
    enable_high_dpi()
    root = tk.Tk()
    CableStatApp(root, project, auto_run=auto_run)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(run_gui())
