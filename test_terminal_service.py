"""工程持久化和服务边界测试，不接触用户工程、桌面或 APPDATA。"""

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import ezdxf

from core.models import UnifiedIssue
from modules.terminal import duanzi_dxf_tool as drawing
from modules.terminal.service import STATE_VERSION, TerminalService


class TerminalServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name) / "老工程"
        self.project.mkdir()
        self.service = TerminalService(self.project)

    def payload(self, **changes):
        data = {"terminals": "X、1、2、3", "wiring": "X1、A610、目标柜", "cabinet": "保护柜",
                "direction": "向下", "directionMemory": {"X": "向上"}, "autoOpen": False}
        data.update(changes)
        return data

    def test_old_project_creates_terminal_directory_without_requiring_migration(self):
        self.assertTrue((self.project / "data" / "terminal").is_dir())
        state = self.service.load_state()
        self.assertEqual("", state["terminals"])
        self.assertEqual("", state["wiring"])
        self.assertEqual("向下", state["direction"])
        self.assertEqual([], self.service.load_warnings)
        self.assertFalse(self.service.output_dir.exists())

    def test_round_trip_preserves_raw_input_and_engineering_settings(self):
        state = self.payload(terminals="X、1、2\r\nX2、1、2\n", direction="UP",
                             directionMemory={"X": "DOWN", "X2": "向上"}, autoOpen=True)
        self.service.save_state(state)
        loaded = TerminalService(self.project).load_state()
        self.assertEqual(state["terminals"], loaded["terminals"])
        self.assertEqual(state["wiring"], loaded["wiring"])
        self.assertEqual("保护柜", loaded["cabinet"])
        self.assertEqual("向上", loaded["direction"])
        self.assertEqual({"X": "向下", "X2": "向上"}, loaded["directionMemory"])
        self.assertTrue(loaded["autoOpen"])
        settings = json.loads((self.service.data_dir / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(STATE_VERSION, settings["version"])
        self.assertNotIn("terminals", settings)
        self.assertNotIn("wiring", settings)
        self.assertNotIn("outputDir", settings)

    def test_projects_do_not_share_inputs_or_directions(self):
        self.service.save_state(self.payload())
        second = TerminalService(Path(self.temporary.name) / "新工程")
        self.assertEqual("", second.load_state()["terminals"])
        self.assertEqual({}, second.load_state()["directionMemory"])
        second.save_state(self.payload(terminals="Y、8、9", cabinet="新柜", directionMemory={"Y": "向下"}))
        self.assertEqual("X、1、2、3", TerminalService(self.project).load_state()["terminals"])
        self.assertEqual("保护柜", self.service.load_state()["cabinet"])
        self.assertEqual("新柜", second.load_state()["cabinet"])

    def test_corrupt_settings_are_reported_and_do_not_hide_raw_inputs(self):
        self.service.save_state(self.payload())
        settings = self.service.data_dir / "settings.json"
        for invalid in ("{坏的", "[]", '{"cabinet": 5, "direction": null, "directionMemory": {"X": []}, "autoOpen": 1}'):
            with self.subTest(settings=invalid):
                settings.write_text(invalid, encoding="utf-8-sig")
                loaded = self.service.load_state()
                self.assertEqual("X、1、2、3", loaded["terminals"])
                self.assertEqual("X1、A610、目标柜", loaded["wiring"])
                self.assertEqual("向下", loaded["direction"])
                self.assertTrue(self.service.load_warnings)

    def test_valid_settings_with_utf8_bom_load_normally(self):
        (self.service.data_dir / "settings.json").write_text('{"cabinet": "甲柜", "direction": "UP"}', encoding="utf-8-sig")
        self.assertEqual("甲柜", self.service.load_state()["cabinet"])
        self.assertEqual("向上", self.service.load_state()["direction"])
        self.assertEqual([], self.service.load_warnings)

    def test_unreadable_input_is_preserved_before_auto_save_uses_defaults(self):
        path = self.service.data_dir / "端子排.txt"
        unreadable = b"\xff\xfe\x80"
        path.write_bytes(unreadable)
        state = self.service.load_state()
        self.assertTrue(self.service.load_warnings)
        self.service.save_state(state)
        backups = list(self.service.data_dir.glob("端子排.txt.unreadable-*.bak"))
        self.assertEqual(1, len(backups))
        self.assertEqual(unreadable, backups[0].read_bytes())

    def test_failed_atomic_replace_preserves_previous_inputs_and_cleans_temporary_files(self):
        self.service.save_state(self.payload())
        with patch("modules.terminal.service.os.replace", side_effect=PermissionError("文件占用")):
            with self.assertRaisesRegex(OSError, "无法保存本工程"):
                self.service.save_state(self.payload(terminals="Y、7、8"))
        self.assertEqual("X、1、2、3", self.service.load_state()["terminals"])
        self.assertEqual([], list(self.service.data_dir.glob("*.tmp")))

    def test_later_replace_failure_restores_all_previous_input_bytes(self):
        self.service.save_state(self.payload())
        before = {path: path.read_bytes() for path in self.service.data_dir.iterdir()}
        original_replace = os.replace
        for locked_name in ("接线.txt", "settings.json"):
            with self.subTest(locked_name=locked_name):
                def locked_input(source, target):
                    if Path(target).name == locked_name:
                        raise PermissionError("文件占用")
                    return original_replace(source, target)

                with patch("modules.terminal.service.os.replace", side_effect=locked_input):
                    with self.assertRaisesRegex(OSError, "无法保存本工程"):
                        self.service.save_state(self.payload(terminals="Y、7、8", wiring="Y7、B、新柜", cabinet="新柜"))
                self.assertEqual(before, {path: path.read_bytes() for path in self.service.data_dir.iterdir()})

    def test_failed_first_save_removes_partially_created_inputs(self):
        original_replace = os.replace

        def locked_settings(source, target):
            if Path(target).name == "settings.json":
                raise PermissionError("文件占用")
            return original_replace(source, target)

        with patch("modules.terminal.service.os.replace", side_effect=locked_settings):
            with self.assertRaisesRegex(OSError, "无法保存本工程"):
                self.service.save_state(self.payload())
        self.assertEqual([], list(self.service.data_dir.iterdir()))

    def test_failed_rollback_keeps_original_backup_and_reports_its_path(self):
        self.service.save_state(self.payload())
        original_bytes = (self.service.data_dir / "端子排.txt").read_bytes()
        original_replace = os.replace

        def locked_input_and_restore(source, target):
            if Path(target).name == "接线.txt" or str(source).endswith(".bak"):
                raise PermissionError("文件占用")
            return original_replace(source, target)

        with patch("modules.terminal.service.os.replace", side_effect=locked_input_and_restore):
            with self.assertRaisesRegex(OSError, "原件备份") as caught:
                self.service.save_state(self.payload(terminals="Y、7、8"))
        backups = list(self.service.data_dir.glob("*.save-backup-*.bak"))
        self.assertEqual(1, len(backups))
        self.assertEqual(original_bytes, backups[0].read_bytes())
        self.assertIn(str(backups[0]), str(caught.exception))
        self.assertEqual([], list(self.service.data_dir.glob("*.tmp")))

    def test_inspect_reports_ordinary_coordinate_and_invalid_input(self):
        ordinary = TerminalService.inspect(self.payload())
        self.assertTrue(ordinary["ok"], ordinary)
        self.assertEqual({"blocks": 1, "terminals": 3, "cables": 1, "points": 1}, ordinary["stats"])
        self.assertEqual("X", ordinary["strips"][0]["ident"])
        coordinate = TerminalService.inspect(self.payload(terminals="100,100 X 端子名\n100,95 1"))
        self.assertTrue(coordinate["ok"], coordinate)
        invalid = TerminalService.inspect(self.payload(wiring="X99、A、目标柜"))
        self.assertFalse(invalid["ok"])
        self.assertTrue(invalid["errors"])
        self.assertEqual("端子", self.service.issues(invalid)[0].source)

    def test_warnings_convert_to_unified_issues(self):
        checked = TerminalService.inspect(self.payload(terminals="X、21\nX2、1", wiring="X21、A、目标柜"))
        self.assertTrue(checked["ok"])
        self.assertTrue(checked["warnings"])
        self.assertIsInstance(self.service.issues(checked)[0], UnifiedIssue)
        self.assertEqual("警告", self.service.issues(checked)[0].level)

    def test_direction_memory_is_by_name_after_inserting_new_coordinate_column(self):
        original_generate = drawing.generate
        seen = []

        def capture(payload):
            seen.append(payload)
            return original_generate(payload)

        terminals = "5,100 Y 端子名\n5,95 1\n10,100 X 端子名\n10,95 1"
        with patch("modules.terminal.service.drawing.generate", side_effect=capture):
            result = self.service.generate(self.payload(terminals=terminals))
        self.assertTrue(result["ok"], result)
        self.assertEqual({"坐标列-1": "DOWN", "坐标列-2": "UP"}, seen[0]["directions"])

    def test_generate_two_valid_drawings_only_inside_current_project(self):
        outside = Path(self.temporary.name) / "不允许的输出"
        result = self.service.generate(self.payload(outputDir=str(outside)))
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.service.output_dir / "保护柜-端子排.dxf", Path(result["path"]))
        self.assertEqual(self.service.output_dir / "保护柜-端子排-仅接线.dxf", Path(result["wiringPath"]))
        self.assertFalse(outside.exists())
        for path in (result["path"], result["wiringPath"]):
            self.assertGreater(Path(path).stat().st_size, 0)
            doc = ezdxf.readfile(path)
            self.assertFalse(doc.audit().has_errors)
            self.assertGreater(len(doc.modelspace().query("LINE")), 0)
            self.assertGreater(len(doc.modelspace().query("TEXT")), 0)
        self.assertEqual("X、1、2、3", self.service.load_state()["terminals"])
        self.assertEqual({"X": "向上"}, self.service.load_state()["directionMemory"])

    def test_empty_cabinet_has_reasonable_default_filename(self):
        result = self.service.generate(self.payload(cabinet=""))
        self.assertTrue(result["ok"], result)
        self.assertEqual("端子排.dxf", Path(result["path"]).name)

    def test_unsafe_cabinet_cannot_escape_project_outputs(self):
        result = self.service.generate(self.payload(cabinet="../../上级\\柜"))
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.service.output_dir, Path(result["path"]).parent)

    def test_locked_cad_file_keeps_timestamp_fallback(self):
        original = drawing.build_dxf
        calls = []

        def lock_once(params, rules, target, wiring_target=None):
            calls.append(target)
            if len(calls) == 1:
                raise PermissionError("CAD 正在使用")
            return original(params, rules, target, wiring_target)

        with patch("modules.terminal.service.drawing.build_dxf", side_effect=lock_once):
            result = self.service.generate(self.payload())
        self.assertTrue(result["ok"], result)
        self.assertNotEqual(calls[0], calls[1])
        self.assertEqual(self.service.output_dir, Path(result["path"]).parent)
        self.assertRegex(Path(result["path"]).name, r"保护柜-端子排-\d{8}-\d{6}\.dxf")

    def test_bad_input_cannot_create_output_files(self):
        result = self.service.generate(self.payload(terminals="123、1"))
        self.assertFalse(result["ok"])
        self.assertTrue(result["errors"])
        self.assertFalse(self.service.output_dir.exists())

    def test_drawing_failure_is_readable_and_does_not_save_failed_work(self):
        with patch("modules.terminal.service.drawing.generate", side_effect=RuntimeError("绘图失败")):
            result = self.service.generate(self.payload())
        self.assertFalse(result["ok"])
        self.assertIn("端子出图失败", result["errors"][0])
        self.assertEqual("", self.service.load_state()["terminals"])

    def test_save_failure_after_generation_does_not_hide_completed_files(self):
        with patch.object(self.service, "save_state", side_effect=OSError("权限不足")):
            result = self.service.generate(self.payload())
        self.assertTrue(result["ok"], result)
        self.assertTrue(Path(result["path"]).exists())
        self.assertTrue(any("DXF 已生成" in warning for warning in result["warnings"]))


if __name__ == "__main__":
    unittest.main()
