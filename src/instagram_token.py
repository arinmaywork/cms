"""
src/instagram_token.py — automatic Instagram long-lived token refresh.

Runs from the launcher's background loop (never in the UI render path).
  • refreshes at most every 7 days (rolls the 60-day window forward forever)
  • after a FAILED attempt, backs off 6 h — a revoked token no longer causes
    an HTTP call every few seconds
  • rewrites .env atomically (a crash can't truncate your credentials)

State: .queue/ig_token_meta.json
  {last_refresh, expires_at, last_attempt, last_status, ok}
"""

import os
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path

import requests

from src.fsutil import atomic_write_json, atomic_write_text, load_json

ROOT      = Path(__file__).resolve().parent.parent
META_FILE = ROOT / ".queue" / "ig_token_meta.json"
ENV_FILE  = ROOT / ".env"

REFRESH_EVERY = timedelta(days=7)
FAIL_BACKOFF  = timedelta(hours=6)
_lock = threading.Lock()


def _meta() -> dict:
    m = load_json(META_FILE, {})
    return m if isinstance(m, dict) else {}


def _ago(iso: str | None) -> timedelta | None:
    try:
        return datetime.now() - datetime.fromisoformat(iso) if iso else None
    except ValueError:
        return None


def _update_env_token(new_token: str) -> None:
    text = ENV_FILE.read_text(encoding="utf-8") if ENV_FILE.exists() else ""
    if re.search(r"^INSTAGRAM_ACCESS_TOKEN=", text, flags=re.M):
        text = re.sub(r"^INSTAGRAM_ACCESS_TOKEN=.*$",
                      lambda _: f"INSTAGRAM_ACCESS_TOKEN={new_token}", text, flags=re.M)
    else:
        text += f"\nINSTAGRAM_ACCESS_TOKEN={new_token}\n"
    atomic_write_text(ENV_FILE, text, mode=0o600)
    os.environ["INSTAGRAM_ACCESS_TOKEN"] = new_token


def _current_token() -> str:
    """Read the token from .env directly — picks up a manually pasted new one."""
    try:
        m = re.search(r"^INSTAGRAM_ACCESS_TOKEN=(.*)$",
                      ENV_FILE.read_text(encoding="utf-8"), flags=re.M)
        if m and m.group(1).strip():
            return m.group(1).strip()
    except OSError:
        pass
    return os.getenv("INSTAGRAM_ACCESS_TOKEN", "")


def status() -> dict:
    """For the UI: {configured, ok, days_left, last_status}. File read only."""
    m = _meta()
    days_left = None
    try:
        if m.get("expires_at"):
            days_left = max(0, (datetime.fromisoformat(m["expires_at"]) - datetime.now()).days)
    except ValueError:
        pass
    return {"configured": bool(_current_token()), "ok": m.get("ok", True),
            "days_left": days_left, "last_status": m.get("last_status", "not checked yet")}


def refresh_if_due(force: bool = False) -> str:
    token = _current_token()
    if not token:
        return "no token configured"
    with _lock:
        m = _meta()
        # A new token pasted by hand → forget the old failure state
        if m.get("token_tail") and m["token_tail"] != token[-12:]:
            m = {}
        if not force:
            age = _ago(m.get("last_refresh"))
            if m.get("ok", True) and age is not None and age < REFRESH_EVERY:
                return m.get("last_status", "fresh")
            tried = _ago(m.get("last_attempt"))
            if not m.get("ok", True) and tried is not None and tried < FAIL_BACKOFF:
                return m.get("last_status", "backing off")

        now = datetime.now()
        m["last_attempt"], m["token_tail"] = now.isoformat(), token[-12:]
        try:
            data = requests.get("https://graph.instagram.com/refresh_access_token",
                                params={"grant_type": "ig_refresh_token",
                                        "access_token": token}, timeout=20).json()
        except Exception as e:
            m.update(ok=False, last_status=f"network error: {str(e)[:80]}")
            atomic_write_json(META_FILE, m)
            return m["last_status"]

        if "access_token" in data:
            secs = int(data.get("expires_in", 60 * 86400))
            _update_env_token(data["access_token"])
            m.update(ok=True, last_refresh=now.isoformat(),
                     expires_at=(now + timedelta(seconds=secs)).isoformat(),
                     token_tail=data["access_token"][-12:],
                     last_status=f"refreshed ✓ (valid {secs // 86400}d)")
            print(f"  [ig:token] {m['last_status']}")
        else:
            err = data.get("error", {})
            msg = err.get("message", str(data)) if isinstance(err, dict) else str(err)
            if "24 hours" in msg or "too soon" in msg.lower():
                m.update(ok=True, last_refresh=now.isoformat(),
                         last_status="token too new — retry next week")
            else:
                m.update(ok=False, last_status=f"refresh failed: {msg[:140]}")
                print(f"  [ig:token] {m['last_status']}")
        atomic_write_json(META_FILE, m)
        return m["last_status"]
