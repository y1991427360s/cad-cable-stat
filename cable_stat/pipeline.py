"""一次完整计算：读数据 → 建路径网 → 算长度 → 写 Excel / CSV / 可视化，返回结构化结果供界面展示。"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .calc import DETAIL_HEADERS, calculate_rows
from .csvio import write_csv_dicts
from .graph import build_graph
from .loaders import (
    apply_aliases,
    apply_case_insensitive_matches,
    load_aliases,
    load_cabinets,
    load_rooms,
    load_rule_overrides,
    load_segments,
    load_shafts,
    load_workbook_rows,
)
from .params import PARAMS_FILE, load_params, load_params_with_warnings
from .report import (
    CABINET_CHECK_HEADERS,
    build_summary,
    classify_issues,
    make_cabinet_check_rows,
    write_results_to_workbook,
)
from .rooms import check_suspicious_rooms, validate_room_assignments
from .visual import build_visualization_data, collect_floors, compute_floor_offsets, write_visualization_files


@dataclass
class RunResult:
    workbook: Path
    output_workbook: Path
    html_path: Path
    json_path: Path
    total: int
    ok: int
    failed: int
    issues: list[tuple[str, str]]
    summary: dict[str, Any]
    cabinet_check_rows: list[dict[str, Any]]
    cad_cabinet_names: list[str]
    elapsed: float
    notes: list[str] = field(default_factory=list)


def duplicate_cable_ids(rows: list[dict[str, Any]]) -> list[str]:
    seen: dict[str, list[int]] = defaultdict(list)
    for row in rows:
        if row["电缆编号"]:
            seen[row["电缆编号"]].append(row["row_no"])
    warnings = []
    for cable_id, row_nos in seen.items():
        if len(row_nos) > 1:
            shown = "、".join(str(n) for n in row_nos[:6]) + ("…" if len(row_nos) > 6 else "")
            warnings.append(f"电缆编号重复：{cable_id}（第 {shown} 行），请确认是否重复录入")
    return warnings


def run(
    workbook: Path, data_dir: Path, output: Path,
    progress: Callable[[float, str], None] | None = None,
) -> RunResult:
    started = time.perf_counter()
    def advance(value: float, message: str) -> None:
        if progress is not None:
            progress(value, message)

    advance(5, "读取清册与计算参数")
    notes: list[str] = []
    params = load_params(data_dir)
    _, param_warnings = load_params_with_warnings(data_dir)
    wb, ws, headers, rows, required_names = load_workbook_rows(workbook)
    try:
        cabinets, cabinet_warnings = load_cabinets(data_dir)
        cad_cabinet_names = sorted(cabinets)
        aliases, alias_warnings = load_aliases(data_dir)
        alias_warnings += apply_aliases(cabinets, aliases)
        alias_warnings += apply_case_insensitive_matches(cabinets, required_names)
        rule_overrides, override_warnings = load_rule_overrides(data_dir)
        segments = load_segments(data_dir, params)
        shafts = load_shafts(data_dir, params)
        rooms, room_warnings = load_rooms(data_dir)
        advance(22, "构建路径网络与柜子接入")
        graph, cabinet_nodes, graph_issues = build_graph(segments, cabinets, shafts, required_names, params)
        room_warnings += validate_room_assignments(cabinets, rooms)
        room_warnings += check_suspicious_rooms(rooms, cabinets, segments)
        advance(48, "计算最短路径、电缆长度与修正量")
        detail_rows, visual_cables = calculate_rows(rows, graph, cabinet_nodes, cabinets, params, rule_overrides, rooms)
        cabinet_check_rows = make_cabinet_check_rows(rows, cabinets)
        floor_offsets, offset_warnings = compute_floor_offsets(collect_floors(segments, cabinets, shafts), shafts)

        issues = (
            param_warnings + cabinet_warnings + alias_warnings + override_warnings + duplicate_cable_ids(rows)
            + room_warnings + graph_issues + offset_warnings
        )
        if not (data_dir / PARAMS_FILE).exists():
            issues.insert(0, "参数提示：data 里没有 参数.csv，本次全部用内置默认值；需要改参数时从项目模板复制一份")
        failed_count = sum(1 for r in detail_rows if r["状态"] != "OK")
        if failed_count:
            issues.insert(0, f"共有 {failed_count} 条电缆未能计算，详见“统计明细”状态列")
        ok_count = len(detail_rows) - failed_count
        issues.insert(0, f"已计算 {ok_count} 条，未计算 {failed_count} 条")

        advance(72, "写入结果工作簿")
        output_path = write_results_to_workbook(
            workbook, output, wb, ws, headers, detail_rows, params, cabinet_check_rows, issues
        )
        outputs_dir = output.parent
        for name, csv_rows, csv_headers in (
            ("统计明细.csv", detail_rows, DETAIL_HEADERS),
            ("柜子清单.csv", cabinet_check_rows, CABINET_CHECK_HEADERS),
        ):
            try:
                write_csv_dicts(outputs_dir / name, csv_rows, csv_headers)
            except PermissionError:
                notes.append(f"注意：{name} 正被占用（多半在 Excel 里开着），本次没有更新它；结果工作簿里有同样的表")
        advance(88, "生成路径可视化与明细")
        visual_data = build_visualization_data(
            workbook, output_path, params, graph, segments, cabinets, shafts, rooms, visual_cables, issues, floor_offsets
        )
        json_path, html_path = write_visualization_files(outputs_dir, visual_data)
        advance(100, "统计完成")
        return RunResult(
            workbook=workbook,
            output_workbook=output_path,
            html_path=html_path,
            json_path=json_path,
            total=len(detail_rows),
            ok=ok_count,
            failed=failed_count,
            issues=classify_issues(issues),
            summary=build_summary(detail_rows, params),
            cabinet_check_rows=cabinet_check_rows,
            cad_cabinet_names=cad_cabinet_names,
            elapsed=time.perf_counter() - started,
            notes=notes,
        )
    finally:
        wb.close()


def print_report(result: RunResult) -> None:
    """运行日志：输出位置、数量、需要人处理的问题（错误/参数提示/房间）。"""
    for note in result.notes:
        print(note)
    print(f"已输出：{result.output_workbook}")
    print(f"路径可视化页面：{result.html_path}")
    print(f"路径可视化数据：{result.json_path}")
    summary = result.summary
    print(
        f"已计算 {result.ok} 条，未计算 {result.failed} 条；"
        f"自动统计合计 {summary['auto_sum']:.1f} 米，向上取整合计 {summary['ceil_sum']} 米（用时 {result.elapsed:.1f} 秒）"
    )
    shown = 0
    hidden = 0
    for level, text in result.issues:
        if text.startswith(("参数提示", "房间范围")):
            print(text)
        elif level == "错误" and not text.startswith("共有"):
            if shown < 40:
                print(f"[{level}] {text.strip()}")
                shown += 1
            else:
                hidden += 1
    if hidden:
        print(f"……另有 {hidden} 条错误，见结果工作簿“问题清单”")
    if result.failed:
        print("请查看输出工作簿的“统计明细”和“问题清单”。")
