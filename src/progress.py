"""
src/progress.py
─────────────────────────────────────────────────────────────────────────────
Lightweight progress-tracking system for publish operations.

Namespaced: each pipeline ("ig", "ig_automation", "behance", "yt", "yt_ai")
writes to its own file — .queue/progress_{ns}.json — so an Instagram publish
and a long YouTube upload can run at the same time without clobbering each
other's state.

Architecture
------------
Publishers (running in background threads) call:
    progress.update(ns, step, status, detail)

The Streamlit UI (main thread) polls:
    progress.read(ns) → state dict with a list of Step dicts

Step statuses
-------------
  "pending"  → grey,  not started yet
  "active"   → blue,  currently running (with animated indicator)
  "done"     → green, completed successfully
  "error"    → red,   failed
"""

import json
import re
import threading
import time
from pathlib import Path
from typing import Any

BASE_DIR   = Path(__file__).resolve().parent.parent
_QUEUE_DIR = BASE_DIR / ".queue"
_lock      = threading.Lock()   # guards in-process concurrent access


def _safe_ns(ns: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_\-]", "_", ns) or "default"


def _progress_file(ns: str) -> Path:
    return _QUEUE_DIR / f"progress_{_safe_ns(ns)}.json"


def _lock_file(ns: str) -> Path:
    return _QUEUE_DIR / f"progress_{_safe_ns(ns)}.lock"


# ── Cross-process file lock (mirrors file_queue.py pattern) ───────────────────
def _acquire_file_lock(ns: str, timeout: float = 5.0) -> None:
    lf = _lock_file(ns)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            fd = lf.open("x")
            fd.close()
            return
        except FileExistsError:
            time.sleep(0.05)
        except FileNotFoundError:
            lf.parent.mkdir(parents=True, exist_ok=True)
    # Timed out — remove stale lock and claim it
    lf.unlink(missing_ok=True)
    try:
        lf.open("x").close()
    except Exception:
        pass


def _release_file_lock(ns: str) -> None:
    _lock_file(ns).unlink(missing_ok=True)


# ── Data model ────────────────────────────────────────────────────────────────
def _empty() -> dict[str, Any]:
    return {
        "active":    False,
        "platform":  "",
        "started":   0.0,
        "steps":     [],     # list of Step dicts
        "result":    None,   # final result dict written by publisher
        "error":     None,   # error string if something blew up
    }


def _step(label: str, status: str = "pending", detail: str = "") -> dict:
    return {"label": label, "status": status, "detail": detail, "ts": time.time()}


# ── Writer API (called from publisher thread) ─────────────────────────────────

def start(ns: str, step_labels: list[str]) -> None:
    """Initialise a new publish session for namespace *ns*."""
    data = _empty()
    data["active"]   = True
    data["platform"] = ns
    data["started"]  = time.time()
    data["steps"]    = [_step(label) for label in step_labels]
    with _lock:
        _write(ns, data)


def add_steps(ns: str, step_labels: list[str]) -> None:
    """Append extra steps to a running session."""
    with _lock:
        data = _read_raw(ns)
        data["steps"].extend(_step(label) for label in step_labels)
        _write(ns, data)


def update(ns: str, step_index: int, status: str, detail: str = "") -> None:
    """Mark step[step_index] with the given status and optional detail text."""
    with _lock:
        data = _read_raw(ns)
        if 0 <= step_index < len(data["steps"]):
            data["steps"][step_index]["status"] = status
            data["steps"][step_index]["detail"] = detail
            data["steps"][step_index]["ts"]     = time.time()
        _write(ns, data)


def finish(ns: str, result: dict[str, Any]) -> None:
    """Mark the session as complete with a final result dict."""
    with _lock:
        data = _read_raw(ns)
        data["active"] = False
        data["result"] = result
        _write(ns, data)


def fail(ns: str, error: str) -> None:
    """Mark the session as failed."""
    with _lock:
        data = _read_raw(ns)
        data["active"] = False
        data["error"]  = error
        _write(ns, data)


def clear(ns: str) -> None:
    """Reset progress state (call before starting a new publish)."""
    with _lock:
        _write(ns, _empty())


# ── Reader API (called from Streamlit main thread) ────────────────────────────

def read(ns: str) -> dict[str, Any]:
    """Return the current progress state for *ns*. Never raises."""
    try:
        _acquire_file_lock(ns)
        try:
            return _read_raw(ns)
        finally:
            _release_file_lock(ns)
    except Exception:
        return _empty()


def is_active(ns: str) -> bool:
    return read(ns).get("active", False)


# ── Internal I/O ──────────────────────────────────────────────────────────────

def _read_raw(ns: str) -> dict[str, Any]:
    pf = _progress_file(ns)
    if not pf.exists():
        return _empty()
    try:
        return json.loads(pf.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _empty()


def _write(ns: str, data: dict[str, Any]) -> None:
    pf = _progress_file(ns)
    pf.parent.mkdir(parents=True, exist_ok=True)
    _acquire_file_lock(ns)
    try:
        pf.write_text(json.dumps(data, indent=2), encoding="utf-8")
    finally:
        _release_file_lock(ns)
