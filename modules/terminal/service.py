"""端子模块的工程边界：原始输入随工程保存，出图复用已有算法。"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any

from core.models import UnifiedIssue

from . import duanzi_dxf_tool as drawing
from .duanzi_dxf_tool import EXAMPLE_CABINET, EXAMPLE_TERMINALS, EXAMPLE_WIRING

STATE_VERSION = 1
DIRECTIONS = {"向上": "UP", "向下": "DOWN", "UP": "UP", "DOWN": "DOWN"}


def _direction(value: Any, default: str = "DOWN") -> str:
    return DIRECTIONS.get(value, default) if isinstance(value, str) else default


def _direction_label(value: Any) -> str:
    return "向上" if _direction(value) == "UP" else "向下"


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _normal_state(state: dict) -> dict:
    memory = state.get("directionMemory", {})
    if not isinstance(memory, dict):
        memory = {}
    return {
        "version": STATE_VERSION,
        "terminals": _text(state.get("terminals")),
        "wiring": _text(state.get("wiring")),
        "cabinet": _text(state.get("cabinet")),
        "direction": _direction_label(state.get("direction")),
        "directionMemory": {
            key: _direction_label(value) for key, value in memory.items()
            if isinstance(key, str) and isinstance(value, str) and value in DIRECTIONS
        },
        "autoOpen": state.get("autoOpen") is True,
    }


def _atomic_write_files(contents: dict[Path, str]) -> None:
    """先写齐新文件和原件备份；替换失败时还原已替换的工程输入。"""
    staged: list[tuple[Path, Path]] = []
    originals: dict[Path, Path | None] = {}
    replaced: list[Path] = []
    retained_backups: set[Path] = set()
    try:
        for target, content in contents.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="", prefix="." + target.name + ".",
                suffix=".tmp", dir=target.parent, delete=False,
            ) as stream:
                temporary = Path(stream.name)
                staged.append((temporary, target))
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        # 三个输入必须来自同一次保存；只保护单个文件仍可能留下新旧混合数据。
        for _, target in staged:
            originals[target] = None
            if target.exists():
                with tempfile.NamedTemporaryFile(
                    mode="wb", prefix="." + target.name + ".save-backup-",
                    suffix=".bak", dir=target.parent, delete=False,
                ) as backup:
                    originals[target] = Path(backup.name)
                    with target.open("rb") as source:
                        shutil.copyfileobj(source, backup)
                    backup.flush()
                    os.fsync(backup.fileno())
        for temporary, target in staged:
            os.replace(temporary, target)
            replaced.append(target)
    except OSError as error:
        recovery_errors: list[str] = []
        for target in reversed(replaced):
            backup = originals[target]
            try:
                if backup is None:
                    target.unlink(missing_ok=True)
                else:
                    os.replace(backup, target)
            except OSError as recovery_error:
                if backup is not None:
                    retained_backups.add(backup)
                    recovery_errors.append("%s（原件备份：%s）：%s" % (target.name, backup, recovery_error))
                else:
                    recovery_errors.append("%s（本次新建，未能移除）：%s" % (target, recovery_error))
        if recovery_errors:
            raise OSError("%s；部分输入未能还原，请保留并恢复原件备份：%s" %
                          (error, "；".join(recovery_errors))) from error
        raise
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)
        for backup in originals.values():
            if backup is not None and backup not in retained_backups:
                backup.unlink(missing_ok=True)


class TerminalService:
    """只操作当前工程，不读写旧程序的 APPDATA 状态或用户桌面。"""

    def __init__(self, project: Path):
        self.project = Path(project).resolve()
        self.data_dir = self.project / "data" / "terminal"
        self.output_dir = self.project / "outputs" / "端子排"
        self.load_warnings: list[str] = []
        self._unreadable_paths: set[Path] = set()
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def load_state(self) -> dict:
        self.load_warnings = []
        self._unreadable_paths = set()
        settings: dict = {}
        settings_path = self.data_dir / "settings.json"
        if settings_path.exists():
            try:
                value = json.loads(settings_path.read_text(encoding="utf-8-sig"))
                if not isinstance(value, dict):
                    raise ValueError("配置内容应为 JSON 对象")
                settings = value
                invalid = []
                if "cabinet" in settings and not isinstance(settings["cabinet"], str):
                    invalid.append("柜名")
                if "direction" in settings and (
                    not isinstance(settings["direction"], str) or settings["direction"] not in DIRECTIONS
                ):
                    invalid.append("默认方向")
                if "autoOpen" in settings and not isinstance(settings["autoOpen"], bool):
                    invalid.append("自动打开")
                if "directionMemory" in settings:
                    memory = settings["directionMemory"]
                    if not isinstance(memory, dict) or any(
                        not isinstance(direction, str) or direction not in DIRECTIONS
                        for direction in memory.values()
                    ):
                        invalid.append("端子排方向")
                if invalid:
                    self._unreadable_paths.add(settings_path)
                    self.load_warnings.append("端子设置中的%s格式无效，已使用默认值；原始输入仍保留。" % "、".join(invalid))
            except (OSError, ValueError) as error:
                self._unreadable_paths.add(settings_path)
                self.load_warnings.append("无法读取端子设置 %s：%s；已使用默认设置。" % (settings_path, error))
        state = _normal_state(settings)
        # 原始文本永远以工程内的两个 TXT 为准，不从全局状态或 JSON 搬入。
        for key, filename in (("terminals", "端子排.txt"), ("wiring", "接线.txt")):
            path = self.data_dir / filename
            state[key] = ""
            if path.exists():
                try:
                    with path.open("r", encoding="utf-8-sig", newline="") as stream:
                        state[key] = stream.read()
                except (OSError, UnicodeError) as error:
                    self._unreadable_paths.add(path)
                    self.load_warnings.append("无法读取端子输入 %s：%s；请检查文件编码和访问权限。" % (path, error))
        return state

    def save_state(self, state: dict) -> None:
        if not isinstance(state, dict):
            raise ValueError("端子状态必须是配置对象")
        normalized = _normal_state(state)
        settings = {key: value for key, value in normalized.items() if key not in ("terminals", "wiring")}
        try:
            # 加载失败时页面会显示默认值；自动保存前留下原件，避免关闭窗口丢失待修复的数据。
            for path in sorted(self._unreadable_paths):
                if path.exists():
                    backup = path.with_name("%s.unreadable-%d.bak" % (path.name, time.time_ns()))
                    shutil.copy2(path, backup)
            self._unreadable_paths.clear()
            _atomic_write_files({
                self.data_dir / "端子排.txt": normalized["terminals"],
                self.data_dir / "接线.txt": normalized["wiring"],
                self.data_dir / "settings.json": json.dumps(settings, ensure_ascii=False, indent=2) + "\n",
            })
        except OSError as error:
            raise OSError("无法保存本工程的端子输入（%s）：%s" % (self.data_dir, error)) from error

    @staticmethod
    def inspect(payload: dict) -> dict:
        """检查可不绑定工程使用，结果字段与原出图核心保持一致。"""
        if not isinstance(payload, dict):
            return {"ok": False, "errors": ["端子输入必须是配置对象"], "warnings": [], "strips": [], "parsed": []}
        blocks, errors = drawing.parse_terminals(_text(payload.get("terminals")))
        connections, cables, wire_errors, warnings = drawing.parse_wiring(
            _text(payload.get("wiring")), blocks, _text(payload.get("cabinet")),
            _text(payload.get("prefix")).strip() or drawing.DEFAULTS["cablePrefix"],
        )
        errors = errors + wire_errors
        strips = drawing.strips_from_blocks(blocks)
        seen: dict[str, int] = {}
        for strip in strips:
            name = strip["name"]
            seen[name] = seen.get(name, 0) + 1
            strip["ident"] = name if seen[name] == 1 else "%s#%d" % (name, seen[name])
        return {
            "ok": not errors, "errors": errors,
            "warnings": drawing.duplicate_terminal_warnings(blocks) + warnings,
            "strips": strips, "parsed": drawing.describe_blocks(blocks, with_numbers=True),
            "stats": {"blocks": len(strips), "terminals": sum(len(block["terminals"]) for block in blocks),
                      "cables": len(cables), "points": len(connections)},
            "cables": cables,
        }

    @staticmethod
    def issues(result: dict) -> list[UnifiedIssue]:
        return [UnifiedIssue("端子", level, str(message))
                for key, level in (("errors", "错误"), ("warnings", "警告"))
                for message in result.get(key, [])]

    def generate(self, payload: dict) -> dict:
        checked = self.inspect(payload)
        if not checked["ok"]:
            return checked
        state = _normal_state(payload)
        directions = payload.get("directions", {})
        if not isinstance(directions, dict):
            directions = {}
        default = _direction(state["direction"])
        generation = dict(payload)
        generation.update({
            "terminals": state["terminals"], "wiring": state["wiring"], "cabinet": state["cabinet"],
            "direction": default, "outputDir": str(self.output_dir),
            "directions": {
                strip["key"]: _direction(directions.get(strip["key"], state["directionMemory"].get(strip["ident"])), default)
                for strip in checked["strips"]
            },
        })
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            result = drawing.generate(generation)
        except Exception as error:
            return {"ok": False, "errors": ["端子出图失败：%s" % error], "warnings": checked["warnings"],
                    "parsed": checked["parsed"], "strips": checked["strips"], "traceback": traceback.format_exc()}
        result.setdefault("errors", [])
        result.setdefault("warnings", [])
        result["strips"] = checked["strips"]
        if result.get("ok"):
            try:
                self.save_state(state)
            except OSError as error:
                result["warnings"].append("DXF 已生成，但%s" % error)
        return result
