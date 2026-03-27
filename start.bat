@echo off
setlocal

:: Move to the directory this script lives in (works when double-clicked)
cd /d "%~dp0"

:: ── Check virtual environment exists ─────────────────────────────────────────
if not exist ".venv\Scripts\activate.bat" (
    echo.
    echo   [ERROR] Virtual environment not found.
    echo           Please run setup.bat first.
    echo.
    pause & exit /b 1
)

:: ── Activate ─────────────────────────────────────────────────────────────────
call .venv\Scripts\activate.bat

:: ── Run ──────────────────────────────────────────────────────────────────────
echo.
echo   Starting Local Social Media CMS...
echo   Press Ctrl+C to stop.
echo.

python launch.py %*
