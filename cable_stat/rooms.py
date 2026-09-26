"""房间判定：柜子在哪个房间、路径是否出了房间（决定是否加“跨房间修正”），以及可疑房间提示。"""

from __future__ import annotations

from typing import Any

from .geometry import point_in_polygon, polygon_bbox, segment_polygon_relation
from .models import Cabinet, Room, Segment

_BBOX_EPS = 1e-6


def segment_room_relation(a: tuple[float, float], b: tuple[float, float], room: Room) -> tuple[bool, bool]:
    """返回（与房间有正长度接触或端点在内，线段完全位于房间内）。"""
    return segment_polygon_relation(a, b, room.vertices)


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


def check_suspicious_rooms(rooms: list[Room], cabinets: dict[str, Cabinet], segments: list[Segment]) -> list[str]:
    """房间里既没有柜子、也没有路径经过，多半是误放在房间图层上的图框/表格。"""
    warnings: list[str] = []
    unique_cabinets = list({id(cab): cab for cab in cabinets.values()}.values())
    for room in rooms:
        where = f"{room.floor} {room.name}" + (f"（句柄 {room.handle}）" if room.handle else "")
        if room.name.startswith("房间_"):
            warnings.append(f"房间范围 {where} 没有名称（名字是CAD句柄自动编的），可能不是用向导“定义房间”建的，请在CAD里用 DDFD_ROOM_CHECK 核对")
        x0, y0, x1, y1 = polygon_bbox(room.vertices)
        has_cabinet = any(
            cab.floor == room.floor and x0 <= cab.x <= x1 and y0 <= cab.y <= y1
            and point_in_polygon((cab.x, cab.y), room.vertices)
            for cab in unique_cabinets
        )
        has_route = any(
            seg.floor == room.floor
            and max(seg.ax, seg.bx) >= x0 and min(seg.ax, seg.bx) <= x1
            and max(seg.ay, seg.by) >= y0 and min(seg.ay, seg.by) <= y1
            and segment_room_relation((seg.ax, seg.ay), (seg.bx, seg.by), room)[0]
            for seg in segments
        )
        if not has_cabinet and not has_route:
            warnings.append(f"房间范围 {where} 里既没有柜子也没有路径经过，可能误选了图框/表格，请在CAD里用 DDFD_ROOM_CHECK 核对")
    return warnings


class RoomAnalyzer:
    """逐条电缆判断路径与房间的关系。

    同一条路径边会被很多电缆经过，边与房间的几何关系只算一次并缓存；
    包围盒不相交的边直接判为“无接触”，不做多边形求交。
    """

    def __init__(self, graph: Any, rooms: list[Room]):
        self.graph = graph
        self.rooms = rooms
        self.bboxes = [polygon_bbox(room.vertices) for room in rooms]
        self.rooms_by_floor: dict[str, list[int]] = {}
        for index, room in enumerate(rooms):
            self.rooms_by_floor.setdefault(room.floor, []).append(index)
        self._cache: dict[tuple[str, str, int], tuple[bool, bool]] = {}
        self._point_cache: dict[str, list[Room]] = {}

    def relation(self, from_id: str, to_id: str, room_index: int) -> tuple[bool, bool]:
        key = (from_id, to_id, room_index)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        a = self.graph.nodes[from_id]
        b = self.graph.nodes[to_id]
        rx0, ry0, rx1, ry1 = self.bboxes[room_index]
        if (
            max(a["x"], b["x"]) < rx0 - _BBOX_EPS or min(a["x"], b["x"]) > rx1 + _BBOX_EPS
            or max(a["y"], b["y"]) < ry0 - _BBOX_EPS or min(a["y"], b["y"]) > ry1 + _BBOX_EPS
        ):
            result = (False, False)
        else:
            result = segment_room_relation((a["x"], a["y"]), (b["x"], b["y"]), self.rooms[room_index])
        self._cache[key] = result
        return result

    def rooms_at(self, node_id: str) -> list[Room]:
        """节点所在的房间（柜子节点被很多电缆共用，结果缓存）。"""
        cached = self._point_cache.get(node_id)
        if cached is None:
            node = self.graph.nodes[node_id]
            x, y = node["x"], node["y"]
            cached = self._point_cache[node_id] = [
                self.rooms[i]
                for i in self.rooms_by_floor.get(node["floor"], ())
                if self.bboxes[i][0] - _BBOX_EPS <= x <= self.bboxes[i][2] + _BBOX_EPS
                and self.bboxes[i][1] - _BBOX_EPS <= y <= self.bboxes[i][3] + _BBOX_EPS
                and point_in_polygon((x, y), self.rooms[i].vertices)
            ]
        return cached

    def analyze(self, path_node_ids: list[str], edge_metas: list[dict[str, Any]]) -> dict[str, Any]:
        """判断路径是否始终包含在同一个房间内。"""
        if not self.rooms or not path_node_ids:
            return {"start_rooms": [], "end_rooms": [], "touched_rooms": [], "crosses_room": False}
        nodes = self.graph.nodes
        start_rooms = self.rooms_at(path_node_ids[0])
        end_rooms = self.rooms_at(path_node_ids[-1])
        touched: list[Room] = []
        fully_containing: list[Room] = []
        # 按楼层分组路径边：房间只可能被同层的边碰到；有任何一条边不在该层，路径就不可能整条在房间里
        by_floor: dict[str, list[tuple[str, str]]] = {}
        bbox_by_floor: dict[str, list[float]] = {}
        for meta in edge_metas:
            a = nodes[meta["from"]]
            b = nodes[meta["to"]]
            if a["floor"] != b["floor"]:
                continue
            by_floor.setdefault(a["floor"], []).append((meta["from"], meta["to"]))
            box = bbox_by_floor.get(a["floor"])
            x0, x1 = min(a["x"], b["x"]), max(a["x"], b["x"])
            y0, y1 = min(a["y"], b["y"]), max(a["y"], b["y"])
            if box is None:
                bbox_by_floor[a["floor"]] = [x0, y0, x1, y1]
            else:
                box[0], box[1], box[2], box[3] = min(box[0], x0), min(box[1], y0), max(box[2], x1), max(box[3], y1)
        for floor, floor_edges in by_floor.items():
            px0, py0, px1, py1 = bbox_by_floor[floor]
            whole_path_here = len(floor_edges) == len(edge_metas)
            for index in self.rooms_by_floor.get(floor, ()):
                rx0, ry0, rx1, ry1 = self.bboxes[index]
                if px1 < rx0 - _BBOX_EPS or px0 > rx1 + _BBOX_EPS or py1 < ry0 - _BBOX_EPS or py0 > ry1 + _BBOX_EPS:
                    continue
                room_touched = False
                room_contains_path = whole_path_here
                for from_id, to_id in floor_edges:
                    if room_touched and not room_contains_path:
                        break
                    interacts, fully_inside = self.relation(from_id, to_id, index)
                    room_touched = room_touched or interacts
                    room_contains_path = room_contains_path and fully_inside
                if room_touched:
                    touched.append(self.rooms[index])
                if room_contains_path:
                    fully_containing.append(self.rooms[index])
        return {
            "start_rooms": start_rooms,
            "end_rooms": end_rooms,
            "touched_rooms": touched,
            "crosses_room": bool(touched) and not bool(fully_containing),
        }


def analyze_path_rooms(
    graph: Any,
    path_node_ids: list[str],
    edge_metas: list[dict[str, Any]],
    rooms: list[Room],
) -> dict[str, Any]:
    """判断路径是否始终包含在同一个房间内（单次调用版，批量计算请复用 RoomAnalyzer）。"""
    return RoomAnalyzer(graph, rooms).analyze(path_node_ids, edge_metas)
