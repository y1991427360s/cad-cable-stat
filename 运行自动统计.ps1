param(
    [string]$Project = (Get-Location).Path
)

$ErrorActionPreference = "Stop"

$toolDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$runner = Join-Path $toolDir "run_auto_stat.py"
python $runner $Project
