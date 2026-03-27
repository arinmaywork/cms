#!/usr/bin/env bash
set -euo pipefail

# ══════════════════════════════════════════════════════════════════
#  Local Social CMS — Setup Script (Mac / Linux)
#  Run once after cloning/unzipping the project.
# ══════════════════════════════════════════════════════════════════

BOLD="\033[1m"
GREEN="\033[0;32m"
YELLOW="\033[0;33m"
RED="\033[0;31m"
RESET="\033[0m"

info()  { echo -e "${GREEN}[OK]${RESET}   $*"; }
warn()  { echo -e "${YELLOW}[WARN]${RESET} $*"; }
step()  { echo -e "${BOLD}[....]${RESET} $*"; }
error() { echo -e "${RED}[ERROR]${RESET} $*" >&2; exit 1; }

# Move to the directory this script lives in
cd "$(dirname "$0")"

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║   Local Social CMS — Setup (Mac / Linux)     ║"
echo "╚══════════════════════════════════════════════╝"
echo ""

# ── 1. Detect Python 3.10+ ────────────────────────────────────────────────────
PYTHON=""
for cmd in python3.13 python3.12 python3.11 python3.10 python3 python; do
    if command -v "$cmd" &>/dev/null; then
        VER=$("$cmd" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
        MAJOR=$(echo "$VER" | cut -d. -f1)
        MINOR=$(echo "$VER" | cut -d. -f2)
        if [ "$MAJOR" -ge 3 ] && [ "$MINOR" -ge 10 ]; then
            PYTHON="$cmd"
            break
        fi
    fi
done

[ -z "$PYTHON" ] && error "Python 3.10+ is required. Install from https://python.org"
info "Python $VER found ($PYTHON)."

# ── 2. Create virtual environment ─────────────────────────────────────────────
if [ -d ".venv" ]; then
    warn ".venv already exists — skipping creation."
else
    step "Creating virtual environment (.venv)…"
    "$PYTHON" -m venv .venv
    info "Virtual environment created."
fi

# ── 3. Activate virtual environment ───────────────────────────────────────────
# shellcheck disable=SC1091
source .venv/bin/activate
info "Virtual environment activated."

# ── 4. Upgrade pip ────────────────────────────────────────────────────────────
step "Upgrading pip…"
pip install --upgrade pip --quiet
info "pip upgraded."

# ── 5. Install Python dependencies ────────────────────────────────────────────
step "Installing Python dependencies from requirements.txt…"
pip install -r requirements.txt --quiet || error "pip install failed — check your internet connection."
info "Dependencies installed."

# ── 6. Install Playwright browser ─────────────────────────────────────────────
step "Installing Playwright Chromium browser…"
playwright install chromium || warn "Playwright install may have failed. Run manually: playwright install chromium"
info "Playwright ready."

# ── 7. Create required directories ────────────────────────────────────────────
mkdir -p input_instagram input_behance history .browser_state .queue
info "Required directories created."

# ── 8. Copy .env template if missing ──────────────────────────────────────────
if [ ! -f ".env" ]; then
    cp .env.example .env
    info ".env created from .env.example."
    echo ""
    warn "ACTION REQUIRED: Open .env and fill in your API credentials before launching."
else
    warn ".env already exists — skipping copy."
fi

# ── 9. Make scripts executable ────────────────────────────────────────────────
chmod +x setup.sh start.sh
info "Scripts marked as executable."

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Setup complete!                                             ║"
echo "║                                                              ║"
echo "║  Next steps:                                                 ║"
echo "║    1. Open .env and add your API credentials                 ║"
echo "║    2. Run:  ./start.sh                                       ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
