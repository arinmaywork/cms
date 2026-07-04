"""
app.py — Local CMS UI (Streamlit)
─────────────────────────────────────────────────────────────────────────────
Launched BY launch.py as a subprocess.  Does NOT start the watcher.
Queue communication happens via .queue/*.json (src/file_queue.py).

Remote access: set APP_PASSWORD in .env to require a login before the UI
renders (recommended when running on a VM). See DEPLOYMENT.md for the full
Oracle-VM guide (HTTPS reverse proxy / Tailscale).
"""

import hmac
import os
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

# ── Password gate (only if APP_PASSWORD is set) ───────────────────────────────
_APP_PASSWORD = os.getenv("APP_PASSWORD", "")
if _APP_PASSWORD:
    if not st.session_state.get("auth_ok", False):
        st.title("🔒 Local Social CMS")
        with st.form("login_form"):
            pw = st.text_input("Password", type="password", key="login_pw")
            submitted = st.form_submit_button("Sign in", type="primary")
        if submitted:
            if hmac.compare_digest(pw, _APP_PASSWORD):
                st.session_state.auth_ok = True
                st.rerun()
            else:
                st.error("Wrong password.")
        st.stop()

# ── Auto-refresh every 3 s to pick up new watcher events ─────────────────────
# Can be paused from any tab's upload widget: constant reruns interfere with
# large browser uploads (files appear client-side but never register server-side).
_upload_paused = any(
    st.session_state.get(f"upl_pause_{p}", False)
    for p in ("instagram", "behance", "youtube")
)
if not _upload_paused:
    try:
        from streamlit_autorefresh import st_autorefresh
        st_autorefresh(interval=3_000, key="cms_autorefresh")
    except ImportError:
        pass

# ── Weekly Instagram token auto-refresh (cheap no-op when not due) ────────────
try:
    from src.instagram_token import refresh_if_due
    _ig_token_status = refresh_if_due()
except Exception as _e:
    _ig_token_status = f"check failed: {_e}"

# ── Main UI ───────────────────────────────────────────────────────────────────
st.title("📱 Local Social Media CMS")
st.caption(
    "Drop a project folder into **`input_instagram/`**, **`input_behance/`** "
    "or **`input_youtube/`** — the watcher routes it here automatically."
)

tab_ig, tab_bh, tab_yt = st.tabs(["📸 Instagram", "🎨 Behance", "▶️ YouTube"])

with tab_ig:
    from ui.instagram_ui import render_instagram_ui
    render_instagram_ui()

with tab_bh:
    from ui.behance_ui import render_behance_ui
    render_behance_ui()

with tab_yt:
    from ui.youtube_ui import render_youtube_ui
    render_youtube_ui()

# ── Sidebar: history viewer + connections + logs ──────────────────────────────
with st.sidebar:
    st.header("🔗 Connections")
    from src.instagram_token import status as ig_token_status
    _s = ig_token_status()
    if os.getenv("INSTAGRAM_ACCESS_TOKEN"):
        if _s["days_left"] is not None:
            icon = "🟢" if _s["days_left"] > 10 else "🟠"
            st.caption(f"{icon} Instagram token: ~{_s['days_left']}d left "
                       f"(auto-refresh: {_ig_token_status})")
        else:
            st.caption(f"🟢 Instagram token set (auto-refresh: {_ig_token_status})")
    else:
        st.caption("🔴 Instagram token not configured")

    st.divider()
    st.header("📋 History Viewer")
    platform = st.radio("Platform", ["instagram", "behance", "youtube"], horizontal=True)
    n = st.slider("Last N entries", 1, 20, 5)

    from src.history_manager import get_last_n
    entries = get_last_n(platform, n)

    if not entries:
        st.info("No history yet.")
    else:
        for e in reversed(entries):
            with st.expander(f"[{e['timestamp'][:10]}] {e['project']}"):
                st.markdown(f"**Files:** {len(e.get('images', []))}")
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
