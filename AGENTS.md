# Repository Guidelines

## 项目结构与模块组织

本仓库是电缆长度自动统计工具，采用**工具/项目分离**结构：根目录只放通用工具代码，每个工程项目一个独立文件夹（默认在 `项目\<项目名>\`，含 `自动统计.xlsx`、`data\`、`outputs\` 和调用工具的 `一键启动.cmd`）；`项目模板\` 是新项目骨架。`calculate_cable_lengths.py` 负责计算，`run_auto_stat.py` 是项目入口，`cable_stat_app.py` 打包为 `电缆统计.exe`。用户侧只内嵌和导出完整向导 `cad_cable_wizard.lsp` 与项目模板；精简版 `cad_export_cable_route.lsp` 仅保留为开发兼容源码。改任何 py/lsp/模板后必须重新打包并用 `CABLE_STAT_SMOKETEST=1` 自检。两份 LISP 的导出逻辑必须同步，均按 GBK + CRLF 字节级读写；房间导出应遍历实体并读取 DXF 顶点/闭合位，不能依赖 ZWCAD 的曲线 COM 返回格式；只认带 `DDFD_CABLE_ROOM` XData 名称的多段线，定义房间不得改 CLAYER；改 LISP 同时更新 `*ddfd-cable-version*`。

项目文件夹的 `data/` 存放 `参数.csv`、`柜子坐标.csv`、`路径线段.csv`、`竖井.csv`、可选的 `房间范围.csv`，以及人工维护的 `柜名别名.csv` 和 `强制规则.csv`。默认按毫米图配置：`CAD每米单位=1000`、`吸附容差=0.05`、`最大接入距离=20000`。项目文件夹的 `outputs/` 存放计算结果、统计明细、问题清单和路径可视化。`.dwg` 和 `.dwl` 是工程图基准文件，修改前确认是否属于当前工程基准。

## 构建、测试与本地运行命令

```powershell
python -m pip install openpyxl
```

安装唯一明确依赖。

```powershell
.\运行自动统计.ps1 -Project "<项目文件夹>"
```

对指定项目文件夹运行完整统计流程，输出到该项目的 `outputs/`（等价于双击项目文件夹的 `一键启动.cmd`）。

```powershell
python .\calculate_cable_lengths.py --workbook "<项目文件夹>\自动统计.xlsx" --data-dir "<项目文件夹>\data" --output "<项目文件夹>\outputs\自动统计_计算结果.xlsx"
```

直接调试核心计算脚本，适合定位参数、路径或柜名问题。

```powershell
python -m py_compile .\calculate_cable_lengths.py .\run_auto_stat.py
```

提交前至少做语法检查。

## 编码风格与命名约定

Python 使用 4 空格缩进、`pathlib.Path`、类型标注和 `dataclass`，延续现有函数式组织方式。CSV/Excel 表头保留中文业务名，例如 `起点`、`终点`、`自动统计`、`柜子名称`。新增参数优先放入 `data/参数.csv`，脚本中提供合理默认值。AutoLISP 命令保持 `DDFD_` 前缀，图层名沿用 `CABLE_*` 规则。

## 验证指南

当前测试入口是 `test_calculate_cable_lengths.py`，可用 `python test_calculate_cable_lengths.py` 或 `python -m pytest` 运行，覆盖图构建、T型连接、拐弯判定、别名表和强制规则。修改计算逻辑后先跑测试，再用一份小范围典型清册运行完整流程，检查 `统计明细.csv` 中 `状态`、`规则`、`基础路径长度` 和 `差值` 是否符合预期；同时查看结果工作簿的 `问题清单`，并打开 `路径可视化.html` 确认路径高亮、楼层切换、详情栏和三维交互正常。新增独立算法时在该测试文件中补用例。

## 提交与 Pull Request 指南

当前目录没有 Git 历史可归纳提交规范。建议使用简短祈使句提交信息，例如 `fix route projection tolerance` 或 `add shaft pairing validation`。PR 需说明变更目的、影响的输入文件或图层约定、验证命令和关键输出截图或 CSV 摘要。不要把临时 Excel 锁文件（`~$*.xlsx`）或无关 CAD 备份文件加入变更。

## 数据与配置注意事项

不要覆盖原始 `自动统计.xlsx`；结果应写入 `outputs/`。所有中文 CSV 必须是带 BOM 的 UTF-8（utf-8-sig），否则 Excel 双击打开乱码；脚本写出已是 utf-8-sig，手工新建时同样用 `encoding='utf-8-sig'`。修改 `data/*.csv` 前先确认来源是 CAD 导出、人工校正还是样例数据。调整 `CAD每米单位`、`竖井高度`、修正量等参数时，在 PR 描述中说明工程依据。
