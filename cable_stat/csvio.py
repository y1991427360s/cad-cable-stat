"""CSV 读写：读取兼容 utf-8-sig / GBK / UTF-16，写出一律 utf-8-sig（Excel 双击打开不乱码）。"""

from __future__ import annotations

import csv
import os
from pathlib import Path
from typing import Any


def read_csv_dicts(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "gbk", "utf-16"):
        try:
            with path.open("r", encoding=encoding, newline="") as f:
                rows = list(csv.DictReader(f))
        except UnicodeError as exc:
            last_error = exc
            continue
        # 表头两侧的空格（手工编辑常见）不应让整列失效
        return [{(key or "").strip(): value for key, value in row.items()} for row in rows]
    if last_error:
        raise last_error
    return []


def write_csv_dicts(path: Path, rows: list[dict[str, Any]], headers: list[str]) -> None:
    """先写临时文件再替换，写到一半出错（磁盘满、被占用）不会留下半截文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".writing")
    with tmp.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({h: row.get(h, "") for h in headers})
    try:
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def data_csv_path(data_dir: Path, name: str, legacy_name: str) -> Path:
    """输入 CSV 优先用中文文件名，找不到时兼容旧英文文件名。"""
    path = data_dir / name
    if path.exists():
        return path
    return data_dir / legacy_name
