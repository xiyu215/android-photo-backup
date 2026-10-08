@echo off
REM ==========================================================================
REM  PhotoBak - one click build (source edition)
REM
REM  ASCII-only on purpose: see the note in "install-deps" batch file.
REM  Everything else (tests, offline check, PyInstaller, packaging) is driven
REM  by tools\build_all.py so that all Chinese output stays encoding-safe.
REM ==========================================================================

chcp 65001 >nul
title PhotoBak - Build
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

%PY% "tools\build_all.py" %*
set "RC=%ERRORLEVEL%"

echo.
pause
exit /b %RC%
