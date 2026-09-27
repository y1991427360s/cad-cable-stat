"""统一 EXE 的可复现验收：合成电缆工程、端子出图、资源与成果回读。

构建脚本通过 ``python -m tools.workbench_smoke prepare|verify`` 使用；
EXE 自检通过 ``run_terminal_smoke`` 使用。不创建 Tk，不启动 CAD。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

EXPECTED_CABLES = {"SMOKE-直线": 17.0, "SMOKE-拐弯": 32.4}
TERMINAL_REPORT = "smoke_validation.json"
TERMINAL_INPUT = "X、1、2、3"
WIRING_INPUT = "X:1、A610、甲柜\nX:2、B903、乙柜\nX:3、701、丙柜"


def require(condition: Any, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _write_csv(path: Path, headers: list[str], rows: list[list[Any]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(headers)
        writer.writerows(rows)


def prepare_project(project: Path) -> None:
    """只创建全新的隔离工程，不覆盖用户工程或上次验收产物。"""
    from openpyxl import Workbook

    project.mkdir(parents=True, exist_ok=False)
    data = project / "data"
    data.mkdir()
    workbook_path = project / "自动统计.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "电缆清册"
    sheet.append(["电缆编号", "起点", "终点", "电缆长度", "型号"])
    sheet.append(["SMOKE-直线", "甲柜清册名", "乙柜", 17, "测试型号"])
    sheet.append(["SMOKE-拐弯", "甲柜", "丙柜", 32.4, "测试型号"])
    workbook.save(workbook_path)
    workbook.close()
    _write_csv(data / "柜子坐标.csv", ["柜子名称", "楼层", "X", "Y", "图层", "对象类型", "句柄"], [
        ["甲柜", "1F", 0, 0, "CABLE_CAB_1F", "TEXT", "A"],
        ["乙柜", "1F", 10000, 0, "CABLE_CAB_1F", "TEXT", "B"],
        ["丙柜", "1F", 10000, 10000, "CABLE_CAB_1F", "TEXT", "C"],
    ])
    _write_csv(data / "路径线段.csv", ["路径编号", "楼层", "起点X", "起点Y", "终点X", "终点Y", "长度_CAD单位", "图层", "句柄", "段号"], [
        ["R1", "1F", 0, 0, 10000, 0, 10000, "CABLE_ROUTE_1F", "10", "1"],
        ["R2", "1F", 10000, 0, 10000, 10000, 10000, "CABLE_ROUTE_1F", "11", "1"],
    ])
    _write_csv(data / "参数.csv", ["参数", "值", "备注"], [
        ["CAD每米单位", 1000, "合成毫米图"], ["不拐弯修正", 7, "合成测试"],
        ["拐弯倍率", 1.2, "合成测试"], ["吸附容差", 0.05, "合成测试"],
    ])
    _write_csv(data / "柜名别名.csv", ["清册名称", "CAD名称", "备注"], [["甲柜清册名", "甲柜", "冒烟别名"]])
    (project / "source.sha256").write_text(hashlib.sha256(workbook_path.read_bytes()).hexdigest(), encoding="ascii")
    print(f"冒烟工程：{project}")


def verify_terminal_outputs(project: Path, result: dict[str, Any]) -> dict[str, Any]:
    """回读完整图/仅接线图，校验目标目录、可编辑实体、字体和锁层。"""
    import ezdxf

    from modules.terminal.duanzi_dxf_tool import DRAWING

    output_dir = (project / "outputs" / "端子排").resolve()
    documents = []
    files = []
    for key in ("path", "wiringPath"):
        path = Path(result[key]).resolve()
        require(path.parent == output_dir, f"端子成果未写入当前工程：{path}")
        require(path.is_file() and path.stat().st_size > 0, f"端子成果为空或不存在：{path}")
        doc = ezdxf.readfile(path)
        require(not doc.audit().has_errors, f"DXF 结构审计失败：{path.name}")
        counts = Counter(entity.dxftype() for entity in doc.modelspace())
        require(counts["LINE"] > 0 and counts["TEXT"] > 0, f"DXF 缺少 LINE/TEXT：{path.name}")
        require("HZ" in doc.styles, f"DXF 缺少 HZ 文字样式：{path.name}")
        require(all(entity.dxf.style == "HZ" and math.isclose(entity.dxf.width, 0.7)
                    for entity in doc.modelspace().query("TEXT")), f"DXF 文字格式改变：{path.name}")
        documents.append(doc)
        files.append({"path": str(path), "size": path.stat().st_size, "entities": dict(counts)})
    full, wiring = documents
    layers = DRAWING["layers"]
    allowed = {layers[role] for role in ("principle", "wire", "label")}
    for role in ("frame", "text", "title"):
        require(full.layers.get(layers[role]).is_locked(), f"完整图背景图层未锁定：{layers[role]}")
    for layer in allowed:
        require(not full.layers.get(layer).is_locked() and not wiring.layers.get(layer).is_locked(),
                f"接线图层被错误锁定：{layer}")
    actual = list(wiring.modelspace())
    require({entity.dxf.layer for entity in actual} == allowed, "仅接线图包含非接线对象或缺少接线图层")
    expected = [entity for entity in full.modelspace() if entity.dxf.layer in allowed]
    require([entity.dxfattribs() for entity in expected] == [entity.dxfattribs() for entity in actual],
            "仅接线图的实体或坐标与完整图不一致")
    principles = {entity.dxf.text for entity in actual if entity.dxf.layer == layers["principle"]}
    require(principles == {"A610", "B903", "701"}, "原理号文字与验收输入不一致")
    return {"ok": True, "files": files, "path": result["path"], "wiringPath": result["wiringPath"]}


def run_terminal_smoke(project: Path, log: Callable[[str], None] = print) -> dict[str, Any]:
    """由统一 EXE 自检入口调用；异常交给调用方写入统一自检日志。"""
    from modules.terminal.service import TerminalService

    state_dir = project / "data" / "terminal"
    # 自检也可能由开发者对已有工程启动；不得把业务输入换成验收输入。
    original = {
        state_dir / name: (state_dir / name).read_bytes() if (state_dir / name).exists() else None
        for name in ("端子排.txt", "接线.txt", "settings.json")
    }
    service = TerminalService(project)
    try:
        result = service.generate({
            "terminals": TERMINAL_INPUT, "wiring": WIRING_INPUT,
            "cabinet": f"验收柜-{uuid4().hex}", "direction": "DOWN", "directions": {},
        })
    finally:
        for path, content in original.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(content)
    require(result.get("ok"), f"端子生成失败：{result.get('errors', result)}")
    report = verify_terminal_outputs(project, result)
    (project / "outputs" / "端子排" / TERMINAL_REPORT).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log("端子自检通过：完整图、仅接线图、ezdxf 回读、LINE/TEXT、HZ、锁层和接线坐标一致。\n")
    return report


def verify_project(project: Path, executable: Path, source_root: Path) -> None:
    from openpyxl import load_workbook
    from PyInstaller.archive.readers import CArchiveReader

    archive = CArchiveReader(str(executable))
    names = {name.replace("\\", "/"): name for name in archive.toc}
    embedded = ["app_icon.ico", "cad_cable_wizard.lsp", "cable_stat/visualizer.html"]
    embedded += [path.relative_to(source_root).as_posix()
                 for path in (source_root / "项目模板").rglob("*") if path.is_file()]
    for name in embedded:
        require(name in names, f"缺少打包资源：{name}")
        require(archive.extract(names[name]) == (source_root / name).read_bytes(), f"内嵌资源不是当前版本：{name}")
    log = (project / "电缆统计_自检.log").read_text(encoding="utf-8")
    require("自检通过" in log and "自检失败" not in log, "EXE 自检日志没有报告通过")
    source = project / "自动统计.xlsx"
    require(hashlib.sha256(source.read_bytes()).hexdigest() == (project / "source.sha256").read_text(encoding="ascii"),
            "原始工作簿被改写")
    outputs = project / "outputs"
    with (outputs / "统计明细.csv").open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    require(len(rows) == len(EXPECTED_CABLES), "电缆数量不符合预期")
    require({row["电缆编号"] for row in rows} == set(EXPECTED_CABLES), "电缆编号不符合预期")
    for row in rows:
        require(row["状态"] == "OK", f"电缆计算失败：{row}")
        require(math.isclose(float(row["自动统计"]), EXPECTED_CABLES[row["电缆编号"]], abs_tol=1e-8), f"电缆长度不符合预期：{row}")
    workbook = load_workbook(outputs / "自动统计_计算结果.xlsx", data_only=True)
    try:
        require({"统计明细", "柜子清单", "参数", "问题清单"}.issubset(workbook.sheetnames), "结果工作簿缺少工作表")
        sheet = workbook["电缆清册"]
        headers = {cell.value: cell.column for cell in sheet[1]}
        for row_no in (2, 3):
            cable_id = sheet.cell(row_no, headers["电缆编号"]).value
            require(sheet.cell(row_no, headers["自动统计"]).value == EXPECTED_CABLES[cable_id], "工作簿长度不符合预期")
    finally:
        workbook.close()
    visual = json.loads((outputs / "路径可视化数据.json").read_text(encoding="utf-8"))
    require(len(visual["cables"]) == len(EXPECTED_CABLES), "可视化电缆数量不符合预期")
    html = (outputs / "路径可视化.html").read_text(encoding="utf-8")
    require("__VISUAL_DATA_JSON__" not in html and "SMOKE-" in html, "可视化数据未内嵌")
    terminal_report = json.loads((outputs / "端子排" / TERMINAL_REPORT).read_text(encoding="utf-8"))
    require(terminal_report.get("ok"), "EXE 端子自检未通过")
    verify_terminal_outputs(project, terminal_report)
    print("冒烟验收通过：内嵌资源、两条电缆长度、Excel/CSV/HTML/JSON、两份 DXF、原始清册未被改写。")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "verify", "terminal"))
    parser.add_argument("project", type=Path)
    parser.add_argument("executable", type=Path, nargs="?")
    parser.add_argument("source_root", type=Path, nargs="?")
    args = parser.parse_args()
    if args.mode == "prepare":
        prepare_project(args.project)
    elif args.mode == "terminal":
        run_terminal_smoke(args.project)
    else:
        if args.executable is None or args.source_root is None:
            parser.error("verify 需要 executable 和 source_root")
        verify_project(args.project, args.executable, args.source_root)


if __name__ == "__main__":
    main()
