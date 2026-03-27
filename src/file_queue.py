"""
src/file_queue.py
─────────────────────────────────────────────────────────────────────────────
Cross-process queue backed by JSON files in .queue/.

WHY:  The watcher runs inside launch.py (Process A).
      Streamlit runs as a subprocess (Process B).
      In-memory queues cannot cross process boundaries, so we use files as
      the IPC channel.  Each platform has its own queue file:

        .queue/instagram.json  →  list of pending folder paths (strings)
        .queue/behance.json

WRITE (watcher side):  file_queue.push(platform, path)
READ  (UI side):       file_queue.pop_all(platform) → list[Path]

Both operations use an exclusive file lock (via a companion .lock file) so
concurrent writes from the watcher thread and reads from Streamlit never
corrupt the JSON.
"""

import json
import time
import threading
from pathlib import Path

BASE_DIR   = Path(__file__).resolve().parent.parent
QUEUE_DIR  = BASE_DIR / ".queue"

_PLATFORMS = ("instagram", "behance")

# One threading.Lock per platform guards in-process concurrent access;
# the file-lock guards cross-process access.
_LOCKS: dict[str, threading.Lock] = {p: threading.Lock() for p in _PLATFORMS}


# ── Internal helpers ──────────────────────────────────────────────────────────

def _queue_file(platform: str) -> Path:
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    return QUEUE_DIR / f"{platform}.json"


def _lock_file(platform: str) -> Path:
    return QUEUE_DIR / f"{platform}.lock"


def _acquire_file_lock(platform: str, timeout: float = 5.0) -> None:
    """Spin-wait on a lock file (cross-process mutex)."""
    lf      = _lock_file(platform)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            # Atomic O_CREAT | O_EXCL — only one process wins
            fd = lf.open("x")
            fd.close()
            return
        except FileExistsError:
            time.sleep(0.05)
    # If we time out, remove the stale lock and try once more
    lf.unlink(missing_ok=True)
    lf.open("x").close()


def _release_file_lock(platform: str) -> None:
    _lock_file(platform).unlink(missing_ok=True)


def _read_raw(platform: str) -> list[str]:
    qf = _queue_file(platform)
    if not qf.exists():
        return []
    try:
        data = json.loads(qf.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _write_raw(platform: str, entries: list[str]) -> None:
    _queue_file(platform).write_text(
        json.dumps(entries, indent=2),
        encoding="utf-8",
    )


# ── Public API ────────────────────────────────────────────────────────────────

def push(platform: str, folder: Path) -> None:
    """
    Called by the watcher to enqueue a new project folder.
    Appends the absolute path string to the platform's queue file.
    """
    if platform not in _PLATFORMS:
        raise ValueError(f"Unknown platform: {platform!r}")
    path_str = str(folder.resolve())
    with _LOCKS[platform]:
        _acquire_file_lock(platform)
        try:
            entries = _read_raw(platform)
            if path_str not in entries:          # de-duplicate
                entries.append(path_str)
                _write_raw(platform, entries)
        finally:
            _release_file_lock(platform)


def pop_one(platform: str) -> Path | None:
    """
    Pops the OLDEST item from the queue and returns it.
    Atomic and cross-process safe.
    """
    if platform not in _PLATFORMS:
        raise ValueError(f"Unknown platform: {platform!r}")
    with _LOCKS[platform]:
        _acquire_file_lock(platform)
        try:
            entries = _read_raw(platform)
            if not entries:
                return None
            oldest = entries.pop(0)
            _write_raw(platform, entries)
            return Path(oldest)
        finally:
            _release_file_lock(platform)


def pop_all(platform: str) -> list[Path]:
    """
    Called by the Streamlit UI on each rerun to drain the queue.
    Returns all pending folder paths and clears the queue file atomically.
    """
    if platform not in _PLATFORMS:
        raise ValueError(f"Unknown platform: {platform!r}")
    with _LOCKS[platform]:
        _acquire_file_lock(platform)
        try:
            entries = _read_raw(platform)
            if entries:
                _write_raw(platform, [])         # clear
            return [Path(p) for p in entries]
        finally:
            _release_file_lock(platform)


def peek(platform: str) -> list[str]:
    """Non-destructive read — used by the launcher dashboard."""
    if platform not in _PLATFORMS:
        raise ValueError(f"Unknown platform: {platform!r}")
    with _LOCKS[platform]:
        return _read_raw(platform)


def clear_all() -> None:
    """Wipe both queues — called on clean launcher shutdown."""
    for platform in _PLATFORMS:
        with _LOCKS[platform]:
            _acquire_file_lock(platform)
            try:
                _write_raw(platform, [])
            finally:
                _release_file_lock(platform)
