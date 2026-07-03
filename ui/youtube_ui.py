"""
ui/youtube_ui.py
─────────────────────────────────────────────────────────────────────────────
YouTube publisher tab.

Flow:
  1. Drop a folder with video file(s) into input_youtube/
     (optionally include a .jpg/.png to use as thumbnail)
  2. Connect the YouTube account once (device-code flow — works on a VM too)
  3. 🤖 Automate: Gemini analyses sampled frames → title/description/tags
  4. Review, choose privacy (public/unlisted/private/scheduled), playlist,
     made-for-kids flag, thumbnail
  5. Publish — resumable upload with live progress, quota-aware
"""

import datetime as _dt
import pathlib
import traceback

import streamlit as st

import src.progress as progress
import src.youtube_auth as yta
import src.youtube_quota as ytq
from src.ai_generator import generate_youtube_metadata, extract_thumbnail_frame
from src.file_queue import pop_one as fq_pop_one
from src.history_manager import save_entry
from src.youtube_publisher import (
    CATEGORIES, TITLE_MAX, TAGS_MAX_CHARS,
    find_videos, find_thumbnails, list_playlists, publish_to_youtube,
)
from ui.progress_widget import start_publish_thread, render_progress
from ui.folder_picker import render_folder_picker

PRIVACY_OPTIONS = ["unlisted", "public", "private"]


# ── Session state ─────────────────────────────────────────────────────────────

def _init():
    defaults = {
        "yt_project":      None,
        "yt_queue":        [],
        "yt_status":       "",
        "yt_status_type":  "info",
        "yt_publishing":   False,
        "yt_automating":   False,
        "yt_meta":         {},     # {video_name: {"title","description","tags_str"}}
        "yt_playlists":    None,   # cached list or None (not loaded)
        "yt_device_code":  None,   # active device-flow info dict
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def _set_status(msg, kind="info"):
    st.session_state.yt_status = msg
    st.session_state.yt_status_type = kind


def _clear():
    st.session_state.yt_project    = None
    st.session_state.yt_meta       = {}
    st.session_state.yt_status     = ""
    st.session_state.yt_publishing = False
    st.session_state.yt_automating = False
    for key in list(st.session_state.keys()):
        if key.startswith(("yt_title_", "yt_desc_", "yt_tags_", "yt_thumb_",
                           "yt_privacy_", "yt_kids_", "yt_sched_")):
            del st.session_state[key]


# ── Background AI worker ──────────────────────────────────────────────────────

def _run_automation(videos: list[pathlib.Path], project_name: str, notes: str):
    """Generate metadata for every video. Runs in a daemon thread — no st.* here."""
    labels = [f"🎬 Analyse {v.name}" for v in videos]
    progress.start("yt_ai", labels)
    meta: dict[str, dict] = {}
    try:
        for i, v in enumerate(videos):
            progress.update("yt_ai", i, "active",
                            "Extracting frames + calling Gemini…")
            m = generate_youtube_metadata(v, project_name, notes)
            meta[v.name] = {
                "title":       m["title"],
                "description": m["description"],
                "tags_str":    ", ".join(m["tags"]),
            }
            progress.update("yt_ai", i, "done", f"'{m['title'][:60]}'")
        progress.finish("yt_ai", {"success": True, "meta": meta})
    except Exception:
        progress.fail("yt_ai", traceback.format_exc())


# ── Sidebar: account + quota ──────────────────────────────────────────────────

def _render_sidebar():
    with st.sidebar:
        st.divider()
        st.header("▶️ YouTube Account")

        if not yta.has_client_secret():
            st.warning(
                "No API credentials yet. Follow **YOUTUBE_SETUP.md** "
                "(5-minute, one-time setup), then place the downloaded JSON at "
                f"`{yta.client_secret_path().name}` inside the `.secrets/` folder."
            )
            return

        if yta.has_token():
            # Cache channel info — costs 1 quota unit + an API round-trip,
            # and this sidebar re-renders every 3s on autorefresh.
            if "yt_channel_info" not in st.session_state:
                st.session_state.yt_channel_info = yta.channel_info()
            info = st.session_state.yt_channel_info
            if info:
                st.success(f"Connected: **{info['title']}**")
            else:
                st.success("Connected")
            if st.button("Sign out of YouTube", key="yt_signout"):
                yta.sign_out()
                yta.clear_auth_status()
                st.session_state.pop("yt_channel_info", None)
                st.rerun()

            # Quota panel
            u = ytq.usage()
            st.caption(
                f"API quota today: **{u['units_used']:,} / {u['daily_quota']:,}** units\n\n"
                f"Uploads today: **{u['uploads_today']} / {u['uploads_cap']}**\n\n"
                f"Resets {ytq.time_until_reset_str()} (midnight PT)"
            )
            st.progress(min(1.0, u["units_used"] / max(1, u["daily_quota"])))
            return

        # Not signed in — device flow
        auth = yta.read_auth_status()
        if auth.get("status") == "pending":
            st.info(
                f"1. On any device open **{auth.get('verification_url','google.com/device')}**\n\n"
                f"2. Enter code: ### `{auth.get('user_code','')}`\n\n"
                "Waiting for approval… (this page refreshes automatically)"
            )
            if st.button("Cancel sign-in", key="yt_cancel_auth"):
                yta.clear_auth_status()
                st.rerun()
        elif auth.get("status") == "granted":
            yta.clear_auth_status()
            st.rerun()
        else:
            if auth.get("status") in ("denied", "expired", "error"):
                st.error(f"Sign-in {auth['status']}. Try again.")
            if st.button("🔐 Connect YouTube account", key="yt_connect",
                         type="primary", width="stretch"):
                try:
                    yta.start_device_flow()
                except Exception as e:
                    _set_status(f"❌ {e}", "error")
                st.rerun()


# ── Main render ───────────────────────────────────────────────────────────────

def render_youtube_ui():
    _init()
    st.header("▶️ YouTube Publisher")
    _render_sidebar()

    # ── Drain queue ───────────────────────────────────────────────────────────
    while True:
        new_path = fq_pop_one("youtube")
        if not new_path:
            break
        if str(new_path) not in [str(p) for p in st.session_state.yt_queue]:
            st.session_state.yt_queue.append(new_path)

    if not st.session_state.yt_project and st.session_state.yt_queue:
        st.session_state.yt_project = st.session_state.yt_queue.pop(0)
        _set_status(f"✅ Loaded from queue: **{st.session_state.yt_project.name}**")
        st.rerun()

    if st.session_state.yt_queue:
        with st.sidebar:
            st.divider()
            st.info(f"📁 **{len(st.session_state.yt_queue)}** more video project(s) pending.")
            for i, p in enumerate(st.session_state.yt_queue[:5]):
                st.caption(f"{i+1}. {p.name}")

    # ── AI automation in progress ─────────────────────────────────────────────
    if st.session_state.yt_automating:
        st.subheader("🤖 Analysing videos…")
        placeholder = st.empty()
        result = render_progress("yt_ai", placeholder)
        if result is not None:
            if result.get("success"):
                meta = result.get("meta", {})
                st.session_state.yt_meta.update(meta)
                # Seed widget keys
                project = st.session_state.yt_project
                videos = find_videos(project) if project else []
                for j, v in enumerate(videos):
                    m = meta.get(v.name)
                    if m:
                        st.session_state[f"yt_title_{j}"] = m["title"]
                        st.session_state[f"yt_desc_{j}"]  = m["description"]
                        st.session_state[f"yt_tags_{j}"]  = m["tags_str"]
                st.session_state.yt_automating = False
                _set_status("✅ AI metadata ready — review below, then publish.", "success")
            else:
                st.session_state.yt_automating = False
                _set_status(f"❌ AI failed: {result.get('error','')[:300]}", "error")
            st.rerun()
        st.stop()

    # ── Publishing in progress ────────────────────────────────────────────────
    if st.session_state.yt_publishing:
        st.subheader("🚀 Publishing to YouTube…")
        placeholder = st.empty()
        result = render_progress("yt", placeholder)
        if result is not None:
            results = result.get("results", [])
            if result.get("success"):
                links = "\n\n".join(f"✅ [{r['title']}]({r['url']}) — {r['privacy']}"
                                    for r in results)
                _set_status(f"✅ Published {len(results)} video(s)!\n\n{links}", "success")
                _clear()
            else:
                partial = (f"\n\n{len(results)} video(s) DID upload before the "
                           f"error: " + ", ".join(r["url"] for r in results)
                           if results else "")
                _set_status(f"❌ {result.get('error','Unknown error')}{partial}", "error")
                st.session_state.yt_publishing = False
            st.rerun()
        st.stop()

    # ── Status banner ─────────────────────────────────────────────────────────
    if st.session_state.yt_status:
        getattr(st, st.session_state.yt_status_type)(st.session_state.yt_status)

    # ── No project ────────────────────────────────────────────────────────────
    if not st.session_state.yt_project:
        st.markdown("### ⏳ Waiting for a project…")
        st.caption("Drop a folder containing video file(s) into `input_youtube/`. "
                   "Add a `.jpg`/`.png` in the same folder to use as thumbnail.")
        render_folder_picker("youtube")
        return

    project = st.session_state.yt_project
    videos  = find_videos(project)
    thumbs  = find_thumbnails(project)

    if not videos:
        st.error("No video files found in this folder.")
        if st.button("Clear", key="yt_clear_empty"):
            _clear(); st.rerun()
        return

    st.subheader(f"`{project.name}` — {len(videos)} video(s)")

    signed_in = yta.has_token()
    if not signed_in:
        st.warning("Connect your YouTube account in the sidebar before publishing.")

    notes = st.text_input("🗒️ AI context:", key="yt_notes",
                          placeholder="e.g. cinematic travel film, moody tones, shot on XT-30")

    if st.button("🤖 Automate Everything (Analyse videos + AI metadata)",
                 type="primary", width="stretch", key="yt_automate"):
        vids_snapshot = list(videos)
        notes_val = st.session_state.get("yt_notes", "")
        pname = project.name

        def _worker():
            _run_automation(vids_snapshot, pname, notes_val)

        start_publish_thread(fn=_worker, platform="yt_ai")
        st.session_state.yt_automating = True
        st.rerun()

    st.divider()

    # ── Global publish settings ───────────────────────────────────────────────
    st.markdown("### ⚙️ Publish Settings (applies to all videos)")
    c1, c2, c3 = st.columns(3)
    with c1:
        privacy = st.radio("Visibility", PRIVACY_OPTIONS, index=0, key="yt_privacy_g",
                           help="Unlisted = anyone with the link. Public = searchable.")
    with c2:
        category_name = st.selectbox("Category", list(CATEGORIES.keys()),
                                     index=list(CATEGORIES.keys()).index("People & Blogs"),
                                     key="yt_category_g")
    with c3:
        made_for_kids = st.checkbox("Made for kids (COPPA)", value=False, key="yt_kids_g",
                                    help="Required declaration. Most creator content is NOT made for kids.")
        notify_subs = st.checkbox("Notify subscribers", value=True, key="yt_notify_g")

    if privacy == "public":
        st.info(
            "ℹ️ **Public uploads via API:** if your Google Cloud project hasn't "
            "completed YouTube's API audit, YouTube locks API-uploaded videos to "
            "*private* automatically. If that happens, upload as **unlisted** and "
            "flip to public in YouTube Studio (10 seconds), or request the audit "
            "once — see YOUTUBE_SETUP.md.",
            icon="🔒",
        )

    # Scheduling
    sched_on = st.checkbox("📅 Schedule publish time", value=False, key="yt_sched_on",
                           help="Video uploads now as private, goes live automatically at the chosen time.")
    publish_at_iso = None
    if sched_on:
        sc1, sc2 = st.columns(2)
        with sc1:
            d = st.date_input("Date", value=_dt.date.today() + _dt.timedelta(days=1),
                              key="yt_sched_d")
        with sc2:
            t = st.time_input("Time (your local time)", value=_dt.time(18, 0),
                              key="yt_sched_t")
        local_dt = _dt.datetime.combine(d, t).astimezone()
        publish_at_iso = local_dt.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        st.caption(f"Will go live: {local_dt.strftime('%a %d %b %Y, %H:%M %Z')}")

    # Playlist
    st.markdown("**Playlist**")
    pc1, pc2, pc3 = st.columns([2, 2, 1])
    playlist_id = ""
    with pc3:
        if st.button("🔄 Load playlists", key="yt_load_pl", disabled=not signed_in):
            try:
                st.session_state.yt_playlists = list_playlists()
            except Exception as e:
                _set_status(f"❌ Could not load playlists: {e}", "error")
            st.rerun()
    with pc1:
        pls = st.session_state.yt_playlists
        if pls:
            options = ["(none)"] + [f"{p['title']} ({p['count']})" for p in pls]
            sel = st.selectbox("Add to existing playlist", options, key="yt_pl_sel")
            if sel != "(none)":
                playlist_id = pls[options.index(sel) - 1]["id"]
        else:
            st.selectbox("Add to existing playlist",
                         ["(click 'Load playlists' first)"], key="yt_pl_sel_empty",
                         disabled=True)
    with pc2:
        new_playlist_title = st.text_input("…or create new playlist", key="yt_pl_new",
                                           placeholder="e.g. Travel Films 2026")

    st.divider()

    # ── Per-video metadata ────────────────────────────────────────────────────
    st.markdown("### 📝 Video Details")
    thumb_options = ["(auto: YouTube default)"] + [t.name for t in thumbs]
    has_multi = len(videos) > 1

    for i, v in enumerate(videos):
        with st.expander(f"🎬 **{v.name}**  ({v.stat().st_size/(1024*1024):.0f} MB)",
                         expanded=not has_multi):
            try:
                st.video(str(v))
            except Exception:
                pass

            title = st.text_input(f"Title", key=f"yt_title_{i}",
                                  max_chars=TITLE_MAX,
                                  placeholder=v.stem.replace("_", " ").title())
            t_len = len(st.session_state.get(f"yt_title_{i}", ""))
            st.caption(f"{t_len}/{TITLE_MAX} characters")

            st.text_area("Description", key=f"yt_desc_{i}", height=200,
                         placeholder="First two lines show above the fold — hook here.")

            tags_str = st.text_input("Tags (comma-separated)", key=f"yt_tags_{i}",
                                     placeholder="travel film, cinematic, fujifilm")
            tag_chars = len(st.session_state.get(f"yt_tags_{i}", ""))
            st.caption(f"~{tag_chars}/{TAGS_MAX_CHARS} characters total")

            tc1, tc2 = st.columns([2, 1])
            with tc1:
                thumb_sel = st.selectbox("Thumbnail", thumb_options, key=f"yt_thumb_{i}")
            with tc2:
                if st.button("🖼 Extract frame from video", key=f"yt_extract_{i}"):
                    p = extract_thumbnail_frame(v)
                    if p:
                        _set_status(f"✅ Frame saved as `{p.name}` — reselect thumbnail.", "success")
                    else:
                        _set_status("❌ ffmpeg not available — cannot extract frame.", "error")
                    st.rerun()

    st.divider()

    # ── Pre-flight + publish ──────────────────────────────────────────────────
    st.markdown("### 🔍 Final Review")

    items = []
    problems = []
    for i, v in enumerate(videos):
        title = st.session_state.get(f"yt_title_{i}", "").strip() or v.stem.replace("_", " ").title()
        desc  = st.session_state.get(f"yt_desc_{i}", "")
        tags  = [t.strip() for t in st.session_state.get(f"yt_tags_{i}", "").split(",") if t.strip()]
        thumb_sel = st.session_state.get(f"yt_thumb_{i}", thumb_options[0])
        thumb = None
        if thumb_sel and thumb_sel != "(auto: YouTube default)":
            cand = project / thumb_sel
            thumb = cand if cand.exists() else None
        if not st.session_state.get(f"yt_title_{i}", "").strip():
            problems.append(f"Video {i+1}: no title (will use '{title}')")
        items.append({
            "path":               v,
            "title":              title,
            "description":        desc,
            "tags":               tags,
            "category_id":        CATEGORIES[category_name],
            "privacy":            privacy,
            "made_for_kids":      made_for_kids,
            "publish_at":         publish_at_iso,
            "playlist_id":        playlist_id,
            "new_playlist_title": new_playlist_title.strip(),
            "thumbnail":          str(thumb) if thumb else None,
            "notify_subscribers": notify_subs,
        })

    n_thumbs  = sum(1 for it in items if it["thumbnail"])
    n_pl_adds = sum(1 for it in items if it["playlist_id"] or it["new_playlist_title"])
    n_new_pl  = 1 if new_playlist_title.strip() else 0
    planned   = ytq.estimate_cost(len(items), n_thumbs, n_pl_adds, n_new_pl)
    ok, reason = ytq.can_publish(len(items), planned)

    u = ytq.usage()
    rc1, rc2 = st.columns(2)
    with rc1:
        st.info(f"**Plan:** {len(items)} upload(s) · {privacy}"
                + (f" · scheduled" if publish_at_iso else "")
                + (f" · playlist" if (playlist_id or new_playlist_title.strip()) else ""))
        for p in problems:
            st.caption(f"⚠️ {p}")
    with rc2:
        st.info(f"**Quota:** ~{planned} units needed · {u['units_remaining']:,} left today · "
                f"uploads {u['uploads_today']}/{u['uploads_cap']}")

    if not ok:
        st.error(reason)

    can_publish = signed_in and ok and bool(items)

    bp, bc = st.columns([2, 1])
    with bp:
        if st.button("🚀 APPROVE & UPLOAD TO YOUTUBE", type="primary",
                     width="stretch", disabled=not can_publish,
                     key="yt_publish"):
            for it in items:
                save_entry("youtube", project.name,
                           f"{it['title']}\n\n{it['description']}",
                           [str(it["path"])])
            items_snapshot = [dict(it) for it in items]
            start_publish_thread(
                fn=lambda: publish_to_youtube(items_snapshot),
                platform="yt",
            )
            st.rerun()
    with bc:
        if st.button("🗑️ Clear Project", width="stretch", key="yt_clear"):
            _clear(); st.rerun()
