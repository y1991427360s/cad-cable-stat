"""计算参数：名称、默认值、说明和取值范围集中登记在 PARAM_SPECS，读取、校验、界面编辑、模板都从这里取。

新增参数只需在 PARAM_SPECS 加一行，计算代码里用 params["参数名"] 读取即可。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .csvio import read_csv_dicts, write_csv_dicts
from .text import normalize_text, parse_float

PARAMS_FILE = "参数.csv"
PARAMS_HEADERS = ["参数", "值", "备注"]


@dataclass(frozen=True)
class ParamSpec:
    name: str
    default: float
    note: str
    minimum: float | None = None
    maximum: float | None = None
    exclusive_min: bool = False

    def check(self, value: float) -> str:
        """返回错误说明，合法时返回空串。"""
        if self.minimum is not None:
            if self.exclusive_min and value <= self.minimum:
                return f"必须大于 {self.minimum:g}"
            if not self.exclusive_min and value < self.minimum:
                return f"不能小于 {self.minimum:g}"
        if self.maximum is not None and value > self.maximum:
            return f"不能大于 {self.maximum:g}"
        return ""


PARAM_SPECS: tuple[ParamSpec, ...] = (
    ParamSpec("CAD每米单位", 1000.0, "图按米画填1，按厘米画填100，按毫米画填1000", 0.0, None, True),
    ParamSpec("竖井高度", 4.5, "跨楼层经竖井加的高度，单位米（竖井.csv 里单独填了高度的以那个为准）", 0.0),
    ParamSpec("不拐弯修正", 7.0, "路径不拐弯时加的余量，单位米"),
    ParamSpec("拐弯倍率", 1.2, "拐弯或跨楼层时先加余量再乘的倍率", 0.0, None, True),
    ParamSpec(
        "吸附容差", 0.05,
        "单位跟图纸一致；线头相距在此范围内视为连通。端点都用捕捉画的保持默认即可，线头有小缝隙时再调大（毫米图最多几十）",
        0.0,
    ),
    ParamSpec("最大接入距离", 20000.0, "单位跟图纸一致；柜子/竖井到最近路径超过此值报错，厘米图约2000、毫米图约20000", 0.0, None, True),
    ParamSpec("跨房间修正", 3.0, "电缆跨房间或出房间时额外增加的长度，单位米"),
    ParamSpec("拐弯判定角度", 5.0, "路径方向偏转超过此角度（度）才算拐弯，用来忽略画线时的微小歪斜", 0.0, 90.0),
    ParamSpec("差值提醒", 5.0, "自动统计与手工长度相差超过此值（米）时在结果里标黄提醒，填0不提醒", 0.0),
    ParamSpec(
        "断点提示距离", 0.5,
        "路径线头离另一条路径线不到此距离（米）却没连上时，提示“疑似断点”并在路径可视化里画红圈，填0不检查",
        0.0,
    ),
)

PARAM_SPEC_BY_NAME = {spec.name: spec for spec in PARAM_SPECS}


def default_params() -> dict[str, float]:
    return {spec.name: spec.default for spec in PARAM_SPECS}


def load_params_with_warnings(data_dir: Path) -> tuple[dict[str, float], list[str]]:
    """读取 参数.csv；非法值、未知参数名给出告警并沿用默认值，不中断计算。"""
    params = default_params()
    warnings: list[str] = []
    for row in read_csv_dicts(data_dir / PARAMS_FILE):
        key = normalize_text(row.get("参数"))
        if not key:
            continue
        raw = normalize_text(row.get("值"))
        value = parse_float(raw)
        spec = PARAM_SPEC_BY_NAME.get(key)
        if value is None:
            if raw:
                warnings.append(f"参数提示：参数.csv 里“{key}”的值“{raw}”不是数字，已按默认值 {params.get(key, '')} 计算")
            continue
        if spec is None:
            warnings.append(f"参数提示：参数.csv 里有未知参数“{key}”（可能写错了名字），可用参数：{'、'.join(PARAM_SPEC_BY_NAME)}")
            params[key] = value
            continue
        problem = spec.check(value)
        if problem:
            warnings.append(f"参数提示：参数“{key}”={value:g} {problem}，已按默认值 {spec.default:g} 计算")
            continue
        params[key] = value
    return params, warnings


def load_params(data_dir: Path) -> dict[str, float]:
    params, _ = load_params_with_warnings(data_dir)
    # CAD每米单位填错会让全部长度差上千倍，这一项不静默回退，直接报错让用户改
    for row in read_csv_dicts(data_dir / PARAMS_FILE):
        if normalize_text(row.get("参数")) == "CAD每米单位":
            value = parse_float(row.get("值"))
            if value is not None and value <= 0:
                raise ValueError("参数“CAD每米单位”必须大于0")
    return params


def save_params(data_dir: Path, values: dict[str, float]) -> None:
    """写回 参数.csv：保留已有行顺序和备注，已登记的参数补齐说明，文件里没有的参数追加到末尾。"""
    path = data_dir / PARAMS_FILE
    existing = read_csv_dicts(path)
    rows: list[dict[str, object]] = []
    written: set[str] = set()
    for row in existing:
        key = normalize_text(row.get("参数"))
        if not key or key in written:
            continue
        note = normalize_text(row.get("备注")) or (PARAM_SPEC_BY_NAME[key].note if key in PARAM_SPEC_BY_NAME else "")
        rows.append({"参数": key, "值": format_param_value(values.get(key, row.get("值"))), "备注": note})
        written.add(key)
    for spec in PARAM_SPECS:
        if spec.name not in written and spec.name in values:
            rows.append({"参数": spec.name, "值": format_param_value(values[spec.name]), "备注": spec.note})
    write_csv_dicts(path, rows, PARAMS_HEADERS)


def format_param_value(value: object) -> str:
    number = parse_float(value)
    if number is None:
        return normalize_text(value)
    return str(int(number)) if number.is_integer() and abs(number) < 1e15 else repr(number)
