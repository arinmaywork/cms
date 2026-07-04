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
    "youtube":   BASE_DIR / "input_youtube",
}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm",
              ".mpg", ".mpeg", ".wmv", ".flv", ".3gp"}
PLATFORM_EXTS = {
    "instagram": IMAGE_EXTS,
    "behance":   IMAGE_EXTS,
    "youtube":   VIDEO_EXTS,
}


def _valid_exts(platform: str) -> set:
    return PLATFORM_EXTS.get(platform, IMAGE_EXTS)


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
            has_images = any(f.suffix.lower() in _valid_exts(platform)
                             for f in p.iterdir())
        except OSError:
            continue  # skip unreadable subdirectory
        if has_images:
            results.append(p)
    return results


def render_upload_widget(platform: str) -> None:
    """
    Browser upload → saves files into input_<platform>/<project>/ on the
    server and queues the project. Lets any device (phone/laptop) push
    content without SFTP when the CMS runs on a VM.
    """
    exts = sorted(e.lstrip(".") for e in _valid_exts(platform))
    with st.expander("⬆️ Upload from this device", expanded=False):
        st.checkbox(
            "⏸ Pause auto-refresh while I upload (recommended for many files)",
            key=f"upl_pause_{platform}",
            help="The page normally refreshes every 3s to catch watcher events; "
                 "that can interrupt large browser uploads. Untick after saving.",
        )
        proj_name = st.text_input(
            "Project name",
            key=f"upl_name_{platform}",
            placeholder="e.g. rome-travel-film",
        )
        files = st.file_uploader(
            "Files",
            type=exts + (["jpg", "jpeg", "png"] if platform == "youtube" else []),
            accept_multiple_files=True,
            key=f"upl_files_{platform}",
            help="On YouTube you can include a .jpg/.png to use as thumbnail.",
        )
        n_seen = len(files) if files else 0
        st.caption(f"Server has received **{n_seen}** file(s)."
                   + ("" if n_seen else " If you selected files but this stays 0, "
                      "tick the pause box above and re-add them."))
        clicked = st.button("📥 Save & queue project",
                            key=f"upl_go_{platform}", type="primary",
                            disabled=not files)
        if clicked and files:
            name = (proj_name or "").strip() or f"upload-{platform}"
            safe = "".join(c if c.isalnum() or c in "-_ " else "_" for c in name).strip()
            dest = INPUT_DIRS[platform] / safe
            dest.mkdir(parents=True, exist_ok=True)
            saved = 0
            for uf in files:
                out = dest / Path(uf.name).name
                with open(out, "wb") as fh:
                    # copy in 8 MB chunks — avoids a second full copy in RAM
                    while True:
                        chunk = uf.read(8 * 1024 * 1024)
                        if not chunk:
                            break
                        fh.write(chunk)
                saved += 1
            fq_push(platform, dest)
            st.success(f"✅ Saved {saved} file(s) to `{safe}` and queued it.")
            st.rerun()


def render_folder_picker(platform: str) -> None:
    """
    Renders the browser-upload widget + detection status + scan button +
    manual loader. Call this inside the "waiting for project" branch of each UI.
    """
    render_upload_widget(platform)

    input_dir = INPUT_DIRS[platform]
    projects  = _count_projects(platform)

    # ── Loose files dropped directly into the input dir (common via SFTP) ─────
    # Projects must be folders; offer one-click grouping instead of a dead end.
    try:
        loose = sorted(f for f in input_dir.iterdir()
                       if f.is_file() and f.suffix.lower() in _valid_exts(platform))
    except OSError:
        loose = []
    if loose:
        st.warning(
            f"Found **{len(loose)} loose file(s)** directly in `{input_dir.name}/` — "
            "projects must be *folders*. Group them into one:",
            icon="📄",
        )
        gc1, gc2 = st.columns([3, 1])
        with gc1:
            grp_name = st.text_input(
                "New project folder name", key=f"loose_name_{platform}",
                value="", placeholder="e.g. integrated-photonics",
                label_visibility="collapsed",
            )
        with gc2:
            if st.button("📁 Group & queue", key=f"loose_go_{platform}",
                         type="primary", width="stretch"):
                name = (grp_name or "").strip() or f"project-{platform}"
                safe = "".join(c if c.isalnum() or c in "-_ " else "_"
                               for c in name).strip()
                dest = input_dir / safe
                dest.mkdir(parents=True, exist_ok=True)
                moved = 0
                for f in loose:
                    try:
                        f.rename(dest / f.name)
                        moved += 1
                    except OSError as e:
                        st.error(f"Could not move {f.name}: {e}")
                fq_push(platform, dest)
                st.success(f"✅ Moved {moved} file(s) into `{safe}` and queued it.")
                st.rerun()

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
                        kind = "video" if platform == "youtube" else "image"
                        st.toast(f"No {kind} project folders found — note: files must "
                                 "be inside a folder, not loose in the input directory.",
                                 icon="⚠️")
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
            files = [f for f in p.iterdir() if f.suffix.lower() in _valid_exts(platform)]
            if files:
                if st.button(
                    f"✅ Load  `{p.name}`  ({len(files)} file(s))",
                    key=f"manual_load_{platform}",
                    width="stretch",
                ):
                    fq_push(platform, p)
                    st.rerun()
            else:
                kind = ".mp4 / .mov video" if platform == "youtube" else ".jpg / .png image"
                st.warning(f"No matching files found in `{p.name}` — add {kind} files first.")
        else:
            st.warning("Path not found or not a folder.")
