"""
src/youtube_quota.py
─────────────────────────────────────────────────────────────────────────────
Local YouTube Data API quota + upload-limit bookkeeping.

Two independent limits are tracked (both real, both enforced by Google):

1. API QUOTA — every Google Cloud project gets 10,000 units/day by default,
   resetting at midnight *Pacific Time*.  Costs (per Google's quota
   calculator, current 2026):
       videos.insert        100      playlists.insert      50
       thumbnails.set        50      playlistItems.insert  50
       videos.update         50      playlists.list         1
       channels.list          1      videoCategories.list   1

2. CHANNEL UPLOAD LIMIT — separate from API quota. YouTube caps how many
   videos a channel may upload per 24h (varies by channel age/standing;
   ~10-15 for newer channels). Exceeding it returns `uploadLimitExceeded`.
   We keep a conservative local cap (YOUTUBE_MAX_UPLOADS_PER_DAY, default 10)
   and stop the queue with a clear "resumes after midnight PT" message
   instead of letting YouTube reject mid-batch.

State lives in .queue/youtube_quota.json and self-resets when the Pacific
date changes.
"""

import json
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT        = Path(__file__).resolve().parent.parent
_QUOTA_FILE = ROOT / ".queue" / "youtube_quota.json"
_lock       = threading.Lock()

PACIFIC = ZoneInfo("America/Los_Angeles")

# Per-method quota costs — source: developers.google.com/youtube/v3/determine_quota_cost
COSTS: dict[str, int] = {
    "videos.insert":        100,
    "videos.update":         50,
    "thumbnails.set":        50,
    "playlists.insert":      50,
    "playlists.list":         1,
    "playlistItems.insert":  50,
    "channels.list":          1,
    "videoCategories.list":   1,
}


def daily_quota() -> int:
    try:
        return int(os.getenv("YOUTUBE_DAILY_QUOTA", "10000"))
    except ValueError:
        return 10_000


def max_uploads_per_day() -> int:
    """0 (default) = auto mode: no local cap, YouTube's own feedback governs
    the pace (see src/youtube_batch.py). Set >0 to enforce a local cap."""
    try:
        return int(os.getenv("YOUTUBE_MAX_UPLOADS_PER_DAY", "0"))
    except ValueError:
        return 0


# ── State I/O ─────────────────────────────────────────────────────────────────

def _today_pacific() -> str:
    return datetime.now(PACIFIC).strftime("%Y-%m-%d")


def _empty() -> dict[str, Any]:
    return {"date": _today_pacific(), "units": 0, "uploads": 0, "events": []}


def _load() -> dict[str, Any]:
    if not _QUOTA_FILE.exists():
        return _empty()
    try:
        data = json.loads(_QUOTA_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _empty()
    if data.get("date") != _today_pacific():        # new Pacific day → reset
        return _empty()
    return data


def _save(data: dict[str, Any]) -> None:
    _QUOTA_FILE.parent.mkdir(parents=True, exist_ok=True)
    _QUOTA_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ── Public API ────────────────────────────────────────────────────────────────

def record(method: str, note: str = "") -> None:
    """Record one API call's quota cost (and count uploads)."""
    cost = COSTS.get(method, 1)
    with _lock:
        data = _load()
        data["units"] += cost
        if method == "videos.insert":
            data["uploads"] += 1
        data["events"].append({
            "ts": time.time(), "method": method, "cost": cost, "note": note,
        })
        data["events"] = data["events"][-200:]
        _save(data)


def usage() -> dict[str, Any]:
    """Current usage snapshot for the UI."""
    with _lock:
        data = _load()
    quota = daily_quota()
    return {
        "date":            data["date"],
        "units_used":      data["units"],
        "units_remaining": max(0, quota - data["units"]),
        "daily_quota":     quota,
        "uploads_today":   data["uploads"],
        "uploads_cap":     max_uploads_per_day(),
    }


def estimate_cost(n_videos: int, n_thumbnails: int = 0,
                  n_playlist_adds: int = 0, n_new_playlists: int = 0) -> int:
    return (n_videos * COSTS["videos.insert"]
            + n_thumbnails * COSTS["thumbnails.set"]
            + n_playlist_adds * COSTS["playlistItems.insert"]
            + n_new_playlists * COSTS["playlists.insert"])


def can_publish(n_videos: int, planned_cost: int) -> tuple[bool, str]:
    """
    Pre-flight check before a publish batch.
    Returns (ok, reason). reason explains what's blocking and when it resets.
    """
    u = usage()
    # uploads_cap == 0 → auto mode: YouTube's live feedback governs the pace
    if u["uploads_cap"] > 0 and u["uploads_today"] + n_videos > u["uploads_cap"]:
        left = max(0, u["uploads_cap"] - u["uploads_today"])
        return False, (
            f"Daily upload cap reached: {u['uploads_today']}/{u['uploads_cap']} "
            f"used, {left} slot(s) left but {n_videos} requested. "
            f"Resets {time_until_reset_str()} (midnight Pacific). "
            "Set YOUTUBE_MAX_UPLOADS_PER_DAY=0 for auto mode (YouTube-governed)."
        )
    if planned_cost > u["units_remaining"]:
        return False, (
            f"Not enough API quota: this publish needs ~{planned_cost} units "
            f"but only {u['units_remaining']} of {u['daily_quota']} remain today. "
            f"Quota resets {time_until_reset_str()} (midnight Pacific)."
        )
    return True, ""


def seconds_until_reset() -> int:
    now = datetime.now(PACIFIC)
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0,
                                                 microsecond=0)
    return int((tomorrow - now).total_seconds())


def time_until_reset_str() -> str:
    s = seconds_until_reset()
    h, m = s // 3600, (s % 3600) // 60
    return f"in {h}h {m:02d}m"
