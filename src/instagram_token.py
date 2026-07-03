"""
src/instagram_token.py
─────────────────────────────────────────────────────────────────────────────
Automatic Instagram long-lived token refresh.

Instagram Login tokens (IGAA…) live 60 days but can be refreshed any time
after they're 24h old via:
    GET https://graph.instagram.com/refresh_access_token
        ?grant_type=ig_refresh_token&access_token=<current>

This module refreshes the token once a week (rolling the 60-day window
forward forever) and writes the new token back into .env — so you never
have to touch the Meta developer portal again unless the token is revoked.

State: .queue/ig_token_meta.json {"last_refresh": iso, "expires_at": iso}
Called opportunistically from app.py on page load (cheap: no-op unless due).
"""

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

ROOT       = Path(__file__).resolve().parent.parent
META_FILE  = ROOT / ".queue" / "ig_token_meta.json"
ENV_FILE   = ROOT / ".env"

REFRESH_EVERY_DAYS = 7
_lock = threading.Lock()


def _load_meta() -> dict:
    if not META_FILE.exists():
        return {}
    try:
        return json.loads(META_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_meta(meta: dict) -> None:
    META_FILE.parent.mkdir(parents=True, exist_ok=True)
    META_FILE.write_text(json.dumps(meta, indent=2), encoding="utf-8")


def _update_env_token(new_token: str) -> None:
    """Rewrite INSTAGRAM_ACCESS_TOKEN in .env, preserving everything else."""
    if not ENV_FILE.exists():
        return
    text = ENV_FILE.read_text(encoding="utf-8")
    if re.search(r"^INSTAGRAM_ACCESS_TOKEN=", text, flags=re.M):
        text = re.sub(r"^INSTAGRAM_ACCESS_TOKEN=.*$",
                      f"INSTAGRAM_ACCESS_TOKEN={new_token}",
                      text, flags=re.M)
    else:
        text += f"\nINSTAGRAM_ACCESS_TOKEN={new_token}\n"
    ENV_FILE.write_text(text, encoding="utf-8")
    os.environ["INSTAGRAM_ACCESS_TOKEN"] = new_token


def status() -> dict:
    """For the sidebar: {"days_left": int|None, "last_refresh": str|None}"""
    meta = _load_meta()
    days_left = None
    if meta.get("expires_at"):
        try:
            exp = datetime.fromisoformat(meta["expires_at"])
            days_left = max(0, (exp - datetime.now()).days)
        except Exception:
            pass
    return {"days_left": days_left, "last_refresh": meta.get("last_refresh")}


def refresh_if_due(force: bool = False) -> str:
    """
    Refresh the IG token if the last refresh is older than REFRESH_EVERY_DAYS.
    Returns a short human-readable status string. Never raises.
    """
    token = os.getenv("INSTAGRAM_ACCESS_TOKEN", "")
    if not token:
        return "no token configured"

    with _lock:
        meta = _load_meta()
        last = meta.get("last_refresh")
        if last and not force:
            try:
                age = datetime.now() - datetime.fromisoformat(last)
                if age < timedelta(days=REFRESH_EVERY_DAYS):
                    return f"fresh (refreshed {age.days}d ago)"
            except Exception:
                pass

        try:
            resp = requests.get(
                "https://graph.instagram.com/refresh_access_token",
                params={"grant_type": "ig_refresh_token", "access_token": token},
                timeout=20,
            )
            data = resp.json()
        except Exception as e:
            return f"refresh failed (network): {e}"

        if "access_token" not in data:
            err = data.get("error", {})
            msg = err.get("message", str(data)) if isinstance(err, dict) else str(err)
            # A token younger than 24h can't be refreshed yet — that's fine
            if "24 hours" in msg or "too soon" in msg.lower():
                _save_meta({"last_refresh": datetime.now().isoformat(),
                            "expires_at": (datetime.now() + timedelta(days=60)).isoformat()})
                return "token too new to refresh — will retry next week"
            return f"refresh failed: {msg[:120]}"

        new_token  = data["access_token"]
        expires_in = int(data.get("expires_in", 60 * 24 * 3600))
        _update_env_token(new_token)
        _save_meta({
            "last_refresh": datetime.now().isoformat(),
            "expires_at":   (datetime.now() + timedelta(seconds=expires_in)).isoformat(),
        })
        print(f"  [ig:token] refreshed — valid {expires_in // 86400} more days")
        return f"refreshed ✓ (valid {expires_in // 86400}d)"
