"""
ui/settings_ui.py — ⚙️ Settings page + the compact status strip.

Everything that used to crowd the sidebar on every page lives here:
connections (Instagram / Behance / YouTube), YouTube quota, history, logs.
"""

import os
import subprocess
import sys
from pathlib import Path

import streamlit as st

import src.youtube_auth as yta
import src.youtube_batch as ytb
import src.youtube_quota as ytq
from src.history_manager import get_last_n
from src.instagram_token import refresh_if_due, status as ig_status

ROOT = Path(__file__).resolve().parent.parent
BH_STATE = ROOT / ".browser_state" / "behance_state.json"


@st.cache_data(ttl=120, show_spinner=False)
def yt_valid() -> bool:
    return yta.is_valid()


@st.cache_data(ttl=600, show_spinner=False)
def yt_channel() -> dict | None:
    return yta.channel_info()


def _clear_yt_caches() -> None:
    yt_valid.clear()
    yt_channel.clear()


# ── Status strip (top of every page) ──────────────────────────────────────────

def render_status_strip() -> None:
    ig = ig_status()
    ig_txt = ("⚪ IG not set" if not ig["configured"] else
              "🟢 IG" if ig["ok"] else "🔴 IG token invalid")
    bh_txt = "🟢 Behance" if BH_STATE.exists() else "🔴 Behance login missing"
    if not yta.has_token():
        yt_txt = "⚪ YouTube not connected"
    elif not yt_valid():
        yt_txt = "🔴 YouTube login expired"
    else:
        u = ytq.usage()
        yt_txt = f"🟢 YouTube {u['units_used']:,}/{u['daily_quota']:,} units"
    c = ytb.counts()
    q_txt = f" · 📦 queue {c['done']}/{c['total']}" if c["total"] else ""
    st.caption(f"{ig_txt} · {bh_txt} · {yt_txt}{q_txt}")


# ── YouTube account ───────────────────────────────────────────────────────────

@st.fragment(run_every=3)
def _yt_device_flow_waiting() -> None:
    auth = yta.read_auth_status()
    if auth.get("status") == "pending":
        st.info(f"1. On any device open **{auth.get('verification_url', 'google.com/device')}**\n\n"
                f"2. Enter code: **`{auth.get('user_code', '')}`**\n\nWaiting for approval…")
        if st.button("Cancel sign-in", key="yt_cancel_auth"):
            yta.clear_auth_status()
            st.rerun()
        return
    if auth.get("status") == "granted":
        yta.clear_auth_status()
        _clear_yt_caches()
        if ytb.read().get("pause_reason") == "auth":   # queue was paused by expired login
            ytb.set_paused(False)
            ytb.ensure_worker()
        st.rerun()


def _yt_connect_button() -> None:
    auth = yta.read_auth_status()
    if auth.get("status") in ("pending", "granted"):
        _yt_device_flow_waiting()
        return
    if auth.get("status") in ("denied", "expired", "error"):
        st.error(f"Sign-in {auth['status']}. Try again.")
    if st.button("🔐 Connect YouTube account", key="yt_connect", type="primary"):
        try:
            yta.start_device_flow()
        except Exception as e:
            st.error(str(e))
            return
        st.rerun()


def _youtube_section() -> None:
    st.subheader("▶️ YouTube")
    if not yta.has_client_secret():
        st.warning("No API credentials. Follow YOUTUBE_SETUP.md, then place the JSON at "
                   "`.secrets/youtube_client_secret.json`.")
        return
    if yta.has_token():
        if yt_valid():
            info = yt_channel()
            st.success(f"Connected: **{info['title']}**" if info else "Connected")
        else:
            st.error("Login expired or revoked — reconnect:")
            _yt_connect_button()
        if st.button("Sign out of YouTube", key="yt_signout"):
            yta.sign_out()
            yta.clear_auth_status()
            _clear_yt_caches()
            st.rerun()
        u = ytq.usage()
        cap = str(u["uploads_cap"]) if u["uploads_cap"] > 0 else "auto"
        st.progress(min(1.0, u["units_used"] / max(1, u["daily_quota"])),
                    text=f"API quota {u['units_used']:,}/{u['daily_quota']:,} · "
                         f"uploads today {u['uploads_today']} (limit: {cap}) · "
                         f"resets {ytq.time_until_reset_str()} (midnight PT)")
    else:
        _yt_connect_button()


# ── Instagram / Behance ───────────────────────────────────────────────────────

def _instagram_section() -> None:
    st.subheader("📸 Instagram")
    s = ig_status()
    if not s["configured"]:
        st.warning("INSTAGRAM_ACCESS_TOKEN not set in .env")
        return
    days = f" · ~{s['days_left']} days left" if s["days_left"] is not None else ""
    (st.success if s["ok"] else st.error)(f"Token: {s['last_status']}{days}")
    if not s["ok"]:
        st.caption("A revoked token (e.g. after a password change) can't be refreshed. "
                   "Meta developer portal → your app → Instagram → API Setup → Generate "
                   "Token → paste into `.env` → restart. Auto-refresh takes over again.")
    if st.button("Check / refresh token now", key="ig_refresh_now"):
        with st.spinner("Contacting Instagram…"):
            st.toast(refresh_if_due(force=True))
        st.rerun()


def _behance_section() -> None:
    st.subheader("🎨 Behance")
    if BH_STATE.exists():
        st.success("Session saved")
    else:
        st.error("No session")
    st.caption("Behance logins need a visible browser. On the VM: log in on your Mac "
               "(button below), then `scp .browser_state/behance_state.json` to the VM.")
    if st.button("🔄 Behance login (opens a browser on this machine)", key="bh_login"):
        script = ROOT / "save_behance_session.py"
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-a", "Terminal", str(script)])
        else:
            kw = {"creationflags": subprocess.CREATE_NEW_CONSOLE} if os.name == "nt" else {}
            subprocess.Popen([sys.executable, str(script)], **kw)
        st.toast("Complete the login in the window that opened.")


# ── History / logs ────────────────────────────────────────────────────────────

def _tail(path: Path, n_bytes: int = 24_000) -> str:
    with open(path, "rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - n_bytes))
        return f.read().decode("utf-8", "replace")


def render_settings_ui() -> None:
    st.header("⚙️ Settings & connections")
    c1, c2 = st.columns(2)
    with c1:
        _youtube_section()
        _instagram_section()
    with c2:
        _behance_section()

    st.divider()
    st.subheader("📋 History")
    hc1, hc2 = st.columns([2, 1])
    platform = hc1.segmented_control("Platform", ["instagram", "behance", "youtube"],
                                     default="youtube", key="hist_platform")
    n = hc2.number_input("Last N", 1, 50, 10, key="hist_n")
    for e in reversed(get_last_n(platform or "youtube", int(n))):
        with st.expander(f"[{e['timestamp'][:10]}] {e['project']}"):
            st.text(e["content"][:600])

    st.divider()
    st.subheader("🛠️ Logs")
    st.caption("UI log tail below. Background services (upload queue, uploads, token "
               "refresh) log to the launcher: `journalctl -u cms -f` on the VM.")
    log = ROOT / "logs" / "streamlit.log"
    if log.exists():
        st.code(_tail(log), language="log")
    else:
        st.info("No log yet.")
