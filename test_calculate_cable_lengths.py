"""calculate_cable_lengths.py 的轻量测试。

运行方式（任选其一）：
    python -m pytest test_calculate_cable_lengths.py -q
    python test_calculate_cable_lengths.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from calculate_cable_lengths import (
    Cabinet,
    Room,
    RouteGraph,
    Segment,
    Shaft,
    apply_aliases,
    build_graph,
    calculate_rows,
    compute_floor_elevations,
    compute_floor_offsets,
    is_route_bent,
    load_aliases,
    load_rule_overrides,
    load_rooms,
    load_params,
    normalize_cabinet_name,
    normalize_shaft_base_id,
    shaft_suffix_floor,
    point_in_polygon,
    segment_room_relation,
)


PARAMS = {
    "竖井高度": 4.5,
    "不拐弯修正": 7.0,
    "拐弯倍率": 1.2,
    "CAD每米单位": 1000.0,
    "吸附容差": 10.0,
    "最大接入距离": 20000.0,
    "跨房间修正": 3.0,
}


def make_segment(idx: int, ax: float, ay: float, bx: float, by: float, floor: str = "1F") -> Segment:
    import math

    length = math.hypot(bx - ax, by - ay)
    return Segment(
        idx=idx,
        route_id=f"R{idx}",
        floor=floor,
        ax=ax,
        ay=ay,
        bx=bx,
        by=by,
        length_units=length,
        length_m=length / PARAMS["CAD每米单位"],
        layer=f"CABLE_ROUTE_{floor}",
        handle=f"H{idx}",
        section_no="1",
    )


def make_cabinet(name: str, x: float, y: float, floor: str = "1F") -> Cabinet:
    return Cabinet(name=name, floor=floor, x=x, y=y, layer=f"CABLE_CABINET_{floor}", object_type="TEXT", handle="")


def make_shaft(shaft_id: str, x: float, y: float, floor: str) -> Shaft:
    return Shaft(
        shaft_id=shaft_id,
        base_id=normalize_shaft_base_id(shaft_id),
        floor=floor,
        x=x,
        y=y,
        height_m=PARAMS["竖井高度"],
        layer=f"CABLE_SHAFT_{floor}",
        object_type="TEXT",
        handle="",
    )


def make_rows(pairs: list[tuple[str, str]]) -> list[dict]:
    return [
        {"row_no": i + 2, "电缆编号": f"C{i + 1}", "起点": a, "终点": b, "手工长度": None}
        for i, (a, b) in enumerate(pairs)
    ]


def make_room(name: str, vertices: list[tuple[float, float]], floor: str = "1F") -> Room:
    return Room(name=name, floor=floor, vertices=vertices, layer=f"CABLE_ROOM_{floor}", handle=name)


def calculate_pair_with_rooms(
    segments: list[Segment], cabinets: dict[str, Cabinet], rooms: list[Room], shafts: list[Shaft] | None = None,
    overrides=None, params=None,
):
    params = params or PARAMS
    graph, nodes, _ = build_graph(segments, cabinets, shafts or [], list(cabinets), params)
    details, visuals = calculate_rows(make_rows([("柜A", "柜B")]), graph, nodes, cabinets, params, overrides, rooms)
    return details[0], visuals[0]


def test_same_room_does_not_add_room_extra():
    segments = [make_segment(1, 0, 0, 10000, 0)]
    cabinets = {"柜A": make_cabinet("柜A", 1000, 0), "柜B": make_cabinet("柜B", 9000, 0)}
    room = make_room("配电室", [(-1000, -1000), (11000, -1000), (11000, 1000), (-1000, 1000)])
    row, visual = calculate_pair_with_rooms(segments, cabinets, [room])
    assert row["自动统计"] == 15.0, row
    assert row["跨房间"] == "否" and visual["crosses_room"] is False
    assert row["起点房间"] == "配电室" and row["终点房间"] == "配电室"


def test_different_rooms_add_room_extra_once():
    segments = [make_segment(1, 0, 0, 10000, 0)]
    cabinets = {"柜A": make_cabinet("柜A", 1000, 0), "柜B": make_cabinet("柜B", 9000, 0)}
    rooms = [
        make_room("房间A", [(-500, -1000), (4000, -1000), (4000, 1000), (-500, 1000)]),
        make_room("房间B", [(6000, -1000), (10500, -1000), (10500, 1000), (6000, 1000)]),
    ]
    row, _ = calculate_pair_with_rooms(segments, cabinets, rooms)
    assert row["自动统计"] == 18.0, row
    assert row["修正量"] == 10.0 and "跨房间 +3" in row["规则"]


def test_same_room_endpoints_but_route_leaves_room():
    segments = [
        make_segment(1, 1000, 1000, 1000, 8000),
        make_segment(2, 1000, 8000, 9000, 8000),
        make_segment(3, 9000, 8000, 9000, 1000),
    ]
    cabinets = {"柜A": make_cabinet("柜A", 1000, 1000), "柜B": make_cabinet("柜B", 9000, 1000)}
    room = make_room("控制室", [(0, 0), (10000, 0), (10000, 5000), (0, 5000)])
    row, _ = calculate_pair_with_rooms(segments, cabinets, [room])
    assert row["起点房间"] == "控制室" and row["终点房间"] == "控制室"
    assert row["跨房间"] == "是", row


def test_room_to_outside_and_outside_only():
    segments = [make_segment(1, 0, 0, 10000, 0)]
    room = make_room("房间A", [(-500, -1000), (4000, -1000), (4000, 1000), (-500, 1000)])
    cabinets = {"柜A": make_cabinet("柜A", 1000, 0), "柜B": make_cabinet("柜B", 9000, 0)}
    row, _ = calculate_pair_with_rooms(segments, cabinets, [room])
    assert row["跨房间"] == "是"
    outdoor_room = make_room("远处房间", [(20000, 20000), (21000, 20000), (21000, 21000), (20000, 21000)])
    row2, _ = calculate_pair_with_rooms(segments, cabinets, [outdoor_room])
    assert row2["跨房间"] == "否" and row2["自动统计"] == 15.0


def test_outside_path_through_room_adds_extra():
    segments = [make_segment(1, 0, 0, 10000, 0)]
    cabinets = {"柜A": make_cabinet("柜A", 500, 0), "柜B": make_cabinet("柜B", 9500, 0)}
    room = make_room("中间房间", [(4000, -1000), (6000, -1000), (6000, 1000), (4000, 1000)])
    row, _ = calculate_pair_with_rooms(segments, cabinets, [room])
    assert not row["起点房间"] and not row["终点房间"]
    assert row["跨房间"] == "是"


def test_cross_floor_rooms_add_extra():
    segments = [make_segment(1, 0, 0, 10000, 0, "1F"), make_segment(2, 0, 0, 10000, 0, "2F")]
    shafts = [make_shaft("ZJ1_1F", 5000, 0, "1F"), make_shaft("ZJ1_2F", 5000, 0, "2F")]
    cabinets = {"柜A": make_cabinet("柜A", 1000, 0, "1F"), "柜B": make_cabinet("柜B", 9000, 0, "2F")}
    bounds = [(-1000, -1000), (11000, -1000), (11000, 1000), (-1000, 1000)]
    rooms = [make_room("一层房间", bounds, "1F"), make_room("二层房间", bounds, "2F")]
    row, _ = calculate_pair_with_rooms(segments, cabinets, rooms, shafts)
    assert row["自动统计"] == 26.4, row
    assert row["跨房间"] == "是"


def test_room_extra_stacks_after_override():
    segments = [make_segment(1, 0, 0, 10000, 0)]
    cabinets = {"柜A": make_cabinet("柜A", 1000, 0), "柜B": make_cabinet("柜B", 9000, 0)}
    rooms = [make_room("房间A", [(-500, -1000), (4000, -1000), (4000, 1000), (-500, 1000)])]
    overrides = {("柜A", "柜B"): {"extra": 2.0, "note": "实测"}}
    row, _ = calculate_pair_with_rooms(segments, cabinets, rooms, overrides=overrides)
    assert row["自动统计"] == 13.0 and row["修正量"] == 5.0, row
    assert "强制规则 +2" in row["规则"] and "跨房间 +3" in row["规则"]


def test_boundary_and_concave_polygon_geometry():
    concave = make_room("凹房间", [(0, 0), (10, 0), (10, 10), (6, 10), (6, 4), (4, 4), (4, 10), (0, 10)])
    assert point_in_polygon((0, 5), concave.vertices)
    assert point_in_polygon((2, 8), concave.vertices)
    assert not point_in_polygon((5, 8), concave.vertices)
    interacts, fully_inside = segment_room_relation((2, 8), (8, 8), concave)
    assert interacts and not fully_inside


def test_load_rooms_validation_and_configurable_extra():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        (data_dir / "房间范围.csv").write_text(
            "房间名称,楼层,顶点序号,X,Y,图层,句柄\n"
            "A,1F,1,0,0,CABLE_ROOM_1F,H1\nA,1F,2,10,0,CABLE_ROOM_1F,H1\nA,1F,3,10,10,CABLE_ROOM_1F,H1\n"
            "坏房间,2F,1,0,0,CABLE_ROOM_2F,H2\n坏房间,2F,2,1,1,CABLE_ROOM_2F,H2\n",
            encoding="utf-8-sig",
        )
        (data_dir / "参数.csv").write_text("参数,值,备注\n跨房间修正,5,测试\n", encoding="utf-8-sig")
        rooms, warnings = load_rooms(data_dir)
        assert len(rooms) == 1 and rooms[0].name == "A"
        assert any("坏房间" in warning for warning in warnings)
        assert load_params(data_dir)["跨房间修正"] == 5.0


def test_app_exports_only_complete_wizard_lsp():
    import tempfile

    from cable_stat_app import export_lsp

    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "cad_export_cable_route.lsp").write_text("旧文件", encoding="utf-8")
        exported = export_lsp(Path(tmp))
        assert exported == ["cad_cable_wizard.lsp"]
        assert (Path(tmp) / "cad_cable_wizard.lsp").is_file()
        assert not (Path(tmp) / "cad_export_cable_route.lsp").exists()


def test_straight_path_plus_7():
    """直线路径：基础长度 + 不拐弯修正。"""
    segments = [make_segment(1, 0, 0, 10000, 0)]
    cabinets = {
        "柜A": make_cabinet("柜A", 1000, 500),
        "柜B": make_cabinet("柜B", 9000, 500),
    }
    graph, nodes, issues = build_graph(segments, cabinets, [], ["柜A", "柜B"], PARAMS)
    assert not [i for i in issues if not i.startswith("提示")], issues
    details, _ = calculate_rows(make_rows([("柜A", "柜B")]), graph, nodes, cabinets, PARAMS)
    row = details[0]
    assert row["状态"] == "OK"
    # 沿线 8m + 两端接入各 0.5m = 9m，+7 = 16.0
    assert row["自动统计"] == 16.0, row
    assert row["向上取整"] == 16, row
    assert "路径不拐弯" in row["规则"]


def test_cabinet_names_ignore_spaces():
    """柜名匹配忽略 CAD/Excel 名称里的空格。"""
    segments = [make_segment(1, 0, 0, 10000, 0)]
    cad_name = normalize_cabinet_name("10kV #1 主变进线柜")
    excel_name = normalize_cabinet_name("10kV#1主变进线柜")
    cabinets = {
        cad_name: make_cabinet(cad_name, 1000, 0),
        "柜B": make_cabinet("柜B", 9000, 0),
    }
    graph, nodes, issues = build_graph(segments, cabinets, [], [excel_name, "柜B"], PARAMS)
    assert not [i for i in issues if not i.startswith("提示")], issues
    details, _ = calculate_rows(make_rows([(excel_name, "柜B")]), graph, nodes, cabinets, PARAMS)
    assert details[0]["状态"] == "OK", details[0]


def test_bent_path_plus_7_then_times_1_2():
    """L形路径判定拐弯，按（CAD 测量距离 +7）×1.2。"""
    segments = [make_segment(1, 0, 0, 10000, 0), make_segment(2, 10000, 0, 10000, 10000)]
    cabinets = {
        "柜A": make_cabinet("柜A", 1000, 0),
        "柜B": make_cabinet("柜B", 10000, 9000),
    }
    graph, nodes, _ = build_graph(segments, cabinets, [], ["柜A", "柜B"], PARAMS)
    details, _ = calculate_rows(make_rows([("柜A", "柜B")]), graph, nodes, cabinets, PARAMS)
    row = details[0]
    assert row["状态"] == "OK"
    assert "拐弯" in row["规则"]
    assert "×1.2" in row["规则"]
    assert row["自动统计"] == 30.0, row  # (9m + 9m + 7) × 1.2


def test_t_junction_connects():
    """端点落在另一条线段中间时自动连通（T型连接）。"""
    # 主干线 + 一条端点搭在主干中间的支线，支线另一端接柜B
    segments = [make_segment(1, 0, 0, 20000, 0), make_segment(2, 10000, 2, 10000, 8000)]
    cabinets = {
        "柜A": make_cabinet("柜A", 1000, 0),
        "柜B": make_cabinet("柜B", 10000, 7500),
    }
    graph, nodes, issues = build_graph(segments, cabinets, [], ["柜A", "柜B"], PARAMS)
    assert any("T 型连接" in i for i in issues), issues
    details, _ = calculate_rows(make_rows([("柜A", "柜B")]), graph, nodes, cabinets, PARAMS)
    assert details[0]["状态"] == "OK", details[0]


def test_cross_junction_connects():
    """两条线段在内部十字交叉（交点不在端点），自动打通交点连通。"""
    # 水平线: (0, 5000) -> (10000, 5000)
    # 垂直线: (5000, 0) -> (5000, 10000)
    # 十字相交于 (5000, 5000)
    segments = [
        make_segment(1, 0, 5000, 10000, 5000),
        make_segment(2, 5000, 0, 5000, 10000),
    ]
    cabinets = {
        "柜A": make_cabinet("柜A", 1000, 5000),
        "柜B": make_cabinet("柜B", 5000, 9000),
    }
    graph, nodes, issues = build_graph(segments, cabinets, [], ["柜A", "柜B"], PARAMS)
    assert any("十字/交叉点" in i for i in issues), issues
    details, _ = calculate_rows(make_rows([("柜A", "柜B")]), graph, nodes, cabinets, PARAMS)
    row = details[0]
    assert row["状态"] == "OK", row
    # 柜A到交点 4m，交点到柜B 4m，总路径 8m，拐弯：(8 + 7) * 1.2 = 18.0
    assert row["基础路径长度"] == 8.0, row
    assert row["自动统计"] == 18.0, row
    assert "拐弯" in row["规则"]


def test_shaft_cross_floor():
    """跨楼层经竖井：路径含竖井高度且判为拐弯。"""
    segments = [make_segment(1, 0, 0, 10000, 0, floor="1F"), make_segment(2, 0, 0, 10000, 0, floor="2F")]
    shafts = [make_shaft("ZJ1_1F", 5000, 0, "1F"), make_shaft("ZJ1_2F", 5000, 0, "2F")]
    cabinets = {
        "柜A": make_cabinet("柜A", 1000, 0, floor="1F"),
        "柜B": make_cabinet("柜B", 9000, 0, floor="2F"),
    }
    graph, nodes, _ = build_graph(segments, cabinets, shafts, ["柜A", "柜B"], PARAMS)
    details, _ = calculate_rows(make_rows([("柜A", "柜B")]), graph, nodes, cabinets, PARAMS)
    row = details[0]
    assert row["状态"] == "OK", row
    # 1F 走 4m + 竖井 4.5m + 2F 走 4m，先 +7 再按 1.2 倍
    assert row["自动统计"] == 23.4, row
    assert row["向上取整"] == 24, row
    assert "竖井" in row["路径说明"]


def test_rule_override():
    """强制规则表覆盖自动判断的修正量。"""
    segments = [make_segment(1, 0, 0, 10000, 0)]
    cabinets = {"柜A": make_cabinet("柜A", 1000, 0), "柜B": make_cabinet("柜B", 9000, 0)}
    graph, nodes, _ = build_graph(segments, cabinets, [], ["柜A", "柜B"], PARAMS)
    overrides = {("柜B", "柜A"): {"extra": 3.0, "note": "现场实测"}}  # 反向也应命中
    details, _ = calculate_rows(make_rows([("柜A", "柜B")]), graph, nodes, cabinets, PARAMS, overrides)
    row = details[0]
    assert row["修正量"] == 3.0, row
    assert "强制规则" in row["规则"] and "现场实测" in row["规则"]
    assert row["自动统计"] == 11.0, row  # 8m + 3


def test_same_start_end_rejected():
    """起点与终点相同的行标记为需人工确认。"""
    segments = [make_segment(1, 0, 0, 10000, 0)]
    cabinets = {"柜A": make_cabinet("柜A", 1000, 0)}
    graph, nodes, _ = build_graph(segments, cabinets, [], ["柜A"], PARAMS)
    details, _ = calculate_rows(make_rows([("柜A", "柜A")]), graph, nodes, cabinets, PARAMS)
    assert "起点与终点相同" in details[0]["状态"]


def test_alias_loading_and_apply(tmp_path=None):
    """别名表加载与应用：清册名映射到CAD柜子，目标缺失给告警。"""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        (data_dir / "柜名别名.csv").write_text(
            "清册名称,CAD名称,备注\n10kV#1主变进线柜,10kV一号主变进线柜,CAD写法不同\n不存在柜,没有这个柜,\n",
            encoding="utf-8-sig",
        )
        aliases, warnings = load_aliases(data_dir)
        assert aliases == {"10kV#1主变进线柜": "10kV一号主变进线柜", "不存在柜": "没有这个柜"}
        assert not warnings
        cabinets = {"10kV一号主变进线柜": make_cabinet("10kV一号主变进线柜", 0, 0)}
        apply_warnings = apply_aliases(cabinets, aliases)
        assert cabinets["10kV#1主变进线柜"] is cabinets["10kV一号主变进线柜"]
        assert any("不存在" in w for w in apply_warnings)


def test_rule_overrides_loading():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        (data_dir / "强制规则.csv").write_text(
            "起点,终点,修正量,备注\n柜A,柜B,7,走直线\n柜A,柜B,9,重复行\n柜C,,5,缺终点\n",
            encoding="utf-8-sig",
        )
        overrides, warnings = load_rule_overrides(data_dir)
        assert overrides[("柜A", "柜B")]["extra"] == 7.0
        assert len(overrides) == 1
        assert len(warnings) == 2  # 重复 + 无效


def test_shaft_helpers():
    assert normalize_shaft_base_id("ZJ1_1F") == "ZJ1"
    assert normalize_shaft_base_id("ZJ1-2F") == "ZJ1"
    assert normalize_shaft_base_id("竖井甲二层") == "竖井甲"
    assert shaft_suffix_floor("ZJ1_1F") == "1F"
    assert shaft_suffix_floor("ZJ2_2F") == "2F"
    assert shaft_suffix_floor("ZJ3") == ""


def test_floor_elevations_for_3d():
    """三维视图楼层标高：层间距优先取跨层配对竖井 max(下层高度, 上层高度)。"""
    tall = make_shaft("ZJ1_1F", 0, 0, "1F")
    tall.height_m = 6.0
    shafts = [tall, make_shaft("ZJ1_2F", 0, 0, "2F")]
    assert compute_floor_elevations(["1F", "2F"], shafts, PARAMS) == {"1F": 0.0, "2F": 6000.0}
    # 高度标注在上层竖井时同样生效（与 build_graph 竖直边 max(h_a, h_b) 语义一致）
    tall_upper = make_shaft("ZJ1_2F", 0, 0, "2F")
    tall_upper.height_m = 6.0
    shafts_upper = [make_shaft("ZJ1_1F", 0, 0, "1F"), tall_upper]
    assert compute_floor_elevations(["1F", "2F"], shafts_upper, PARAMS) == {"1F": 0.0, "2F": 6000.0}
    # 无竖井时回退到参数"竖井高度"
    assert compute_floor_elevations(["1F", "2F"], [], PARAMS) == {"1F": 0.0, "2F": 4500.0}
    assert compute_floor_elevations([], [], PARAMS) == {}


def test_floor_offsets_for_3d():
    """三维视图楼层对齐：CAD 并排绘制的楼层平面用同名竖井平移对齐到首层。"""
    shafts = [
        make_shaft("ZJ1_1F", 1000, 2000, "1F"),
        make_shaft("ZJ2_1F", 3000, 2000, "1F"),
        make_shaft("ZJ1_2F", 61000, 2100, "2F"),
        make_shaft("ZJ2_2F", 63000, 2100, "2F"),
    ]
    offsets, warnings = compute_floor_offsets(["1F", "2F"], shafts)
    assert offsets["1F"] == {"dx": 0.0, "dy": 0.0}
    assert offsets["2F"] == {"dx": -60000.0, "dy": -100.0}
    assert not warnings
    # 无同名竖井可对齐时保持零偏移并告警
    offsets2, warnings2 = compute_floor_offsets(["1F", "2F"], shafts[:2])
    assert offsets2["2F"] == {"dx": 0.0, "dy": 0.0}
    assert any("2F" in w and "无法对齐" in w for w in warnings2)


def test_floor_offsets_chained_3_floors():
    """三层链式对齐：3F 只与 2F 有同名竖井，偏移叠加 offsets[2F]。"""
    shafts = [
        make_shaft("ZJA_1F", 1000, 2000, "1F"),
        make_shaft("ZJA_2F", 61000, 2100, "2F"),
        make_shaft("ZJB_2F", 65000, 2100, "2F"),
        make_shaft("ZJB_3F", 125000, 2200, "3F"),
    ]
    offsets, warnings = compute_floor_offsets(["1F", "2F", "3F"], shafts)
    assert offsets["2F"] == {"dx": -60000.0, "dy": -100.0}
    assert offsets["3F"] == {"dx": -120000.0, "dy": -200.0}
    assert not warnings
    # base_id 归一化要能剥掉 3F 后缀
    assert normalize_shaft_base_id("ZJB_3F") == "ZJB"
    assert normalize_shaft_base_id("ZJ5-10F") == "ZJ5"
    assert normalize_shaft_base_id("竖井甲三层") == "竖井甲"


def test_straight_collinear_segments_not_bent():
    """两段共线线段不应判为拐弯。"""
    segments = [make_segment(1, 0, 0, 5000, 0), make_segment(2, 5000, 0, 10000, 0)]
    graph = RouteGraph(segments, PARAMS)
    graph.finalize_route_edges()
    start = graph.segment_points[1][0][1]
    end = graph.segment_points[2][1][1]
    _, _, edge_metas = graph.shortest_path(start, end)
    assert not is_route_bent(graph, edge_metas)


def test_disconnected_network_reported():
    """路径网断成多块时，问题清单要列出断块明细。"""
    segments = [make_segment(1, 0, 0, 5000, 0), make_segment(2, 50000, 0, 55000, 0)]
    cabinets = {
        "柜A": make_cabinet("柜A", 1000, 500),
        "柜B": make_cabinet("柜B", 54000, 500),
    }
    graph, nodes, issues = build_graph(segments, cabinets, [], ["柜A", "柜B"], PARAMS)
    assert any("互不连通" in i for i in issues), issues
    assert any("柜A" in i for i in issues if i.strip().startswith("第")), issues
    details, _ = calculate_rows(make_rows([("柜A", "柜B")]), graph, nodes, cabinets, PARAMS)
    assert "不连通" in details[0]["状态"], details[0]


def test_unattached_cabinet_message():
    """柜子有坐标但离路径太远时，状态要说明是未接入而不是缺坐标。"""
    segments = [make_segment(1, 0, 0, 10000, 0)]
    cabinets = {
        "柜A": make_cabinet("柜A", 1000, 500),
        "远柜": make_cabinet("远柜", 1000, 999999),
    }
    graph, nodes, issues = build_graph(segments, cabinets, [], ["柜A", "远柜"], PARAMS)
    assert any("接入失败" in i for i in issues), issues
    details, _ = calculate_rows(make_rows([("柜A", "远柜")]), graph, nodes, cabinets, PARAMS)
    assert "未接入路径网" in details[0]["状态"], details[0]


def test_param_sanity_hints():
    """吸附容差过大、接入距离不够时，问题清单给出带建议值的参数提示。"""
    params = dict(PARAMS)
    params["吸附容差"] = 6000.0
    params["最大接入距离"] = 100.0
    segments = [make_segment(1, 0, 0, 10000, 0)]
    cabinets = {
        "柜A": make_cabinet("柜A", 1000, 500),
        "柜B": make_cabinet("柜B", 5000, 3000),
    }
    graph, nodes, issues = build_graph(segments, cabinets, [], ["柜A", "柜B"], params)
    assert any(i.startswith("参数提示") and "吸附容差" in i for i in issues), issues
    assert any(i.startswith("参数提示") and "最大接入距离" in i and "3901" in i for i in issues), issues


def test_workbook_headers_tolerant():
    """表头带换行/空格要能识别；缺「自动统计」「向上取整」列时自动在表尾补建。"""
    import tempfile

    import openpyxl

    from calculate_cable_lengths import load_workbook_rows

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["分类", "电缆编号", "起 点", "终点", "电缆\n长度"])
    ws.append(["", "1U-101", "A柜", "B柜", 8])
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "自动统计.xlsx"
        wb.save(path)
        wb2, ws2, headers, rows, names = load_workbook_rows(path)
        assert headers["电缆长度"] == 5
        assert headers["起点"] == 3
        # 自动补建在最后一个表头之后，「向上取整」紧跟「自动统计」右边
        assert headers["自动统计"] == 6
        assert ws2.cell(1, 6).value == "自动统计"
        assert headers["向上取整"] == 7
        assert ws2.cell(1, 7).value == "向上取整"
        assert rows[0]["起点"] == "A柜"
        assert rows[0]["终点"] == "B柜"


def test_workbook_ceil_column_inserted_next_to_auto():
    """「自动统计」右边已有其他列时，「向上取整」插在它右边且原数据整体右移。"""
    import tempfile

    import openpyxl

    from calculate_cable_lengths import load_workbook_rows

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["电缆编号", "起点", "终点", "电缆长度", "自动统计", "备注"])
    ws.append(["1U-101", "A柜", "B柜", 8, 9.5, "留观"])
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "自动统计.xlsx"
        wb.save(path)
        wb2, ws2, headers, rows, names = load_workbook_rows(path)
        assert headers["自动统计"] == 5
        assert headers["向上取整"] == 6
        assert ws2.cell(1, 6).value == "向上取整"
        # 原「备注」列被顺移到第 7 列，数据不丢
        assert headers["备注"] == 7
        assert ws2.cell(2, 7).value == "留观"
        assert rows[0]["起点"] == "A柜"


def _run_all() -> int:
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failed += 1
                print(f"FAIL {name}: {exc}")
    return failed


if __name__ == "__main__":
    sys.exit(_run_all())
