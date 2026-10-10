"""
ui/instagram_ui.py — Instagram page.

Drafts (Cloudinary URLs, hook, per-slide text, alt text, hashtags) persist in
<project>/.cms.json — written by the automation thread itself and on every
edit — so a refresh/restart never loses work. Published projects are moved
to archive/instagram/ so they can't be published twice.
"""

import traceback
from pathlib import Path

import streamlit as st
from PIL import Image

import src.progress as progress
from src.ai_generator import (generate_alt_text, generate_footer_text,
                              generate_image_text, generate_title_text)
from src.history_manager import save_entry
from src.instagram_publisher import publish_to_instagram
from src.projects import archive_project, load_draft, media_files, save_draft
from src.uploader import upload_multiple
from ui.folder_picker import render_project_picker
from ui.progress_widget import live_progress, start_publish_thread

CHAR_LIMIT = 2_200
EMPTY = {"title": "", "footer": "", "urls": {}, "texts": {}, "alts": {}}


def _init():
    for k, v in {"ig_project": None, "ig_d": dict(EMPTY), "ig_toast": "",
                 "ig_error": "", "ig_automating": False, "ig_publishing": False}.items():
        st.session_state.setdefault(k, v)


def _drop_widgets():
    for k in [k for k in st.session_state if str(k).startswith("igw_")]:
        del st.session_state[k]


def _open(path: Path):
    st.session_state.ig_project = path
    st.session_state.ig_d = {**EMPTY, **load_draft(path).get("instagram", {})}
    _drop_widgets()


def _close():
    st.session_state.ig_project = None
    st.session_state.ig_d = dict(EMPTY)
    _drop_widgets()


def _save():
    p = st.session_state.ig_project
    if p and Path(p).exists():
        data = load_draft(p)
        data["instagram"] = st.session_state.ig_d
        save_draft(p, data)


def _seed(key: str, value: str) -> str:
    st.session_state.setdefault(key, value)
    return key


# ── Background automation (no st.* in here) ──────────────────────────────────

def _run_automation(project: Path, images: list[Path], notes: str) -> None:
    ns = "ig_automation"
    progress.start(ns, ["☁️ Upload images to Cloudinary", "✍️ Hook", "🏷️ Hashtags"]
                   + [f"🖼️ {img.name}" for img in images])
    try:
        d = {**EMPTY, **load_draft(project).get("instagram", {})}
        progress.update(ns, 0, "active", "Uploading…")
        d["urls"] = {img.name: u for img, u in zip(images, upload_multiple(images))}
        progress.update(ns, 0, "done", f"{len(images)} uploaded")
        progress.update(ns, 1, "active", "Gemini…")
        d["title"] = generate_title_text("instagram", project.name, notes)
        progress.update(ns, 1, "done", "Done")
        progress.update(ns, 2, "active", "Gemini…")
        d["footer"] = generate_footer_text("instagram", project.name, notes)
        progress.update(ns, 2, "done", "Done")
        for i, img in enumerate(images):
            progress.update(ns, 3 + i, "active", "Analysing…")
            d["texts"][img.name] = generate_image_text("instagram", project.name, img,
                                                      i, len(images), notes)
            d["alts"][img.name] = generate_alt_text(img)
            progress.update(ns, 3 + i, "done", "Done")
        data = load_draft(project)
        data["instagram"] = d
        save_draft(project, data)            # persisted even if nobody is watching
        progress.finish(ns, {"success": True})
    except Exception:
        progress.fail(ns, traceback.format_exc())


def _publish_job(project: Path, urls: list[str], caption: str, alts: list[str]):
    r = publish_to_instagram(urls, caption, alt_texts=alts)
    if r.get("success"):
        archive_project("instagram", project)


def _on_automation_done(result: dict):
    st.session_state.ig_automating = False
    if result.get("success"):
        _open(st.session_state.ig_project)
        st.session_state.ig_toast = "✅ AI copy ready — review and publish."
    else:
        st.session_state.ig_error = f"Automation failed: {result.get('error', '')[-500:]}"


def _on_publish_done(result: dict):
    st.session_state.ig_publishing = False
    if result.get("success"):
        st.session_state.ig_toast = f"✅ Published to Instagram (post {result.get('post_id')})"
        _close()
    else:
        st.session_state.ig_error = f"Publish failed: {result.get('error')}"


# ── Page ──────────────────────────────────────────────────────────────────────

def render_instagram_ui():
    _init()
    if st.session_state.ig_toast:
        st.toast(st.session_state.ig_toast)
        st.session_state.ig_toast = ""
    if st.session_state.ig_error:
        e1, e2 = st.columns([10, 1])
        e1.error(st.session_state.ig_error)
        if e2.button("✕", key="ig_err_x"):
            st.session_state.ig_error = ""
            st.rerun()

    if st.session_state.ig_automating:
        live_progress("ig_automation", "🤖 Uploading & writing copy…", _on_automation_done)
        st.stop()
    if st.session_state.ig_publishing:
        live_progress("ig", "🚀 Publishing to Instagram…", _on_publish_done)
        st.stop()

    project = st.session_state.ig_project
    if not project or not Path(project).exists():
        picked = render_project_picker("instagram")
        if picked:
            _open(picked)
            st.rerun()
        return

    project = Path(project)
    images = media_files(project, "instagram")
    d = st.session_state.ig_d
    h1, h2 = st.columns([5, 1])
    h1.subheader(f"📸 {project.name} — {len(images)} image(s)")
    if h2.button("← Projects", key="ig_back", width="stretch"):
        _close()
        st.rerun()
    if not images:
        st.error("No images in this folder.")
        return
    if len(images) > 10:
        st.warning(f"Instagram carousels allow 10 images — this folder has {len(images)}.")

    notes = st.text_input("🗒️ AI context", key="ig_notes", placeholder="e.g. minimal architecture")
    if st.button("🤖 Automate (upload + AI copy)", type="primary", width="stretch"):
        st.session_state.ig_automating = True
        start_publish_thread(fn=lambda: _run_automation(project, list(images), notes),
                             platform="ig_automation")
        st.rerun()

    d["title"] = st.text_area("Hook", key=_seed("igw_title", d["title"]))
    for i, img in enumerate(images):
        c1, c2 = st.columns([1, 3])
        with c1:
            st.image(Image.open(img), width="stretch")
        with c2:
            st.markdown(f"**Slide {i + 1}** · `{img.name}`")
            d["texts"][img.name] = st.text_area(
                "Caption", key=_seed(f"igw_cap_{i}", d["texts"].get(img.name, "")), height=90)
            d["alts"][img.name] = st.text_input(
                "Alt text", key=_seed(f"igw_alt_{i}", d["alts"].get(img.name, "")))
    d["footer"] = st.text_area("Hashtags", key=_seed("igw_footer", d["footer"]))

    with st.expander("Advanced: image URLs (filled automatically by Automate)"):
        for i, img in enumerate(images):
            d["urls"][img.name] = st.text_input(
                img.name, key=_seed(f"igw_url_{i}", d["urls"].get(img.name, "")))
    _save()

    caption = "\n\n".join(x for x in [d["title"].strip()]
                          + [d["texts"].get(img.name, "").strip() for img in images]
                          + [d["footer"].strip()] if x)
    urls = [d["urls"].get(img.name, "").strip() for img in images]
    missing = sum(1 for u in urls if not u.startswith("http"))

    st.divider()
    with st.expander(f"🔍 Caption preview ({len(caption):,}/{CHAR_LIMIT:,})", expanded=True):
        st.text(caption or "(empty)")
    problems = ([f"{missing} image(s) not uploaded yet — run Automate"] if missing else []) \
        + (["Caption is empty"] if not caption else []) \
        + ([f"Caption too long ({len(caption):,}/{CHAR_LIMIT:,})"] if len(caption) > CHAR_LIMIT else []) \
        + (["Max 10 images"] if len(images) > 10 else [])
    for p in problems:
        st.caption(f"⚠️ {p}")
    if st.button("🚀 APPROVE & PUBLISH", type="primary", width="stretch",
                 disabled=bool(problems)):
        save_entry("instagram", project.name, caption, urls)
        alts = [d["alts"].get(img.name, "") for img in images]
        st.session_state.ig_publishing = True
        start_publish_thread(fn=lambda: _publish_job(project, urls, caption, alts),
                             platform="ig")
        st.rerun()
