"""对比两次计算的 统计明细.csv（重构/优化前后回归用）：python tools/compare_details.py <旧outputs> <新outputs>"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

FIELDS = ["起点", "终点", "起点房间", "终点房间", "跨房间", "自动统计", "向上取整", "差值", "基础路径长度", "修正量", "规则", "状态", "路径说明"]


def load(path: Path) -> dict[str, dict[str, str]]:
    with (path / "统计明细.csv").open(encoding="utf-8-sig", newline="") as f:
        return {row["行号"]: row for row in csv.DictReader(f)}


def main() -> int:
    old, new = load(Path(sys.argv[1])), load(Path(sys.argv[2]))
    diffs = 0
    for key in sorted(set(old) | set(new), key=lambda k: int(k)):
        a, b = old.get(key), new.get(key)
        if a is None or b is None:
            print(f"行 {key}: 只在{'新' if a is None else '旧'}结果里")
            diffs += 1
            continue
        changed = [(f, a.get(f, ""), b.get(f, "")) for f in FIELDS if a.get(f, "") != b.get(f, "")]
        if changed:
            diffs += 1
            if diffs <= 30:
                print(f"行 {key}: " + "；".join(f"{f}: {x!r} -> {y!r}" for f, x, y in changed))
    print(f"共 {len(new)} 行，差异 {diffs} 行")
    return 1 if diffs else 0


if __name__ == "__main__":
    sys.exit(main())
