"""
src/projects.py — the input folders ARE the source of truth.

No consumable queue: projects are listed straight from input_<platform>/
on every render, so a browser refresh or a second user never "loses" one.
After a successful publish a project is moved to archive/<platform>/, so
it can't reappear and be published twice.

Drafts (AI text + your edits) persist in <project>/.cms.json so they
survive refreshes, restarts and crashes.
"""

import json
import re
import shutil
import time
from pathlib import Path

from src.fsutil import atomic_write_json, load_json
from src.natsort import natkey

ROOT        = Path(__file__).resolve().parent.parent
ARCHIVE_DIR = ROOT / "archive"
INPUT_DIRS  = {
    "instagram": ROOT / "input_instagram",
    "behance":   ROOT / "input_behance",
    "youtube":   ROOT / "input_youtube",
}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm",
              ".mpg", ".mpeg", ".wmv", ".flv", ".3gp"}
PLATFORM_EXTS = {"instagram": IMAGE_EXTS, "behance": IMAGE_EXTS, "youtube": VIDEO_EXTS}
DRAFT_FILE = ".cms.json"
BUSY_SECONDS = 20   # files modified this recently = still being copied


def media_files(folder: Path, platform: str) -> list[Path]:
    """Natural-sorted media files; skips dotfiles (incl. macOS '._x.mp4' forks)."""
    exts = PLATFORM_EXTS[platform]
    try:
        return sorted((f for f in folder.iterdir()
                       if f.is_file() and not f.name.startswith(".")
                       and f.suffix.lower() in exts),
                      key=lambda f: natkey(f.name))
    except OSError:
        return []


def list_projects(platform: str) -> list[dict]:
    d = INPUT_DIRS[platform]
    d.mkdir(parents=True, exist_ok=True)
    out = []
    now = time.time()
    for p in sorted((x for x in d.iterdir() if x.is_dir() and not x.name.startswith(".")),
                    key=lambda x: natkey(x.name)):
        files = media_files(p, platform)
        try:
            entries = list(p.iterdir())
        except OSError:
            continue
        uploading = any(e.name.endswith(".part") for e in entries)
        recent = any(now - e.stat().st_mtime < BUSY_SECONDS for e in entries if e.is_file())
        out.append({
            "path": p, "name": p.name, "count": len(files),
            "size_mb": sum(f.stat().st_size for f in files) / 1_048_576,
            "busy": uploading or recent,
        })
    return out


def loose_files(platform: str) -> list[Path]:
    d = INPUT_DIRS[platform]
    try:
        return sorted((f for f in d.iterdir()
                       if f.is_file() and not f.name.startswith(".")
                       and f.suffix.lower() in PLATFORM_EXTS[platform]),
                      key=lambda f: natkey(f.name))
    except OSError:
        return []


def safe_name(name: str, fallback: str = "project") -> str:
    s = re.sub(r"[^\w\-. ()&,+#]", "_", (name or "").strip()).strip(" .")
    return s[:120] or fallback


def group_loose(platform: str, name: str) -> Path:
    dest = INPUT_DIRS[platform] / safe_name(name, f"project-{platform}")
    dest.mkdir(parents=True, exist_ok=True)
    for f in loose_files(platform):
        f.rename(dest / f.name)
    return dest


def archive_project(platform: str, folder: Path) -> Path | None:
    folder = Path(folder)
    if not folder.exists():
        return None
    dest_root = ARCHIVE_DIR / platform
    dest_root.mkdir(parents=True, exist_ok=True)
    dest = dest_root / folder.name
    if dest.exists():
        dest = dest_root / f"{folder.name}-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.move(str(folder), str(dest))
    print(f"  [projects] archived {folder.name} → archive/{platform}/{dest.name}")
    return dest


def load_draft(folder: Path) -> dict:
    data = load_json(Path(folder) / DRAFT_FILE, {})
    return data if isinstance(data, dict) else {}


def save_draft(folder: Path, data: dict) -> None:
    """Atomic; skips the write when nothing changed (cheap on every render)."""
    folder = Path(folder)
    if not folder.exists():
        return
    p = folder / DRAFT_FILE
    new = json.dumps(data, sort_keys=True, ensure_ascii=False)
    try:
        if p.exists() and json.dumps(load_draft(folder), sort_keys=True,
                                     ensure_ascii=False) == new:
            return
    except Exception:
        pass
    atomic_write_json(p, data)
