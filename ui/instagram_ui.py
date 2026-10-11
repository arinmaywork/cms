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
import src.ig_frame as igf
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

def _upload_framed(project: Path, images: list[Path], cfg: dict, d: dict) -> None:
    files = igf.frame_files(images, cfg, project) if cfg["on"] else images
    d["urls"] = {img.name: u for img, u in zip(images, upload_multiple(files, project.name))}
    d["frame_sig"] = igf.signature(cfg)


def _run_upload_only(project: Path, images: list[Path], cfg: dict) -> None:
    ns = "ig_automation"
    progress.start(ns, ["🖼 Frame + ☁️ upload"])
    try:
        progress.update(ns, 0, "active", f"{len(images)} image(s)…")
        data = load_draft(project)
        d = {**EMPTY, **data.get("instagram", {})}
        _upload_framed(project, images, cfg, d)
        data["instagram"] = d
        save_draft(project, data)
        progress.update(ns, 0, "done", "Done")
        progress.finish(ns, {"success": True})
    except Exception:
        progress.fail(ns, traceback.format_exc())


def _run_automation(project: Path, images: list[Path], notes: str, cfg: dict) -> None:
    ns = "ig_automation"
    progress.start(ns, ["☁️ Upload images to Cloudinary", "✍️ Hook", "🏷️ Hashtags"]
                   + [f"🖼️ {img.name}" for img in images])
    try:
        d = {**EMPTY, **load_draft(project).get("instagram", {})}
        progress.update(ns, 0, "active", "Uploading…")
        _upload_framed(project, images, cfg, d)
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


@st.cache_data(max_entries=60, show_spinner=False)
def _preview(path: str, mtime: float, ratio: float, color: str, margin: float) -> bytes:
    return igf.preview_jpeg(Path(path), ratio, color, margin)


def _frame_ui(project: Path, images: list[Path], d: dict) -> dict:
    """Frame settings (remembered as defaults) + live preview + re-upload prompt."""
    base = {**igf.load_defaults(), **d.get("frame", {})}
    with st.expander("🖼 Frame for Instagram — no cropping", expanded=True):
        c1, c2, c3, c4 = st.columns([1, 2, 1, 2])
        on = c1.toggle("Frame", value=base["on"], key="igf_on")
        names = list(igf.RATIOS)
        ratio = c2.selectbox("Aspect ratio", names, index=names.index(base["ratio"]),
                             key="igf_ratio", disabled=not on)
        color = c3.color_picker("Fill", base["color"], key="igf_color", disabled=not on)
        margin = c4.slider("Extra border %", 0, 15, int(base["margin"]), key="igf_margin",
                           disabled=not on,
                           help="0 = pad only where needed. >0 = uniform frame on all sides.")
        cfg = {"on": on, "ratio": ratio, "color": color, "margin": margin}
        if on:
            r = igf.target_ratio(cfg, images[0])
            st.caption(f"All {len(images)} slide(s) → {r:.2f}:1 at 1440 px wide"
                       + (" (Instagram shows every carousel slide at the first slide's ratio)"
                          if len(images) > 1 else ""))
            cols = st.columns(min(4, len(images)))
            for i, img in enumerate(images[:8]):
                cols[i % len(cols)].image(_preview(str(img), img.stat().st_mtime, r, color,
                                                   margin), width="stretch")
        elif len(images) > 1:
            st.caption("⚠️ Unframed carousel: Instagram crops slides to the first slide's ratio.")
    d["frame"] = cfg
    igf.save_defaults(cfg)
    if any(d["urls"].values()) and d.get("frame_sig") != igf.signature(cfg):
        st.warning("Frame settings changed since the images were uploaded.")
        if st.button("🖼 Re-frame & upload", key="igf_reupload", type="primary"):
            st.session_state.ig_automating = True
            start_publish_thread(fn=lambda: _run_upload_only(project, list(images), cfg),
                                 platform="ig_automation")
            st.rerun()
    return cfg


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

    cfg = _frame_ui(project, images, d)
    notes = st.text_input("🗒️ AI context", key="ig_notes", placeholder="e.g. minimal architecture")
    if st.button("🤖 Automate (upload + AI copy)", type="primary", width="stretch"):
        st.session_state.ig_automating = True
        start_publish_thread(fn=lambda: _run_automation(project, list(images), notes, cfg),
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
