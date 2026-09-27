import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import ezdxf

from modules.terminal.duanzi_dxf_tool import (DRAWING, build_cables, build_dxf, generate, parse_strips, parse_terminals,
                             parse_wiring, parse_vertical_coordinate_terminals, resolve_reference)


def horizontal_wire_ys(doc, layer):
    """收集某层上所有横向（y1==y2）线条的 y 坐标。"""
    ys = set()
    for line in doc.modelspace().query("LINE"):
        if line.dxf.layer == layer and abs(line.dxf.start.y - line.dxf.end.y) < 1e-9:
            ys.add(round(line.dxf.start.y, 3))
    return ys


class TerminalLeadDistributionTests(unittest.TestCase):
    def build(self, cable_count):
        positions = {
            ("1-2UD", "4"): {"center": 12.5, "order": 0, "rowKey": "row"},
        }
        connections = [
            {
                "terminalBlock": "1-2UD",
                "terminal": "4",
                "cableNumber": "WL-%02d" % (index + 1),
                "toCabinet": "35kV PT柜",
            }
            for index in range(cable_count)
        ]
        cables, skipped = build_cables({"connections": connections}, positions, DRAWING["terminalLeadSpacing"])
        self.assertEqual([], skipped)
        return cables

    def test_single_lead_stays_centered(self):
        cables = self.build(1)
        self.assertEqual([12.5], [cable["points"][0]["leadX"] for cable in cables])

    def test_two_leads_are_centered_one_millimeter_apart(self):
        cables = self.build(2)
        self.assertEqual([11.75, 13.25], [cable["points"][0]["leadX"] for cable in cables])

    def test_three_leads_are_centered_and_ordered_near_to_far(self):
        cables = self.build(3)
        self.assertEqual([11.0, 12.5, 14.0], [cable["points"][0]["leadX"] for cable in cables])
        self.assertEqual(["WL-01", "WL-02", "WL-03"], [cable["number"] for cable in cables])

    def test_dxf_contains_two_distinct_vertical_leads_for_reused_terminal(self):
        params = {
            "projectName": "测试柜",
            "direction": "DOWN",
            "firstDistance": 10,
            "distanceStep": 5,
            "textHeight": 3,
            "terminalBlocks": [{"name": "1-2UD", "terminals": ["4"]}],
            "connections": [
                {"terminalBlock": "1-2UD", "terminal": "4", "cableNumber": "WL-01", "toCabinet": "35kV PT柜"},
                {"terminalBlock": "1-2UD", "terminal": "4", "cableNumber": "WL-02", "toCabinet": "35kV PT柜"},
            ],
        }
        with TemporaryDirectory() as folder:
            output = Path(folder) / "reused-terminal.dxf"
            build_dxf(params, DRAWING, output)
            doc = ezdxf.readfile(output)
            vertical_wire_xs = sorted({
                round(line.dxf.start.x, 6)
                for line in doc.modelspace().query("LINE")
                if line.dxf.layer == "电缆-接线" and abs(line.dxf.start.x - line.dxf.end.x) < 1e-9
            })
        self.assertEqual([11.75, 13.25], vertical_wire_xs)


class StripsDiscoveryTests(unittest.TestCase):
    def test_ordinary_format_is_one_default_strip(self):
        strips = parse_strips("ZD、1、11\nJD、1、4")
        self.assertEqual(1, len(strips))
        self.assertEqual("默认行", strips[0]["key"])
        self.assertEqual("ZD、JD", strips[0]["name"])
        self.assertEqual(4, strips[0]["terminals"])

    def test_coordinate_format_groups_by_x(self):
        text = "20453.52,-4715.01 1QD 端子名\n20453.52,-4720.01 1\n20572.02,-4716.29 CD 端子名\n20572.02,-4721.29 26"
        strips = parse_strips(text)
        self.assertEqual(["1QD", "CD"], [s["name"] for s in strips])
        self.assertEqual([1, 1], [s["terminals"] for s in strips])

    def test_coordinate_column_tolerates_cad_text_insertion_jitter(self):
        text = "\n".join([
            "-2448.30,100.00 备用电压端子 端子名",
            "-2448.45,95.00 端子号",
            "-2448.45,90.00 1",
        ])
        blocks, errors = parse_terminals(text)
        self.assertEqual([], errors)
        self.assertEqual(["备用电压端子"], [block["label"] for block in blocks])

    def test_marked_name_is_taken_literally(self):
        text = "20453.52,-4715.01 13D 端子名\n20453.52,-4720.01 1\n20453.52,-4725.01 2\n20453.52,-4730.01 3"
        strips = parse_strips(text)
        self.assertEqual(1, len(strips))
        self.assertEqual("13D", strips[0]["name"])
        self.assertEqual(3, strips[0]["terminals"])

    def test_numbered_bs_coordinate_block_keeps_terminals_1_to_20(self):
        lines = ["24665.54,7716.15 1-21BS 端子名"]
        lines.extend("24665.54,%.2f %d" % (7716.15 - number * 5, number) for number in range(1, 21))
        strips = parse_strips("\n".join(lines))
        self.assertEqual(1, len(strips))
        self.assertEqual("1-21BS", strips[0]["name"])
        self.assertEqual(20, strips[0]["terminals"])

    def test_numbered_ykd_coordinate_block_is_also_recognized(self):
        text = "24665.54,7716.15 1-21YKD 端子名\n24665.54,7711.15 1\n24665.54,7706.15 2"
        strips = parse_strips(text)
        self.assertEqual("1-21YKD", strips[0]["name"])
        self.assertEqual(2, strips[0]["terminals"])

    def test_only_marked_names_start_blocks_and_8a_is_ignored(self):
        lines = [
            "22618.41,3204.29 1ID 端子名",
            "22618.41,3199.29 1",
            "22618.41,3179.29 FW 端子名",
            "22618.41,3174.29 1",
            "22618.41,3154.29 JL 端子名",
            "22618.41,3149.29 1",
            "22618.41,3129.29 LT 端子名",
            "22618.41,3124.29 1",
            "22618.41,3104.29 8A",
            "22618.41,3099.29 2",
            "22618.41,3079.29 XMX 端子名",
            "22618.41,3074.29 1",
        ]
        blocks, errors = parse_terminals("\n".join(lines))
        self.assertEqual([], errors)
        self.assertEqual(["1ID", "FW", "JL", "LT", "XMX"], [b["label"] for b in blocks])
        self.assertEqual(["1", "1", "1", "1", "1"], [b["terminals"][0] for b in blocks])
        self.assertNotIn("8A", [b["label"] for b in blocks])

    def test_unmarked_text_is_kept_as_terminal_in_coordinate_columns(self):
        text = "\n".join([
            "22618.41,3064.29 1UD 端子名", "22741.12,3064.29 1YD 端子名",
            "22618.41,3059.29 1", "22741.12,3059.29 3",
            "22618.41,3054.29 1", "22741.12,3044.29 FW 端子名",
            "22618.41,3049.29 2", "22741.12,3039.29 1",
            "22618.41,2999.29 JD 端子名", "22741.12,2999.29 2",
            "22618.41,2994.29 1", "22741.12,2989.29 3",
            "22618.41,2989.29 2", "22741.12,2984.29 JL 端子名",
            "22618.41,2984.29 8A", "22741.12,2979.29 1",
            "22618.41,2979.29 3", "22741.12,2974.29 2",
        ])
        blocks, errors = parse_terminals(text)
        self.assertEqual([], errors)
        by_label = {block["label"]: block["terminals"] for block in blocks}
        self.assertEqual(["1", "1", "2"], by_label["1UD"])
        self.assertEqual(["3"], by_label["1YD"])
        self.assertEqual(["1", "2", "8A", "3"], by_label["JD"])
        self.assertEqual(["1", "2", "3"], by_label["FW"])
        self.assertEqual(["1", "2"], by_label["JL"])
        self.assertIn("8A", by_label["JD"])

    def test_x_and_x2_need_name_mark_in_coordinates(self):
        lines = ["-2435.48,992.73 X 端子名", "-2297.36,992.73 X2 端子名"]
        lines.extend("-2435.48,%.2f %d" % (987.73 - (number - 1) * 5, number) for number in range(1, 51))
        lines.extend("-2297.36,%.2f %d" % (987.73 - (number - 51) * 5, number) for number in range(51, 98))
        blocks, errors = parse_terminals("\n".join(lines))
        self.assertEqual([], errors)
        by_label = {block["label"]: block["terminals"] for block in blocks}
        self.assertEqual(["X", "X2"], [block["label"] for block in blocks])
        self.assertEqual([str(number) for number in range(1, 51)], by_label["X"])
        self.assertEqual([str(number) for number in range(51, 98)], by_label["X2"])
        strips = parse_strips("\n".join(lines))
        self.assertEqual(["X", "X2"], [strip["name"] for strip in strips])
        self.assertEqual([50, 47], [strip["terminals"] for strip in strips])

    def test_unmarked_coordinate_text_is_not_a_block_name(self):
        text = "\n".join([
            "-2435.48,992.73 X",
            "-2297.36,992.73 X2",
            "-2435.48,987.73 1",
            "-2297.36,987.73 51",
        ])
        blocks, errors = parse_terminals(text)
        self.assertEqual([], blocks)
        self.assertTrue(errors)
        self.assertTrue(any("端子名" in item for item in errors))

    def test_mixed_text_is_kept_as_terminal_when_unmarked(self):
        text = "\n".join([
            "-2435.48,992.73 X 端子名",
            "-2297.36,992.73 X2 端子名",
            "-2435.48,987.73 1",
            "-2297.36,987.73 51",
            "-2435.48,982.73 A610",
            "-2297.36,982.73 8A",
            "-2435.48,977.73 2",
            "-2297.36,977.73 52",
        ])
        blocks, errors = parse_terminals(text)
        self.assertEqual([], errors)
        by_label = {block["label"]: block["terminals"] for block in blocks}
        self.assertEqual(["1", "A610", "2"], by_label["X"])
        self.assertEqual(["51", "8A", "52"], by_label["X2"])

    def test_chinese_coordinate_terminal_numbers_are_kept(self):
        text = "\n".join([
            "-2711.93,2688.77 X1-1 报警接点 端子名",
            "-2707.01,2682.91 端子号",
            "-2704.22,2677.91 1",
            "-2704.57,2672.91 2",
            "-2599,2467.13 X0 交流电源端子 端子名",
            "-2597.00,2445.32 备用电压端子",
            "-2596.00,2439.46 端子号",
            "-2595.00,2434.46 1-19号端子",
        ])
        blocks, errors = parse_terminals(text)
        self.assertEqual([], errors)
        by_label = {block["label"]: block["terminals"] for block in blocks}
        self.assertEqual(["端子号", "2", "1"], by_label["X1-1 报警接点"])
        self.assertEqual(["备用电压端子", "端子号", "1-19号端子"], by_label["X0 交流电源端子"])

    def test_empty_name_mark_is_an_error(self):
        blocks, errors = parse_terminals("-2435.48,992.73 端子名\n-2435.48,987.73 1")
        self.assertEqual([], blocks)
        self.assertTrue(any("名称是空的" in item for item in errors))

    def test_missing_coordinate_name_reports_x_and_sample(self):
        rows = [
            {"x": -2500.0, "y": 100.0, "text": "X0", "isName": True},
            {"x": -2500.0, "y": 95.0, "text": "1", "isName": False},
            {"x": -2400.0, "y": 100.0, "text": "端子号", "isName": False},
            {"x": -2400.0, "y": 95.0, "text": "2", "isName": False},
        ]
        blocks, errors, _ = parse_vertical_coordinate_terminals(rows)
        self.assertEqual(["X0"], [block["label"] for block in blocks])
        self.assertTrue(any("横坐标 -2400.00" in item and "端子号、2" in item for item in errors))

    def test_empty_input_has_no_strips(self):
        self.assertEqual([], parse_strips(""))


class WiringParsingTests(unittest.TestCase):
    def test_principle_jd_901_is_not_split_as_jd_terminal(self):
        blocks, errors = parse_terminals("1-21YXD、15、16\nJD、1、2")
        self.assertEqual([], errors)
        connections, cables, errors, warnings = parse_wiring(
            "1-21YXD15、JD-901\n1-21YXD16、JD-903、电流接地选线及电能量采集柜",
            blocks, "本柜", "WL"
        )
        self.assertEqual([], errors)
        self.assertEqual([], warnings)
        self.assertEqual(["JD-901", "JD-903"], [item["principle"] for item in connections])
        self.assertEqual(["15", "16"], [item["terminal"] for item in connections])
        self.assertEqual("电流接地选线及电能量采集柜", cables[0]["destination"])

    def test_valid_reference_still_splits_continuous_wiring_text(self):
        blocks, _ = parse_terminals("ZD、1、11")
        connections, _, errors, _ = parse_wiring(
            "ZD1、+KM1、ZD11、-KM1、直流馈线柜", blocks, "本柜", "WL"
        )
        self.assertEqual([], errors)
        self.assertEqual(["1", "11"], [item["terminal"] for item in connections])


class ColonReferenceTests(unittest.TestCase):
    """接线引用里半角/全角冒号在名称与编号交界处可有可无：ZD:2、ZD：2、ZD2 等价。"""

    BLOCKS, BLOCK_ERRORS = parse_terminals(
        "ZD、1、2、6、11\nJD、1、4\n1UD、1、3、6、8、11、13\n1ID、1、2"
    )

    CASES = [
        # 端子排, 端子号, 全部等价写法
        ("ZD", "2", "ZD2", "ZD:2", "ZD：2"),
        ("ZD", "6", "ZD6", "ZD:6", "ZD：6"),
        ("JD", "1", "JD1", "JD:1", "JD：1"),
        ("JD", "4", "JD4", "JD:4", "JD：4"),
        ("1UD", "1", "1UD1", "1UD:1", "1UD：1"),
        ("1UD", "3", "1UD3", "1UD:3", "1UD：3"),
        ("1UD", "6", "1UD6", "1UD:6", "1UD：6"),
        ("1UD", "8", "1UD8", "1UD:8", "1UD：8"),
        ("1UD", "11", "1UD11", "1UD:11", "1UD：11"),
        ("1UD", "13", "1UD13", "1UD:13", "1UD：13"),
        ("1ID", "1", "1ID1", "1ID:1", "1ID：1"),
        ("1ID", "2", "1ID2", "1ID:2", "1ID：2"),
    ]

    def setUp(self):
        self.assertEqual([], self.BLOCK_ERRORS)

    def test_colon_and_plain_forms_resolve_to_same_terminal(self):
        for block, terminal, plain, colon, colon_full in self.CASES:
            expected = {"block": block, "terminal": terminal}
            for ref in (plain, colon, colon_full):
                resolved, error = resolve_reference(ref, self.BLOCKS)
                self.assertEqual(expected, resolved, "%s 应解析成 %s" % (ref, expected))
                self.assertEqual("", error, "%s 不应报错" % ref)

    def test_bare_block_name_still_missing_terminal(self):
        resolved, error = resolve_reference("ZD:", self.BLOCKS)
        self.assertIsNone(resolved)
        self.assertIn("缺端子号", error)

    def test_colon_forms_parse_in_wiring_end_to_end(self):
        connections, cables, errors, warnings = parse_wiring(
            "ZD:2、+KM1\n1UD：11、A411\n1ID2、B411、直流馈线柜", self.BLOCKS, "本柜", "WL"
        )
        self.assertEqual([], errors)
        self.assertEqual([], warnings)
        self.assertEqual([("ZD", "2"), ("1UD", "11"), ("1ID", "2")],
                         [(item["terminalBlock"], item["terminal"]) for item in connections])
        self.assertEqual(["+KM1", "A411", "B411"], [item["principle"] for item in connections])
        self.assertEqual("直流馈线柜", cables[0]["destination"])

    def test_colon_form_splits_continuous_wiring_text(self):
        connections, _, errors, _ = parse_wiring(
            "ZD1、+KM1、ZD:6、-KM1、直流馈线柜", self.BLOCKS, "本柜", "WL"
        )
        self.assertEqual([], errors)
        self.assertEqual([("ZD", "1"), ("ZD", "6")],
                         [(item["terminalBlock"], item["terminal"]) for item in connections])

    def test_jd_901_principle_still_protected_with_colon_present(self):
        # JD 只有 1、4 号端子；JD:901 不该被当成 JD 的 901 号端子，而该是原理号。
        connections, _, errors, _ = parse_wiring(
            "JD1、JD:901、JD4、JD-903、相邻屏柜", self.BLOCKS, "本柜", "WL"
        )
        self.assertEqual([], errors)
        self.assertEqual([("JD", "1"), ("JD", "4")],
                         [(item["terminalBlock"], item["terminal"]) for item in connections])
        self.assertEqual(["JD:901", "JD-903"], [item["principle"] for item in connections])


class X2ReferenceTests(unittest.TestCase):
    """X 与 X2 同时存在时，较长名称优先，避免把 X251 拆成 X 的 251 号。"""

    BLOCKS, BLOCK_ERRORS = parse_terminals("X、1、2、50\nX2、51、52、97")

    def setUp(self):
        self.assertEqual([], self.BLOCK_ERRORS)

    def test_x2_references_resolve_to_x2_not_x(self):
        cases = [
            ("X2", "51", "X251", "X2:51", "X2：51"),
            ("X2", "97", "X297", "X2:97", "X2：97"),
            ("X", "1", "X1", "X:1", "X：1"),
            ("X", "50", "X50", "X:50", "X：50"),
        ]
        for block, terminal, plain, colon, colon_full in cases:
            expected = {"block": block, "terminal": terminal}
            for ref in (plain, colon, colon_full):
                resolved, error = resolve_reference(ref, self.BLOCKS)
                self.assertEqual(expected, resolved, "%s 应解析成 %s" % (ref, expected))
                self.assertEqual("", error, "%s 不应报错" % ref)

    def test_x2_wiring_splits_continuous_text(self):
        connections, cables, errors, warnings = parse_wiring(
            "X1、A1、X251、B51、X2:97、C97、去向柜", self.BLOCKS, "本柜", "WL"
        )
        self.assertEqual([], errors)
        self.assertEqual([], warnings)
        self.assertEqual([("X", "1"), ("X2", "51"), ("X2", "97")],
                         [(item["terminalBlock"], item["terminal"]) for item in connections])
        self.assertEqual("去向柜", cables[0]["destination"])


class QdComReferenceTests(unittest.TestCase):
    BLOCKS, BLOCK_ERRORS = parse_terminals("QD-COM、1、2、3、4、5、6、7、8")

    def setUp(self):
        self.assertEqual([], self.BLOCK_ERRORS)
        self.assertEqual("QD-COM", self.BLOCKS[0]["name"])

    def test_qd_com_is_recognized_as_a_block_name(self):
        blocks, errors = parse_terminals("ZD、1、QD-COM、1、2")
        self.assertEqual([], errors)
        self.assertEqual(["ZD", "QD-COM"], [block["name"] for block in blocks])

    def test_qd_colon_com_1_to_8_resolve_to_qd_com(self):
        for number in range(1, 9):
            expected = {"block": "QD-COM", "terminal": str(number)}
            for reference in ("QD:COM%d" % number, "QD：COM%d" % number, "QD-COM%d" % number):
                with self.subTest(reference=reference):
                    resolved, error = resolve_reference(reference, self.BLOCKS)
                    self.assertEqual(expected, resolved)
                    self.assertEqual("", error)

    def test_qd_colon_com_forms_parse_end_to_end(self):
        wiring = "、".join("QD:COM%d、P%d" % (number, number) for number in range(1, 8))
        wiring += "、QD:COM8、P8、去向柜"
        connections, cables, errors, warnings = parse_wiring(wiring, self.BLOCKS, "本柜", "WL")
        self.assertEqual([], errors)
        self.assertEqual([], warnings)
        self.assertEqual([str(number) for number in range(1, 9)],
                         [item["terminal"] for item in connections])
        self.assertEqual("去向柜", cables[0]["destination"])


class DirectionTests(unittest.TestCase):
    BLOCKS = [
        {"name": "1QD", "terminals": ["1"], "physicalGroup": "列1"},
        {"name": "CD", "terminals": ["26"], "physicalGroup": "列2"},
    ]
    WIRING = [
        {"terminalBlock": "1QD", "terminal": "1", "cableNumber": "WL-01", "toCabinet": "A柜"},
        {"terminalBlock": "CD", "terminal": "26", "cableNumber": "WL-02", "toCabinet": "B柜"},
    ]

    def build(self, directions=None):
        params = {
            "projectName": "测试柜",
            "direction": "DOWN",
            "firstDistance": 10,
            "distanceStep": 5,
            "textHeight": 3,
            "terminalBlocks": list(self.BLOCKS),
            "connections": list(self.WIRING),
        }
        if directions is not None:
            params["directions"] = directions
        with TemporaryDirectory() as folder:
            output = Path(folder) / "direction.dxf"
            result = build_dxf(params, DRAWING, output)
            return result, ezdxf.readfile(output)

    def test_default_direction_is_down_for_every_row(self):
        result, _ = self.build()
        self.assertEqual({"列1": False, "列2": False}, {r["key"]: r["up"] for r in result["rows"]})

    def test_mixed_directions_keep_rows_separated(self):
        result, _ = self.build(directions={"列1": "UP", "列2": "DOWN"})
        rows = {r["key"]: r for r in result["rows"]}
        self.assertTrue(rows["列1"]["up"])
        self.assertFalse(rows["列2"]["up"])

        def occupied(r):
            if r["up"]:
                return (r["blockY"], r["blockY"] + 75 + r["span"])   # 端子排在上，电缆区在其上方
            return (r["blockY"] - r["span"], r["blockY"] + 75)       # 电缆区在端子排下方

        a, b = occupied(rows["列1"]), occupied(rows["列2"])
        lower, higher = (a, b) if max(a) <= min(b) else (b, a)
        self.assertLessEqual(max(lower), min(higher), "两块端子排（含各自电缆区）不能重叠")

    def test_cable_lies_on_the_chosen_side_of_its_block(self):
        result, doc = self.build(directions={"列1": "UP", "列2": "DOWN"})
        rows = {r["key"]: r for r in result["rows"]}
        cable_ys = horizontal_wire_ys(doc, "电缆-接线")
        up, down = rows["列1"], rows["列2"]
        # 首根电缆 offset = firstDistance = 10 = span（单根时）
        self.assertIn(round(up["pinY"] + up["span"], 3), cable_ys, "向上块电缆应在 pin 线上方")
        self.assertIn(round(down["pinY"] - down["span"], 3), cable_ys, "向下块电缆应在 pin 线下方")
        self.assertGreater(round(up["pinY"] + up["span"], 3), round(up["blockY"] + 75, 3),
                           "向上电缆要高于该块顶部")
        self.assertLess(round(down["pinY"] - down["span"], 3), round(down["blockY"], 3),
                        "向下电缆要低于该块底部")

    def test_lead_corner_marks_and_requested_colors(self):
        result, doc = self.build(directions={"列1": "UP", "列2": "DOWN"})
        rows = {r["key"]: r for r in result["rows"]}
        msp = doc.modelspace()
        wire_lines = list(msp.query('LINE[layer=="电缆-接线"]'))

        horizontal = [line for line in wire_lines if abs(line.dxf.start.y - line.dxf.end.y) < 1e-9]
        self.assertTrue(horizontal)
        self.assertTrue(all(line.dxf.color == 3 for line in horizontal))

        diagonals = [
            line for line in wire_lines
            if abs(abs(line.dxf.start.x - line.dxf.end.x) - 1.0) < 1e-9
            and abs(abs(line.dxf.start.y - line.dxf.end.y) - 1.0) < 1e-9
        ]
        self.assertEqual(2, len(diagonals))
        up_y = rows["列1"]["pinY"] + rows["列1"]["span"]
        down_y = rows["列2"]["pinY"] - rows["列2"]["span"]
        self.assertTrue(any(max(line.dxf.start.y, line.dxf.end.y) == up_y for line in diagonals))
        self.assertTrue(any(min(line.dxf.start.y, line.dxf.end.y) == down_y for line in diagonals))

    def test_principle_text_is_cyan(self):
        params = {
            "projectName": "测试柜", "direction": "DOWN", "firstDistance": 10,
            "distanceStep": 5, "textHeight": 3,
            "terminalBlocks": [{"name": "1QD", "terminals": ["1"]}],
            "connections": [{
                "terminalBlock": "1QD", "terminal": "1", "principle": "A610",
                "cableNumber": "WL-01", "toCabinet": "去向柜",
            }],
        }
        with TemporaryDirectory() as folder:
            output = Path(folder) / "principle-color.dxf"
            build_dxf(params, DRAWING, output)
            doc = ezdxf.readfile(output)
            principle = next(item for item in doc.modelspace().query("TEXT") if item.dxf.text == "A610")
        self.assertEqual(4, principle.dxf.color)
        self.assertEqual((0, 2), (principle.dxf.halign, principle.dxf.valign))

    def test_principle_text_uses_end_alignment_for_upward_cables(self):
        params = {
            "projectName": "测试柜", "direction": "UP", "firstDistance": 10,
            "distanceStep": 5, "textHeight": 3,
            "terminalBlocks": [{"name": "1QD", "terminals": ["1"]}],
            "connections": [{
                "terminalBlock": "1QD", "terminal": "1", "principle": "A610",
                "cableNumber": "WL-01", "toCabinet": "去向柜",
            }],
        }
        with TemporaryDirectory() as folder:
            output = Path(folder) / "principle-up.dxf"
            build_dxf(params, DRAWING, output)
            doc = ezdxf.readfile(output)
            principle = next(item for item in doc.modelspace().query("TEXT") if item.dxf.text == "A610")
        self.assertEqual((2, 2), (principle.dxf.halign, principle.dxf.valign))

    def test_bian_annotation_geometry_and_text_positions(self):
        params = {
            "projectName": "测试柜",
            "direction": "DOWN",
            "firstDistance": 10,
            "distanceStep": 5,
            "textHeight": 3,
            "terminalBlocks": [{"name": "1QD", "terminals": ["1"]}],
            "connections": [{
                "terminalBlock": "1QD", "terminal": "1", "cableNumber": "UPS-12",
                "toCabinet": "直流馈线柜", "cableSpec": "",
            }],
        }
        with TemporaryDirectory() as folder:
            output = Path(folder) / "bian.dxf"
            build_dxf(params, DRAWING, output)
            doc = ezdxf.readfile(output)
            msp = doc.modelspace()
            triangle = next(iter(msp.query('LWPOLYLINE[layer=="电缆-接线"]')))
            points = [(round(p[0], 6), round(p[1], 6)) for p in triangle.get_points("xy")]
            tip_x, cable_y = points[0]
            horizontal = [
                item for item in msp.query('LINE[layer=="电缆-接线"]')
                if abs(item.dxf.start.y - item.dxf.end.y) < 1e-9
            ]
            line_right = max(max(item.dxf.start.x, item.dxf.end.x) for item in horizontal)
            labels = {item.dxf.text: item for item in msp.query('TEXT[layer=="电缆-文字"]')}

        self.assertTrue(triangle.closed)
        self.assertAlmostEqual(5.0, points[1][1] - points[2][1])
        self.assertAlmostEqual(40.0, line_right - tip_x)
        self.assertAlmostEqual(tip_x - 20.0, labels["UPS-12"].dxf.insert.x)
        self.assertAlmostEqual(tip_x + 1.5, labels["至直流馈线柜"].dxf.insert.x)
        self.assertAlmostEqual(tip_x + 32.0, labels["未填写"].dxf.insert.x)
        self.assertAlmostEqual(cable_y + 0.5, labels["UPS-12"].dxf.insert.y)


class WiringOnlyExportTests(unittest.TestCase):
    def test_generated_pair_preserves_wiring_and_locks_only_background(self):
        terminals = "100,100 X 端子名\n100,95 1\n100,90 2\n200,100 X2 端子名\n200,95 1"
        strips = parse_strips(terminals)
        self.assertEqual(2, len(strips))
        with TemporaryDirectory() as folder, patch("modules.terminal.duanzi_dxf_tool.desktop_dir", return_value=Path(folder)):
            result = generate({
                "terminals": terminals,
                "wiring": "X1、A610、甲柜\nX2、B903、乙柜\nX2:1、701、丙柜",
                "directions": {strips[0]["key"]: "UP", strips[1]["key"]: "DOWN"},
            })
            self.assertTrue(result["ok"], result)
            full = ezdxf.readfile(result["path"])
            wiring = ezdxf.readfile(result["wiringPath"])
            allowed = {DRAWING["layers"][role] for role in ("principle", "wire", "label")}
            for role in ("frame", "text", "title"):
                layer = full.layers.get(DRAWING["layers"][role])
                self.assertTrue(layer.is_locked())
                self.assertFalse(layer.is_off())
                self.assertFalse(layer.is_frozen())
            for layer in allowed:
                self.assertFalse(full.layers.get(layer).is_locked())
                self.assertFalse(wiring.layers.get(layer).is_locked())
            expected = [e for e in full.modelspace() if e.dxf.layer in allowed]
            actual = list(wiring.modelspace())
            self.assertTrue(actual)
            self.assertEqual([e.dxfattribs() for e in expected], [e.dxfattribs() for e in actual])
            self.assertEqual(allowed, {e.dxf.layer for e in actual})
            self.assertEqual({"A610", "B903", "701"}, {
                e.dxf.text for e in actual if e.dxf.layer == DRAWING["layers"]["principle"]})
            self.assertTrue(all(e.dxf.width == 0.7 and e.dxf.style == "HZ"
                                for e in wiring.modelspace().query("TEXT")))
            self.assertFalse(full.audit().has_errors)
            self.assertFalse(wiring.audit().has_errors)

    def test_no_connections_produces_empty_wiring_file(self):
        with TemporaryDirectory() as folder, patch("modules.terminal.duanzi_dxf_tool.desktop_dir", return_value=Path(folder)):
            result = generate({"terminals": "ZD、1、2", "wiring": ""})
            self.assertTrue(result["ok"])
            self.assertEqual(0, len(ezdxf.readfile(result["wiringPath"]).modelspace()))


class MixedFormatRejectionTests(unittest.TestCase):
    """两种输入格式混用必须报错，不能静默产出假端子排，也不能抛异常。"""

    def test_normal_first_then_coordinate_line_is_rejected(self):
        blocks, errors = parse_terminals("ZD、1、2\n-100.0,50.0 X 端子名\n-100.0,45.0 1")
        self.assertEqual([], blocks)
        self.assertEqual(1, len(errors))
        self.assertIn("第 2 行", errors[0])
        self.assertIn("不能混用", errors[0])

    def test_coordinate_first_then_normal_line_is_rejected(self):
        blocks, errors = parse_terminals("10,100 X1 端子名\nZD、5、6")
        self.assertEqual([], blocks)
        self.assertEqual(1, len(errors))
        self.assertIn("第 2 行", errors[0])
        self.assertIn("不能混用", errors[0])

    def test_mixed_format_generate_reports_error_instead_of_crashing(self):
        result = generate({"terminals": "10,100 X1 端子名\nZD、5、6",
                           "wiring": "X1:1、a、甲柜", "cabinet": "测试柜"})
        self.assertFalse(result["ok"])
        self.assertTrue(any("不能混用" in item for item in result["errors"]))

    def test_pure_formats_still_parse(self):
        blocks, errors = parse_terminals("ZD、1、2、3\nZD、4、5、6")
        self.assertEqual([], errors)
        self.assertEqual([("ZD", ["1", "2", "3", "4", "5", "6"])],
                         [(item["name"], item["terminals"]) for item in blocks])
        blocks, errors = parse_terminals("100.0,50 X 端子名\n100.0,45 1\n100.0,40 2")
        self.assertEqual([], errors)
        self.assertEqual([("X", ["1", "2"])],
                         [(item["name"], item["terminals"]) for item in blocks])


class HorizontalCoordinateTests(unittest.TestCase):
    ROW = "0,100 X 端子名\n10,100 1\n15,100 2\n20,100 3\n40,100 Y 端子名\n50,100 1\n55,100 2"

    def test_horizontal_rows_split_by_name_x(self):
        blocks, errors = parse_terminals(self.ROW)
        self.assertEqual([], errors)
        self.assertEqual("横排", blocks[0]["coordinateMode"])
        self.assertEqual([("X", ["1", "2", "3"]), ("Y", ["1", "2"])],
                         [(block["label"], block["terminals"]) for block in blocks])

    def test_row_without_marked_name_is_reported_not_dropped(self):
        blocks, errors = parse_terminals(self.ROW + "\n0,0 Z\n10,0 1\n15,0 2")
        self.assertEqual(["X", "Y"], [block["label"] for block in blocks])
        self.assertTrue(any("没有标「端子名」" in item and "Z" in item for item in errors))

    def test_text_left_of_first_name_is_reported(self):
        _, errors = parse_terminals("-5,100 9\n" + self.ROW)
        self.assertTrue(any("“9”左边没有端子排名称" in item for item in errors))

    def test_text_on_next_name_x_is_kept(self):
        blocks, errors = parse_terminals(self.ROW + "\n40,99 4")
        self.assertEqual([], errors)
        self.assertEqual(["1", "2", "3", "4"], blocks[0]["terminals"])


class OutputTests(unittest.TestCase):
    def test_safe_name_handles_windows_restrictions(self):
        from modules.terminal.duanzi_dxf_tool import safe_name
        self.assertEqual("a_b_c", safe_name("a/b:c"))
        self.assertEqual("柜", safe_name("柜. "))
        self.assertEqual("_CON", safe_name("CON"))
        self.assertEqual("端子排", safe_name("  "))

    def test_output_dir_is_respected(self):
        with TemporaryDirectory() as folder:
            result = generate({"terminals": "ZD、1、2", "wiring": "ZD1、a、甲柜", "outputDir": folder})
            self.assertTrue(result["ok"])
            self.assertEqual(Path(folder), Path(result["path"]).parent)

    def test_locked_target_falls_back_then_reports_error(self):
        with TemporaryDirectory() as folder:
            calls = []

            def locked_once(params, drawing, output, wiring_output=None):
                calls.append(output)
                if len(calls) == 1:
                    raise PermissionError("locked")
                return build_dxf(params, drawing, output, wiring_output)
            with patch("modules.terminal.duanzi_dxf_tool.build_dxf", side_effect=locked_once):
                result = generate({"terminals": "ZD、1", "wiring": "", "outputDir": folder})
            self.assertTrue(result["ok"])
            self.assertNotEqual(calls[0], calls[1])
            self.assertTrue(Path(result["wiringPath"]).name.endswith("-仅接线.dxf"))
            with patch("modules.terminal.duanzi_dxf_tool.build_dxf", side_effect=PermissionError("locked")):
                result = generate({"terminals": "ZD、1", "wiring": "", "outputDir": folder})
            self.assertFalse(result["ok"])
            self.assertIn("无法写入", result["errors"][0])


class ReferenceAmbiguityTests(unittest.TestCase):
    def test_explicit_colon_never_falls_back_to_shorter_name(self):
        # X2 没有 1 号时，X2:1 必须报错，不能静默接到 X 的 21 号上。
        blocks, _ = parse_terminals("X、1、21、50\nX2、51、52")
        resolved, error = resolve_reference("X2:1", blocks)
        self.assertIsNone(resolved)
        self.assertIn("X2 没有 1 号端子", error)
        self.assertEqual({"block": "X", "terminal": "21"}, resolve_reference("X:21", blocks)[0])
        self.assertEqual({"block": "X", "terminal": "21"}, resolve_reference("X21", blocks)[0])

    def test_ambiguous_reference_prefers_longer_name_and_warns(self):
        blocks, _ = parse_terminals("X、1、21\nX2、1、2")
        connections, _, errors, warnings = parse_wiring("X21、a、柜", blocks, "", "WL")
        self.assertEqual([], errors)
        self.assertEqual(("X2", "1"), (connections[0]["terminalBlock"], connections[0]["terminal"]))
        self.assertTrue(any("X2:1" in item and "X:21" in item for item in warnings))
        connections, _, errors, warnings = parse_wiring("X:21、a、柜", blocks, "", "WL")
        self.assertEqual(("X", "21"), (connections[0]["terminalBlock"], connections[0]["terminal"]))
        self.assertEqual([], warnings)

    def test_duplicate_labels_in_coordinates_warn_with_internal_names(self):
        text = "10,100 X 端子名\n10,95 1\n60,100 X 端子名\n60,95 1"
        blocks, errors = parse_terminals(text)
        self.assertEqual([], errors)
        connections, _, errors, warnings = parse_wiring("X1、a、柜", blocks, "", "WL")
        self.assertEqual([], errors)
        self.assertEqual("X", connections[0]["terminalBlock"])
        self.assertTrue(any("X@2:1" in item for item in warnings))


class InputHygieneTests(unittest.TestCase):
    def test_numeric_block_name_is_rejected(self):
        blocks, errors = parse_terminals("ZD、1、2\n3、4")
        self.assertTrue(any("纯数字" in item for item in errors))
        # 制表符分隔的坐标不符合坐标格式，只会被当成普通格式；此时必须报错而不是画出假端子排。
        _, errors = parse_terminals("100\t50\tX 端子名\n100\t45\t1")
        self.assertTrue(any("纯数字" in item for item in errors))

    def test_repeated_coordinate_terminals_get_their_own_cells(self):
        text = "10,100 1UD 端子名\n10,95 1\n10,90 1\n10,85 2"
        with TemporaryDirectory() as folder:
            result = generate({"terminals": text, "wiring": "1UD1、a、柜", "outputDir": folder})
            self.assertTrue(result["ok"])
            self.assertEqual(3, result["stats"]["terminals"])
            self.assertTrue(any("出现了不止一次" in item for item in result["warnings"]))
            doc = ezdxf.readfile(result["path"])
            xs = sorted(round(item.dxf.align_point.x, 3) for item in doc.modelspace().query("TEXT")
                        if item.dxf.layer == DRAWING["layers"]["text"] and item.dxf.text in ("1", "2"))
            self.assertEqual([12.5, 17.5, 22.5], xs)
            lead = [line for line in doc.modelspace().query("LINE")
                    if line.dxf.layer == DRAWING["layers"]["wire"] and abs(line.dxf.start.x - line.dxf.end.x) < 1e-9]
            self.assertEqual({12.5}, {round(line.dxf.start.x, 3) for line in lead})

    def test_control_characters_never_reach_dxf_text(self):
        with TemporaryDirectory() as folder:
            result = generate({"terminals": "ZD、1", "wiring": "ZD1、a、柜", "cabinet": "一号\n柜\t甲",
                               "outputDir": folder})
            self.assertTrue(result["ok"])
            self.assertEqual("一号 柜 甲-端子排.dxf", Path(result["path"]).name)
            texts = [item.dxf.text for item in ezdxf.readfile(result["path"]).modelspace().query("TEXT")]
            self.assertIn("一号 柜 甲", texts)
            self.assertFalse(any(char in value for value in texts for char in "\r\n\t"))

    def test_strips_from_blocks_matches_parse_strips(self):
        from modules.terminal.duanzi_dxf_tool import strips_from_blocks
        text = "10,100 1QD 端子名\n10,95 1\n10,85 2QD 端子名\n10,80 1\n60,100 CD 端子名\n60,95 1"
        blocks, _ = parse_terminals(text)
        self.assertEqual(parse_strips(text), strips_from_blocks(blocks))
        self.assertEqual(["1QD、2QD", "CD"], [strip["name"] for strip in strips_from_blocks(blocks)])


if __name__ == "__main__":
    unittest.main()
