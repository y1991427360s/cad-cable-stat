"""路径可视化：把计算结果整理成页面数据，嵌入 visualizer.html 模板生成单文件 HTML。

数据格式（version 4）：全部电缆共用一张去重的边表 edges，每条电缆的 path 只存边序号，
序号为负数（~k）表示沿第 k 条边反向走。几千条电缆时文件体积比逐条展开小一个数量级。
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .graph import RouteGraph
from .models import Cabinet, Room, Segment, Shaft
from .report import classify_issues
from .text import floor_sort_key

VISUAL_DATA_VERSION = 4
TEMPLATE_PATH = Path(__file__).with_name("visualizer.html")
DATA_PLACEHOLDER = "__VISUAL_DATA_JSON__"


def rounded_number(value: Any, digits: int = 3) -> float | str:
    if isinstance(value, (int, float)):
        return round(float(value), digits)
    return ""


def visual_node(graph: RouteGraph, node_id: str) -> dict[str, Any]:
    node = graph.nodes[node_id]
    return {
        "id": node_id,
        "floor": node.get("floor", ""),
        "x": rounded_number(node.get("x")),
        "y": rounded_number(node.get("y")),
        "label": node.get("label", ""),
        "kind": node.get("kind", ""),
    }


def visual_edge(graph: RouteGraph, meta: dict[str, Any]) -> dict[str, Any]:
    """路径边只引用节点编号（节点详情在顶层 nodes 里），可视化页面加载时再换回节点对象。"""
    from_floor = graph.nodes[meta["from"]].get("floor", "")
    to_floor = graph.nodes[meta["to"]].get("floor", "")
    edge = {
        "kind": meta.get("kind", ""),
        "from": meta["from"],
        "to": meta["to"],
        "length_m": rounded_number(meta.get("length_m")),
        "floor": from_floor if from_floor == to_floor else "跨楼层",
    }
    for key in ("route_id", "section_no", "layer", "segment_idx", "shaft_id", "name", "attach_kind"):
        if key in meta:
            edge[key] = meta.get(key)
    return edge


def edge_key(meta: dict[str, Any]) -> tuple[Any, ...]:
    a, b = meta["from"], meta["to"]
    ident = meta.get("segment_idx", meta.get("shaft_id", meta.get("name", "")))
    return meta.get("kind", ""), min(a, b), max(a, b), ident


def build_edge_table(graph: RouteGraph, visual_cables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把各电缆的路径边去重成共用边表，电缆里改存 path（边序号，~k 表示反向）。"""
    edges: list[dict[str, Any]] = []
    index: dict[tuple[Any, ...], int] = {}
    for cable in visual_cables:
        path: list[int] = []
        for meta in cable.pop("path_metas", []):
            key = edge_key(meta)
            k = index.get(key)
            if k is None:
                k = index[key] = len(edges)
                edges.append(visual_edge(graph, meta))
            path.append(k if edges[k]["from"] == meta["from"] else ~k)
        cable["path"] = path
        cable.pop("path_nodes", None)
    return edges


def route_segment_to_visual(seg: Segment) -> dict[str, Any]:
    return {
        "idx": seg.idx,
        "route_id": seg.route_id,
        "floor": seg.floor,
        "ax": rounded_number(seg.ax),
        "ay": rounded_number(seg.ay),
        "bx": rounded_number(seg.bx),
        "by": rounded_number(seg.by),
        "length_m": rounded_number(seg.length_m),
        "layer": seg.layer,
        "handle": seg.handle,
        "section_no": seg.section_no,
    }


def cabinet_to_visual(cab: Cabinet) -> dict[str, Any]:
    return {
        "name": cab.name,
        "floor": cab.floor,
        "x": rounded_number(cab.x),
        "y": rounded_number(cab.y),
        "layer": cab.layer,
        "object_type": cab.object_type,
        "handle": cab.handle,
    }


def shaft_to_visual(shaft: Shaft) -> dict[str, Any]:
    return {
        "shaft_id": shaft.shaft_id,
        "base_id": shaft.base_id,
        "floor": shaft.floor,
        "x": rounded_number(shaft.x),
        "y": rounded_number(shaft.y),
        "height_m": rounded_number(shaft.height_m),
        "layer": shaft.layer,
        "object_type": shaft.object_type,
        "handle": shaft.handle,
    }


def room_to_visual(room: Room) -> dict[str, Any]:
    return {
        "name": room.name,
        "floor": room.floor,
        "vertices": [[rounded_number(x), rounded_number(y)] for x, y in room.vertices],
        "layer": room.layer,
        "handle": room.handle,
    }


def compute_floor_elevations(floors: list[str], shafts: list[Shaft], params: dict[str, float]) -> dict[str, float]:
    """各楼层相对标高（CAD单位），供三维视图使用。

    相邻层间距优先取跨这两层配对的同名竖井 max(下层高度, 上层高度)（与 build_graph 竖直边一致），
    无配对时回退到下层竖井最大高度，再无则用参数“竖井高度”。
    """
    unit = params["CAD每米单位"]
    default_gap = params["竖井高度"]
    by_floor: dict[str, dict[str, float]] = defaultdict(dict)
    for shaft in shafts:
        if shaft.floor and shaft.height_m > 0:
            by_floor[shaft.floor][shaft.base_id] = max(by_floor[shaft.floor].get(shaft.base_id, 0.0), shaft.height_m)
    elevations: dict[str, float] = {}
    z = 0.0
    for i, floor in enumerate(floors):
        elevations[floor] = round(z, 3)
        if i + 1 >= len(floors):
            break
        lower = by_floor.get(floor, {})
        upper = by_floor.get(floors[i + 1], {})
        paired = [max(lower[b], upper[b]) for b in lower.keys() & upper.keys()]
        gap_m = max(paired or list(lower.values()) or [default_gap])
        z += gap_m * unit
    return elevations


def compute_floor_offsets(floors: list[str], shafts: list[Shaft]) -> tuple[dict[str, dict[str, float]], list[str]]:
    """三维视图楼层对齐偏移（CAD单位）：CAD 中各楼层平面常并排绘制，用同名竖井把每层平移对齐到首层。

    返回 (偏移表, 告警列表)；无法对齐的楼层保持零偏移并给出告警。
    """
    offsets = {floor: {"dx": 0.0, "dy": 0.0} for floor in floors}
    warnings: list[str] = []
    by_floor: dict[str, dict[str, Shaft]] = defaultdict(dict)
    for shaft in shafts:
        if shaft.floor and shaft.base_id not in by_floor[shaft.floor]:
            by_floor[shaft.floor][shaft.base_id] = shaft
    for i, floor in enumerate(floors[1:], start=1):
        aligned = False
        for ref in floors[:i]:
            common = sorted(set(by_floor.get(floor, {})) & set(by_floor.get(ref, {})))
            if not common:
                continue
            dx = sum(by_floor[ref][b].x - by_floor[floor][b].x for b in common) / len(common)
            dy = sum(by_floor[ref][b].y - by_floor[floor][b].y for b in common) / len(common)
            offsets[floor] = {
                "dx": round(offsets[ref]["dx"] + dx, 3),
                "dy": round(offsets[ref]["dy"] + dy, 3),
            }
            aligned = True
            break
        if not aligned and len(floors) > 1:
            warnings.append(
                f"三维视图：楼层 {floor} 与下层无同名竖井，无法对齐，该层将显示在 CAD 原始位置"
            )
    return offsets, warnings


def collect_floors(segments: list[Segment], cabinets: dict[str, Cabinet], shafts: list[Shaft]) -> list[str]:
    return sorted(
        {item for item in [seg.floor for seg in segments] + [cab.floor for cab in cabinets.values()] + [shaft.floor for shaft in shafts] if item},
        key=floor_sort_key,
    )


def build_visualization_data(
    workbook_path: Path,
    output_path: Path,
    params: dict[str, float],
    graph: RouteGraph | None,
    segments: list[Segment],
    cabinets: dict[str, Cabinet],
    shafts: list[Shaft],
    rooms: list[Room],
    visual_cables: list[dict[str, Any]],
    issues: list[str],
    floor_offsets: dict[str, dict[str, float]] | None = None,
) -> dict[str, Any]:
    floors = sorted(set(collect_floors(segments, cabinets, shafts)) | {room.floor for room in rooms if room.floor}, key=floor_sort_key)
    if floor_offsets is None:
        floor_offsets, _ = compute_floor_offsets(floors, shafts)
    ok_count = sum(1 for cable in visual_cables if cable["status"] == "OK")
    failed_count = len(visual_cables) - ok_count
    edges = build_edge_table(graph, visual_cables) if graph else []
    if not graph:
        for cable in visual_cables:
            cable.pop("path_metas", None)
            cable.pop("path_nodes", None)
            cable["path"] = []
    unique_cabinets = {id(cab): cab for cab in cabinets.values()}.values()
    return {
        "version": VISUAL_DATA_VERSION,
        "title": f"电缆路径可视化 · {Path(workbook_path).parent.name}" if Path(workbook_path).parent.name else "电缆路径可视化",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_workbook": str(workbook_path),
        "output_workbook": str(output_path),
        "summary": {
            "total": len(visual_cables),
            "ok": ok_count,
            "failed": failed_count,
            "auto_sum": round(sum(c["auto_length"] for c in visual_cables if isinstance(c["auto_length"], (int, float))), 1),
            "route_segments": len(segments),
            "cabinets": len(unique_cabinets),
            "shafts": len(shafts),
            "rooms": len(rooms),
        },
        "params": params,
        "floors": floors,
        "floor_elevations": compute_floor_elevations(floors, shafts, params),
        "floor_offsets": floor_offsets,
        "route_segments": [route_segment_to_visual(seg) for seg in segments],
        "cabinets": [cabinet_to_visual(cab) for cab in sorted(unique_cabinets, key=lambda item: (floor_sort_key(item.floor), item.name))],
        "shafts": [shaft_to_visual(shaft) for shaft in sorted(shafts, key=lambda item: (floor_sort_key(item.floor), item.shaft_id))],
        "rooms": [room_to_visual(room) for room in sorted(rooms, key=lambda item: (floor_sort_key(item.floor), item.name))],
        "nodes": [visual_node(graph, node_id) for node_id in sorted(graph.nodes, key=lambda n: int(n[1:]))] if graph else [],
        "edges": edges,
        "gaps": [gap_to_visual(gap) for gap in getattr(graph, "gaps", [])] if graph else [],
        "cables": visual_cables,
        "issues": [{"level": level, "text": text} for level, text in classify_issues(issues)],
    }


def gap_to_visual(gap: dict[str, Any]) -> dict[str, Any]:
    return {
        "floor": gap["floor"],
        "x": rounded_number(gap["x"]),
        "y": rounded_number(gap["y"]),
        "px": rounded_number(gap["px"]),
        "py": rounded_number(gap["py"]),
        "dist": rounded_number(gap["dist"]),
        "dist_m": rounded_number(gap["dist_m"]),
        "route_id": gap["route_id"],
        "bridging": gap["bridging"],
    }


def load_template() -> str:
    try:
        return TEMPLATE_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise FileNotFoundError(f"找不到可视化模板 {TEMPLATE_PATH}，打包时需要把 cable_stat\\visualizer.html 一并加入") from exc


def render_html(visual_data: dict[str, Any]) -> str:
    compact = json.dumps(visual_data, ensure_ascii=False, separators=(",", ":"))
    return load_template().replace(DATA_PLACEHOLDER, compact.replace("</", "<\\/"))


def write_visualization_files(outputs_dir: Path, visual_data: dict[str, Any]) -> tuple[Path, Path]:
    outputs_dir.mkdir(parents=True, exist_ok=True)
    data_path = outputs_dir / "路径可视化数据.json"
    html_path = outputs_dir / "路径可视化.html"
    data_path.write_text(json.dumps(visual_data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    html_path.write_text(render_html(visual_data), encoding="utf-8")
    return data_path, html_path
