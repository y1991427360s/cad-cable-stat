[CmdletBinding()]
param(
    [string]$Python = "python",
    [switch]$SkipTests,
    [ValidateRange(15, 1800)]
    [int]$SmokeTimeoutSeconds = 120
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$toolRoot = $PSScriptRoot
$buildRoot = Join-Path $toolRoot "_build_release"
$distRoot = Join-Path $buildRoot "dist"
$stagedExe = Join-Path $distRoot "电缆统计.exe"
$releaseExe = Join-Path $toolRoot "电缆统计.exe"
$stamp = Get-Date -Format "yyyyMMdd_HHmmss_fff"
$smokeProject = Join-Path $buildRoot "smoke_$stamp"
$publishTemp = Join-Path $toolRoot "电缆统计.release-$PID.tmp"

function Invoke-CheckedPython {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    & $Python @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Python 命令失败（退出码 $LASTEXITCODE）：$($Arguments -join ' ')"
    }
}

Push-Location -LiteralPath $toolRoot
try {
    # 只使用已安装的运行时；缺依赖时停止，不在构建脚本中安装或升级依赖。
    Invoke-CheckedPython -Arguments @("-c", "import sys, openpyxl, PyInstaller; assert sys.version_info[:2] == (3, 14), 'Build requires Python 3.14'; print('Python:', sys.executable); print('PyInstaller:', PyInstaller.__version__)")
    foreach ($relative in @("app_icon.ico", "cad_cable_wizard.lsp", "项目模板", "cable_stat/visualizer.html", "cable_stat_app.py", "cable_stat_gui.py")) {
        if (-not (Test-Path -LiteralPath (Join-Path $toolRoot $relative))) {
            throw "缺少打包资源：$relative"
        }
    }

    Write-Host "检查 Python 语法…"
    $sources = @(Get-ChildItem -LiteralPath $toolRoot -Filter "*.py" -File | ForEach-Object FullName)
    $sources += @(Get-ChildItem -LiteralPath (Join-Path $toolRoot "cable_stat") -Filter "*.py" -File -Recurse | ForEach-Object FullName)
    Invoke-CheckedPython -Arguments (@("-m", "py_compile") + $sources)
    if ($SkipTests) {
        Write-Warning "已显式指定 -SkipTests：跳过测试集；语法检查与打包后的冒烟验收仍会执行。"
    }
    else {
        Write-Host "运行核心计算测试…"
        Invoke-CheckedPython -Arguments @("-m", "test_calculate_cable_lengths")
        Write-Host "运行独立回归与 GUI 测试…"
        Invoke-CheckedPython -Arguments @("-m", "unittest", "discover", "-v")
    }

    New-Item -ItemType Directory -Path $buildRoot -Force | Out-Null
    $fixtureScript = Join-Path $buildRoot "smoke_fixture.py"
    # 冒烟工程与用户工程完全分开；测试两条电缆的直线路径、拐弯路径和别名映射。
    $fixtureCode = @'
from __future__ import annotations

import csv
import hashlib
import json
import math
import sys
from pathlib import Path

from openpyxl import Workbook, load_workbook


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def write_csv(path, headers, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(headers)
        writer.writerows(rows)


mode, project_arg = sys.argv[1:3]
project = Path(project_arg)
workbook_path = project / "自动统计.xlsx"
expected = {"SMOKE-直线": 17.0, "SMOKE-拐弯": 32.4}
if mode == "prepare":
    project.mkdir(parents=True, exist_ok=False)
    data = project / "data"
    data.mkdir()
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "电缆清册"
    sheet.append(["电缆编号", "起点", "终点", "电缆长度", "型号"])
    sheet.append(["SMOKE-直线", "甲柜清册名", "乙柜", 17, "测试型号"])
    sheet.append(["SMOKE-拐弯", "甲柜", "丙柜", 32.4, "测试型号"])
    workbook.save(workbook_path)
    workbook.close()
    write_csv(data / "柜子坐标.csv", ["柜子名称", "楼层", "X", "Y", "图层", "对象类型", "句柄"], [
        ["甲柜", "1F", 0, 0, "CABLE_CAB_1F", "TEXT", "A"],
        ["乙柜", "1F", 10000, 0, "CABLE_CAB_1F", "TEXT", "B"],
        ["丙柜", "1F", 10000, 10000, "CABLE_CAB_1F", "TEXT", "C"],
    ])
    write_csv(data / "路径线段.csv", ["路径编号", "楼层", "起点X", "起点Y", "终点X", "终点Y", "长度_CAD单位", "图层", "句柄", "段号"], [
        ["R1", "1F", 0, 0, 10000, 0, 10000, "CABLE_ROUTE_1F", "10", "1"],
        ["R2", "1F", 10000, 0, 10000, 10000, 10000, "CABLE_ROUTE_1F", "11", "1"],
    ])
    write_csv(data / "参数.csv", ["参数", "值", "备注"], [
        ["CAD每米单位", 1000, "合成毫米图"], ["不拐弯修正", 7, "合成测试"],
        ["拐弯倍率", 1.2, "合成测试"], ["吸附容差", 0.05, "合成测试"],
    ])
    write_csv(data / "柜名别名.csv", ["清册名称", "CAD名称", "备注"], [["甲柜清册名", "甲柜", "冒烟别名"]])
    (project / "source.sha256").write_text(hashlib.sha256(workbook_path.read_bytes()).hexdigest(), encoding="ascii")
    print(f"Smoke project: {project}")
elif mode == "verify":
    from PyInstaller.archive.readers import CArchiveReader

    archive = CArchiveReader(sys.argv[3])
    source_root = Path(sys.argv[4])
    archive_names = {name.replace("\\", "/"): name for name in archive.toc}
    embedded = ["app_icon.ico", "cad_cable_wizard.lsp", "cable_stat/visualizer.html"]
    embedded += [path.relative_to(source_root).as_posix() for path in (source_root / "项目模板").rglob("*") if path.is_file()]
    for name in embedded:
        require(name in archive_names, f"Missing packaged resource: {name}")
        require(archive.extract(archive_names[name]) == (source_root / name).read_bytes(), f"Stale packaged resource: {name}")
    log = (project / "电缆统计_自检.log").read_text(encoding="utf-8")
    require("自检通过" in log and "自检失败" not in log, "Executable smoke log did not report success")
    require(hashlib.sha256(workbook_path.read_bytes()).hexdigest() == (project / "source.sha256").read_text(encoding="ascii"), "Original workbook changed")
    outputs = project / "outputs"
    with (outputs / "统计明细.csv").open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    require(len(rows) == len(expected), "Unexpected cable count in detail CSV")
    require({row["电缆编号"] for row in rows} == set(expected), "Unexpected cable IDs")
    for row in rows:
        require(row["状态"] == "OK", f"Cable calculation failed: {row}")
        require(math.isclose(float(row["自动统计"]), expected[row["电缆编号"]], abs_tol=1e-8), f"Wrong cable length: {row}")
    workbook = load_workbook(outputs / "自动统计_计算结果.xlsx", data_only=True)
    try:
        require({"统计明细", "柜子清单", "参数", "问题清单"}.issubset(workbook.sheetnames), "Missing output workbook sheets")
        sheet = workbook["电缆清册"]
        headers = {cell.value: cell.column for cell in sheet[1]}
        for row_no in (2, 3):
            cable_id = sheet.cell(row_no, headers["电缆编号"]).value
            require(sheet.cell(row_no, headers["自动统计"]).value == expected[cable_id], "Workbook length mismatch")
    finally:
        workbook.close()
    visual = json.loads((outputs / "路径可视化数据.json").read_text(encoding="utf-8"))
    require(len(visual["cables"]) == len(expected), "Visualization cable count mismatch")
    html = (outputs / "路径可视化.html").read_text(encoding="utf-8")
    require("__VISUAL_DATA_JSON__" not in html and "SMOKE-" in html, "Visualization data was not embedded")
    print("Smoke validation passed: embedded resources, 2 cable lengths, workbook, CSV, HTML, JSON, and unchanged source.")
else:
    raise ValueError(f"Unknown fixture mode: {mode}")
'@
    [System.IO.File]::WriteAllText($fixtureScript, $fixtureCode, [System.Text.UTF8Encoding]::new($false))
    Invoke-CheckedPython -Arguments @($fixtureScript, "prepare", $smokeProject)

    Write-Host "打包暂存版 exe…"
    $buildArguments = @(
        "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--noconsole",
        "--name", "电缆统计", "--icon", (Join-Path $toolRoot "app_icon.ico"),
        "--distpath", $distRoot, "--workpath", (Join-Path $buildRoot "work"),
        "--specpath", (Join-Path $buildRoot "spec"),
        "--add-data", ((Join-Path $toolRoot "app_icon.ico") + ";."),
        "--add-data", ((Join-Path $toolRoot "cad_cable_wizard.lsp") + ";."),
        "--add-data", ((Join-Path $toolRoot "项目模板") + ";项目模板"),
        "--add-data", ((Join-Path $toolRoot "cable_stat/visualizer.html") + ";cable_stat"),
        (Join-Path $toolRoot "cable_stat_app.py")
    )
    Invoke-CheckedPython -Arguments $buildArguments
    if (-not (Test-Path -LiteralPath $stagedExe -PathType Leaf)) {
        throw "PyInstaller 未生成暂存 exe：$stagedExe"
    }

    Write-Host "运行暂存 exe 冒烟验收…"
    $previousSmoke = [Environment]::GetEnvironmentVariable("CABLE_STAT_SMOKETEST", "Process")
    try {
        $env:CABLE_STAT_SMOKETEST = "1"
        $smokeProcess = Start-Process -FilePath $stagedExe -WorkingDirectory $smokeProject -WindowStyle Hidden -PassThru
        # Windows PowerShell 5.1 需先缓存句柄，进程退出后才能可靠读取 ExitCode。
        $null = $smokeProcess.Handle
        $deadline = [DateTime]::UtcNow.AddSeconds($SmokeTimeoutSeconds)
        while (-not $smokeProcess.WaitForExit(1000)) {
            if ([DateTime]::UtcNow -ge $deadline) {
                & taskkill.exe /PID $smokeProcess.Id /T /F | Out-Null
                throw "冒烟验收超过 $SmokeTimeoutSeconds 秒，已终止本次测试进程；原发布版未替换。"
            }
        }
        if ($smokeProcess.ExitCode -ne 0) {
            throw "暂存 exe 自检失败（退出码 $($smokeProcess.ExitCode)），日志位于 $smokeProject；原发布版未替换。"
        }
    }
    finally {
        [Environment]::SetEnvironmentVariable("CABLE_STAT_SMOKETEST", $previousSmoke, "Process")
    }
    Invoke-CheckedPython -Arguments @($fixtureScript, "verify", $smokeProject, $stagedExe, $toolRoot)

    # 先备份旧版，再在同一卷上原子替换；构建/测试失败不会提前覆盖用户正在用的版本。
    $backupPath = $null
    if (Test-Path -LiteralPath $releaseExe -PathType Leaf) {
        $backupRoot = Join-Path $buildRoot "backups"
        New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
        $backupPath = Join-Path $backupRoot "电缆统计_$stamp.exe"
        Copy-Item -LiteralPath $releaseExe -Destination $backupPath
    }
    Copy-Item -LiteralPath $stagedExe -Destination $publishTemp -Force
    try {
        if (Test-Path -LiteralPath $releaseExe -PathType Leaf) {
            # Windows PowerShell 5.1 会将此重载的 $null 转成空路径；使用已准备的备份路径。
            [System.IO.File]::Replace($publishTemp, $releaseExe, $backupPath)
        }
        else {
            [System.IO.File]::Move($publishTemp, $releaseExe)
        }
    }
    finally {
        if (Test-Path -LiteralPath $publishTemp -PathType Leaf) {
            Remove-Item -LiteralPath $publishTemp
        }
    }
    Write-Host "发布完成：$releaseExe"
    Write-Host "SHA256：$((Get-FileHash -LiteralPath $releaseExe -Algorithm SHA256).Hash)"
    Write-Host "冒烟工程和日志：$smokeProject"
    if ($backupPath) {
        Write-Host "旧版备份：$backupPath"
    }
}
finally {
    Pop-Location
}
