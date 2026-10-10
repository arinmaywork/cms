"""
ui/youtube_ui.py — YouTube page.

• Upload queue panel: a self-refreshing fragment (table + metrics), not a
  whole-page autorefresh.
• AI metadata runs in the background WITHOUT blocking the page; the worker
  writes each video's result to <project>/.cms.json as it goes, filling only
  empty fields — your edits are never overwritten and nothing is lost on a
  refresh or restart.
• Video details are paginated (10/page); no inline players for big projects.
• Queued projects move to archive/youtube/ automatically when fully uploaded.
"""

import datetime as _dt
import math
import traceback
from pathlib import Path

import pandas as pd
import streamlit as st

import src.progress as progress
import src.youtube_auth as yta
import src.youtube_batch as ytb
import src.youtube_quota as ytq
from src.ai_generator import extract_thumbnail_frame, generate_youtube_metadata
from src.history_manager import save_entry
from src.natsort import natkey
from src.projects import load_draft, save_draft
from src.youtube_publisher import (CATEGORIES, TAGS_MAX_CHARS, TITLE_MAX,
                                   find_thumbnails, find_videos, list_playlists)
from ui.folder_picker import render_project_picker
from ui.progress_widget import live_progress, start_publish_thread
from ui.settings_ui import yt_valid

PAGE_SIZE = 10
AUTO_THUMB = "(YouTube default)"


def _init():
    for k, v in {"yt_project": None, "yt_meta": {}, "yt_toast": "", "yt_error": "",
                 "yt_automating": False, "yt_playlists": None}.items():
        st.session_state.setdefault(k, v)


def _drop_widgets():
    for k in [k for k in st.session_state if str(k).startswith("ytw_")]:
        del st.session_state[k]


def _load_meta(path: Path) -> dict:
    return load_draft(path).get("youtube", {}).get("videos", {})


def _open(path: Path):
    st.session_state.yt_project = path
    st.session_state.yt_meta = _load_meta(path)
    _drop_widgets()


def _close():
    st.session_state.yt_project = None
    st.session_state.yt_meta = {}
    _drop_widgets()


def _save():
    p = st.session_state.yt_project
    if p and Path(p).exists():
        data = load_draft(p)
        data.setdefault("youtube", {})["videos"] = st.session_state.yt_meta
        save_draft(p, data)


def _seed(key: str, value):
    st.session_state.setdefault(key, value)
    return key


# ── Background AI (no st.* in here) ───────────────────────────────────────────

def _run_automation(project: Path, videos: list[Path], notes: str, redo: bool) -> None:
    ns = "yt_ai"
    todo = [v for v in videos if redo or not _load_meta(project).get(v.name, {}).get("ai")]
    progress.start(ns, [f"🎬 {v.name}" for v in todo] or ["Nothing to do"])
    try:
        for i, v in enumerate(todo):
            progress.update(ns, i, "active", "Frames + Gemini…")
            m = generate_youtube_metadata(v, project.name, notes)
            data = load_draft(project)                        # re-read: keep user edits
            vids = data.setdefault("youtube", {}).setdefault("videos", {})
            cur = vids.setdefault(v.name, {})
            for key, val in (("title", m["title"]), ("description", m["description"]),
                             ("tags_str", ", ".join(m["tags"]))):
                if redo or not (cur.get(key) or "").strip():
                    cur[key] = val
            cur["ai"] = True
            save_draft(project, data)                         # persisted per video
            progress.update(ns, i, "done", m["title"][:70])
        progress.finish(ns, {"success": True, "count": len(todo)})
    except Exception:
        progress.fail(ns, traceback.format_exc())


def _on_ai_done(result: dict):
    st.session_state.yt_automating = False
    if st.session_state.yt_project:
        st.session_state.yt_meta = _load_meta(st.session_state.yt_project)
        _drop_widgets()
    if result.get("success"):
        st.session_state.yt_toast = f"✅ AI metadata ready for {result.get('count', 0)} video(s)"
    else:
        st.session_state.yt_error = (f"AI stopped: {result.get('error', '')[-400:]} — "
                                     "finished videos are saved; run Automate again to continue.")


# ── Upload queue panel ────────────────────────────────────────────────────────

_ICON = {"pending": "⬜ pending", "uploading": "🔄 uploading", "done": "✅ done",
         "failed": "❌ failed", "deferred": "🕛 waiting reset"}
_FILTER = {"All": None, "Pending": {"pending", "uploading"}, "Waiting": {"deferred"},
           "Failed": {"failed"}, "Done": {"done"}}


@st.fragment(run_every=5)
def _queue_panel():
    batch = ytb.read()
    items = sorted(batch["items"], key=lambda it: natkey(Path(it["meta"]["path"]).name))
    if not items:
        return
    c = ytb.counts()
    remaining = c["pending"] + c["uploading"] + c["deferred"]
    cap = (batch.get("observed_cap") or {}).get("count") or 60
    st.markdown("### 📦 Upload queue")

    import time as _t
    if batch.get("pause_reason") == "auth":
        st.error(f"⏸ {batch.get('limit_note')}", icon="🔐")
    elif batch["resume_at"] and batch["resume_at"] > _t.time():
        when = _dt.datetime.fromtimestamp(batch["resume_at"]).strftime("%a %H:%M")
        st.warning(f"⏸ {batch.get('limit_note', 'Daily limit reached')} — auto-resumes ~{when}",
                   icon="🕛")
    elif batch["paused"]:
        st.info("⏸ Queue paused.")

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Done", f"{c['done']}/{c['total']}")
    m2.metric("Waiting", remaining)
    m3.metric("Failed", c["failed"])
    m4.metric("ETA", f"~{math.ceil(remaining / cap)} day(s)" if remaining else "—")

    live = progress.read("yt")
    if live.get("active"):
        for s in live.get("steps", []):
            if s["status"] == "active":
                st.caption(f"🔄 {s['label']} — {s.get('detail', '')}")

    flt = st.segmented_control("Show", list(_FILTER), default="All", key="yt_q_filter",
                               label_visibility="collapsed") or "All"
    shown = [it for it in items if _FILTER[flt] is None or it["status"] in _FILTER[flt]]
    df = pd.DataFrame([{
        "#": items.index(it) + 1,
        "File": Path(it["meta"]["path"]).name,
        "Status": _ICON.get(it["status"], it["status"]),
        "Link": it["url"] or None,
        "Note": (it["error"] or "")[:90],
    } for it in shown])
    ev = st.dataframe(df, hide_index=True, width="stretch", height=min(420, 38 + 35 * len(df)),
                      column_config={"Link": st.column_config.LinkColumn(display_text="open ↗")},
                      on_select="rerun", selection_mode="multi-row", key="yt_q_table")
    picked = [shown[i]["id"] for i in (ev.selection.rows if ev else [])]

    b1, b2, b3, b4 = st.columns(4)
    if batch["paused"] or batch["resume_at"]:
        if b1.button("▶️ Resume now", key="yt_q_resume", width="stretch"):
            ytb.set_paused(False)
            ytb.ensure_worker()
            st.rerun()
    elif b1.button("⏸ Pause", key="yt_q_pause", width="stretch"):
        ytb.set_paused(True)
        st.rerun()
    if c["failed"] and b2.button(f"🔁 Retry {c['failed']} failed", key="yt_q_retry",
                                 width="stretch"):
        ytb.retry_failed()
        ytb.ensure_worker()
        st.rerun()
    if c["done"] and b3.button(f"🧹 Clear {c['done']} done", key="yt_q_clear", width="stretch"):
        ytb.clear_finished()
        st.rerun()
    if picked and b4.button(f"✕ Remove {len(picked)} selected", key="yt_q_rm", width="stretch"):
        ytb.remove_items(picked)
        st.rerun()
    st.divider()


# ── Page ──────────────────────────────────────────────────────────────────────

def render_youtube_ui():
    _init()
    if st.session_state.yt_toast:
        st.toast(st.session_state.yt_toast)
        st.session_state.yt_toast = ""
    if st.session_state.yt_error:
        e1, e2 = st.columns([10, 1])
        e1.error(st.session_state.yt_error)
        if e2.button("✕", key="yt_err_x"):
            st.session_state.yt_error = ""
            st.rerun()

    signed_in = yta.has_token() and yt_valid()
    if not signed_in:
        st.warning("YouTube isn't connected (or the login expired) — open **⚙️ Settings** "
                   "to connect. You can still prepare projects.", icon="🔐")

    _queue_panel()

    if st.session_state.yt_automating:      # non-blocking: page stays usable
        live_progress("yt_ai", "🤖 AI metadata running in the background", _on_ai_done)

    project = st.session_state.yt_project
    if not project or not Path(project).exists():
        picked = render_project_picker("youtube")
        if picked:
            _open(picked)
            st.rerun()
        return

    project = Path(project)
    videos = find_videos(project)
    thumbs = find_thumbnails(project)
    meta = st.session_state.yt_meta
    h1, h2 = st.columns([5, 1])
    h1.subheader(f"▶️ {project.name} — {len(videos)} video(s)")
    if h2.button("← Projects", key="yt_back", width="stretch"):
        _close()
        st.rerun()
    if not videos:
        st.error("No video files in this folder.")
        return

    # ── AI + quick fill ───────────────────────────────────────────────────────
    n_ai = sum(1 for v in videos if meta.get(v.name, {}).get("ai"))
    with st.container(border=True):
        notes = st.text_input("🗒️ AI context", key="yt_notes",
                              placeholder="e.g. university course on integrated photonics")
        redo = st.checkbox("Regenerate videos that already have AI text", key="yt_redo")
        todo = len(videos) if redo else len(videos) - n_ai
        st.caption(f"{n_ai}/{len(videos)} videos have AI text. Automate will make ≈{todo} "
                   f"Gemini call(s), roughly {max(1, todo * 10 // 60)} min. The free tier may "
                   "throttle on large sets; those videos keep filename titles.")
        a1, a2 = st.columns(2)
        if a1.button(f"🤖 Automate {todo} video(s)", type="primary", width="stretch",
                     disabled=st.session_state.yt_automating or todo == 0):
            _save()
            st.session_state.yt_automating = True
            vids = list(videos)
            start_publish_thread(fn=lambda: _run_automation(project, vids, notes, redo),
                                 platform="yt_ai")
            st.rerun()
        if a2.button("📝 Use filenames as titles (empty only)", width="stretch"):
            for v in videos:
                m = meta.setdefault(v.name, {})
                if not (m.get("title") or "").strip():
                    m["title"] = v.stem.replace("_", " ").strip()[:TITLE_MAX]
            _drop_widgets()
            _save()
            st.rerun()

    # ── Publish settings ──────────────────────────────────────────────────────
    st.markdown("#### ⚙️ Publish settings (all videos)")
    s1, s2, s3 = st.columns(3)
    privacy = s1.radio("Visibility", ["unlisted", "public", "private"], key="yt_privacy",
                       help="API uploads set to public get locked private until your Google "
                            "Cloud project passes YouTube's audit — unlisted is the safe default.")
    cats = list(CATEGORIES)
    category = s2.selectbox("Category", cats, index=cats.index("People & Blogs"), key="yt_cat")
    kids = s3.checkbox("Made for kids (COPPA)", key="yt_kids")
    notify = s3.checkbox("Notify subscribers", value=True, key="yt_notify")

    publish_at = None
    if st.checkbox("📅 Schedule publish time", key="yt_sched"):
        d1, d2 = st.columns(2)
        day = d1.date_input("Date", _dt.date.today() + _dt.timedelta(days=1), key="yt_sd")
        tm = d2.time_input("Time (your local time)", _dt.time(18, 0), key="yt_st")
        local = _dt.datetime.combine(day, tm).astimezone()
        publish_at = local.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    p1, p2, p3 = st.columns([2, 2, 1])
    playlist_id = ""
    pls = st.session_state.yt_playlists
    if p3.button("🔄 Load playlists", disabled=not signed_in, width="stretch"):
        try:
            st.session_state.yt_playlists = list_playlists()
        except Exception as e:
            st.session_state.yt_error = f"Could not load playlists: {e}"
        st.rerun()
    if pls:
        opts = ["(none)"] + [f"{p['title']} ({p['count']})" for p in pls]
        sel = p1.selectbox("Existing playlist", opts, key="yt_pl")
        if sel != "(none)":
            playlist_id = pls[opts.index(sel) - 1]["id"]
    else:
        p1.caption("Load playlists to pick an existing one.")
    new_pl = p2.text_input("…or new playlist name (reused if it exists)", key="yt_pl_new",
                           value=project.name.replace("_", " ") if not pls else "")

    # ── Per-video details (paginated) ─────────────────────────────────────────
    st.markdown("#### 📝 Video details")
    pages = max(1, math.ceil(len(videos) / PAGE_SIZE))
    page = 1
    if pages > 1:
        page = st.number_input(f"Page (of {pages}, {PAGE_SIZE} per page)", 1, pages, 1,
                               key="yt_page")
    thumb_opts = [AUTO_THUMB] + [t.name for t in thumbs]
    start = (page - 1) * PAGE_SIZE
    for gi in range(start, min(start + PAGE_SIZE, len(videos))):
        v = videos[gi]
        m = meta.setdefault(v.name, {})
        badge = " · 🤖" if m.get("ai") else ""
        with st.expander(f"{gi + 1}. {v.name} ({v.stat().st_size / 1_048_576:.0f} MB){badge}",
                         expanded=len(videos) == 1):
            if len(videos) <= 6:
                st.video(str(v))
            m["title"] = st.text_input("Title", key=_seed(f"ytw_t_{gi}", m.get("title", "")),
                                       max_chars=TITLE_MAX, placeholder=v.stem)
            m["description"] = st.text_area(
                "Description", height=140, key=_seed(f"ytw_d_{gi}", m.get("description", "")))
            m["tags_str"] = st.text_input(
                f"Tags (comma-separated, ≤{TAGS_MAX_CHARS} chars)",
                key=_seed(f"ytw_g_{gi}", m.get("tags_str", "")))
            t1, t2 = st.columns([2, 1])
            cur = m.get("thumb", AUTO_THUMB)
            m["thumb"] = t1.selectbox("Thumbnail", thumb_opts, key=f"ytw_th_{gi}",
                                      index=thumb_opts.index(cur) if cur in thumb_opts else 0)
            if t2.button("🖼 Grab frame", key=f"ytw_x_{gi}", width="stretch"):
                p = extract_thumbnail_frame(v)
                st.session_state.yt_toast = (f"Frame saved as {p.name} — pick it above"
                                             if p else "ffmpeg not available")
                st.rerun()
    _save()

    # ── Review + queue ────────────────────────────────────────────────────────
    st.divider()
    items, untitled = [], 0
    for v in videos:
        m = meta.get(v.name, {})
        title = (m.get("title") or "").strip()
        untitled += not title
        th = m.get("thumb", AUTO_THUMB)
        items.append({
            "path": v, "title": title or v.stem.replace("_", " "),
            "description": m.get("description", ""),
            "tags": [t.strip() for t in (m.get("tags_str") or "").split(",") if t.strip()],
            "category_id": CATEGORIES[category], "privacy": privacy, "made_for_kids": kids,
            "publish_at": publish_at, "playlist_id": playlist_id,
            "new_playlist_title": "" if playlist_id else new_pl.strip(),
            "thumbnail": str(project / th) if th != AUTO_THUMB and (project / th).exists() else None,
            "notify_subscribers": notify,
        })
    n_th = sum(1 for it in items if it["thumbnail"])
    n_pl = len(items) if (playlist_id or new_pl.strip()) else 0
    cost = ytq.estimate_cost(len(items), n_th, n_pl, 1 if new_pl.strip() and not playlist_id else 0)
    u = ytq.usage()
    st.caption(f"Plan: {len(items)} video(s) · {privacy}{' · scheduled' if publish_at else ''}"
               f"{' · playlist' if n_pl else ''} · ≈{cost:,} quota units "
               f"({u['units_remaining']:,} left today — the queue spreads the rest over "
               "following days automatically)"
               + (f" · {untitled} untitled → filename used" if untitled else ""))
    if st.button(f"🚀 QUEUE {len(items)} VIDEO(S) FOR UPLOAD", type="primary", width="stretch",
                 disabled=not signed_in):
        for it in items:
            save_entry("youtube", project.name, f"{it['title']}\n\n{it['description']}",
                       [str(it["path"])])
        added = ytb.enqueue(items)
        ytb.ensure_worker()
        st.session_state.yt_toast = (f"✅ Queued {added} video(s)"
                                     + (f" ({len(items) - added} already queued, skipped)"
                                        if added < len(items) else ""))
        _close()
        st.rerun()
