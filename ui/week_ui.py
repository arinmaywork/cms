"""ui/week_ui.py — 🗓 Week planner: one batch of photos/videos → a week of scheduled posts."""

from datetime import date
from pathlib import Path

import streamlit as st

import src.ig_week as wk
from src.projects import list_projects, media_files
from ui.folder_picker import upload_url
from ui.progress_widget import live_progress, start_publish_thread

VIDEO = wk.VIDEO


def _img(path: Path) -> bytes | None:
    from ui.instagram_ui import _frame_cached, _thumb
    if path.suffix.lower() in VIDEO:
        return _frame_cached(str(path), path.stat().st_mtime, 1.0)
    return _thumb(str(path), path.stat().st_mtime, 300)


def _on_done(result: dict):
    st.session_state.wk_busy = False
    st.toast(f"📅 Scheduled {result.get('scheduled', 0)} day(s)" if result.get("success")
             else f"Week scheduling failed: {result.get('error', '')[-200:]}")


def render_week_planner():
    if st.session_state.get("wk_busy"):
        live_progress("ig_week", "📅 Scheduling the week…", _on_done)
        st.stop()

    batches = list_projects("week")
    top1, top2 = st.columns([3, 1])
    top2.link_button("⬆️ Upload a week batch", upload_url("week", f"week-{date.today():%Y-%m-%d}"),
                     width="stretch")
    if not batches:
        top1.info("Upload all of the week's photos/reels in one go (⬆️ — choose platform **Week**), "
                  "then plan them here.")
        return
    names = [b["name"] for b in batches]
    bname = top1.selectbox("Week batch", names, key="wk_batch_sel")
    batch = next(b["path"] for b in batches if b["name"] == bname)
    k = lambda s: f"wk_{bname}_{s}"                       # widget keys scoped per batch
    w = wk.plan(batch)

    c1, c2, c3 = st.columns(3)
    w["start"] = c1.date_input("First day", date.fromisoformat(w["start"]), key=k("start")).isoformat()
    w["days_n"] = int(c2.number_input("Days", 1, 14, int(w["days_n"]), key=k("n")))
    w["time"] = c3.time_input("Default time", value=__import__("datetime").time(
        *map(int, w["time"].split(":"))), key=k("time"), step=900).strftime("%H:%M")
    wk.save_plan(batch, w)
    w = wk.plan(batch)
    days = wk.visible_days(w)
    open_days = [d for d in days if not w["days"][d].get("status")]

    # ── Pool of unassigned files ──────────────────────────────────────────────
    assigned = {n for d in days for n in w["days"][d]["items"]}
    pool = [f for f in media_files(batch, "week") if f.name not in assigned]
    st.markdown(f"#### 🧺 Unassigned ({len(pool)})")
    if pool:
        cols = st.columns(8)
        for i, f in enumerate(pool):
            with cols[i % 8]:
                b = _img(f)
                if b:
                    st.image(b, width="stretch")
                st.checkbox(("🎬 " if f.suffix.lower() in VIDEO else "") + f.name, key=k(f"sel_{f.name}"))
        sel = [f.name for f in pool if st.session_state.get(k(f"sel_{f.name}"))]
        a1, a2 = st.columns([2, 1])
        if open_days:
            target = a1.selectbox("Add selected to", open_days, key=k("target"),
                                  format_func=lambda d: date.fromisoformat(d).strftime("%a %d %b"))
            if a2.button(f"➕ Add {len(sel)} selected", disabled=not sel, width="stretch", type="primary"):
                w["days"][target]["items"] += sel
                wk.save_plan(batch, w)
                for n in sel:
                    st.session_state.pop(k(f"sel_{n}"), None)
                st.rerun()
    else:
        st.caption("Everything in this batch is assigned.")

    # ── Day cards ─────────────────────────────────────────────────────────────
    st.markdown("#### 🗓 Days")
    from src import ig_archive as ia
    from ui.instagram_ui import _dups
    for d in days:
        day = w["days"][d]
        label = date.fromisoformat(d).strftime("%a %d %b")
        with st.container(border=True):
            if day.get("status"):
                st.markdown(f"**{label}** · 📅 scheduled ({len(day['items'])} file(s)) — manage it in "
                            "the Instagram schedule panel")
                continue
            h1, h2 = st.columns([3, 1])
            items = day["items"]
            kind = ("🎬 Reel" if items and Path(items[0]).suffix.lower() in VIDEO
                    else f"🖼 Carousel ({len(items)})" if len(items) > 1 else "🖼 Image" if items else "— empty")
            h1.markdown(f"**{label}** · {kind}")
            day["time"] = h2.time_input("Time", value=__import__("datetime").time(
                *map(int, (day.get("time") or w["time"]).split(":"))), key=k(f"t_{d}"), step=900,
                label_visibility="collapsed").strftime("%H:%M")
            if items:
                cols = st.columns(min(10, max(4, len(items))))
                for i, n in enumerate(items):
                    with cols[i % len(cols)]:
                        f = batch / n
                        if f.exists():
                            b = _img(f)
                            if b:
                                st.image(b, width="stretch")
                        b1, b2, b3 = st.columns(3)
                        if b1.button("◀", key=k(f"l_{d}_{i}"), disabled=i == 0):
                            items.insert(i - 1, items.pop(i)); wk.save_plan(batch, w); st.rerun()
                        if b2.button("✕", key=k(f"x_{d}_{i}"), help="Back to unassigned"):
                            items.pop(i); wk.save_plan(batch, w); st.rerun()
                        if b3.button("▶", key=k(f"r_{d}_{i}"), disabled=i == len(items) - 1):
                            items.insert(i + 1, items.pop(i)); wk.save_plan(batch, w); st.rerun()
                if Path(items[0]).suffix.lower() in VIDEO:
                    day["cover_second"] = st.number_input("Reel cover frame (seconds)", 0.0, 900.0,
                                                          float(day.get("cover_second", 1.0)), 0.5,
                                                          key=k(f"cv_{d}"))
                elif ia.ARCHIVE.exists():
                    for name, info in _dups(tuple(str(batch / n) for n in items),
                                            ia.ARCHIVE.stat().st_mtime).items():
                        st.warning(f"**{name}** looks already posted on {info['date']} — "
                                   f"[open]({info['permalink']})", icon="🔁")
            for field, lbl, hgt in (("caption", "Caption", 90), ("signature", "Signature", 60),
                                    ("footer", "Hashtags", 60)):
                key = k(f"{field}_{d}")
                st.session_state.setdefault(key, day.get(field, ""))
                day[field] = st.text_area(lbl, key=key, height=hgt)
            if items:
                probs = wk.problems(day)
                cap = wk.caption_of(day)
                st.caption(f"{len(cap):,}/2,200 chars" + "".join(f" · ⛔ {p}" for p in probs))
    wk.save_plan(batch, w)

    ready = [d for d in open_days if w["days"][d]["items"] and not wk.problems(w["days"][d])]
    st.divider()
    if st.button(f"📅 Schedule {len(ready)} ready day(s)", type="primary", width="stretch",
                 disabled=not ready):
        st.session_state.wk_busy = True
        start_publish_thread(fn=lambda: wk.schedule_week(batch), platform="ig_week")
        st.rerun()
    st.caption("Each day becomes its own post: photos are framed with your saved frame settings and "
               "uploaded now; the server publishes at the set time. Days with ⛔ issues are skipped.")
