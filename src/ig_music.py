"""
src/ig_music.py — Instagram licensed music for Reels (Meta Audio API, June 2026).

Only works via the Instagram API *with Facebook Login*, so it uses its own
credentials, separate from the normal Instagram-Login token:
  IG_FB_TOKEN    long-lived Facebook User token (instagram_basic, instagram_content_publish)
  IG_FB_USER_ID  Instagram Business Account ID (auto-discovered if empty)
See MUSIC_SETUP.md.

A chosen track is stored as {"audio_id", "title", "artist", "audio_volume", "video_volume"}.
"""

import json
import os
from typing import Any

import requests

FB_BASE = "https://graph.facebook.com/v25.0"


def _token() -> str:
    return os.getenv("IG_FB_TOKEN", "").strip()


def configured() -> bool:
    return bool(_token())


def _get(path: str, params: dict) -> dict:
    r = requests.get(f"{FB_BASE}/{path}", params=params,
                     headers={"Authorization": f"Bearer {_token()}"}, timeout=30)
    data = r.json()
    if "error" in data:
        e = data["error"]
        raise RuntimeError(f"Graph API error: code {e.get('code')} — {e.get('message')}")
    return data


def user_id() -> str:
    uid = os.getenv("IG_FB_USER_ID", "").strip()
    if uid:
        return uid
    pages = _get("me/accounts", {"fields": "instagram_business_account"}).get("data", [])
    for p in pages:
        if p.get("instagram_business_account", {}).get("id"):
            uid = p["instagram_business_account"]["id"]
            os.environ["IG_FB_USER_ID"] = uid
            return uid
    raise RuntimeError("No Instagram Business account linked to your Facebook Page(s).")


def creds() -> dict[str, str]:
    if not configured():
        raise EnvironmentError("Music needs IG_FB_TOKEN in .env — see MUSIC_SETUP.md")
    return {"access_token": _token(), "ig_user_id": user_id()}


def search(query: str = "") -> list[dict[str, Any]]:
    """Music Meta has licensed for third-party posting. Empty query → trending."""
    p = {"audio_type": "music", "user_id": user_id()}
    if query.strip():
        p["search_query"] = query.strip()
    out = []
    for a in _get("ig_audio", p).get("audio", []):
        out.append({"audio_id": a["audio_id"], "title": a.get("title", ""),
                    "artist": a.get("display_artist", ""),
                    "seconds": round((a.get("duration_in_ms") or 0) / 1000),
                    "preview": a.get("download_url") or "",
                    "link": a.get("on_platform_audio_preview_link") or "",
                    "art": a.get("cover_artwork_thumbnail_uri") or ""})
    return out


def audio_config(m: dict) -> str:
    return json.dumps({"audio_id": str(m["audio_id"]),
                       "audio_volume": int(m.get("audio_volume", 100)),
                       "video_volume": int(m.get("video_volume", 0))})


def label(m: dict | None) -> str:
    return f"🎵 {m.get('title', '')} — {m.get('artist', '')}" if m else ""
