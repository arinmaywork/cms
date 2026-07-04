"""
src/youtube_batch.py
─────────────────────────────────────────────────────────────────────────────
Persistent, adaptive YouTube upload queue.

Philosophy: no hard daily number. The worker uploads sequentially until
YouTube itself pushes back (`uploadLimitExceeded` on the channel cap, or
`quotaExceeded` on API units). On push-back it:
  1. records how many uploads succeeded today → the "observed capacity"
     (shown in the UI as what YouTube actually allows this channel),
  2. defers the remaining items,
  3. sleeps until the midnight-Pacific reset (+ a small buffer),
  4. resumes automatically — day after day until the queue is empty.

A local cap is only applied if YOUTUBE_MAX_UPLOADS_PER_DAY > 0 (opt-in).
Set it to 0 (default now) for pure YouTube-feedback mode.

State lives in .queue/youtube_batch.json and survives app restarts; the
worker is re-spawned on app start if unfinished items exist.
"""

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import src.youtube_quota as quota

ROOT        = Path(__file__).resolve().parent.parent
_STATE_FILE = ROOT / ".queue" / "youtube_batch.json"
_lock       = threading.Lock()

_WORKER_NAME = "yt-batch-worker"

# Gentle pacing between uploads (seconds) — avoids a machine-gun burst
# pattern that looks spammy to YouTube. Override in .env.
def _gap_seconds() -> int:
    try:
        return max(0, int(os.getenv("YOUTUBE_UPLOAD_GAP_SECONDS", "90")))
    except ValueError:
        return 90


# ── State I/O ─────────────────────────────────────────────────────────────────

def _empty() -> dict[str, Any]:
    return {
        "items":        [],     # [{id, meta, status, url, error, ts}]
        "paused":       False,
        "resume_at":    0,      # epoch — worker sleeps until this when limited
        "limit_note":   "",     # human-readable reason for the current wait
        "observed_cap": None,   # {"date": "YYYY-MM-DD", "count": N} learned from YouTube
    }


def _load() -> dict[str, Any]:
    if not _STATE_FILE.exists():
        return _empty()
    try:
        data = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        base = _empty()
        base.update(data)
        return base
    except (json.JSONDecodeError, OSError):
        return _empty()


def _save(data: dict[str, Any]) -> None:
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _STATE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def read() -> dict[str, Any]:
    with _lock:
        return _load()


# ── Queue operations (called from UI) ─────────────────────────────────────────

def enqueue(items: list[dict[str, Any]]) -> int:
    """Add publish items (same meta dicts publish_to_youtube takes)."""
    with _lock:
        data = _load()
        existing_paths = {it["meta"].get("path") for it in data["items"]
                          if it["status"] in ("pending", "deferred", "uploading")}
        added = 0
        for meta in items:
            meta = dict(meta)
            meta["path"] = str(meta["path"])
            if meta.get("thumbnail"):
                meta["thumbnail"] = str(meta["thumbnail"])
            if meta["path"] in existing_paths:
                continue        # don't double-queue the same file
            data["items"].append({
                "id":     uuid.uuid4().hex[:8],
                "meta":   meta,
                "status": "pending",   # pending|uploading|done|failed|deferred
                "url":    "",
                "error":  "",
                "ts":     time.time(),
            })
            added += 1
        _save(data)
    return added


def set_paused(paused: bool) -> None:
    with _lock:
        data = _load()
        data["paused"] = paused
        if not paused:
            # Manual resume also clears a waiting period (user's explicit choice)
            data["resume_at"] = 0
            data["limit_note"] = ""
            for it in data["items"]:
                if it["status"] == "deferred":
                    it["status"] = "pending"
        _save(data)


def retry_failed() -> int:
    with _lock:
        data = _load()
        n = 0
        for it in data["items"]:
            if it["status"] == "failed":
                it["status"], it["error"] = "pending", ""
                n += 1
        _save(data)
    return n


def clear_finished() -> None:
    with _lock:
        data = _load()
        data["items"] = [it for it in data["items"] if it["status"] != "done"]
        _save(data)


def remove_item(item_id: str) -> None:
    with _lock:
        data = _load()
        data["items"] = [it for it in data["items"]
                         if it["id"] != item_id or it["status"] == "uploading"]
        _save(data)


def counts() -> dict[str, int]:
    data = read()
    c = {"pending": 0, "uploading": 0, "done": 0, "failed": 0, "deferred": 0}
    for it in data["items"]:
        c[it["status"]] = c.get(it["status"], 0) + 1
    c["total"] = len(data["items"])
    return c


# ── Worker ────────────────────────────────────────────────────────────────────

def worker_running() -> bool:
    return any(t.name == _WORKER_NAME and t.is_alive()
               for t in threading.enumerate())


def ensure_worker() -> bool:
    """Start the worker if there's work and none is running. Returns True if a worker is (now) alive."""
    if worker_running():
        return True
    c = counts()
    if c["pending"] + c["deferred"] + c["uploading"] == 0:
        return False
    t = threading.Thread(target=_worker_loop, daemon=True, name=_WORKER_NAME)
    t.start()
    return True


def _is_limit_error(err: str) -> tuple[bool, str]:
    """Return (is_daily_limit, note). Matches the friendly messages from
    youtube_publisher._friendly_api_error and raw API reasons."""
    e = err.lower()
    if "uploadlimitexceeded" in e or "upload limit" in e:
        return True, "YouTube channel upload limit reached"
    if "quotaexceeded" in e or "quota exhausted" in e or "not enough api quota" in e \
       or "dailylimitexceeded" in e:
        return True, "YouTube API daily quota exhausted"
    return False, ""


def _mark(data_mutation) -> None:
    with _lock:
        data = _load()
        data_mutation(data)
        _save(data)


def _worker_loop() -> None:
    # Local import to avoid a circular import at module load
    from src.youtube_publisher import publish_to_youtube

    print("  [yt:batch] worker started")
    while True:
        with _lock:
            data = _load()

        if data["paused"]:
            time.sleep(5)
            continue

        now = time.time()
        if data["resume_at"] and now < data["resume_at"]:
            time.sleep(min(60, data["resume_at"] - now))
            continue
        if data["resume_at"] and now >= data["resume_at"]:
            # Waiting period over — reactivate deferred items
            def _reactivate(d):
                d["resume_at"] = 0
                d["limit_note"] = ""
                for it in d["items"]:
                    if it["status"] == "deferred":
                        it["status"] = "pending"
            _mark(_reactivate)
            continue

        # Recover an item stuck in "uploading" from a previous crash
        nxt = next((it for it in data["items"] if it["status"] == "uploading"), None)
        if nxt is None:
            nxt = next((it for it in data["items"] if it["status"] == "pending"), None)
        if nxt is None:
            print("  [yt:batch] queue empty — worker exiting")
            return

        item_id = nxt["id"]
        def _set_uploading(d):
            for it in d["items"]:
                if it["id"] == item_id:
                    it["status"] = "uploading"
        _mark(_set_uploading)

        meta = dict(nxt["meta"])
        meta["path"] = Path(meta["path"])
        result = publish_to_youtube([meta])

        if result.get("success"):
            url = result["results"][0]["url"] if result.get("results") else ""
            def _set_done(d):
                for it in d["items"]:
                    if it["id"] == item_id:
                        it["status"], it["url"] = "done", url
            _mark(_set_done)
            print(f"  [yt:batch] done: {meta['path']} → {url}")
            time.sleep(_gap_seconds())
            continue

        err = str(result.get("error", "unknown error"))
        limited, note = _is_limit_error(err)

        if limited:
            # YouTube said stop → learn today's real capacity, park until reset
            u = quota.usage()
            resume_at = time.time() + quota.seconds_until_reset() + 300  # +5 min buffer
            def _defer(d):
                d["resume_at"]  = resume_at
                d["limit_note"] = f"{note} after {u['uploads_today']} upload(s) today"
                d["observed_cap"] = {"date": u["date"], "count": u["uploads_today"]}
                for it in d["items"]:
                    if it["id"] == item_id or it["status"] == "pending":
                        it["status"] = "deferred"
                        it["error"]  = ""
            _mark(_defer)
            print(f"  [yt:batch] {note} — resuming after midnight PT "
                  f"(observed capacity today: {u['uploads_today']})")
            continue

        # Genuine per-item failure → mark failed, move on
        def _set_failed(d):
            for it in d["items"]:
                if it["id"] == item_id:
                    it["status"], it["error"] = "failed", err[:300]
        _mark(_set_failed)
        print(f"  [yt:batch] failed: {meta['path']} — {err[:120]}")
        time.sleep(10)
