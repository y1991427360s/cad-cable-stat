"""工程内的端子排编辑页面；Tk 生命周期由工作台主窗口管理。"""
from __future__ import annotations

import gc
import os
import queue
import subprocess
import threading
import tkinter as tk
import tkinter.font as tkfont
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable

from .service import TerminalService, EXAMPLE_CABINET, EXAMPLE_TERMINALS, EXAMPLE_WIRING
from core.models import UnifiedIssue

UI_FONT = "Microsoft YaHei UI"
CODE_FONT = ("Consolas", 11)
MUTED = "#61748a"
ARROWS = {"向下": "↓ 向下", "向上": "↑ 向上"}
MAX_IMPORT_BYTES = 5 * 1024 * 1024
GUIDE = """使用步骤：
1. 在①粘贴端子排。普通格式一行一块，如「ZD、1、2、3」；坐标格式每行「横坐标,纵坐标 文字」，名称行以“端子名”结尾。
2. 在②粘贴接线。一行一条「端子号、原理号、去向柜」，同一根电缆只在最后一条写去向柜；也可以留空只画端子排。
3. 确认各块方向，按 F5 检查、Ctrl+Enter 生成 DXF。

输入保存在当前工程的 data/terminal；成果保存在 outputs/端子排。"""


def read_text_file(path: str | Path) -> str:
    """识别 CAD / Excel 文本常见的 BOM、UTF-8、GB18030 编码。"""
    data = Path(path).read_bytes()
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("gb18030", errors="replace")


class LineNumbers(tk.Canvas):
    def __init__(self, master: tk.Misc, target: tk.Text):
        self.number_font = tkfont.Font(root=master, font=CODE_FONT)
        super().__init__(master, width=self.number_font.measure("000") + 12,
                         highlightthickness=0, background="#edf2f6")
        self.target = target
        self._job: str | None = None
        self.bind("<Destroy>", lambda _event: self.cancel())

    def schedule(self, *_args: Any) -> None:
        if self._job is None:
            self._job = self.after_idle(self.redraw)

    def cancel(self) -> None:
        if self._job is not None:
            self.after_cancel(self._job)
            self._job = None

    def redraw(self) -> None:
        self._job = None
        last = int(self.target.index("end-1c").split(".")[0])
        width = self.number_font.measure("0" * max(3, len(str(last)))) + 12
        if int(self["width"]) != width:
            self.configure(width=width)
        self.delete("all")
        line = int(self.target.index("@0,0").split(".")[0])
        while line <= last:
            info = self.target.dlineinfo(f"{line}.0")
            if info is None:
                break
            self.create_text(width - 6, info[1], anchor="ne", text=str(line),
                             font=self.number_font, fill=MUTED)
            line += 1


class TerminalPage(ttk.Frame):
    """可嵌入单根窗口的页面，后台生成只接收普通数据。"""

    def __init__(self, master: tk.Misc, *,
                 on_issues: Callable[[list[UnifiedIssue]], None] | None = None,
                 on_changed: Callable[[], None] | None = None,
                 on_busy: Callable[[bool], None] | None = None):
        super().__init__(master)
        self.project: Path | None = None
        self.service: TerminalService | None = None
        self.on_issues = on_issues or (lambda _issues: None)
        self.on_changed = on_changed or (lambda: None)
        self.on_busy = on_busy or (lambda _busy: None)
        self.busy = False
        self._external_busy = False
        self._active = False
        self._disposed = False
        self._live_job: str | None = None
        self._poll_job: str | None = None
        self.strip_dirs: dict[str, str] = {}
        self.strip_idents: dict[str, str] = {}
        self.direction_memory: dict[str, str] = {}
        self.last_output: str | None = None
        self.last_wiring_output: str | None = None
        self._editors: list[tk.Widget] = []
        self._actions: list[ttk.Button] = []
        self._gutters: list[LineNumbers] = []
        self.ui_scale = self.winfo_fpixels("1i") / 96.0
        self._menu = tk.Menu(self, tearoff=False)
        self._build_ui()
        # 根窗口统一分发 F5 / Ctrl+Enter；Text 仅负责截断 Return 类绑定。
        self.bind("<Destroy>", self._on_destroy, add="+")
        self.write(GUIDE)
        self._update_enabled()

    def px(self, value: int) -> int:
        return round(value * self.ui_scale)

    def _button(self, parent: tk.Misc, text: str, command: Callable,
                **kwargs: Any) -> ttk.Button:
        button = ttk.Button(parent, text=text, command=command, **kwargs)
        self._actions.append(button)
        return button

    def _build_ui(self) -> None:
        heading = ttk.Frame(self, padding=(12, 8, 12, 5))
        heading.pack(fill="x")
        ttk.Label(heading, text="端子排出图", font=(UI_FONT, 15, "bold")).pack(side="left")
        ttk.Label(heading, text="F5 检查 · Ctrl+Enter 生成", foreground=MUTED).pack(side="right")
        self.status = tk.StringVar(self, "请先新建或打开工程")
        ttk.Label(self, textvariable=self.status, foreground=MUTED, padding=(12, 4)).pack(side="bottom", fill="x")
        panes = ttk.Panedwindow(self, orient="horizontal")
        panes.pack(fill="both", expand=True, padx=12, pady=(3, 6))
        left = ttk.Frame(panes)
        right_holder = ttk.Frame(panes)
        panes.add(left, weight=3)
        panes.add(right_holder, weight=2)
        inputs = ttk.Panedwindow(left, orient="vertical")
        inputs.pack(fill="both", expand=True)
        top = ttk.Frame(inputs)
        bottom = ttk.Frame(inputs, padding=(0, 8, 0, 0))
        inputs.add(top, weight=3)
        inputs.add(bottom, weight=2)
        self.terminals = self._input_section(top, "① 端子排 · 坐标名称以“端子名”结尾", 10, live=True)
        self.wiring = self._input_section(bottom, "② 接线 · 端子号、原理号、去向柜", 8)

        # 右栏整体可滚动，缩放或窗口缩小时仍可访问全部设置与日志。
        self.right_canvas = tk.Canvas(right_holder, highlightthickness=0, width=self.px(410))
        scroll = ttk.Scrollbar(right_holder, orient="vertical", command=self.right_canvas.yview)
        self.right_canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.right_canvas.pack(side="left", fill="both", expand=True)
        right = ttk.Frame(self.right_canvas, padding=(10, 0, 2, 0))
        window = self.right_canvas.create_window((0, 0), window=right, anchor="nw")
        right.bind("<Configure>", lambda _e: self.right_canvas.configure(scrollregion=self.right_canvas.bbox("all")))
        self.right_canvas.bind("<Configure>", lambda e: self.right_canvas.itemconfigure(window, width=e.width))

        settings = ttk.LabelFrame(right, text="出图设置", padding=8)
        settings.pack(fill="x")
        settings.columnconfigure(1, weight=1)
        ttk.Label(settings, text="柜名").grid(row=0, column=0, sticky="w")
        self.cabinet = ttk.Entry(settings)
        self.cabinet.grid(row=0, column=1, sticky="ew", padx=(8, 0), pady=2)
        self.cabinet.bind("<Button-3>", self._edit_menu)
        self._editors.append(self.cabinet)
        ttk.Label(settings, text="默认方向").grid(row=1, column=0, sticky="w")
        self.direction = ttk.Combobox(settings, values=list(ARROWS), state="readonly", width=8)
        self.direction.set("向下")
        self.direction.grid(row=1, column=1, sticky="w", padx=(8, 0), pady=2)
        self.direction.bind("<<ComboboxSelected>>", self._on_default_direction)
        self._editors.append(self.direction)
        ttk.Label(settings, text="改动后应用到全部块", foreground=MUTED).grid(row=2, column=1, sticky="w", padx=(8, 0))
        self.auto_open = tk.BooleanVar(self, False)
        check = ttk.Checkbutton(settings, text="生成后自动打开完整图", variable=self.auto_open)
        check.grid(row=3, column=0, columnspan=2, sticky="w", pady=(5, 0))
        self._editors.append(check)
        ttk.Label(settings, text="输出：当前工程 / outputs / 端子排", foreground=MUTED).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(4, 0))

        actions = ttk.Frame(right)
        actions.pack(fill="x", pady=(8, 0))
        for i in range(4):
            actions.columnconfigure(i, weight=1, uniform="terminal_actions")
        self.build_button = self._button(actions, "生成 DXF", self.build, width=9, style="Primary.TButton")
        self.build_button.grid(row=0, column=0, columnspan=2, sticky="ew", padx=(0, 3))
        self.inspect_button = self._button(actions, "检查输入", self.inspect, width=9)
        self.inspect_button.grid(row=0, column=2, columnspan=2, sticky="ew", padx=(3, 0))
        self._button(actions, "填入示例", self.demo, width=9).grid(row=1, column=0, columnspan=2, sticky="ew", padx=(0, 3), pady=(4, 0))
        self._button(actions, "清空输入", self.clear, width=9).grid(row=1, column=2, columnspan=2, sticky="ew", padx=(3, 0), pady=(4, 0))
        opens = ttk.Frame(right)
        opens.pack(fill="x", pady=(4, 0))
        self.open_button = ttk.Button(opens, text="完整图", width=7, command=self.open_output)
        self.open_button.pack(side="left", fill="x", expand=True)
        self.wiring_button = ttk.Button(opens, text="仅接线图", width=9, command=lambda: self.open_output(wiring=True))
        self.wiring_button.pack(side="left", fill="x", expand=True, padx=4)
        self.folder_button = ttk.Button(opens, text="文件夹", width=7, command=self.open_folder)
        self.folder_button.pack(side="left", fill="x", expand=True)
        self._build_strip_table(right)
        ttk.Label(right, text="诊断 / 生成结果").pack(anchor="w", pady=(8, 3))
        result_frame = ttk.Frame(right)
        result_frame.pack(fill="both", expand=True)
        self.result = tk.Text(result_frame, width=30, height=9, state="disabled", wrap="word",
                              relief="flat", padx=8, pady=6, font=(UI_FONT, 10))
        ys = ttk.Scrollbar(result_frame, orient="vertical", command=self.result.yview)
        self.result.configure(yscrollcommand=ys.set)
        self.result.pack(side="left", fill="both", expand=True)
        ys.pack(side="right", fill="y")
        for tag, color in (("error", "#b42332"), ("warn", "#975b0c"), ("ok", "#237748")):
            self.result.tag_configure(tag, foreground=color)
        self.result.bind("<Button-3>", self._edit_menu)
        self._bind_right_wheel(right_holder)

    def _bind_right_wheel(self, widget: tk.Misc) -> None:
        """右栏滚动仅绑定本页子控件，不污染主窗口和其他页面。"""
        widget.bind("<MouseWheel>", self._scroll_right, add="+")
        for child in widget.winfo_children():
            self._bind_right_wheel(child)

    def _scroll_right(self, event: tk.Event) -> str | None:
        if self._disposed or not event.delta:
            return "break"
        # 日志和端子列表优先滚动自身；到边界后继续滚动整个右栏。
        if isinstance(event.widget, (tk.Text, ttk.Treeview)):
            first, last = event.widget.yview()
            if (event.delta > 0 and first > 0) or (event.delta < 0 and last < 1):
                return None
        steps = max(1, abs(event.delta) // 120) * 3
        self.right_canvas.yview_scroll(-steps if event.delta > 0 else steps, "units")
        return "break"

    def _build_strip_table(self, parent: tk.Misc) -> None:
        box = ttk.LabelFrame(parent, text="各块端子排的电缆方向", padding=6)
        box.pack(fill="x", pady=(8, 0))
        holder = ttk.Frame(box)
        holder.pack(fill="x")
        table = ttk.Treeview(holder, columns=("name", "count", "dir"), show="headings", height=3, selectmode="extended")
        for key, title, width, anchor in (("name", "端子排", 135, "w"), ("count", "端子数", 56, "center"), ("dir", "方向", 72, "center")):
            table.heading(key, text=title, anchor=anchor)
            table.column(key, width=self.px(width), minwidth=self.px(40), anchor=anchor, stretch=key == "name")
        table.tag_configure("up", foreground="#1f5fa8")
        ys = ttk.Scrollbar(holder, orient="vertical", command=table.yview)
        table.configure(yscrollcommand=ys.set)
        table.pack(side="left", fill="x", expand=True)
        ys.pack(side="right", fill="y")
        table.bind("<Button-1>", self._on_table_click)
        table.bind("<space>", self._toggle_selected)
        self.strip_table = table
        self.strip_hint = tk.StringVar(self, "粘贴后自动识别；单击方向格切换")
        ttk.Label(box, textvariable=self.strip_hint, foreground=MUTED, wraplength=self.px(350)).pack(anchor="w", pady=(3, 0))
        buttons = ttk.Frame(box)
        buttons.pack(fill="x", pady=(4, 0))
        self._button(buttons, "选中向下", lambda: self._set_selected("向下"), width=9).pack(side="right")
        self._button(buttons, "选中向上", lambda: self._set_selected("向上"), width=9).pack(side="right", padx=4)

    def _input_section(self, parent: tk.Misc, title: str, height: int, live: bool = False) -> tk.Text:
        head = ttk.Frame(parent)
        head.pack(fill="x")
        ttk.Label(head, text=title).pack(side="left")
        frame = ttk.Frame(parent)
        box = tk.Text(frame, wrap="none", undo=True, font=CODE_FONT, width=34, height=height,
                      relief="flat", borderwidth=0, highlightthickness=1, highlightbackground="#c9d5e1", padx=6, pady=4)
        self._editors.append(box)
        gutter = LineNumbers(frame, box)
        self._gutters.append(gutter)
        ys = ttk.Scrollbar(frame, orient="vertical", command=box.yview)
        xs = ttk.Scrollbar(frame, orient="horizontal", command=box.xview)

        def on_yscroll(first: str, last: str) -> None:
            ys.set(first, last)
            gutter.schedule()

        def on_modified(_event: tk.Event) -> None:
            if not box.edit_modified():
                return
            box.edit_modified(False)
            gutter.schedule()
            if live and not self.busy and not self._external_busy and not self._disposed:
                self._schedule_live_refresh()

        box.configure(yscrollcommand=on_yscroll, xscrollcommand=xs.set)
        gutter.grid(row=0, column=0, sticky="ns")
        box.grid(row=0, column=1, sticky="nsew")
        ys.grid(row=0, column=2, sticky="ns")
        xs.grid(row=1, column=0, columnspan=2, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(1, weight=1)
        box.bind("<<Modified>>", on_modified)
        box.bind("<Configure>", gutter.schedule)
        box.bind("<Control-Return>", self._shortcut_build)
        box.bind("<Button-3>", self._edit_menu)
        gutter.bind("<MouseWheel>", lambda e: box.yview_scroll(-1 if e.delta > 0 else 1, "units"))
        self._button(head, "导入文本…", lambda: self.import_file(box), width=10).pack(side="right")
        frame.pack(fill="both", expand=True, pady=(4, 0))
        return box

    def _edit_menu(self, event: tk.Event) -> str:
        widget = event.widget
        editable = str(widget.cget("state")) != "disabled"
        self._menu.delete(0, "end")
        if editable:
            widget.focus_set()
            self._menu.add_command(label="剪切", command=lambda: widget.event_generate("<<Cut>>"))
        self._menu.add_command(label="复制", command=lambda: widget.event_generate("<<Copy>>"))
        if editable:
            self._menu.add_command(label="粘贴", command=lambda: widget.event_generate("<<Paste>>"))
            if isinstance(widget, tk.Text):
                self._menu.add_command(label="撤销", command=lambda: widget.event_generate("<<Undo>>"))
        self._menu.add_separator()
        self._menu.add_command(label="全选", command=lambda: widget.event_generate("<<SelectAll>>"))
        try:
            self._menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._menu.grab_release()
        return "break"

    @staticmethod
    def get_text(box: tk.Text) -> str:
        return box.get("1.0", "end-1c")

    def _snapshot(self) -> dict[str, Any]:
        memory = dict(self.direction_memory)
        memory.update({self.strip_idents[key]: value for key, value in self.strip_dirs.items()})
        return {"terminals": self.get_text(self.terminals), "wiring": self.get_text(self.wiring),
                "cabinet": self.cabinet.get(), "direction": self.direction.get(),
                "directions": {key: "UP" if value == "向上" else "DOWN" for key, value in self.strip_dirs.items()},
                "directionMemory": memory, "autoOpen": self.auto_open.get()}

    def save_state(self) -> bool:
        if self._disposed:
            return False
        if self.busy:
            self.status.set("请等待 DXF 生成完成后保存端子数据")
            return False
        if self.service is None:
            return True
        try:
            self.service.save_state(self._snapshot())
        except Exception as error:
            self.status.set("保存失败，当前输入仍保留在页面中")
            messagebox.showerror("端子数据未保存", f"无法保存当前工程的端子输入：\n{error}\n\n请检查目录权限或磁盘空间后重试。", parent=self)
            return False
        self.on_changed()
        return True

    def set_project(self, project: Path | None) -> bool:
        if self._disposed:
            return False
        target = Path(project).resolve() if project is not None else None
        if target == self.project:
            return True
        if self.busy or self._external_busy:
            self.status.set("请等待当前工程的任务完成后再切换工程")
            return False
        if not self.save_state():
            return False
        try:
            service = TerminalService(target) if target is not None else None
            state = service.load_state() if service else {}
        except Exception as error:
            messagebox.showerror("无法读取端子数据", str(error), parent=self)
            return False
        self._cancel_job("_live_job")
        self.project, self.service = target, service
        self.direction_memory = dict(state.get("directionMemory", {}))
        for box, key in ((self.terminals, "terminals"), (self.wiring, "wiring")):
            box.configure(state="normal")
            box.delete("1.0", "end")
            box.insert("1.0", state.get(key, ""))
            box.edit_reset()
            box.edit_modified(False)
        self.cabinet.configure(state="normal")
        self.cabinet.delete(0, "end")
        self.cabinet.insert(0, state.get("cabinet", ""))
        self.direction.set(state.get("direction", "向下"))
        self.auto_open.set(state.get("autoOpen") is True)
        self.last_output = self.last_wiring_output = None
        self.refresh_directions()
        self._update_enabled()
        warnings = list(service.load_warnings) if service else []
        self.write(("已恢复当前工程的端子输入和方向设置。\n\n" if self._has_input() else "") + GUIDE)
        if warnings:
            self.append("\n\n读取提醒：\n" + "\n".join(warnings), "warn")
        self.on_issues([UnifiedIssue("端子", "警告", text) for text in warnings])
        self.status.set(f"当前工程：{target.name}" if target else "请先新建或打开工程")
        return True

    def activate(self, active: bool) -> None:
        self._active = bool(active) and not self._disposed

    def set_external_busy(self, busy: bool) -> None:
        """与工作台其他后台任务互斥，避免计算时同时修改工程输入。"""
        if self._disposed:
            return
        self._external_busy = bool(busy)
        if busy:
            self._cancel_job("_live_job")
        elif self.project is not None and not self.busy:
            self._schedule_live_refresh()
        self._update_enabled()

    def _shortcut_inspect(self, _event: tk.Event | None = None) -> str | None:
        if self._active and not self._disposed:
            if not self.busy and not self._external_busy:
                self.inspect()
            return "break"
        return None

    def _shortcut_build(self, _event: tk.Event | None = None) -> str:
        if self._active and not self._disposed and not self.busy and not self._external_busy:
            self.build()
        # 始终在 Text 的 Return 类绑定前截断，避免光标所在行被拆开。
        return "break"

    def _cancel_job(self, name: str) -> None:
        job = getattr(self, name)
        if job is not None:
            self.after_cancel(job)
            setattr(self, name, None)

    def dispose(self) -> bool:
        if self.busy:
            return False
        if self._disposed:
            return True
        self._disposed = True
        self._active = False
        for name in ("_live_job", "_poll_job"):
            self._cancel_job(name)
        for gutter in self._gutters:
            gutter.cancel()
        if self.winfo_exists():
            self._update_enabled()
        return True

    def on_close(self) -> bool:
        if self.busy or not self.save_state():
            return False
        return self.dispose()

    def _on_destroy(self, event: tk.Event) -> None:
        if event.widget is self:
            # 外部直接销毁页面时取消排队回调，工作线程只写普通数据队列。
            self.busy = False
            self.dispose()

    def _has_input(self) -> bool:
        return bool(self.get_text(self.terminals).strip() or self.get_text(self.wiring).strip())

    def write(self, text: str = "", tag: str | None = None) -> None:
        self.result.configure(state="normal")
        self.result.delete("1.0", "end")
        self.result.configure(state="disabled")
        if text:
            self.append(text, tag)

    def append(self, text: str, tag: str | None = None) -> None:
        self.result.configure(state="normal")
        self.result.insert("end", text, tag or ())
        self.result.configure(state="disabled")

    def _ready(self) -> bool:
        if self.busy or self._external_busy or self._disposed:
            return False
        if self.service is None:
            self.status.set("请先新建或打开工程")
            return False
        return True

    def demo(self) -> None:
        if not self._ready():
            return
        if self._has_input() and not messagebox.askyesno("填入示例", "示例会覆盖当前输入，继续吗？\n输入框内容可按 Ctrl+Z 撤销。", parent=self):
            return
        for box, value in ((self.terminals, EXAMPLE_TERMINALS), (self.wiring, EXAMPLE_WIRING)):
            box.delete("1.0", "end")
            box.insert("1.0", value)
        self.cabinet.delete(0, "end")
        self.cabinet.insert(0, EXAMPLE_CABINET)
        self.refresh_directions()
        self.write("已填入示例。按 F5 检查，Ctrl+Enter 生成 DXF。")

    def clear(self) -> None:
        if not self._ready() or not self._has_input():
            return
        if not messagebox.askyesno("清空输入", "清空端子排和接线输入？\n可按 Ctrl+Z 撤销。", parent=self):
            return
        for box in (self.terminals, self.wiring):
            box.delete("1.0", "end")
        self.refresh_directions()
        self.write(GUIDE)

    def import_file(self, box: tk.Text) -> None:
        if not self._ready():
            return
        path = filedialog.askopenfilename(title="导入文本", parent=self,
            initialdir=str(self.project), filetypes=[("文本文件", "*.txt *.csv *.tsv"), ("所有文件", "*.*")])
        if not path:
            return
        try:
            if Path(path).stat().st_size > MAX_IMPORT_BYTES:
                raise ValueError("文件超过 5 MB，不像是端子排或接线清单。")
            content = read_text_file(path)
        except (OSError, UnicodeError, ValueError) as error:
            messagebox.showerror("导入失败", str(error), parent=self)
            return
        box.delete("1.0", "end")
        box.insert("1.0", content.replace("\r\n", "\n").replace("\r", "\n"))
        box.focus_set()
        self.status.set(f"已导入 {Path(path).name}（可按 Ctrl+Z 撤销）")

    def _publish_issues(self, result: dict[str, Any]) -> None:
        self.on_issues([UnifiedIssue("端子", level, str(text)) for key, level in
                       (("errors", "错误"), ("warnings", "警告")) for text in result.get(key, [])])

    def inspect(self) -> dict[str, Any] | None:
        if not self._ready():
            return None
        self.refresh_directions()
        result = TerminalService.inspect(self._snapshot())
        errors = result.get("errors", [])
        warnings = result.get("warnings", [])
        self.write("识别到的端子排：\n" + "\n".join(result.get("parsed", []) or ["（没有识别到端子排）"]) + "\n\n")
        if errors:
            self.append(f"输入问题（{len(errors)} 处）：\n" + "\n".join(errors) + "\n", "error")
        else:
            self.append("输入检查通过。\n", "ok")
        if warnings:
            self.append("\n提醒：\n" + "\n".join(warnings) + "\n", "warn")
        self._publish_issues(result)
        self.status.set(f"检查发现 {len(errors)} 处问题" if errors else "检查通过")
        return result

    def refresh_directions(self) -> None:
        text = self.get_text(self.terminals)
        result = TerminalService.inspect({"terminals": text, "wiring": ""})
        strips = result.get("strips", [])
        selected = set(self.strip_table.selection())
        self.strip_table.delete(*self.strip_table.get_children())
        self.strip_dirs, self.strip_idents = {}, {}
        for strip in strips:
            ident = strip["ident"]
            value = self.direction_memory.get(ident, self.direction.get())
            if value not in ARROWS:
                value = "向下"
            key = strip["key"]
            self.strip_dirs[key], self.strip_idents[key] = value, ident
            self.strip_table.insert("", "end", iid=key, values=(strip["name"], strip["terminals"], ARROWS[value]), tags=("up",) if value == "向上" else ())
        keep = [key for key in selected if key in self.strip_dirs]
        if keep:
            self.strip_table.selection_set(keep)
        errors = result.get("errors", [])
        if errors and text.strip():
            self.strip_hint.set(f"输入有 {len(errors)} 处问题，按 F5 查看")
        elif strips:
            self.strip_hint.set(f"共 {len(strips)} 块、{sum(s['terminals'] for s in strips)} 个端子")
        else:
            self.strip_hint.set("粘贴后自动识别；单击方向格切换")

    def _schedule_live_refresh(self) -> None:
        self._cancel_job("_live_job")
        self._live_job = self.after(400, self._live_refresh)

    def _live_refresh(self) -> None:
        self._live_job = None
        if not self._disposed and not self.busy and not self._external_busy:
            self.refresh_directions()

    def _apply_directions(self, keys: list[str], value: str) -> None:
        if not self._ready():
            return
        for key in keys:
            self.strip_dirs[key] = value
            self.direction_memory[self.strip_idents[key]] = value
            self.strip_table.set(key, "dir", ARROWS[value])
            self.strip_table.item(key, tags=("up",) if value == "向上" else ())

    def _on_default_direction(self, _event: tk.Event | None = None) -> None:
        self._apply_directions(list(self.strip_dirs), self.direction.get())

    def _on_table_click(self, event: tk.Event) -> str | None:
        if self.busy or self._external_busy or self._disposed or self.project is None:
            return "break"
        if self.strip_table.identify_region(event.x, event.y) == "cell" and self.strip_table.identify_column(event.x) == "#3":
            key = self.strip_table.identify_row(event.y)
            if key:
                self._apply_directions([key], "向下" if self.strip_dirs[key] == "向上" else "向上")
            return "break"
        return None

    def _toggle_selected(self, _event: tk.Event | None = None) -> str:
        for key in self.strip_table.selection():
            self._apply_directions([key], "向下" if self.strip_dirs[key] == "向上" else "向上")
        return "break"

    def _set_selected(self, value: str) -> None:
        keys = list(self.strip_table.selection())
        if not keys:
            self.status.set("先选中端子排；按住 Ctrl 或 Shift 可多选")
            return
        self._apply_directions(keys, value)

    def _update_enabled(self) -> None:
        enabled = self.project is not None and not self.busy and not self._external_busy and not self._disposed
        for widget in self._editors:
            widget.configure(state=("readonly" if widget is self.direction else "normal") if enabled else "disabled")
        for button in self._actions:
            button.configure(state="normal" if enabled else "disabled")
        for button, path in ((self.open_button, self.last_output), (self.wiring_button, self.last_wiring_output)):
            button.configure(state="normal" if enabled and path and Path(path).is_file() else "disabled")
        self.folder_button.configure(state="normal" if enabled else "disabled")

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        self._update_enabled()
        self.configure(cursor="watch" if busy else "")
        if busy:
            self.status.set("正在生成 DXF……")
        self.on_busy(busy)

    def build(self) -> None:
        if not self._ready():
            return
        self._cancel_job("_live_job")
        self.refresh_directions()
        payload, service = self._snapshot(), self.service
        payload["direction"] = "UP" if payload["direction"] == "向上" else "DOWN"
        self._set_busy(True)
        self.write("正在生成 DXF，请稍候……")
        results: queue.Queue[dict[str, Any]] = queue.Queue()

        def work() -> None:
            try:
                results.put(service.generate(payload))
            except Exception as error:
                results.put({"ok": False, "errors": [f"生成未完成：{error}"], "traceback": traceback.format_exc()})

        def poll() -> None:
            if self._disposed:
                self._poll_job = None
                return
            try:
                result = results.get_nowait()
            except queue.Empty:
                self._poll_job = self.after(50, poll)
                return
            self._poll_job = None
            self._show_result(result)

        gc.collect()  # 避免工作线程触发旧 Tk 对象的循环垃圾回收。
        try:
            threading.Thread(target=work, daemon=True).start()
            self._poll_job = self.after(50, poll)
        except Exception as error:
            self._show_result({"ok": False, "errors": [f"无法开始生成：{error}"], "traceback": traceback.format_exc()})

    def _show_result(self, result: dict[str, Any]) -> None:
        self._set_busy(False)
        self._publish_issues(result)
        if not result.get("ok"):
            self.last_output = self.last_wiring_output = None
            self._update_enabled()
            self.write("没有生成，请先处理这些问题：\n" + "\n".join(result.get("errors", [])), "error")
            if result.get("traceback"):
                self.append("\n\n技术日志：\n" + result["traceback"])
            self.status.set("生成失败，输入仍保留")
            return
        self.last_output = result.get("path")
        self.last_wiring_output = result.get("wiringPath")
        self._update_enabled()
        stats = result.get("stats", {})
        self.write("生成成功\n", "ok")
        self.append(f"完整图：{self.last_output}\n仅接线图：{self.last_wiring_output or ''}\n"
                    f"{stats.get('blocks', 0)} 块端子排、{stats.get('terminals', 0)} 个端子、{stats.get('cables', 0)} 根电缆\n"
                    + result.get("verify", "") + "\n" + "\n".join(result.get("parsed", [])))
        if result.get("cables"):
            self.append("\n\n电缆清单：\n" + "\n".join(f"{c['number']} 至 {c['destination']}：{'、'.join(c['points'])}" for c in result["cables"]))
        if result.get("warnings"):
            self.append("\n\n提醒：\n" + "\n".join(result["warnings"]), "warn")
        self.status.set(f"已生成：{Path(self.last_output).name}")
        self.save_state()
        if self.auto_open.get():
            self.open_output()

    def open_output(self, wiring: bool = False) -> None:
        path = self.last_wiring_output if wiring else self.last_output
        if not path or not Path(path).is_file():
            self._update_enabled()
            messagebox.showwarning("没有文件", "请先生成 DXF 文件。", parent=self)
            return
        try:
            os.startfile(path)
        except OSError as error:
            messagebox.showerror("打开失败", str(error), parent=self)

    def open_folder(self) -> None:
        if self.project is None:
            return
        try:
            if self.last_output and Path(self.last_output).is_file():
                subprocess.Popen(["explorer.exe", f"/select,{self.last_output}"])
            else:
                folder = self.project / "outputs" / "端子排"
                folder.mkdir(parents=True, exist_ok=True)
                os.startfile(str(folder))
        except OSError as error:
            messagebox.showerror("打开失败", str(error), parent=self)
