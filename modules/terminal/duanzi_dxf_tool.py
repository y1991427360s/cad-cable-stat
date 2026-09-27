# -*- coding: utf-8 -*-
"""端子排出图工具 — 解析与绘图核心

本文件只负责纯逻辑：解析端子/接线文本、计算布局、写出 DXF。没有界面代码，
`generate()` 是唯一对外入口，桌面版 `duanzi_gui.py` 直接调用它。

启动器 `端子排出图桌面版.cmd` 必须保持纯 ASCII 且用 CRLF 换行，所以本文件用英文名；
cmd.exe 按系统代码页读批处理，UTF-8 中文或 LF 换行都会让它把命令行拆错。

依赖：ezdxf（缺少时执行 pip install ezdxf）
"""

from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 冒号不当作字段分隔符：接线引用里 ZD:2 与 ZD2 等价，半角/全角冒号在名称与编号
# 交界处可有可无，统一交给 resolve_reference 在解析名称+端子号时剥掉。
FIELD_SPLIT = re.compile(r"[\t,，、]")
RECORD_SPLIT = re.compile(r"[;；|｜\r\n]+")
# 普通格式仍用名称形态切段：通常以 D 结尾，也兼容 QD-COM、短字母名和 X2。
# 坐标格式不再猜名称，只认行末「端子名」标记。
BLOCK_NAME_HINT = re.compile(
    r"^(?:(?=.*[A-Za-z一-龥])[0-9A-Za-z一-龥\-]*(?:[Dd]|-[Cc][Oo][Mm])|[A-Za-z]{1,4}[0-9]{0,2})$"
)
COORDINATE_LINE = re.compile(
    r"^\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*,\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s+(.+?)\s*$"
)
COORDINATE_NAME_MARK = re.compile(r"^(?P<name>.*?)\s*端子名\s*$")
NUMERIC_TOKEN = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$")
# TEXT 实体只能是单行，粘贴带进来的换行、制表等控制字符会写坏 DXF。
CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]+")
# CAD 导出的文字插入点常有 0.1~0.3 mm 抖动；按物理列归组时不能使用过严的 0.05。
COORDINATE_X_TOLERANCE = 0.5
CJK_START = 0x2E80

DRAWING = {
    "style": "HZ",
    "font": "txt.shx",
    "bigfont": "hztxt.shx",
    "widthFactor": 0.7,
    "color": 7,
    "cableLineColor": 3,
    "principleColor": 4,
    "terminalWidth": 5.0,
    "terminalLeadSpacing": 1.5,
    "numberBandHeight": 5.0,
    "blockNameWidth": 10.0,
    "terminalZoneHeight": 35.0,
    "physicalGroupGap": 15.0,
    "physicalRowGap": 20.0,
    "trunkExtend": 18.0,
    "columnGap": 4.0,
    # AA整合版本.lsp 的 BIAN 命令标注基准：三角尖点相对电缆线右端左移 40 mm。
    "bianRightOffset": 40.0,
    "bianTriangleSide": 5.0,
    "bianNumberOffset": -20.0,
    "bianToOffset": 1.5,
    "bianSpecOffset": 32.0,
    "bianTextYOffset": 0.5,
    "drawTitle": True,
    "drawFrame": False,
    "layers": {
        "frame": "端子排-框线",
        "text": "端子排-文字",
        "principle": "接线-原理号",
        "wire": "电缆-接线",
        "label": "电缆-文字",
        "title": "图签-文字",
    },
}

DEFAULTS = {"direction": "DOWN", "firstDistance": 10, "distanceStep": 5, "textHeight": 3, "cablePrefix": "WL"}


# ==================== 输入解析 ====================

def split_fields(text: str) -> list[str]:
    return [part.strip() for part in FIELD_SPLIT.split(text)]


def clean_tokens(text: str) -> list[str]:
    return [part.rstrip("。.!！") for part in split_fields(text) if part.strip()]


def looks_like_block_name(token: str) -> bool:
    """识别 ZD/1-2ID/JD/QD-COM 等端子排名称，排除普通端子号和原理号。"""
    return bool(BLOCK_NAME_HINT.match(token))


def split_coordinate_name(text: str):
    """坐标行末带「端子名」则前面是端子排名称；没标就不是名称。"""
    match = COORDINATE_NAME_MARK.match(text or "")
    if not match:
        return None, False
    return match.group("name").strip(), True


def unique_block_name(label: str, name_counts: dict[str, int]) -> str:
    name_counts[label] = name_counts.get(label, 0) + 1
    return label if name_counts[label] == 1 else "%s@%d" % (label, name_counts[label])


def cluster_coordinate_bands(rows: list[dict], gap: float = 50.0) -> list[list[dict]]:
    bands: list[list[dict]] = []
    for row in sorted(rows, key=lambda item: item["y"], reverse=True):
        if not bands or abs(bands[-1][-1]["y"] - row["y"]) > gap:
            bands.append([row])
        else:
            bands[-1].append(row)
    return bands


def parse_vertical_coordinate_terminals(rows: list[dict]):
    """旧式纵排坐标：相同 X 为一块物理端子排，按 Y 从上到下读取。"""
    columns = []
    for row in sorted(rows, key=lambda item: item["x"]):
        column = next((item for item in columns if abs(item["x"] - row["x"]) <= COORDINATE_X_TOLERANCE), None)
        if column is None:
            column = {"x": row["x"], "rows": []}
            columns.append(column)
        column["rows"].append(row)

    blocks = []
    name_counts: dict[str, int] = {}
    errors = []
    for column_index, column in enumerate(sorted(columns, key=lambda item: item["x"]), start=1):
        current = None
        physical_group = "坐标列-%d" % column_index
        if not any(row.get("isName") for row in column["rows"]):
            sample = [row["text"] for row in sorted(column["rows"], key=lambda item: item["y"], reverse=True)
                      if row.get("text")][:3]
            sample_text = "、".join(sample)
            errors.append(
                "坐标列 %d（横坐标 %.2f）没有标「端子名」的端子排名称；该列示例文字：%s。"
                "请在本列任意一行补写「端子排名称 端子名」"
                % (column_index, column["x"], sample_text or "（空）")
            )
            continue
        for row in sorted(column["rows"], key=lambda item: item["y"], reverse=True):
            token = row["text"]
            if row.get("isName"):
                current = {"name": unique_block_name(token, name_counts), "label": token, "terminals": [], "segments": 1,
                           "layoutGroup": "坐标端子排", "physicalGroup": physical_group,
                           "sourceColumn": physical_group, "rowOrder": column_index}
                blocks.append(current)
            elif token:
                if current is None:
                    errors.append("坐标列 %d 的“%s”前面没有端子排名称" % (column_index, token))
                else:
                    current["terminals"].append(token)
    return blocks, errors, "纵列"


def parse_horizontal_coordinate_terminals(rows: list[dict]):
    """横排坐标：按水平带分组，再由名称的 X 位置划分每一块端子排。"""
    blocks = []
    name_counts: dict[str, int] = {}
    errors = []
    block_order = 0
    for band_index, band in enumerate(cluster_coordinate_bands(rows), start=1):
        anchors = sorted((row for row in band if row.get("isName")), key=lambda item: item["x"])
        terminal_rows = sorted((row for row in band if not row.get("isName") and row["text"]),
                               key=lambda item: item["x"])
        # 和纵列模式一致：没有归属的文字必须报出来，不能静默丢掉（漏标「端子名」会让整排端子从图上消失）。
        if not anchors:
            if terminal_rows:
                errors.append("纵坐标 %.2f 附近的一排文字没有标「端子名」的端子排名称；示例文字：%s。"
                              "请在这一排补写「端子排名称 端子名」"
                              % (band[0]["y"], "、".join(row["text"] for row in terminal_rows[:3])))
            continue
        orphans = [row["text"] for row in terminal_rows if row["x"] <= anchors[0]["x"]]
        if orphans:
            errors.append("纵坐标 %.2f 附近的“%s”左边没有端子排名称" % (band[0]["y"], "、".join(orphans[:3])))
        for anchor_index, anchor in enumerate(anchors):
            next_x = anchors[anchor_index + 1]["x"] if anchor_index + 1 < len(anchors) else float("inf")
            # 右端取闭区间：正好落在下一个名称横坐标上的文字归前一块，不会两边都不要。
            terminals = [row for row in terminal_rows if anchor["x"] < row["x"] <= next_x]
            if not terminals:
                errors.append("端子排「%s」右侧没有识别到端子号" % anchor["text"])
                continue
            block_order += 1
            label = anchor["text"]
            blocks.append({
                "name": unique_block_name(label, name_counts), "label": label,
                "terminals": [row["text"] for row in terminals], "segments": 1,
                "layoutGroup": "坐标端子排", "physicalGroup": "坐标块-%d" % block_order,
                "rowOrder": block_order, "sourceBand": band_index,
            })
    if not blocks:
        errors.append("坐标清单里没有标「端子名」的横向端子排")
    return blocks, errors, "横排"


def parse_coordinate_terminals(text: str):
    """自动识别纵列型或横排型“X,Y 文字”清单；仅行末「端子名」建立端子排，其余文字均为端子号。"""
    rows = []
    errors = []
    for line_number, raw in enumerate((text or "").splitlines(), start=1):
        if not raw.strip():
            continue
        match = COORDINATE_LINE.match(raw)
        if not match:
            # 返回空列表而不是 None：调用方会先把 blocks 交给 parse_wiring，None 会抛 TypeError。
            return [], ["第 %d 行不是“横坐标,纵坐标 文字”格式：%s。"
                        "两种格式不能混用，请整段统一" % (line_number, raw.strip())]
        token = match.group(3).strip()
        name, marked = split_coordinate_name(token)
        if marked and not name:
            errors.append("第 %d 行标了端子名，但名称是空的" % line_number)
            continue
        rows.append({
            "x": float(match.group(1)),
            "y": float(match.group(2)),
            "text": name if marked else token,
            "isName": marked,
        })
    if not rows:
        return [], errors
    if not any(row.get("isName") for row in rows):
        return [], errors + ["坐标清单里没有标「端子名」的端子排名称。请写成「X 端子名」这种格式"]
    anchors = [row for row in rows if row.get("isName")]
    terminal_rows = [row for row in rows if not row.get("isName") and row["text"]]
    vertical_matches = sum(1 for row in terminal_rows if any(abs(row["x"] - anchor["x"]) <= COORDINATE_X_TOLERANCE for anchor in anchors))
    if terminal_rows and vertical_matches / len(terminal_rows) >= 0.5:
        blocks, parse_errors, mode = parse_vertical_coordinate_terminals(rows)
    else:
        blocks, parse_errors, mode = parse_horizontal_coordinate_terminals(rows)
    errors.extend(parse_errors)
    for block in blocks:
        if not block["terminals"]:
            errors.append("端子排「%s」后面没有端子号" % block.get("label", block["name"]))
        block["coordinateMode"] = mode
    return blocks, errors


def parse_terminals(text: str):
    """一块端子排一行：第一项是名称，其余是端子号。
    整段用顿号连着粘过来也认：中途遇到像端子排名称的项就自动开下一块。
    同一个名称分成几段写（厂家图里一条端子排画成两截）会自动接成一块，按出现顺序续端子号。"""
    nonempty = [line for line in (text or "").splitlines() if line.strip()]
    if nonempty and COORDINATE_LINE.match(nonempty[0]):
        return parse_coordinate_terminals(text)

    blocks: list[dict] = []
    errors: list[str] = []
    index: dict[str, dict] = {}
    # 只看首行会把后面混进来的坐标行当成顿号字段，静默画出「-100.0」这种假端子排。
    for line_number, raw in enumerate((text or "").splitlines(), start=1):
        if raw.strip() and COORDINATE_LINE.match(raw):
            return [], ["第 %d 行是坐标格式，但整段按普通格式解析：%s。"
                        "两种格式不能混用，请整段统一" % (line_number, raw.strip())]
    for unit in [u.strip() for u in RECORD_SPLIT.split(text or "") if u.strip()]:
        current: dict | None = None
        for position, token in enumerate(clean_tokens(unit)):
            if position == 0 or looks_like_block_name(token) or current is None:
                if token in index:
                    current = index[token]
                    current["segments"] += 1
                else:
                    current = {"name": token, "terminals": [], "segments": 1}
                    index[token] = current
                    blocks.append(current)
                continue
            current["terminals"].append(token)
    for block in blocks:
        if NUMERIC_TOKEN.match(block["name"]):
            # 真实端子排名称都带字母；纯数字多半是漏写名称，或坐标用了制表符而没按坐标格式识别。
            errors.append("端子排名称「%s」是纯数字，多半是漏写了名称；"
                          "坐标格式每行要写成「横坐标,纵坐标 文字」" % block["name"])
            continue
        if not block["terminals"]:
            errors.append("端子排「%s」后面没有端子号" % block["name"])
            continue
        kept: list[str] = []
        repeated: list[str] = []
        for number in block["terminals"]:
            if number in kept:
                if number not in repeated:
                    repeated.append(number)
            else:
                kept.append(number)
        if repeated:
            errors.append("端子排「%s」里这些端子号出现了不止一次：%s" % (block["name"], "、".join(repeated)))
        block["terminals"] = kept
    if not blocks and not errors:
        errors.append("上面的端子排还没填")
    return blocks, errors


def parse_strips(text: str) -> list[dict]:
    """只解析端子排清单，返回每块物理端子排的概览，供界面列出 向上/向下 按钮。

    普通格式的所有端子段在同一块物理端子排（默认行）；坐标格式按横坐标分块。
    """
    blocks, _ = parse_terminals(text or "")
    return strips_from_blocks(blocks)


def strips_from_blocks(blocks: list[dict]) -> list[dict]:
    """把已解析的端子段按物理端子排汇总，界面已有解析结果时不必再解析一遍。"""
    strips: dict[str, dict] = {}
    for block in blocks:
        key = block.get("physicalGroup") or "默认行"
        strip = strips.setdefault(key, {"key": key, "names": [], "terminals": 0})
        label = block.get("label", block["name"])
        if label not in strip["names"]:
            strip["names"].append(label)
        strip["terminals"] += len(block["terminals"])
    return [{"key": strip["key"], "name": "、".join(strip["names"]), "terminals": strip["terminals"]}
            for strip in strips.values()]


def describe_blocks(blocks: list[dict], with_numbers: bool = False) -> list[str]:
    out = []
    for block in blocks:
        merged = "，%d 段接成一块" % block.get("segments", 1) if block.get("segments", 1) > 1 else ""
        if with_numbers:
            out.append("%s：%s%s" % (block.get("label", block["name"]), "、".join(block["terminals"]) or "（没有端子号）", merged))
        else:
            out.append("%s（%d 个端子%s）" % (block.get("label", block["name"]), len(block["terminals"]), merged))
    return out


def duplicate_terminal_warnings(blocks: list[dict]) -> list[str]:
    """坐标格式保留重复端子号（普通格式直接报错），提醒接线只会落在第一次出现的那格。"""
    warnings = []
    for block in blocks:
        seen: set[str] = set()
        repeated: list[str] = []
        for number in block["terminals"]:
            if number in seen and number not in repeated:
                repeated.append(number)
            seen.add(number)
        if repeated:
            warnings.append("端子排「%s」里端子号 %s 出现了不止一次，每格都照画，接线接在第一次出现的那格"
                            % (block.get("label", block["name"]), "、".join(repeated)))
    return warnings


def compact_reference(text: str) -> str:
    return re.sub(r"[-_:：\s]+", "", text or "").upper()


def separator_positions(label: str) -> set[int]:
    """名称里分隔符（连字符、冒号、空白）在去掉分隔符后的位置，如 QD-COM 为 {2}。"""
    positions, length = set(), 0
    for part in re.split(r"[-_:：\s]+", label.strip())[:-1]:
        length += len(part)
        positions.add(length)
    return positions


def reference_matches(reference: str, blocks: list[dict]):
    """列出引用能对上的全部 (名称, 端子排, 端子号)，名称长的排前面；一个都对不上时附原因。

    忽略冒号、连字符和空白后按名称前缀匹配，较长名称优先（X251 是 X2 的 51 号）。
    写了冒号时冒号必须落在名称末尾，或落在名称自带的分隔符上：X2:1 只能是 X2 的 1 号，
    X:21 只能是 X 的 21 号，互不串用；QD:COM1 的冒号对上 QD-COM 里的连字符，仍是它的 1 号。
    """
    text = (reference or "").strip()
    if not text:
        return [], "缺少端子号"
    compact_text = compact_reference(text)
    colon = re.search(r"[:：]", text)
    colon_at = len(compact_reference(text[:colon.start()])) if colon else None
    # 按块出现顺序去重再按长度排，同长名称的先后才稳定，不受集合遍历顺序影响。
    labels = sorted(dict.fromkeys(block.get("label", block["name"]) for block in blocks),
                    key=lambda label: len(compact_reference(label)), reverse=True)
    matches: list[tuple[str, str, str]] = []
    reason = "「%s」对不上任何端子排" % text
    for label in labels:
        compact_name = compact_reference(label)
        if not compact_name or not compact_text.startswith(compact_name):
            continue
        if colon_at is not None and colon_at != len(compact_name) and colon_at not in separator_positions(label):
            continue
        wanted = compact_text[len(compact_name):]
        if not wanted:
            reason = "「%s」只有端子排名称，缺端子号" % text
            continue
        found = [(label, block["name"], number) for block in blocks if block.get("label", block["name"]) == label
                 for number in block["terminals"] if compact_reference(number) == wanted]
        if not found:
            reason = "%s 没有 %s 号端子" % (label, wanted)
        matches.extend(dict.fromkeys(found))
    return matches, "" if matches else reason


def resolve_reference(reference: str, blocks: list[dict]):
    """把 1-2ID:4、QD:COM1 等引用拆成端子排名称和端子号；有多种拆法时取名称最长的。"""
    matches, reason = reference_matches(reference, blocks)
    if not matches:
        return None, reason
    _, block, terminal = matches[0]
    return {"block": block, "terminal": terminal}, ""


def looks_like_reference(token: str, blocks: list[dict]) -> bool:
    """只有确实能对应现有端子的文字才算下一条接线的开头。

    原理号可能恰好以某个端子排名称开头，例如 JD-901；仅按前缀判断会把它
    错拆成 JD 的 901 号端子。每行第一项仍由 parse_wiring 单独校验输入错误。
    """
    resolved, error = resolve_reference(token, blocks)
    return resolved is not None and not error


def split_wiring_records(text: str, blocks: list[dict]) -> list[list[str]]:
    """把接线文字切成一条条记录。
    先按换行和分号切，再在每一段里遇到「端子排名称+端子号」的项就另起一条，
    这样整段用顿号连着粘过来也能自动断开。空的原理号（连着两个顿号）保留。"""
    records: list[list[str]] = []

    def push(chunk: list[str]) -> None:
        while chunk and not chunk[-1]:
            chunk.pop()
        if chunk:
            records.append(chunk)

    for unit in [u.strip() for u in RECORD_SPLIT.split(text or "") if u.strip()]:
        current: list[str] = []
        for token in [part.rstrip("。.!！") for part in split_fields(unit)]:
            if current and looks_like_reference(token, blocks):
                push(current)
                current = [token]
            else:
                current.append(token)
        push(current)
    return records


def parse_wiring(text: str, blocks: list[dict], from_cabinet: str, prefix: str):
    """一条记录「端子号、原理号[、去向柜]」；没写去向柜的并入后面第一条写了去向柜的记录。"""
    if not (text or "").strip():
        return [], [], [], []
    rows: list[dict] = []
    errors: list[str] = []
    ambiguous: list[str] = []
    for index, fields in enumerate(split_wiring_records(text, blocks), start=1):
        record = "、".join(fields)
        reference = fields[0] if fields else ""
        principle = fields[1] if len(fields) > 1 else ""
        destination = fields[2] if len(fields) > 2 else ""
        if len(fields) > 3:
            errors.append("第 %d 条「%s」项数太多，一条只能写 端子号、原理号、去向柜" % (index, record))
            continue
        matches, error = reference_matches(reference, blocks)
        if error:
            errors.append("第 %d 条「%s」：%s" % (index, record, error))
            continue
        if len(matches) > 1:
            options = ["%s:%s" % (label, number) for label, _, number in matches]
            if len(set(options)) < len(options):
                options = ["%s:%s" % (name, number) for _, name, number in matches]
            ambiguous.append("第 %d 条「%s」可以理解成 %s，按 %s 接；要接别的请用冒号写明，如 %s"
                             % (index, reference, "、".join(options), options[0], options[1]))
        _, block, terminal = matches[0]
        rows.append({"index": index, "block": block, "terminal": terminal,
                     "principle": principle, "destination": destination})
    seen: dict[tuple, int] = {}
    repeated: list[str] = []
    for row in rows:
        key = (row["block"], row["terminal"])
        if key in seen:
            # 一个端子接两根线是真实存在的（短接、串接），只提醒不拦。
            repeated.append("第 %d 条和第 %d 条都接在 %s-%s 上，确认不是重复输入" % (row["index"], seen[key], key[0], key[1]))
        else:
            seen[key] = row["index"]
    cables: list[dict] = []
    bucket: list[dict] = []
    for row in rows:
        bucket.append(row)
        if row["destination"]:
            cables.append({"rows": bucket, "destination": row["destination"], "closed": True})
            bucket = []
    if bucket:
        cables.append({"rows": bucket, "destination": "", "closed": False})
    connections: list[dict] = []
    warnings: list[str] = ambiguous + repeated
    for order, cable in enumerate(cables, start=1):
        cable["number"] = "%s-%02d" % (prefix, order)
        if not cable["closed"]:
            warnings.append("最后 %d 条记录没有写去向柜，先单独算成一根电缆 %s" % (len(cable["rows"]), cable["number"]))
        for row in cable["rows"]:
            connections.append({
                "terminalBlock": row["block"], "terminal": row["terminal"], "principle": row["principle"],
                "fromCabinet": from_cabinet, "toCabinet": cable["destination"],
                "cableNumber": cable["number"], "cableSpec": "",
            })
    if not connections and not errors:
        errors.append("上面的接线还没填")
    return connections, cables, errors, warnings


# ==================== DXF 绘图 ====================

def natural_key(text: str):
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", str(text))]


def text_width(value, height: float, factor: float) -> float:
    total = 0.0
    for char in str(value):
        total += 1.0 if ord(char) >= CJK_START else 0.55
    return total * height * factor


def build_columns(params, drawing):
    name_w = drawing["blockNameWidth"]
    term_w = drawing["terminalWidth"]
    positions = {}
    layouts = []
    cursors = {}
    order = 0
    for block in params["terminalBlocks"]:
        row_key = block.get("physicalGroup") or "默认行"
        cursor = cursors.get(row_key, 0.0)
        terminals = [str(t) for t in block["terminals"]]
        start_x = cursor
        first_x = start_x + name_w
        for index, terminal in enumerate(terminals):
            x = first_x + index * term_w
            # 坐标格式允许同一块里端子号重复：每格照画，接线落在第一次出现的那格，不能让后一格覆盖前一格。
            positions.setdefault((block["name"], terminal), {
                "x": x, "center": x + term_w / 2.0, "order": order, "rowKey": row_key
            })
            order += 1
        width = name_w + max(1, len(terminals)) * term_w
        layouts.append({"name": block["name"], "label": block.get("label") or block["name"], "terminals": terminals,
                        "centers": [first_x + (index + 0.5) * term_w for index in range(len(terminals))],
                        "startX": start_x, "firstX": first_x, "width": width, "rowKey": row_key})
        cursors[row_key] = start_x + width
    return positions, layouts, max(cursors.values(), default=0.0)


def build_cables(params, positions, lead_spacing=1.0):
    groups = {}
    skipped = []
    for connection in params["connections"]:
        number = str(connection.get("cableNumber") or "").strip()
        key = (connection.get("terminalBlock"), str(connection.get("terminal")))
        if not number:
            skipped.append("%s-%s 缺少电缆号" % key)
            continue
        if key not in positions:
            skipped.append("%s-%s 端子不存在" % key)
            continue
        row_key = positions[key]["rowKey"]
        point = dict(connection)
        point["terminalKey"] = key
        groups.setdefault((number, row_key), []).append(point)
    cables = []
    for (number, row_key), points in groups.items():
        points = sorted(points, key=lambda p: positions[(p["terminalBlock"], str(p["terminal"]))]["center"])
        leftmost = min(positions[(p["terminalBlock"], str(p["terminal"]))]["order"] for p in points)
        cables.append({
            "number": number,
            "rowKey": row_key,
            "points": points,
            "leftmost": leftmost,
            "destination": next((p.get("toCabinet") for p in points if p.get("toCabinet")), ""),
            "spec": next((p.get("cableSpec") for p in points if p.get("cableSpec")), ""),
        })
    cables.sort(key=lambda cable: (cable["rowKey"], cable["leftmost"], natural_key(cable["number"])))

    # 同一端子接多根电缆时，引线以端子中心为基准等距展开。
    # 电缆按最终绘图顺序从近到远排列，因此最靠左的引线也对应最近的电缆。
    usage_counts = {}
    for cable in cables:
        for point in cable["points"]:
            key = point["terminalKey"]
            usage_counts[key] = usage_counts.get(key, 0) + 1
    usage_indexes = {}
    for cable in cables:
        for point in cable["points"]:
            key = point["terminalKey"]
            index = usage_indexes.get(key, 0)
            count = usage_counts[key]
            point["leadOffset"] = (index - (count - 1) / 2.0) * lead_spacing
            point["leadX"] = positions[key]["center"] + point["leadOffset"]
            usage_indexes[key] = index + 1
    return cables, skipped


def build_dxf(params, drawing, output: Path, wiring_output: Path | None = None):
    import ezdxf
    from ezdxf.enums import TextEntityAlignment

    text_h = float(params.get("textHeight") or 3)
    factor = float(drawing["widthFactor"])
    color = int(drawing["color"])
    layers = drawing["layers"]
    term_w = drawing["terminalWidth"]
    zone_h = drawing["terminalZoneHeight"]
    band_h = drawing["numberBandHeight"]
    block_h = zone_h * 2 + band_h
    default_dir = str(params.get("direction", "DOWN")).upper()
    directions = {str(key): str(value).upper() for key, value in (params.get("directions") or {}).items()}

    positions, layouts, total_w = build_columns(params, drawing)
    terminal_count = sum(len(layout["terminals"]) for layout in layouts)
    row_keys = list(dict.fromkeys(layout["rowKey"] for layout in layouts))
    physical_block_count = len(row_keys)
    cables, skipped = build_cables(params, positions, drawing.get("terminalLeadSpacing", 1.0))
    cell_text_h = min(text_h, term_w - 1.5)
    if cell_text_h < text_h:
        skipped.append("端子排内文字高度由 %s 收敛到 %s，避免超出 %s 宽的端子格" % (text_h, cell_text_h, term_w))

    step = max(float(params.get("distanceStep") or 5), text_h * 1.6)
    first = float(params.get("firstDistance") or 10)
    row_cables = {row_key: [cable for cable in cables if cable["rowKey"] == row_key] for row_key in row_keys}
    row_plans = {}
    cursor_y = 0.0
    row_gap = drawing.get("physicalRowGap", 20.0)
    for row_key in reversed(row_keys):
        count = len(row_cables[row_key])
        span = first + max(0, count - 1) * step if count else 0.0
        up = directions.get(row_key, default_dir) == "UP"
        block_y = cursor_y if up else cursor_y + span
        pin_y = block_y + block_h if up else block_y
        row_plans[row_key] = {"blockY": block_y, "pinY": pin_y, "span": span, "up": up}
        cursor_y += block_h + span + row_gap

    bus_x = total_w + drawing["trunkExtend"]
    number_x = bus_x + drawing["columnGap"]
    marker_tip_x = number_x - drawing["bianNumberOffset"]
    line_right_x = marker_tip_x + drawing["bianRightOffset"]
    spec_x = marker_tip_x + drawing["bianSpecOffset"]
    right_x = max(
        line_right_x,
        max([marker_tip_x + drawing["bianToOffset"] + text_width("至" + str(c["destination"] or "未填写"), text_h, factor)
             for c in cables] or [0]),
        max([spec_x + text_width(c["spec"] or "未填写", text_h, factor) for c in cables] or [0]),
    )

    doc = ezdxf.new("R2010")
    doc.header["$INSUNITS"] = 4
    style_name = drawing["style"]
    style = doc.styles.new(style_name) if style_name not in doc.styles else doc.styles.get(style_name)
    style.dxf.font = drawing["font"]
    style.dxf.bigfont = drawing["bigfont"]
    style.dxf.width = factor
    for layer_name in layers.values():
        if layer_name not in doc.layers:
            doc.layers.new(layer_name, dxfattribs={"color": color})
    # 锁层只防止修改；跨 CAD 的纯接线复制由独立 DXF 保证。
    for role in ("frame", "text", "title"):
        doc.layers.get(layers[role]).lock()
    msp = doc.modelspace()

    def line(p1, p2, layer, entity_color=None):
        msp.add_line(p1, p2, dxfattribs={
            "layer": layer,
            "color": color if entity_color is None else entity_color,
        })

    def closed_polyline(points, layer):
        msp.add_lwpolyline(points, close=True, dxfattribs={"layer": layer, "color": color})

    def text(value, x, y, layer, align=None, rotation=0.0, height=None, entity_color=None):
        content = CONTROL_CHARS.sub(" ", str(value or "")).strip()
        if not content:
            return
        entity = msp.add_text(content, dxfattribs={
            "style": style_name, "height": height or text_h, "width": factor,
            "color": color if entity_color is None else entity_color,
            "layer": layer, "rotation": rotation,
        })
        entity.set_placement((x, y), align=align or TextEntityAlignment.MIDDLE_CENTER)

    for layout in layouts:
        block_y = row_plans[layout["rowKey"]]["blockY"]
        band_y0 = block_y + zone_h
        band_y1 = band_y0 + band_h
        number_center = band_y0 + band_h / 2.0
        start_x = layout["startX"]
        first_x = layout["firstX"]
        end_x = start_x + layout["width"]
        line((start_x, block_y), (end_x, block_y), layers["frame"])
        line((end_x, block_y), (end_x, block_y + block_h), layers["frame"])
        line((end_x, block_y + block_h), (start_x, block_y + block_h), layers["frame"])
        line((start_x, block_y + block_h), (start_x, block_y), layers["frame"])
        line((first_x, block_y), (first_x, block_y + block_h), layers["frame"])
        line((first_x, band_y0), (end_x, band_y0), layers["frame"])
        line((first_x, band_y1), (end_x, band_y1), layers["frame"])
        for index in range(1, len(layout["terminals"])):
            x = first_x + index * term_w
            line((x, block_y), (x, block_y + block_h), layers["frame"])
        text(layout["label"], start_x + drawing["blockNameWidth"] / 2.0, block_y + block_h / 2.0,
             layers["text"], rotation=90.0, height=cell_text_h)
        for terminal, center in zip(layout["terminals"], layout["centers"]):
            text(terminal, center, number_center, layers["text"], rotation=90.0, height=cell_text_h)

    principles = {}
    for connection in params["connections"]:
        key = (connection.get("terminalBlock"), str(connection.get("terminal")))
        if key in positions and connection.get("principle") and key not in principles:
            principles[key] = connection["principle"]
    for key, value in principles.items():
        row_key = positions[key]["rowKey"]
        plan = row_plans[row_key]
        # 旋转 90 度后，MIDDLE_LEFT / MIDDLE_RIGHT 分别锚定文字的下端 / 上端；
        # 不能用 MIDDLE_CENTER，否则原理号始终会落在端子区中间。
        if plan["up"]:
            principle_y = plan["blockY"] + block_h - 2.0
            principle_align = TextEntityAlignment.MIDDLE_RIGHT
        else:
            principle_y = plan["blockY"] + 2.0
            principle_align = TextEntityAlignment.MIDDLE_LEFT
        text(value, positions[key]["center"], principle_y, layers["principle"], align=principle_align, rotation=90.0,
             height=cell_text_h, entity_color=drawing.get("principleColor", 4))

    triangle_half = drawing["bianTriangleSide"] / 2.0
    triangle_depth = triangle_half * (3.0 ** 0.5)
    label_y_offset = drawing["bianTextYOffset"]
    for row_key in row_keys:
      plan = row_plans[row_key]
      pin_y = plan["pinY"]
      up = plan["up"]
      for index, cable in enumerate(row_cables[row_key]):
        offset = first + index * step
        y = pin_y + offset if up else pin_y - offset
        xs = [p["leadX"] for p in cable["points"]]
        for x in xs:
            line((x, pin_y), (x, y), layers["wire"])
            if up:
                # SJ：在竖直引线最上端添加 1 x 1 mm 上接斜短线。
                line((x + 1.0, y), (x, y - 1.0), layers["wire"])
            else:
                # XJ：在竖直引线最下端添加 1 x 1 mm 下接斜短线。
                line((x + 1.0, y), (x, y + 1.0), layers["wire"])
        line((min(xs), y), (line_right_x, y), layers["wire"], drawing.get("cableLineColor", 3))
        closed_polyline([
            (marker_tip_x, y),
            (marker_tip_x - triangle_depth, y + triangle_half),
            (marker_tip_x - triangle_depth, y - triangle_half),
        ], layers["wire"])
        text(cable["number"], marker_tip_x + drawing["bianNumberOffset"], y + label_y_offset,
             layers["label"], align=TextEntityAlignment.LEFT)
        text("至" + str(cable["destination"] or "未填写"), marker_tip_x + drawing["bianToOffset"], y + label_y_offset,
             layers["label"], align=TextEntityAlignment.LEFT)
        text(cable["spec"] or "未填写", spec_x, y + label_y_offset,
             layers["label"], align=TextEntityAlignment.LEFT)

    top_y = max(plan["blockY"] + block_h + (plan["span"] if plan["up"] else 0.0) for plan in row_plans.values())
    bottom_y = min(plan["blockY"] - (plan["span"] if not plan["up"] else 0.0) for plan in row_plans.values())
    title_y = top_y + text_h * 3.0
    if drawing["drawTitle"]:
        label = str(params.get("projectName") or "")
        text(label, 0.0, title_y, layers["title"], align=TextEntityAlignment.MIDDLE_LEFT, height=text_h * 1.5)
        up_count = sum(1 for plan in row_plans.values() if plan["up"])
        down_count = len(row_plans) - up_count
        if up_count and down_count:
            direction_note = "向上 %d 块 / 向下 %d 块" % (up_count, down_count)
        elif up_count:
            direction_note = "全部向上"
        else:
            direction_note = "全部向下"
        note = "%d 块端子排 / %d 个端子 / %d 根电缆 / %s" % (
            physical_block_count, terminal_count, len(cables), direction_note)
        text(note, right_x, title_y, layers["title"], align=TextEntityAlignment.MIDDLE_RIGHT)
    if drawing["drawFrame"]:
        margin = text_h * 2.0
        x0, y0 = -margin, bottom_y - margin
        x1, y1 = right_x + margin, title_y + text_h * 2.0
        for p1, p2 in (((x0, y0), (x1, y0)), ((x1, y0), (x1, y1)), ((x1, y1), (x0, y1)), ((x0, y1), (x0, y0))):
            line(p1, p2, layers["frame"])

    output.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(output)
    if wiring_output is not None:
        wiring_layers = {layers[role] for role in ("principle", "wire", "label")}
        for entity in list(msp):
            if entity.dxf.layer not in wiring_layers:
                msp.delete_entity(entity)
        wiring_output.parent.mkdir(parents=True, exist_ok=True)
        doc.saveas(wiring_output)
    return {"blocks": physical_block_count, "segments": len(layouts), "terminals": terminal_count, "cables": len(cables),
            "points": sum(len(c["points"]) for c in cables), "skipped": skipped,
            "rows": [{"key": key, "up": plan["up"], "blockY": plan["blockY"], "pinY": plan["pinY"], "span": plan["span"]}
                     for key, plan in row_plans.items()],
            "size": (round(right_x, 1), round(title_y + text_h - bottom_y, 1))}


def verify(output: Path, drawing) -> str:
    """回读校验：样式、实体数量、每个文字的宽度因子。返回一句话结论。"""
    import ezdxf
    doc = ezdxf.readfile(output)
    msp = doc.modelspace()
    texts = list(msp.query("TEXT"))
    lines = list(msp.query("LINE"))
    bad = [t for t in texts if abs(float(t.dxf.get("width", 1.0)) - float(drawing["widthFactor"])) > 1e-9]
    ok = drawing["style"] in doc.styles and not bad and texts and lines
    return "已校验：%d 条线、%d 个文字，样式 %s%s" % (
        len(lines), len(texts), drawing["style"], "" if ok else "（校验异常，请检查）")


def desktop_dir() -> Path:
    try:
        import winreg
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            candidate = Path(winreg.QueryValueEx(key, "Desktop")[0])
            if candidate.is_dir():
                return candidate
    except Exception:
        pass
    home = Path.home() / "Desktop"
    return home if home.is_dir() else Path(__file__).resolve().parent


def safe_name(value: str) -> str:
    # Windows 文件名不能含控制符、不能以点或空格结尾，也不能是 CON/NUL 等保留名。
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", (value or "").strip()).rstrip(". ")
    if re.fullmatch(r"(?i)(con|prn|aux|nul|com\d|lpt\d)(\..*)?", name):
        name = "_" + name
    return name[:120] or "端子排"


def generate(payload: dict) -> dict:
    blocks, block_errors = parse_terminals(payload.get("terminals", ""))
    # 柜名同时用于图签和文件名，粘贴带进来的换行、制表符一律并成单个空格。
    cabinet = " ".join((payload.get("cabinet") or "").split())
    prefix = (payload.get("prefix") or DEFAULTS["cablePrefix"]).strip() or DEFAULTS["cablePrefix"]
    connections, cables, wire_errors, warnings = parse_wiring(payload.get("wiring", ""), blocks, cabinet, prefix)
    errors = block_errors + wire_errors
    if errors:
        # 一次只让人改前几条，剩下的多半是同一个原因
        shown = errors[:10]
        if len(errors) > len(shown):
            shown.append("……还有 %d 条类似问题，先把上面这些改掉再点一次" % (len(errors) - len(shown)))
        return {"ok": False, "errors": shown, "parsed": describe_blocks(blocks, with_numbers=True)}
    params = {
        "projectName": cabinet or "端子排接线图",
        "direction": payload.get("direction") or DEFAULTS["direction"],
        "directions": payload.get("directions") or {},
        "firstDistance": DEFAULTS["firstDistance"],
        "distanceStep": DEFAULTS["distanceStep"],
        "textHeight": DEFAULTS["textHeight"],
        "terminalBlocks": blocks,
        "connections": connections,
    }
    folder = Path(payload["outputDir"]) if payload.get("outputDir") else desktop_dir()
    base = safe_name(cabinet) + "-端子排" if cabinet else "端子排"
    target = folder / (base + ".dxf")
    wiring_target = folder / (base + "-仅接线.dxf")
    try:
        result = build_dxf(params, DRAWING, target, wiring_target)
    except OSError:
        # 同名图多半正开在 CAD 里被占用，换带时间戳的新名字再写一次。
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = folder / ("%s-%s.dxf" % (base, stamp))
        wiring_target = folder / ("%s-%s-仅接线.dxf" % (base, stamp))
        try:
            result = build_dxf(params, DRAWING, target, wiring_target)
        except OSError as error:
            return {"ok": False, "errors": ["无法写入 %s：%s" % (folder, error)],
                    "parsed": describe_blocks(blocks, with_numbers=True)}
    return {
        "ok": True,
        "path": str(target),
        "wiringPath": str(wiring_target),
        "folder": str(target.parent),
        "verify": verify(target, DRAWING),
        "warnings": duplicate_terminal_warnings(blocks) + warnings + result["skipped"],
        "size": "%s × %s mm" % result["size"],
        "parsed": describe_blocks(blocks),
        "stats": {"blocks": result["blocks"], "terminals": result["terminals"],
                  "cables": result["cables"], "points": result["points"]},
        "cables": [{"number": c["number"], "destination": c["destination"] or "未填写",
                    "points": ["%s-%s" % (r["block"], r["terminal"]) for r in c["rows"]]} for c in cables],
    }


EXAMPLE_TERMINALS = "ZD、1、11\n1-2ID、1、2、3、4\nJD、1、4\n1ID、1、2、3、6\n1-2UD、1、2、3、4、6"
EXAMPLE_WIRING = "\n".join([
    "ZD1、+KM1",
    "ZD11、-KM1、直流馈线柜",
    "1-2ID4、1(2)B-N4121",
    "1-2ID1、1(2)B-A4121",
    "1-2ID2、1(2)B-B4121",
    "1-2ID3、1(2)B-C4121、35kV 1(2)#主变进线柜",
    "JD1、L",
    "JD4、N、相邻屏柜",
    "1ID1、1(2)B-A4111",
    "1ID2、1(2)B-B4111",
    "1ID3、1(2)B-C4111",
    "1ID6、1(2)B-N4111、35kV 1(2)#主变进线柜",
    "1-2UD1、A610",
    "1-2UD2、B610",
    "1-2UD3、C610",
    "1-2UD4、N600",
    "1-2UD6、L610、35kV PT柜",
])
EXAMPLE_CABINET = "35kV 1#主变保护柜"
