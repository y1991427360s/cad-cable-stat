"""固定路径网：线段端点吸附成节点，T 型/十字交叉自动打通，柜子/竖井垂直接入，Dijkstra 求最短路。"""

from __future__ import annotations

import heapq
import math
from collections import defaultdict
from typing import Any

from .geometry import PointGrid, SegmentGrid, project_to_segment
from .loaders import shaft_suffix_floor
from .models import AttachPoint, Cabinet, Segment, Shaft

# 单源最短路树：start/dist/prev 用整数节点下标；targets 为 None 表示整棵树已跑完，否则只保证这些目标的结果
PathTree = dict[str, Any]


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
        self._snap_grids: dict[str, PointGrid] = {}
        self._segment_grids: dict[str, SegmentGrid] = {}
        self._compiled: tuple[list[str], dict[str, int], list[list[tuple[int, float, dict[str, Any]]]]] | None = None
        # 路径边信息字典按（前驱, 节点, 边）缓存复用：几千条电缆经过同一段路时不重复建字典
        self._edge_dicts: dict[tuple[int, int, int], dict[str, Any]] = {}
        # build_graph 算出的疑似断点（find_route_gaps），供问题清单和可视化使用
        self.gaps: list[dict[str, Any]] = []
        self._next_node_no = 1
        self._init_segment_endpoints()

    # ---------- 节点 ----------

    def _new_node(self, floor: str, x: float, y: float, label: str, kind: str) -> str:
        node_id = f"N{self._next_node_no}"
        self._next_node_no += 1
        self.nodes[node_id] = {"floor": floor, "x": x, "y": y, "label": label, "kind": kind}
        return node_id

    def _snap_grid(self, floor: str) -> PointGrid:
        grid = self._snap_grids.get(floor)
        if grid is None:
            grid = self._snap_grids[floor] = PointGrid(self.snap_tol)
        return grid

    def _get_snapped_node(self, floor: str, x: float, y: float, label: str = "", kind: str = "route") -> str:
        """吸附容差内已有节点就复用（取最近的），否则新建并登记为可吸附节点。"""
        grid = self._snap_grid(floor)
        found = grid.nearest_within(x, y, self.snap_tol)
        if found is not None:
            return found
        node_id = self._new_node(floor, x, y, label, kind)
        self._snap_nodes_by_floor[floor].append(node_id)
        grid.add(node_id, x, y)
        return node_id

    def _init_segment_endpoints(self) -> None:
        for seg in self.segments.values():
            a = self._get_snapped_node(seg.floor, seg.ax, seg.ay, f"{seg.route_id}-起点", "route")
            b = self._get_snapped_node(seg.floor, seg.bx, seg.by, f"{seg.route_id}-终点", "route")
            self.segment_points[seg.idx].append((0.0, a))
            self.segment_points[seg.idx].append((seg.length_units, b))

    def _segment_grid(self, floor: str) -> SegmentGrid:
        grid = self._segment_grids.get(floor)
        if grid is None:
            floor_segments = [s for s in self.segments.values() if s.floor == floor]
            grid = self._segment_grids[floor] = SegmentGrid(floor_segments, pad=self.snap_tol)
        return grid

    # ---------- 自动连通 ----------

    def connect_t_junctions(self) -> int:
        """把落在其他线段中间（吸附容差内）的路径端点接入该线段，支持T型连接。"""
        found: dict[int, list[tuple[int, float, str]]] = defaultdict(list)
        for floor, node_ids in self._snap_nodes_by_floor.items():
            grid = self._segment_grid(floor)
            for order, node_id in enumerate(node_ids):
                node = self.nodes[node_id]
                for seg in grid.near_point(node["x"], node["y"]):
                    if node_id in (self.segment_points[seg.idx][0][1], self.segment_points[seg.idx][1][1]):
                        continue
                    _, _, along, dist = project_to_segment(node["x"], node["y"], seg)
                    if dist <= self.snap_tol and self.snap_tol < along < seg.length_units - self.snap_tol:
                        found[seg.idx].append((order, along, node_id))
        count = 0
        for seg in self.segments.values():
            for _, along, node_id in sorted(found.get(seg.idx, ())):
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
            position = {seg.idx: pos for pos, seg in enumerate(seg_list)}
            pairs = sorted(
                (min(position[a], position[b]), max(position[a], position[b]))
                for a, b in self._segment_grid(floor).candidate_pairs()
            )
            for i, j in pairs:
                s1 = seg_list[i]
                s2 = seg_list[j]
                l1 = s1.length_units
                l2 = s2.length_units
                if l1 <= self.snap_tol * 2 or l2 <= self.snap_tol * 2:
                    continue
                r1x, r1y = s1.bx - s1.ax, s1.by - s1.ay
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
                    cross_node = self._get_snapped_node(floor, px, py, f"交叉点-{s1.route_id}x{s2.route_id}", "cross")
                    self.segment_points[s1.idx].append((t * l1, cross_node))
                    self.segment_points[s2.idx].append((u * l2, cross_node))
                    count += 1
        return count

    # ---------- 柜子/竖井接入 ----------

    def nearest_segment(self, floor: str, x: float, y: float) -> tuple[Segment, float, float, float, float]:
        """同层（或未标楼层的）最近路径线段；未指定楼层时在全部线段里找。"""
        present = {seg.floor for seg in self.segments.values()}
        if floor:
            floors = [f for f in (floor, "") if f in present]
        else:
            floors = sorted(present)
        best: tuple[float, int, Segment, float, float, float] | None = None
        for fl in floors:
            item = self._segment_grid(fl).nearest(x, y)
            if item is not None and (best is None or (item[0], item[1]) < (best[0], best[1])):
                best = item
        if best is None:
            raise ValueError(f"没有找到楼层 {floor or '未指定'} 的路径线段")
        dist, _, seg, px, py, along = best
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

    # ---------- 建边 ----------

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
        self._compiled = None

    # ---------- 最短路 ----------

    def _compile(self) -> tuple[list[str], dict[str, int], list[list[tuple[int, float, dict[str, Any]]]]]:
        """把邻接表换成整数下标，Dijkstra 快几倍。

        下标按节点编号的字符串顺序分配，堆里距离相同时的出队顺序与按字符串比较时一致，
        所以等长路径的取舍（进而拐弯判定）与逐对计算的旧实现完全相同。
        """
        compiled = self._compiled
        if compiled is None:
            ids = sorted(set(self.graph) | set(self.nodes))
            index = {node_id: i for i, node_id in enumerate(ids)}
            adj: list[list[tuple[int, float, dict[str, Any]]]] = [[] for _ in ids]
            for node_id, items in self.graph.items():
                adj[index[node_id]] = [(index[nxt], length_m, meta) for nxt, length_m, meta in items]
            compiled = self._compiled = (ids, index, adj)
            self._edge_dicts = {}
        return compiled

    def _dijkstra(self, start: str, targets: set[str] | None = None) -> PathTree:
        """单源最短路；给了 targets 时全部目标出队即停（已出队节点的前驱不会再变，结果与跑完整棵树相同）。"""
        ids, index, adj = self._compile()
        n = len(ids)
        dist = [math.inf] * n
        prev: list[tuple[int, float, dict[str, Any]] | None] = [None] * n
        s = index[start]
        dist[s] = 0.0
        is_target: list[bool] | None = None
        remaining = 0
        if targets:
            is_target = [False] * n
            for t in targets:
                i = index.get(t)
                if i is not None and not is_target[i]:
                    is_target[i] = True
                    remaining += 1
        queue: list[tuple[float, int]] = [(0.0, s)]
        heappop = heapq.heappop
        heappush = heapq.heappush
        while queue:
            cur_dist, node = heappop(queue)
            if cur_dist != dist[node]:
                continue
            if is_target is not None and is_target[node]:
                remaining -= 1
                if remaining == 0:
                    break
            for nxt, length_m, meta in adj[node]:
                cand = cur_dist + length_m
                if cand < dist[nxt]:
                    dist[nxt] = cand
                    prev[nxt] = (node, length_m, meta)
                    heappush(queue, (cand, nxt))
        return {"start": s, "dist": dist, "prev": prev, "targets": None if targets is None else set(targets)}

    def shortest_path_tree(self, start: str, targets: set[str] | None = None) -> PathTree:
        """从 start 出发的最短路树；同一起点的多条电缆共用一棵树。"""
        return self._dijkstra(start, targets)

    @staticmethod
    def tree_covers(tree: PathTree, end: str) -> bool:
        return tree["targets"] is None or end in tree["targets"]

    def path_from_tree(self, tree: PathTree, start: str, end: str) -> tuple[float, list[str], list[dict[str, Any]]]:
        ids, index, _ = self._compile()
        e = index.get(end)
        dist, prev = tree["dist"], tree["prev"]
        if e is None or dist[e] == math.inf:
            raise ValueError("固定路径网络不连通")
        node_ids = [end]
        edge_metas: list[dict[str, Any]] = []
        cache = self._edge_dicts
        cur = e
        s = tree["start"]
        while cur != s:
            pre, length_m, meta = prev[cur]  # type: ignore[misc]
            key = (pre, cur, id(meta))
            edge = cache.get(key)
            if edge is None:
                edge = cache[key] = {"from": ids[pre], "to": ids[cur], "length_m": length_m, **meta}
            edge_metas.append(edge)
            node_ids.append(ids[pre])
            cur = pre
        node_ids.reverse()
        edge_metas.reverse()
        return dist[e], node_ids, edge_metas

    def shortest_path(self, start: str, end: str) -> tuple[float, list[str], list[dict[str, Any]]]:
        return self.path_from_tree(self._dijkstra(start, {end}), start, end)


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


def find_components(graph: RouteGraph) -> list[list[str]]:
    """路径网的连通块（每块是一组节点编号），按节点数从多到少排列。"""
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
    components.sort(key=len, reverse=True)
    return components


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
    components = find_components(graph)
    if len(components) > 1:
        issues.append(f"固定路径网络分成 {len(components)} 块互不连通（块与块之间的线端点没有真正搭上），断块明细：")
        for idx, comp in enumerate(components, 1):
            floors = sorted({graph.nodes[n]["floor"] for n in comp if graph.nodes[n].get("floor")})
            labels = [graph.nodes[n]["label"] for n in comp if graph.nodes[n].get("kind") in ("cabinet", "shaft")]
            shown = "、".join(labels[:8]) + ("…" if len(labels) > 8 else "")
            issues.append(f"  第{idx}块（楼层 {'/'.join(floors) or '?'}，{len(comp)}个节点）挂载：{shown or '无柜子/竖井'}")
    graph.gaps = find_route_gaps(graph, components, params.get("断点提示距离", 0.0) * graph.unit)
    shown_gaps = graph.gaps[:GAP_ISSUE_LIMIT]
    issues.extend(format_gap(gap, snap_tol) for gap in shown_gaps)
    if len(graph.gaps) > len(shown_gaps):
        issues.append(f"疑似断点：另有 {len(graph.gaps) - len(shown_gaps)} 处没有逐条列出，见路径可视化里的红圈")
    return graph, cabinet_nodes, issues


# 问题清单里逐条列出的疑似断点数，其余只在可视化里画出
GAP_ISSUE_LIMIT = 20
# 可视化里最多画出的疑似断点数
GAP_LIMIT = 300


def find_route_gaps(graph: RouteGraph, components: list[list[str]], max_gap_units: float) -> list[dict[str, Any]]:
    """疑似断点：只连着一条路径边、也没有柜子/竖井接入的路径线头，离另一条路径线不到 max_gap_units 却没连上。

    画线时差一点没搭上是最常见的出错原因：断开处会让路径网分块（电缆算不出来），
    或者让电缆绕远路（长度偏大）。两边属于不同连通块的排在最前面。
    """
    if max_gap_units <= 0 or not graph.segments:
        return []
    route_degree: dict[str, int] = defaultdict(int)
    attached: set[str] = set()
    for node_id, items in graph.graph.items():
        for _, _, meta in items:
            if meta.get("kind") == "route":
                route_degree[node_id] += 1
            elif meta.get("kind") == "connector":
                attached.add(node_id)
    node_segments: dict[str, set[int]] = defaultdict(set)
    for seg_idx, points in graph.segment_points.items():
        for _, node_id in points:
            node_segments[node_id].add(seg_idx)
    component_of = {node_id: number for number, comp in enumerate(components, start=1) for node_id in comp}
    gaps: list[dict[str, Any]] = []
    reported_pairs: set[tuple[str, str]] = set()
    for floor, node_ids in graph._snap_nodes_by_floor.items():
        grid = graph._segment_grid(floor)
        for node_id in node_ids:
            if route_degree.get(node_id, 0) != 1 or node_id in attached:
                continue
            node = graph.nodes[node_id]
            best: tuple[float, int, Segment, float, float, float] | None = None
            for seg in grid.within(node["x"], node["y"], max_gap_units):
                if seg.idx in node_segments[node_id]:
                    continue
                px, py, along, dist = project_to_segment(node["x"], node["y"], seg)
                if dist <= max_gap_units and (best is None or (dist, seg.idx) < (best[0], best[1])):
                    best = (dist, seg.idx, seg, px, py, along)
            if best is None or best[0] <= 1e-9:
                continue
            dist, _, seg, px, py, along = best
            start_node = graph.segment_points[seg.idx][0][1]
            end_node = graph.segment_points[seg.idx][1][1]
            # 两根线头对线头差一点时双方会互相找到对方，只报一次
            other = start_node if along <= graph.snap_tol else end_node if seg.length_units - along <= graph.snap_tol else None
            if other is not None:
                pair = (min(node_id, other), max(node_id, other))
                if pair in reported_pairs:
                    continue
                reported_pairs.add(pair)
            own_component = component_of.get(node_id, 0)
            other_component = component_of.get(start_node, 0)
            gaps.append(
                {
                    "floor": floor,
                    "x": node["x"],
                    "y": node["y"],
                    "px": px,
                    "py": py,
                    "dist": dist,
                    "dist_m": dist / graph.unit,
                    "route_id": seg.route_id,
                    "segment_idx": seg.idx,
                    "component": own_component,
                    "other_component": other_component,
                    "bridging": own_component != other_component,
                }
            )
    gaps.sort(key=lambda gap: (not gap["bridging"], gap["dist"]))
    return gaps[:GAP_LIMIT]


def _format_units(value: float) -> str:
    return f"{value:.1f}" if abs(value) >= 1 else f"{value:.3f}"


def format_gap(gap: dict[str, Any], snap_tol: float) -> str:
    where = f"{gap['floor'] or '未知楼层'} ({_format_units(gap['x'])}, {_format_units(gap['y'])})"
    dist = _format_units(gap["dist"])
    if gap["bridging"]:
        return (
            f"疑似断点（导致断网）：{where} 的路径线头离路径 {gap['route_id']} 只差 {dist}（吸附容差 {snap_tol:g}），"
            f"两边分属第{gap['component']}块和第{gap['other_component']}块路径网。请在CAD里用端点捕捉把线头搭上"
        )
    return (
        f"疑似断点：{where} 的路径线头离路径 {gap['route_id']} 只差 {dist}，没有连上，电缆可能要绕远路；"
        f"本应相连时请在CAD里把线头搭上"
    )
