"""长度规则：判断路径是否拐弯，按“+余量 / (+余量)×倍率 / 强制规则 / 跨房间修正”算出每条电缆的自动统计值。"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any

from .graph import PathTree, RouteGraph
from .models import Cabinet, Room
from .rooms import RoomAnalyzer
from .text import normalize_text, round_half_up

DETAIL_HEADERS = [
    "行号",
    "电缆编号",
    "型号",
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

# 规则类别：可视化页面和汇总表按它分组
RULE_STRAIGHT = "不拐弯"
RULE_BENT = "拐弯/跨楼层"
RULE_OVERRIDE = "强制规则"


def is_route_bent(graph: RouteGraph, edge_metas: list[dict[str, Any]], angle_deg: float | None = None) -> bool:
    """经过竖井，或路径方向相对第一段偏转超过“拐弯判定角度”（默认 5°）、出现折返，都算拐弯。"""
    if any(meta.get("kind") == "shaft" for meta in edge_metas):
        return True
    route_edges = [meta for meta in edge_metas if meta.get("kind") == "route"]
    if not route_edges:
        return False
    if angle_deg is None:
        angle_deg = graph.params.get("拐弯判定角度", 5.0)
    threshold = math.sin(math.radians(angle_deg))
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
        if cross > threshold or dot < 0:
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


def json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return normalize_text(value)


class PathFinder:
    """按起终点节点对缓存最短路，结果与逐对计算的旧实现完全一致：

    - 反方向已算过的直接反转复用；
    - 同一起点的全部终点事先登记，Dijkstra 从该起点只跑一次，所有终点出队即停。
    """

    def __init__(self, graph: RouteGraph, pairs: list[tuple[str, str]] | None = None):
        self.graph = graph
        self._trees: dict[str, PathTree] = {}
        self._paths: dict[tuple[str, str], tuple[float, list[str], list[dict[str, Any]]]] = {}
        self._targets: dict[str, set[str]] = defaultdict(set)
        # 按调用顺序模拟缓存命中：只有“正反向都还没算过”的节点对才需要从起点出发跑 Dijkstra
        planned: set[tuple[str, str]] = set()
        for a, b in pairs or ():
            if (a, b) in planned or (b, a) in planned:
                planned.add((a, b))
                continue
            planned.add((a, b))
            self._targets[a].add(b)

    def path(self, a: str, b: str) -> tuple[float, list[str], list[dict[str, Any]]]:
        key = (a, b)
        cached = self._paths.get(key)
        if cached is not None:
            return cached
        reverse = self._paths.get((b, a))
        if reverse is not None:
            dist_m, node_ids, metas = reverse
            reversed_metas = [{**meta, "from": meta["to"], "to": meta["from"]} for meta in reversed(metas)]
            result = (dist_m, list(reversed(node_ids)), reversed_metas)
        else:
            tree = self._trees.get(a)
            if tree is None or not self.graph.tree_covers(tree, b):
                targets = self._targets.get(a, set()) | {b}
                if tree is not None and tree["targets"]:
                    targets |= tree["targets"]
                tree = self._trees[a] = self.graph.shortest_path_tree(a, targets)
            result = self.graph.path_from_tree(tree, a, b)
        self._paths[key] = result
        return result


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
    pairs = [
        (cabinet_nodes[row["起点"]], cabinet_nodes[row["终点"]])
        for row in rows
        if row["起点"] in cabinet_nodes and row["终点"] in cabinet_nodes and row["起点"] != row["终点"]
    ]
    finder = PathFinder(graph, pairs) if graph is not None else None
    analyzer = RoomAnalyzer(graph, rooms) if graph is not None else None

    for row in rows:
        start = row["起点"]
        end = row["终点"]
        status = "OK"
        auto_value: float | str = ""
        diff: float | str = ""
        base_length: float | str = ""
        extra: float | str = ""
        rule = ""
        rule_kind = ""
        desc = ""
        start_room = ""
        end_room = ""
        crosses_room = False
        path_nodes: list[str] = []
        path_metas: list[dict[str, Any]] = []
        try:
            if graph is None or finder is None or analyzer is None:
                raise ValueError("缺少CAD路径数据")
            if not start:
                raise ValueError("清册起点为空")
            if not end:
                raise ValueError("清册终点为空")
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
            dist_m, path_node_ids, edge_metas = finder.path(cabinet_nodes[start], cabinet_nodes[end])
            bent = is_route_bent(graph, edge_metas)
            room_info = analyzer.analyze(path_node_ids, edge_metas)
            start_room = "、".join(room.name for room in room_info["start_rooms"])
            end_room = "、".join(room.name for room in room_info["end_rooms"])
            crosses_room = bool(room_info["crosses_room"])
            room_extra = params.get("跨房间修正", 3.0) if crosses_room else 0.0
            override = rule_overrides.get((start, end)) or rule_overrides.get((end, start))
            if override is not None:
                auto_value = round_half_up(dist_m + float(override["extra"]) + room_extra, 1)
                rule = f"强制规则 {override['extra']:+g}"
                rule_kind = RULE_OVERRIDE
                if override["note"]:
                    rule += f"（{override['note']}）"
            elif bent:
                multiplier = params["拐弯倍率"]
                base_extra = params["不拐弯修正"]
                auto_value = round_half_up((dist_m + base_extra) * multiplier + room_extra, 1)
                rule = f"路径拐弯/跨楼层（+{base_extra:g}）×{multiplier:g}"
                rule_kind = RULE_BENT
            else:
                auto_value = round_half_up(dist_m + params["不拐弯修正"] + room_extra, 1)
                rule = f"路径不拐弯 +{params['不拐弯修正']:g}"
                rule_kind = RULE_STRAIGHT
            extra = round(auto_value - dist_m, 3)
            if crosses_room:
                rule += f"；跨房间 +{room_extra:g}"
            base_length = round(dist_m, 3)
            manual = row["手工长度"]
            if isinstance(manual, (int, float)) and not isinstance(manual, bool):
                diff = round_half_up(auto_value - manual, 1)
            desc = path_description(edge_metas)
            path_nodes = list(path_node_ids)
            path_metas = edge_metas
        except Exception as exc:
            status = str(exc)
        ceil_value: float | str = math.ceil(auto_value) if isinstance(auto_value, (int, float)) else ""
        detail = {
            "行号": row["row_no"],
            "电缆编号": row["电缆编号"],
            "型号": row.get("型号", ""),
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
                "model": row.get("型号", ""),
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
                "rule_kind": rule_kind,
                "status": status,
                "description": desc,
                "path_nodes": path_nodes,
                "path_metas": path_metas,
            }
        )
    return detail_rows, visual_cables
