# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

本仓库另有 `AGENTS.md`（编码风格、提交规范、数据注意事项），与本文件保持一致；修改约定时两边同步。面向使用者的文档是 `快速上手（最简版）.md`（步骤版）和 `使用说明.md`（详细版），改动图层约定、参数或运行方式时同步更新这两份。

## 项目概述

电缆长度自动统计工具：根据 CAD 导出的柜子坐标、固定走线路径（电缆沟/桥架中心线）和电缆竖井数据，自动计算 `自动统计.xlsx` 中每条电缆的路径长度，并生成统计结果与路径可视化页面。

**工具/项目分离**：本目录只放通用工具代码；每个工程项目一个独立文件夹（含 `自动统计.xlsx`、`data\`、`outputs\`、`一键启动.cmd`），可建在 `项目\<项目名>\` 或任何位置（用户实际把项目放在工程目录树里，如 `E:\366256\2025年\...\110 青啤五厂\03 输出\电缆统计\`）。`项目模板\` 是新项目骨架；建新项目用 `电缆统计.exe` 的「新建项目」按钮或 `新建项目.cmd`。项目里的 `一键启动.cmd` 按绝对路径 start 本目录的 `电缆统计.exe`。不要往本目录根放任何项目数据。计算规则：路径不拐弯按 CAD 固定路径最短长度 +7m，路径拐弯/跨楼层按 `(CAD固定路径最短长度 + 7m) × 1.2`，跨层经竖井计入竖井高度。

## 常用命令

```powershell
# 安装唯一依赖
python -m pip install openpyxl

# 一键运行完整流程（在项目文件夹里查找 自动统计.xlsx，输出到项目的 outputs/）
# 等价于双击项目文件夹里的 一键启动.cmd
.\运行自动统计.ps1 -Project "<项目文件夹>"     # 等价于 python .\run_auto_stat.py <项目文件夹>

# 直接调试核心脚本（定位参数、路径、柜名问题时用）
python .\calculate_cable_lengths.py --workbook "<项目文件夹>\自动统计.xlsx" --data-dir "<项目文件夹>\data" --output "<项目文件夹>\outputs\自动统计_计算结果.xlsx" --make-cabinet-checklist

# 运行轻量测试（pytest 风格，也可直接 python 执行）
python .\test_calculate_cable_lengths.py

# 提交前最低限度的语法检查
python -m py_compile .\calculate_cable_lengths.py .\run_auto_stat.py
```

注意：本机 Python 输出中文依赖 `PYTHONUTF8=1`（已设为 Windows 用户环境变量）。

## 架构

数据流：**CAD → CSV → Python 计算 → Excel + HTML 可视化**

1. `cad_export_cable_route.lsp` — 在 CAD 中执行 `DDFD_EXPORT_CABLE_ROUTE`，从约定图层导出 `柜子坐标.csv`、`路径线段.csv`、`竖井.csv`、`房间范围.csv`。`cad_cable_wizard.lsp` 是自包含向导（含 `DDFD_CABLE_ROOMS` 房间定义和完整导出逻辑），用户侧只需加载它。房间边界复制到 `CABLE_ROOM_*F` 并以 XData 保存名称；导出时遍历数据库实体、按图层或 XData 识别房间，直接读取 DXF 顶点和闭合位，避免 ZWCAD 曲线 COM/选集差异。两份 LISP 的导出逻辑必须同步，且当前两份均为 **GBK + CRLF**，必须字节级读写并检查替换字符与括号平衡。`data/` 里另有两张人工维护表：`柜名别名.csv` 和 `强制规则.csv`。
2. `run_auto_stat.py` — 薄封装：第一个命令行参数是项目文件夹（缺省用当前目录；等于工具目录时报错引导；参数会清洗尾引号/尾反斜杠），在项目文件夹内按活动工作表表头（起点/终点/电缆长度，经 `normalize_header` 剔除全部空白后匹配）查找工作簿，直接 import 调用 `calculate_cable_lengths.main(argv)`（不再 subprocess，便于打包），data 和 outputs 都取项目文件夹下的。「自动统计」「向上取整」列可缺省，`load_workbook_rows` 会自动补建（「向上取整」固定在「自动统计」右边一列，存 `math.ceil(自动统计)`，右边已有列时插列并顺移）；「电缆编号」列必需。
3. `cable_stat_app.py` — tkinter 图形界面主程序，也是 `电缆统计.exe` 的打包入口：选/建项目、开始计算、打开结果/可视化、只导出完整向导 `cad_cable_wizard.lsp`；精简版 `cad_export_cable_route.lsp` 不再内嵌或导出给用户。窗口和 EXE 使用 `app_icon.ico`，项目模板作为资源内嵌。`CABLE_STAT_SMOKETEST=1` 走无界面自检。改 lsp/模板/任何 py 后都要按 README 的绝对路径命令重新打包并删除 `_build`；使用 `--specpath _build` 时资源路径不能写相对路径。
4. `calculate_cable_lengths.py` — 全部核心逻辑（单文件，约 1600 行）：
   - **数据加载**：`load_params`/`load_cabinets`/`load_segments`/`load_shafts`/`load_aliases`/`load_rule_overrides`/`load_workbook_rows`，dataclass：`Segment`/`Cabinet`/`Shaft`/`AttachPoint`。柜名经 `normalize_cabinet_name` 去掉全部空白字符后匹配；别名经 `apply_aliases` 直接注册进 cabinets 字典。
   - **`RouteGraph`** 类是核心：把路径线段端点按 `吸附容差` 吸附合并成节点（按楼层分组）；`connect_t_junctions` 把落在其他线段中间的端点接入（T型连接）；`connect_cross_junctions` 检测同层线段内部十字交叉/斜交并在交点处切分打通共享节点；`add_attachment` 把柜子/竖井垂直投影到最近线段（`make_attachment` + `project_to_segment`，超过 `最大接入距离` 报错）；`finalize_route_edges` 按沿线距离排序切分线段为边；同名竖井（`ZJ1_1F`/`ZJ1_2F` → base_id `ZJ1`）跨楼层加竖直边；`shortest_path` 是 Dijkstra（`calculate_rows` 内按起终点节点对缓存，反向复用）。
   - **规则判定**：`is_route_bent` 通过路径边方向向量变化或是否经过竖井判断"拐弯"；`calculate_rows` 对不拐弯路径应用 +7，涉及拐弯/跨楼层应用先 +7 再 ×1.2；强制规则表优先级最高（正反向均命中）。
   - **校验告警**：`build_graph` 输出问题清单——柜名缺失、竖井编号后缀与楼层不一致、竖井未跨楼层配对、柜子缺楼层、路径网断块明细（分成几块、每块挂载哪些柜子/竖井）等；另有「参数提示」体检（吸附容差≥最短线段一半、柜子超出最大接入距离时给出建议值、路径总长换算后明显不合理时提示核对 CAD每米单位），`main()` 会把参数提示同时打印到运行日志；`make_cabinet_check_rows` 对缺坐标柜子用 difflib 给出 `近似CAD柜名建议`。
   - **输出**：`write_results_to_workbook` 写结果工作簿（Sheet1 填值 + 统计明细/柜子清单/参数/问题清单 sheet）；`VISUALIZER_HTML_TEMPLATE`（内嵌的大段 HTML/JS 模板）+ `build_visualization_data` 生成 `路径可视化.html` 和 `路径可视化数据.json`。
   - 所有长度先以 CAD 单位计算，经 `CAD每米单位` 换算成米。

## 关键约定

- 表头/列名/参数名全部用中文业务名（`起点`、`终点`、`柜子名称`、`竖井高度` 等），不要英文化。
- 柜子名称是 CAD 文字与 Excel 起终点匹配的唯一键；柜名里的普通空格、换行、制表符和全角空格会自动剔除，单纯空格差异不需要别名；其他对不上时优先在 `data/柜名别名.csv` 加映射（参考柜子清单的 `近似CAD柜名建议` 列），不要轻易改图。
- 路径连通性支持端点吸附、T 型连接（端点搭在另一条线段中间）以及十字/斜向交叉连接（两条线段在内部相交时自动切分打通），纯空间无交点不连通。
- 新增可调参数放 `data/参数.csv`（`load_params` 读取），脚本里给合理默认值。
- 默认按毫米图配置：`CAD每米单位=1000`、`吸附容差=0.05`、`最大接入距离=20000`；项目模板与内置默认值必须同步。
- AutoLISP 命令保持 `DDFD_` 前缀，图层名沿用 `CABLE_*` 规则。
- 不要覆盖原始 `自动统计.xlsx`，结果一律写项目文件夹的 `outputs/`。
- 改 `项目模板\` 里的文件时注意：已建项目不会自动同步，必要时手动更新各项目（含各项目的 `一键启动.cmd`）；模板改动后**必须重新打包 exe**（模板是内嵌资源）。`一键启动.cmd` 和 `新建项目.cmd` 都是 GBK 编码（含中文路径/提示，双击时控制台按 936 解析），内容是 `cd /d "%~dp0"` 后 `start` 绝对路径的 `电缆统计.exe`（GUI 检测到当前目录是项目文件夹就自动开算）。cmd 里传 `"%~dp0"` 会因尾反斜杠转义引号而产生坏路径——启动脚本靠 cd 后不传参数来规避。
- 所有中文 CSV（`data/` 和 `outputs/`）必须是**带 BOM 的 UTF-8（utf-8-sig）**，否则 Excel 双击打开乱码。脚本的 `write_csv_dicts` 已用 utf-8-sig；手工新建 CSV 时不要用裸 UTF-8 写入，用 `encoding='utf-8-sig'`。
- `.dwg`/`.dwl` 是工程图基准文件，勿随意修改；`~$*.xlsx` 锁文件勿提交。

## 验证

先跑 `python test_calculate_cable_lengths.py`（覆盖图构建、T型连接、十字交叉、拐弯判定、别名表、强制规则）。改动计算逻辑后再跑一遍完整流程，检查 `outputs/统计明细.csv` 的 `状态`、`规则`、`基础路径长度`、`差值`，查看结果工作簿的 `问题清单`，并打开 `路径可视化.html` 确认高亮路径、楼层切换、详情栏、三维旋转/平移/缩放正常。新增独立算法在该测试文件补用例。
