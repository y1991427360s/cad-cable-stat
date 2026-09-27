"""端子页面回归：单根窗口、工程隔离、快捷键和后台生成。"""
from __future__ import annotations

import json
import threading
import time
import tkinter as tk
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from modules.terminal import page as gui


class ReadTextFileTests(unittest.TestCase):
    def test_common_encodings(self):
        with TemporaryDirectory() as folder:
            for encoding in ("utf-8", "utf-8-sig", "gbk", "utf-16", "utf-16-be"):
                data = "ZD、1、2".encode(encoding)
                if encoding == "utf-16-be":
                    data = b"\xfe\xff" + data
                path = Path(folder) / "输入.txt"
                path.write_bytes(data)
                self.assertEqual("ZD、1、2", gui.read_text_file(path), encoding)


class TerminalPageTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.project = Path(self.folder.name) / "工程甲"
        self.project.mkdir()
        self.other = Path(self.folder.name) / "工程乙"
        self.other.mkdir()
        self.root = tk.Tk()
        self.root.withdraw()
        self.pages = []
        self.issues = []
        self.busy_events = []
        for name, kwargs in (("askyesno", {"return_value": True}), ("showerror", {}), ("showwarning", {})):
            replacement = patch.object(gui.messagebox, name, **kwargs)
            replacement.start()
            self.addCleanup(replacement.stop)

    def tearDown(self):
        for page in self.pages:
            self.wait(page)
            page.dispose()
        self.root.destroy()
        self.folder.cleanup()

    def open(self, project=True):
        page = gui.TerminalPage(self.root, on_issues=self.issues.append, on_busy=self.busy_events.append)
        page.pack(fill="both", expand=True)
        self.pages.append(page)
        if project:
            self.assertTrue(page.set_project(self.project))
        page.activate(True)
        self.root.update()
        return page

    def wait(self, page):
        deadline = time.monotonic() + 30
        while page.busy and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.assertFalse(page.busy, "后台生成未能恢复空闲")

    def test_embedded_frame_never_creates_another_root(self):
        with patch.object(gui.tk, "Tk", side_effect=AssertionError("不允许创建第二个根")):
            page = self.open()
        self.assertIs(page.winfo_toplevel(), self.root)
        self.assertEqual("TFrame", page.winfo_class())
        self.assertTrue(page.on_close())
        self.assertTrue(self.root.winfo_exists())

    def test_no_project_disables_editor_and_generation(self):
        page = self.open(project=False)
        self.assertEqual("disabled", str(page.terminals["state"]))
        self.assertEqual("disabled", str(page.build_button["state"]))
        with patch.object(gui.TerminalService, "generate") as generate:
            page.build()
        generate.assert_not_called()
        self.assertFalse((self.project / "data").exists())

    def test_broken_settings_do_not_block_open(self):
        state = self.project / "data" / "terminal" / "settings.json"
        state.parent.mkdir(parents=True)
        for content in ("{坏的", '["不是字典"]', '{"cabinet": 5, "directionMemory": {"a": 1}, "direction": null}'):
            state.write_text(content, encoding="utf-8")
            page = self.open()
            self.assertEqual("", page.get_text(page.terminals))
            self.assertIn("读取提醒", page.get_text(page.result))
            page.dispose()
            page.destroy()
            self.pages.remove(page)

    def test_bom_state_restores_input(self):
        folder = self.project / "data" / "terminal"
        folder.mkdir(parents=True)
        (folder / "settings.json").write_text('{"cabinet": "甲柜"}', encoding="utf-8-sig")
        (folder / "端子排.txt").write_text("ZD、1、2", encoding="utf-8-sig")
        page = self.open()
        self.assertEqual("ZD、1、2", page.get_text(page.terminals))
        self.assertEqual("甲柜", page.cabinet.get())
        self.assertTrue(page.get_text(page.result).startswith("已恢复"))

    def test_switch_saves_restores_and_keeps_directions_by_name(self):
        page = self.open()
        page.terminals.insert("1.0", "10,100 1QD 端子名\n10,95 1\n60,100 2QD 端子名\n60,95 1")
        page.wiring.insert("1.0", "1QD1、a、柜")
        page.cabinet.insert(0, "测试柜")
        page.refresh_directions()
        second = page.strip_table.get_children()[1]
        page._apply_directions([second], "向上")
        self.assertTrue(page.set_project(self.other))
        self.assertEqual("", page.get_text(page.terminals))
        self.assertEqual({}, page.direction_memory)
        page.terminals.insert("1.0", "XD、8、9")
        self.assertTrue(page.set_project(self.project))
        self.assertEqual("测试柜", page.cabinet.get())
        self.assertEqual(["向下", "向上"], list(page.strip_dirs.values()))
        page.terminals.insert("1.0", "5,100 0QD 端子名\n5,95 1\n")
        page.refresh_directions()
        names = {page.strip_table.item(key)["values"][0]: value for key, value in page.strip_dirs.items()}
        self.assertEqual({"0QD": "向下", "1QD": "向下", "2QD": "向上"}, names)
        self.assertEqual("XD、8、9", (self.other / "data" / "terminal" / "端子排.txt").read_text(encoding="utf-8"))

    def test_failed_save_keeps_old_project_and_input(self):
        page = self.open()
        page.terminals.insert("1.0", "ZD、1、2")
        with patch.object(page.service, "save_state", side_effect=OSError("磁盘只读")):
            self.assertFalse(page.set_project(self.other))
            self.assertFalse(page.on_close())
        self.assertEqual(self.project.resolve(), page.project)
        self.assertEqual("ZD、1、2", page.get_text(page.terminals))
        gui.messagebox.showerror.assert_called()
        self.assertTrue(self.root.winfo_exists())

    def test_default_direction_applies_to_every_strip(self):
        page = self.open()
        page.terminals.insert("1.0", "10,100 1QD 端子名\n10,95 1\n60,100 2QD 端子名\n60,95 1")
        page.refresh_directions()
        page.direction.set("向上")
        page._on_default_direction()
        self.assertEqual({"向上"}, set(page.strip_dirs.values()))

    def test_shortcuts_only_run_on_active_page_and_stop_return(self):
        page = self.open()
        for box in (page.terminals, page.wiring):
            self.assertTrue(box.bind("<Control-Return>"))
        with patch.object(page, "build") as build, patch.object(page, "inspect") as inspect:
            self.assertEqual("break", page._shortcut_build())
            self.assertEqual("break", page._shortcut_inspect())
            build.assert_called_once()
            inspect.assert_called_once()
            page.activate(False)
            self.assertEqual("break", page._shortcut_build())
            self.assertIsNone(page._shortcut_inspect())
            self.assertEqual(1, build.call_count)
            self.assertEqual(1, inspect.call_count)

    def test_page_does_not_change_main_window_shortcuts(self):
        original = self.root.bind("<F5>", lambda _event: None, add="+")
        original_build = self.root.bind("<Control-Return>", lambda _event: None, add="+")
        bindings = [self.root.bind(key) for key in ("<F5>", "<Control-Return>")]
        page = self.open()
        self.assertIn(original, self.root.bind("<F5>"))
        self.assertIn(original_build, self.root.bind("<Control-Return>"))
        self.assertEqual(bindings, [self.root.bind(key) for key in ("<F5>", "<Control-Return>")])
        page.dispose()
        self.assertEqual(bindings, [self.root.bind(key) for key in ("<F5>", "<Control-Return>")])

    def test_text_ctrl_enter_stops_class_newline_and_root_dispatch(self):
        page = self.open()
        root_calls = []
        self.root.bind("<Control-Return>", lambda _event: root_calls.append(True))
        self.root.deiconify()
        self.root.update()
        for box in (page.terminals, page.wiring):
            box.insert("1.0", "ZD、1、2")
            box.mark_set("insert", "1.3")
            box.focus_force()
            self.root.update()
            with patch.object(page, "build") as build:
                box.event_generate("<Control-Return>")
                self.root.update()
                build.assert_called_once()
            self.assertEqual("ZD、1、2", page.get_text(box))
        self.assertEqual([], root_calls)

    def test_right_column_wheel_scrolls_during_busy_without_global_binding(self):
        page = self.open()
        self.root.geometry("900x420")
        self.root.deiconify()
        self.root.update()
        self.assertTrue(page.build_button.bind("<MouseWheel>"))
        self.assertFalse(self.root.bind("<MouseWheel>"))
        self.assertFalse(page.terminals.bind("<MouseWheel>"))
        self.assertEqual("Primary.TButton", str(page.build_button["style"]))
        page.set_external_busy(True)
        before = page.right_canvas.yview()[0]
        event = SimpleNamespace(delta=-120, widget=page.build_button)
        self.assertEqual("break", page._scroll_right(event))
        self.assertGreater(page.right_canvas.yview()[0], before)
        # 日志内容长时优先使用 Text 自己的滚动，避免鼠标一动两层同时滚。
        page.write("\n".join(str(index) for index in range(100)))
        self.root.update()
        page.result.yview_moveto(0.5)
        self.assertIsNone(page._scroll_right(SimpleNamespace(delta=-120, widget=page.result)))
        page.set_external_busy(False)

    def test_inspect_publishes_terminal_issues(self):
        page = self.open()
        page.terminals.insert("1.0", "ZD、1、2")
        page.wiring.insert("1.0", "ZD99、A、远方柜")
        result = page.inspect()
        self.assertFalse(result["ok"])
        self.assertTrue(self.issues[-1])
        self.assertTrue(all(issue.source == "端子" for issue in self.issues[-1]))
        self.assertIn("输入问题", page.get_text(page.result))

    def test_build_writes_engineering_files_and_recovers_failure(self):
        page = self.open()
        page.demo()
        page.build()
        self.wait(page)
        self.assertTrue(page.get_text(page.result).startswith("生成成功"))
        output = Path(page.last_output)
        self.assertEqual(self.project / "outputs" / "端子排", output.parent)
        self.assertTrue(output.is_file())
        self.assertTrue(Path(page.last_wiring_output).is_file())
        self.assertEqual("normal", str(page.open_button["state"]))
        self.assertEqual([True, False], self.busy_events)
        settings = json.loads((self.project / "data" / "terminal" / "settings.json").read_text(encoding="utf-8"))
        self.assertIn("directionMemory", settings)
        with patch.object(page.service, "generate", side_effect=RuntimeError("测试异常")):
            page.build()
            self.wait(page)
        self.assertIn("测试异常", page.get_text(page.result))
        self.assertIn("Traceback (most recent call last)", page.get_text(page.result))
        self.assertEqual("normal", str(page.build_button["state"]))
        self.assertEqual("disabled", str(page.open_button["state"]))

    def test_busy_blocks_editing_duplicate_generation_switch_and_close(self):
        page = self.open()
        page.demo()
        finish = threading.Event()
        threads = []

        def generate(_payload):
            threads.append(threading.current_thread().ident)
            finish.wait(5)
            return {"ok": False, "errors": ["已完成测试"], "warnings": []}

        with patch.object(page.service, "generate", side_effect=generate) as build:
            page.build()
            try:
                self.assertTrue(page.busy)
                self.assertEqual("disabled", str(page.terminals["state"]))
                self.assertEqual("disabled", str(page.wiring["state"]))
                self.assertEqual("disabled", str(page.cabinet["state"]))
                self.assertFalse(page.set_project(self.other))
                self.assertFalse(page.on_close())
                page.build()
            finally:
                finish.set()
                self.wait(page)
            self.assertEqual(1, build.call_count)
        self.assertNotEqual(threading.main_thread().ident, threads[0])
        self.assertEqual("normal", str(page.terminals["state"]))

    def test_thread_start_failure_restores_ui_and_reports_trace(self):
        page = self.open()
        page.demo()
        with patch.object(gui.threading.Thread, "start", side_effect=RuntimeError("线程启动失败")):
            page.build()
        self.assertFalse(page.busy)
        self.assertEqual([True, False], self.busy_events)
        self.assertIn("无法开始生成：线程启动失败", page.get_text(page.result))
        self.assertIn("Traceback (most recent call last)", page.get_text(page.result))
        self.assertEqual("normal", str(page.build_button["state"]))

    def test_destroy_cancels_poll_and_live_callbacks(self):
        page = self.open()
        page.demo()
        finish = threading.Event()
        completed = threading.Event()

        def generate(_payload):
            finish.wait(5)
            completed.set()
            return {"ok": False, "errors": ["已结束"]}

        with patch.object(page.service, "generate", side_effect=generate):
            page.build()
            self.assertFalse(page.dispose())
            self.assertIsNotNone(page._poll_job)
            page.destroy()
            self.assertTrue(page._disposed)
            self.assertIsNone(page._poll_job)
            self.assertIsNone(page._live_job)
            finish.set()
            self.assertTrue(completed.wait(5))
            self.root.update()
        self.assertTrue(page.dispose())
        self.assertFalse(page.set_project(self.other))
        self.assertFalse(page.save_state())

    def test_other_module_busy_prevents_actions(self):
        page = self.open()
        page.demo()
        original = page.get_text(page.terminals)
        page.set_external_busy(True)
        with patch.object(page.service, "generate") as generate, patch.object(gui.filedialog, "askopenfilename") as choose:
            page.build()
            page.clear()
            page.import_file(page.terminals)
            self.assertIsNone(page.inspect())
            self.assertFalse(page.set_project(self.other))
        generate.assert_not_called()
        choose.assert_not_called()
        with patch.object(page, "build") as build, patch.object(page, "inspect") as inspect:
            self.assertEqual("break", page._shortcut_build())
            self.assertEqual("break", page._shortcut_inspect())
        build.assert_not_called()
        inspect.assert_not_called()
        self.assertIsNone(page._live_job)
        self.assertEqual(original, page.get_text(page.terminals))
        self.assertEqual("disabled", str(page.build_button["state"]))
        page.set_external_busy(False)
        self.assertEqual("normal", str(page.build_button["state"]))

    def test_import_normalizes_newlines_and_rejects_oversized_file(self):
        page = self.open()
        path = self.project / "CAD导出.txt"
        path.write_bytes("ZD、1、2\r\nXD、3、4\r".encode("gbk"))
        with patch.object(gui.filedialog, "askopenfilename", return_value=str(path)):
            page.import_file(page.terminals)
            self.assertEqual("ZD、1、2\nXD、3、4\n", page.get_text(page.terminals))
            path.write_bytes(b"x" * (gui.MAX_IMPORT_BYTES + 1))
            page.import_file(page.terminals)
        self.assertEqual("ZD、1、2\nXD、3、4\n", page.get_text(page.terminals))
        self.assertIn("5 MB", gui.messagebox.showerror.call_args.args[1])

    def test_demo_and_clear_allow_cancelling_replacement(self):
        page = self.open()
        page.terminals.insert("1.0", "ZD、1")
        with patch.object(gui.messagebox, "askyesno", return_value=False):
            page.demo()
            page.clear()
        self.assertEqual("ZD、1", page.get_text(page.terminals))


if __name__ == "__main__":
    unittest.main()
