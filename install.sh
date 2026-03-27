#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

G="\033[0;32m"; Y="\033[0;33m"; R="\033[0;31m"; B="\033[1m"; X="\033[0m"
ok()   { echo -e "${G}[OK]${X}    $*"; }
warn() { echo -e "${Y}[WARN]${X}  $*"; }
step() { echo -e "${B}[....]${X}  $*"; }
die()  { echo -e "${R}[ERROR]${X} $*" >&2; exit 1; }

echo ""
echo -e "${B}╔══════════════════════════════════════════════════════╗${X}"
echo -e "${B}║       Local Social CMS — Full Installer              ║${X}"
echo -e "${B}╚══════════════════════════════════════════════════════╝${X}"
echo ""

# ── 1. Find Python 3.10+ ─────────────────────────────────────────────────────
step "Looking for Python 3.10+..."
PYTHON=""
for cmd in python3.13 python3.12 python3.11 python3.10 python3 python; do
    if command -v "$cmd" &>/dev/null; then
        maj=$("$cmd" -c 'import sys; print(sys.version_info.major)')
        min=$("$cmd" -c 'import sys; print(sys.version_info.minor)')
        if [ "$maj" -ge 3 ] && [ "$min" -ge 10 ]; then
            PYTHON="$cmd"; VER="$maj.$min"; break
        fi
    fi
done
[ -z "$PYTHON" ] && die "Python 3.10+ not found. Install from https://python.org"
ok "Python $VER ($PYTHON)"

# ── 2. Virtual environment ────────────────────────────────────────────────────
if [ -d ".venv" ]; then
    warn ".venv exists — reusing it."
else
    step "Creating .venv..."
    "$PYTHON" -m venv .venv
    ok ".venv created."
fi
source .venv/bin/activate
ok "Virtual environment activated."

# ── 3. Upgrade pip ────────────────────────────────────────────────────────────
step "Upgrading pip..."
pip install --upgrade pip --quiet
ok "pip ready."

# ── 4. Install all packages ───────────────────────────────────────────────────
step "Installing Python packages..."
pip install \
    "streamlit>=1.35.0,<2.0.0" \
    "streamlit-quill>=0.0.3" \
    "streamlit-autorefresh>=1.0.1" \
    "colorama>=0.4.6" \
    "watchdog>=4.0.0" \
    "google-generativeai>=0.8.0" \
    "requests>=2.32.0" \
    "playwright>=1.44.0" \
    "beautifulsoup4>=4.12.0" \
    "Pillow>=10.3.0" \
    "python-dotenv>=1.0.0" \
    --quiet
ok "All packages installed."

# ── 5. Playwright browser ────────────────────────────────────────────────────
step "Installing Playwright Chromium..."
playwright install chromium && ok "Playwright Chromium ready." \
    || warn "Playwright install failed. Run later: playwright install chromium"

# ── 6. Directories ────────────────────────────────────────────────────────────
mkdir -p input_instagram input_behance history .browser_state .queue
ok "Directories ready."

# ── 7. .env ───────────────────────────────────────────────────────────────────
if [ ! -f ".env" ] && [ -f ".env.example" ]; then
    cp .env.example .env
    ok ".env created from .env.example."
    warn "Open .env and add your GEMINI_API_KEY before launching."
elif [ ! -f ".env" ]; then
    cat > .env << 'ENVEOF'
# Get free Gemini key at: https://aistudio.google.com/apikey
GEMINI_API_KEY=AIzaSyYour-Key-Here

INSTAGRAM_ACCESS_TOKEN=EAAyour-token-here
INSTAGRAM_USER_ID=your-ig-user-id

BEHANCE_EMAIL=you@example.com
BEHANCE_PASSWORD=your-password
ENVEOF
    ok ".env created. Add your GEMINI_API_KEY before launching."
else
    warn ".env already exists — skipping."
fi

# ── 8. Write requirements.txt ────────────────────────────────────────────────
cat > requirements.txt << 'REQEOF'
streamlit>=1.35.0,<2.0.0
streamlit-quill>=0.0.3
streamlit-autorefresh>=1.0.1
colorama>=0.4.6
watchdog>=4.0.0
google-generativeai>=0.8.0
requests>=2.32.0
playwright>=1.44.0
beautifulsoup4>=4.12.0
Pillow>=10.3.0
python-dotenv>=1.0.0
REQEOF
ok "requirements.txt written."

# ── 9. Write setup.sh / start.sh ─────────────────────────────────────────────
cat > setup.sh << 'SHEOF'
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
echo "Re-running full installer..."
bash install.sh
SHEOF
chmod +x setup.sh

cat > start.sh << 'SHEOF'
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -f ".venv/bin/activate" ]; then
    echo "ERROR: Run:  bash install.sh  first."
    exit 1
fi
source .venv/bin/activate
echo ""
echo "  Starting Local Social Media CMS..."
echo "  Press Ctrl+C to stop."
echo ""
exec python launch.py "$@"
SHEOF
chmod +x start.sh install.sh
ok "start.sh and setup.sh written."

# ── Done ─────────────────────────────────────────────────────────────────────
echo ""
echo -e "${B}╔══════════════════════════════════════════════════════════╗${X}"
echo -e "${B}║  Installation complete!                                  ║${X}"
echo -e "${B}║                                                          ║${X}"
echo -e "${B}║  1. Open .env  →  add your GEMINI_API_KEY                ║${X}"
echo -e "${B}║     Get a free key: https://aistudio.google.com/apikey   ║${X}"
echo -e "${B}║                                                          ║${X}"
echo -e "${B}║  2. Run:  ./start.sh                                     ║${X}"
echo -e "${B}╚══════════════════════════════════════════════════════════╝${X}"
echo ""
