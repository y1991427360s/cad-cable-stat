@echo off
cd /d "%~dp0"
if exist "..\..\电气二次设计工作台.exe" (
    start "" "..\..\电气二次设计工作台.exe"
    exit /b
)
if exist "..\电气二次设计工作台.exe" (
    start "" "..\电气二次设计工作台.exe"
    exit /b
)
echo 请打开 电气二次设计工作台.exe，再选择本工程文件夹。
echo 从工作台新建工程可生成指向程序位置的启动器。
pause
