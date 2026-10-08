@echo off
REM ==========================================================================
REM  PhotoBak - setup (source edition)
REM
REM  NOTE: this file is intentionally ASCII-only.
REM  cmd.exe cannot reliably parse UTF-8 batch files: it reads them by byte
REM  offset, so a multi-byte Chinese character spanning an internal buffer
REM  boundary gets truncated and the leftover half is executed as a command
REM  ("'xx' is not recognized as an internal or external command").
REM  All Chinese output therefore comes from Python (tools\setup_env.py),
REM  and the one message we need before Python exists is printed with
REM  `type` from a UTF-8 text file, which does not involve the parser.
REM ==========================================================================

chcp 65001 >nul
title PhotoBak - Setup
cd /d "%~dp0"

set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY if defined PHOTOBAK_PYTHON set "PY=%PHOTOBAK_PYTHON%"
if not defined PY (
    where py >nul 2>nul && set "PY=py -3"
)
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)

if not defined PY (
    type "tools\messages\no_python.txt"
    echo.
    pause
    exit /b 1
)

set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

%PY% "tools\setup_env.py"
set "RC=%ERRORLEVEL%"

echo.
pause
exit /b %RC%
