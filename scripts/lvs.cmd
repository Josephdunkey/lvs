@echo off
REM `lvs` 启动器：用项目自带的 venv 运行 CLI，无需手动激活环境。
REM 用法：scripts\lvs.cmd doctor / run --demo / parse "路径.md" ...
REM 建议：把本目录加入 PATH，之后可直接在任意目录敲 `lvs`。

setlocal
set "LVS_ROOT=%~dp0.."
"%LVS_ROOT%\.venv\Scripts\python.exe" -m lvs %*
endlocal
