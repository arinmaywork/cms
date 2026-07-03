"""
src/youtube_auth.py
─────────────────────────────────────────────────────────────────────────────
YouTube OAuth via the *device code* flow (OAuth 2.0 for TV & Limited-Input
Devices).  Chosen deliberately:

  • Works identically on a laptop AND on a headless Oracle VM — the app shows
    a short code, you approve it at google.com/device from ANY phone/browser.
  • No localhost redirect needed (Google requires HTTPS for non-localhost
    redirect URIs, which breaks classic flows on remote VMs).
  • The `https://www.googleapis.com/auth/youtube` scope is on Google's
    allowed-scopes list for device flow and covers videos.insert,
    thumbnails.set, playlists.* and playlistItems.* — everything we need.

Setup (one time, see YOUTUBE_SETUP.md):
  1. Google Cloud Console → enable "YouTube Data API v3"
  2. Create OAuth client of type **"TVs and Limited Input devices"**
  3. Download the client secret JSON → save as .secrets/youtube_client_secret.json

Token is persisted to .secrets/youtube_token.json and auto-refreshed.
"""

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import requests

ROOT         = Path(__file__).resolve().parent.parent
SECRETS_DIR  = ROOT / ".secrets"
TOKEN_FILE   = SECRETS_DIR / "youtube_token.json"
AUTH_STATUS_FILE = ROOT / ".queue" / "yt_auth_status.json"

SCOPES = ["https://www.googleapis.com/auth/youtube"]

DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"
TOKEN_URL       = "https://oauth2.googleapis.com/token"


# ── Client secret ─────────────────────────────────────────────────────────────

def client_secret_path() -> Path:
    custom = os.getenv("YOUTUBE_CLIENT_SECRET_FILE", "")
    return Path(custom) if custom else SECRETS_DIR / "youtube_client_secret.json"


def has_client_secret() -> bool:
    return client_secret_path().exists()


def load_client_config() -> tuple[str, str]:
    """Return (client_id, client_secret) from the downloaded JSON."""
    p = client_secret_path()
    if not p.exists():
        raise FileNotFoundError(
            f"YouTube client secret not found at {p}.\n"
            "Follow YOUTUBE_SETUP.md to create an OAuth client of type "
            "'TVs and Limited Input devices' and save the JSON there."
        )
    data = json.loads(p.read_text(encoding="utf-8"))
    # Google wraps the config under "installed" (TV clients use this too)
    for key in ("installed", "web"):
        if key in data:
            data = data[key]
            break
    cid, csec = data.get("client_id", ""), data.get("client_secret", "")
    if not cid or not csec:
        raise ValueError(f"client_id/client_secret missing in {p}")
    return cid, csec


# ── Token persistence ─────────────────────────────────────────────────────────

def has_token() -> bool:
    return TOKEN_FILE.exists()


def _save_token(payload: dict[str, Any], client_id: str, client_secret: str) -> None:
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    info = {
        "token":         payload.get("access_token"),
        "refresh_token": payload.get("refresh_token"),
        "token_uri":     TOKEN_URL,
        "client_id":     client_id,
        "client_secret": client_secret,
        "scopes":        SCOPES,
        "expiry_epoch":  time.time() + float(payload.get("expires_in", 3600)),
    }
    TOKEN_FILE.write_text(json.dumps(info, indent=2), encoding="utf-8")
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        pass


def sign_out() -> None:
    """Revoke and delete the stored token."""
    try:
        info = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
        tok = info.get("refresh_token") or info.get("token")
        if tok:
            requests.post("https://oauth2.googleapis.com/revoke",
                          params={"token": tok}, timeout=10)
    except Exception:
        pass
    TOKEN_FILE.unlink(missing_ok=True)


def get_credentials():
    """Return refreshed google.oauth2 Credentials, or None if not signed in."""
    if not TOKEN_FILE.exists():
        return None
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    info = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    creds = Credentials(
        token=info.get("token"),
        refresh_token=info.get("refresh_token"),
        token_uri=info.get("token_uri", TOKEN_URL),
        client_id=info.get("client_id"),
        client_secret=info.get("client_secret"),
        scopes=info.get("scopes", SCOPES),
    )
    # Refresh proactively if near/past expiry
    if info.get("expiry_epoch", 0) < time.time() + 120 and creds.refresh_token:
        try:
            creds.refresh(Request())
            info["token"]        = creds.token
            info["expiry_epoch"] = time.time() + 3500
            TOKEN_FILE.write_text(json.dumps(info, indent=2), encoding="utf-8")
        except Exception as e:
            # Refresh token revoked/expired → force re-auth
            print(f"  [yt:auth] token refresh failed: {e}")
            return None
    return creds


def get_service():
    """Return an authorised YouTube Data API v3 service, or raise."""
    creds = get_credentials()
    if creds is None:
        raise RuntimeError(
            "Not signed in to YouTube. Open the ▶️ YouTube tab and click "
            "'Connect YouTube account'."
        )
    from googleapiclient.discovery import build
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


# ── Device flow ───────────────────────────────────────────────────────────────

def _write_auth_status(status: str, **extra) -> None:
    AUTH_STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    AUTH_STATUS_FILE.write_text(
        json.dumps({"status": status, "ts": time.time(), **extra}),
        encoding="utf-8",
    )


def read_auth_status() -> dict[str, Any]:
    if not AUTH_STATUS_FILE.exists():
        return {"status": "idle"}
    try:
        return json.loads(AUTH_STATUS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"status": "idle"}


def clear_auth_status() -> None:
    AUTH_STATUS_FILE.unlink(missing_ok=True)


def start_device_flow() -> dict[str, Any]:
    """
    Step 1: request device + user codes.
    Returns {"user_code", "verification_url", "device_code", "interval", "expires_in"}
    and spawns a background thread that polls Google until the user approves.
    Poll outcome is written to .queue/yt_auth_status.json
    ("pending" → "granted" | "denied" | "expired" | "error").
    """
    client_id, client_secret = load_client_config()
    resp = requests.post(
        DEVICE_CODE_URL,
        data={"client_id": client_id, "scope": " ".join(SCOPES)},
        timeout=15,
    )
    data = resp.json()
    if "error" in data or "device_code" not in data:
        raise RuntimeError(
            f"Device-code request failed: {data.get('error', data)}.\n"
            "Make sure the OAuth client type is 'TVs and Limited Input devices'."
        )

    _write_auth_status("pending",
                       user_code=data["user_code"],
                       verification_url=data["verification_url"])

    t = threading.Thread(
        target=_poll_for_token,
        args=(client_id, client_secret, data["device_code"],
              int(data.get("interval", 5)), int(data.get("expires_in", 1800))),
        daemon=True, name="yt-device-poll",
    )
    t.start()
    return data


def _poll_for_token(client_id: str, client_secret: str, device_code: str,
                    interval: int, expires_in: int) -> None:
    deadline = time.time() + expires_in
    wait = max(interval, 5)
    while time.time() < deadline:
        time.sleep(wait)
        try:
            resp = requests.post(TOKEN_URL, data={
                "client_id":     client_id,
                "client_secret": client_secret,
                "device_code":   device_code,
                "grant_type":    "urn:ietf:params:oauth:grant-type:device_code",
            }, timeout=15)
            data = resp.json()
        except Exception as e:
            print(f"  [yt:auth] poll error: {e}")
            continue

        if "access_token" in data:
            _save_token(data, client_id, client_secret)
            _write_auth_status("granted")
            print("  [yt:auth] ✅ YouTube authorised")
            return
        err = data.get("error", "")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            wait += 5
            continue
        if err == "access_denied":
            _write_auth_status("denied")
            return
        if err in ("expired_token", "invalid_grant"):
            _write_auth_status("expired")
            return
        _write_auth_status("error", error=str(data))
        return
    _write_auth_status("expired")


# ── Channel info (1 quota unit) ───────────────────────────────────────────────

def channel_info() -> dict[str, Any] | None:
    """Return {"title", "id", "thumbnail"} for the signed-in channel, or None."""
    try:
        yt = get_service()
        resp = yt.channels().list(part="snippet", mine=True).execute()
        items = resp.get("items", [])
        if not items:
            return None
        sn = items[0]["snippet"]
        return {
            "id":        items[0]["id"],
            "title":     sn.get("title", ""),
            "thumbnail": sn.get("thumbnails", {}).get("default", {}).get("url", ""),
        }
    except Exception as e:
        print(f"  [yt:auth] channel_info failed: {e}")
        return None
