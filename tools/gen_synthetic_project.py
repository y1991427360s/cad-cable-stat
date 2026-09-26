"""生成大规模合成项目，用于性能基准和重构前后结果对比（不含任何真实工程数据）。

用法：
    python tools/gen_synthetic_project.py <输出项目文件夹> [--floors 3] [--grid 12] [--cables 3000] [--seed 1]

生成的项目与真实项目结构相同：自动统计.xlsx + data\\（柜子坐标/路径线段/竖井/房间范围/参数）。
路径网是每层一张“井”字形电缆沟网格（内部十字交叉），每个网格单元再挂一条 T 型支线，
柜子放在支线末端附近；同名竖井在各层同一位置，楼层平面在 CAD 里按 X 方向并排摆放。
"""

from __future__ import annotations

import argparse
import csv
import random
import sys
from pathlib import Path

import openpyxl


def write_csv(path: Path, headers: list[str], rows: list[list]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(rows)


def generate(project: Path, floors: int, grid: int, cables: int, seed: int) -> None:
    rnd = random.Random(seed)
    pitch = 6000.0  # 网格间距 6m（毫米图）
    size = pitch * grid
    floor_gap = size + 20000.0  # 各层平面在 CAD 里并排摆放
    segments: list[list] = []
    cabinets: list[list] = []
    shafts: list[list] = []
    rooms: list[list] = []
    handle = 0x1000

    def next_handle() -> str:
        nonlocal handle
        handle += 1
        return f"{handle:X}"

    for f in range(1, floors + 1):
        fl = f"{f}F"
        ox = (f - 1) * floor_gap
        layer = f"CABLE_ROUTE_{fl}"
        # 贯通的横纵主干：每条是一整根线，与其他主干内部交叉
        for i in range(grid + 1):
            y = i * pitch
            h = next_handle()
            segments.append([f"R{h}", fl, ox, y, ox + size, y, size, layer, h, 1])
            x = ox + i * pitch
            h = next_handle()
            segments.append([f"R{h}", fl, x, 0, x, size, size, layer, h, 1])
        # 每个网格单元一条 T 型支线，从下边主干中点伸进单元
        for i in range(grid):
            for j in range(grid):
                bx = ox + i * pitch + pitch / 2
                by = j * pitch
                h = next_handle()
                segments.append([f"R{h}", fl, bx, by, bx, by + pitch * 0.6, pitch * 0.6, layer, h, 1])
                name = f"{fl}-柜{i:02d}{j:02d}"
                cabinets.append([name, fl, "", bx + 300, by + pitch * 0.6 + 200, f"CABLE_CABINET_{fl}", "TEXT", next_handle()])
        # 房间：每 4×4 个单元一间
        step = 4
        for i in range(0, grid, step):
            for j in range(0, grid, step):
                name = f"{fl}房间{i}{j}"
                h = next_handle()
                x0, y0 = ox + i * pitch + 100, j * pitch + 100
                x1, y1 = ox + min(grid, i + step) * pitch - 100, min(grid, j + step) * pitch - 100
                for k, (x, y) in enumerate(((x0, y0), (x1, y0), (x1, y1), (x0, y1)), start=1):
                    rooms.append([name, fl, k, x, y, f"CABLE_ROOM_{fl}", h])
        # 竖井：四个角附近各一个，同名跨层
        for n, (sx, sy) in enumerate(((pitch, pitch), (size - pitch, pitch), (pitch, size - pitch), (size - pitch, size - pitch)), start=1):
            shafts.append([f"ZJ{n}_{fl}", fl, ox + sx + 250, sy + 250, "", f"CABLE_SHAFT_{fl}", "TEXT", next_handle()])

    data = project / "data"
    write_csv(data / "路径线段.csv", ["路径编号", "楼层", "起点X", "起点Y", "终点X", "终点Y", "长度_CAD单位", "图层", "句柄", "段号"], segments)
    write_csv(data / "柜子坐标.csv", ["柜子名称", "楼层", "房间", "X", "Y", "图层", "对象类型", "句柄"], cabinets)
    write_csv(data / "竖井.csv", ["竖井编号", "楼层", "X", "Y", "高度", "图层", "对象类型", "句柄"], shafts)
    write_csv(data / "房间范围.csv", ["房间名称", "楼层", "顶点序号", "X", "Y", "图层", "句柄"], rooms)
    write_csv(
        data / "参数.csv",
        ["参数", "值", "备注"],
        [["CAD每米单位", 1000, ""], ["吸附容差", 1, ""], ["最大接入距离", 20000, ""]],
    )

    names = [row[0] for row in cabinets]
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["序号", "电缆编号", "型号", "起点", "终点", "电缆长度", "自动统计"])
    models = ["ZR-KVVP2-22 4×1.5", "ZR-KVVP2-22 7×1.5", "ZR-YJV22 4×25", "屏蔽双绞线"]
    for n in range(1, cables + 1):
        a, b = rnd.sample(names, 2)
        ws.append([n, f"C{n:05d}", rnd.choice(models), a, b, rnd.randint(10, 300), None])
    project.mkdir(parents=True, exist_ok=True)
    wb.save(project / "自动统计.xlsx")
    print(f"已生成：{project}（{floors} 层，{len(segments)} 条线段，{len(cabinets)} 个柜子，{cables} 条电缆）")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("project", type=Path)
    parser.add_argument("--floors", type=int, default=3)
    parser.add_argument("--grid", type=int, default=12)
    parser.add_argument("--cables", type=int, default=3000)
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args(argv)
    generate(args.project, args.floors, args.grid, args.cables, args.seed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
