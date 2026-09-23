from __future__ import annotations

import argparse
import csv
import difflib
import heapq
import json
import math
import re
import shutil
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
except ImportError as exc:
    raise SystemExit("缺少 openpyxl，请先安装：python -m pip install openpyxl") from exc


DETAIL_HEADERS = [
    "行号",
    "电缆编号",
    "起点",
    "终点",
    "起点房间",
    "终点房间",
    "跨房间",
    "手工长度",
    "自动统计",
    "向上取整",
    "差值",
    "基础路径长度",
    "修正量",
    "规则",
    "状态",
    "路径说明",
]

CABINET_CHECK_HEADERS = ["柜子名称", "清册出现次数", "已提供坐标", "楼层", "X", "Y", "近似CAD柜名建议"]


@dataclass
class Segment:
    idx: int
    route_id: str
    floor: str
    ax: float
    ay: float
    bx: float
    by: float
    length_units: float
    length_m: float
    layer: str
    handle: str
    section_no: str


@dataclass
class Cabinet:
    name: str
    floor: str
    x: float
    y: float
    layer: str
    object_type: str
    handle: str


@dataclass
class Shaft:
    shaft_id: str
    base_id: str
    floor: str
    x: float
    y: float
    height_m: float
    layer: str
    object_type: str
    handle: str


@dataclass
class Room:
    name: str
    floor: str
    vertices: list[tuple[float, float]]
    layer: str
    handle: str


@dataclass
class AttachPoint:
    kind: str
    name: str
    floor: str
    x: float
    y: float
    segment_idx: int
    along_units: float
    distance_units: float
    projection_x: float
    projection_y: float
    node_id: str | None = None
    projection_node_id: str | None = None


def normalize_text(value: Any) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\u3000", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_cabinet_name(value: Any) -> str:
    return re.sub(r"[\s\u00a0\u3000]+", "", normalize_text(value))


def normalize_header(value: Any) -> str:
    """\u8868\u5934\u5339\u914d\uff1a\u5254\u9664\u5168\u90e8\u7a7a\u767d\uff08\u542b\u5355\u5143\u683c\u5185\u6362\u884c\uff09\uff0c\u517c\u5bb9\u8bbe\u8ba1\u9662\u6e05\u518c\u5e38\u89c1\u7684\u300c\u7535\u7f06\u21b5\u957f\u5ea6\u300d\u5199\u6cd5\u3002"""
    return normalize_cabinet_name(value)


def normalize_floor(value: Any) -> str:
    text = normalize_text(value).upper()
    if text in {"1", "1F", "F1"} or "一层" in text or "一楼" in text:
        return "1F"
    if text in {"2", "2F", "F2"} or "二层" in text or "二楼" in text:
        return "2F"
    match = re.fullmatch(r"(\d+)F?|F(\d+)", text)
    if match:
        return f"{match.group(1) or match.group(2)}F"
    return text


def infer_floor_from_layer(layer: str) -> str:
    text = normalize_text(layer)
    if "一层" in text or "一楼" in text:
        return "1F"
    if "二层" in text or "二楼" in text:
        return "2F"
    match = re.search(r"(\d+)F", text.upper())
    if match:
        return f"{match.group(1)}F"
    return ""


def parse_float(value: Any, default: float | None = None) -> float | None:
    text = normalize_text(value)
    if text == "":
        return default
    try:
        return float(text)
    except ValueError:
        return default


def read_csv_dicts(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "gbk", "utf-16"):
        try:
            with path.open("r", encoding=encoding, newline="") as f:
                return list(csv.DictReader(f))
        except UnicodeError as exc:
            last_error = exc
    if last_error:
        raise last_error
    return []


def write_csv_dicts(path: Path, rows: list[dict[str, Any]], headers: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({h: row.get(h, "") for h in headers})


def data_csv_path(data_dir: Path, name: str, legacy_name: str) -> Path:
    """输入 CSV 优先用中文文件名，找不到时兼容旧英文文件名。"""
    path = data_dir / name
    if path.exists():
        return path
    return data_dir / legacy_name


def load_params(data_dir: Path) -> dict[str, float]:
    params = {
        "竖井高度": 4.5,
        "不拐弯修正": 7.0,
        "拐弯倍率": 1.2,
        "CAD每米单位": 1000.0,
        "吸附容差": 0.05,
        "最大接入距离": 20000.0,
        "跨房间修正": 3.0,
    }
    for row in read_csv_dicts(data_dir / "参数.csv"):
        key = normalize_text(row.get("参数"))
        val = parse_float(row.get("值"))
        if key and val is not None:
            params[key] = val
    if params["CAD每米单位"] <= 0:
        raise ValueError("参数“CAD每米单位”必须大于0")
    return params


def load_cabinets(data_dir: Path) -> tuple[dict[str, Cabinet], list[str]]:
    cabinets: dict[str, Cabinet] = {}
    warnings: list[str] = []
    for row in read_csv_dicts(data_csv_path(data_dir, "柜子坐标.csv", "cabinets.csv")):
        name = normalize_cabinet_name(row.get("柜子名称"))
        x = parse_float(row.get("X"))
        y = parse_float(row.get("Y"))
        if not name or x is None or y is None:
            continue
        floor = normalize_floor(row.get("楼层")) or infer_floor_from_layer(row.get("图层", ""))
        cabinet = Cabinet(
            name=name,
            floor=floor,
            x=x,
            y=y,
            layer=normalize_text(row.get("图层")),
            object_type=normalize_text(row.get("对象类型")),
            handle=normalize_text(row.get("句柄")),
        )
        if name in cabinets:
            warnings.append(f"柜子名称重复：{name}，已采用第一条坐标")
            continue
        cabinets[name] = cabinet
    return cabinets, warnings


def load_segments(data_dir: Path, params: dict[str, float]) -> list[Segment]:
    segments: list[Segment] = []
    unit = params["CAD每米单位"]
    for i, row in enumerate(read_csv_dicts(data_csv_path(data_dir, "路径线段.csv", "route_segments.csv")), start=1):
        ax = parse_float(row.get("起点X"))
        ay = parse_float(row.get("起点Y"))
        bx = parse_float(row.get("终点X"))
        by = parse_float(row.get("终点Y"))
        if None in (ax, ay, bx, by):
            continue
        layer = normalize_text(row.get("图层"))
        floor = normalize_floor(row.get("楼层")) or infer_floor_from_layer(layer)
        length_units = parse_float(row.get("长度_CAD单位"))
        chord = math.hypot(bx - ax, by - ay)
        if length_units is None or length_units <= 0:
            length_units = chord
        if length_units <= 0:
            continue
        segments.append(
            Segment(
                idx=i,
                route_id=normalize_text(row.get("路径编号")) or f"R{i}",
                floor=floor,
                ax=ax,
                ay=ay,
                bx=bx,
                by=by,
                length_units=length_units,
                length_m=length_units / unit,
                layer=layer,
                handle=normalize_text(row.get("句柄")),
                section_no=normalize_text(row.get("段号")),
            )
        )
    return segments


def normalize_shaft_base_id(value: str) -> str:
    text = normalize_text(value)
    text = re.sub(r"[_\-\s]*(\d+F|F\d+)$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"[一二三四五六七八九十]+[层楼]$", "", text)
    return text or "竖井"


def load_shafts(data_dir: Path, params: dict[str, float]) -> list[Shaft]:
    shafts: list[Shaft] = []
    default_height = params["竖井高度"]
    for row in read_csv_dicts(data_csv_path(data_dir, "竖井.csv", "shafts.csv")):
        shaft_id = normalize_text(row.get("竖井编号"))
        x = parse_float(row.get("X"))
        y = parse_float(row.get("Y"))
        if not shaft_id or x is None or y is None:
            continue
        layer = normalize_text(row.get("图层"))
        floor = normalize_floor(row.get("楼层")) or infer_floor_from_layer(layer)
        height = parse_float(row.get("高度"), default_height) or default_height
        shafts.append(
            Shaft(
                shaft_id=shaft_id,
                base_id=normalize_shaft_base_id(shaft_id),
                floor=floor,
                x=x,
                y=y,
                height_m=height,
                layer=layer,
                object_type=normalize_text(row.get("对象类型")),
                handle=normalize_text(row.get("句柄")),
            )
        )
    return shafts


def load_rooms(data_dir: Path) -> tuple[list[Room], list[str]]:
    """读取房间边界顶点；同层重名或无效多边形不会参与计算。"""
    rows = read_csv_dicts(data_csv_path(data_dir, "房间范围.csv", "rooms.csv"))
    grouped: dict[tuple[str, str], list[tuple[int, float, float, str, str]]] = defaultdict(list)
    warnings: list[str] = []
    for row_no, row in enumerate(rows, start=2):
        name = normalize_text(row.get("房间名称"))
        layer = normalize_text(row.get("图层"))
        floor = normalize_floor(row.get("楼层")) or infer_floor_from_layer(layer)
        handle = normalize_text(row.get("句柄"))
        vertex_no = parse_float(row.get("顶点序号"))
        x = parse_float(row.get("X"))
        y = parse_float(row.get("Y"))
        if not name or not floor or x is None or y is None:
            warnings.append(f"房间范围第 {row_no} 行无效：需要房间名称、楼层、X、Y")
            continue
        key = (floor, handle or f"名称:{name}")
        grouped[key].append((int(vertex_no or row_no), x, y, name, layer))

    candidates: list[Room] = []
    for (floor, handle_key), points in grouped.items():
        points.sort(key=lambda item: item[0])
        name = points[0][3]
        layer = points[0][4]
        if any(item[3] != name for item in points):
            warnings.append(f"房间范围句柄 {handle_key} 包含多个房间名称，已忽略")
            continue
        vertices: list[tuple[float, float]] = []
        for _, x, y, _, _ in points:
            point = (x, y)
            if not vertices or point != vertices[-1]:
                vertices.append(point)
        if len(vertices) > 1 and vertices[0] == vertices[-1]:
            vertices.pop()
        if len(vertices) < 3 or abs(polygon_signed_area(vertices)) <= 1e-9:
            warnings.append(f"房间范围无效：{floor} {name} 至少需要三个不共线顶点，已忽略")
            continue
        candidates.append(Room(name=name, floor=floor, vertices=vertices, layer=layer, handle="" if handle_key.startswith("名称:") else handle_key))

    name_counts: dict[tuple[str, str], int] = defaultdict(int)
    for room in candidates:
        name_counts[(room.floor, room.name)] += 1
    rooms: list[Room] = []
    for room in candidates:
        if name_counts[(room.floor, room.name)] > 1:
            warnings.append(f"房间名称同层重复：{room.floor} {room.name}，相关范围已忽略")
            continue
        rooms.append(room)
    return rooms, list(dict.fromkeys(warnings))


def polygon_signed_area(vertices: list[tuple[float, float]]) -> float:
    return 0.5 * sum(
        ax * by - bx * ay
        for (ax, ay), (bx, by) in zip(vertices, vertices[1:] + vertices[:1])
    )


def point_on_segment(point: tuple[float, float], a: tuple[float, float], b: tuple[float, float], tolerance: float = 1e-8) -> bool:
    px, py = point
    ax, ay = a
    bx, by = b
    cross = (px - ax) * (by - ay) - (py - ay) * (bx - ax)
    scale = max(1.0, math.hypot(bx - ax, by - ay))
    if abs(cross) > tolerance * scale:
        return False
    dot = (px - ax) * (px - bx) + (py - ay) * (py - by)
    return dot <= tolerance * scale


def point_in_polygon(point: tuple[float, float], vertices: list[tuple[float, float]], include_boundary: bool = True) -> bool:
    for a, b in zip(vertices, vertices[1:] + vertices[:1]):
        if point_on_segment(point, a, b):
            return include_boundary
    x, y = point
    inside = False
    for (ax, ay), (bx, by) in zip(vertices, vertices[1:] + vertices[:1]):
        if (ay > y) != (by > y):
            cross_x = ax + (y - ay) * (bx - ax) / (by - ay)
            if cross_x > x:
                inside = not inside
    return inside


def segment_boundary_parameters(
    a: tuple[float, float], b: tuple[float, float], vertices: list[tuple[float, float]]
) -> list[float]:
    """返回线段与多边形边界交点在线段上的参数，包含共线重叠端点。"""
    ax, ay = a
    bx, by = b
    rx, ry = bx - ax, by - ay
    rr = rx * rx + ry * ry
    values = [0.0, 1.0]
    if rr <= 1e-18:
        return values
    for (cx, cy), (dx, dy) in zip(vertices, vertices[1:] + vertices[:1]):
        sx, sy = dx - cx, dy - cy
        denom = rx * sy - ry * sx
        qx, qy = cx - ax, cy - ay
        if abs(denom) <= 1e-12:
            if abs(qx * ry - qy * rx) <= 1e-9:
                for px, py in ((cx, cy), (dx, dy)):
                    t = ((px - ax) * rx + (py - ay) * ry) / rr
                    if -1e-9 <= t <= 1.0 + 1e-9:
                        values.append(min(1.0, max(0.0, t)))
            continue
        t = (qx * sy - qy * sx) / denom
        u = (qx * ry - qy * rx) / denom
        if -1e-9 <= t <= 1.0 + 1e-9 and -1e-9 <= u <= 1.0 + 1e-9:
            values.append(min(1.0, max(0.0, t)))
    return sorted({round(value, 12) for value in values})


def segment_room_relation(a: tuple[float, float], b: tuple[float, float], room: Room) -> tuple[bool, bool]:
    """返回（与房间有正长度接触或端点在内，线段完全位于房间内）。"""
    params = segment_boundary_parameters(a, b, room.vertices)
    samples = [0.0, 1.0]
    samples.extend((left + right) / 2.0 for left, right in zip(params, params[1:]) if right - left > 1e-10)
    ax, ay = a
    bx, by = b
    inside = [point_in_polygon((ax + (bx - ax) * t, ay + (by - ay) * t), room.vertices) for t in samples]
    return any(inside), all(inside)


def rooms_for_point(floor: str, x: float, y: float, rooms: list[Room]) -> list[Room]:
    return [room for room in rooms if room.floor == floor and point_in_polygon((x, y), room.vertices)]


def validate_room_assignments(cabinets: dict[str, Cabinet], rooms: list[Room]) -> list[str]:
    warnings: list[str] = []
    seen_cabinets: set[int] = set()
    for name, cabinet in cabinets.items():
        identity = id(cabinet)
        if identity in seen_cabinets:
            continue
        seen_cabinets.add(identity)
        matches = rooms_for_point(cabinet.floor, cabinet.x, cabinet.y, rooms)
        if len(matches) > 1:
            warnings.append(f"柜子同时位于多个房间范围内：{name}（{'、'.join(room.name for room in matches)}），请检查范围重叠")
    return warnings


def load_aliases(data_dir: Path) -> tuple[dict[str, str], list[str]]:
    """读取柜名别名表：清册名称 -> CAD名称。文件不存在时返回空映射。"""
    aliases: dict[str, str] = {}
    warnings: list[str] = []
    for row in read_csv_dicts(data_dir / "柜名别名.csv"):
        source = normalize_cabinet_name(row.get("清册名称"))
        target = normalize_cabinet_name(row.get("CAD名称"))
        if not source or not target:
            continue
        if source == target:
            continue
        if source in aliases:
            if aliases[source] != target:
                warnings.append(f"柜名别名重复且指向不同CAD名称：{source}，已采用第一条")
            continue
        aliases[source] = target
    return aliases, warnings


def apply_aliases(cabinets: dict[str, Cabinet], aliases: dict[str, str]) -> list[str]:
    """把别名注册进柜子表，使清册名称能直接命中CAD柜子坐标。"""
    warnings: list[str] = []
    for source, target in aliases.items():
        cabinet = cabinets.get(target)
        if cabinet is None:
            warnings.append(f"柜名别名目标在 柜子坐标.csv 中不存在：{source} -> {target}")
            continue
        existing = cabinets.get(source)
        if existing is not None and existing is not cabinet:
            warnings.append(f"柜名别名与已有柜子同名，忽略别名：{source} -> {target}")
            continue
        cabinets[source] = cabinet
    return warnings


def load_rule_overrides(data_dir: Path) -> tuple[dict[tuple[str, str], dict[str, Any]], list[str]]:
    """读取强制规则表：按起点+终点覆盖自动判断的修正量。文件不存在时返回空映射。"""
    overrides: dict[tuple[str, str], dict[str, Any]] = {}
    warnings: list[str] = []
    for row in read_csv_dicts(data_dir / "强制规则.csv"):
        start = normalize_cabinet_name(row.get("起点"))
        end = normalize_cabinet_name(row.get("终点"))
        extra = parse_float(row.get("修正量"))
        if not start and not end:
            continue
        if not start or not end or extra is None:
            warnings.append(f"强制规则无效（需要起点、终点和数字修正量）：{start or '?'} -> {end or '?'}")
            continue
        if (start, end) in overrides or (end, start) in overrides:
            warnings.append(f"强制规则重复：{start} -> {end}，已采用第一条")
            continue
        overrides[(start, end)] = {"extra": extra, "note": normalize_text(row.get("备注"))}
    return overrides, warnings


def shaft_suffix_floor(shaft_id: str) -> str:
    """从竖井编号尾部解析楼层标注，例如 ZJ1_1F -> 1F；无标注返回空。"""
    match = re.search(r"(\d+F|F\d+|一层|一楼|二层|二楼)$", normalize_text(shaft_id), flags=re.IGNORECASE)
    return normalize_floor(match.group(1)) if match else ""


def load_workbook_rows(workbook_path: Path) -> tuple[Any, Any, dict[str, int], list[dict[str, Any]], list[str]]:
    wb = openpyxl.load_workbook(workbook_path)
    ws = wb.active
    headers = {normalize_header(cell.value): cell.column for cell in ws[1] if normalize_header(cell.value)}
    required = ["电缆编号", "起点", "终点", "电缆长度"]
    missing = [h for h in required if h not in headers]
    if missing:
        raise ValueError(f"清册缺少表头：{', '.join(missing)}")
    if "自动统计" not in headers:
        auto_col = max(headers.values()) + 1
        ws.cell(1, auto_col).value = "自动统计"
        headers["自动统计"] = auto_col
    if "向上取整" not in headers:
        ceil_col = headers["自动统计"] + 1
        if any(col >= ceil_col for col in headers.values()):
            # 「自动统计」右侧还有别的列，插入新列并顺移已记录的列号
            ws.insert_cols(ceil_col)
            headers = {name: (col + 1 if col >= ceil_col else col) for name, col in headers.items()}
        ws.cell(1, ceil_col).value = "向上取整"
        headers["向上取整"] = ceil_col
    rows: list[dict[str, Any]] = []
    names: set[str] = set()
    for row_no in range(2, ws.max_row + 1):
        start = normalize_cabinet_name(ws.cell(row_no, headers["起点"]).value)
        end = normalize_cabinet_name(ws.cell(row_no, headers["终点"]).value)
        if not start and not end:
            continue
        if start:
            names.add(start)
        if end:
            names.add(end)
        rows.append(
            {
                "row_no": row_no,
                "电缆编号": normalize_text(ws.cell(row_no, headers["电缆编号"]).value),
                "起点": start,
                "终点": end,
                "手工长度": ws.cell(row_no, headers["电缆长度"]).value,
            }
        )
    return wb, ws, headers, rows, sorted(names)


class RouteGraph:
    def __init__(self, segments: list[Segment], params: dict[str, float]):
        self.segments = {s.idx: s for s in segments}
        self.params = params
        self.unit = params["CAD每米单位"]
        self.snap_tol = params["吸附容差"]
        self.nodes: dict[str, dict[str, Any]] = {}
        self.graph: dict[str, list[tuple[str, float, dict[str, Any]]]] = defaultdict(list)
        self.segment_points: dict[int, list[tuple[float, str]]] = defaultdict(list)
        self._snap_nodes_by_floor: dict[str, list[str]] = defaultdict(list)
        self._next_node_no = 1
        self._init_segment_endpoints()

    def _new_node(self, floor: str, x: float, y: float, label: str, kind: str) -> str:
        node_id = f"N{self._next_node_no}"
        self._next_node_no += 1
        self.nodes[node_id] = {"floor": floor, "x": x, "y": y, "label": label, "kind": kind}
        return node_id

    def _get_snapped_node(self, floor: str, x: float, y: float, label: str = "", kind: str = "route") -> str:
        best_id = None
        best_dist = None
        for node_id in self._snap_nodes_by_floor[floor]:
            node = self.nodes[node_id]
            dist = math.hypot(x - node["x"], y - node["y"])
            if dist <= self.snap_tol and (best_dist is None or dist < best_dist):
                best_id = node_id
                best_dist = dist
        if best_id is not None:
            return best_id
        node_id = self._new_node(floor, x, y, label, kind)
        self._snap_nodes_by_floor[floor].append(node_id)
        return node_id

    def _init_segment_endpoints(self) -> None:
        for seg in self.segments.values():
            a = self._get_snapped_node(seg.floor, seg.ax, seg.ay, f"{seg.route_id}-起点", "route")
            b = self._get_snapped_node(seg.floor, seg.bx, seg.by, f"{seg.route_id}-终点", "route")
            self.segment_points[seg.idx].append((0.0, a))
            self.segment_points[seg.idx].append((seg.length_units, b))

    def connect_t_junctions(self) -> int:
        """把落在其他线段中间（吸附容差内）的路径端点接入该线段，支持T型连接。"""
        count = 0
        for seg in self.segments.values():
            own_nodes = {node_id for _, node_id in self.segment_points[seg.idx]}
            for node_id in self._snap_nodes_by_floor[seg.floor]:
                if node_id in own_nodes:
                    continue
                node = self.nodes[node_id]
                _, _, along, dist = project_to_segment(node["x"], node["y"], seg)
                if dist <= self.snap_tol and self.snap_tol < along < seg.length_units - self.snap_tol:
                    self.segment_points[seg.idx].append((along, node_id))
                    count += 1
        return count

    def connect_cross_junctions(self) -> int:
        """检测同层路径线段内部十字交叉/斜交，并在交点处自动打通切分，使电缆能通过交叉点转折。"""
        count = 0
        floor_segs: dict[str, list[Segment]] = defaultdict(list)
        for seg in self.segments.values():
            floor_segs[seg.floor].append(seg)

        for floor, seg_list in floor_segs.items():
            n = len(seg_list)
            for i in range(n):
                s1 = seg_list[i]
                l1 = s1.length_units
                if l1 <= self.snap_tol * 2:
                    continue
                r1x, r1y = s1.bx - s1.ax, s1.by - s1.ay
                for j in range(i + 1, n):
                    s2 = seg_list[j]
                    l2 = s2.length_units
                    if l2 <= self.snap_tol * 2:
                        continue
                    r2x, r2y = s2.bx - s2.ax, s2.by - s2.ay

                    # 计算方向向量叉积
                    denom = r1x * r2y - r1y * r2x
                    if abs(denom) <= 1e-12:
                        continue  # 平行或共线

                    # 参数 t 对应 s1，u 对应 s2
                    q12x = s2.ax - s1.ax
                    q12y = s2.ay - s1.ay
                    t = (q12x * r2y - q12y * r2x) / denom
                    u = (q12x * r1y - q12y * r1x) / denom

                    t_min = self.snap_tol / l1
                    t_max = 1.0 - (self.snap_tol / l1)
                    u_min = self.snap_tol / l2
                    u_max = 1.0 - (self.snap_tol / l2)

                    # 仅处理两条线段内部交叉（远离端点的情况，端点相交已由吸附/T型连接处理）
                    if t_min < t < t_max and u_min < u < u_max:
                        px = s1.ax + t * r1x
                        py = s1.ay + t * r1y
                        along1 = t * l1
                        along2 = u * l2
                        cross_node = self._get_snapped_node(
                            floor,
                            px,
                            py,
                            f"交叉点-{s1.route_id}x{s2.route_id}",
                            "cross",
                        )
                        self.segment_points[s1.idx].append((along1, cross_node))
                        self.segment_points[s2.idx].append((along2, cross_node))
                        count += 1
        return count

    def nearest_segment(self, floor: str, x: float, y: float) -> tuple[Segment, float, float, float, float]:
        candidates = [s for s in self.segments.values() if not floor or not s.floor or s.floor == floor]
        if not candidates:
            raise ValueError(f"没有找到楼层 {floor or '未指定'} 的路径线段")
        best = None
        for seg in candidates:
            px, py, along, dist = project_to_segment(x, y, seg)
            item = (dist, seg, px, py, along)
            if best is None or item[0] < best[0]:
                best = item
        assert best is not None
        dist, seg, px, py, along = best
        return seg, px, py, along, dist

    def add_attachment(self, attach: AttachPoint) -> tuple[str, str]:
        seg = self.segments[attach.segment_idx]
        terminal_node = self._new_node(attach.floor, attach.x, attach.y, attach.name, attach.kind)
        if attach.along_units <= self.snap_tol:
            projection_node = self.segment_points[seg.idx][0][1]
        elif (seg.length_units - attach.along_units) <= self.snap_tol:
            projection_node = self.segment_points[seg.idx][1][1]
        else:
            projection_node = self._new_node(
                attach.floor,
                attach.projection_x,
                attach.projection_y,
                f"{attach.name}-接入点",
                "projection",
            )
            self.segment_points[seg.idx].append((attach.along_units, projection_node))
        self._add_edge(
            terminal_node,
            projection_node,
            attach.distance_units / self.unit,
            {"kind": "connector", "name": attach.name, "attach_kind": attach.kind},
        )
        attach.node_id = terminal_node
        attach.projection_node_id = projection_node
        return terminal_node, projection_node

    def finalize_route_edges(self) -> None:
        for seg_idx, points in self.segment_points.items():
            seg = self.segments[seg_idx]
            ordered = sorted(points, key=lambda item: item[0])
            for (a_dist, a_node), (b_dist, b_node) in zip(ordered, ordered[1:]):
                length_m = abs(b_dist - a_dist) / self.unit
                if a_node == b_node:
                    continue
                self._add_edge(
                    a_node,
                    b_node,
                    length_m,
                    {
                        "kind": "route",
                        "segment_idx": seg_idx,
                        "route_id": seg.route_id,
                        "layer": seg.layer,
                        "section_no": seg.section_no,
                    },
                )

    def add_vertical_edge(self, a_node: str, b_node: str, height_m: float, shaft_id: str) -> None:
        self._add_edge(a_node, b_node, height_m, {"kind": "shaft", "shaft_id": shaft_id})

    def _add_edge(self, a: str, b: str, length_m: float, meta: dict[str, Any]) -> None:
        self.graph[a].append((b, length_m, meta))
        self.graph[b].append((a, length_m, meta))

    def shortest_path(self, start: str, end: str) -> tuple[float, list[str], list[dict[str, Any]]]:
        queue: list[tuple[float, str]] = [(0.0, start)]
        dist: dict[str, float] = {start: 0.0}
        prev: dict[str, tuple[str, dict[str, Any]]] = {}
        while queue:
            cur_dist, node = heapq.heappop(queue)
            if node == end:
                break
            if cur_dist != dist.get(node):
                continue
            for nxt, length_m, meta in self.graph.get(node, []):
                cand = cur_dist + length_m
                if cand < dist.get(nxt, math.inf):
                    dist[nxt] = cand
                    prev[nxt] = (node, {"from": node, "to": nxt, "length_m": length_m, **meta})
                    heapq.heappush(queue, (cand, nxt))
        if end not in dist:
            raise ValueError("固定路径网络不连通")
        path_nodes = [end]
        edge_metas: list[dict[str, Any]] = []
        cur = end
        while cur != start:
            pre, meta = prev[cur]
            edge_metas.append(meta)
            path_nodes.append(pre)
            cur = pre
        path_nodes.reverse()
        edge_metas.reverse()
        return dist[end], path_nodes, edge_metas


def project_to_segment(x: float, y: float, seg: Segment) -> tuple[float, float, float, float]:
    dx = seg.bx - seg.ax
    dy = seg.by - seg.ay
    chord2 = dx * dx + dy * dy
    if chord2 <= 0:
        return seg.ax, seg.ay, 0.0, math.hypot(x - seg.ax, y - seg.ay)
    t = ((x - seg.ax) * dx + (y - seg.ay) * dy) / chord2
    t = max(0.0, min(1.0, t))
    px = seg.ax + t * dx
    py = seg.ay + t * dy
    along = t * seg.length_units
    dist = math.hypot(x - px, y - py)
    return px, py, along, dist


def make_attachment(
    graph: RouteGraph,
    kind: str,
    name: str,
    floor: str,
    x: float,
    y: float,
    max_distance: float,
) -> AttachPoint:
    seg, px, py, along, dist = graph.nearest_segment(floor, x, y)
    if dist > max_distance:
        raise ValueError(
            f"{name} 到最近路径距离 {dist:.1f} 超过最大接入距离 {max_distance:.1f}，请检查楼层或路径"
        )
    return AttachPoint(
        kind=kind,
        name=name,
        floor=seg.floor or floor,
        x=x,
        y=y,
        segment_idx=seg.idx,
        along_units=along,
        distance_units=dist,
        projection_x=px,
        projection_y=py,
    )


def build_graph(
    segments: list[Segment],
    cabinets: dict[str, Cabinet],
    shafts: list[Shaft],
    required_names: list[str],
    params: dict[str, float],
) -> tuple[RouteGraph | None, dict[str, str], list[str]]:
    if not segments:
        return None, {}, ["缺少路径线段数据：请先从CAD导出 路径线段.csv"]
    graph = RouteGraph(segments, params)
    issues: list[str] = []
    # 参数体检：参数和图纸数据明显不匹配时给出具体建议值
    min_len = min(seg.length_units for seg in segments)
    snap_tol = params["吸附容差"]
    if snap_tol >= min_len / 2:
        issues.append(
            f"参数提示：吸附容差 {snap_tol:g} 相对最短路径线段 {min_len:.1f} 过大，"
            f"会把短线段两端焊成一点或误连相邻线，建议在 data\\参数.csv 改成 {round(max(min_len / 10, 0.001), 3):g} 左右"
        )
    total_m = sum(seg.length_m for seg in segments)
    if len(segments) >= 5 and total_m < 10:
        issues.append(
            f"参数提示：全部路径折算后总长仅 {total_m:.2f} 米，明显偏短，"
            f"CAD每米单位（当前 {params['CAD每米单位']:g}）可能填大了：米图填1、厘米图填100、毫米图填1000"
        )
    elif len(segments) >= 5 and total_m > 100000:
        issues.append(
            f"参数提示：全部路径折算后总长达 {total_m:.0f} 米，明显偏长，"
            f"CAD每米单位（当前 {params['CAD每米单位']:g}）可能填小了：米图填1、厘米图填100、毫米图填1000"
        )
    t_count = graph.connect_t_junctions()
    if t_count:
        issues.append(f"提示：已自动连通 T 型连接 {t_count} 处（端点落在其他线段中间且在吸附容差内）")
    cross_count = graph.connect_cross_junctions()
    if cross_count:
        issues.append(f"提示：已自动连通十字/交叉点 {cross_count} 处（线段内部交叉点自动打通）")
    cabinet_nodes: dict[str, str] = {}
    max_distance = params["最大接入距离"]
    attach_fail_dists: list[float] = []
    for name in required_names:
        cabinet = cabinets.get(name)
        if not cabinet:
            issues.append(f"清册柜子未在 柜子坐标.csv 中找到：{name}")
            continue
        if not cabinet.floor:
            issues.append(f"柜子缺少楼层信息，按全图最近路径接入：{name}")
        try:
            attach = make_attachment(graph, "cabinet", name, cabinet.floor, cabinet.x, cabinet.y, max_distance)
            node_id, _ = graph.add_attachment(attach)
            cabinet_nodes[name] = node_id
        except Exception as exc:
            issues.append(f"柜子接入失败：{name}，{exc}")
            try:
                _, _, _, _, dist = graph.nearest_segment(cabinet.floor, cabinet.x, cabinet.y)
                attach_fail_dists.append(dist)
            except Exception:
                pass
    if attach_fail_dists:
        worst = max(attach_fail_dists)
        suggest = int(worst * 1.3) + 1
        issues.append(
            f"参数提示：{len(attach_fail_dists)} 个柜子超出最大接入距离（当前 {max_distance:g}），实际最远 {worst:.1f}。"
            f"若图上路径线确实已画到柜子附近，把 data\\参数.csv 的 最大接入距离 改成 {suggest} 以上再跑；"
            f"若柜子附近本来就没画线，应在CAD里补画路径而不是调参数"
        )
    shaft_nodes_by_base: dict[str, list[tuple[str, str, float]]] = defaultdict(list)
    for shaft in shafts:
        suffix = shaft_suffix_floor(shaft.shaft_id)
        if suffix and shaft.floor and suffix != shaft.floor:
            issues.append(f"竖井编号后缀与所在楼层不一致：{shaft.shaft_id} 标注 {suffix}，实际在 {shaft.floor}，请确认CAD标注")
        try:
            attach = make_attachment(graph, "shaft", shaft.shaft_id, shaft.floor, shaft.x, shaft.y, max_distance)
            node_id, _ = graph.add_attachment(attach)
            shaft_nodes_by_base[shaft.base_id].append((shaft.floor, node_id, shaft.height_m))
        except Exception as exc:
            issues.append(f"竖井接入失败：{shaft.shaft_id}，{exc}")
    graph.finalize_route_edges()
    all_floors = {seg.floor for seg in segments if seg.floor}
    for base_id, entries in shaft_nodes_by_base.items():
        floors = {floor for floor, _, _ in entries if floor}
        if len(entries) > len(floors):
            issues.append(f"竖井 {base_id} 在同一楼层出现多个点，请检查是否重复标注")
        if len(floors) < 2 and len(all_floors) > 1:
            issues.append(f"竖井 {base_id} 只在 {('、'.join(sorted(floors)) or '未知楼层')} 出现，未跨楼层配对")
        for i, (floor_a, node_a, height_a) in enumerate(entries):
            for floor_b, node_b, height_b in entries[i + 1 :]:
                if node_a != node_b and floor_a != floor_b:
                    graph.add_vertical_edge(node_a, node_b, max(height_a, height_b), base_id)
    # 连通性检查：路径网断成多块时，把每块挂载的柜子/竖井列出来，便于在图上定位断点
    seen: set[str] = set()
    components: list[list[str]] = []
    for node_id in graph.graph:
        if node_id in seen:
            continue
        stack = [node_id]
        comp: list[str] = []
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            comp.append(cur)
            for neighbor, *_ in graph.graph.get(cur, ()):
                if neighbor not in seen:
                    stack.append(neighbor)
        components.append(comp)
    if len(components) > 1:
        components.sort(key=len, reverse=True)
        issues.append(f"固定路径网络分成 {len(components)} 块互不连通（块与块之间的线端点没有真正搭上），断块明细：")
        for idx, comp in enumerate(components, 1):
            floors = sorted({graph.nodes[n]["floor"] for n in comp if graph.nodes[n].get("floor")})
            labels = [graph.nodes[n]["label"] for n in comp if graph.nodes[n].get("kind") in ("cabinet", "shaft")]
            shown = "、".join(labels[:8]) + ("…" if len(labels) > 8 else "")
            issues.append(f"  第{idx}块（楼层 {'/'.join(floors) or '?'}，{len(comp)}个节点）挂载：{shown or '无柜子/竖井'}")
    return graph, cabinet_nodes, issues


def is_route_bent(graph: RouteGraph, edge_metas: list[dict[str, Any]]) -> bool:
    if any(meta.get("kind") == "shaft" for meta in edge_metas):
        return True
    route_edges = [meta for meta in edge_metas if meta.get("kind") == "route"]
    if not route_edges:
        return False
    vectors: list[tuple[float, float]] = []
    for meta in route_edges:
        a = graph.nodes[meta["from"]]
        b = graph.nodes[meta["to"]]
        vx = b["x"] - a["x"]
        vy = b["y"] - a["y"]
        length = math.hypot(vx, vy)
        if length > 1e-9:
            vectors.append((vx / length, vy / length))
    if len(vectors) <= 1:
        return False
    base = vectors[0]
    for vx, vy in vectors[1:]:
        cross = abs(base[0] * vy - base[1] * vx)
        dot = base[0] * vx + base[1] * vy
        if cross > 0.0872 or dot < 0:
            return True
    return False


def path_description(edge_metas: list[dict[str, Any]]) -> str:
    route_parts: list[str] = []
    shafts: list[str] = []
    for meta in edge_metas:
        if meta.get("kind") == "route":
            part = f"{meta.get('route_id', '')}:{meta.get('section_no', '')}".strip(":")
            if part and (not route_parts or route_parts[-1] != part):
                route_parts.append(part)
        elif meta.get("kind") == "shaft":
            shaft = normalize_text(meta.get("shaft_id"))
            if shaft and shaft not in shafts:
                shafts.append(shaft)
    text = " -> ".join(route_parts[:20])
    if len(route_parts) > 20:
        text += f" -> ... 共{len(route_parts)}段"
    if shafts:
        text += ("；" if text else "") + "竖井：" + "、".join(shafts)
    return text


def analyze_path_rooms(
    graph: RouteGraph,
    path_node_ids: list[str],
    edge_metas: list[dict[str, Any]],
    rooms: list[Room],
) -> dict[str, Any]:
    """判断路径是否始终包含在同一个房间内。"""
    if not rooms or not path_node_ids:
        return {"start_rooms": [], "end_rooms": [], "touched_rooms": [], "crosses_room": False}

    start_node = graph.nodes[path_node_ids[0]]
    end_node = graph.nodes[path_node_ids[-1]]
    start_rooms = rooms_for_point(start_node["floor"], start_node["x"], start_node["y"], rooms)
    end_rooms = rooms_for_point(end_node["floor"], end_node["x"], end_node["y"], rooms)
    touched: list[Room] = []
    fully_containing: list[Room] = []

    for room in rooms:
        room_touched = False
        room_contains_path = bool(edge_metas)
        for meta in edge_metas:
            a = graph.nodes[meta["from"]]
            b = graph.nodes[meta["to"]]
            if a["floor"] != room.floor or b["floor"] != room.floor:
                room_contains_path = False
                continue
            interacts, fully_inside = segment_room_relation((a["x"], a["y"]), (b["x"], b["y"]), room)
            room_touched = room_touched or interacts
            room_contains_path = room_contains_path and fully_inside
        if room_touched:
            touched.append(room)
        if room_contains_path:
            fully_containing.append(room)

    return {
        "start_rooms": start_rooms,
        "end_rooms": end_rooms,
        "touched_rooms": touched,
        "crosses_room": bool(touched) and not bool(fully_containing),
    }


def json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return normalize_text(value)


def rounded_number(value: Any, digits: int = 3) -> float | str:
    if isinstance(value, (int, float)):
        return round(float(value), digits)
    return ""


def floor_sort_key(floor: str) -> tuple[int, str]:
    match = re.search(r"\d+", floor)
    if match:
        return int(match.group()), floor
    return 999, floor


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
    from_node = visual_node(graph, meta["from"])
    to_node = visual_node(graph, meta["to"])
    edge = {
        "kind": meta.get("kind", ""),
        "from": from_node,
        "to": to_node,
        "length_m": rounded_number(meta.get("length_m")),
        "floor": from_node["floor"] if from_node["floor"] == to_node["floor"] else "跨楼层",
    }
    for key in ("route_id", "section_no", "layer", "segment_idx", "shaft_id", "name", "attach_kind"):
        if key in meta:
            edge[key] = meta.get(key)
    return edge


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


def calculate_rows(
    rows: list[dict[str, Any]],
    graph: RouteGraph | None,
    cabinet_nodes: dict[str, str],
    cabinets: dict[str, Cabinet],
    params: dict[str, float],
    rule_overrides: dict[tuple[str, str], dict[str, Any]] | None = None,
    rooms: list[Room] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rule_overrides = rule_overrides or {}
    rooms = rooms or []
    detail_rows: list[dict[str, Any]] = []
    visual_cables: list[dict[str, Any]] = []
    path_cache: dict[tuple[str, str], tuple[float, list[str], list[dict[str, Any]]]] = {}

    def cached_shortest_path(a: str, b: str) -> tuple[float, list[str], list[dict[str, Any]]]:
        assert graph is not None
        key = (a, b)
        if key not in path_cache:
            reverse = path_cache.get((b, a))
            if reverse is not None:
                dist_m, node_ids, metas = reverse
                path_cache[key] = (dist_m, list(reversed(node_ids)), list(reversed(metas)))
            else:
                path_cache[key] = graph.shortest_path(a, b)
        return path_cache[key]

    for row in rows:
        start = row["起点"]
        end = row["终点"]
        status = "OK"
        auto_value: float | str = ""
        diff: float | str = ""
        base_length: float | str = ""
        extra: float | str = ""
        rule = ""
        desc = ""
        start_room = ""
        end_room = ""
        crosses_room = False
        path_nodes: list[dict[str, Any]] = []
        path_edges: list[dict[str, Any]] = []
        try:
            if graph is None:
                raise ValueError("缺少CAD路径数据")
            if start == end:
                raise ValueError("起点与终点相同，请人工确认清册")
            if start not in cabinet_nodes:
                if start in cabinets:
                    raise ValueError(f"起点柜有坐标但未接入路径网（离路径太远或该层无路径，见问题清单）：{start}")
                raise ValueError(f"缺少起点柜坐标：{start}")
            if end not in cabinet_nodes:
                if end in cabinets:
                    raise ValueError(f"终点柜有坐标但未接入路径网（离路径太远或该层无路径，见问题清单）：{end}")
                raise ValueError(f"缺少终点柜坐标：{end}")
            dist_m, path_node_ids, edge_metas = cached_shortest_path(cabinet_nodes[start], cabinet_nodes[end])
            bent = is_route_bent(graph, edge_metas)
            room_info = analyze_path_rooms(graph, path_node_ids, edge_metas, rooms)
            start_room = "、".join(room.name for room in room_info["start_rooms"])
            end_room = "、".join(room.name for room in room_info["end_rooms"])
            crosses_room = bool(room_info["crosses_room"])
            room_extra = params.get("跨房间修正", 3.0) if crosses_room else 0.0
            override = rule_overrides.get((start, end)) or rule_overrides.get((end, start))
            if override is not None:
                auto_value = round(dist_m + float(override["extra"]) + room_extra, 1)
                extra = round(auto_value - dist_m, 3)
                rule = f"强制规则 +{override['extra']:g}"
                if override["note"]:
                    rule += f"（{override['note']}）"
            elif bent:
                multiplier = params["拐弯倍率"]
                base_extra = params["不拐弯修正"]
                auto_value = round((dist_m + base_extra) * multiplier + room_extra, 1)
                extra = round(auto_value - dist_m, 3)
                rule = f"路径拐弯/跨楼层（+{base_extra:g}）×{multiplier:g}"
            else:
                auto_value = round(dist_m + params["不拐弯修正"] + room_extra, 1)
                extra = round(auto_value - dist_m, 3)
                rule = f"路径不拐弯 +{params['不拐弯修正']:g}"
            if crosses_room:
                rule += f"；跨房间 +{room_extra:g}"
            base_length = round(dist_m, 3)
            manual = row["手工长度"]
            if isinstance(manual, (int, float)):
                diff = round(auto_value - manual, 1)
            desc = path_description(edge_metas)
            path_nodes = [visual_node(graph, node_id) for node_id in path_node_ids]
            path_edges = [visual_edge(graph, meta) for meta in edge_metas]
        except Exception as exc:
            status = str(exc)
        ceil_value: float | str = math.ceil(auto_value) if isinstance(auto_value, (int, float)) else ""
        detail = {
            "行号": row["row_no"],
            "电缆编号": row["电缆编号"],
            "起点": start,
            "终点": end,
            "起点房间": start_room,
            "终点房间": end_room,
            "跨房间": "是" if crosses_room else "否",
            "手工长度": row["手工长度"],
            "自动统计": auto_value,
            "向上取整": ceil_value,
            "差值": diff,
            "基础路径长度": base_length,
            "修正量": extra,
            "规则": rule,
            "状态": status,
            "路径说明": desc,
        }
        detail_rows.append(detail)
        visual_cables.append(
            {
                "row_no": row["row_no"],
                "cable_id": row["电缆编号"],
                "start": start,
                "end": end,
                "start_room": start_room,
                "end_room": end_room,
                "crosses_room": crosses_room,
                "manual_length": json_safe(row["手工长度"]),
                "auto_length": json_safe(auto_value),
                "ceil_length": json_safe(ceil_value),
                "diff": json_safe(diff),
                "base_length": json_safe(base_length),
                "extra": json_safe(extra),
                "rule": rule,
                "status": status,
                "description": desc,
                "path_nodes": path_nodes,
                "path_edges": path_edges,
            }
        )
    return detail_rows, visual_cables


def remove_sheet_if_exists(wb: Any, name: str) -> None:
    if name in wb.sheetnames:
        del wb[name]


def write_sheet(wb: Any, name: str, headers: list[str], rows: list[dict[str, Any]]) -> Any:
    remove_sheet_if_exists(wb, name)
    ws = wb.create_sheet(name)
    ws.append(headers)
    for row in rows:
        ws.append([row.get(h, "") for h in headers])
    style_header(ws)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    fit_columns(ws, max_width=50)
    return ws


def style_header(ws: Any) -> None:
    fill = PatternFill("solid", fgColor="D9EAF7")
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = fill
        cell.alignment = Alignment(horizontal="center", vertical="center")


def fit_columns(ws: Any, max_width: int = 40) -> None:
    for col in range(1, ws.max_column + 1):
        letter = get_column_letter(col)
        width = 10
        for row in range(1, min(ws.max_row, 200) + 1):
            value = ws.cell(row, col).value
            if value is None:
                continue
            width = max(width, min(max_width, len(str(value)) * 1.4 + 2))
        ws.column_dimensions[letter].width = width


def write_results_to_workbook(
    workbook_path: Path,
    output_path: Path,
    wb: Any,
    ws: Any,
    headers: dict[str, int],
    detail_rows: list[dict[str, Any]],
    params: dict[str, float],
    cabinet_check_rows: list[dict[str, Any]],
    issues: list[str],
) -> None:
    auto_col = headers["自动统计"]
    ceil_col = headers["向上取整"]
    for detail in detail_rows:
        row_no = detail["行号"]
        value = detail["自动统计"]
        ws.cell(row_no, auto_col).value = value if isinstance(value, (int, float)) else None
        ws.cell(row_no, auto_col).number_format = "0.0"
        ceil_value = detail["向上取整"]
        ws.cell(row_no, ceil_col).value = ceil_value if isinstance(ceil_value, (int, float)) else None
        ws.cell(row_no, ceil_col).number_format = "0"

    write_sheet(wb, "统计明细", DETAIL_HEADERS, detail_rows)
    write_sheet(wb, "柜子清单", CABINET_CHECK_HEADERS, cabinet_check_rows)
    write_sheet(
        wb,
        "参数",
        ["参数", "值"],
        [{"参数": key, "值": value} for key, value in params.items()],
    )
    write_sheet(
        wb,
        "问题清单",
        ["类型", "说明"],
        [{"类型": "问题", "说明": issue} for issue in issues] or [{"类型": "状态", "说明": "未发现结构性问题"}],
    )
    fit_columns(ws)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(output_path)


def make_cabinet_check_rows(rows: list[dict[str, Any]], cabinets: dict[str, Cabinet]) -> list[dict[str, Any]]:
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        if row["起点"]:
            counts[row["起点"]] += 1
        if row["终点"]:
            counts[row["终点"]] += 1
    candidate_names = sorted(cabinets)
    result = []
    for name, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
        cab = cabinets.get(name)
        suggestion = ""
        if not cab and candidate_names:
            suggestion = "；".join(difflib.get_close_matches(name, candidate_names, n=3, cutoff=0.55))
        result.append(
            {
                "柜子名称": name,
                "清册出现次数": count,
                "已提供坐标": "是" if cab else "否",
                "楼层": cab.floor if cab else "",
                "X": cab.x if cab else "",
                "Y": cab.y if cab else "",
                "近似CAD柜名建议": suggestion,
            }
        )
    return result


VISUALIZER_HTML_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>电缆路径可视化</title>
  <style>
    :root {
      --paper: #f4f5f2;
      --panel: #ffffff;
      --ink: #17211d;
      --muted: #5d6862;
      --line: #d9ded8;
      --route: #9aa39d;
      --ok: #147d64;
      --warn: #c7791b;
      --bad: #c43f4a;
      --accent: #0b6f97;
      --shaft: #9a4fb0;
      --room: #3f7d8c;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      color: var(--ink);
      background: var(--paper);
      font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", sans-serif;
      letter-spacing: 0;
    }
    .topbar {
      height: 64px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0 20px;
      border-bottom: 1px solid var(--line);
      background: #fbfcfa;
    }
    .brand strong { display: block; font-size: 18px; line-height: 1.1; }
    .brand span { color: var(--muted); font-size: 12px; }
    .metrics { display: flex; gap: 12px; align-items: center; }
    .metric { min-width: 86px; padding: 8px 10px; border-left: 3px solid var(--accent); background: #eef3f2; }
    .metric b { display: block; font-size: 18px; line-height: 1; }
    .metric span { color: var(--muted); font-size: 11px; }
    .workspace {
      height: calc(100vh - 64px);
      display: grid;
      grid-template-columns: minmax(260px, 340px) 1fr minmax(280px, 380px);
      gap: 0;
    }
    aside, .stage { min-width: 0; min-height: 0; }
    .sidebar, .detail {
      background: var(--panel);
      border-right: 1px solid var(--line);
      display: flex;
      flex-direction: column;
    }
    .detail { border-right: 0; border-left: 1px solid var(--line); }
    .filters { padding: 14px; border-bottom: 1px solid var(--line); display: grid; gap: 10px; }
    input, select {
      width: 100%;
      border: 1px solid #cbd2cc;
      background: #fff;
      color: var(--ink);
      border-radius: 6px;
      padding: 9px 10px;
      font: inherit;
      font-size: 13px;
    }
    .list { overflow: auto; padding: 8px; }
    .cable-item {
      width: 100%;
      border: 1px solid transparent;
      border-radius: 6px;
      background: transparent;
      color: var(--ink);
      padding: 9px;
      margin: 0 0 6px;
      text-align: left;
      cursor: pointer;
      font: inherit;
    }
    .cable-item:hover { background: #f0f4f1; }
    .cable-item.active { border-color: var(--accent); background: #e8f3f6; }
    .cable-line { display: flex; justify-content: space-between; gap: 8px; font-size: 13px; font-weight: 700; }
    .cable-sub { margin-top: 4px; color: var(--muted); font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .pill { display: inline-flex; align-items: center; min-width: 42px; justify-content: center; border-radius: 999px; padding: 2px 7px; font-size: 11px; color: #fff; }
    .ok { background: var(--ok); }
    .fail { background: var(--bad); }
    .stage {
      display: grid;
      grid-template-rows: auto 1fr;
      background:
        linear-gradient(#dfe5df 1px, transparent 1px),
        linear-gradient(90deg, #dfe5df 1px, transparent 1px),
        #f9faf8;
      background-size: 28px 28px;
    }
    .toolbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 10px 14px;
      border-bottom: 1px solid var(--line);
      background: rgba(255,255,255,.92);
    }
    .segmented { display: flex; gap: 6px; flex-wrap: wrap; }
    .segmented button, .toggle {
      border: 1px solid #cbd2cc;
      border-radius: 6px;
      background: #fff;
      color: var(--ink);
      padding: 7px 9px;
      font: inherit;
      font-size: 12px;
      cursor: pointer;
    }
    .segmented button.active { background: var(--ink); color: #fff; border-color: var(--ink); }
    .toggles { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
    .toggle input { width: auto; margin: 0 5px 0 0; }
    .map-wrap { position: relative; min-height: 0; }
    svg { width: 100%; height: 100%; display: block; }
    .legend {
      position: absolute;
      left: 14px;
      bottom: 14px;
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      background: rgba(255,255,255,.9);
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 8px 10px;
      color: var(--muted);
      font-size: 12px;
    }
    .swatch { width: 22px; height: 3px; display: inline-block; vertical-align: middle; margin-right: 5px; background: var(--route); }
    .swatch.path { background: var(--ok); }
    .swatch.connector { background: var(--warn); }
    .swatch.shaft { background: var(--shaft); }
    .swatch.room { height: 10px; border: 1px solid var(--room); background: rgba(63,125,140,0.14); }
    .swatch.all { background: #c2d6e4; }
    canvas#map3d {
      position: absolute;
      inset: 0;
      width: 100%;
      height: 100%;
      display: none;
      cursor: grab;
      touch-action: none;
    }
    canvas#map3d.dragging { cursor: grabbing; }
    .tooltip-3d {
      position: absolute;
      pointer-events: none;
      z-index: 100;
      background: rgba(23,33,29,0.9);
      color: #fff;
      padding: 5px 12px;
      border-radius: 6px;
      font-size: 13px;
      white-space: nowrap;
      display: none;
      font-family: "Microsoft YaHei UI", "Microsoft YaHei", sans-serif;
      transform: translate(-50%, -100%);
      margin-top: -8px;
    }
    .tooltip-3d::after {
      content: "";
      position: absolute;
      left: 50%;
      bottom: -6px;
      transform: translateX(-50%);
      border: 6px solid transparent;
      border-top-color: rgba(23,33,29,0.9);
    }
    .stage[data-view="3d"] canvas#map3d { display: block; }
    .stage[data-view="3d"] svg { display: none; }
    .only3d { display: none !important; }
    .stage[data-view="3d"] .only3d { display: inline-flex !important; align-items: center; }
    .stage[data-view="3d"] .onlyplan { display: none !important; }
    .toolbar-left { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
    .toggle input[type="range"] { width: 90px; margin: 0 0 0 6px; }
    .detail-inner { overflow: auto; padding: 16px; }
    .detail h2 { margin: 0 0 4px; font-size: 18px; line-height: 1.25; }
    .detail .sub { color: var(--muted); font-size: 13px; margin-bottom: 14px; }
    .stat-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-bottom: 14px; }
    .stat { border: 1px solid var(--line); border-radius: 6px; padding: 9px; background: #fbfcfa; }
    .stat span { display: block; color: var(--muted); font-size: 11px; margin-bottom: 4px; }
    .stat b { font-size: 16px; }
    .rule { border-left: 3px solid var(--accent); background: #edf5f7; padding: 10px; border-radius: 6px; font-size: 13px; margin: 12px 0; }
    .issue { border-left-color: var(--bad); background: #fff1f2; }
    table { width: 100%; border-collapse: collapse; font-size: 12px; }
    th, td { border-bottom: 1px solid var(--line); padding: 7px 4px; text-align: left; vertical-align: top; }
    th { color: var(--muted); font-weight: 600; }
    .empty { color: var(--muted); padding: 24px 8px; text-align: center; }
    @media (max-width: 1080px) {
      .workspace { grid-template-columns: 300px 1fr; }
      .detail { display: none; }
      .metrics { display: none; }
    }
    @media (max-width: 760px) {
      .topbar { height: auto; align-items: flex-start; padding: 12px; }
      .workspace { height: auto; min-height: calc(100vh - 58px); grid-template-columns: 1fr; grid-template-rows: 260px 560px; }
      .sidebar { border-right: 0; border-bottom: 1px solid var(--line); }
      .toolbar { align-items: flex-start; flex-direction: column; }
    }
  </style>
</head>
<body>
  <script type="application/json" id="visual-data">__VISUAL_DATA_JSON__</script>
  <header class="topbar">
    <div class="brand">
      <span>电缆路径查看器</span>
      <strong id="title">路径可视化</strong>
    </div>
    <div class="metrics">
      <div class="metric"><b id="totalCount">0</b><span>电缆总数</span></div>
      <div class="metric"><b id="okCount">0</b><span>已计算</span></div>
      <div class="metric"><b id="failCount">0</b><span>未计算</span></div>
    </div>
  </header>
  <main class="workspace">
    <aside class="sidebar">
      <div class="filters">
        <input id="search" type="search" placeholder="搜索电缆编号、起点、终点">
        <select id="statusFilter">
          <option value="ALL">全部状态</option>
          <option value="OK">只看已计算</option>
          <option value="FAIL">只看未计算</option>
        </select>
      </div>
      <div id="cableList" class="list"></div>
    </aside>
    <section class="stage">
      <div class="toolbar">
        <div class="toolbar-left">
          <div class="segmented">
            <button type="button" id="viewPlan" class="active">平面</button>
            <button type="button" id="view3d">三维</button>
          </div>
          <div id="floorTabs" class="segmented"></div>
        </div>
        <div class="toggles">
          <label class="toggle"><input id="showNetwork" type="checkbox" checked>路径网</label>
          <label class="toggle"><input id="showLabels" type="checkbox" checked>标注</label>
          <label class="toggle only3d"><input id="showAllCables" type="checkbox" checked>全部电缆</label>
          <label class="toggle only3d">层间距<input id="zScale" type="range" min="1" max="8" step="0.5"><span id="zScaleVal"></span></label>
          <button class="toggle only3d" id="resetView" type="button">复位视角</button>
        </div>
      </div>
      <div class="map-wrap">
        <svg id="map" role="img" aria-label="电缆路径图"></svg>
        <canvas id="map3d" aria-label="电缆路径三维图"></canvas>
        <div id="tooltip3d" class="tooltip-3d"></div>
        <div class="legend">
          <span><i class="swatch"></i>固定路径</span>
          <span><i class="swatch path"></i>本条路径</span>
          <span><i class="swatch connector"></i>柜子接入</span>
          <span><i class="swatch shaft"></i>竖井</span>
          <span><i class="swatch room"></i>房间范围</span>
          <span class="only3d"><i class="swatch all"></i>全部电缆（叠加越多越深）</span>
          <span class="only3d">中键拖动平移 · Shift+中键旋转 · 左键旋转 · 右键平移 · 滚轮缩放 · 双击复位 · 点击电缆线选中 · 楼层过滤时其他层淡显作参照</span>
          <span class="onlyplan">中键拖动平移 · 滚轮缩放 · 双击自适应</span>
        </div>
      </div>
    </section>
    <aside class="detail">
      <div id="detail" class="detail-inner"></div>
    </aside>
  </main>
  <script>
    const data = JSON.parse(document.getElementById("visual-data").textContent);
    data.rooms = data.rooms || [];
    const state = {
      selected: Math.max(0, data.cables.findIndex(c => c.status === "OK")),
      floor: "ALL",
      query: "",
      status: "ALL",
      showNetwork: true,
      showLabels: true,
      showAllCables: true,
      view: location.hash === "#3d" ? "3d" : "plan",
      zScale: 1
    };
    const svg = document.getElementById("map");
    const ns = "http://www.w3.org/2000/svg";

    document.getElementById("title").textContent = data.title || "路径可视化";
    document.getElementById("totalCount").textContent = data.summary.total;
    document.getElementById("okCount").textContent = data.summary.ok;
    document.getElementById("failCount").textContent = data.summary.failed;
    document.getElementById("search").addEventListener("input", event => {
      state.query = event.target.value.trim().toLowerCase();
      renderList();
    });
    document.getElementById("statusFilter").addEventListener("change", event => {
      state.status = event.target.value;
      renderList();
    });
    document.getElementById("showNetwork").addEventListener("change", event => {
      state.showNetwork = event.target.checked;
      renderMap();
    });
    document.getElementById("showLabels").addEventListener("change", event => {
      state.showLabels = event.target.checked;
      renderMap();
    });
    document.getElementById("showAllCables").addEventListener("change", event => {
      state.showAllCables = event.target.checked;
      renderMap();
    });

    function esc(value) {
      return String(value ?? "").replace(/[&<>"']/g, ch => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
      }[ch]));
    }
    function fmt(value, suffix = "") {
      if (value === "" || value === null || value === undefined) return "未计算";
      return `${value}${suffix}`;
    }
    function isOk(cable) { return cable.status === "OK"; }
    function statusLabel(cable) { return isOk(cable) ? "OK" : "问题"; }
    function mapPoint(point) { return { x: Number(point.x), y: -Number(point.y) }; }
    function floorVisible(floor) { return state.floor === "ALL" || floor === state.floor; }
    function edgeVisible(edge) {
      if (state.floor === "ALL") return true;
      return edge.from.floor === state.floor && edge.to.floor === state.floor;
    }
    function cableText(cable) {
      return [cable.cable_id, cable.start, cable.end, cable.status].join(" ").toLowerCase();
    }
    function filteredCables() {
      return data.cables.map((cable, index) => ({ cable, index })).filter(item => {
        if (state.status === "OK" && !isOk(item.cable)) return false;
        if (state.status === "FAIL" && isOk(item.cable)) return false;
        if (state.query && !cableText(item.cable).includes(state.query)) return false;
        return true;
      });
    }
    function renderFloorTabs() {
      const root = document.getElementById("floorTabs");
      root.replaceChildren();
      ["ALL", ...data.floors].forEach(floor => {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = floor === "ALL" ? "全部" : floor;
        button.className = state.floor === floor ? "active" : "";
        button.addEventListener("click", () => {
          state.floor = floor;
          renderFloorTabs();
          renderMap();
        });
        root.appendChild(button);
      });
    }
    function renderList() {
      const root = document.getElementById("cableList");
      const items = filteredCables();
      root.replaceChildren();
      if (!items.length) {
        const empty = document.createElement("div");
        empty.className = "empty";
        empty.textContent = "没有匹配的电缆";
        root.appendChild(empty);
        return;
      }
      let activeButton = null;
      items.forEach(({ cable, index }) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = `cable-item ${index === state.selected ? "active" : ""}`;
        button.innerHTML = `
          <span class="cable-line"><span>${esc(cable.cable_id || `行 ${cable.row_no}`)}</span><span class="pill ${isOk(cable) ? "ok" : "fail"}">${statusLabel(cable)}</span></span>
          <span class="cable-sub">${esc(cable.start)} → ${esc(cable.end)}</span>
        `;
        button.addEventListener("click", () => {
          state.selected = index;
          renderList();
          renderDetail();
          renderMap();
        });
        if (index === state.selected) activeButton = button;
        root.appendChild(button);
      });
      if (activeButton) requestAnimationFrame(() => activeButton.scrollIntoView({ block: "center" }));
    }
    function collectBoundsPoints(cable) {
      const points = [];
      const visiblePathEdges = cable.path_edges.filter(edgeVisible);
      if (visiblePathEdges.length) {
        visiblePathEdges.forEach(edge => points.push(mapPoint(edge.from), mapPoint(edge.to)));
        data.cabinets.forEach(cab => {
          if ((cab.name === cable.start || cab.name === cable.end) && floorVisible(cab.floor)) points.push(mapPoint(cab));
        });
        data.shafts.forEach(shaft => {
          const used = cable.path_edges.some(edge => edge.shaft_id && edge.shaft_id === shaft.base_id);
          if (used && floorVisible(shaft.floor)) points.push(mapPoint(shaft));
        });
        return points.filter(point => Number.isFinite(point.x) && Number.isFinite(point.y));
      }
      if (state.showNetwork) {
        data.route_segments.forEach(seg => {
          if (!floorVisible(seg.floor)) return;
          points.push(mapPoint({ x: seg.ax, y: seg.ay }), mapPoint({ x: seg.bx, y: seg.by }));
        });
      }
      data.rooms.forEach(room => {
        if (!floorVisible(room.floor)) return;
        room.vertices.forEach(([x, y]) => points.push(mapPoint({ x, y })));
      });
      data.cabinets.forEach(cab => {
        if (floorVisible(cab.floor)) points.push(mapPoint(cab));
      });
      data.shafts.forEach(shaft => {
        if (floorVisible(shaft.floor)) points.push(mapPoint(shaft));
      });
      cable.path_edges.forEach(edge => {
        if (edgeVisible(edge)) points.push(mapPoint(edge.from), mapPoint(edge.to));
      });
      if (!points.length) {
        data.route_segments.forEach(seg => points.push(mapPoint({ x: seg.ax, y: seg.ay }), mapPoint({ x: seg.bx, y: seg.by })));
      }
      return points.filter(point => Number.isFinite(point.x) && Number.isFinite(point.y));
    }
    function boundsFor(cable) {
      const points = collectBoundsPoints(cable);
      if (!points.length) return { minX: 0, minY: 0, width: 1000, height: 1000, span: 1000 };
      let minX = Math.min(...points.map(p => p.x));
      let maxX = Math.max(...points.map(p => p.x));
      let minY = Math.min(...points.map(p => p.y));
      let maxY = Math.max(...points.map(p => p.y));
      const span = Math.max(maxX - minX, maxY - minY, 1000);
      const pad = span * 0.08;
      return { minX: minX - pad, minY: minY - pad, width: (maxX - minX) + pad * 2, height: (maxY - minY) + pad * 2, span };
    }
    function addLine(root, a, b, options) {
      const line = document.createElementNS(ns, "line");
      const pa = mapPoint(a);
      const pb = mapPoint(b);
      line.setAttribute("x1", pa.x);
      line.setAttribute("y1", pa.y);
      line.setAttribute("x2", pb.x);
      line.setAttribute("y2", pb.y);
      line.setAttribute("stroke", options.stroke);
      line.setAttribute("stroke-width", options.width);
      line.setAttribute("stroke-linecap", "round");
      line.setAttribute("fill", "none");
      if (options.dash) line.setAttribute("stroke-dasharray", options.dash);
      if (options.opacity) line.setAttribute("opacity", options.opacity);
      root.appendChild(line);
    }
    function addCircle(root, point, options) {
      const p = mapPoint(point);
      const circle = document.createElementNS(ns, "circle");
      circle.setAttribute("cx", p.x);
      circle.setAttribute("cy", p.y);
      circle.setAttribute("r", options.r);
      circle.setAttribute("fill", options.fill);
      circle.setAttribute("stroke", options.stroke || "#fff");
      circle.setAttribute("stroke-width", options.strokeWidth || options.r * 0.35);
      root.appendChild(circle);
    }
    function addLabel(root, point, text, options) {
      if (!state.showLabels || !text) return;
      const p = mapPoint(point);
      const label = document.createElementNS(ns, "text");
      label.setAttribute("x", p.x + options.dx);
      label.setAttribute("y", p.y + options.dy);
      label.setAttribute("font-size", options.size);
      label.setAttribute("fill", options.fill || "#17211d");
      label.setAttribute("paint-order", "stroke");
      label.setAttribute("stroke", "#fff");
      label.setAttribute("stroke-width", options.size * 0.22);
      label.textContent = text;
      root.appendChild(label);
    }
    function addRoom(root, room, lineWidth, labelSize) {
      if (!room.vertices.length) return;
      const polygon = document.createElementNS(ns, "polygon");
      polygon.setAttribute("points", room.vertices.map(([x, y]) => `${x},${-Number(y)}`).join(" "));
      polygon.setAttribute("fill", "rgba(63,125,140,0.10)");
      polygon.setAttribute("stroke", "var(--room)");
      polygon.setAttribute("stroke-width", lineWidth);
      polygon.setAttribute("stroke-dasharray", `${lineWidth * 4} ${lineWidth * 2}`);
      root.appendChild(polygon);
      const center = room.vertices.reduce((acc, point) => ({ x: acc.x + Number(point[0]) / room.vertices.length, y: acc.y + Number(point[1]) / room.vertices.length }), { x: 0, y: 0 });
      addLabel(root, center, room.name, { dx: 0, dy: 0, size: labelSize * 0.75, fill: "var(--room)" });
    }
    function renderPlan() {
      const cable = data.cables[state.selected] || data.cables[0] || { path_edges: [] };
      // 换了选中电缆或楼层就恢复自适应；纯显示开关等重绘保留用户手动调过的视口
      const sameTarget = planKeep.selected === state.selected && planKeep.floor === state.floor;
      const keepBox = planUserView && sameTarget ? svg.getAttribute("viewBox") : null;
      planKeep = { selected: state.selected, floor: state.floor };
      if (!keepBox) planUserView = false;
      const box = boundsFor(cable);
      const width = Math.max(box.width, 1000);
      const height = Math.max(box.height, 1000);
      const size = Math.max(width, height);
      const networkWidth = Math.max(size / 1200, 18);
      const pathWidth = Math.min(Math.max(size / 520, 28), 140);
      const pointR = Math.min(Math.max(size / 500, 22), 120);
      const labelSize = Math.min(Math.max(size / 220, 18), 180);
      svg.setAttribute("viewBox", keepBox || `${box.minX} ${box.minY} ${width} ${height}`);
      planBaseWidth = width;
      svg.replaceChildren();
      const base = document.createElementNS(ns, "g");
      const overlay = document.createElementNS(ns, "g");
      svg.appendChild(base);
      svg.appendChild(overlay);
      data.rooms.forEach(room => {
        if (floorVisible(room.floor)) addRoom(base, room, networkWidth * 0.9, labelSize);
      });
      if (state.showNetwork) {
        data.route_segments.forEach(seg => {
          if (!floorVisible(seg.floor)) return;
          addLine(base, { x: seg.ax, y: seg.ay }, { x: seg.bx, y: seg.by }, {
            stroke: "var(--route)", width: networkWidth, opacity: "0.55"
          });
        });
      }
      data.shafts.forEach(shaft => {
        if (!floorVisible(shaft.floor)) return;
        addCircle(base, shaft, { r: pointR * 0.72, fill: "var(--shaft)", stroke: "#fff" });
        addLabel(base, shaft, shaft.shaft_id, { dx: pointR * 1.2, dy: -pointR * 0.7, size: labelSize * 0.75, fill: "var(--shaft)" });
      });
      data.cabinets.forEach(cab => {
        if (!floorVisible(cab.floor)) return;
        const selected = cab.name === cable.start || cab.name === cable.end;
        addCircle(base, cab, { r: selected ? pointR * 1.15 : pointR * 0.55, fill: selected ? "var(--accent)" : "#69756f", stroke: "#fff" });
      });
      cable.path_edges.forEach(edge => {
        if (!edgeVisible(edge)) return;
        const stroke = edge.kind === "connector" ? "var(--warn)" : edge.kind === "shaft" ? "var(--shaft)" : "var(--ok)";
        addLine(overlay, edge.from, edge.to, {
          stroke,
          width: edge.kind === "connector" ? pathWidth * 0.72 : pathWidth,
          dash: edge.kind === "shaft" ? `${pathWidth * 2} ${pathWidth * 1.4}` : "",
          opacity: "0.95"
        });
      });
      if (isOk(cable)) {
        addLabel(overlay, cable.path_edges[0]?.from || {}, "起点", { dx: pointR * 1.1, dy: -pointR * 1.4, size: labelSize, fill: "var(--ok)" });
        const last = cable.path_edges[cable.path_edges.length - 1];
        addLabel(overlay, last?.to || {}, "终点", { dx: pointR * 1.1, dy: pointR * 2.2, size: labelSize, fill: "var(--bad)" });
      }
    }
    // ---------- 平面视图交互：中键拖动平移、滚轮缩放、双击恢复自适应（只改 viewBox，不重建图形） ----------
    let planDrag = null;
    let planUserView = false;
    let planBaseWidth = 1000;
    let planKeep = { selected: -1, floor: "ALL" };
    function wheelDeltaY(event) {
      // Firefox 等浏览器 deltaMode 可能是“行/页”而非像素，统一折算成像素
      if (event.deltaMode === 1) return event.deltaY * 33;
      if (event.deltaMode === 2) return event.deltaY * 300;
      return event.deltaY;
    }
    function planViewBox() {
      const vb = (svg.getAttribute("viewBox") || "").trim().split(/ +/).map(Number);
      return vb.length === 4 && vb.every(Number.isFinite) && vb[2] > 0 && vb[3] > 0 ? vb : null;
    }
    function planUnitsPerPixel(vb) {
      const w = svg.clientWidth;
      const h = svg.clientHeight;
      if (!w || !h) return 1;
      // preserveAspectRatio 默认 xMidYMid meet：内容按较紧的一边缩放并居中
      return Math.max(vb[2] / w, vb[3] / h);
    }
    svg.addEventListener("mousedown", event => {
      if (event.button !== 1) return;
      event.preventDefault();
      planDrag = { x: event.clientX, y: event.clientY };
    });
    window.addEventListener("mousemove", event => {
      if (!planDrag) return;
      const vb = planViewBox();
      if (!vb) return;
      const k = planUnitsPerPixel(vb);
      vb[0] -= (event.clientX - planDrag.x) * k;
      vb[1] -= (event.clientY - planDrag.y) * k;
      planDrag.x = event.clientX;
      planDrag.y = event.clientY;
      planUserView = true;
      svg.setAttribute("viewBox", vb.join(" "));
    });
    window.addEventListener("mouseup", event => {
      if (event.button === 1) planDrag = null;
    });
    svg.addEventListener("wheel", event => {
      event.preventDefault();
      const vb = planViewBox();
      if (!vb) return;
      const f = Math.exp(wheelDeltaY(event) * 0.0012);
      // 缩放限幅：防止无限缩放导致浮点精度崩溃或 viewBox 溢出后交互静默失效
      const nextW = vb[2] * f;
      if (!Number.isFinite(nextW) || nextW < planBaseWidth / 200 || nextW > planBaseWidth * 5) return;
      const rect = svg.getBoundingClientRect();
      const k = planUnitsPerPixel(vb);
      // 鼠标位置对应的用户坐标（xMidYMid：屏幕中心对应 viewBox 中心）
      const px = vb[0] + vb[2] / 2 + (event.clientX - rect.left - rect.width / 2) * k;
      const py = vb[1] + vb[3] / 2 + (event.clientY - rect.top - rect.height / 2) * k;
      vb[0] = px - (px - vb[0]) * f;
      vb[1] = py - (py - vb[1]) * f;
      vb[2] *= f;
      vb[3] *= f;
      planUserView = true;
      svg.setAttribute("viewBox", vb.join(" "));
    }, { passive: false });
    svg.addEventListener("dblclick", () => {
      planUserView = false;
      renderPlan();
    });

    // ---------- 三维视图 ----------
    const stage = document.querySelector(".stage");
    const canvas = document.getElementById("map3d");
    const ctx = canvas.getContext("2d");
    const floorElevations = data.floor_elevations || {};
    const floorOffsets = data.floor_offsets || {};
    const floorIndex = new Map(data.floors.map((floor, i) => [floor, i]));
    let lastProjector = null;

    function baseZ(floor) {
      if (Object.prototype.hasOwnProperty.call(floorElevations, floor)) return Number(floorElevations[floor]) || 0;
      return (floorIndex.get(floor) || 0) * 4500;
    }
    function zOf(floor) { return baseZ(floor) * state.zScale; }
    function wx(floor, x) { return Number(x) + (floorOffsets[floor] ? Number(floorOffsets[floor].dx) || 0 : 0); }
    function wy(floor, y) { return Number(y) + (floorOffsets[floor] ? Number(floorOffsets[floor].dy) || 0 : 0); }

    const scene = (() => {
      const xs = [];
      const ys = [];
      data.route_segments.forEach(seg => {
        xs.push(wx(seg.floor, seg.ax), wx(seg.floor, seg.bx));
        ys.push(wy(seg.floor, seg.ay), wy(seg.floor, seg.by));
      });
      data.cabinets.forEach(cab => { xs.push(wx(cab.floor, cab.x)); ys.push(wy(cab.floor, cab.y)); });
      data.shafts.forEach(shaft => { xs.push(wx(shaft.floor, shaft.x)); ys.push(wy(shaft.floor, shaft.y)); });
      data.rooms.forEach(room => room.vertices.forEach(([x, y]) => { xs.push(wx(room.floor, x)); ys.push(wy(room.floor, y)); }));
      const fx = xs.filter(Number.isFinite);
      const fy = ys.filter(Number.isFinite);
      if (!fx.length || !fy.length) return { minX: 0, maxX: 1000, minY: 0, maxY: 1000 };
      return { minX: Math.min(...fx), maxX: Math.max(...fx), minY: Math.min(...fy), maxY: Math.max(...fy) };
    })();
    const zTop = Math.max(...data.floors.map(baseZ), 0);
    const spanXY = Math.max(scene.maxX - scene.minX, scene.maxY - scene.minY, 1);

    state.zScale = (() => {
      if (!zTop) return 1;
      const raw = 0.28 * spanXY / zTop;
      return Math.min(8, Math.max(1, Math.round(raw * 2) / 2));
    })();
    const zScaleInput = document.getElementById("zScale");
    const zScaleVal = document.getElementById("zScaleVal");
    function syncZScaleLabel() {
      zScaleVal.textContent = state.zScale === 1 ? "1×(真实)" : `${state.zScale}×`;
    }
    zScaleInput.value = state.zScale;
    syncZScaleLabel();

    const camera = {};
    function resetCamera() {
      camera.yaw = -0.5;
      camera.pitch = 1.05;
      camera.zoom = 1;
      camera.tx = (scene.minX + scene.maxX) / 2;
      camera.ty = (scene.minY + scene.maxY) / 2;
      camera.tz = zTop * state.zScale / 2;
      // 屏幕像素偏移：目标点默认投影在画布中心，平移/换支点时只挪这个偏移，避免视角跳变
      camera.ox = 0;
      camera.oy = 0;
    }
    resetCamera();

    function sceneRadius() {
      const dz = zTop * state.zScale;
      return Math.max(Math.hypot(scene.maxX - scene.minX, scene.maxY - scene.minY, dz) / 2, 1);
    }
    function viewScale(width, height) {
      return Math.min(width, height) * 0.42 / sceneRadius() * camera.zoom;
    }
    function makeProjector(width, height) {
      const sy = Math.sin(camera.yaw);
      const cy = Math.cos(camera.yaw);
      const sp = Math.sin(camera.pitch);
      const cp = Math.cos(camera.pitch);
      const radius = sceneRadius();
      const fl = radius * 4;
      const scale = viewScale(width, height);
      const cx = width / 2 + camera.ox;
      const cyc = height / 2 + camera.oy;
      return function project(x, y, z) {
        const px = x - camera.tx;
        const py = y - camera.ty;
        const pz = z - camera.tz;
        const x1 = px * cy - py * sy;
        const y1 = px * sy + py * cy;
        const depth = y1 * cp - pz * sp;
        const w = fl / Math.max(fl + depth, radius * 0.2);
        return { x: cx + x1 * scale * w, y: cyc - (y1 * sp + pz * cp) * scale * w, depth };
      };
    }
    function floorAlpha(floor) {
      if (state.floor === "ALL" || !floor) return 1;
      return floor === state.floor ? 1 : 0.12;
    }
    function render3D() {
      const dpr = window.devicePixelRatio || 1;
      const width = canvas.clientWidth;
      const height = canvas.clientHeight;
      if (!width || !height) return;
      canvas.width = Math.round(width * dpr);
      canvas.height = Math.round(height * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, width, height);
      const project = makeProjector(width, height);
      lastProjector = project;
      const cable = data.cables[state.selected] || data.cables[0] || { path_edges: [], start: "", end: "" };
      const prims = [];
      const labels = [];
      const eps = sceneRadius() * 0.004;

      function addLine3d(ax, ay, az, bx, by, bz, style) {
        const a = project(ax, ay, az);
        const b = project(bx, by, bz);
        prims.push({ kind: "line", a, b, depth: (a.depth + b.depth) / 2 + (style.bias || 0), style });
      }
      function addPoint3d(x, y, z, style) {
        const p = project(x, y, z);
        prims.push({ kind: "point", p, depth: p.depth + (style.bias || 0), style });
      }
      function addPoly3d(points, style) {
        const projected = points.map(pt => project(pt[0], pt[1], pt[2]));
        const depth = projected.reduce((sum, p) => sum + p.depth, 0) / projected.length + (style.bias || 0);
        prims.push({ kind: "poly", points: projected, depth, style });
      }
      function addLabel3d(x, y, z, text, style) {
        if (!state.showLabels || !text) return;
        labels.push({ p: project(x, y, z), text, style });
      }

      // 楼层板（统一外框，叠成楼层盒子）
      const padX = (scene.maxX - scene.minX) * 0.04 + 1;
      const padY = (scene.maxY - scene.minY) * 0.04 + 1;
      const x0 = scene.minX - padX;
      const x1 = scene.maxX + padX;
      const y0 = scene.minY - padY;
      const y1 = scene.maxY + padY;
      const plateCorners = floor => {
        const z = zOf(floor);
        return [[x0, y0, z], [x1, y0, z], [x1, y1, z], [x0, y1, z]];
      };
      data.floors.forEach((floor, fi) => {
        const alpha = floorAlpha(floor);
        addPoly3d(plateCorners(floor), {
          fill: `rgba(120,144,134,${0.07 * alpha})`,
          stroke: `rgba(125,140,132,${0.55 * alpha})`,
          width: 1,
          bias: eps * 4
        });
        // 楼层标签按层错开放在不同角，避免近俯视时叠在一起
        const corner = plateCorners(floor)[fi % 2 === 0 ? 0 : 3];
        addLabel3d(corner[0], corner[1], corner[2], floor, { size: 14, fill: "#5d6862", weight: 700, dx: 8, dy: -6 });
      });
      for (let i = 0; i + 1 < data.floors.length; i++) {
        const lo = plateCorners(data.floors[i]);
        const hi = plateCorners(data.floors[i + 1]);
        for (let k = 0; k < 4; k++) {
          addLine3d(lo[k][0], lo[k][1], lo[k][2], hi[k][0], hi[k][1], hi[k][2], {
            stroke: "rgba(150,162,154,0.5)", width: 1, dash: [4, 4], bias: eps * 4
          });
        }
      }

      // 房间范围
      data.rooms.forEach(room => {
        const alpha = floorAlpha(room.floor);
        const z = zOf(room.floor) - eps * 0.4;
        const points = room.vertices.map(([x, y]) => [wx(room.floor, x), wy(room.floor, y), z]);
        if (points.length < 3) return;
        addPoly3d(points, {
          fill: `rgba(63,125,140,${0.08 * alpha})`,
          stroke: `rgba(63,125,140,${0.7 * alpha})`,
          width: 1.4,
          dash: [6, 4],
          bias: eps * 2
        });
        const center = points.reduce((acc, p) => [acc[0] + p[0] / points.length, acc[1] + p[1] / points.length, z], [0, 0, z]);
        addLabel3d(center[0], center[1], center[2], room.name, { size: 11, fill: "#3f7d8c", dx: 5, dy: -5 });
      });

      // 固定路径网
      if (state.showNetwork) {
        data.route_segments.forEach(seg => {
          const alpha = floorAlpha(seg.floor);
          const z = zOf(seg.floor);
          addLine3d(wx(seg.floor, seg.ax), wy(seg.floor, seg.ay), z, wx(seg.floor, seg.bx), wy(seg.floor, seg.by), z, {
            stroke: `rgba(122,132,125,${0.65 * alpha})`, width: 1.6
          });
        });
      }

      // 竖井：同名竖井跨楼层连成立柱
      const shaftGroups = new Map();
      data.shafts.forEach(shaft => {
        if (!shaftGroups.has(shaft.base_id)) shaftGroups.set(shaft.base_id, []);
        shaftGroups.get(shaft.base_id).push(shaft);
      });
      shaftGroups.forEach(group => {
        group.sort((a, b) => (floorIndex.get(a.floor) ?? 99) - (floorIndex.get(b.floor) ?? 99));
        group.forEach(shaft => addPoint3d(wx(shaft.floor, shaft.x), wy(shaft.floor, shaft.y), zOf(shaft.floor), {
          r: 4, fill: `rgba(154,79,176,${0.95 * floorAlpha(shaft.floor)})`, bias: -eps
        }));
        for (let i = 0; i + 1 < group.length; i++) {
          const a = group[i];
          const b = group[i + 1];
          addLine3d(wx(a.floor, a.x), wy(a.floor, a.y), zOf(a.floor), wx(b.floor, b.x), wy(b.floor, b.y), zOf(b.floor), {
            stroke: "rgba(154,79,176,0.85)", width: 3.5, dash: [7, 5]
          });
        }
        const top = group[group.length - 1];
        addLabel3d(wx(top.floor, top.x), wy(top.floor, top.y), zOf(top.floor), top.base_id, { size: 12, fill: "#9a4fb0", dx: 8, dy: -8 });
      });

      // 柜子
      data.cabinets.forEach(cab => {
        const isEnd = cab.name === cable.start || cab.name === cable.end;
        if (isEnd) {
          addPoint3d(wx(cab.floor, cab.x), wy(cab.floor, cab.y), zOf(cab.floor), {
            r: 5.5, fill: "#0b6f97", stroke: "#fff", strokeWidth: 1.5, bias: -eps * 2
          });
        } else {
          addPoint3d(wx(cab.floor, cab.x), wy(cab.floor, cab.y), zOf(cab.floor), {
            r: 2.4, fill: `rgba(105,117,111,${0.85 * floorAlpha(cab.floor)})`
          });
        }
      });

      // 全部电缆走向（半透明叠加，重叠越多颜色越深）；按样式合批，几千条边只需少量 stroke
      if (state.showAllCables) {
        const batches = new Map();
        data.cables.forEach(other => {
          if (!isOk(other) || other === cable) return;
          other.path_edges.forEach(edge => {
            const alpha = Math.max(floorAlpha(edge.from.floor), floorAlpha(edge.to.floor));
            const isShaft = edge.kind === "shaft";
            const stroke = `rgba(31,108,153,${(isShaft ? 0.3 : 0.16) * alpha})`;
            const key = stroke + (isShaft ? "|s" : "");
            let batch = batches.get(key);
            if (!batch) {
              batch = { path: new Path2D(), stroke, width: isShaft ? 2.4 : 1.2, dash: isShaft ? [6, 4] : null, depthSum: 0, n: 0 };
              batches.set(key, batch);
            }
            const a = project(wx(edge.from.floor, edge.from.x), wy(edge.from.floor, edge.from.y), zOf(edge.from.floor));
            const b = project(wx(edge.to.floor, edge.to.x), wy(edge.to.floor, edge.to.y), zOf(edge.to.floor));
            batch.path.moveTo(a.x, a.y);
            batch.path.lineTo(b.x, b.y);
            batch.depthSum += (a.depth + b.depth) / 2;
            batch.n += 1;
          });
        });
        batches.forEach(batch => prims.push({
          kind: "batch",
          path: batch.path,
          depth: batch.depthSum / batch.n - eps,
          style: { stroke: batch.stroke, width: batch.width, dash: batch.dash }
        }));
      }

      // 当前选中电缆（白色衬底 + 按类型着色）
      cable.path_edges.forEach(edge => {
        const fx = wx(edge.from.floor, edge.from.x);
        const fy = wy(edge.from.floor, edge.from.y);
        const fz = zOf(edge.from.floor);
        const tx = wx(edge.to.floor, edge.to.x);
        const ty = wy(edge.to.floor, edge.to.y);
        const tz = zOf(edge.to.floor);
        addLine3d(fx, fy, fz, tx, ty, tz, { stroke: "rgba(255,255,255,0.9)", width: 6.5, bias: -eps * 2 });
        const stroke = edge.kind === "connector" ? "#c7791b" : edge.kind === "shaft" ? "#9a4fb0" : "#147d64";
        addLine3d(fx, fy, fz, tx, ty, tz, {
          stroke, width: 3.2, dash: edge.kind === "shaft" ? [8, 6] : null, bias: -eps * 3
        });
      });
      if (isOk(cable) && cable.path_edges.length) {
        const first = cable.path_edges[0].from;
        const last = cable.path_edges[cable.path_edges.length - 1].to;
        addLabel3d(wx(first.floor, first.x), wy(first.floor, first.y), zOf(first.floor), "起点", { size: 13, fill: "#147d64", weight: 700, dx: 8, dy: -8 });
        addLabel3d(wx(last.floor, last.x), wy(last.floor, last.y), zOf(last.floor), "终点", { size: 13, fill: "#c43f4a", weight: 700, dx: 8, dy: 16 });
      }

      prims.sort((a, b) => b.depth - a.depth);
      prims.forEach(prim => {
        if (prim.kind === "batch") {
          ctx.strokeStyle = prim.style.stroke;
          ctx.lineWidth = prim.style.width;
          ctx.lineCap = "round";
          ctx.setLineDash(prim.style.dash || []);
          ctx.stroke(prim.path);
          ctx.setLineDash([]);
        } else if (prim.kind === "poly") {
          ctx.beginPath();
          prim.points.forEach((p, i) => (i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y)));
          ctx.closePath();
          if (prim.style.fill) { ctx.fillStyle = prim.style.fill; ctx.fill(); }
          if (prim.style.stroke) {
            ctx.strokeStyle = prim.style.stroke;
            ctx.lineWidth = prim.style.width || 1;
            ctx.setLineDash(prim.style.dash || []);
            ctx.stroke();
            ctx.setLineDash([]);
          }
        } else if (prim.kind === "line") {
          ctx.beginPath();
          ctx.moveTo(prim.a.x, prim.a.y);
          ctx.lineTo(prim.b.x, prim.b.y);
          ctx.strokeStyle = prim.style.stroke;
          ctx.lineWidth = prim.style.width;
          ctx.lineCap = "round";
          ctx.setLineDash(prim.style.dash || []);
          ctx.stroke();
          ctx.setLineDash([]);
        } else {
          ctx.beginPath();
          ctx.arc(prim.p.x, prim.p.y, prim.style.r, 0, Math.PI * 2);
          ctx.fillStyle = prim.style.fill;
          ctx.fill();
          if (prim.style.stroke) {
            ctx.strokeStyle = prim.style.stroke;
            ctx.lineWidth = prim.style.strokeWidth || 1;
            ctx.stroke();
          }
        }
      });
      const placed = [];
      labels.forEach(item => {
        const dx = item.style.dx || 0;
        let dy = item.style.dy || 0;
        const size = item.style.size;
        ctx.font = `${item.style.weight || 400} ${size}px "Microsoft YaHei", sans-serif`;
        const w = ctx.measureText(item.text).width;
        // 简单标签避让：与已放置标签重叠时向下错开
        for (let tries = 0; tries < 4; tries++) {
          const lx = item.p.x + dx;
          const ly = item.p.y + dy;
          const hit = placed.some(b => lx < b.x + b.w && lx + w > b.x && ly - size < b.y && ly > b.y - b.h);
          if (!hit) break;
          dy += size + 3;
        }
        const lx = item.p.x + dx;
        const ly = item.p.y + dy;
        placed.push({ x: lx, y: ly, w, h: size });
        ctx.strokeStyle = "rgba(255,255,255,0.9)";
        ctx.lineWidth = 3;
        ctx.strokeText(item.text, lx, ly);
        ctx.fillStyle = item.style.fill || "#17211d";
        ctx.fillText(item.text, lx, ly);
      });
    }

    function distToSegment2d(px, py, a, b) {
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const len2 = dx * dx + dy * dy;
      let t = len2 > 0 ? ((px - a.x) * dx + (py - a.y) * dy) / len2 : 0;
      t = Math.max(0, Math.min(1, t));
      return Math.hypot(px - (a.x + t * dx), py - (a.y + t * dy));
    }
    function nearestOnSegment2d(px, py, a, b) {
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const len2 = dx * dx + dy * dy;
      let t = len2 > 0 ? ((px - a.x) * dx + (py - a.y) * dy) / len2 : 0;
      t = Math.max(0, Math.min(1, t));
      return { x: a.x + t * dx, y: a.y + t * dy, t, dist: Math.hypot(px - (a.x + t * dx), py - (a.y + t * dy)) };
    }
    function pickCable(mx, my) {
      if (!lastProjector || !state.showAllCables) return -1;
      let best = -1;
      let bestDist = 9;
      data.cables.forEach((item, index) => {
        if (!isOk(item)) return;
        item.path_edges.forEach(edge => {
          const a = lastProjector(wx(edge.from.floor, edge.from.x), wy(edge.from.floor, edge.from.y), zOf(edge.from.floor));
          const b = lastProjector(wx(edge.to.floor, edge.to.x), wy(edge.to.floor, edge.to.y), zOf(edge.to.floor));
          const dist = distToSegment2d(mx, my, a, b);
          if (dist < bestDist) { bestDist = dist; best = index; }
        });
      });
      return best;
    }
    function panBy(dxPx, dyPx) {
      // 平移即纯屏幕偏移，任何视角下都精确跟手
      camera.ox += dxPx;
      camera.oy += dyPx;
    }

    function pickNearestPoint(mx, my, includeRoute) {
      if (!lastProjector) return null;
      let best = null;
      let bestDist = 18;
      data.cabinets.forEach(cab => {
        if (!floorVisible(cab.floor)) return;
        const p = lastProjector(wx(cab.floor, cab.x), wy(cab.floor, cab.y), zOf(cab.floor));
        const dist = Math.hypot(mx - p.x, my - p.y);
        if (dist < bestDist) {
          bestDist = dist;
          best = { type: "cabinet", name: cab.name, x: wx(cab.floor, cab.x), y: wy(cab.floor, cab.y), z: zOf(cab.floor), floor: cab.floor };
        }
      });
      data.shafts.forEach(shaft => {
        if (!floorVisible(shaft.floor)) return;
        const p = lastProjector(wx(shaft.floor, shaft.x), wy(shaft.floor, shaft.y), zOf(shaft.floor));
        const dist = Math.hypot(mx - p.x, my - p.y);
        if (dist < bestDist) {
          bestDist = dist;
          best = { type: "shaft", name: shaft.base_id, x: wx(shaft.floor, shaft.x), y: wy(shaft.floor, shaft.y), z: zOf(shaft.floor), floor: shaft.floor };
        }
      });
      // 未找到柜子/竖井时，按路径网的最近位置做支点（仅 mousedown 调用时启用）
      if (!best && includeRoute) {
        let routeBestDist = 28;
        data.route_segments.forEach(seg => {
          if (!floorVisible(seg.floor)) return;
          const z = zOf(seg.floor);
          const ax = wx(seg.floor, seg.ax);
          const ay = wy(seg.floor, seg.ay);
          const bx = wx(seg.floor, seg.bx);
          const by = wy(seg.floor, seg.by);
          const a = lastProjector(ax, ay, z);
          const b = lastProjector(bx, by, z);
          const near = nearestOnSegment2d(mx, my, a, b);
          if (near.dist < routeBestDist) {
            routeBestDist = near.dist;
            best = { type: "route", x: ax + (bx - ax) * near.t, y: ay + (by - ay) * near.t, z };
          }
        });
      }
      return best;
    }

    function unprojectAtTargetDepth(mx, my) {
      // 把画布像素点反投影到"目标点深度"所在平面上的世界坐标，作为空白处旋转的支点
      const width = canvas.clientWidth;
      const height = canvas.clientHeight;
      if (!width || !height) return null;
      const scale = viewScale(width, height);
      const vx = (mx - width / 2 - camera.ox) / scale;
      const vu = -(my - height / 2 - camera.oy) / scale;
      const sy = Math.sin(camera.yaw);
      const cy = Math.cos(camera.yaw);
      const sp = Math.sin(camera.pitch);
      const cp = Math.cos(camera.pitch);
      return {
        x: camera.tx + vx * cy + vu * sy * sp,
        y: camera.ty - vx * sy + vu * cy * sp,
        z: camera.tz + vu * cp
      };
    }
    function setRotationTarget(point) {
      if (!point || !lastProjector) return false;
      // 换支点时用屏幕偏移把它钉回原屏幕位置：旋转绕支点进行且画面不跳（CAD 式环绕）
      const before = lastProjector(point.x, point.y, point.z);
      // 支点深度平面的透视因子在换支点后变为 1，用 zoom 同步补偿，支点邻域尺寸保持不变
      const radius = sceneRadius();
      camera.zoom *= (radius * 4) / Math.max(radius * 4 + before.depth, radius * 0.2);
      camera.tx = point.x;
      camera.ty = point.y;
      camera.tz = point.z;
      camera.ox = before.x - canvas.clientWidth / 2;
      camera.oy = before.y - canvas.clientHeight / 2;
      return true;
    }
    window.__cable3dDebug = {
      camera: () => ({ yaw: camera.yaw, pitch: camera.pitch, zoom: camera.zoom, tx: camera.tx, ty: camera.ty, tz: camera.tz, ox: camera.ox, oy: camera.oy }),
      pickNearestPoint: (mx, my, includeRoute) => pickNearestPoint(mx, my, includeRoute),
      projectWorld: (x, y, z) => (lastProjector ? lastProjector(x, y, z) : null)
    };

    const tooltip3d = document.getElementById("tooltip3d");
    function showTooltip(event, point) {
      const rect = canvas.getBoundingClientRect();
      if (!point) { tooltip3d.style.display = "none"; return; }
      const label = point.type === "cabinet" ? point.name : `竖井：${point.name}${point.floor ? "（" + point.floor + "）" : ""}`;
      tooltip3d.textContent = label;
      tooltip3d.style.left = (event.clientX - rect.left) + "px";
      tooltip3d.style.top = (event.clientY - rect.top) + "px";
      tooltip3d.style.display = "block";
    }
    canvas.addEventListener("mousemove", event => {
      if (drag) { tooltip3d.style.display = "none"; return; }
      const rect = canvas.getBoundingClientRect();
      const mx = event.clientX - rect.left;
      const my = event.clientY - rect.top;
      const point = pickNearestPoint(mx, my);
      showTooltip(event, point);
    });
    canvas.addEventListener("mouseleave", () => { tooltip3d.style.display = "none"; });

    let drag = null;
    let pickTimer = null;
    let lastPick = null;
    let suppressContextMenu = false;
    canvas.addEventListener("mousedown", event => {
      if (event.button !== 0 && event.button !== 1 && event.button !== 2) return;
      event.preventDefault();
      clearTimeout(pickTimer);
      const rect = canvas.getBoundingClientRect();
      const mx = event.clientX - rect.left;
      const my = event.clientY - rect.top;
      // CAD 习惯：中键拖动平移、Shift+中键旋转；同时保留左键旋转、右键/Shift+左键平移
      const rotate = event.button === 1 ? event.shiftKey : (event.button === 0 && !event.shiftKey);
      // 旋转支点：优先吸附鼠标下的柜子/竖井/路径点，空白处退回目标深度平面上的鼠标点，
      // 支点会被钉在原屏幕位置，画面绕它环绕而不跳动；prevCam 供单击未拖动时恢复相机
      const prevCam = rotate ? { ...camera } : null;
      if (rotate) setRotationTarget(pickNearestPoint(mx, my, true) || unprojectAtTargetDepth(mx, my));
      drag = { x: event.clientX, y: event.clientY, sx: event.clientX, sy: event.clientY, button: event.button, rotate, moved: false, prevCam };
      canvas.classList.add("dragging");
    });
    window.addEventListener("mousemove", event => {
      if (!drag) return;
      const dx = event.clientX - drag.x;
      const dy = event.clientY - drag.y;
      // moved 按离按下点的累计位移判定，慢速拖动不会被误认成点击
      if (Math.abs(event.clientX - drag.sx) + Math.abs(event.clientY - drag.sy) > 3) drag.moved = true;
      drag.x = event.clientX;
      drag.y = event.clientY;
      if (drag.rotate) {
        // 方向遵循“抓着物体转”：向右拖动，物体朝向观察者的一面向右转（CAD 手感）
        camera.yaw += dx * 0.005;
        camera.pitch = Math.min(1.55, Math.max(0.08, camera.pitch + dy * 0.005));
      } else {
        panBy(dx, dy);
      }
      render3D();
    });
    window.addEventListener("mouseup", event => {
      if (!drag || event.button !== drag.button) return;
      const wasDrag = drag.moved;
      const button = drag.button;
      // 单击（未拖动）不应移动相机：恢复按下时为选支点所做的调整
      if (!wasDrag && drag.prevCam) {
        Object.assign(camera, drag.prevCam);
        render3D();
      }
      if (wasDrag && button === 2) suppressContextMenu = true;
      drag = null;
      canvas.classList.remove("dragging");
      if (!wasDrag && button === 0 && state.view === "3d") {
        const rect = canvas.getBoundingClientRect();
        const mx = event.clientX - rect.left;
        const my = event.clientY - rect.top;
        clearTimeout(pickTimer);
        // 延迟拾取，给 dblclick 留出取消窗口，避免双击复位时误切换选中
        pickTimer = setTimeout(() => {
          const index = pickCable(mx, my);
          if (index >= 0 && index !== state.selected) {
            lastPick = { prev: state.selected, at: performance.now() };
            state.selected = index;
            renderList();
            renderDetail();
            renderMap();
          }
        }, 250);
      }
    });
    canvas.addEventListener("wheel", event => {
      event.preventDefault();
      const rect = canvas.getBoundingClientRect();
      const oldZoom = camera.zoom;
      const next = oldZoom * Math.exp(-wheelDeltaY(event) * 0.0012);
      // 上下限 [0.2, 50]；支点透视补偿可能让 zoom 已在界外，此时不反向夹取以免跳变
      camera.zoom = next > oldZoom
        ? Math.min(next, Math.max(50, oldZoom))
        : Math.max(next, Math.min(0.2, oldZoom));
      const f = camera.zoom / oldZoom;
      // 以鼠标位置为缩放锚点：内容绕目标点缩放，屏幕偏移按同比例收缩把指针下的内容留在原处
      const ax = event.clientX - rect.left - rect.width / 2;
      const ay = event.clientY - rect.top - rect.height / 2;
      camera.ox = ax - (ax - camera.ox) * f;
      camera.oy = ay - (ay - camera.oy) * f;
      render3D();
    }, { passive: false });
    canvas.addEventListener("dblclick", () => {
      clearTimeout(pickTimer);
      // 慢双击时首击的延迟拾取可能已换选电缆：双击只应复位视角，把选中回滚
      if (lastPick && performance.now() - lastPick.at < 500) {
        state.selected = lastPick.prev;
        renderList();
        renderDetail();
      }
      lastPick = null;
      resetCamera();
      render3D();
    });
    canvas.addEventListener("contextmenu", event => event.preventDefault());
    window.addEventListener("contextmenu", event => {
      // 右键平移拖出画布后松开时，抑制落在其他元素上的原生右键菜单
      if (suppressContextMenu) {
        suppressContextMenu = false;
        event.preventDefault();
      }
    });

    zScaleInput.addEventListener("input", event => {
      const next = Number(event.target.value) || 1;
      // 目标点高度按比例跟随层间距变化（支点 z = 标高×zScale 同比缩放），不硬重置到中层
      camera.tz *= next / (state.zScale || 1);
      state.zScale = next;
      syncZScaleLabel();
      renderMap();
    });
    document.getElementById("resetView").addEventListener("click", () => { resetCamera(); render3D(); });

    function setView(view) {
      state.view = view;
      stage.dataset.view = view === "3d" ? "3d" : "plan";
      document.getElementById("viewPlan").className = view === "3d" ? "" : "active";
      document.getElementById("view3d").className = view === "3d" ? "active" : "";
      history.replaceState(null, "", view === "3d" ? "#3d" : location.pathname.split("/").pop() + location.search);
      renderMap();
    }
    document.getElementById("viewPlan").addEventListener("click", () => setView("plan"));
    document.getElementById("view3d").addEventListener("click", () => setView("3d"));

    function renderMap() {
      if (state.view === "3d") render3D();
      else renderPlan();
    }

    function edgeName(edge) {
      if (edge.kind === "connector") return `接入：${edge.name || ""}`;
      if (edge.kind === "shaft") return `竖井：${edge.shaft_id || ""}`;
      return `路径：${edge.route_id || ""}${edge.section_no ? ":" + edge.section_no : ""}`;
    }
    function renderDetail() {
      const root = document.getElementById("detail");
      const cable = data.cables[state.selected] || data.cables[0];
      if (!cable) {
        root.innerHTML = '<div class="empty">暂无数据</div>';
        return;
      }
      const issueClass = isOk(cable) ? "" : " issue";
      const steps = cable.path_edges.map((edge, index) => `
        <tr>
          <td>${index + 1}</td>
          <td>${esc(edgeName(edge))}</td>
          <td>${fmt(edge.length_m, "m")}</td>
          <td>${esc(edge.floor || "")}</td>
        </tr>
      `).join("");
      root.innerHTML = `
        <h2>${esc(cable.cable_id || `行 ${cable.row_no}`)}</h2>
        <div class="sub">${esc(cable.start)} → ${esc(cable.end)}</div>
        <div class="stat-grid">
          <div class="stat"><span>基础路径</span><b>${fmt(cable.base_length, "m")}</b></div>
          <div class="stat"><span>修正量</span><b>${fmt(cable.extra, "m")}</b></div>
          <div class="stat"><span>自动统计</span><b>${fmt(cable.auto_length, "m")}</b></div>
          <div class="stat"><span>向上取整</span><b>${fmt(cable.ceil_length, "m")}</b></div>
          <div class="stat"><span>差值</span><b>${fmt(cable.diff, "m")}</b></div>
          <div class="stat"><span>起点房间</span><b>${esc(cable.start_room || "室外")}</b></div>
          <div class="stat"><span>终点房间</span><b>${esc(cable.end_room || "室外")}</b></div>
          <div class="stat"><span>跨房间</span><b>${cable.crosses_room ? "是" : "否"}</b></div>
        </div>
        <div class="rule${issueClass}">${esc(isOk(cable) ? cable.rule || "已计算" : cable.status)}</div>
        <div class="rule">${esc(cable.description || "无路径说明")}</div>
        <table>
          <thead><tr><th>#</th><th>路径步骤</th><th>长度</th><th>楼层</th></tr></thead>
          <tbody>${steps || '<tr><td colspan="4">无可展示路径</td></tr>'}</tbody>
        </table>
      `;
    }
    renderFloorTabs();
    renderList();
    renderDetail();
    setView(state.view);
    // 平面视图靠 viewBox 自动适配窗口变化，重绘只会重置用户视口；只有 3D 需要按新尺寸重投影
    window.addEventListener("resize", () => {
      if (state.view === "3d") render3D();
    });
  </script>
</body>
</html>
"""


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
    return {
        "version": 2,
        "title": "电缆路径可视化",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_workbook": str(workbook_path),
        "output_workbook": str(output_path),
        "summary": {
            "total": len(visual_cables),
            "ok": ok_count,
            "failed": failed_count,
            "route_segments": len(segments),
            "cabinets": len(cabinets),
            "shafts": len(shafts),
            "rooms": len(rooms),
        },
        "params": params,
        "floors": floors,
        "floor_elevations": compute_floor_elevations(floors, shafts, params),
        "floor_offsets": floor_offsets,
        "route_segments": [route_segment_to_visual(seg) for seg in segments],
        "cabinets": [cabinet_to_visual(cab) for cab in sorted(cabinets.values(), key=lambda item: (floor_sort_key(item.floor), item.name))],
        "shafts": [shaft_to_visual(shaft) for shaft in sorted(shafts, key=lambda item: (floor_sort_key(item.floor), item.shaft_id))],
        "rooms": [room_to_visual(room) for room in sorted(rooms, key=lambda item: (floor_sort_key(item.floor), item.name))],
        "nodes": [visual_node(graph, node_id) for node_id in sorted(graph.nodes)] if graph else [],
        "cables": visual_cables,
        "issues": issues,
    }


def write_visualization_files(outputs_dir: Path, visual_data: dict[str, Any]) -> tuple[Path, Path]:
    outputs_dir.mkdir(parents=True, exist_ok=True)
    data_path = outputs_dir / "路径可视化数据.json"
    html_path = outputs_dir / "路径可视化.html"
    data_json = json.dumps(visual_data, ensure_ascii=False, indent=2)
    data_path.write_text(data_json, encoding="utf-8")
    html = VISUALIZER_HTML_TEMPLATE.replace("__VISUAL_DATA_JSON__", data_json.replace("</", "<\\/"))
    html_path.write_text(html, encoding="utf-8")
    return data_path, html_path


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="按CAD固定路径自动统计电缆长度")
    parser.add_argument("--workbook", required=True, type=Path, help="自动统计.xlsx 路径")
    parser.add_argument("--data-dir", required=True, type=Path, help="包含CSV数据的目录")
    parser.add_argument("--output", required=True, type=Path, help="输出xlsx路径")
    parser.add_argument("--make-cabinet-checklist", action="store_true", help="额外输出柜子清单CSV")
    args = parser.parse_args(argv)

    params = load_params(args.data_dir)
    wb, ws, headers, rows, required_names = load_workbook_rows(args.workbook)
    cabinets, cabinet_warnings = load_cabinets(args.data_dir)
    aliases, alias_warnings = load_aliases(args.data_dir)
    alias_warnings += apply_aliases(cabinets, aliases)
    rule_overrides, override_warnings = load_rule_overrides(args.data_dir)
    segments = load_segments(args.data_dir, params)
    shafts = load_shafts(args.data_dir, params)
    rooms, room_warnings = load_rooms(args.data_dir)
    graph, cabinet_nodes, graph_issues = build_graph(segments, cabinets, shafts, required_names, params)
    room_warnings += validate_room_assignments(cabinets, rooms)
    detail_rows, visual_cables = calculate_rows(rows, graph, cabinet_nodes, cabinets, params, rule_overrides, rooms)
    cabinet_check_rows = make_cabinet_check_rows(rows, cabinets)
    floor_offsets, offset_warnings = compute_floor_offsets(collect_floors(segments, cabinets, shafts), shafts)

    issues = cabinet_warnings + alias_warnings + override_warnings + room_warnings + graph_issues + offset_warnings
    failed_count = sum(1 for r in detail_rows if r["状态"] != "OK")
    if failed_count:
        issues.insert(0, f"共有 {failed_count} 条电缆未能计算，详见“统计明细”状态列")
    ok_count = len(detail_rows) - failed_count
    issues.insert(0, f"已计算 {ok_count} 条，未计算 {failed_count} 条")

    write_results_to_workbook(
        args.workbook,
        args.output,
        wb,
        ws,
        headers,
        detail_rows,
        params,
        cabinet_check_rows,
        issues,
    )

    outputs_dir = args.output.parent
    write_csv_dicts(outputs_dir / "统计明细.csv", detail_rows, DETAIL_HEADERS)
    write_csv_dicts(
        outputs_dir / "柜子清单.csv",
        cabinet_check_rows,
        CABINET_CHECK_HEADERS,
    )
    visual_data = build_visualization_data(
        args.workbook,
        args.output,
        params,
        graph,
        segments,
        cabinets,
        shafts,
        rooms,
        visual_cables,
        issues,
        floor_offsets,
    )
    visual_json_path, visual_html_path = write_visualization_files(outputs_dir, visual_data)
    if args.make_cabinet_checklist:
        checklist_path = args.data_dir / "柜子清单.csv"
        shutil.copyfile(outputs_dir / "柜子清单.csv", checklist_path)

    print(f"已输出：{args.output}")
    print(f"路径可视化页面：{visual_html_path}")
    print(f"路径可视化数据：{visual_json_path}")
    print(f"已计算 {ok_count} 条，未计算 {failed_count} 条")
    for issue in issues:
        if issue.startswith("参数提示"):
            print(issue)
    if failed_count:
        print("请查看输出工作簿的“统计明细”和“问题清单”。")


if __name__ == "__main__":
    main()
