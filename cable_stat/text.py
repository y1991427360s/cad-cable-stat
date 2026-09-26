"""文本、楼层、数值的归一化工具：CAD 文字与 Excel 清册之间一切“名字对得上”的规则都在这里。"""

from __future__ import annotations

import math
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

# 全角 ASCII（！到～）与半角一一对应，清册里常见“１＃主变”这类全角写法
_FULLWIDTH_TABLE = {code: code - 0xFEE0 for code in range(0xFF01, 0xFF5F)}
_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_NUMBER = "零〇一二两三四五六七八九十"
# 与 openpyxl 的非法控制字符范围一致；制表、换行和回车仍按原有空白规则归一化。
_ILLEGAL_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def normalize_text(value: Any) -> str:
    text = "" if value is None else str(value)
    text = _ILLEGAL_CONTROL_CHARS.sub("", text)
    text = text.replace("　", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_cabinet_name(value: Any) -> str:
    """柜名匹配键：去掉全部空白字符，全角字母数字符号转半角。"""
    text = normalize_text(value).translate(_FULLWIDTH_TABLE)
    return re.sub(r"[\s 　]+", "", text)


def normalize_header(value: Any) -> str:
    """表头匹配：剔除全部空白（含单元格内换行），兼容设计院清册常见的「电缆↵长度」写法。"""
    return normalize_cabinet_name(value)


def chinese_number(text: str) -> int | None:
    """解析 1~99 的中文数字（一、十二、二十、二十三），也接受阿拉伯数字。"""
    if text.isdigit():
        return int(text)
    if not text or any(ch not in _CN_NUMBER for ch in text):
        return None
    if "十" not in text:
        return _CN_DIGITS[text] if len(text) == 1 else None
    tens, _, ones = text.partition("十")
    if len(tens) > 1 or len(ones) > 1:
        return None
    return (_CN_DIGITS[tens] if tens else 1) * 10 + (_CN_DIGITS[ones] if ones else 0)


def _floor_label(number: int, basement: bool) -> str:
    return f"B{number}F" if basement else f"{number}F"


def normalize_floor(value: Any) -> str:
    """楼层统一写成 1F、12F；地下层写成 B1F。支持 1 / F1 / 一层 / 十二楼 / B1 / -1F / 地下一层。"""
    text = normalize_text(value).upper().replace(" ", "")
    if not text:
        return ""
    match = re.fullmatch(r"F?(\d+)F?", text)
    if match:
        return _floor_label(int(match.group(1)), False)
    match = re.fullmatch(r"(?:B|-|负)(\d+)F?|F?B(\d+)", text)
    if match:
        return _floor_label(int(match.group(1) or match.group(2)), True)
    match = re.search(r"(地下|负)?([零〇一二两三四五六七八九十]+|\d+)[层楼]", text)
    if match:
        number = chinese_number(match.group(2))
        if number is not None:
            return _floor_label(number, bool(match.group(1)))
    return text


def infer_floor_from_layer(layer: str) -> str:
    """从图层名推断楼层：CABLE_ROUTE_2F→2F，CABLE_ROUTE_B1F→B1F，电缆沟二层→2F。"""
    text = normalize_text(layer).upper()
    match = re.search(r"(?<![A-Z0-9])B(\d+)F", text)
    if match:
        return _floor_label(int(match.group(1)), True)
    match = re.search(r"(地下|负)?([零〇一二两三四五六七八九十]+)[层楼]", text)
    if match:
        number = chinese_number(match.group(2))
        if number is not None:
            return _floor_label(number, bool(match.group(1)))
    match = re.search(r"(\d+)F", text)
    if match:
        return f"{match.group(1)}F"
    return ""


def floor_sort_key(floor: str) -> tuple[int, str]:
    """地下层在最下面（B2F < B1F < 1F < 2F），认不出的楼层排最后。"""
    match = re.fullmatch(r"B(\d+)F", floor)
    if match:
        return -int(match.group(1)), floor
    match = re.search(r"\d+", floor)
    if match:
        return int(match.group()), floor
    return 999, floor


def parse_float(value: Any, default: float | None = None) -> float | None:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else default
    text = normalize_text(value)
    if text == "":
        return default
    try:
        number = float(text)
    except ValueError:
        return default
    return number if math.isfinite(number) else default


def parse_length(value: Any) -> float | None:
    """手工长度宽松解析：数字、"12"、"12.5m"、"12米"、"1,200" 都算；公式、文字说明返回 None。"""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if math.isfinite(value) else None
    text = normalize_cabinet_name(value).replace(",", "")
    text = re.sub(r"(?i)(m|米)$", "", text)
    return parse_float(text)


def round_half_up(value: float, digits: int = 1) -> float:
    """四舍五入（Python 的 round 是银行家舍入，7.25 会得到 7.2，与 Excel 不一致）。"""
    try:
        quantum = Decimal(1).scaleb(-digits)
        # 先抹掉二进制浮点噪声（23.449999999999996 视为 23.45），再按四舍五入进位
        return float(Decimal(repr(round(value, 9))).quantize(quantum, rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError):
        return round(value, digits)
