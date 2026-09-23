@echo off
setlocal
cd /d "%~dp0"
set /p NAME=请输入新项目名称: 
if "%NAME%"=="" (echo 未输入名称，退出。 & pause & exit /b 1)
if exist "项目\%NAME%" (echo 项目已存在：项目\%NAME% & pause & exit /b 1)
xcopy /e /i /y "项目模板" "项目\%NAME%" >nul
echo.
echo 已创建：%~dp0项目\%NAME%
echo 接下来：
echo   1. 把电缆清册 Excel（自动统计.xlsx）放进这个文件夹
echo   2. CAD 向导第5步会把 CSV 直接导入它的 data 文件夹
echo   3. 双击文件夹里的 一键启动.cmd，结果生成在它的 outputs 文件夹
start "" explorer "%~dp0项目\%NAME%"
pause
