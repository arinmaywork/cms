"""
ui/folder_picker.py
─────────────────────────────────────────────────────────────────────────────
Shared "load a project folder" widget used by both Instagram and Behance UIs.

Shows:
  • A status bar: how many folders are in each input dir
  • A "🔍 Scan Now" button that flushes seen-set and re-scans immediately
  • A manual path input for when drag-and-drop / watchdog still doesn't fire
"""

from pathlib import Path
import streamlit as st
from src.file_queue import push as fq_push

BASE_DIR        = Path(__file__).resolve().parent.parent
INPUT_DIRS = {
    "instagram": BASE_DIR / "input_instagram",
    "behance":   BASE_DIR / "input_behance",
}
VALID_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def _count_projects(platform: str) -> list[Path]:
    d = INPUT_DIRS[platform]
    if not d.exists():
        return []
    results = []
    try:
        candidates = list(d.iterdir())
    except OSError:
        return []
    for p in candidates:
        if not p.is_dir() or p.name.startswith("."):
            continue
        try:
            has_images = any(f.suffix.lower() in VALID_EXTS for f in p.iterdir())
        except OSError:
            continue  # skip unreadable subdirectory
        if has_images:
            results.append(p)
    return results


def render_folder_picker(platform: str) -> None:
    """
    Renders the detection status + scan button + manual loader.
    Call this inside the "waiting for project" branch of each UI.
    """
    input_dir = INPUT_DIRS[platform]
    projects  = _count_projects(platform)

    # ── Status card ───────────────────────────────────────────────────────────
    if projects:
        st.success(
            f"📁 **{len(projects)} project folder(s)** found in `{input_dir.name}/` "
            f"but not yet loaded. Click **Scan Now** to detect them.",
            icon="✅",
        )
    else:
        st.info(
            f"Drop a project folder into  `{input_dir}/`  \n"
            f"then click **🔍 Scan Now** if it doesn't appear automatically.",
            icon="📂",
        )

    col_scan, col_open = st.columns([1, 2])

    # ── Scan Now button ───────────────────────────────────────────────────────
    with col_scan:
        if st.button("🔍 Scan Now", key=f"scan_{platform}", width="stretch",
                     help="Re-scan the input folder immediately"):
            try:
                # Import here to avoid circular deps
                from src.watcher import scan_once
                n = scan_once()
                if n:
                    st.toast(f"Found {n} new project(s)!", icon="✅")
                    st.rerun()
                else:
                    # Queue any existing folder directly even if scan_once skips it
                    pushed = 0
                    for p in _count_projects(platform):
                        fq_push(platform, p)
                        pushed += 1
                    if pushed:
                        st.toast(f"Queued {pushed} folder(s).", icon="📂")
                        st.rerun()
                    else:
                        st.toast("No image folders found in the input directory.", icon="⚠️")
            except Exception as e:
                st.error(f"Scan error: {e}")

    # ── Open input folder in Finder/Explorer ─────────────────────────────────
    with col_open:
        abs_path = str(input_dir.resolve())
        st.code(abs_path, language=None)

    st.markdown("**Or load a folder path manually:**")

    # ── Manual path input ─────────────────────────────────────────────────────
    manual = st.text_input(
        "Paste folder path here:",
        key=f"manual_path_{platform}",
        placeholder="/Users/you/Desktop/my-project   (or drag the folder here)",
        label_visibility="collapsed",
    )
    if manual:
        p = Path(manual.strip().strip("'\""))   # strip shell quotes
        if p.is_dir():
            imgs = [f for f in p.iterdir() if f.suffix.lower() in VALID_EXTS]
            if imgs:
                if st.button(
                    f"✅ Load  `{p.name}`  ({len(imgs)} image(s))",
                    key=f"manual_load_{platform}",
                    width="stretch",
                ):
                    fq_push(platform, p)
                    st.rerun()
            else:
                st.warning(f"No images found in `{p.name}` — add .jpg / .png files first.")
        else:
            st.warning("Path not found or not a folder.")
