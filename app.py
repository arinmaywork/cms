"""
app.py — Local CMS UI (Streamlit)
─────────────────────────────────────────────────────────────────────────────
Launched BY launch.py as a subprocess.  Does NOT start the watcher.
Queue communication happens via .queue/*.json (src/file_queue.py).
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env", override=True)  # override=True so token updates are picked up without restart

import streamlit as st

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Local Social CMS",
    page_icon="📱",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ── Auto-refresh every 3 s to pick up new watcher events ─────────────────────
try:
    from streamlit_autorefresh import st_autorefresh
    st_autorefresh(interval=3_000, key="cms_autorefresh")
except ImportError:
    pass

# ── Main UI ───────────────────────────────────────────────────────────────────
st.title("📱 Local Social Media CMS")
st.caption(
    "Drop a project folder into **`input_instagram/`** or **`input_behance/`** "
    "— the launcher watcher will route it here automatically."
)

tab_ig, tab_bh = st.tabs(["📸 Instagram", "🎨 Behance"])

with tab_ig:
    from ui.instagram_ui import render_instagram_ui
    render_instagram_ui()

with tab_bh:
    from ui.behance_ui import render_behance_ui
    render_behance_ui()

# ── Sidebar history viewer ────────────────────────────────────────────────────
with st.sidebar:
    st.header("📋 History Viewer")
    platform = st.radio("Platform", ["instagram", "behance"], horizontal=True)
    n = st.slider("Last N entries", 1, 20, 5)

    from src.history_manager import get_last_n
    entries = get_last_n(platform, n)

    if not entries:
        st.info("No history yet.")
    else:
        for e in reversed(entries):
            with st.expander(f"[{e['timestamp'][:10]}] {e['project']}"):
                st.markdown(f"**Images:** {len(e.get('images', []))}")
                preview = e["content"][:400]
                st.text(preview + ("..." if len(e["content"]) > 400 else ""))

    st.divider()
    st.header("🛠️ Debug Logs")
    log_file = ROOT / "logs" / "streamlit.log"
    if log_file.exists():
        if st.button("🔄 Refresh Logs"):
            st.rerun()
        
        try:
            # Read last 100 lines
            with open(log_file, "r") as f:
                lines = f.readlines()
                log_text = "".join(lines[-100:])
            
            st.code(log_text, language="log")
            
            if st.button("🗑️ Clear Log File"):
                with open(log_file, "w"):
                    pass  # truncate file safely with context manager
                st.success("Logs cleared.")
                st.rerun()
        except Exception as e:
            st.error(f"Could not read logs: {e}")
    else:
        st.info("No logs found yet. Start automation to generate logs.")
