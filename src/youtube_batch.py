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
from src.fsutil import atomic_write_json, load_json

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
        "observed_cap": None,
        "pause_reason": "",     # "auth" when paused because login expired   # {"date": "YYYY-MM-DD", "count": N} learned from YouTube
    }


def _load() -> dict[str, Any]:
    data = load_json(_STATE_FILE, None)   # corrupt file is preserved, never overwritten
    base = _empty()
    if isinstance(data, dict):
        base.update(data)
    return base


def _save(data: dict[str, Any]) -> None:
    atomic_write_json(_STATE_FILE, data)


def read() -> dict[str, Any]:
    with _lock:
        return _load()


# ── Queue operations (called from UI) ─────────────────────────────────────────

def enqueue(items: list[dict[str, Any]]) -> int:
    """Add publish items (same meta dicts publish_to_youtube takes).
    De-dupes against EVERY existing item — including done — so re-queuing a
    project after a restart can never upload duplicates. To re-publish a
    finished video intentionally, use 'Clear finished' first."""
    with _lock:
        data = _load()
        existing_paths = {it["meta"].get("path") for it in data["items"]}
        added = 0
        for meta in items:
            meta = dict(meta)
            meta["path"] = str(meta["path"])
            if meta.get("thumbnail"):
                meta["thumbnail"] = str(meta["thumbnail"])
            if meta["path"] in existing_paths:
                continue        # don't double-queue the same file
            data["items"].append({
                "id":       uuid.uuid4().hex[:8],
                "meta":     meta,
                "status":   "pending",   # pending|uploading|done|failed|deferred
                "url":      "",
                "error":    "",
                "attempts": 0,
                "ts":       time.time(),
            })
            added += 1
        _save(data)
    return added


def set_paused(paused: bool) -> None:
    with _lock:
        data = _load()
        data["paused"] = paused
        if not paused:
            data["pause_reason"] = ""
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


def remove_items(item_ids: list[str]) -> None:
    ids = set(item_ids)
    with _lock:
        data = _load()
        data["items"] = [it for it in data["items"]
                         if it["id"] not in ids or it["status"] == "uploading"]
        _save(data)


def project_paths_in_queue() -> set[str]:
    return {str(Path(it["meta"]["path"]).parent) for it in read()["items"]}


def counts() -> dict[str, int]:
    data = read()
    c = {"pending": 0, "uploading": 0, "done": 0, "failed": 0, "deferred": 0}
    for it in data["items"]:
        c[it["status"]] = c.get(it["status"], 0) + 1
    c["total"] = len(data["items"])
    return c


# ── Worker ────────────────────────────────────────────────────────────────────

# Exactly ONE worker across ALL processes (launcher + Streamlit): an OS-level
# flock held for the worker's lifetime. The kernel releases it if the process
# dies, so a crash can never leave a stale lock. Two workers = duplicate uploads.
_WORKER_LOCK = ROOT / ".queue" / "yt_worker.lock"
_NO_FCNTL = -1


def _try_lock() -> int | None:
    try:
        import fcntl
    except ImportError:                      # Windows: fall back to in-process check
        return _NO_FCNTL
    _WORKER_LOCK.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(_WORKER_LOCK, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except OSError:
        os.close(fd)
        return None


def _local_worker_alive() -> bool:
    return any(t.name == _WORKER_NAME and t.is_alive() for t in threading.enumerate())


def worker_running() -> bool:
    if _local_worker_alive():
        return True
    fd = _try_lock()
    if fd is None:
        return True                          # held by the other process
    if fd != _NO_FCNTL:
        os.close(fd)
    return False


def ensure_worker() -> bool:
    """Start the worker if there's work and none runs anywhere. True if one is alive."""
    if _local_worker_alive():
        return True
    c = counts()
    if c["pending"] + c["deferred"] + c["uploading"] == 0:
        return False
    fd = _try_lock()
    if fd is None:
        return True

    def _main():
        try:
            _worker_loop()
        finally:
            if fd != _NO_FCNTL:
                os.close(fd)

    threading.Thread(target=_main, daemon=True, name=_WORKER_NAME).start()
    return True


def _maybe_archive(folder: Path) -> None:
    """Move a YouTube project to archive/ once every queued video in it is done."""
    from src.projects import INPUT_DIRS, archive_project
    if folder.parent != INPUT_DIRS["youtube"]:
        return
    siblings = [it for it in read()["items"]
                if Path(it["meta"]["path"]).parent == folder]
    if siblings and all(it["status"] == "done" for it in siblings):
        try:
            archive_project("youtube", folder)
        except Exception as e:
            print(f"  [yt:batch] archive failed for {folder.name}: {e}")


def _is_limit_error(err: str) -> tuple[bool, str]:
    """Return (is_daily_limit, note). Primary signal: the LIMIT_MARKER that
    youtube_publisher embeds after structured HttpError-reason parsing.
    Substring fallbacks cover preflight messages and raw API text."""
    e = err.lower()
    if "[[limit]]" in e or "uploadlimitexceeded" in e or "upload limit" in e \
       or "exceeded the number of videos" in e:
        return True, "YouTube channel upload limit reached"
    if "quotaexceeded" in e or "quota exhausted" in e or "not enough api quota" in e \
       or "dailylimitexceeded" in e or "exceeded your quota" in e:
        return True, "YouTube API daily quota exhausted"
    return False, ""


# Circuit breaker: this many consecutive item failures (whatever the error)
# means something systemic — stop burning the queue and wait for the reset.
MAX_CONSECUTIVE_FAILURES = 3


def _mark(data_mutation) -> None:
    with _lock:
        data = _load()
        data_mutation(data)
        _save(data)


def _worker_loop() -> None:
    # Local import to avoid a circular import at module load
    from src.youtube_publisher import publish_to_youtube

    print("  [yt:batch] worker started")
    consecutive_failures = 0
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
            # Waiting period over — reactivate deferred items, and give failed
            # items (< 3 attempts) another chance: most "failures" in a limit
            # window are really limit errors in disguise.
            def _reactivate(d):
                d["resume_at"] = 0
                d["limit_note"] = ""
                for it in d["items"]:
                    if it["status"] == "deferred":
                        it["status"] = "pending"
                    elif (it["status"] == "failed"
                          and it.get("attempts", 0) < 3):
                        it["status"], it["error"] = "pending", ""
            _mark(_reactivate)
            print("  [yt:batch] reset window passed — queue reactivated")
            continue

        # Natural order (1.1.2 before 1.1.10) regardless of enqueue order,
        # so uploads — and therefore playlist order — follow the curriculum.
        from src.natsort import natkey
        by_order = sorted(data["items"],
                          key=lambda it: natkey(Path(it["meta"]["path"]).name))
        # Recover an item stuck in "uploading" from a previous crash
        nxt = next((it for it in by_order if it["status"] == "uploading"), None)
        if nxt is None:
            nxt = next((it for it in by_order if it["status"] == "pending"), None)
        if nxt is None:
            print("  [yt:batch] queue empty — worker exiting")
            return

        # Crash recovery: YouTube may have accepted this video before we saved
        # its ID. Check recent uploads (~2 units) before uploading it again.
        if nxt["status"] == "uploading":
            found = None
            try:
                from src.youtube_publisher import find_recent_upload
                found = find_recent_upload(nxt["meta"].get("title")
                                           or Path(nxt["meta"]["path"]).stem)
            except Exception as e:
                print(f"  [yt:batch] duplicate check skipped: {e}")
            if found:
                rid = nxt["id"]
                def _recovered(d):
                    for it in d["items"]:
                        if it["id"] == rid:
                            it["status"], it["url"] = "done", found
                _mark(_recovered)
                print(f"  [yt:batch] recovered already-uploaded item → {found}")
                _maybe_archive(Path(nxt["meta"]["path"]).parent)
                continue

        item_id = nxt["id"]
        def _set_uploading(d):
            for it in d["items"]:
                if it["id"] == item_id:
                    it["status"] = "uploading"
                    it["attempts"] = it.get("attempts", 0) + 1
        _mark(_set_uploading)

        meta = dict(nxt["meta"])
        meta["path"] = Path(meta["path"])

        # Resolve a named playlist ONCE and stamp the ID onto every queued
        # item that references it — prevents one-playlist-per-video dupes
        # (publish_to_youtube's ensure_playlist is a second safety net).
        npt = (meta.get("new_playlist_title") or "").strip()
        if npt and not meta.get("playlist_id"):
            try:
                from src.youtube_publisher import ensure_playlist
                pl_privacy = (meta.get("privacy", "unlisted")
                              if meta.get("privacy") != "private" else "unlisted")
                pid = ensure_playlist(npt, pl_privacy)
                def _stamp(d):
                    for it in d["items"]:
                        if (it["meta"].get("new_playlist_title") or "").strip().lower() == npt.lower():
                            it["meta"]["playlist_id"] = pid
                            it["meta"]["new_playlist_title"] = ""
                _mark(_stamp)
                meta["playlist_id"] = pid
                meta["new_playlist_title"] = ""
                print(f"  [yt:batch] playlist '{npt}' resolved → {pid}")
            except Exception as e:
                print(f"  [yt:batch] playlist resolve failed ({e}) — "
                      "publisher will handle it per-item")

        result = publish_to_youtube([meta])

        if result.get("success"):
            url = result["results"][0]["url"] if result.get("results") else ""
            def _set_done(d):
                for it in d["items"]:
                    if it["id"] == item_id:
                        it["status"], it["url"] = "done", url
            _mark(_set_done)
            consecutive_failures = 0
            _maybe_archive(Path(meta["path"]).parent)
            print(f"  [yt:batch] done: {meta['path']} → {url}")
            time.sleep(_gap_seconds())
            continue

        err = str(result.get("error", "unknown error"))
        limited, note = _is_limit_error(err)
        err_display = err.replace("[[LIMIT]]", "").strip()

        if "[[AUTH]]" in err:
            # Login problem: pause immediately, don't burn attempts or wait
            # for midnight — resumes as soon as you reconnect.
            def _auth_pause(d):
                d["paused"], d["pause_reason"] = True, "auth"
                d["limit_note"] = ("YouTube login expired — reconnect in "
                                   "⚙️ Settings; the queue resumes automatically")
                for it in d["items"]:
                    if it["id"] == item_id:
                        it["status"], it["error"] = "pending", ""
                        it["attempts"] = max(0, it.get("attempts", 1) - 1)
            _mark(_auth_pause)
            consecutive_failures = 0
            print("  [yt:batch] auth error — queue paused until reconnect")
            continue

        if not limited:
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                # Same-looking failures back to back: almost certainly a
                # systemic condition (undetected limit, expired auth, network).
                # Stop burning through the queue; park until the reset.
                limited = True
                note = (f"{MAX_CONSECUTIVE_FAILURES} consecutive failures "
                        f"(last: {err_display[:80]}) — pausing as a safety measure")

        if limited:
            # YouTube said stop → learn today's real capacity, park until reset
            u = quota.usage()
            resume_at = time.time() + quota.seconds_until_reset() + 300  # +5 min buffer
            def _defer(d):
                d["resume_at"]  = resume_at
                d["limit_note"] = f"{note} — after {u['uploads_today']} upload(s) today"
                if u["uploads_today"] > 0:
                    d["observed_cap"] = {"date": u["date"], "count": u["uploads_today"]}
                for it in d["items"]:
                    if it["id"] == item_id or it["status"] in ("pending", "uploading"):
                        it["status"] = "deferred"
                        it["error"]  = ""
            _mark(_defer)
            consecutive_failures = 0
            print(f"  [yt:batch] {note} — resuming after midnight PT "
                  f"(uploads completed today: {u['uploads_today']})")
            continue

        # Genuine per-item failure → mark failed, move on
        def _set_failed(d):
            for it in d["items"]:
                if it["id"] == item_id:
                    it["status"], it["error"] = "failed", err_display[:300]
        _mark(_set_failed)
        print(f"  [yt:batch] failed: {meta['path']} — {err_display[:120]}")
        time.sleep(10)
