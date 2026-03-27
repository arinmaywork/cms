@echo off
setlocal enabledelayedexpansion

echo.
echo ============================================================
echo    Local Social CMS -- Setup (Windows)
echo    Run this once after unzipping the project.
echo ============================================================
echo.

:: Move to the directory this script lives in
cd /d "%~dp0"

:: ── 1. Check Python ──────────────────────────────────────────────────────────
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found.
    echo         Install Python 3.10+ from https://python.org
    echo         Make sure to tick "Add Python to PATH" during install.
    pause & exit /b 1
)
for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PY_VER=%%v
echo [OK]   Python %PY_VER% found.

:: ── 2. Create virtual environment ────────────────────────────────────────────
if exist ".venv\Scripts\activate.bat" (
    echo [SKIP] .venv already exists.
) else (
    echo [....] Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 ( echo [ERROR] Failed to create venv. & pause & exit /b 1 )
    echo [OK]   Virtual environment created.
)

:: ── 3. Activate ───────────────────────────────────────────────────────────────
call .venv\Scripts\activate.bat
echo [OK]   Virtual environment activated.

:: ── 4. Upgrade pip ───────────────────────────────────────────────────────────
echo [....] Upgrading pip...
python -m pip install --upgrade pip --quiet
echo [OK]   pip upgraded.

:: ── 5. Install requirements ──────────────────────────────────────────────────
echo [....] Installing Python dependencies...
pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] pip install failed. Check your internet connection.
    pause & exit /b 1
)
echo [OK]   Dependencies installed.

:: ── 6. Playwright browser ────────────────────────────────────────────────────
echo [....] Installing Playwright Chromium browser...
playwright install chromium
if errorlevel 1 (
    echo [WARN] Playwright install may have failed.
    echo        Run manually later: playwright install chromium
) else (
    echo [OK]   Playwright ready.
)

:: ── 7. Create directories ────────────────────────────────────────────────────
if not exist input_instagram  mkdir input_instagram
if not exist input_behance    mkdir input_behance
if not exist history          mkdir history
if not exist .browser_state   mkdir .browser_state
if not exist .queue           mkdir .queue
echo [OK]   Required directories created.

:: ── 8. Copy .env template ────────────────────────────────────────────────────
if not exist ".env" (
    copy .env.example .env >nul
    echo [OK]   .env created from .env.example.
    echo [!]    Open .env and fill in your API credentials before launching.
) else (
    echo [SKIP] .env already exists.
)

:: ── Done ─────────────────────────────────────────────────────────────────────
echo.
echo ============================================================
echo  Setup complete!
echo.
echo  Next steps:
echo    1. Open .env and add your API credentials
echo    2. Double-click start.bat  (or run: python launch.py)
echo ============================================================
echo.
pause
