"""电缆长度自动统计 - 图形界面版（打包成单文件 exe 的入口）。

- 双击 exe：打开界面，选择/新建项目文件夹后点「开始计算」。
- 从项目文件夹的 一键启动.cmd 进来（当前目录是项目文件夹）：自动填好并直接开算。
- 环境变量 CABLE_STAT_SMOKETEST=1 时走无界面自检：对当前目录项目算一遍，
  日志写到项目里的 电缆统计_自检.log，用于打包后回归验证。
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import sys
import threading
import traceback
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import calculate_cable_lengths
from run_auto_stat import find_workbook

APP_TITLE = "电缆长度自动统计"
APP_ICON_FILE = "app_icon.ico"
LSP_FILES = ["cad_cable_wizard.lsp"]
LEGACY_EXPORTED_LSP_FILES = ["cad_export_cable_route.lsp"]


def resource_dir() -> Path:
    """打包后资源在 PyInstaller 解压目录，源码运行时就是本目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).resolve().parent


def exe_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve()
    return Path(__file__).resolve()


def config_file() -> Path:
    base = Path(os.environ.get("APPDATA", str(Path.home()))) / "电缆统计"
    base.mkdir(parents=True, exist_ok=True)
    return base / "config.json"


def load_config() -> dict:
    try:
        return json.loads(config_file().read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(cfg: dict) -> None:
    try:
        config_file().write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def write_launcher(project_dir: Path) -> None:
    """在项目文件夹生成 一键启动.cmd，指向当前 exe（源码运行时指向 python 入口）。"""
    exe = exe_path()
    if exe.suffix.lower() == ".exe":
        content = f'@echo off\r\ncd /d "%~dp0"\r\nstart "" "{exe}"\r\n'
    else:
        tool = Path(__file__).resolve().parent
        content = (
            "@echo off\r\nchcp 65001 >nul\r\ncd /d \"%~dp0\"\r\n"
            f'python "{tool}\\run_auto_stat.py"\r\necho.\r\npause\r\n'
        )
    (project_dir / "一键启动.cmd").write_bytes(content.encode("gbk"))


def create_project(parent_dir: Path, name: str) -> Path:
    project_dir = parent_dir / name
    if project_dir.exists():
        raise FileExistsError(f"项目已存在：{project_dir}")
    template = resource_dir() / "项目模板"
    if template.is_dir():
        shutil.copytree(template, project_dir)
        launcher = project_dir / "一键启动.cmd"
        if launcher.exists():
            launcher.unlink()
    else:
        (project_dir / "data").mkdir(parents=True)
    write_launcher(project_dir)
    return project_dir


def export_lsp(target_dir: Path) -> list[str]:
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
    if not path.is_dir():
        return False
    if path == exe_path().parent:
        return False
    if (path / "data").is_dir():
        return True
    try:
        find_workbook(path)
        return True
    except Exception:
        return False


def run_calculation(project_dir: Path, log) -> bool:
    """跑一个项目的完整统计，所有 print 输出交给 log 回调。成功返回 True。"""

    class _Writer:
        def write(self, s: str) -> None:
            if s:
                log(s)

        def flush(self) -> None:
            pass

    writer = _Writer()
    try:
        with redirect_stdout(writer), redirect_stderr(writer):
            workbook = find_workbook(project_dir)
            print(f"项目：{project_dir}")
            print(f"清册：{workbook.name}")
            calculate_cable_lengths.main(
                [
                    "--workbook", str(workbook),
                    "--data-dir", str(project_dir / "data"),
                    "--output", str(project_dir / "outputs" / "自动统计_计算结果.xlsx"),
                    "--make-cabinet-checklist",
                ]
            )
        return True
    except Exception as exc:
        log("\n" + traceback.format_exc())
        log(f"\n计算失败：{exc}\n")
        return False


def smoke_test() -> int:
    project_dir = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd().resolve()
    lines: list[str] = []
    ok = run_calculation(project_dir, lines.append)
    (project_dir / "电缆统计_自检.log").write_text("".join(lines), encoding="utf-8")
    return 0 if ok else 1


# ---------------- 图形界面 ----------------


def run_gui() -> None:
    import tkinter as tk
    from tkinter import filedialog, messagebox, simpledialog
    from tkinter.scrolledtext import ScrolledText

    cfg = load_config()
    log_queue: "queue.Queue[str]" = queue.Queue()

    root = tk.Tk()
    root.title(APP_TITLE)
    icon_path = resource_dir() / APP_ICON_FILE
    if icon_path.is_file():
        try:
            root.iconbitmap(default=str(icon_path))
        except tk.TclError:
            pass
    root.geometry("860x560")
    root.minsize(680, 420)

    top = tk.Frame(root)
    top.pack(fill="x", padx=10, pady=(10, 4))
    tk.Label(top, text="项目文件夹：").pack(side="left")
    project_var = tk.StringVar()
    entry = tk.Entry(top, textvariable=project_var)
    entry.pack(side="left", fill="x", expand=True, padx=4)

    def current_project() -> Path | None:
        text = project_var.get().strip().strip('"')
        if not text:
            messagebox.showwarning(APP_TITLE, "请先选择项目文件夹")
            return None
        path = Path(text)
        if not path.is_dir():
            messagebox.showwarning(APP_TITLE, f"文件夹不存在：\n{path}")
            return None
        return path.resolve()

    def choose_project() -> None:
        initial = project_var.get().strip() or cfg.get("last_parent", str(exe_path().parent))
        picked = filedialog.askdirectory(title="选择项目文件夹", initialdir=initial)
        if picked:
            project_var.set(str(Path(picked)))

    def new_project() -> None:
        initial = cfg.get("last_parent", str(exe_path().parent / "项目"))
        parent = filedialog.askdirectory(title="新项目建在哪个文件夹下", initialdir=initial)
        if not parent:
            return
        name = simpledialog.askstring(APP_TITLE, "新项目名称：", parent=root)
        if not name or not name.strip():
            return
        try:
            project_dir = create_project(Path(parent), name.strip())
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"创建失败：{exc}")
            return
        cfg["last_parent"] = parent
        save_config(cfg)
        project_var.set(str(project_dir))
        log_queue.put(
            f"已创建项目：{project_dir}\n"
            "接下来：\n"
            "  1. 把电缆清册 Excel（建议命名 自动统计.xlsx）放进这个文件夹；\n"
            "  2. 点「导出CAD插件」把 lsp 给 CAD 用，在 CAD 里跑 DDFD_CABLE_WIZARD，\n"
            "     第5步把 CSV 复制进它的 data 文件夹；\n"
            "  3. 回来点「开始计算」。\n"
        )
        try:
            os.startfile(project_dir)  # noqa: S606
        except OSError:
            pass

    tk.Button(top, text="浏览…", command=choose_project).pack(side="left", padx=2)
    tk.Button(top, text="新建项目", command=new_project).pack(side="left", padx=2)

    bar = tk.Frame(root)
    bar.pack(fill="x", padx=10, pady=4)

    run_btn = tk.Button(bar, text="开始计算", width=12)
    run_btn.pack(side="left", padx=2)

    def open_path(getter, missing_hint: str):
        def _open() -> None:
            project = current_project()
            if not project:
                return
            target = getter(project)
            if target.exists():
                os.startfile(target)  # noqa: S606
            else:
                messagebox.showinfo(APP_TITLE, missing_hint)

        return _open

    tk.Button(bar, text="打开项目文件夹", command=open_path(lambda p: p, "")).pack(side="left", padx=2)
    tk.Button(
        bar,
        text="打开计算结果",
        command=open_path(lambda p: p / "outputs" / "自动统计_计算结果.xlsx", "还没有计算结果，请先点「开始计算」"),
    ).pack(side="left", padx=2)
    tk.Button(
        bar,
        text="打开路径可视化",
        command=open_path(lambda p: p / "outputs" / "路径可视化.html", "还没有可视化结果，请先点「开始计算」"),
    ).pack(side="left", padx=2)

    def do_export_lsp() -> None:
        initial = project_var.get().strip() or str(exe_path().parent)
        picked = filedialog.askdirectory(title="把 CAD 插件(lsp)导出到哪个文件夹", initialdir=initial)
        if not picked:
            return
        exported = export_lsp(Path(picked))
        if exported:
            log_queue.put(
                f"已导出 {('、'.join(exported))} 到 {picked}\n"
                "CAD 里用 APPLOAD 加载 cad_cable_wizard.lsp，输入 DDFD_CABLE_WIZARD 开始。\n"
            )
        else:
            messagebox.showerror(APP_TITLE, "没有找到内置的 lsp 资源")

    tk.Button(bar, text="导出CAD插件", command=do_export_lsp).pack(side="left", padx=2)

    log_box = ScrolledText(root, state="disabled", font=("Consolas", 10))
    log_box.pack(fill="both", expand=True, padx=10, pady=(4, 10))

    def poll_queue() -> None:
        try:
            while True:
                chunk = log_queue.get_nowait()
                log_box.configure(state="normal")
                log_box.insert("end", chunk)
                log_box.see("end")
                log_box.configure(state="disabled")
        except queue.Empty:
            pass
        root.after(100, poll_queue)

    running = {"flag": False}

    def start_run() -> None:
        if running["flag"]:
            return
        project = current_project()
        if not project:
            return
        cfg["last_project"] = str(project)
        save_config(cfg)
        running["flag"] = True
        run_btn.configure(state="disabled", text="计算中…")
        log_queue.put("\n" + "=" * 52 + "\n开始计算…\n")

        def worker() -> None:
            ok = run_calculation(project, log_queue.put)
            log_queue.put("完成。\n" if ok else "本次计算未完成，请按上面的提示处理后重试。\n")
            running["flag"] = False
            root.after(0, lambda: run_btn.configure(state="normal", text="开始计算"))

        threading.Thread(target=worker, daemon=True).start()

    run_btn.configure(command=start_run)

    # 启动逻辑：从项目文件夹进来就自动开算，否则回填上次的项目
    startup = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd().resolve()
    if looks_like_project(startup):
        project_var.set(str(startup))
        root.after(200, start_run)
    elif cfg.get("last_project") and Path(cfg["last_project"]).is_dir():
        project_var.set(cfg["last_project"])
        log_queue.put("已回填上次的项目文件夹，确认后点「开始计算」；换项目用「浏览…」，新工程用「新建项目」。\n")
    else:
        log_queue.put("先点「新建项目」创建项目文件夹，或用「浏览…」选择已有项目。\n")

    poll_queue()
    root.mainloop()


def main() -> int:
    if os.environ.get("CABLE_STAT_SMOKETEST"):
        return smoke_test()
    run_gui()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
