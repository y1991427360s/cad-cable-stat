# 电气二次设计工作台 — 项目约定

`AGENTS.md` 与 `CLAUDE.md` 保存同一套项目规则，修改后保持逐字一致。使用与架构说明见 `README.md`，CAD 详细步骤见 `使用说明.md`，入门见 `快速上手（最简版）.md`。

## 产品与模块边界

本仓库以 `cad-cable-stat` 为母项目，把 `duanziHTML` 的端子解析和绘图接入同一个 Windows tkinter/ttk 工作台。正式入口是 `app.py`，正式交付物是 `电气二次设计工作台.exe`。不得启动旧端子 GUI、第二个 `tk.Tk` 或第二个 EXE 来拼接功能；不引入 HTTP 服务、数据库或云存储。

- `cable_stat_app.py`：工程创建、资源导出、全局配置、单实例交接及自检；`cable_stat_gui.py`：唯一主窗口和电缆页面；`ui/workbench_pages.py`：统一导航、问题和成果页面。
- `cable_stat/`：原电缆引擎，计算在 `calc.py`、图构建在 `graph.py`、流程在 `pipeline.py`、HTML 模板为 `visualizer.html`。`calculate_cable_lengths.py`、`run_auto_stat.py` 是兼容入口，不是完整算法所在文件。
- `modules/terminal/duanzi_dxf_tool.py`：原解析、布局及 DXF 核心；`service.py`：工程状态和输出目录适配；`page.py`：嵌入式端子页面。界面不得承接绘图算法。
- `core/models.py`：统一问题和后续业务实体接口；V1 保留 Excel/CSV/TXT/JSON/DXF 的文件存储方式。

## 工程、数据与编码

工具与工程分离：根目录只放通用源码和发布程序；每个工程一个文件夹，可位于 `项目/<工程名>/` 或其他位置。`项目模板/` 是骨架。旧电缆工程可直接打开，缺少端子数据时显示空输入。

- 电缆输入：源清册、`data/参数.csv`、`柜子坐标.csv`、`路径线段.csv`、`竖井.csv`、可选 `房间范围.csv`；`柜名别名.csv`、`强制规则.csv` 是人工校正，CAD 再导出不得覆盖。
- 端子输入：`data/terminal/端子排.txt`、`接线.txt`、`settings.json`，包括柜名和方向。业务输入必须随工程保存，不可写到旧端子全局 `state.json`。成功生成、切换工程前、关闭前保存；保存失败不得继续切换或关闭。三个输入须成组保存；任一替换失败还原已替换文件，回滚失败保留 `.save-backup-*.bak` 原件并报告路径。
- 全局 `%APPDATA%/电气二次设计工作台/config.json` 只保存最近工程、窗口等应用设置；兼容读取旧电缆配置。测试用 mock 隔离配置，不改真实用户状态。
- 不覆盖原始 `自动统计.xlsx`；电缆成果写入 `outputs/`，端子完整图及仅接线图写入 `outputs/端子排/`。目标被 CAD/Excel 占用时沿用时间戳退避文件名。
- 中文 CSV 统一 `utf-8-sig`；端子业务 TXT/JSON 用 UTF-8。修改数据前分清 CAD 导出、人工校正和测试样例。`.dwg`、`.dwl` 是原图资料，勿随意改；Office 锁文件和 CAD 备份不提交。
- 生成的 `一键启动.cmd` 与 `新建项目.cmd` 使用 GBK + CRLF。工程启动器先 `cd /d "%~dp0"` 再启动程序，不把带尾反斜杠的 `"%~dp0"` 作为参数传入。程序迁移位置后需刷新工程启动器；模板改动不会自动同步已建工程。

## GUI 与线程

- 只允许主线程读取或修改 Tk 对象，包括变量和 `after()`；工作线程只向 `queue.Queue` 投递结果，由主线程定时取回。启动工作线程前在主线程 `gc.collect()`，避免 Tk 变量在后台被析构。
- 电缆计算和端子生成共用忙碌锁；运行期间禁止重复生成、并行运行另一模块或切换工程。后台异常必须结束忙碌状态并回显；无控制台 EXE 的 `report_callback_exception` 要显示错误。
- `F5`、`Ctrl+Enter` 按当前页面路由。端子两个 Text 自身也要绑定 `Ctrl+Enter` 并返回 `"break"`，避免 `<Return>` 类绑定先插入换行。
- 方向按端子排名称及同名出现次序（`#2`）记忆，不按易变的位置键；默认方向改动应用到全部块。DPI 感知在创建 Tk 前启用，布局保留缩放和滚动能力。

## 电缆与 CAD 合同

- 表头、参数名保持中文业务名。活动清册必需 `电缆编号`、`起点`、`终点`、`电缆长度`，表头空白自动忽略；缺少 `自动统计`、`向上取整` 时在结果工作簿补列。
- 柜名忽略空白后匹配，其他差异通过别名表人工确认。默认毫米图参数：`CAD每米单位=1000`、`吸附容差=0.05`、`最大接入距离=20000`；新参数放参数表并同步内置默认值与模板。
- 保留端点吸附、T 型接入、同层内部十字/斜交切分、同名竖井跨层、最短路径、别名和强制规则。默认直线基础长度 +7m；拐弯/跨层为（基础长度 +7m）×1.2，竖井高度已计入基础长度；跨房间修正在倍率后加。不得为整合修改已验证的计算行为。
- 用户只导出和加载完整 `cad_cable_wizard.lsp`；`cad_export_cable_route.lsp` 保留为开发兼容源码，不内嵌。两份导出逻辑必须同步，`test_room_export_contract.py` 检查共享函数逐字一致。
- 两份 LISP 严格按 GBK + CRLF 字节级读写，检查替换字符和括号平衡；修改后同时更新 `*ddfd-cable-version*`。命令使用 `DDFD_` 前缀，图层使用 `CABLE_*`。
- 房间导出遍历实体读取 DXF 顶点与闭合位，不依赖 ZWCAD 曲线 COM 返回格式；仅认带 `DDFD_CABLE_ROOM` XData 名称的多段线。定义房间不得改 CLAYER。CSV 临时生成后成组替换，读取失败不得覆盖原数据。

## 端子解析与出图合同

- 沿用原核心解析与出图行为。坐标输入名称只认行末 `端子名`；未标注文字按端子号保留，无法归属必须报错。普通/坐标格式不能混用，两个混用方向都要检查。
- 坐标格式重复端子号每格保留，接线连首次出现的位置并提示；普通格式重复报错。保留 `QD-COM`、`X/X2`、D 结尾名称、普通同名段合并及连续接线；不得把 `JD-901` 等原理号误拆为端子。
- `ZD2`、`ZD:2`、`ZD：2` 等价；`QD:COM1` 引用 `QD-COM` 的 1 号。显式冒号严格拆分，`X2:1` 不得回退为 `X:21`；无冒号歧义沿用最长名称并提示。
- DXF R2010，1 单位 = 1mm；端子格节距 5mm、总高 75mm；默认向下，各行电缆距离 10、15、20…mm；多电缆引线居中以 1mm 展开，左侧对应较近电缆；向上保留 SJ、向下保留 XJ 短线。
- 文字 HZ（`txt.shx` + `hztxt.shx`）、常规字高 3、宽度因子 0.7；原理号 ACI 4、水平电缆线 ACI 3，其余 ACI 7。BIAN 三角尖点在电缆右端左侧 40mm，编号在左 20mm，去向从右 1.5mm 起，规格在右 32mm。
- 完整图背景框线/名称/编号/图签锁层，接线层可编辑；仅接线图只含接线对象，与完整图坐标一致。保持可编辑 LINE/TEXT 实体，不栅格化、不依赖 CAD 进程出图。

## 运行、验证与发布

```powershell
python -m pip install -r .\requirements.txt
python .\app.py
python -m test_calculate_cable_lengths
python -m unittest discover -v
.\运行自动统计.ps1 -Project "<工程文件夹>"
powershell -NoProfile -ExecutionPolicy Bypass -File .\build_exe.ps1
```

- Python 使用 4 空格缩进、`pathlib.Path`、类型标注和 dataclass。当前构建环境为 Python 3.14，固定依赖见 `requirements.txt`；构建脚本不自动安装或升级依赖。
- 电缆基线必须独立运行，不能只用 unittest discovery 代替。计算改动还应跑典型清册并查明细、问题清单和路径可视化；端子改动要跑原核心、工程存储、嵌入页面及联合回归，并用 ezdxf 回读成果。
- 修改任意 py/lsp/模板后必须重打包并用 `CABLE_STAT_SMOKETEST=1` 自检。`build_exe.ps1` 先语法/导入/测试，打包到 `_build_release/dist/`，用独立合成工程验收电缆、DXF、内嵌资源与源清册哈希；全部通过才备份并替换正式 EXE。失败保留原版。
- 冒烟也可能对已有工程运行：端子验收必须在 finally 恢复三个业务输入的原字节（原来不存在的文件删除），使用唯一验收柜名，不覆盖已有 DXF。`test_workbench_smoke.py` 覆盖此约束。兼容日志名为 `电缆统计_自检.log`，端子报告为 `outputs/端子排/smoke_validation.json`。
- PyInstaller 资源参数用绝对路径；正式包为单文件无控制台并内嵌图标、完整 LISP、项目模板、HTML 及依赖资源。不能以 EXE 字符串搜索代替实际自检。
- 提交使用简短祈使句；说明影响的输入、验证与工程参数依据。工程数据、EXE、构建目录、临时锁文件不入库。产品名称、按钮、数据目录或运行方式变更时同步使用文档。
