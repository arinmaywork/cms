#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  start.sh — Launch the Local Social Media CMS
#  Run this every time you want to use the tool.
#
#  Usage:
#    ./start.sh                 # default port 8501, opens browser automatically
#    ./start.sh --port 8888     # custom port
#    ./start.sh --no-browser    # skip auto-open
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# Move to the directory this script lives in (works when double-clicked too)
cd "$(dirname "$0")"

# ── Check virtual environment exists ─────────────────────────────────────────
if [ ! -f ".venv/bin/activate" ]; then
    echo ""
    echo "  ❌  Virtual environment not found."
    echo "      Please run setup first:"
    echo ""
    echo "        chmod +x setup.sh && ./setup.sh"
    echo ""
    exit 1
fi

# ── Activate virtual environment ──────────────────────────────────────────────
# shellcheck disable=SC1091
source .venv/bin/activate

# ── Verify launch.py exists ───────────────────────────────────────────────────
if [ ! -f "launch.py" ]; then
    echo "  ❌  launch.py not found. Make sure you are in the cms-local directory."
    exit 1
fi

# ── Run ──────────────────────────────────────────────────────────────────────
echo ""
echo "  Starting Local Social Media CMS…"
echo "  Press Ctrl+C to stop."
echo ""

exec python launch.py "$@"
