"""平面几何与空间索引：点/线段/多边形关系，以及把 O(n²) 近邻查找降到近似线性的网格索引。"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from .models import Segment

# 单条线段包围盒覆盖的网格数超过这个值时不再逐格登记，改放进“超长线段”表，每次查询都带上
_MAX_CELLS_PER_ITEM = 4096
# 远离路径的杂点可能与路径相隔上亿个格子；限制空格扫描，超过后回退到逐线段比较。
_MAX_NEAREST_RINGS = 64


def polygon_signed_area(vertices: list[tuple[float, float]]) -> float:
    return 0.5 * sum(
        ax * by - bx * ay
        for (ax, ay), (bx, by) in zip(vertices, vertices[1:] + vertices[:1])
    )


def polygon_bbox(vertices: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    xs = [x for x, _ in vertices]
    ys = [y for _, y in vertices]
    return min(xs), min(ys), max(xs), max(ys)


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


def segment_polygon_relation(
    a: tuple[float, float], b: tuple[float, float], vertices: list[tuple[float, float]]
) -> tuple[bool, bool]:
    """返回（与多边形有正长度接触或端点在内，线段完全位于多边形内）。"""
    params = segment_boundary_parameters(a, b, vertices)
    samples = [0.0, 1.0]
    samples.extend((left + right) / 2.0 for left, right in zip(params, params[1:]) if right - left > 1e-10)
    ax, ay = a
    bx, by = b
    inside = [point_in_polygon((ax + (bx - ax) * t, ay + (by - ay) * t), vertices) for t in samples]
    return any(inside), all(inside)


def project_to_segment(x: float, y: float, seg: Segment) -> tuple[float, float, float, float]:
    """点到线段的垂足：返回（垂足X, 垂足Y, 沿线距离[CAD单位], 垂距）。"""
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


class PointGrid:
    """点的均匀网格索引，用于吸附容差内的近邻查找（格子边长 = 查找半径，只需查 3×3 格）。"""

    def __init__(self, cell: float):
        self.cell = cell if cell > 0 else 1.0
        self.cells: dict[tuple[int, int], list[tuple[int, Any, float, float]]] = defaultdict(list)
        self._order = 0

    def _key(self, x: float, y: float) -> tuple[int, int]:
        return math.floor(x / self.cell), math.floor(y / self.cell)

    def add(self, item: Any, x: float, y: float) -> None:
        self.cells[self._key(x, y)].append((self._order, item, x, y))
        self._order += 1

    def nearest_within(self, x: float, y: float, radius: float) -> Any | None:
        """半径内最近的点；距离相同时取先加入的（与逐个比较的旧实现结果一致）。"""
        kx, ky = self._key(x, y)
        reach = max(1, math.ceil(radius / self.cell))
        best: tuple[float, int, Any] | None = None
        for i in range(kx - reach, kx + reach + 1):
            for j in range(ky - reach, ky + reach + 1):
                for order, item, px, py in self.cells.get((i, j), ()):
                    dist = math.hypot(x - px, y - py)
                    if dist <= radius and (best is None or (dist, order) < (best[0], best[1])):
                        best = (dist, order, item)
        return best[2] if best else None


class SegmentGrid:
    """线段的均匀网格索引：线段按（外扩 pad 后的）包围盒登记到覆盖的每个格子。

    用于：端点落在哪些线段附近（T 型连接）、哪些线段可能相交（十字连接）、离某点最近的线段（柜子接入）。
    """

    def __init__(self, segments: Iterable[Segment], pad: float = 0.0):
        self.segments = list(segments)
        self.pad = max(pad, 0.0)
        self.cells: dict[tuple[int, int], list[Segment]] = defaultdict(list)
        self.oversized: list[Segment] = []
        self.cell = self._choose_cell()
        self.bounds: tuple[int, int, int, int] | None = None
        for seg in self.segments:
            self._insert(seg)

    def _choose_cell(self) -> float:
        if not self.segments:
            return 1.0
        mean_len = sum(math.hypot(s.bx - s.ax, s.by - s.ay) for s in self.segments) / len(self.segments)
        return max(mean_len, self.pad * 2, 1e-6)

    def _key(self, x: float, y: float) -> tuple[int, int]:
        return math.floor(x / self.cell), math.floor(y / self.cell)

    def _insert(self, seg: Segment) -> None:
        x0, y0 = self._key(min(seg.ax, seg.bx) - self.pad, min(seg.ay, seg.by) - self.pad)
        x1, y1 = self._key(max(seg.ax, seg.bx) + self.pad, max(seg.ay, seg.by) + self.pad)
        if self.bounds is None:
            self.bounds = (x0, y0, x1, y1)
        else:
            bx0, by0, bx1, by1 = self.bounds
            self.bounds = (min(bx0, x0), min(by0, y0), max(bx1, x1), max(by1, y1))
        if (x1 - x0 + 1) * (y1 - y0 + 1) > _MAX_CELLS_PER_ITEM:
            self.oversized.append(seg)
            return
        for i in range(x0, x1 + 1):
            for j in range(y0, y1 + 1):
                self.cells[(i, j)].append(seg)

    def near_point(self, x: float, y: float) -> list[Segment]:
        """包围盒（含 pad）可能覆盖该点的线段；按线段原始顺序返回，不重复。"""
        found = list(self.cells.get(self._key(x, y), ()))
        if self.oversized:
            found.extend(self.oversized)
            found = list({seg.idx: seg for seg in found}.values())
        return found

    def within(self, x: float, y: float, radius: float) -> list[Segment]:
        """可能离 (x, y) 不到 radius 的线段（候选，按 idx 排序，还需精确算距离）。"""
        if not self.segments:
            return []
        x0, y0 = self._key(x - radius, y - radius)
        x1, y1 = self._key(x + radius, y + radius)
        if (x1 - x0 + 1) * (y1 - y0 + 1) > _MAX_CELLS_PER_ITEM:
            return sorted(self.segments, key=lambda seg: seg.idx)
        found: dict[int, Segment] = {seg.idx: seg for seg in self.oversized}
        for i in range(x0, x1 + 1):
            for j in range(y0, y1 + 1):
                for seg in self.cells.get((i, j), ()):
                    found[seg.idx] = seg
        return [found[idx] for idx in sorted(found)]

    def candidate_pairs(self) -> set[tuple[int, int]]:
        """可能相交的线段对（按线段 idx 从小到大）。"""
        pairs: set[tuple[int, int]] = set()
        for bucket in self.cells.values():
            n = len(bucket)
            for i in range(n):
                for j in range(i + 1, n):
                    a, b = bucket[i].idx, bucket[j].idx
                    pairs.add((a, b) if a < b else (b, a))
        for big in self.oversized:
            for seg in self.segments:
                if seg.idx != big.idx:
                    pairs.add((big.idx, seg.idx) if big.idx < seg.idx else (seg.idx, big.idx))
        return pairs

    def nearest(self, x: float, y: float) -> tuple[float, int, Segment, float, float, float] | None:
        """离点最近的线段：返回（垂距, 线段idx, 线段, 垂足X, 垂足Y, 沿线距离）；距离相同取 idx 小的。

        从点所在格子一圈圈向外找；找到的最近距离不超过已搜索半径时即可停止。
        空间跨度过大时，有限圈数后改为全量比较，避免孤立杂点造成无界空格扫描。
        """
        if not self.segments:
            return None
        best: tuple[float, int, Segment, float, float, float] | None = None

        def consider(seg: Segment) -> None:
            nonlocal best
            px, py, along, dist = project_to_segment(x, y, seg)
            if best is None or (dist, seg.idx) < (best[0], best[1]):
                best = (dist, seg.idx, seg, px, py, along)

        for seg in self.oversized:
            consider(seg)
        kx, ky = self._key(x, y)
        assert self.bounds is not None
        bx0, by0, bx1, by1 = self.bounds
        max_ring = max(abs(kx - bx0), abs(kx - bx1), abs(ky - by0), abs(ky - by1)) + 1
        seen: set[int] = {seg.idx for seg in self.oversized}
        search_limit = min(max_ring, _MAX_NEAREST_RINGS)
        for ring in range(search_limit + 1):
            for i in range(kx - ring, kx + ring + 1):
                for j in (range(ky - ring, ky + ring + 1) if i in (kx - ring, kx + ring) else (ky - ring, ky + ring)):
                    for seg in self.cells.get((i, j), ()):
                        if seg.idx not in seen:
                            seen.add(seg.idx)
                            consider(seg)
            # 第 ring 圈之外的格子离查询点至少 ring 个格宽（取严格小于，等距时再看一圈保证并列取 idx 小的）
            if best is not None and best[0] < ring * self.cell:
                return best
        # 未能用搜索半径证明最优时必须检查剩余线段，保留距离并列时取较小 idx 的规则。
        for seg in self.segments:
            if seg.idx not in seen:
                consider(seg)
        return best
