"""项目文件夹：找清册、盘点 data\\ 与 outputs\\ 状态、写柜名别名表。界面和命令行共用。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import openpyxl

from .csvio import read_csv_dicts, write_csv_dicts
from .loaders import HEADER_SCAN_ROWS, REQUIRED_HEADERS, find_header_row
from .text import normalize_cabinet_name, normalize_text

RESULT_WORKBOOK = "自动统计_计算结果.xlsx"
RESULT_HTML = "路径可视化.html"
ALIAS_FILE = "柜名别名.csv"
ALIAS_HEADERS = ["清册名称", "CAD名称", "备注"]

# (文件名, 说明, 是否必需)；计数按 CSV 数据行统计
DATA_FILES = (
    ("柜子坐标.csv", "柜子", True),
    ("路径线段.csv", "路径线段", True),
    ("竖井.csv", "竖井", False),
    ("房间范围.csv", "房间顶点", False),
    ("柜名别名.csv", "别名", False),
    ("强制规则.csv", "强制规则", False),
    ("参数.csv", "参数", False),
)


def has_required_headers(path: Path) -> bool:
    if path.name.startswith("~$"):
        return False
    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=False)
        try:
            ws = wb.active
            rows = [tuple(row) for row in ws.iter_rows(min_row=1, max_row=HEADER_SCAN_ROWS, values_only=True)]
        finally:
            wb.close()
        return find_header_row(rows) is not None
    except Exception:
        return False


def find_workbook(project_dir: Path) -> Path:
    candidates = [path for path in sorted(project_dir.glob("*.xlsx")) if has_required_headers(path)]
    if not candidates:
        raise FileNotFoundError(
            f"没有找到表头包含“{'、'.join(REQUIRED_HEADERS)}”的 xlsx 文件（表头可以不在第一行，空格/换行会自动忽略，“自动统计”列可以没有）。\n"
            f"请把原清册（建议命名为“自动统计.xlsx”）放到项目文件夹：{project_dir}"
        )
    exact = [path for path in candidates if path.stem == "自动统计"]
    if exact:
        return exact[0]
    if len(candidates) == 1:
        return candidates[0]
    names = "\n".join(str(path) for path in candidates)
    raise RuntimeError(f"找到多个候选工作簿，请只保留一个自动统计表或改名为“自动统计.xlsx”：\n{names}")


@dataclass
class FileStatus:
    name: str
    label: str
    required: bool
    exists: bool
    rows: int = 0
    mtime: float = 0.0

    @property
    def mtime_text(self) -> str:
        return datetime.fromtimestamp(self.mtime).strftime("%m-%d %H:%M") if self.mtime else ""


@dataclass
class ProjectStatus:
    project_dir: Path
    workbook: Path | None
    workbook_error: str
    files: list[FileStatus] = field(default_factory=list)
    result_mtime: float = 0.0
    input_mtime: float = 0.0

    @property
    def has_result(self) -> bool:
        return self.result_mtime > 0

    @property
    def result_outdated(self) -> bool:
        """清册或 data 里的文件比上次结果新：CAD 重新导出过或改过参数，需要重算。"""
        return self.has_result and self.input_mtime > self.result_mtime + 1

    @property
    def missing_required(self) -> list[str]:
        return [f.name for f in self.files if f.required and (not f.exists or f.rows == 0)]


def count_csv_rows(path: Path) -> int:
    try:
        return len(read_csv_dicts(path))
    except Exception:
        return 0


def scan_project(project_dir: Path) -> ProjectStatus:
    try:
        workbook: Path | None = find_workbook(project_dir)
        error = ""
    except Exception as exc:
        workbook = None
        error = str(exc).splitlines()[0]
    data_dir = project_dir / "data"
    files: list[FileStatus] = []
    input_mtime = workbook.stat().st_mtime if workbook else 0.0
    for name, label, required in DATA_FILES:
        path = data_dir / name
        if path.is_file():
            stat = path.stat()
            files.append(FileStatus(name, label, required, True, count_csv_rows(path), stat.st_mtime))
            input_mtime = max(input_mtime, stat.st_mtime)
        else:
            files.append(FileStatus(name, label, required, False))
    result = project_dir / "outputs" / RESULT_WORKBOOK
    result_mtime = result.stat().st_mtime if result.is_file() else 0.0
    return ProjectStatus(project_dir, workbook, error, files, result_mtime, input_mtime)


def input_signature(project_dir: Path) -> tuple[tuple[str, float, int], ...]:
    """清册与 data\\ 下 CSV 的（名称, 修改时间, 大小），用来发现 CAD 刚导出了新数据。"""
    items: list[tuple[str, float, int]] = []
    paths = list(project_dir.glob("*.xlsx")) + list((project_dir / "data").glob("*.csv"))
    for path in paths:
        if path.name.startswith("~$"):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        items.append((path.name, stat.st_mtime, stat.st_size))
    return tuple(sorted(items))


def load_alias_rows(data_dir: Path) -> list[dict[str, str]]:
    return [
        {h: normalize_text(row.get(h)) for h in ALIAS_HEADERS}
        for row in read_csv_dicts(data_dir / ALIAS_FILE)
        if normalize_text(row.get("清册名称")) or normalize_text(row.get("CAD名称"))
    ]


def save_aliases(data_dir: Path, mapping: dict[str, str], note: str = "界面匹配") -> int:
    """把 清册名称 -> CAD名称 写进 柜名别名.csv：同名旧行被替换，其余行原样保留。返回新写入条数。"""
    rows = load_alias_rows(data_dir)
    keys = {normalize_cabinet_name(source) for source in mapping}
    kept = [row for row in rows if normalize_cabinet_name(row["清册名称"]) not in keys]
    added = [{"清册名称": source, "CAD名称": target, "备注": note} for source, target in mapping.items() if source and target]
    write_csv_dicts(data_dir / ALIAS_FILE, kept + added, ALIAS_HEADERS)
    return len(added)
