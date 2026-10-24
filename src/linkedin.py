"""
src/linkedin.py — post to your LinkedIn profile (Posts API + Images API).

Auth: OAuth 2.0 authorization code with a "paste the redirect URL" step, so it
works from any device even though the CMS runs on a private Tailscale address.
Redirect URI (register it in the LinkedIn app): LINKEDIN_REDIRECT_URI, default
http://localhost:8765/linkedin-callback — the page won't load; you just copy
its address back into Settings. Scopes: openid profile w_member_social.
Standard apps get 60-day tokens (no refresh) → status shows days left.

Posts: text, 1 image, or 2–20 images (multiImage). Commentary is converted to
LinkedIn "little text" (reserved chars escaped, #tags → real hashtags).
"""

import datetime
import io
import os
import re
import secrets
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from PIL import Image, ImageOps

import src.progress as progress
from src.fsutil import atomic_write_json, load_json

ROOT = Path(__file__).resolve().parent.parent
TOKEN_FILE = ROOT / ".secrets" / "linkedin_token.json"
STATE_FILE = ROOT / ".queue" / "li_oauth_state.json"
SCOPES = "openid profile w_member_social"
NS, MAX_CHARS, MAX_IMAGES = "li", 3000, 20
API = "https://api.linkedin.com/rest"


def _redirect() -> str:
    return os.getenv("LINKEDIN_REDIRECT_URI", "http://localhost:8765/linkedin-callback")


def configured() -> bool:
    return bool(os.getenv("LINKEDIN_CLIENT_ID") and os.getenv("LINKEDIN_CLIENT_SECRET"))


def auth_url() -> str:
    state = secrets.token_urlsafe(16)
    atomic_write_json(STATE_FILE, {"state": state, "ts": time.time()})
    return "https://www.linkedin.com/oauth/v2/authorization?" + urlencode({
        "response_type": "code", "client_id": os.getenv("LINKEDIN_CLIENT_ID", ""),
        "redirect_uri": _redirect(), "state": state, "scope": SCOPES})


def finish_auth(pasted: str) -> tuple[bool, str]:
    pasted = pasted.strip()
    q = parse_qs(urlparse(pasted).query) if "?" in pasted else {}
    if q.get("error"):
        return False, q.get("error_description", q["error"])[0]
    code = q.get("code", [pasted])[0]
    if q.get("state") and q["state"][0] != load_json(STATE_FILE, {}).get("state"):
        return False, "Security check failed (state mismatch) — click Generate login link again."
    try:
        r = requests.post("https://www.linkedin.com/oauth/v2/accessToken", timeout=20, data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": _redirect(),
            "client_id": os.getenv("LINKEDIN_CLIENT_ID", ""),
            "client_secret": os.getenv("LINKEDIN_CLIENT_SECRET", "")}).json()
        if "access_token" not in r:
            return False, f"LinkedIn: {r.get('error_description', r)}"
        me = requests.get("https://api.linkedin.com/v2/userinfo", timeout=20,
                          headers={"Authorization": f"Bearer {r['access_token']}"}).json()
    except Exception as e:
        return False, f"Network error: {e}"
    if "sub" not in me:
        return False, f"Couldn't read your profile (is 'Sign In with LinkedIn using OpenID Connect' added?): {me}"
    atomic_write_json(TOKEN_FILE, {
        "access_token": r["access_token"], "refresh_token": r.get("refresh_token"),
        "expires_at": time.time() + int(r.get("expires_in", 5_184_000)),
        "urn": f"urn:li:person:{me['sub']}", "name": me.get("name", "")}, mode=0o600)
    STATE_FILE.unlink(missing_ok=True)
    return True, f"Connected as {me.get('name', '')} ✓"


def status() -> dict:
    t = load_json(TOKEN_FILE, {})
    if not isinstance(t, dict) or not t.get("access_token"):
        return {"connected": False}
    days = int((t.get("expires_at", 0) - time.time()) // 86400)
    return {"connected": days >= 0, "name": t.get("name", ""), "days_left": max(0, days)}


def sign_out() -> None:
    TOKEN_FILE.unlink(missing_ok=True)


def _version() -> str:
    # Versions are monthly and sunset after ~1 year; ~2 months back is always live.
    d = datetime.date.today().replace(day=1) - datetime.timedelta(days=45)
    return os.getenv("LINKEDIN_VERSION", d.strftime("%Y%m"))


def _headers(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}", "LinkedIn-Version": _version(),
            "X-Restli-Protocol-Version": "2.0.0", "Content-Type": "application/json"}


def _check(r: requests.Response) -> None:
    if r.status_code >= 300:
        try:
            msg = r.json().get("message", r.text[:300])
        except Exception:
            msg = r.text[:300]
        if r.status_code == 401:
            msg += " — reconnect LinkedIn in ⚙️ Settings"
        raise RuntimeError(f"LinkedIn {r.status_code}: {msg}")


_RESERVED = re.compile(r"([\\|{}@\[\]()<>#*_~])")


def to_little_text(text: str) -> str:
    tags: list[str] = []

    def keep(m):
        tags.append(m.group(1))
        return f"\x00{len(tags) - 1}\x00"

    t = _RESERVED.sub(r"\\\1", re.sub(r"#(\w+)", keep, text))
    return re.sub(r"\x00(\d+)\x00",
                  lambda m: "{hashtag|\\#|" + tags[int(m.group(1))] + "}", t)


def _jpeg(path: Path) -> bytes:
    img = ImageOps.exif_transpose(Image.open(path))
    icc = img.info.get("icc_profile")
    img = img.convert("RGB")
    img.thumbnail((4096, 4096), Image.LANCZOS)        # well under LinkedIn's 36 MP cap
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=92, **({"icc_profile": icc} if icc else {}))
    return buf.getvalue()


def _upload_image(tok: str, urn: str, path: Path) -> str:
    r = requests.post(f"{API}/images?action=initializeUpload", headers=_headers(tok),
                      json={"initializeUploadRequest": {"owner": urn}}, timeout=30)
    _check(r)
    v = r.json()["value"]
    put = requests.put(v["uploadUrl"], data=_jpeg(path), timeout=300,
                       headers={"Authorization": f"Bearer {tok}"})
    if put.status_code >= 300:
        raise RuntimeError(f"Image upload failed ({path.name}): HTTP {put.status_code}")
    return v["image"]


def publish(text: str, images: list[Path], alts: list[str]) -> dict:
    progress.start(NS, [f"Upload image {i + 1}/{len(images)}" for i in range(len(images))]
                   + ["Create post"])
    try:
        t = load_json(TOKEN_FILE, {})
        if not t.get("access_token") or t.get("expires_at", 0) < time.time():
            raise RuntimeError("LinkedIn not connected or token expired — reconnect in ⚙️ Settings.")
        tok, urn = t["access_token"], t["urn"]
        ids = []
        for i, p in enumerate(images):
            progress.update(NS, i, "active", p.name)
            ids.append(_upload_image(tok, urn, p))
            progress.update(NS, i, "done", "✓")
        body = {"author": urn, "commentary": to_little_text(text), "visibility": "PUBLIC",
                "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [],
                                 "thirdPartyDistributionChannels": []},
                "lifecycleState": "PUBLISHED", "isReshareDisabledByAuthor": False}
        alt = lambda i: (alts[i] if i < len(alts) else "")[:4000]
        if len(ids) == 1:
            body["content"] = {"media": {"id": ids[0], "altText": alt(0)}}
        elif ids:
            body["content"] = {"multiImage": {"images": [{"id": x, "altText": alt(i)}
                                                         for i, x in enumerate(ids)]}}
        step = len(images)
        progress.update(NS, step, "active", "Posting…")
        r = requests.post(f"{API}/posts", headers=_headers(tok), json=body, timeout=30)
        _check(r)
        pid = r.headers.get("x-restli-id", "")
        url = f"https://www.linkedin.com/feed/update/{pid}/" if pid else ""
        progress.update(NS, step, "done", url or "posted")
        res = {"success": True, "url": url}
        progress.finish(NS, res)
        return res
    except Exception as e:
        progress.fail(NS, str(e))
        return {"success": False, "error": str(e)}
