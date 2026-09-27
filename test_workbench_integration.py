"""单窗口工作台整合回归：真实 Tk、隔离工程、实际电缆计算和 DXF 回读。"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import tkinter as tk
import unittest
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import ezdxf

import cable_stat_app as app
import cable_stat_gui as gui
from cable_stat.project import save_aliases
from core.models import UnifiedIssue
from modules.terminal.service import TerminalService
from test_cable_stat_gui import make_project


class WorkbenchIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.project = make_project(self.base / "工程甲")
        self.other = make_project(self.base / "工程乙")
        self.errors = []
        self.callback_errors = []
        self.patches = [
            patch.object(app, "load_config", return_value={}),
            patch.object(app, "save_config"),
            patch.object(app, "claim_handoff_request", return_value=None),
            patch.object(gui.messagebox, "showerror", side_effect=lambda *a, **kw: self.errors.append(a)),
        ]
        for item in self.patches:
            item.start()
        self.root = tk.Tk()
        self.root.withdraw()
        with patch.object(tk, "Tk", side_effect=AssertionError("页面不可创建第二个 Tk root")):
            self.ui = gui.CableStatApp(self.root)
        self.root.report_callback_exception = lambda *args: self.callback_errors.append(args)
        self.ui.select_project(self.project)
        self.wait_idle()

    def tearDown(self):
        try:
            if not self.ui.closed:
                self.wait_idle()
                self.ui._destroy()
        finally:
            for item in reversed(self.patches):
                item.stop()
            self.temp.cleanup()
        self.assertEqual([], self.callback_errors)

    def wait_idle(self):
        deadline = time.monotonic() + 20
        while not self.ui.closed:
            self.root.update()
            if not self.ui.task_busy:
                return
            if time.monotonic() >= deadline:
                self.fail("工作台后台任务超时")
            time.sleep(0.01)

    def fill_terminal(self, name="保护柜", terminals="X、1、2、3", wiring="X1、A610、目标柜"):
        page = self.ui.terminal_page
        for widget, text in ((page.terminals, terminals), (page.wiring, wiring)):
            widget.delete("1.0", "end")
            widget.insert("1.0", text)
        page.cabinet.delete(0, "end")
        page.cabinet.insert(0, name)
        page.auto_open.set(False)
        page.refresh_directions()

    def test_one_root_and_all_pages_share_current_engineering_context(self):
        self.assertIs(self.ui.terminal_page.winfo_toplevel(), self.root)
        self.assertIs(tk._default_root, self.root)
        for name, frame in self.ui.tabs.items():
            self.ui.show_page(name)
            self.root.update_idletasks()
            self.assertEqual(str(frame), self.ui.notebook.select())
            self.assertEqual(name == "端子排出图", self.ui.terminal_page._active)
        self.assertEqual(self.project, self.ui.terminal_page.project)

    def test_overview_reflects_terminal_edits_when_returning_to_page(self):
        self.assertEqual("空", self.ui.file_tree.item("terminal_input", "values")[2])
        self.ui.show_page("端子排出图")
        self.fill_terminal()
        self.ui.show_page("工程概览")
        self.assertEqual("已有", self.ui.file_tree.item("terminal_input", "values")[2])
        self.assertEqual("已有", self.ui.file_tree.item("terminal_wiring", "values")[2])
        self.ui.terminal_page.terminals.delete("1.0", "end")
        self.ui.terminal_page.wiring.delete("1.0", "end")
        self.ui.show_page("端子排出图")
        self.ui.show_page("工程概览")
        self.assertEqual("空", self.ui.file_tree.item("terminal_input", "values")[2])
        self.assertEqual("空", self.ui.file_tree.item("terminal_wiring", "values")[2])

    def test_same_project_runs_both_modules_and_refreshes_outputs(self):
        save_aliases(self.project / "data", {"清册柜A": "柜A"})
        original = (self.project / "自动统计.xlsx").read_bytes()
        self.ui.refresh()
        self.wait_idle()
        self.ui.start_calculation()
        self.wait_idle()
        self.assertEqual(2, self.ui.result.ok)
        self.assertEqual(0, self.ui.result.failed)
        self.fill_terminal()
        self.ui.show_page("端子排出图")
        self.assertTrue(self.ui.terminal_page.inspect()["ok"])
        self.ui.terminal_page.build()
        self.wait_idle()
        for value in (self.ui.terminal_page.last_output, self.ui.terminal_page.last_wiring_output):
            self.assertTrue(value, self.ui.terminal_page.get_text(self.ui.terminal_page.result))
            path = Path(value)
            self.assertEqual(self.project / "outputs" / "端子排", path.parent)
            self.assertGreater(len(ezdxf.readfile(path).modelspace().query("LINE")), 0)
        self.assertEqual(original, (self.project / "自动统计.xlsx").read_bytes())
        self.ui.show_page("输出文件")
        self.assertIn(self.project / "outputs" / "自动统计_计算结果.xlsx", self.ui.output_paths.values())
        self.assertIn(self.project / "outputs" / "路径可视化.html", self.ui.output_paths.values())
        self.assertIn(Path(self.ui.terminal_page.last_output), self.ui.output_paths.values())
        sources = {self.ui.output_tree.item(key, "values")[0] for key in self.ui.output_tree.get_children()}
        self.assertEqual({"电缆", "端子"}, sources)
        self.assertEqual("已生成", self.ui.file_tree.item("terminal_dxf", "values")[2])
        self.assertEqual([], self.errors)

    def test_switching_engineering_projects_saves_and_restores_distinct_terminal_inputs(self):
        self.fill_terminal(name="甲柜")
        first_key = next(iter(self.ui.terminal_page.strip_dirs))
        self.ui.terminal_page._apply_directions([first_key], "向上")
        self.ui.select_project(self.other)
        self.wait_idle()
        self.assertEqual("", self.ui.terminal_page.get_text(self.ui.terminal_page.terminals))
        self.fill_terminal(name="乙柜", terminals="Y、8、9", wiring="Y8、N600、另一柜")
        self.ui.select_project(self.project)
        self.wait_idle()
        page = self.ui.terminal_page
        self.assertEqual("甲柜", page.cabinet.get())
        self.assertEqual("X、1、2、3", page.get_text(page.terminals))
        self.assertEqual({"向上"}, set(page.strip_dirs.values()))
        self.assertEqual("乙柜", TerminalService(self.other).load_state()["cabinet"])
        self.assertEqual("Y、8、9", TerminalService(self.other).load_state()["terminals"])

    def test_terminal_save_failure_cancels_project_switch_and_close(self):
        self.fill_terminal()
        page = self.ui.terminal_page
        with patch.object(page.service, "save_state", side_effect=OSError("测试磁盘不可写")):
            self.ui.select_project(self.other)
            self.assertEqual(self.project, self.ui.project)
            self.assertEqual(self.project, page.project)
            self.assertEqual("X、1、2、3", page.get_text(page.terminals))
            self.ui.on_close()
            self.assertFalse(self.ui.closed)
        self.assertEqual(2, len(self.errors))

    def test_terminal_worker_blocks_project_switch_and_cable_calculation(self):
        self.fill_terminal()
        page = self.ui.terminal_page
        release = threading.Event()
        original_generate = page.service.generate

        def blocked(payload):
            if not release.wait(5):
                raise TimeoutError("测试未释放端子任务")
            return original_generate(payload)

        with patch.object(page.service, "generate", side_effect=blocked), patch.object(app, "calculate_project") as calculate:
            page.build()
            try:
                self.assertTrue(self.ui.task_busy)
                self.assertEqual("disabled", str(self.ui.run_button["state"]))
                self.ui.select_project(self.other)
                self.ui.start_calculation()
                self.assertEqual(self.project, self.ui.project)
                calculate.assert_not_called()
            finally:
                release.set()
                self.wait_idle()
        self.assertFalse(page.busy)
        self.assertEqual("normal", str(self.ui.run_button["state"]))

    def test_cable_worker_blocks_terminal_generation_and_project_switch(self):
        self.fill_terminal()
        release = threading.Event()
        original_calculate = app.calculate_project

        def blocked(*args, **kwargs):
            if not release.wait(5):
                raise TimeoutError("测试未释放电缆任务")
            return original_calculate(*args, **kwargs)

        with patch.object(app, "calculate_project", side_effect=blocked), \
                patch.object(self.ui.terminal_page.service, "generate") as generate:
            self.ui.start_calculation()
            try:
                self.assertTrue(self.ui.task_busy)
                self.assertTrue(self.ui.terminal_page._external_busy)
                self.ui.terminal_page.build()
                self.ui.select_project(self.other)
                generate.assert_not_called()
                self.assertEqual(self.project, self.ui.project)
            finally:
                release.set()
                self.wait_idle()
        self.assertFalse(self.ui.terminal_page._external_busy)

    def test_terminal_worker_close_can_cancel_then_wait_until_files_are_saved(self):
        self.fill_terminal(name="关闭前的柜")
        page = self.ui.terminal_page
        release = threading.Event()
        original_generate = page.service.generate

        def blocked(payload):
            if not release.wait(5):
                raise TimeoutError("测试未释放端子任务")
            return original_generate(payload)

        with patch.object(page.service, "generate", side_effect=blocked):
            page.build()
            try:
                with patch.object(gui.messagebox, "askyesno", return_value=False):
                    self.ui.on_close()
                self.assertFalse(self.ui.closed)
                self.assertFalse(self.ui.close_when_done)
                with patch.object(gui.messagebox, "askyesno", return_value=True):
                    self.ui.on_close()
                self.assertTrue(self.ui.close_when_done)
                self.assertFalse(self.ui.closed)
            finally:
                release.set()
                self.wait_idle()
        deadline = time.monotonic() + 3
        while not self.ui.closed and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.assertTrue(self.ui.closed)
        self.assertEqual("关闭前的柜", TerminalService(self.project).load_state()["cabinet"])
        self.assertGreater(len(list((self.project / "outputs" / "端子排").glob("*.dxf"))), 0)
        self.assertEqual([], self.errors)

    def test_deferred_close_save_failure_keeps_page_and_cancels_future_close(self):
        self.fill_terminal()
        self.ui.close_when_done = True
        with patch.object(self.ui.terminal_page.service, "save_state", side_effect=OSError("测试保存失败")):
            self.ui._process_events()
            self.assertFalse(self.ui.closed)
            self.assertFalse(self.ui.close_when_done)
            self.ui._process_events()
        self.assertEqual(1, len(self.errors))
        self.assertEqual("X、1、2、3", self.ui.terminal_page.get_text(self.ui.terminal_page.terminals))

    def test_handoff_waits_for_terminal_task_then_saves_old_project_before_switch(self):
        self.fill_terminal(name="交接前甲柜")
        page = self.ui.terminal_page
        page._set_busy(True)
        try:
            request = {"project": str(self.other), "run": False}
            with patch.object(app, "claim_handoff_request", return_value=request):
                self.ui._poll_handoff()
            self.assertEqual(self.project, self.ui.project)
            self.assertEqual([request], self.ui.pending_handoffs)
        finally:
            page._set_busy(False)
        with patch.object(self.root, "deiconify"), patch.object(self.root, "lift"):
            self.ui._poll_handoff()
        self.wait_idle()
        self.assertEqual(self.other, self.ui.project)
        self.assertEqual([], self.ui.pending_handoffs)
        self.assertEqual("交接前甲柜", TerminalService(self.project).load_state()["cabinet"])
        self.assertEqual("", page.cabinet.get())

    def test_shortcuts_route_to_active_module_and_text_keeps_its_return_binding(self):
        page = self.ui.terminal_page
        with patch.object(page, "inspect") as terminal_check, patch.object(page, "build") as terminal_build, \
                patch.object(self.ui, "refresh") as cable_check, patch.object(self.ui, "start_calculation") as cable_build:
            self.ui.show_page("端子排出图")
            self.assertEqual("break", self.ui._shortcut_inspect())
            self.assertEqual("break", self.ui._shortcut_build())
            terminal_check.assert_called_once()
            terminal_build.assert_called_once()
            cable_check.assert_not_called()
            cable_build.assert_not_called()
            self.ui.show_page("计算参数")
            self.ui._shortcut_inspect()
            self.ui._shortcut_build()
            cable_check.assert_called_once()
            cable_build.assert_called_once()
            terminal_check.assert_called_once()
            terminal_build.assert_called_once()
        self.assertTrue(self.root.bind("<F5>"))
        self.assertTrue(self.root.bind("<Control-Return>"))
        for box in (page.terminals, page.wiring):
            self.assertTrue(box.bind("<Control-Return>"))
        self.assertEqual("break", page._shortcut_build())

    def test_real_key_events_route_once_without_inserting_a_text_newline(self):
        page = self.ui.terminal_page
        self.fill_terminal()
        self.root.deiconify()
        self.ui.show_page("端子排出图")
        self.root.update()
        with patch.object(page, "inspect") as terminal_check, patch.object(page, "build") as terminal_build, \
                patch.object(self.ui, "refresh") as cable_check, patch.object(self.ui, "start_calculation") as cable_build:
            for widget in (page.terminals, page.wiring, page.cabinet):
                widget.focus_force()
                self.root.update()
                before = page.get_text(widget) if isinstance(widget, tk.Text) else widget.get()
                widget.event_generate("<F5>")
                widget.event_generate("<Control-Return>")
                self.root.update()
                after = page.get_text(widget) if isinstance(widget, tk.Text) else widget.get()
                self.assertEqual(before, after)
            self.assertEqual(3, terminal_check.call_count)
            self.assertEqual(3, terminal_build.call_count)
            cable_check.assert_not_called()
            cable_build.assert_not_called()
            self.ui.show_page("计算参数")
            self.root.focus_force()
            self.root.update()
            self.root.event_generate("<F5>")
            self.root.event_generate("<Control-Return>")
            self.root.update()
            cable_check.assert_called_once()
            cable_build.assert_called_once()
        self.root.withdraw()

    def test_unified_issues_filter_by_source_severity_and_text(self):
        self.ui.issues = [("错误", "未匹配柜名"), ("警告", "路径断点")]
        self.ui._terminal_issues_changed([UnifiedIssue("端子", "错误", "第 3 行端子名缺失"),
                                          UnifiedIssue("端子", "警告", "端子编号重复")])
        self.assertEqual(4, len(self.ui.issue_tree.get_children()))
        self.ui.issue_source.set("端子")
        self.ui.issue_level.set("错误")
        self.ui.issue_search.set("端子名")
        self.ui._render_issues()
        keys = self.ui.issue_tree.get_children()
        self.assertEqual(1, len(keys))
        self.assertEqual(("端子", "错误", "第 3 行端子名缺失"), self.ui.issue_tree.item(keys[0], "values"))
        self.ui.issue_tree.selection_set(keys[0])
        self.ui._issue_selected()
        self.assertIn("第 3 行端子名缺失", self.ui.issue_detail.get("1.0", "end"))

    def test_output_open_commands_use_selected_current_project_artifact(self):
        folder = self.project / "outputs" / "端子排"
        folder.mkdir(parents=True)
        target = folder / "保护柜-端子排.dxf"
        target.write_text("test", encoding="utf-8")
        self.ui._refresh_outputs()
        key = next(key for key, path in self.ui.output_paths.items() if path == target)
        self.ui.output_tree.selection_set(key)
        with patch.object(self.ui, "_open_path") as open_path:
            self.ui._open_selected_output()
            self.ui._open_selected_output(folder=True)
        self.assertEqual([target, folder], [call.args[0] for call in open_path.call_args_list])
        self.ui.select_project(self.other)
        self.wait_idle()
        self.assertNotIn(target, self.ui.output_paths.values())


class StartupIntegrationTests(unittest.TestCase):
    def test_terminal_only_projects_open_without_automatic_cable_calculation(self):
        with TemporaryDirectory() as folder:
            terminal = Path(folder) / "仅端子工程"
            TerminalService(terminal).save_state({"terminals": "X、1、2", "cabinet": "端子柜"})
            cable = make_project(Path(folder) / "电缆工程")
            for argv, cwd, expected in (
                (["workbench", str(terminal)], Path(folder), (terminal, False)),
                (["workbench"], terminal, (terminal, False)),
                (["workbench", str(cable)], Path(folder), (cable, True)),
                (["workbench"], cable, (cable, True)),
            ):
                with self.subTest(argv=argv, cwd=cwd):
                    self.assertEqual(expected, app.startup_target(argv, cwd))

    def test_second_launch_writes_one_claimable_request_and_does_not_create_gui(self):
        with TemporaryDirectory() as folder:
            target = Path(folder) / "工程"
            target.mkdir()
            request_file = Path(folder) / "handoff.json"
            with patch.dict(os.environ, {"CABLE_STAT_SMOKETEST": "", "WORKBENCH_SMOKETEST": ""}), \
                    patch.object(app, "startup_target", return_value=(target, False)), \
                    patch.object(app, "claim_single_instance", return_value=True), \
                    patch.object(app, "allow_foreground_switch"), \
                    patch.object(app, "handoff_file", return_value=request_file), \
                    patch.object(gui, "run_gui") as run_gui:
                self.assertEqual(0, app.main())
                run_gui.assert_not_called()
                self.assertTrue(request_file.is_file())
                request = app.claim_handoff_request()
                self.assertEqual(str(target), request["project"])
                self.assertFalse(request["run"])
                self.assertFalse(request_file.exists())
                self.assertIsNone(app.claim_handoff_request())
                request_file.write_text(json.dumps(dict(request, time=time.time() - 600)), encoding="utf-8")
                self.assertIsNone(app.claim_handoff_request())

    @unittest.skipUnless(sys.platform == "win32", "Windows 命名互斥量")
    def test_native_single_instance_mutex_is_shared_by_repeated_claims(self):
        import ctypes
        from ctypes import wintypes

        start = len(app._instance_handles)
        name = "Local\\ElectricalWorkbenchIntegration-" + uuid.uuid4().hex
        try:
            self.assertIs(False, app.claim_single_instance(name))
            self.assertIs(True, app.claim_single_instance(name))
        finally:
            close = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
            close.argtypes = (wintypes.HANDLE,)
            close.restype = wintypes.BOOL
            for handle in app._instance_handles[start:]:
                close(handle)
            del app._instance_handles[start:]


class WorkbenchLayoutTests(unittest.TestCase):
    def test_major_controls_fit_at_simulated_1920x1080_125_percent(self):
        root = tk.Tk()
        root.withdraw()
        ui = None
        callback_errors = []
        previous_scaling = root.tk.call("tk", "scaling")
        try:
            root.tk.call("tk", "scaling", 120 / 72)
            with patch.object(root, "winfo_screenwidth", return_value=1920), \
                    patch.object(root, "winfo_screenheight", return_value=1080), \
                    patch.object(app, "load_config", return_value={}), \
                    patch.object(app, "save_config"), \
                    patch.object(app, "claim_handoff_request", return_value=None):
                ui = gui.CableStatApp(root)
                root.report_callback_exception = lambda *args: callback_errors.append(args)
                root.deiconify()
                root.update()
                self.assertAlmostEqual(1525, root.winfo_width(), delta=3)
                self.assertEqual(980, root.winfo_height())
                for name in ("工程概览", "计算参数", "端子排出图", "问题清单", "输出文件"):
                    ui.show_page(name)
                    root.update()
                    widgets = [ui.run_button, ui.tabs[name], *ui.nav_buttons.values()]
                    if name == "端子排出图":
                        widgets += [ui.terminal_page.terminals, ui.terminal_page.wiring,
                                    ui.terminal_page.right_canvas, ui.terminal_page.build_button]
                    elif name == "问题清单":
                        widgets += [ui.issue_tree, ui.issue_detail]
                    elif name == "输出文件":
                        widgets += [ui.output_tree]
                    elif name == "工程概览":
                        widgets += [ui.file_tree]
                    for widget in widgets:
                        with self.subTest(page=name, widget=str(widget)):
                            self.assertTrue(widget.winfo_ismapped())
                            x = widget.winfo_rootx() - root.winfo_rootx()
                            y = widget.winfo_rooty() - root.winfo_rooty()
                            self.assertGreaterEqual(x, 0)
                            self.assertGreaterEqual(y, 0)
                            self.assertLessEqual(x + widget.winfo_width(), root.winfo_width())
                            self.assertLessEqual(y + widget.winfo_height(), root.winfo_height())
                ui._destroy()
                self.assertEqual([], callback_errors)
        finally:
            # Tk 的缩放设置会影响本进程后续创建的根窗口。
            if ui is not None and not ui.closed:
                with patch.object(app, "save_config"):
                    ui._destroy()
            elif ui is None:
                root.tk.call("tk", "scaling", previous_scaling)
                root.destroy()


if __name__ == "__main__":
    unittest.main()
