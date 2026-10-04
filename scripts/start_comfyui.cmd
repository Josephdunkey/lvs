@echo off
REM ===========================================================================
REM  ComfyUI launcher -- thin forwarder.
REM
REM  The real launcher is shipped WITH ComfyUI itself, because it needs to know
REM  where ComfyUI lives and is useful without this project:
REM
REM      D:\ComfyUI\start_comfyui.cmd
REM
REM  This file stays as a forwarder so the paths referenced by config.toml and
REM  README.md keep working. Extra arguments are forwarded verbatim:
REM
REM      scripts\start_comfyui.cmd --check
REM      scripts\start_comfyui.cmd --safevram
REM      scripts\start_comfyui.cmd --port 8190
REM
REM  NOTE: keep this file pure ASCII. Mixing non-ASCII text with "chcp 65001"
REM  corrupts cmd.exe's batch-parser offset and garbles the whole script.
REM ===========================================================================

setlocal
set "REAL=D:\ComfyUI\start_comfyui.cmd"

if not exist "%REAL%" (
    echo [ERROR] ComfyUI's launcher was not found:
    echo         %REAL%
    echo.
    echo         Check that ComfyUI is still installed at D:\ComfyUI.
    echo.
    pause
    exit /b 1
)

call "%REAL%" %*
exit /b %ERRORLEVEL%
