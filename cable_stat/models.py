"""数据模型：路径线段、柜子、竖井、房间和柜子/竖井在路径上的接入点。"""

from __future__ import annotations

from dataclasses import dataclass


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
