"""后续跨模块协作的数据接口，现有 CSV / Excel / TXT 无需迁移。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Project:
    name: str
    path: Path


@dataclass(frozen=True)
class Cabinet:
    name: str


@dataclass(frozen=True)
class Cable:
    cable_id: str
    from_cabinet: str
    to_cabinet: str
    specification: str = ""
    length: float | None = None


@dataclass(frozen=True)
class TerminalStrip:
    cabinet: str
    name: str


@dataclass(frozen=True)
class Terminal:
    strip: TerminalStrip
    number: str


@dataclass(frozen=True)
class Connection:
    terminal: Terminal
    principle_number: str = ""
    target_cabinet: str = ""


@dataclass(frozen=True)
class CableCore:
    cable: Cable
    core_number: str
    from_terminal: Terminal | None = None
    to_terminal: Terminal | None = None


@dataclass(frozen=True)
class UnifiedIssue:
    source: str
    level: str
    message: str
