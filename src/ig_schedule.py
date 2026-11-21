"""
src/ig_schedule.py — scheduled Instagram posts.

Instagram's API has no native feed scheduling, so the CMS keeps the schedule
and the always-on launcher publishes at the right minute (tick() every 30 s).

• Each item is a snapshot (urls, caption, alts) taken when you schedule —
  images are already framed + uploaded, so nothing heavy happens at post time.
• Restart-safe & never double-posts: an item found mid-"publishing" after a
  crash is marked failed ("check Instagram before retrying"), not re-sent.
• Failures retry up to 3× (10 min apart), then stay visible as failed.
• Token check: if a post is due within 24 h, the token is verified (every 6 h);
  problems surface as an alert in the UI before the slot.

State: .queue/ig_schedule.json (atomic writes + cross-process flock).
"""

import contextlib
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from src.fsutil import atomic_write_json, load_json

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / ".queue" / "ig_schedule.json"
LOCK = ROOT / ".queue" / "ig_schedule.lock"
MAX_ATTEMPTS, RETRY_MIN = 3, 10


def tz() -> ZoneInfo:
    return ZoneInfo(os.getenv("CMS_TZ", "Asia/Kolkata"))


@contextlib.contextmanager
def _locked():
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(LOCK, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        except ImportError:
            pass
        yield
    finally:
        os.close(fd)


def _load() -> dict:
    d = load_json(STATE, {})
    d = d if isinstance(d, dict) else {}
    d.setdefault("items", [])
    d.setdefault("settings", {"time": "19:00"})
    d.setdefault("alert", "")
    return d


def read() -> dict:
    return _load()


def _mutate(fn):
    with _locked():
        d = _load()
        out = fn(d)
        atomic_write_json(STATE, d)
        return out


def _utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(iso: str) -> datetime:
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def local_str(iso: str) -> str:
    return parse(iso).astimezone(tz()).strftime("%a %d %b, %H:%M")


def next_free_slot() -> datetime:
    """Next day (local) at the default time with no scheduled post."""
    d = _load()
    hh, mm = map(int, d["settings"].get("time", "19:00").split(":"))
    taken = {parse(i["when"]).astimezone(tz()).date()
             for i in d["items"] if i["status"] in ("scheduled", "publishing")}
    now = datetime.now(tz())
    day = now.date() if now.time() < now.replace(hour=hh, minute=mm).time() else now.date() + timedelta(days=1)
    while day in taken:
        day += timedelta(days=1)
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz())


def _inbox(item: dict, **kw) -> None:
    """Mirror state into <inbox>/<id>/status.json for the Telegram tool."""
    if not item.get("inbox_dir"):
        return
    p = Path(item["inbox_dir"]) / "status.json"
    try:
        st = load_json(p, {})
        st.update(kw, publish_at=item.get("when"), schedule_id=item["id"],
                  updated=_utc(datetime.now(timezone.utc)))
        atomic_write_json(p, st)
    except Exception as e:
        print(f"  [ig:schedule] inbox status write failed: {e}")


def add(project: Path, urls: list[str], caption: str, alts: list[str], when: datetime,
        extra: dict | None = None) -> str:
    """extra: {"type": "reel", "video_url", "cover_url", "share_to_feed", "inbox_dir", "inbox_id"}"""
    item = {"id": uuid.uuid4().hex[:8], "project": str(project), "name": Path(project).name,
            "when": _utc(when), "status": "scheduled", "urls": urls, "caption": caption,
            "alts": alts, "attempts": 0, "error": "", "post_id": "", "ts": time.time(),
            "type": "carousel" if len(urls) > 1 else "image", **(extra or {})}
    _mutate(lambda d: d["items"].append(item))
    _inbox(item, state="scheduled")
    return item["id"]


def cancel_inbox(inbox_dir: str) -> bool:
    hit = []
    def f(d):
        keep = []
        for i in d["items"]:
            if i.get("inbox_dir") == inbox_dir and i["status"] in ("scheduled", "failed"):
                hit.append(i)
            else:
                keep.append(i)
        d["items"] = keep
    _mutate(f)
    return bool(hit)


def has_inbox_id(inbox_id: str) -> bool:
    return any(i.get("inbox_id") == inbox_id for i in _load()["items"])


def remove(ids: list[str]) -> None:
    _mutate(lambda d: d.__setitem__("items", [i for i in d["items"]
                                              if i["id"] not in ids or i["status"] == "publishing"]))


def set_when(item_id: str, when: datetime) -> None:
    def f(d):
        for i in d["items"]:
            if i["id"] == item_id and i["status"] in ("scheduled", "failed"):
                i.update(when=_utc(when), status="scheduled", attempts=0, error="")
    _mutate(f)


def set_default_time(hhmm: str) -> None:
    _mutate(lambda d: d["settings"].__setitem__("time", hhmm))


def scheduled_paths() -> set[str]:
    return {i["project"] for i in _load()["items"] if i["status"] in ("scheduled", "publishing")}


def next_post() -> dict | None:
    up = sorted((i for i in _load()["items"] if i["status"] == "scheduled"), key=lambda i: i["when"])
    return up[0] if up else None


# ── Worker (launcher process) ─────────────────────────────────────────────────

def _check_token(d: dict) -> None:
    import requests
    soon = [i for i in d["items"] if i["status"] == "scheduled"
            and parse(i["when"]) - datetime.now(timezone.utc) < timedelta(hours=24)]
    if not soon or time.time() - d.get("token_checked", 0) < 6 * 3600:
        return
    d["token_checked"] = time.time()
    try:
        r = requests.get("https://graph.instagram.com/v25.0/me", params={"fields": "username"},
                         headers={"Authorization": f"Bearer {os.getenv('INSTAGRAM_ACCESS_TOKEN', '')}"},
                         timeout=20).json()
        d["alert"] = "" if "username" in r else (
            "Instagram token problem before your next scheduled post: "
            f"{r.get('error', {}).get('message', r)} — paste a new token in ⚙️ Settings.")
    except Exception as e:
        d["alert"] = f"Couldn't verify the Instagram token ({e}) — will retry."


def tick() -> None:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env", override=True)        # pick up tokens pasted in the UI
    now = datetime.now(timezone.utc)

    def pick(d):
        for i in d["items"]:                          # crash mid-publish → never auto-resend
            if i["status"] == "publishing" and time.time() - i.get("started", 0) > 900:
                i.update(status="failed", error="Interrupted while publishing — check "
                         "Instagram before retrying (it may already be live).")
                _inbox(i, state="failed", error=i["error"])
        _check_token(d)
        due = sorted((i for i in d["items"] if i["status"] == "scheduled"
                      and parse(i["when"]) <= now and i.get("retry_at", 0) <= time.time()),
                     key=lambda i: i["when"])
        if not due:
            return None
        due[0].update(status="publishing", started=time.time())
        return dict(due[0])

    item = _mutate(pick)
    if not item:
        return
    from src.instagram_publisher import publish_to_instagram
    print(f"  [ig:schedule] publishing {item['name']} (due {item['when']})")
    _inbox(item, state="publishing")
    if item.get("type") == "reel":
        from src.instagram_publisher import publish_reel
        res = publish_reel(item["video_url"], item["caption"], item.get("cover_url"),
                           item.get("share_to_feed", True), music=item.get("music"))
    else:
        res = publish_to_instagram(item["urls"], item["caption"], alt_texts=item["alts"])
        if res.get("success"):
            from src.instagram_publisher import permalink
            res["permalink"] = permalink(res.get("post_id", ""))

    def done(d):
        for i in d["items"]:
            if i["id"] != item["id"]:
                continue
            if res.get("success"):
                i.update(status="done", post_id=res.get("post_id", ""), error="",
                         permalink=res.get("permalink", ""),
                         published_at=_utc(datetime.now(timezone.utc)))
                _inbox(i, state="published", post_id=i["post_id"], permalink=i["permalink"], error="")
            else:
                i["attempts"] += 1
                i["error"] = str(res.get("error", ""))[:300]
                if i["attempts"] < MAX_ATTEMPTS:
                    i.update(status="scheduled", retry_at=time.time() + RETRY_MIN * 60)
                    _inbox(i, state="scheduled", error=f"retrying: {i['error']}")
                else:
                    i["status"] = "failed"
                    _inbox(i, state="failed", error=i["error"])
    _mutate(done)
    if res.get("success"):
        from src.projects import archive_project
        try:
            archive_project("instagram", Path(item["project"]))
        except Exception as e:
            print(f"  [ig:schedule] archive failed: {e}")
        print(f"  [ig:schedule] ✓ {item['name']} → post {res.get('post_id')}")
    else:
        print(f"  [ig:schedule] ✗ {item['name']}: {res.get('error')}")
