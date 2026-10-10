"""
src/history_manager.py
Thread-safe JSON history for Instagram and Behance.
Each platform maintains its own file.
The AI generator reads the last N entries; the UI writes on approval.
"""

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from src.fsutil import atomic_write_json, load_json

BASE_DIR    = Path(__file__).resolve().parent.parent
HISTORY_DIR = BASE_DIR / "history"

_FILES: dict[str, Path] = {
    "instagram": HISTORY_DIR / "history_instagram.json",
    "behance":   HISTORY_DIR / "history_behance.json",
    "youtube":   HISTORY_DIR / "history_youtube.json",
}

_locks: dict[str, threading.Lock] = {
    "instagram": threading.Lock(),
    "behance":   threading.Lock(),
    "youtube":   threading.Lock(),
}


# ── Internals ─────────────────────────────────────────────────────────────────
def _load(platform: str) -> list[dict[str, Any]]:
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    data = load_json(_FILES[platform], [])
    return data if isinstance(data, list) else []


def _save(platform: str, data: list[dict[str, Any]]) -> None:
    atomic_write_json(_FILES[platform], data)


# ── Public API ────────────────────────────────────────────────────────────────
def get_last_n(platform: str, n: int = 5) -> list[dict[str, Any]]:
    """Return the last *n* history entries for the given platform."""
    if platform not in _FILES:
        raise ValueError(f"Unknown platform: {platform!r}")
    with _locks[platform]:
        history = _load(platform)
    return history[-n:] if history else []


def save_entry(
    platform:    str,
    project:     str,
    content:     str,
    image_paths: list[str],
) -> None:
    """Append a new entry to the platform's history file."""
    if platform not in _FILES:
        raise ValueError(f"Unknown platform: {platform!r}")
    entry: dict[str, Any] = {
        "timestamp": datetime.now().isoformat(),
        "project":   project,
        "content":   content,
        "images":    image_paths,
    }
    with _locks[platform]:
        history = _load(platform)
        history.append(entry)
        _save(platform, history)
    print(f"[history:{platform}] Saved entry for project '{project}'")
