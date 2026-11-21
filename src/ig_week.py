"""
src/ig_week.py — week planner backend.

A "week batch" is one folder of photos/videos (input_week/<batch>/). The plan
lives in <batch>/.cms.json under "week":
  {"start": "2026-11-24", "days_n": 7, "time": "19:00",
   "days": {"2026-11-24": {"items": ["a.jpg", "b.jpg"], "caption": "", "signature": "",
                           "footer": "", "time": "19:00", "alts": {}, "cover_second": 1.0,
                           "status": "", "schedule_id": ""}}}

Scheduling reuses the normal engine: each day's files are moved into
input_instagram/<batch>_<date>/, framed + uploaded (or the video uploaded),
then added to the Instagram schedule. Unassigned files stay in the batch.
"""

import re
import shutil
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path

import src.ig_schedule as igs
import src.progress as progress
from src.projects import INPUT_DIRS, load_draft, safe_name, save_draft

NS = "ig_week"
VIDEO = {".mp4", ".mov", ".m4v"}


def plan(batch: Path) -> dict:
    from src.ig_text import text_defaults
    w = load_draft(batch).get("week") or {}
    w.setdefault("start", (igs.next_free_slot().date()).isoformat())
    w.setdefault("days_n", 7)
    w.setdefault("time", igs.read()["settings"].get("time", "19:00"))
    w.setdefault("days", {})
    td = text_defaults()
    for i in range(int(w["days_n"])):
        d = (date.fromisoformat(w["start"]) + timedelta(days=i)).isoformat()
        w["days"].setdefault(d, {"items": [], "caption": "", "signature": td["signature"],
                                 "footer": td["hashtags"], "time": w["time"], "alts": {},
                                 "cover_second": 1.0, "status": "", "schedule_id": ""})
    return w


def save_plan(batch: Path, w: dict) -> None:
    data = load_draft(batch)
    data["week"] = w
    save_draft(batch, data)


def visible_days(w: dict) -> list[str]:
    start = date.fromisoformat(w["start"])
    return [(start + timedelta(days=i)).isoformat() for i in range(int(w["days_n"]))]


def caption_of(day: dict) -> str:
    return "\n\n".join(x for x in [day.get("caption", "").strip(), day.get("signature", "").strip(),
                                   day.get("footer", "").strip()] if x)


def problems(day: dict) -> list[str]:
    items = day.get("items", [])
    vids = [n for n in items if Path(n).suffix.lower() in VIDEO]
    cap = caption_of(day)
    out = []
    if vids and len(items) > 1:
        out.append("A reel must be one video on its own")
    if len(items) > 10:
        out.append(f"{len(items)} photos — max 10")
    if not cap:
        out.append("Caption is empty")
    if len(cap) > 2200:
        out.append(f"Caption {len(cap)}/2200")
    if len(re.findall(r"#\w+", cap)) > 30:
        out.append("More than 30 hashtags")
    return out


def schedule_week(batch: Path) -> dict:
    """Background job: schedule every filled, valid, not-yet-scheduled day."""
    import src.ig_frame as igf
    from src.uploader import upload_image, upload_multiple, upload_video
    w = plan(batch)
    todo = [d for d in visible_days(w)
            if w["days"][d]["items"] and not w["days"][d].get("status") and not problems(w["days"][d])]
    progress.start(NS, [f"{d} · {len(w['days'][d]['items'])} file(s)" for d in todo] or ["Nothing to schedule"])
    done = 0
    try:
        for n, d in enumerate(todo):
            day = w["days"][d]
            progress.update(NS, n, "active", "Moving files…")
            proj = INPUT_DIRS["instagram"] / safe_name(f"{batch.name}_{d}")
            proj.mkdir(parents=True, exist_ok=True)
            files = []
            for i, name in enumerate(day["items"]):
                src = batch / name
                dst = proj / f"{i + 1:02d}_{name}"
                if src.exists():
                    shutil.move(str(src), dst)
                files.append(dst)
            hh, mm = map(int, (day.get("time") or w["time"]).split(":"))
            when = datetime(*map(int, d.split("-")), hh, mm, tzinfo=igs.tz())
            caption = caption_of(day)
            if Path(files[0]).suffix.lower() in VIDEO:
                from src import ig_reel
                progress.update(NS, n, "active", "Uploading video…")
                cover = proj / ".ig_ready" / "cover.jpg"
                ig_reel.frame_at(files[0], float(day.get("cover_second", 1.0)), cover)
                extra = {"type": "reel", "video_url": upload_video(files[0], proj.name),
                         "cover_url": upload_image(cover, proj.name) if cover.exists() else None,
                         "share_to_feed": True}
                sid = igs.add(proj, [], caption, [], when, extra=extra)
            else:
                progress.update(NS, n, "active", "Framing + uploading…")
                cfg = igf.load_defaults()
                framed = igf.frame_files(files, cfg, proj) if cfg.get("on", True) else files
                urls = upload_multiple(framed, proj.name)
                alts = [day.get("alts", {}).get(nm, "")[:1000] for nm in day["items"]]
                sid = igs.add(proj, urls, caption, alts, when,
                              extra={"type": "carousel" if len(urls) > 1 else "image"})
            w = plan(batch)                                   # re-read, then mark this day
            w["days"][d].update(status="scheduled", schedule_id=sid)
            save_plan(batch, w)
            progress.update(NS, n, "done", f"📅 {igs.local_str(igs._utc(when))}")
            done += 1
        res = {"success": True, "scheduled": done}
        progress.finish(NS, res)
        return res
    except Exception:
        progress.fail(NS, traceback.format_exc()[-600:])
        return {"success": False, "error": traceback.format_exc()[-300:]}
