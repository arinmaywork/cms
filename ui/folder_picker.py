"""
ui/folder_picker.py — project picker shared by all platform pages.

Projects are read straight from input_<platform>/ on every render (no
consumable queue), so refreshing the page or a second user never loses one.
Uploads go through the resumable upload service (src/upload_server.py),
not st.file_uploader — see that module for why.
"""

import os
from pathlib import Path
from urllib.parse import quote

import streamlit as st

from src.projects import INPUT_DIRS, group_loose, list_projects, loose_files

KIND = {"youtube": "video", "instagram": "image", "behance": "image", "linkedin": "image",
        "website": "image"}


def upload_url(platform: str, project: str = "") -> str:
    port = os.getenv("CMS_UPLOAD_PORT", "8502")
    try:
        host = st.context.headers.get("host", "localhost")
    except Exception:
        host = "localhost"
    host = host.split("]")[0] + "]" if host.startswith("[") else host.rsplit(":", 1)[0]
    return f"http://{host}:{port}/?platform={platform}&project={quote(project)}"


def render_project_picker(platform: str) -> Path | None:
    """Render the project grid. Returns the folder the user opened, else None."""
    in_queue: set[str] = set()
    if platform == "youtube":
        from src.youtube_batch import project_paths_in_queue
        in_queue = project_paths_in_queue()

    top1, top2 = st.columns([3, 1])
    with top1:
        st.markdown(f"### Pick a {platform.title()} project")
    with top2:
        st.link_button("⬆️ Upload files", upload_url(platform), width="stretch",
                       help="Resumable uploader — survives dropped connections")

    loose = loose_files(platform)
    if loose:
        with st.container(border=True):
            st.markdown(f"📄 **{len(loose)} loose {KIND[platform]} file(s)** sit directly in "
                        f"`{INPUT_DIRS[platform].name}/` — projects must be folders.")
            c1, c2 = st.columns([3, 1])
            name = c1.text_input("Folder name", key=f"loose_{platform}",
                                 placeholder="e.g. integrated-photonics",
                                 label_visibility="collapsed")
            if c2.button("📁 Group", key=f"loose_go_{platform}", type="primary",
                         width="stretch"):
                return group_loose(platform, name)

    projects = list_projects(platform)
    if not projects:
        st.info(f"No projects yet. Use **⬆️ Upload files**, or copy folders into "
                f"`{INPUT_DIRS[platform]}` via SFTP/rsync.", icon="📂")
        return None

    cols = st.columns(3)
    for i, p in enumerate(projects):
        with cols[i % 3].container(border=True):
            st.markdown(f"**{p['name']}**")
            st.caption(f"{p['count']} {KIND[platform]}(s) · {p['size_mb']:.0f} MB")
            if p["busy"]:
                st.caption("⏳ still uploading/copying…")
            if str(p["path"]) in in_queue:
                st.caption("📦 in upload queue")
            if st.button("Open", key=f"open_{platform}_{p['name']}", width="stretch",
                         disabled=p["busy"] or p["count"] == 0):
                return p["path"]
    st.button("🔄 Refresh list", key=f"refresh_{platform}")
    return None
