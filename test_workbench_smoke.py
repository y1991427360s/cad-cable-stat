"""自检会真实出图和保存状态，但不得把工程业务输入改成测试数据。"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from modules.terminal.service import TerminalService
from tools.workbench_smoke import run_terminal_smoke


class WorkbenchSmokeTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        self.state_dir = self.project / "data" / "terminal"
        self.state_dir.mkdir(parents=True)
        # 包含 BOM、CRLF 和非 UTF-8 字节，验证按原字节恢复而不是重新序列化。
        self.original = {
            "端子排.txt": "X、8、9\r\n".encode("utf-8-sig"),
            "接线.txt": "X8、N600、原柜\r\n".encode("gbk"),
            "settings.json": b'\xef\xbb\xbf{ "cabinet": "saved", "custom": 123 }\r\n',
        }
        for name, content in self.original.items():
            (self.state_dir / name).write_bytes(content)

    def assert_original_state(self):
        for name, content in self.original.items():
            self.assertEqual(content, (self.state_dir / name).read_bytes(), name)

    def test_success_restores_all_input_bytes_and_uses_distinct_dxf_names(self):
        output_dir = self.project / "outputs" / "端子排"
        output_dir.mkdir(parents=True)
        prior_output = output_dir / "验收柜-端子排.dxf"
        prior_output.write_bytes(b"existing engineering drawing")
        first = run_terminal_smoke(self.project, lambda _message: None)
        self.assert_original_state()
        first_bytes = {first[key]: Path(first[key]).read_bytes() for key in ("path", "wiringPath")}
        second = run_terminal_smoke(self.project, lambda _message: None)
        self.assert_original_state()
        self.assertTrue(first["ok"] and second["ok"])
        for key in ("path", "wiringPath"):
            self.assertNotEqual(first[key], second[key])
            self.assertEqual(first_bytes[first[key]], Path(first[key]).read_bytes())
            self.assertTrue(Path(second[key]).is_file())
        self.assertEqual(b"existing engineering drawing", prior_output.read_bytes())

    def test_generated_state_files_are_removed_if_originally_absent(self):
        for name in ("接线.txt", "settings.json"):
            (self.state_dir / name).unlink()
        report = run_terminal_smoke(self.project, lambda _message: None)
        self.assertTrue(report["ok"])
        self.assertEqual(self.original["端子排.txt"], (self.state_dir / "端子排.txt").read_bytes())
        self.assertFalse((self.state_dir / "接线.txt").exists())
        self.assertFalse((self.state_dir / "settings.json").exists())

    def test_generation_exception_restores_state_even_after_autosave(self):
        def generate_then_fail(service, payload):
            service.save_state(payload)
            self.assertNotEqual(self.original["端子排.txt"], (self.state_dir / "端子排.txt").read_bytes())
            raise RuntimeError("模拟出图中断")

        with patch.object(TerminalService, "generate", autospec=True, side_effect=generate_then_fail):
            with self.assertRaisesRegex(RuntimeError, "模拟出图中断"):
                run_terminal_smoke(self.project, lambda _message: None)
        self.assert_original_state()


if __name__ == "__main__":
    unittest.main()
