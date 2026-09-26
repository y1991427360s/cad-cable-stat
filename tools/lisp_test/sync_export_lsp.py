"""把 cad_cable_wizard.lsp 的“通用与导出部分”原样同步到 cad_export_cable_route.lsp。

用法（在仓库根目录）：
    python tools\\lisp_test\\sync_export_lsp.py          # 重写精简版的共享段和版本号
    python tools\\lisp_test\\sync_export_lsp.py --check  # 只检查是否一致，不一致时退出码 1

精简版 = 它自己的文件头（含版本号）+ 向导里从“通用与导出部分”横幅到“引导向导部分”横幅之前的整段 + 加载提示。
两份 LISP 都是 GBK + CRLF，这里按字节读写：严格 GBK 解码、不允许替换字符、只允许 CRLF 换行。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WIZARD = ROOT / "cad_cable_wizard.lsp"
EXPORT = ROOT / "cad_export_cable_route.lsp"

SHARED_BANNER = ";;; ================= 通用与导出部分 ================="
WIZARD_BANNER = ";;; ================= 引导向导部分 ================="
FOOTER_BANNER = ";;; ================= 加载提示 ================="
VERSION_RE = re.compile(r'\(setq \*ddfd-cable-version\* "([^"]+)"\)')


def read_lsp(path: Path) -> str:
    """严格按 GBK + CRLF 读取，返回用 \\n 换行的文本。"""
    raw = path.read_bytes()
    if b"\r" in raw.replace(b"\r\n", b"") or b"\n" in raw.replace(b"\r\n", b""):
        raise SystemExit(f"{path.name}: 存在非 CRLF 换行")
    text = raw.decode("gbk", errors="strict")
    if chr(0xFFFD) in text:
        raise SystemExit(f"{path.name}: 含替换字符 U+FFFD")
    return text.replace("\r\n", "\n")


def encode_lsp(text: str) -> bytes:
    """\\n 文本 -> GBK + CRLF 字节；遇到 GBK 表示不了的字符直接报错。"""
    data = text.replace("\r\n", "\n").replace("\n", "\r\n").encode("gbk", errors="strict")
    if data.decode("gbk") != text.replace("\r\n", "\n").replace("\n", "\r\n"):
        raise SystemExit("GBK 往返不一致")
    return data


def section(text: str, start: str, end: str, name: str) -> tuple[int, int]:
    """返回 [start 横幅行首, end 横幅行首) 的下标；横幅必须各出现一次且在行首。"""
    positions = []
    for banner in (start, end):
        hits = [m.start() for m in re.finditer(re.escape(banner), text) if m.start() == 0 or text[m.start() - 1] == "\n"]
        if len(hits) != 1:
            raise SystemExit(f"{name}: 横幅 {banner!r} 应出现 1 次，实际 {len(hits)} 次")
        positions.append(hits[0])
    if positions[0] >= positions[1]:
        raise SystemExit(f"{name}: 横幅顺序不对")
    return positions[0], positions[1]


def build_export(wizard_text: str, export_text: str) -> str:
    w0, w1 = section(wizard_text, SHARED_BANNER, WIZARD_BANNER, WIZARD.name)
    e0, e1 = section(export_text, SHARED_BANNER, FOOTER_BANNER, EXPORT.name)
    version = VERSION_RE.search(wizard_text[:w0])
    if not version:
        raise SystemExit(f"{WIZARD.name}: 文件头里没有 *ddfd-cable-version*")
    header = export_text[:e0]
    if not VERSION_RE.search(header):
        raise SystemExit(f"{EXPORT.name}: 文件头里没有 *ddfd-cable-version*")
    header = VERSION_RE.sub(f'(setq *ddfd-cable-version* "{version.group(1)}")', header, count=1)
    return header + wizard_text[w0:w1] + export_text[e1:]


def main(argv: list[str]) -> int:
    check = "--check" in argv
    wizard_text = read_lsp(WIZARD)
    export_text = read_lsp(EXPORT)
    new_text = build_export(wizard_text, export_text)
    if new_text == export_text:
        print(f"{EXPORT.name} 已与向导的共享段一致")
        return 0
    if check:
        print(f"{EXPORT.name} 与向导的共享段不一致，请运行 python tools\\lisp_test\\sync_export_lsp.py")
        return 1
    EXPORT.write_bytes(encode_lsp(new_text))
    read_lsp(EXPORT)
    print(f"已同步 {EXPORT.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
