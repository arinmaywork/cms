"""
src/youtube_publisher.py
─────────────────────────────────────────────────────────────────────────────
Uploads videos to YouTube via the Data API v3 (resumable protocol).

Per video:
  1. Metadata validation (YouTube hard limits: title ≤100 chars & no <>,
     description ≤5000 bytes, tags ≤500 chars total)
  2. Resumable chunked upload with live % progress + exponential-backoff
     retries on transient (5xx / network) errors
  3. Optional custom thumbnail (thumbnails.set — needs a phone-verified
     channel; failure is non-fatal)
  4. Optional playlist add (existing or newly created)
  5. Privacy: public / unlisted / private, or scheduled (publishAt →
     video stays private until the scheduled time, per YouTube rules)
  6. Made-for-kids self-declaration (required by COPPA — YouTube demands
     this flag on every upload)

Every API call is recorded in src.youtube_quota so the UI can show
remaining quota and stop batches BEFORE Google rejects them.

Progress namespace: "yt".
"""

import mimetypes
import random
import time
from pathlib import Path
from typing import Any

import src.progress as progress
import src.youtube_quota as quota
from src.youtube_auth import get_service

NS = "yt"

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".mpg", ".mpeg", ".wmv", ".flv", ".3gp"}
THUMB_EXTS = {".jpg", ".jpeg", ".png"}

TITLE_MAX       = 100
DESC_MAX_BYTES  = 5000
TAGS_MAX_CHARS  = 500      # total, per YouTube docs
THUMB_MAX_BYTES = 2 * 1024 * 1024

CHUNK_SIZE  = 8 * 1024 * 1024   # 8 MB chunks
MAX_RETRIES = 10

# Standard YouTube category IDs (videoCategories.list, region US — stable set)
CATEGORIES: dict[str, str] = {
    "Film & Animation": "1",  "Autos & Vehicles": "2",   "Music": "10",
    "Pets & Animals": "15",   "Sports": "17",            "Travel & Events": "19",
    "Gaming": "20",           "People & Blogs": "22",    "Comedy": "23",
    "Entertainment": "24",    "News & Politics": "25",   "Howto & Style": "26",
    "Education": "27",        "Science & Technology": "28", "Nonprofits & Activism": "29",
}


# ── Validation ────────────────────────────────────────────────────────────────

def sanitize_metadata(title: str, description: str, tags: list[str]) -> dict[str, Any]:
    """Clamp metadata to YouTube's hard limits. Returns cleaned values + warnings."""
    warnings: list[str] = []

    title = (title or "").replace("<", "(").replace(">", ")").strip()
    if len(title) > TITLE_MAX:
        title = title[:TITLE_MAX - 1].rstrip() + "…"
        warnings.append(f"Title truncated to {TITLE_MAX} chars")
    if not title:
        title = "Untitled video"

    description = (description or "").replace("<", "(").replace(">", ")")
    while len(description.encode("utf-8")) > DESC_MAX_BYTES:
        description = description[:-50]
    if description != (description or ""):
        pass
    if len((description or "").encode("utf-8")) > DESC_MAX_BYTES:
        warnings.append(f"Description truncated to {DESC_MAX_BYTES} bytes")

    clean_tags: list[str] = []
    total = 0
    for t in tags or []:
        t = str(t).strip().lstrip("#")
        if not t:
            continue
        # Tags containing spaces count extra (quotes) — be conservative
        cost = len(t) + (2 if " " in t else 0) + 1
        if total + cost > TAGS_MAX_CHARS:
            warnings.append("Some tags dropped (500-char total limit)")
            break
        clean_tags.append(t)
        total += cost

    return {"title": title, "description": description,
            "tags": clean_tags, "warnings": warnings}


def find_videos(folder: Path) -> list[Path]:
    from src.natsort import natkey
    try:
        return sorted((f for f in folder.iterdir() if f.suffix.lower() in VIDEO_EXTS),
                      key=lambda f: natkey(f.name))
    except OSError:
        return []


def find_thumbnails(folder: Path) -> list[Path]:
    from src.natsort import natkey
    try:
        return sorted((f for f in folder.iterdir() if f.suffix.lower() in THUMB_EXTS),
                      key=lambda f: natkey(f.name))
    except OSError:
        return []


# ── Error classification ──────────────────────────────────────────────────────
# LIMIT_MARKER is embedded in friendly messages for daily-limit conditions so
# the batch worker (src/youtube_batch.py) can classify reliably. Never shown
# to users — the batch strips it.
LIMIT_MARKER = "[[LIMIT]]"


def _error_reasons(exc: Exception) -> list[str]:
    """Extract structured error 'reason' codes from a googleapiclient HttpError.
    Far more reliable than substring-matching str(exc): Google's human message
    ('you have exceeded your quota') doesn't contain the reason token."""
    reasons: list[str] = []
    try:
        from googleapiclient.errors import HttpError
        if not isinstance(exc, HttpError):
            return reasons
        try:
            for d in (exc.error_details or []):
                if isinstance(d, dict) and d.get("reason"):
                    reasons.append(str(d["reason"]))
        except Exception:
            pass
        try:
            import json as _json
            data = _json.loads(exc.content.decode("utf-8", "replace"))
            for e in data.get("error", {}).get("errors", []):
                if e.get("reason"):
                    reasons.append(str(e["reason"]))
        except Exception:
            pass
    except ImportError:
        pass
    return reasons


_LIMIT_REASONS = {"uploadlimitexceeded", "quotaexceeded", "dailylimitexceeded",
                  "ratelimitexceeded", "userratelimitexceeded"}
_LIMIT_PHRASES = ("exceeded your quota", "exceeded the number of videos",
                  "upload limit", "uploadlimitexceeded", "quotaexceeded",
                  "dailylimitexceeded")


def _friendly_api_error(exc: Exception) -> str:
    s = str(exc)
    sl = s.lower()
    reasons = [r.lower() for r in _error_reasons(exc)]

    if "uploadlimitexceeded" in reasons or "upload limit" in sl \
            or "exceeded the number of videos" in sl:
        return (f"{LIMIT_MARKER} YouTube says this channel has reached its "
                "upload limit for the last 24 hours. Remaining videos stay "
                f"queued and resume automatically ({quota.time_until_reset_str()}).")
    if any(r in _LIMIT_REASONS for r in reasons) \
            or any(p in sl for p in _LIMIT_PHRASES):
        return (f"{LIMIT_MARKER} YouTube API daily quota exhausted. It resets "
                f"at midnight Pacific ({quota.time_until_reset_str()}).")
    if "youtubesignuprequired" in reasons or "youtubeSignupRequired" in s:
        return ("This Google account has no YouTube channel. Open youtube.com "
                "once with this account and create the channel, then retry.")
    if "authError" in s or "invalid credentials" in sl or "invalid_grant" in sl:
        return "YouTube session expired — click 'Connect YouTube account' again."
    if reasons:
        return f"{s} [reason: {','.join(reasons)}]"
    return s


def _is_retryable_http(exc: Exception) -> bool:
    try:
        from googleapiclient.errors import HttpError
        if isinstance(exc, HttpError):
            return exc.resp.status in (500, 502, 503, 504)
    except ImportError:
        pass
    msg = str(exc).lower()
    return any(k in msg for k in ("timed out", "timeout", "connection reset",
                                  "broken pipe", "ssl", "temporarily"))


# ── Playlists ─────────────────────────────────────────────────────────────────

def list_playlists() -> list[dict[str, str]]:
    """Return the signed-in channel's playlists [{id, title, count}]. ~1-2 units."""
    yt = get_service()
    out: list[dict[str, str]] = []
    token = None
    while True:
        resp = yt.playlists().list(
            part="snippet,contentDetails", mine=True, maxResults=50,
            pageToken=token,
        ).execute()
        quota.record("playlists.list")
        for it in resp.get("items", []):
            out.append({
                "id":    it["id"],
                "title": it["snippet"]["title"],
                "count": str(it.get("contentDetails", {}).get("itemCount", "")),
            })
        token = resp.get("nextPageToken")
        if not token:
            break
    return out


def ensure_playlist(title: str, privacy: str = "unlisted") -> str:
    """Return the ID of the channel's playlist with this title, creating it
    only if it doesn't exist. Idempotent — safe to call once per video."""
    want = title.strip().lower()
    try:
        for p in list_playlists():
            if p["title"].strip().lower() == want:
                return p["id"]
    except Exception as e:
        print(f"  [yt] playlist lookup failed ({e}) — will create")
    return create_playlist(title, privacy)


def create_playlist(title: str, privacy: str = "unlisted",
                    description: str = "") -> str:
    """Create a playlist, return its ID. 50 units."""
    yt = get_service()
    resp = yt.playlists().insert(
        part="snippet,status",
        body={
            "snippet": {"title": title[:150], "description": description[:5000]},
            "status":  {"privacyStatus": privacy},
        },
    ).execute()
    quota.record("playlists.insert", note=title)
    return resp["id"]


def _add_to_playlist(yt, playlist_id: str, video_id: str) -> None:
    yt.playlistItems().insert(
        part="snippet",
        body={"snippet": {
            "playlistId": playlist_id,
            "resourceId": {"kind": "youtube#video", "videoId": video_id},
        }},
    ).execute()
    quota.record("playlistItems.insert", note=video_id)


# ── Core upload ───────────────────────────────────────────────────────────────

def _upload_one(yt, video_path: Path, meta: dict[str, Any],
                step: int) -> str:
    """Resumable upload of one video. Returns the new video ID."""
    from googleapiclient.http import MediaFileUpload

    clean = sanitize_metadata(meta.get("title", video_path.stem),
                              meta.get("description", ""),
                              meta.get("tags", []))

    body: dict[str, Any] = {
        "snippet": {
            "title":       clean["title"],
            "description": clean["description"],
            "tags":        clean["tags"],
            "categoryId":  str(meta.get("category_id", "22")),
        },
        "status": {
            "privacyStatus":            meta.get("privacy", "unlisted"),
            "selfDeclaredMadeForKids":  bool(meta.get("made_for_kids", False)),
        },
    }
    # Scheduled publish: YouTube requires privacyStatus=private + publishAt
    publish_at = meta.get("publish_at")  # ISO-8601 string or None
    if publish_at:
        body["status"]["privacyStatus"] = "private"
        body["status"]["publishAt"]     = publish_at

    mime = mimetypes.guess_type(str(video_path))[0] or "video/*"
    media = MediaFileUpload(str(video_path), mimetype=mime,
                            chunksize=CHUNK_SIZE, resumable=True)

    size_mb = video_path.stat().st_size / (1024 * 1024)
    request = yt.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
        notifySubscribers=bool(meta.get("notify_subscribers", True)),
    )

    response = None
    retries = 0
    started = time.time()
    while response is None:
        try:
            status, response = request.next_chunk()
            retries = 0
            if status:
                pct = int(status.progress() * 100)
                elapsed = int(time.time() - started)
                progress.update(NS, step, "active",
                                f"Uploading {video_path.name} — {pct}% of "
                                f"{size_mb:.0f} MB ({elapsed}s)")
        except Exception as exc:
            if _is_retryable_http(exc) and retries < MAX_RETRIES:
                retries += 1
                sleep = min(60, (2 ** retries) + random.random())
                progress.update(NS, step, "active",
                                f"Transient error — retry {retries}/{MAX_RETRIES} "
                                f"in {sleep:.0f}s…")
                time.sleep(sleep)
                continue
            # Google charges quota for FAILED insert attempts too — record it
            # so the local meter never silently drifts below Google's real one.
            quota.record("videos.insert", success=False,
                         note=f"FAILED attempt: {video_path.name}")
            raise

    quota.record("videos.insert", note=clean["title"])
    return response["id"]


def _set_thumbnail(yt, video_id: str, thumb_path: Path, step: int) -> bool:
    """Set a custom thumbnail. Non-fatal on failure. Returns success."""
    from googleapiclient.http import MediaFileUpload
    try:
        if thumb_path.stat().st_size > THUMB_MAX_BYTES:
            # Recompress to fit YouTube's 2 MB thumbnail limit
            thumb_path = _shrink_thumbnail(thumb_path)
        yt.thumbnails().set(
            videoId=video_id,
            media_body=MediaFileUpload(str(thumb_path)),
        ).execute()
        quota.record("thumbnails.set", note=video_id)
        return True
    except Exception as e:
        detail = str(e)
        if "403" in detail or "forbidden" in detail.lower():
            detail = ("channel not verified for custom thumbnails — verify "
                      "your phone at youtube.com/verify")
        progress.update(NS, step, "active", f"Thumbnail skipped: {detail[:100]}")
        return False


def _shrink_thumbnail(path: Path) -> Path:
    from PIL import Image
    import io
    img = Image.open(path).convert("RGB")
    img.thumbnail((1280, 720))
    out = path.parent / f".thumb_{path.stem}.jpg"
    q = 90
    while q >= 40:
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=q, optimize=True)
        if buf.tell() <= THUMB_MAX_BYTES:
            out.write_bytes(buf.getvalue())
            return out
        q -= 10
    out.write_bytes(buf.getvalue())
    return out


# ── Public API — batch publish ────────────────────────────────────────────────

def publish_to_youtube(items: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Publish a batch of videos sequentially.

    Each item: {
      "path": Path, "title", "description", "tags": list,
      "category_id", "privacy", "made_for_kids", "publish_at" (ISO or None),
      "playlist_id" (or ""), "new_playlist_title" (or ""),
      "thumbnail": Path or None, "notify_subscribers": bool,
    }

    Quota/upload-cap pre-flight runs here again (defence in depth — the UI
    also checks before enabling the button).
    """
    n = len(items)
    labels: list[str] = ["Pre-flight checks"]
    for i, it in enumerate(items):
        labels.append(f"Upload {i+1}/{n}: {Path(it['path']).name}")
        labels.append(f"Finalize {i+1}/{n} (thumbnail / playlist)")
    progress.start(NS, labels)

    results: list[dict[str, Any]] = []
    try:
        # ── Step 0: pre-flight ───────────────────────────────────────────────
        progress.update(NS, 0, "active", "Checking quota, upload cap, files…")
        for it in items:
            p = Path(it["path"])
            if not p.exists():
                raise FileNotFoundError(f"Video file missing: {p}")

        n_thumbs    = sum(1 for it in items if it.get("thumbnail"))
        n_pl_adds   = sum(1 for it in items
                          if it.get("playlist_id") or it.get("new_playlist_title"))
        n_new_pl    = len({it["new_playlist_title"] for it in items
                           if it.get("new_playlist_title")})
        cost = quota.estimate_cost(n, n_thumbs, n_pl_adds, n_new_pl)
        ok, reason = quota.can_publish(n, cost)
        if not ok:
            raise RuntimeError(reason)

        yt = get_service()
        progress.update(NS, 0, "done",
                        f"OK — {n} video(s), ~{cost} quota units planned")

        # Create any new playlists once, remember their IDs
        new_playlist_ids: dict[str, str] = {}

        # ── Per-video: upload → finalize ─────────────────────────────────────
        for i, it in enumerate(items):
            up_step, fin_step = 1 + i * 2, 2 + i * 2
            path = Path(it["path"])

            progress.update(NS, up_step, "active", f"Starting {path.name}…")
            video_id = _upload_one(yt, path, it, up_step)
            url = f"https://youtu.be/{video_id}"
            sched = f" (scheduled {it['publish_at']})" if it.get("publish_at") else ""
            progress.update(NS, up_step, "done", f"{url}{sched}")

            progress.update(NS, fin_step, "active", "Finalizing…")
            notes = []

            thumb = it.get("thumbnail")
            if thumb:
                if _set_thumbnail(yt, video_id, Path(thumb), fin_step):
                    notes.append("thumbnail ✓")
                else:
                    notes.append("thumbnail skipped")

            pl_id = it.get("playlist_id") or ""
            new_pl = (it.get("new_playlist_title") or "").strip()
            if new_pl and not pl_id:
                if new_pl not in new_playlist_ids:
                    # ensure_playlist reuses an existing playlist of the same
                    # name — repeated single-item calls can't create duplicates
                    new_playlist_ids[new_pl] = ensure_playlist(
                        new_pl, privacy=it.get("privacy", "unlisted")
                        if it.get("privacy") != "private" else "unlisted")
                    notes.append(f"playlist '{new_pl}' ready")
                pl_id = new_playlist_ids[new_pl]
            if pl_id:
                try:
                    _add_to_playlist(yt, pl_id, video_id)
                    notes.append("added to playlist ✓")
                except Exception as e:
                    notes.append(f"playlist add failed: {str(e)[:60]}")

            progress.update(NS, fin_step, "done", ", ".join(notes) or "Done")
            results.append({
                "video_id": video_id, "url": url,
                "title": it.get("title", path.stem),
                "privacy": "scheduled" if it.get("publish_at")
                           else it.get("privacy", "unlisted"),
            })

        result = {"success": True, "results": results, "count": len(results)}
        progress.finish(NS, result)
        return result

    except Exception as exc:
        err = _friendly_api_error(exc)
        state = progress.read(NS)
        for idx, s in enumerate(state.get("steps", [])):
            if s["status"] in ("active", "pending"):
                progress.update(NS, idx, "error", err[:140])
        progress.fail(NS, err)
        # Report partial success so already-uploaded videos aren't lost
        return {"success": False, "error": err, "results": results}
