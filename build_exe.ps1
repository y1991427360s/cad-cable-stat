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
$stagedExe = Join-Path $distRoot "电气二次设计工作台.exe"
$releaseExe = Join-Path $toolRoot "电气二次设计工作台.exe"
$stamp = Get-Date -Format "yyyyMMdd_HHmmss_fff"
$smokeProject = Join-Path $buildRoot "smoke_$stamp"
$publishTemp = Join-Path $toolRoot "电气二次设计工作台.release-$PID.tmp"

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
    Invoke-CheckedPython -Arguments @("-c", "import sys, openpyxl, ezdxf, PyInstaller; assert sys.version_info[:2] == (3, 14), 'Build requires Python 3.14'; print('Python:', sys.executable); print('PyInstaller:', PyInstaller.__version__)")
    foreach ($relative in @("app_icon.ico", "cad_cable_wizard.lsp", "项目模板", "cable_stat/visualizer.html", "app.py", "cable_stat_app.py", "cable_stat_gui.py", "modules/terminal/service.py", "tools/workbench_smoke.py")) {
        if (-not (Test-Path -LiteralPath (Join-Path $toolRoot $relative))) {
            throw "缺少打包资源：$relative"
        }
    }

    Write-Host "检查 Python 语法…"
    $sources = @(Get-ChildItem -LiteralPath $toolRoot -Filter "*.py" -File | ForEach-Object FullName)
    foreach ($package in @("cable_stat", "core", "modules", "ui", "tests", "tools")) {
        $packagePath = Join-Path $toolRoot $package
        if (Test-Path -LiteralPath $packagePath -PathType Container) {
            $sources += @(Get-ChildItem -LiteralPath $packagePath -Filter "*.py" -File -Recurse | ForEach-Object FullName)
        }
    }
    Invoke-CheckedPython -Arguments (@("-m", "py_compile") + $sources)
    Write-Host "检查端子模块及统一入口导入…"
    Invoke-CheckedPython -Arguments @("-c", "import app; from modules.terminal.service import TerminalService; from tools.workbench_smoke import run_terminal_smoke; print('Workbench imports: OK')")
    if ($SkipTests) {
        Write-Warning "已显式指定 -SkipTests：跳过测试集；语法检查与打包后的冒烟验收仍会执行。"
    }
    else {
        Write-Host "运行核心计算测试…"
        Invoke-CheckedPython -Arguments @("-m", "test_calculate_cable_lengths")
        Write-Host "运行两模块独立回归、工程存储与 GUI 测试…"
        Invoke-CheckedPython -Arguments @("-m", "unittest", "discover", "-v")
    }

    New-Item -ItemType Directory -Path $buildRoot -Force | Out-Null
    # 合成工程与用户工程完全分开，保留电缆基线并新增端子完整图/仅接线图验收。
    Invoke-CheckedPython -Arguments @("-m", "tools.workbench_smoke", "prepare", $smokeProject)

    Write-Host "打包暂存版 exe…"
    $buildArguments = @(
        "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--noconsole",
        "--name", "电气二次设计工作台", "--icon", (Join-Path $toolRoot "app_icon.ico"),
        "--distpath", $distRoot, "--workpath", (Join-Path $buildRoot "work"),
        "--specpath", (Join-Path $buildRoot "spec"),
        "--add-data", ((Join-Path $toolRoot "app_icon.ico") + ";."),
        "--add-data", ((Join-Path $toolRoot "cad_cable_wizard.lsp") + ";."),
        "--add-data", ((Join-Path $toolRoot "项目模板") + ";项目模板"),
        "--add-data", ((Join-Path $toolRoot "cable_stat/visualizer.html") + ";cable_stat"),
        "--collect-all", "ezdxf", "--collect-all", "openpyxl",
        (Join-Path $toolRoot "app.py")
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
    Invoke-CheckedPython -Arguments @("-m", "tools.workbench_smoke", "verify", $smokeProject, $stagedExe, $toolRoot)

    # 先备份旧版，再在同一卷上原子替换；构建/测试失败不会提前覆盖用户正在用的版本。
    $backupPath = $null
    if (Test-Path -LiteralPath $releaseExe -PathType Leaf) {
        $backupRoot = Join-Path $buildRoot "backups"
        New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
        $backupPath = Join-Path $backupRoot "电气二次设计工作台_$stamp.exe"
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
