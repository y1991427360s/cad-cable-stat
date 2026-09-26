"""按 CAD 固定路径自动统计电缆长度（命令行入口）。

核心逻辑已拆到 cable_stat 包里（见 cable_stat/__init__.py 的模块说明）；
本文件保留原来的命令行用法和函数名，旧脚本、测试 `from calculate_cable_lengths import ...` 照常可用。

    python calculate_cable_lengths.py --workbook <清册.xlsx> --data-dir <data目录> --output <结果.xlsx>
"""

from __future__ import annotations

import argparse
from pathlib import Path

try:
    import openpyxl  # noqa: F401
except ImportError as exc:
    raise SystemExit("缺少 openpyxl，请先安装：python -m pip install openpyxl") from exc

from cable_stat.calc import DETAIL_HEADERS, calculate_rows, is_route_bent, json_safe, path_description  # noqa: F401
from cable_stat.csvio import data_csv_path, read_csv_dicts, write_csv_dicts  # noqa: F401
from cable_stat.geometry import (  # noqa: F401
    point_in_polygon,
    point_on_segment,
    polygon_signed_area,
    project_to_segment,
    segment_boundary_parameters,
)
from cable_stat.graph import RouteGraph, build_graph, make_attachment  # noqa: F401
from cable_stat.loaders import (  # noqa: F401
    apply_aliases,
    load_aliases,
    load_cabinets,
    load_rooms,
    load_rule_overrides,
    load_segments,
    load_shafts,
    load_workbook_rows,
    normalize_shaft_base_id,
    shaft_suffix_floor,
)
from cable_stat.models import AttachPoint, Cabinet, Room, Segment, Shaft  # noqa: F401
from cable_stat.params import load_params  # noqa: F401
from cable_stat.pipeline import RunResult, print_report, run
from cable_stat.report import (  # noqa: F401
    CABINET_CHECK_HEADERS,
    make_cabinet_check_rows,
    write_results_to_workbook,
)
from cable_stat.rooms import (  # noqa: F401
    analyze_path_rooms,
    check_suspicious_rooms,
    rooms_for_point,
    segment_room_relation,
    validate_room_assignments,
)
from cable_stat.text import (  # noqa: F401
    floor_sort_key,
    infer_floor_from_layer,
    normalize_cabinet_name,
    normalize_floor,
    normalize_header,
    normalize_text,
    parse_float,
)
from cable_stat.visual import (  # noqa: F401
    build_visualization_data,
    collect_floors,
    compute_floor_elevations,
    compute_floor_offsets,
    write_visualization_files,
)


def main(argv: list[str] | None = None) -> RunResult:
    parser = argparse.ArgumentParser(description="按CAD固定路径自动统计电缆长度")
    parser.add_argument("--workbook", required=True, type=Path, help="自动统计.xlsx 路径")
    parser.add_argument("--data-dir", required=True, type=Path, help="包含CSV数据的目录")
    parser.add_argument("--output", required=True, type=Path, help="输出xlsx路径")
    args = parser.parse_args(argv)
    result = run(args.workbook, args.data_dir, args.output)
    print_report(result)
    return result


if __name__ == "__main__":
    main()
