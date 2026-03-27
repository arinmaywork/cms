"""
src/progress.py
─────────────────────────────────────────────────────────────────────────────
Lightweight progress-tracking system for publish operations.

Architecture
------------
Publishers (running in background threads) call:
    progress.update(step, status, detail)

The Streamlit UI (main thread) polls:
    progress.read() → list of Step dicts

State is stored in .queue/publish_progress.json so it works across the
publisher thread boundary without any shared memory.

Step statuses
-------------
  "pending"  → grey,  not started yet
  "active"   → blue,  currently running (with animated indicator)
  "done"     → green, completed successfully
  "error"    → red,   failed
"""

import json
import threading
import time
from pathlib import Path
from typing import Any

BASE_DIR       = Path(__file__).resolve().parent.parent
_PROGRESS_FILE = BASE_DIR / ".queue" / "publish_progress.json"
_LOCK_FILE     = BASE_DIR / ".queue" / "publish_progress.lock"
_lock          = threading.Lock()   # guards in-process concurrent access


# ── Cross-process file lock (mirrors file_queue.py pattern) ───────────────────
def _acquire_file_lock(timeout: float = 5.0) -> None:
    """Spin-wait on a lock file to guard cross-process reads/writes."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            fd = _LOCK_FILE.open("x")
            fd.close()
            return
        except FileExistsError:
            time.sleep(0.05)
    # Timed out — remove stale lock and claim it
    _LOCK_FILE.unlink(missing_ok=True)
    _LOCK_FILE.open("x").close()


def _release_file_lock() -> None:
    _LOCK_FILE.unlink(missing_ok=True)


# ── Data model ────────────────────────────────────────────────────────────────
def _empty() -> dict[str, Any]:
    return {
        "active":    False,
        "platform":  "",
        "started":   0.0,
        "steps":     [],     # list of Step dicts (see below)
        "result":    None,   # final result dict written by publisher
        "error":     None,   # error string if something blew up
    }


def _step(label: str, status: str = "pending", detail: str = "") -> dict:
    return {
        "label":   label,
        "status":  status,   # pending | active | done | error
        "detail":  detail,
        "ts":      time.time(),
    }


# ── Writer API (called from publisher thread) ─────────────────────────────────

def start(platform: str, step_labels: list[str]) -> None:
    """
    Initialise a new publish session.
    Call this before spawning the publisher thread.
    """
    data = _empty()
    data["active"]   = True
    data["platform"] = platform
    data["started"]  = time.time()
    data["steps"]    = [_step(label) for label in step_labels]
    with _lock:
        _write(data)


def update(step_index: int, status: str, detail: str = "") -> None:
    """Mark step[step_index] with the given status and optional detail text."""
    with _lock:
        data = _read_raw()
        if step_index < len(data["steps"]):
            data["steps"][step_index]["status"] = status
            data["steps"][step_index]["detail"] = detail
            data["steps"][step_index]["ts"]     = time.time()
        _write(data)


def finish(result: dict[str, Any]) -> None:
    """Mark the session as complete with a final result dict."""
    with _lock:
        data = _read_raw()
        data["active"] = False
        data["result"] = result
        _write(data)


def fail(error: str) -> None:
    """Mark the session as failed."""
    with _lock:
        data = _read_raw()
        data["active"] = False
        data["error"]  = error
        _write(data)


def clear() -> None:
    """Reset progress state (call before starting a new publish)."""
    with _lock:
        _write(_empty())


# ── Reader API (called from Streamlit main thread) ────────────────────────────

def read() -> dict[str, Any]:
    """Return the current progress state. Never raises."""
    try:
        _acquire_file_lock()
        try:
            return _read_raw()
        finally:
            _release_file_lock()
    except Exception:
        return _empty()


def is_active() -> bool:
    return read().get("active", False)


# ── Internal I/O ──────────────────────────────────────────────────────────────

def _read_raw() -> dict[str, Any]:
    """Read progress file. Caller must hold _lock (in-process) before calling."""
    if not _PROGRESS_FILE.exists():
        return _empty()
    try:
        return json.loads(_PROGRESS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _empty()


def _write(data: dict[str, Any]) -> None:
    """Write progress file with cross-process file lock. Caller holds _lock."""
    _PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _acquire_file_lock()
    try:
        _PROGRESS_FILE.write_text(
            json.dumps(data, indent=2),
            encoding="utf-8",
        )
    finally:
        _release_file_lock()
