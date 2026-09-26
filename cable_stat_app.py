"""电缆长度自动统计 - 图形界面版入口（打包成单文件 exe 的入口）。

- 双击 exe：打开界面（窗口代码在 cable_stat_gui.py），选择/新建项目文件夹后点「开始计算」。
- 从项目文件夹的 一键启动.cmd 或 CAD 向导进来（第一个参数或当前目录是项目文件夹）：自动选中并直接开算；
  已经开着一个窗口时把项目交给那个窗口去算，不再开第二个。
- 环境变量 CABLE_STAT_SMOKETEST=1 时走无界面自检：对参数或当前目录的项目算一遍，
  日志写到项目里的 电缆统计_自检.log，用于打包后回归验证。

本模块不导入 tkinter：自检、测试和 `import cable_stat_app` 都不碰图形界面。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
import traceback
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from cable_stat import __version__
from cable_stat.pipeline import RunResult, print_report, run
from cable_stat.project import RESULT_WORKBOOK, find_workbook

APP_TITLE = "电缆长度自动统计"
APP_ICON_FILE = "app_icon.ico"
LSP_FILES = ["cad_cable_wizard.lsp"]
LEGACY_EXPORTED_LSP_FILES = ["cad_export_cable_route.lsp"]
LAUNCHER_NAME = "一键启动.cmd"
SMOKE_LOG_NAME = "电缆统计_自检.log"
CONFIG_DIR_NAME = "电缆统计"
MAX_RECENT = 8

# 单实例：已有窗口时，新启动的进程把项目写进交接文件后退出，由已运行的窗口认领并开算
INSTANCE_MUTEX = "Local\\DianlanCableStatGUI"
HANDOFF_FILE = "handoff_request.json"
HANDOFF_MAX_AGE = 60.0
HANDOFF_WAIT = 4.0

_INVALID_NAME_CHARS = '\\/:*?"<>|'
_RESERVED_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL"] + [f"COM{i}" for i in range(1, 10)] + [f"LPT{i}" for i in range(1, 10)]
)
_instance_handles: list[int] = []


def resource_dir() -> Path:
    """打包后资源在 PyInstaller 解压目录，源码运行时就是本目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).resolve().parent


def exe_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve()
    return Path(__file__).resolve()


def tool_dir() -> Path:
    """工具所在文件夹（exe 所在目录；源码运行时是本目录），不放项目数据。"""
    return exe_path().parent


def path_key(path: Path | str) -> str:
    """比较路径用：绝对化 + 忽略大小写和斜杠差异。"""
    return os.path.normcase(os.path.abspath(str(path)))


def same_path(a: Path | str | None, b: Path | str | None) -> bool:
    return a is not None and b is not None and path_key(a) == path_key(b)


def clean_path_arg(raw: str) -> str:
    # cmd 里 "%~dp0" 的尾反斜杠会把引号转义进参数；引号不可能出现在合法路径里
    text = raw.strip().strip('"').rstrip("\\")
    return text + "\\" if text.endswith(":") else text


# ---------------- 配置 ----------------


def config_dir() -> Path:
    base = Path(os.environ.get("APPDATA") or Path.home()) / CONFIG_DIR_NAME
    base.mkdir(parents=True, exist_ok=True)
    return base


def config_file() -> Path:
    return config_dir() / "config.json"


def load_config() -> dict[str, Any]:
    try:
        data = json.loads(config_file().read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def save_config(cfg: dict[str, Any]) -> None:
    """先写临时文件再替换，两个窗口同时保存也不会留下半截 JSON。"""
    try:
        path = config_file()
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        pass


def recent_projects(cfg: dict[str, Any]) -> list[str]:
    """最近项目（最近的在前）：去重、剔除已不存在的文件夹，最多 MAX_RECENT 个；兼容只有 last_project 的旧配置。"""
    stored = cfg.get("recent_projects")
    candidates = [cfg.get("last_project")] + (list(stored) if isinstance(stored, list) else [])
    result: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        if not isinstance(item, str) or not item.strip():
            continue
        key = path_key(item)
        if key in seen:
            continue
        seen.add(key)
        if Path(item).is_dir():
            result.append(item)
            if len(result) >= MAX_RECENT:
                break
    return result


def remember_project(cfg: dict[str, Any], project: Path | str) -> list[str]:
    """把项目放到最近列表最前面并记为 last_project（只改 cfg，由调用方保存）。"""
    text = str(project)
    items = [text] + [p for p in recent_projects(cfg) if not same_path(p, text)]
    cfg["recent_projects"] = items[:MAX_RECENT]
    cfg["last_project"] = text
    return cfg["recent_projects"]


# ---------------- 项目文件夹 ----------------


def validate_project_name(name: str) -> str:
    """新项目名称能否当 Windows 文件夹名：合法返回空串，否则返回中文原因。"""
    if not name or not name.strip():
        return "请输入项目名称"
    if name != name.strip():
        return "名称首尾不能有空格"
    bad = sorted({ch for ch in name if ch in _INVALID_NAME_CHARS or ord(ch) < 32})
    if bad:
        shown = " ".join(ch if ord(ch) >= 32 else "控制字符" for ch in bad)
        return f"名称里有不能用的字符：{shown}（\\ / : * ? \" < > | 都不行）"
    if name.endswith("."):
        return "名称不能以句点结尾"
    if name.split(".")[0].strip().upper() in _RESERVED_NAMES:
        return f"“{name}”是 Windows 保留名称，请换一个"
    if len(name) > 120:
        return "名称太长（最多 120 个字）"
    return ""


def check_new_project(parent_dir: Path | str, name: str) -> str:
    """新建项目前的完整检查（名称 + 上级文件夹 + 重名）；合法返回空串。"""
    problem = validate_project_name(name)
    if problem:
        return problem
    text = str(parent_dir).strip().strip('"')
    if not text:
        return "请选择上级文件夹"
    parent = Path(text)
    if not parent.is_dir():
        return "上级文件夹不存在"
    if same_path(parent, tool_dir()):
        return "不要建在工具文件夹根目录，请选“项目”子文件夹或其他位置"
    if (parent / name).exists():
        return "这个位置已有同名文件夹"
    return ""


def write_launcher(project_dir: Path) -> None:
    """在项目文件夹生成 一键启动.cmd（GBK + CRLF），指向当前 exe（源码运行时指向 python 入口）。

    不能把 "%~dp0" 当参数传给 exe：尾反斜杠会转义引号；cd 进项目后不带参数启动，界面按当前目录认项目。
    """
    exe = exe_path()
    if exe.suffix.lower() == ".exe":
        content = f'@echo off\r\ncd /d "%~dp0"\r\nstart "" "{exe}"\r\n'
    else:
        tool = Path(__file__).resolve().parent
        content = f'@echo off\r\ncd /d "%~dp0"\r\npython "{tool}\\run_auto_stat.py"\r\necho.\r\npause\r\n'
    (project_dir / LAUNCHER_NAME).write_bytes(content.encode("gbk"))


def create_project(parent_dir: Path, name: str) -> Path:
    problem = validate_project_name(name)
    if problem:
        raise ValueError(problem)
    project_dir = Path(parent_dir) / name
    if project_dir.exists():
        raise FileExistsError(f"项目已存在：{project_dir}")
    template = resource_dir() / "项目模板"
    if template.is_dir():
        shutil.copytree(template, project_dir, ignore=shutil.ignore_patterns("~$*", "__pycache__"))
        launcher = project_dir / LAUNCHER_NAME
        if launcher.exists():
            launcher.unlink()
    else:
        (project_dir / "data").mkdir(parents=True)
    write_launcher(project_dir)
    return project_dir


def export_lsp(target_dir: Path) -> list[str]:
    """只导出完整向导；目标里旧版精简导出插件一并删掉，免得用户加载错。"""
    target_dir.mkdir(parents=True, exist_ok=True)
    for name in LEGACY_EXPORTED_LSP_FILES:
        legacy = target_dir / name
        if legacy.is_file():
            legacy.unlink()
    exported = []
    for name in LSP_FILES:
        src = resource_dir() / name
        if src.exists():
            shutil.copyfile(src, target_dir / name)
            exported.append(name)
    return exported


def looks_like_project(path: Path) -> bool:
    try:
        if not path.is_dir() or same_path(path, tool_dir()):
            return False
        if (path / "data").is_dir():
            return True
        find_workbook(path)
        return True
    except Exception:
        return False


def startup_target(argv: list[str] | None = None, cwd: Path | None = None) -> tuple[Path | None, bool]:
    """启动时打开哪个项目、要不要直接开算。

    第一个参数或当前目录像项目文件夹（一键启动.cmd / CAD 向导就是这样启动的）→ 选中并开算；
    参数是普通文件夹（比如把文件夹拖到 exe 上）→ 只选中；拖的是文件则取它所在文件夹。
    """
    argv = sys.argv if argv is None else argv
    raw = clean_path_arg(argv[1]) if len(argv) > 1 else ""
    if raw:
        try:
            path = Path(raw).resolve()
            if path.is_file():
                path = path.parent
            if looks_like_project(path):
                return path, True
            if path.is_dir() and not same_path(path, tool_dir()):
                return path, False
        except OSError:
            pass
    try:
        here = (cwd or Path.cwd()).resolve()
    except OSError:
        return None, False
    if looks_like_project(here):
        return here, True
    return None, False


# ---------------- 计算 ----------------


class _LogWriter:
    """把 print 输出转交给回调（界面日志 / 自检日志）。"""

    def __init__(self, log: Callable[[str], None]) -> None:
        self._log = log

    def write(self, text: str) -> int:
        if text:
            self._log(text)
        return len(text)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False


def calculate_project(
    project_dir: Path, log: Callable[[str], None],
    progress: Callable[[float, str], None] | None = None,
) -> RunResult:
    """对一个项目跑完整统计并打印标准运行摘要，print 输出全部交给 log；出错直接抛异常。

    redirect_stdout 是进程级的：界面线程不 print，所以放在工作线程里用没问题。
    """
    writer = _LogWriter(log)
    with redirect_stdout(writer), redirect_stderr(writer):
        workbook = find_workbook(project_dir)
        print(f"项目：{project_dir}")
        print(f"清册：{workbook.name}")
        result = run(workbook, project_dir / "data", project_dir / "outputs" / RESULT_WORKBOOK, progress=progress)
        print_report(result)
    return result


def run_calculation(project_dir: Path, log: Callable[[str], None]) -> bool:
    """跑一个项目的完整统计，所有 print 输出交给 log 回调。成功返回 True。"""
    try:
        calculate_project(project_dir, log)
        return True
    except Exception as exc:
        log("\n" + traceback.format_exc())
        log(f"\n计算失败：{exc}\n")
        return False


def smoke_test(argv: list[str] | None = None) -> int:
    argv = sys.argv if argv is None else argv
    raw = clean_path_arg(argv[1]) if len(argv) > 1 else ""
    project_dir = Path(raw).resolve() if raw else Path.cwd().resolve()
    lines = [f"{APP_TITLE} 自检  版本 {__version__}  {datetime.now():%Y-%m-%d %H:%M:%S}\n"]
    if project_dir.is_dir():
        ok = run_calculation(project_dir, lines.append)
    else:
        ok = False
        lines.append(f"项目文件夹不存在：{project_dir}\n")
    lines.append("\n自检通过\n" if ok else "\n自检失败\n")
    try:
        (project_dir if project_dir.is_dir() else Path.cwd()).joinpath(SMOKE_LOG_NAME).write_text(
            "".join(lines), encoding="utf-8"
        )
    except OSError:
        pass
    return 0 if ok else 1


# ---------------- 单实例交接 ----------------


def claim_single_instance(name: str = INSTANCE_MUTEX) -> bool | None:
    """创建命名互斥量（句柄留到进程结束）。True=已有窗口在运行，False=本进程是第一个，None=无法判断。"""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        ctypes.set_last_error(0)
        handle = kernel32.CreateMutexW(None, False, name)
        if not handle:
            return None
        already = ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS
        _instance_handles.append(handle)
        return already
    except Exception:
        return None


def handoff_file() -> Path:
    return config_dir() / HANDOFF_FILE


def write_handoff_request(project: Path, run_now: bool = True) -> Path:
    path = handoff_file()
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    payload = {"project": str(project), "run": run_now, "time": time.time(), "pid": os.getpid()}
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    return path


def claim_handoff_request(max_age: float = HANDOFF_MAX_AGE) -> dict[str, Any] | None:
    """已运行的窗口认领交接请求：先改名占为己有（两个进程抢同一个文件只有一个能成功），再读出删掉。"""
    try:
        path = handoff_file()
        claimed = path.with_name(f"{path.name}.{os.getpid()}.claimed")
        os.replace(path, claimed)
    except OSError:
        return None
    try:
        data = json.loads(claimed.read_text(encoding="utf-8"))
    except Exception:
        data = None
    finally:
        try:
            claimed.unlink()
        except OSError:
            pass
    if not isinstance(data, dict) or not isinstance(data.get("project"), str) or not data["project"]:
        return None
    try:
        age = abs(time.time() - float(data.get("time") or 0))
    except (TypeError, ValueError):
        return None
    return data if age <= max_age else None


def allow_foreground_switch() -> None:
    """允许已运行的窗口抢到前台（Windows 默认不让后台进程把自己提到最前）。"""
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
        except Exception:
            pass


def hand_off_to_running_instance(project: Path, wait: float = HANDOFF_WAIT) -> bool:
    """把项目交给已运行的窗口。对方在 wait 秒内认领返回 True；没人认领就撤回请求返回 False（自己开窗口）。"""
    try:
        path = write_handoff_request(project)
    except Exception:
        return False
    allow_foreground_switch()
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if not path.exists():
            return True
        time.sleep(0.1)
    for _ in range(5):
        try:
            path.unlink()
            return False
        except FileNotFoundError:
            return True
        except OSError:
            time.sleep(0.2)
    return not path.exists()


def main() -> int:
    if os.environ.get("CABLE_STAT_SMOKETEST"):
        return smoke_test()
    project, auto_run = startup_target()
    already_running = claim_single_instance()
    if already_running and project is not None and auto_run and hand_off_to_running_instance(project):
        return 0
    from cable_stat_gui import run_gui

    return run_gui(project, auto_run=auto_run)


if __name__ == "__main__":
    raise SystemExit(main())
