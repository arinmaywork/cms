"""
app.py — Streamlit UI root (launched by launch.py).

Performance model: there is NO whole-app autorefresh. Only the active page
is rendered, and only live widgets (progress panels, the upload queue,
the YouTube sign-in) rerun themselves via @st.fragment(run_every=…).
"""

import hmac
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env", override=True)   # picks up refreshed tokens without restart

import streamlit as st

st.set_page_config(page_title="Social CMS", page_icon="📱", layout="wide",
                   initial_sidebar_state="collapsed")

_PW = os.getenv("APP_PASSWORD", "")
if _PW and not st.session_state.get("auth_ok"):
    st.title("🔒 Social CMS")
    with st.form("login"):
        pw = st.text_input("Password", type="password")
        ok = st.form_submit_button("Sign in", type="primary")
    if ok:
        if hmac.compare_digest(pw, _PW):
            st.session_state.auth_ok = True
            st.rerun()
        st.error("Wrong password.")
    st.stop()

PAGES = {"📸 Instagram": "instagram", "🎨 Behance": "behance",
         "▶️ YouTube": "youtube", "💼 LinkedIn": "linkedin", "🌐 Website": "website",
         "⚙️ Settings": "settings"}

head1, head2 = st.columns([3, 3])
with head1:
    choice = st.segmented_control("Page", list(PAGES), default="▶️ YouTube",
                                  key="nav", label_visibility="collapsed")
with head2:
    from ui.settings_ui import render_status_strip
    render_status_strip()

page = PAGES.get(choice or "▶️ YouTube")
if page == "instagram":
    from ui.instagram_ui import render_instagram_ui
    render_instagram_ui()
elif page == "behance":
    from ui.behance_ui import render_behance_ui
    render_behance_ui()
elif page == "linkedin":
    from ui.linkedin_ui import render_linkedin_ui
    render_linkedin_ui()
elif page == "website":
    from ui.website_ui import render_website_ui
    render_website_ui()
elif page == "youtube":
    from ui.youtube_ui import render_youtube_ui
    render_youtube_ui()
else:
    from ui.settings_ui import render_settings_ui
    render_settings_ui()
