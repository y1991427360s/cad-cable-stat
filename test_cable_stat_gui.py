"""桌面工作流回归：真实 Tk 事件循环 + 临时工程，无需额外测试依赖。"""

from __future__ import annotations

import hashlib
import tempfile
import threading
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

import openpyxl

import cable_stat_app as app
import cable_stat_gui as gui
from cable_stat.csvio import read_csv_dicts, write_csv_dicts
from cable_stat.params import PARAM_SPECS, default_params, save_params
from cable_stat.pipeline import run
from cable_stat.project import load_alias_rows


def make_project(project: Path) -> Path:
    data = project / "data"
    data.mkdir(parents=True)
    save_params(data, default_params())
    write_csv_dicts(data / "柜子坐标.csv", [
        {"柜子名称": name, "楼层": "1F", "X": x, "Y": 0}
        for name, x in (("柜A", 0), ("柜B", 5000), ("柜C", 10000))
    ], ["柜子名称", "楼层", "X", "Y"])
    write_csv_dicts(data / "路径线段.csv", [{"路径编号": "R1", "楼层": "1F", "起点X": 0,
                                             "起点Y": 0, "终点X": 10000, "终点Y": 0}],
                    ["路径编号", "楼层", "起点X", "起点Y", "终点X", "终点Y"])
    wb = openpyxl.Workbook()
    wb.active.append(["电缆编号", "起点", "终点", "电缆长度"])
    wb.active.append(["C1", "清册柜A", "柜B", 12])
    wb.active.append(["C2", "柜B", "柜C", 12])
    wb.save(project / "自动统计.xlsx")
    wb.close()
    return project


class ParameterValidationTests(unittest.TestCase):
    def test_invalid_nonfinite_and_boundary_values(self) -> None:
        raw = {spec.name: str(spec.default) for spec in PARAM_SPECS}
        for text in ("nan", "inf", "-inf", "", "abc", "0"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                gui.validate_parameter_values(dict(raw, CAD每米单位=text))
        self.assertEqual(gui.validate_parameter_values(dict(raw, 不拐弯修正="-3.5"))["不拐弯修正"], -3.5)

    def test_progress_is_monotonic_and_result_does_not_overwrite_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = make_project(Path(tmp))
            workbook = project / "自动统计.xlsx"
            original = workbook.read_bytes()
            progress = []
            result = run(workbook, project / "data", project / "outputs" / "自动统计_计算结果.xlsx",
                         lambda value, phase: progress.append((value, phase)))
            self.assertEqual(result.total, 2)
            self.assertEqual(result.failed, 1)
            self.assertEqual([p[0] for p in progress], sorted(p[0] for p in progress))
            self.assertEqual(progress[-1][0], 100)
            self.assertTrue(all(p[1] for p in progress))
            self.assertEqual(workbook.read_bytes(), original)
            self.assertTrue(result.html_path.exists())


class DesktopWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.project = make_project(self.base / "工程A")
        self.errors: list = []
        self.callback_errors: list = []
        self.patches = [
            patch.object(app, "load_config", return_value={}),
            patch.object(app, "save_config"),
            patch.object(app, "claim_handoff_request", return_value=None),
            patch.object(gui.messagebox, "showerror", side_effect=lambda *a, **kw: self.errors.append(a)),
        ]
        for mock in self.patches:
            mock.start()
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.report_callback_exception = lambda *args: self.callback_errors.append(args)
        self.ui = gui.CableStatApp(self.root)
        self.ui.select_project(self.project)
        self.wait_idle()

    def tearDown(self) -> None:
        if not self.ui.closed:
            self.wait_idle()
            self.ui._destroy()
        for mock in reversed(self.patches):
            mock.stop()
        self.temp.cleanup()
        self.assertEqual(self.callback_errors, [])

    def wait_idle(self) -> None:
        deadline = time.monotonic() + 15
        while not self.ui.closed:
            self.root.update()
            if not self.ui.busy:
                break
            if time.monotonic() > deadline:
                self.fail("GUI 工作线程超时")
            time.sleep(0.01)

    def test_unmatched_alias_save_recalculate_and_filter(self) -> None:
        workbook = self.project / "自动统计.xlsx"
        original = hashlib.sha256(workbook.read_bytes()).hexdigest()
        self.ui.start_calculation()
        self.wait_idle()
        self.assertEqual(self.ui.result.failed, 1)
        self.assertEqual(str(self.ui.run_button["state"]), "normal")
        missing_id = next(item for item in self.ui.alias_tree.get_children()
                          if self.ui.alias_tree.item(item, "values")[0] == "清册柜A")
        self.ui.alias_tree.selection_set(missing_id)
        self.ui._alias_selected()
        self.ui.alias_target.set("柜A")
        self.ui.stage_alias()
        self.assertEqual(self.ui.pending_aliases, {"清册柜A": "柜A"})
        with patch.object(gui.messagebox, "askyesnocancel", return_value=True):
            self.ui.start_calculation()
        self.wait_idle()
        self.assertEqual(self.ui.result.ok, 2)
        self.assertEqual(self.ui.result.failed, 0)
        self.assertEqual(load_alias_rows(self.project / "data")[0]["CAD名称"], "柜A")
        self.ui.only_missing.set(False)
        self.ui._render_cabinets()
        values = [self.ui.alias_tree.item(item, "values") for item in self.ui.alias_tree.get_children()]
        self.assertEqual(next(row[3] for row in values if row[0] == "清册柜A"), "柜A")
        self.ui.issues = [("错误", "错误项"), ("警告", "警告项")]
        self.ui.issue_level.set("错误")
        self.ui._render_issues()
        self.assertEqual(len(self.ui.issue_tree.get_children()), 1)
        self.assertEqual(hashlib.sha256(workbook.read_bytes()).hexdigest(), original)
        self.assertEqual(self.errors, [])

    def test_parameter_save_preserves_notes_and_unknown_rows(self) -> None:
        path = self.project / "data" / "参数.csv"
        rows = read_csv_dicts(path)
        rows[0]["备注"] = "毫米图，设计确认"
        rows.append({"参数": "用户补充", "值": "42", "备注": "保留"})
        write_csv_dicts(path, rows, ["参数", "值", "备注"])
        self.ui.param_vars["竖井高度"].set("5.8")
        self.assertTrue(self.ui.parameters_dirty())
        self.assertTrue(self.ui.save_parameters())
        self.assertFalse(self.ui.parameters_dirty())
        saved = {r["参数"]: r for r in read_csv_dicts(path)}
        self.assertEqual(saved["CAD每米单位"]["备注"], "毫米图，设计确认")
        self.assertEqual(saved["竖井高度"]["值"], "5.8")
        self.assertEqual(saved["用户补充"]["值"], "42")
        self.assertTrue(path.read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_switch_with_unsaved_edits_can_cancel_or_save(self) -> None:
        other = make_project(self.base / "工程B")
        self.ui.param_vars["竖井高度"].set("6")
        with patch.object(gui.messagebox, "askyesnocancel", return_value=None):
            self.ui.select_project(other)
        self.assertEqual(self.ui.project, self.project)
        with patch.object(gui.messagebox, "askyesnocancel", return_value=True):
            self.ui.select_project(other)
        self.wait_idle()
        self.assertEqual(self.ui.project, other)
        self.assertEqual(self.ui.param_vars["竖井高度"].get(), "4.5")
        original = {r["参数"]: r["值"] for r in read_csv_dicts(self.project / "data" / "参数.csv")}
        self.assertEqual(original["竖井高度"], "6")

    def test_failed_scan_cannot_write_previous_project_parameters(self) -> None:
        other = make_project(self.base / "损坏工程")
        original = (other / "data" / "参数.csv").read_bytes()
        self.ui.param_vars["竖井高度"].set("9")
        self.ui.save_parameters()
        with patch.object(gui, "inspect_project", side_effect=UnicodeError("无法读取 CSV")):
            self.ui.select_project(other)
            self.wait_idle()
        self.assertIsNone(self.ui.snapshot)
        self.assertEqual(self.ui.param_vars["竖井高度"].get(), "")
        self.assertFalse(self.ui.save_parameters())
        self.assertEqual((other / "data" / "参数.csv").read_bytes(), original)
        self.ui.refresh()
        self.wait_idle()
        self.assertIsNotNone(self.ui.snapshot)

    def test_empty_worker_exception_recovers_and_accepts_next_run(self) -> None:
        with patch.object(app, "calculate_project", side_effect=AssertionError()):
            self.ui.start_calculation()
            self.wait_idle()
        self.assertIn("AssertionError", self.ui.state_text.get())
        self.assertTrue(self.errors)
        self.ui.start_calculation()
        self.wait_idle()
        self.assertEqual(self.ui.result.total, 2)

    def test_busy_project_switch_and_deferred_close(self) -> None:
        release = threading.Event()
        work_entered = threading.Event()
        snapshot = self.ui.snapshot
        def slow_work():
            work_entered.set()
            release.wait(3)
            return snapshot
        self.ui._launch("测试后台任务", slow_work, "scanned", False)
        self.assertTrue(work_entered.wait(1))
        self.ui.select_project(self.base)
        self.assertEqual(self.ui.project, self.project)
        self.assertEqual(str(self.ui.run_button["state"]), "disabled")
        with patch.object(gui.messagebox, "askyesno", return_value=True):
            self.ui.on_close()
        self.assertFalse(self.ui.closed)
        release.set()
        self.wait_idle()
        self.assertTrue(self.ui.closed)


if __name__ == "__main__":
    unittest.main()
