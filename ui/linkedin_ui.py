"""
ui/linkedin_ui.py — LinkedIn page: text / image / multi-image posts to your
profile. Source: a LinkedIn project folder, an Instagram project (cross-post,
incl. archived ones — files are only read), or a text-only post.
"""

import traceback
from pathlib import Path

import streamlit as st

import src.linkedin as li
import src.progress as progress
from src.ai_generator import generate_linkedin_text
from src.history_manager import save_entry
from src.projects import (ARCHIVE_DIR, INPUT_DIRS, archive_project, list_projects,
                          load_draft, media_files, save_draft)
from ui.folder_picker import render_project_picker
from ui.progress_widget import live_progress, start_publish_thread

TEXT_ONLY = INPUT_DIRS["linkedin"].parent / ".queue" / "li_textonly"


def _init():
    for k, v in {"li_project": None, "li_toast": "", "li_error": "",
                 "li_publishing": False, "li_ai": False}.items():
        st.session_state.setdefault(k, v)


def _open(p: Path):
    st.session_state.li_project = p
    for k in [k for k in st.session_state if str(k).startswith("liw_")]:
        del st.session_state[k]


def _draft(p: Path) -> dict:
    return load_draft(p).get("linkedin", {})


def _save(p: Path, d: dict):
    p.mkdir(parents=True, exist_ok=True)
    data = load_draft(p)
    data["linkedin"] = d
    save_draft(p, data)


def _ai_job(p: Path, image: Path | None, notes: str):
    progress.start("li_ai", ["✍️ Writing the post"])
    try:
        d = _draft(p)
        d["text"] = generate_linkedin_text(p.name if p != TEXT_ONLY else "", image, notes)
        _save(p, d)
        progress.finish("li_ai", {"success": True})
    except Exception:
        progress.fail("li_ai", traceback.format_exc())


def _publish_job(p: Path, text: str, images: list, alts: list):
    r = li.publish(text, images, alts)
    if r.get("success"):
        if p.parent == INPUT_DIRS["linkedin"]:
            archive_project("linkedin", p)
        elif p == TEXT_ONLY:
            _save(p, {})


def _on_ai(result: dict):
    st.session_state.li_ai = False
    _open(st.session_state.li_project)
    if not result.get("success"):
        st.session_state.li_error = f"AI failed: {result.get('error', '')[-300:]}"


def _on_pub(result: dict):
    st.session_state.li_publishing = False
    if result.get("success"):
        st.session_state.li_toast = f"✅ Posted to LinkedIn {result.get('url', '')}"
        st.session_state.li_project = None
    else:
        st.session_state.li_error = f"LinkedIn post failed: {result.get('error')}"


def _instagram_sources() -> list[Path]:
    arch = ARCHIVE_DIR / "instagram"
    olds = sorted((d for d in arch.iterdir() if d.is_dir()), key=lambda d: -d.stat().st_mtime) \
        if arch.exists() else []
    return [x["path"] for x in list_projects("instagram")] + olds[:30]


def render_linkedin_ui():
    _init()
    if st.session_state.li_toast:
        st.toast(st.session_state.li_toast)
        st.session_state.li_toast = ""
    if st.session_state.li_error:
        e1, e2 = st.columns([10, 1])
        e1.error(st.session_state.li_error)
        if e2.button("✕", key="li_err_x"):
            st.session_state.li_error = ""
            st.rerun()
    s = li.status()
    if not s.get("connected"):
        st.warning("LinkedIn isn't connected — open **⚙️ Settings → 💼 LinkedIn**.", icon="🔐")
    if st.session_state.li_ai:
        live_progress("li_ai", "🤖 Writing your LinkedIn post…", _on_ai)
        st.stop()
    if st.session_state.li_publishing:
        live_progress("li", "🚀 Posting to LinkedIn…", _on_pub)
        st.stop()

    p = st.session_state.li_project
    if not p or not Path(p).exists() and Path(p) != TEXT_ONLY:
        src = st.segmented_control("Source", ["LinkedIn projects", "Cross-post an Instagram project",
                                              "Text-only post"], default="LinkedIn projects",
                                   key="li_src") or "LinkedIn projects"
        if src == "Text-only post":
            _open(TEXT_ONLY)
            st.rerun()
        if src.startswith("Cross"):
            opts = _instagram_sources()
            if not opts:
                st.info("No Instagram projects (active or archived) yet.")
                return
            sel = st.selectbox("Instagram project", opts, format_func=lambda x: (
                "📦 " if x.parent.name == "instagram" and x.parent.parent == ARCHIVE_DIR else "") + x.name)
            if st.button("Open", type="primary"):
                _open(sel)
                st.rerun()
            return
        picked = render_project_picker("linkedin")
        if picked:
            _open(picked)
            st.rerun()
        return

    p = Path(p)
    d = _draft(p)
    imgs = [] if p == TEXT_ONLY else media_files(p, "instagram")
    h1, h2 = st.columns([5, 1])
    h1.subheader(f"💼 {'Text-only post' if p == TEXT_ONLY else p.name}")
    if h2.button("← Back", key="li_back", width="stretch"):
        st.session_state.li_project = None
        st.rerun()

    chosen = []
    if imgs:
        chosen = st.multiselect(f"Images to include (max {li.MAX_IMAGES})", [i.name for i in imgs],
                                default=[i.name for i in imgs][:li.MAX_IMAGES], key="liw_imgs")
        cols = st.columns(6)
        for i, name in enumerate(chosen[:12]):
            cols[i % 6].image(str(p / name), width="stretch")

    ig = load_draft(p).get("instagram", {}) if imgs else {}
    notes = st.text_input("🗒️ AI context", key="liw_notes",
                          placeholder="e.g. what you learned shooting this, who it's for")
    if st.button("🤖 Write with AI (replaces the text below)"):
        _save(p, {**d, "text": st.session_state.get("liw_text", d.get("text", ""))})
        st.session_state.li_ai = True
        first = p / chosen[0] if chosen else None
        start_publish_thread(fn=lambda: _ai_job(p, first, notes), platform="li_ai")
        st.rerun()
    st.session_state.setdefault("liw_text", d.get("text", ""))
    text = st.text_area("Post", key="liw_text", height=260,
                        placeholder="First line is the hook (shown before '…see more').")
    _save(p, {**d, "text": text})
    st.caption(f"{len(text):,}/{li.MAX_CHARS:,} characters · #hashtags become real LinkedIn hashtags")

    problems = (["Post text is empty"] if not text.strip() else []) \
        + ([f"Too long ({len(text):,}/{li.MAX_CHARS:,})"] if len(text) > li.MAX_CHARS else []) \
        + ([f"Max {li.MAX_IMAGES} images"] if len(chosen) > li.MAX_IMAGES else []) \
        + (["LinkedIn not connected"] if not s.get("connected") else [])
    for x in problems:
        st.caption(f"⚠️ {x}")
    if st.button("🚀 POST TO LINKEDIN", type="primary", width="stretch", disabled=bool(problems)):
        paths = [p / n for n in chosen]
        alts = [ig.get("alts", {}).get(n, "") for n in chosen]
        save_entry("linkedin", p.name, text, [str(x) for x in paths])
        st.session_state.li_publishing = True
        start_publish_thread(fn=lambda: _publish_job(p, text, paths, alts), platform="li")
        st.rerun()
