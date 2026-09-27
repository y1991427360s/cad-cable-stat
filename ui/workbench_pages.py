"""工程概览、成果与模块导航；不参与电缆或端子计算。"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk
import traceback

from core.models import UnifiedIssue


class WorkbenchPages:
    @property
    def task_busy(self) -> bool:
        return self.busy or bool(getattr(self, "terminal_page", None) and self.terminal_page.busy)

    def show_page(self, name: str) -> None:
        self.notebook.select(self.tabs[name])
        self._page_changed()

    def _page_changed(self, _event=None) -> None:
        selected = self.notebook.select()
        terminal = selected == str(self.tabs["端子排出图"])
        if hasattr(self, "terminal_page"):
            self.terminal_page.activate(terminal)
        for name, button in self.nav_buttons.items():
            button.configure(bg="#305776" if selected == str(self.tabs[name]) else "#162c45")
        if hasattr(self, "elapsed") and not self.task_busy:
            self.elapsed.set("F5 检查输入  ·  Ctrl+Enter 生成 DXF" if terminal else "F5 刷新工程  ·  Ctrl+Enter 计算电缆")
        if selected == str(self.tabs["输出文件"]):
            self._refresh_outputs()
        elif selected == str(self.tabs["工程概览"]):
            self._refresh_module_status()

    def _shortcut_inspect(self, _event=None):
        if self.notebook.select() == str(self.tabs["端子排出图"]):
            self.terminal_page.inspect()
        else:
            self.refresh()
        return "break"

    def _shortcut_build(self, _event=None):
        if self.notebook.select() == str(self.tabs["端子排出图"]):
            self.terminal_page.build()
        else:
            self.start_calculation()
        return "break"

    def _build_route(self) -> None:
        page = self.tabs["路径复核"]
        ttk.Label(page, text="电缆路径复核", style="Title.TLabel").pack(anchor="w", pady=(0, 18))
        ttk.Label(page, text="完成电缆计算后，可打开当前工程的路径可视化。\n支持路径高亮、楼层切换、断点检查和三维交互。",
                  style="Card.TLabel", justify="left").pack(anchor="w", pady=(0, 18))
        self._button(page, "打开路径可视化", lambda: self.open_artifact("html"), primary=True).pack(anchor="w")
        self.route_notice = tk.StringVar(value="请先选择工程。")
        ttk.Label(page, textvariable=self.route_notice, style="Card.TLabel", wraplength=760).pack(anchor="w", pady=20)
        self._button(page, "查看输出文件", lambda: self.show_page("输出文件")).pack(anchor="w")

    def _build_cad(self) -> None:
        page = self.tabs["数据导出"]
        ttk.Label(page, text="CAD 数据导出", style="Title.TLabel").pack(anchor="w", pady=(0, 18))
        text = ("1. 导出 CAD 向导，在 CAD 中用 APPLOAD 加载 cad_cable_wizard.lsp。\n\n"
                "2. 运行 DDFD_CABLE_WIZARD，按向导标记柜子、路径、竖井和房间。\n\n"
                "3. 将导出位置设为当前工程的 data 文件夹。\n\n"
                "4. 回到工作台刷新输入，核对柜名后计算电缆。")
        label = ttk.Label(page, text=text, style="Card.TLabel", justify="left", wraplength=750)
        label.pack(anchor="w", fill="x", pady=(0, 20))
        label.bind("<Configure>", lambda e: label.configure(wraplength=max(240, e.width - 10)))
        self._button(page, "导出 CAD 向导…", self.export_cad, primary=True, mutate=True).pack(anchor="w")
        self._button(page, "打开工程数据文件夹", lambda: self._open_path(self.project / "data") if self.project else None).pack(anchor="w", pady=12)

    def _build_outputs(self) -> None:
        page = self.tabs["输出文件"]
        bar = ttk.Frame(page, style="Card.TFrame")
        bar.pack(fill="x")
        for label, callback in (("刷新", self._refresh_outputs), ("打开", self._open_selected_output),
                                ("打开所在文件夹", lambda: self._open_selected_output(folder=True))):
            self._button(bar, label, callback).pack(side="left", padx=(0, 8))
        self.output_notice = tk.StringVar(value="选择工程后查看输出成果。")
        ttk.Label(page, textvariable=self.output_notice, style="Card.TLabel").pack(anchor="w", pady=(10, 0))
        self.output_tree = self._table(page, [("source", "模块", 65), ("file", "输出文件", 380),
                                              ("size", "大小", 90), ("modified", "修改时间", 165)])
        self.output_tree.bind("<Double-1>", lambda _e: self._open_selected_output())

    def _refresh_outputs(self) -> None:
        if not hasattr(self, "output_tree"):
            return
        self.output_tree.delete(*self.output_tree.get_children())
        self.output_paths.clear()
        if not self.project:
            return
        output = self.project / "outputs"
        try:
            files = sorted((p for p in output.rglob("*") if p.is_file() and not p.name.startswith("~$")),
                           key=lambda p: str(p.relative_to(output)))
            for index, path in enumerate(files):
                info = path.stat()
                relative = path.relative_to(output)
                source = "端子" if relative.parts[0] == "端子排" else "电缆"
                self.output_paths[str(index)] = path
                self.output_tree.insert("", "end", iid=str(index), values=(source, str(relative),
                    f"{info.st_size / 1024:,.1f} KB", datetime.fromtimestamp(info.st_mtime).strftime("%Y-%m-%d %H:%M")))
            self.output_notice.set(f"共 {len(files)} 个成果文件 · {output}")
        except OSError as exc:
            self.output_notice.set(f"读取成果失败：{exc}")

    def _open_selected_output(self, folder: bool = False) -> None:
        selected = self.output_tree.selection()
        path = self.output_paths.get(selected[0]) if selected else None
        if path:
            self._open_path(path.parent if folder else path)
        elif folder and self.project:
            self._open_path(self.project / "outputs")

    def _terminal_issues_changed(self, issues: list[UnifiedIssue]) -> None:
        self.terminal_issues = issues
        self._render_issues()

    def _terminal_changed(self) -> None:
        if not hasattr(self, "terminal_page"):
            return
        self._refresh_module_status()
        self._refresh_outputs()

    def _terminal_busy_changed(self, busy: bool) -> None:
        self._update_enabled()
        if busy:
            self.state_text.set("正在生成端子 DXF…")
        else:
            self.state_text.set("端子任务已结束 · 结果见端子页面及输出文件")
        # 延迟到 page 处理完结果及保存后再关闭。
        if not busy and self.close_when_done:
            self._later(100, self.on_close)

    def _refresh_module_status(self) -> None:
        if not self.project or not hasattr(self, "terminal_page"):
            return
        terminal = self.terminal_page
        outputs = self.project / "outputs"
        rows = [
            ("cable_result", "电缆计算结果", (outputs / "自动统计_计算结果.xlsx").exists(), "已生成", "未生成"),
            ("terminal_input", "端子排输入", bool(terminal.get_text(terminal.terminals).strip()), "已有", "空"),
            ("terminal_wiring", "接线数据", bool(terminal.get_text(terminal.wiring).strip()), "已有", "空"),
            ("terminal_dxf", "端子 DXF", any((outputs / "端子排").glob("*.dxf")), "已生成", "未生成"),
        ]
        for key, label, exists, yes, no in rows:
            if self.file_tree.exists(key):
                self.file_tree.delete(key)
            self.file_tree.insert("", "end", iid=key, values=(label, "—", yes if exists else no, ""))
        html = outputs / "路径可视化.html"
        self.route_notice.set(("已生成：" if html.exists() else "尚未生成：") + str(html))

    def _callback_error(self, kind, value, trace) -> None:
        if hasattr(self, "log_text"):
            self._append_log("".join(traceback.format_exception(kind, value, trace)), "error")
        self._error("操作未完成", f"{value or kind.__name__}\n详细信息已记录在运行日志。")
