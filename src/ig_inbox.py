"""
src/ig_inbox.py — receives finished posts from other tools (e.g. the Telegram
content bot) and schedules them. Contract: INBOX_CONTRACT.md.

  inbox/instagram/<id>/post.json + media   (written as <id>.tmp/ then renamed)
  → validate → copy into input_instagram/<id>/ → frame + upload images /
    upload video (+cover) → add to the Instagram schedule
  → <id>/status.json: received → scheduled → publishing → published | failed | rejected | cancelled
  → <id>/cancel (empty file) cancels if not yet published

Runs in the launcher every 30 s (one submission per tick keeps the 1 GB VM calm).
"""

import re
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import src.ig_schedule as igs
from src.fsutil import atomic_write_json, load_json
from src.projects import INPUT_DIRS, safe_name

ROOT = Path(__file__).resolve().parent.parent
INBOX = ROOT / "inbox" / "instagram"
IMG = {".jpg", ".jpeg", ".png"}


def _status(d: Path, **kw) -> None:
    st = load_json(d / "status.json", {})
    st.update(kw, updated=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    atomic_write_json(d / "status.json", st)


def _file(d: Path, name: str, exts: set[str]) -> Path:
    p = (d / Path(str(name)).name)                 # no paths outside the submission
    if not p.is_file():
        raise ValueError(f"missing file: {name}")
    if p.suffix.lower() not in exts:
        raise ValueError(f"unsupported file type: {name}")
    return p


def _ingest(d: Path) -> None:
    post = load_json(d / "post.json", None)
    if not isinstance(post, dict):
        raise ValueError("post.json missing or not valid JSON")
    if post.get("schema") != 1:
        raise ValueError("schema must be 1")
    pid, kind = str(post.get("id", "")).strip(), post.get("type")
    if not pid or kind not in ("image", "carousel", "reel"):
        raise ValueError("id and type (image|carousel|reel) are required")
    if igs.has_inbox_id(pid):
        raise ValueError(f"duplicate id {pid} — already in the schedule")

    caption = str(post.get("caption", "")).strip()
    if post.get("append_defaults"):
        from src.ig_text import append_defaults
        caption = append_defaults(caption)
    if len(caption) > 2200:
        raise ValueError(f"caption is {len(caption)} chars (max 2200)")
    if len(re.findall(r"#\w+", caption)) > 30:
        raise ValueError("more than 30 hashtags")

    raw = post.get("publish_at")
    when = datetime.fromisoformat(raw) if raw else igs.next_free_slot()
    if when.tzinfo is None:
        when = when.replace(tzinfo=igs.tz())
    if when < datetime.now(timezone.utc):
        when = datetime.now(timezone.utc) + timedelta(minutes=1)

    project = INPUT_DIRS["instagram"] / safe_name(pid, "inbox")
    project.mkdir(parents=True, exist_ok=True)
    _status(d, state="received", error="")

    if kind == "reel":
        from src import ig_reel
        from src.uploader import upload_image, upload_video
        v = post.get("video") or {}
        src_v = _file(d, v.get("file", ""), ig_reel.VIDEO_EXTS)
        video = project / src_v.name
        shutil.copy2(src_v, video)
        bad = [m for lvl, m in ig_reel.checks(ig_reel.probe(video)) if lvl == "error"]
        if bad:
            raise ValueError("; ".join(bad))
        cover_url = None
        if post.get("cover"):
            c = project / ".ig_ready" / "cover.jpg"
            c.parent.mkdir(exist_ok=True)
            shutil.copy2(_file(d, post["cover"], IMG), c)
            cover_url = upload_image(c, project.name)
        elif post.get("cover_second") is not None:
            c = project / ".ig_ready" / "cover.jpg"
            if ig_reel.frame_at(video, float(post["cover_second"]), c):
                cover_url = upload_image(c, project.name)
        extra = {"type": "reel", "video_url": upload_video(video, project.name),
                 "cover_url": cover_url, "share_to_feed": bool(post.get("share_to_feed", True))}
        urls, alts = [], []
    else:
        import src.ig_frame as igf
        from src.uploader import upload_multiple
        slides = post.get("slides") or []
        if not 1 <= len(slides) <= 10:
            raise ValueError("1–10 slides required")
        paths = []
        for i, s in enumerate(slides):
            p = _file(d, s.get("file", ""), IMG)
            dst = project / f"{i + 1:02d}_{p.name}"
            shutil.copy2(p, dst)
            paths.append(dst)
        cfg = {**igf.load_defaults(), **(post.get("frame") or {})}
        files = igf.frame_files(paths, cfg, project) if cfg.get("on", True) else paths
        urls = upload_multiple(files, project.name)
        alts = [str(s.get("alt_text", ""))[:1000] for s in slides]
        extra = {"type": kind}

    extra.update(inbox_dir=str(d), inbox_id=pid, source=post.get("source", "inbox"))
    igs.add(project, urls, caption, alts, when, extra=extra)
    print(f"  [ig:inbox] scheduled {pid} ({kind}) for {igs.local_str(igs._utc(when))}")


def process() -> None:
    INBOX.mkdir(parents=True, exist_ok=True)
    for d in sorted(INBOX.iterdir()):
        if not d.is_dir() or d.name.endswith(".tmp") or d.name.startswith("."):
            continue
        state = load_json(d / "status.json", {}).get("state")
        if (d / "cancel").exists() and state not in ("published", "cancelled", "rejected"):
            if state == "publishing":
                continue                              # too late; let it finish
            igs.cancel_inbox(str(d))
            _status(d, state="cancelled")
            print(f"  [ig:inbox] cancelled {d.name}")
            continue
        if state:
            continue
        if time.time() - (d / "post.json").stat().st_mtime < 5 if (d / "post.json").exists() else True:
            continue                                  # still being written / no post.json yet
        try:
            _ingest(d)
        except Exception as e:
            _status(d, state="rejected", error=str(e)[:500])
            print(f"  [ig:inbox] rejected {d.name}: {e}")
        return                                        # one per tick
