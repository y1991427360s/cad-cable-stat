"""命令行运行一个项目：python run_auto_stat.py [项目文件夹]（缺省为当前目录）。"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

import calculate_cable_lengths
from cable_stat.project import RESULT_WORKBOOK, find_workbook, has_required_headers  # noqa: F401
from cable_stat.text import normalize_header  # noqa: F401

# 与 load_workbook_rows 的必需表头一致；「自动统计」「向上取整」列可以没有，核心脚本会自动补建
REQUIRED_HEADERS = {"电缆编号", "起点", "终点", "电缆长度"}


def clean_path_arg(raw: str) -> str:
    # 兼容 cmd 里 "%~dp0" 这类尾反斜杠把引号转义进参数的情况（引号不可能出现在合法路径里）
    return raw.strip().strip('"').rstrip("\\")


def main() -> int:
    if getattr(sys, "frozen", False):
        tool_dir = Path(sys.executable).resolve().parent
    else:
        tool_dir = Path(__file__).resolve().parent
    raw = clean_path_arg(sys.argv[1]) if len(sys.argv) > 1 else ""
    project_dir = Path(raw).resolve() if raw else Path.cwd().resolve()
    if project_dir == tool_dir:
        print("这里是通用工具文件夹，不是项目文件夹。")
        print("请双击某个项目文件夹里的 一键启动.cmd 来运行统计。")
        print("新项目：双击本文件夹的 新建项目.cmd，或复制“项目模板”文件夹到任何地方再改名。")
        return 1
    try:
        workbook = find_workbook(project_dir)
        calculate_cable_lengths.main(
            [
                "--workbook",
                str(workbook),
                "--data-dir",
                str(project_dir / "data"),
                "--output",
                str(project_dir / "outputs" / RESULT_WORKBOOK),
            ]
        )
    except Exception as exc:
        traceback.print_exc()
        print(f"\n计算失败：{exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
