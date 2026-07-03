"""
ui/instagram_ui.py
"""

import pathlib
import traceback
from typing import Any

import streamlit as st
from PIL import Image

from src.ai_generator import (
    generate_title_text,
    generate_image_text,
    generate_footer_text,
    generate_alt_text,
)
from src.history_manager import get_last_n, save_entry
from src.instagram_publisher import publish_to_instagram
from src.file_queue import pop_one as fq_pop_one
from src.uploader import upload_multiple
from ui.progress_widget import start_publish_thread, render_progress
from ui.folder_picker import render_folder_picker
import src.progress as progress

VALID_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
CHAR_LIMIT = 2_200


def _images(folder: pathlib.Path) -> list[pathlib.Path]:
    if not folder:
        return []
    try:
        return sorted(f for f in folder.iterdir() if f.suffix.lower() in VALID_EXTS)
    except OSError:
        return []


def _init():
    if "ig_urls" not in st.session_state: st.session_state.ig_urls = {}
    if "ig_image_texts" not in st.session_state: st.session_state.ig_image_texts = {}
    if "ig_alt_texts" not in st.session_state: st.session_state.ig_alt_texts = {}
    if "ig_queue" not in st.session_state: st.session_state.ig_queue = []

    defaults = {
        "ig_project":     None,
        "ig_title":       "",
        "ig_footer":      "",
        "ig_status":      "",
        "ig_status_type": "info",
        "ig_publishing":  False,
        "ig_automating":  False,   # True while automation background thread is running
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def _set_status(msg, kind="info"):
    st.session_state.ig_status = msg
    st.session_state.ig_status_type = kind


def _clear():
    st.session_state.ig_project = None
    st.session_state.ig_title = ""
    st.session_state.ig_footer = ""
    st.session_state.ig_urls = {}
    st.session_state.ig_image_texts = {}
    st.session_state.ig_alt_texts = {}
    st.session_state.ig_status = ""
    st.session_state.ig_publishing = False
    st.session_state.ig_automating = False

    for key in list(st.session_state.keys()):
        if any(key.startswith(p) for p in ["ig_url_w_", "ig_cap_w_", "ig_alt_w_", "ig_title_ed_", "ig_footer_ed_"]):
            del st.session_state[key]


def _assemble_caption(title, image_texts, footer, images):
    parts = []
    if title.strip(): parts.append(title.strip())
    for img in images:
        txt = image_texts.get(img.name, "").strip()
        if txt: parts.append(txt)
    if footer.strip(): parts.append(footer.strip())
    return "\n\n".join(parts)


# ── Background automation worker ──────────────────────────────────────────────
# Runs in a daemon thread spawned by start_publish_thread().
# NEVER call st.* from here — this is not the Streamlit script runner thread.
# Results are written to the progress file; the UI thread reads them on the
# next autorefresh tick.

def _run_automation(images: list[pathlib.Path], project_name: str, notes: str) -> None:
    """Upload images + generate all AI copy.  Called from a background thread."""
    step_labels = (
        ["☁️ Upload images to Cloudinary", "✍️ Generate hook / title", "🏷️ Generate hashtags"]
        + [f"🖼️ AI copy: {img.name}" for img in images]
    )

    progress.start("ig_automation", step_labels)
    step = 0
    urls: dict[str, str] = {}
    image_texts: dict[str, str] = {}
    alt_texts: dict[str, str] = {}
    title = ""
    footer = ""

    try:
        # ── Step 0: upload ────────────────────────────────────────────────────
        progress.update("ig_automation", step, "active", "Uploading to Cloudinary…")
        uploaded = upload_multiple(images)
        for i, u in enumerate(uploaded):
            urls[images[i].name] = u
        progress.update("ig_automation", step, "done", f"{len(uploaded)} image(s) uploaded")
        step += 1

        # ── Step 1: title / hook ──────────────────────────────────────────────
        progress.update("ig_automation", step, "active", "Calling Gemini…")
        title = generate_title_text("instagram", project_name, notes)
        progress.update("ig_automation", step, "done", "Done")
        step += 1

        # ── Step 2: footer / hashtags ─────────────────────────────────────────
        progress.update("ig_automation", step, "active", "Calling Gemini…")
        footer = generate_footer_text("instagram", project_name, notes)
        progress.update("ig_automation", step, "done", "Done")
        step += 1

        # ── Steps 3+: per-image captions & alt text ───────────────────────────
        for i, img in enumerate(images):
            progress.update("ig_automation", step, "active", f"Analysing {img.name}…")
            image_texts[img.name] = generate_image_text(
                "instagram", project_name, img, i, len(images), notes
            )
            alt_texts[img.name] = generate_alt_text(img)
            progress.update("ig_automation", step, "done", "Done")
            step += 1

        progress.finish("ig_automation", {
            "success":     True,
            "urls":        urls,
            "title":       title,
            "footer":      footer,
            "image_texts": image_texts,
            "alt_texts":   alt_texts,
        })

    except Exception:
        progress.fail("ig_automation", traceback.format_exc())


# ── Main UI ────────────────────────────────────────────────────────────────────

def render_instagram_ui():
    _init()
    st.header("📸 Instagram Publisher")

    # ── Drain queue ───────────────────────────────────────────────────────────
    while True:
        new_path = fq_pop_one("instagram")
        if not new_path: break
        if str(new_path) not in [str(p) for p in st.session_state.ig_queue]:
            st.session_state.ig_queue.append(new_path)

    if not st.session_state.ig_project and st.session_state.ig_queue:
        st.session_state.ig_project = st.session_state.ig_queue.pop(0)
        _set_status(f"✅ Loaded from queue: **{st.session_state.ig_project.name}**", "info")
        st.rerun()

    if st.session_state.ig_queue:
        with st.sidebar:
            st.divider()
            st.info(f"📁 **{len(st.session_state.ig_queue)}** more projects pending.")
            for i, p in enumerate(st.session_state.ig_queue[:5]):
                st.caption(f"{i+1}. {p.name}")

    # ── Automation in progress (background thread) ────────────────────────────
    # All blocking Cloudinary + AI work runs in a daemon thread.
    # The script runner returns in milliseconds on every autorefresh tick
    # (it just reads the progress file and renders HTML) — this eliminates
    # the re-entrant BufferedWriter crash caused by Streamlit internally
    # calling threading.interrupt_main() to interrupt a blocked script runner.
    if st.session_state.ig_automating:
        st.subheader("🤖 Running Automation…")
        prog_placeholder = st.empty()
        result = render_progress("ig_automation", prog_placeholder)

        if result is not None:
            # Thread finished — populate session state from results
            if result.get("success"):
                project = st.session_state.ig_project
                images  = _images(project) if project else []

                urls        = result.get("urls", {})
                image_texts = result.get("image_texts", {})
                alt_texts   = result.get("alt_texts", {})
                title       = result.get("title", "")
                footer      = result.get("footer", "")

                st.session_state.ig_urls.update(urls)
                st.session_state.ig_image_texts.update(image_texts)
                st.session_state.ig_alt_texts.update(alt_texts)
                st.session_state.ig_title  = title
                st.session_state.ig_footer = footer

                # Seed the individual widget keys so text areas/inputs show values
                st.session_state["ig_title_ed_2"]  = title
                st.session_state["ig_footer_ed_2"] = footer
                for j, img in enumerate(images):
                    st.session_state[f"ig_url_w_2_{j}"] = urls.get(img.name, "")
                    st.session_state[f"ig_cap_w_2_{j}"] = image_texts.get(img.name, "")
                    st.session_state[f"ig_alt_w_2_{j}"] = alt_texts.get(img.name, "")

                st.session_state.ig_automating = False
                _set_status("✅ Automation complete! Review and publish when ready.", "success")

            else:
                err = result.get("error", "Unknown error")
                st.session_state.ig_automating = False
                _set_status(f"❌ Automation failed: {err}", "error")

            st.rerun()

        st.stop()

    # ── Publishing in progress (background thread) ────────────────────────────
    if st.session_state.ig_publishing:
        st.subheader("🚀 Publishing to Instagram…")
        prog_placeholder = st.empty()
        result = render_progress("ig", prog_placeholder)
        if result is not None:
            if result.get("success"):
                _set_status(f"✅ Published! ID: `{result.get('post_id')}`", "success")
                _clear()
            else:
                _set_status(f"❌ Failed: {result.get('error')}", "error")
            st.rerun()
        st.stop()

    # ── Status banner ─────────────────────────────────────────────────────────
    if st.session_state.ig_status:
        getattr(st, st.session_state.ig_status_type)(st.session_state.ig_status)

    # ── No project loaded ─────────────────────────────────────────────────────
    if not st.session_state.ig_project:
        st.markdown("### ⏳ Waiting for a project…")
        render_folder_picker("instagram")
        return

    project = st.session_state.ig_project
    images  = _images(project)

    if not images:
        st.error("No images found.")
        if st.button("Clear"): _clear(); st.rerun()
        return

    st.subheader(f"`{project.name}`")
    notes = st.text_input(
        "🗒️ AI context:", key="ig_notes_field_2",
        placeholder="e.g. minimal architecture"
    )

    # ── Automate Everything button ────────────────────────────────────────────
    # Clicking starts a background thread and returns IMMEDIATELY.
    # No blocking work happens in the script runner — eliminates the crash.
    if st.button("🤖 Automate Everything (Upload + AI Copy)", type="primary", width="stretch"):
        notes_val = st.session_state.get("ig_notes_field_2", "")
        imgs_snapshot = list(images)  # capture list now, before any rerun

        def _worker():
            _run_automation(imgs_snapshot, project.name, notes_val)

        start_publish_thread(fn=_worker, platform="ig_automation")
        st.session_state.ig_automating = True
        st.rerun()

    st.divider()

    # ── Edit content details ──────────────────────────────────────────────────
    with st.expander("📝 Edit Content Details", expanded=True):
        st.session_state.ig_title = st.text_area(
            "Hook", value=st.session_state.ig_title, key="ig_title_ed_2"
        )
        for i, img in enumerate(images):
            st.markdown(f"**Slide {i+1}** — `{img.name}`")
            col_p, col_e = st.columns([1, 2])
            with col_p:
                st.image(Image.open(img), width="stretch")
                st.session_state.ig_urls[img.name] = st.text_input(
                    f"URL {i+1}",
                    value=st.session_state.ig_urls.get(img.name, ""),
                    key=f"ig_url_w_2_{i}"
                )
            with col_e:
                st.session_state.ig_image_texts[img.name] = st.text_area(
                    f"Cap {i+1}",
                    value=st.session_state.ig_image_texts.get(img.name, ""),
                    key=f"ig_cap_w_2_{i}"
                )
                st.session_state.ig_alt_texts[img.name] = st.text_area(
                    f"Alt {i+1}",
                    value=st.session_state.ig_alt_texts.get(img.name, ""),
                    key=f"ig_alt_w_2_{i}"
                )
        st.session_state.ig_footer = st.text_area(
            "Hashtags", value=st.session_state.ig_footer, key="ig_footer_ed_2"
        )

    # ── Final review + publish ────────────────────────────────────────────────
    st.markdown("### 🔍 Final Review")
    final_cap = _assemble_caption(
        st.session_state.ig_title,
        st.session_state.ig_image_texts,
        st.session_state.ig_footer,
        images,
    )
    c1, c2 = st.columns(2)
    with c1:
        st.info("**Caption Preview**")
        if final_cap.strip(): st.text(final_cap)
        else: st.warning("(Empty)")
    with c2:
        st.info("**Media Status**")
        all_ok, u_list = True, []
        for i, img in enumerate(images):
            u = st.session_state.ig_urls.get(img.name, "").strip()
            u_list.append(u)
            if u.startswith("http"): st.write(f"✅ Img {i+1}: Ready")
            else: st.write(f"❌ Img {i+1}: Missing URL"); all_ok = False

    st.divider()
    cap_len    = len(final_cap)
    over_limit = cap_len > CHAR_LIMIT
    can_pub    = all_ok and final_cap.strip() and not over_limit

    cp, cc = st.columns([2, 1])
    with cp:
        if st.button("🚀 APPROVE & PUBLISH LIVE", type="primary", width="stretch", disabled=not can_pub):
            save_entry("instagram", project.name, final_cap, u_list)
            alts = [st.session_state.ig_alt_texts.get(img.name, "") for img in images]
            start_publish_thread(
                fn=lambda: publish_to_instagram(u_list, final_cap, alt_texts=alts),
                platform="ig",
            )
            st.rerun()
        if not all_ok:
            st.warning("⚠️ All image URLs must be filled before publishing.")
        elif not final_cap.strip():
            st.warning("⚠️ Caption is empty — add a hook, slide text, or hashtags.")
        elif over_limit:
            st.warning(
                f"⚠️ Caption is too long: {cap_len:,} / {CHAR_LIMIT:,} characters. "
                "Shorten the text to enable publishing."
            )
    with cc:
        if st.button("🗑️ Clear Project", width="stretch"): _clear(); st.rerun()
