"""
ui/instagram_ui.py — Instagram page.

Drafts (Cloudinary URLs, hook, per-slide text, alt text, hashtags) persist in
<project>/.cms.json — written by the automation thread itself and on every
edit — so a refresh/restart never loses work. Published projects are moved
to published/instagram/ so they can't be published twice.
"""

import re
import traceback
from pathlib import Path

import streamlit as st
from PIL import Image

import src.progress as progress
from src.ai_generator import (generate_alt_text, generate_footer_text,
                              generate_image_text, generate_title_text)
from src.history_manager import get_last_n, save_entry
import src.ig_schedule as igs
from src.instagram_publisher import publish_to_instagram
import src.ig_frame as igf
from src.projects import archive_project, load_draft, media_files, save_draft
from src.uploader import upload_multiple
from ui.folder_picker import render_project_picker
from ui.progress_widget import live_progress, start_publish_thread

CHAR_LIMIT = 2_200
EMPTY = {"title": "", "footer": "", "signature": "", "urls": {}, "texts": {}, "alts": {}}
from src.ig_text import TEXT_DEFAULTS_FILE, text_defaults as _text_defaults



def _init():
    for k, v in {"ig_project": None, "ig_d": dict(EMPTY), "ig_toast": "",
                 "ig_error": "", "ig_automating": False, "ig_publishing": False}.items():
        st.session_state.setdefault(k, v)


def _drop_widgets():
    for k in [k for k in st.session_state if str(k).startswith("igw_")]:
        del st.session_state[k]


def _open(path: Path):
    st.session_state.ig_project = path
    saved = load_draft(path).get("instagram")
    st.session_state.ig_d = {**EMPTY, **(saved or {})}
    if saved is None:                       # brand-new project → prefill your defaults
        td = _text_defaults()
        st.session_state.ig_d.update(footer=td["hashtags"], signature=td["signature"])
    _drop_widgets()


def _close():
    st.session_state.ig_project = None
    st.session_state.ig_d = dict(EMPTY)
    _drop_widgets()


def _has_content(d: dict) -> bool:
    return bool((d.get("title") or "").strip() or (d.get("footer") or "").strip()
                or any(v.strip() for v in d.get("texts", {}).values())
                or any(v.strip() for v in d.get("urls", {}).values()))


def _save():
    p = st.session_state.ig_project
    if p and Path(p).exists():
        data = load_draft(p)
        # Guard: an empty editor must never wipe a draft that has content
        if not _has_content(st.session_state.ig_d) and _has_content(data.get("instagram", {})):
            return
        data["instagram"] = st.session_state.ig_d
        save_draft(p, data)


def _seed(key: str, value: str) -> str:
    st.session_state.setdefault(key, value)
    return key


# ── Background automation (no st.* in here) ──────────────────────────────────

def _sig(cfg: dict, images: list) -> str:
    """Frame signature; with Auto ratio the cover slide decides the shape."""
    first = images[0].name if images and str(cfg.get("ratio", "")).startswith("Auto") else ""
    return igf.signature({**cfg, "first": first})


def _upload_framed(project: Path, images: list[Path], cfg: dict, d: dict) -> None:
    files = igf.frame_files(images, cfg, project) if cfg["on"] else images
    d["urls"] = {img.name: u for img, u in zip(images, upload_multiple(files, project.name))}
    d["frame_sig"] = _sig(cfg, images)


def _run_upload_only(project: Path, images: list[Path], cfg: dict) -> None:
    ns = "ig_automation"
    progress.start(ns, ["🖼 Frame + ☁️ upload"])
    try:
        progress.update(ns, 0, "active", f"{len(images)} image(s)…")
        data = load_draft(project)
        d = {**EMPTY, **data.get("instagram", {})}
        _upload_framed(project, images, cfg, d)
        data["instagram"] = d
        save_draft(project, data)
        progress.update(ns, 0, "done", "Done")
        progress.finish(ns, {"success": True})
    except Exception:
        progress.fail(ns, traceback.format_exc())


def _run_automation(project: Path, images: list[Path], notes: str, cfg: dict) -> None:
    ns = "ig_automation"
    progress.start(ns, ["☁️ Upload images to Cloudinary", "✍️ Hook", "🏷️ Hashtags"]
                   + [f"🖼️ {img.name}" for img in images])
    try:
        d = {**EMPTY, **load_draft(project).get("instagram", {})}
        progress.update(ns, 0, "active", "Uploading…")
        _upload_framed(project, images, cfg, d)
        progress.update(ns, 0, "done", f"{len(images)} uploaded")
        progress.update(ns, 1, "active", "Gemini…")
        if not d["title"].strip():          # never overwrite what you typed
            d["title"] = generate_title_text("instagram", project.name, notes)
        progress.update(ns, 1, "done", "Done")
        progress.update(ns, 2, "active", "Gemini…")
        if not d["footer"].strip():
            d["footer"] = generate_footer_text("instagram", project.name, notes)
        progress.update(ns, 2, "done", "Done")
        for i, img in enumerate(images):
            progress.update(ns, 3 + i, "active", "Analysing…")
            if not d["texts"].get(img.name, "").strip():
                d["texts"][img.name] = generate_image_text("instagram", project.name, img,
                                                          i, len(images), notes)
            if not d["alts"].get(img.name, "").strip():
                d["alts"][img.name] = generate_alt_text(img)
            progress.update(ns, 3 + i, "done", "Done")
        data = load_draft(project)
        data["instagram"] = d
        save_draft(project, data)            # persisted even if nobody is watching
        progress.finish(ns, {"success": True})
    except Exception:
        progress.fail(ns, traceback.format_exc())


def _publish_job(project: Path, urls: list[str], caption: str, alts: list[str]):
    r = publish_to_instagram(urls, caption, alt_texts=alts)
    if r.get("success"):
        archive_project("instagram", project)


@st.cache_data(max_entries=60, show_spinner=False)
def _preview(path: str, mtime: float, ratio: float, color: str, margin: float, gs: bool) -> bytes:
    return igf.preview_jpeg(Path(path), ratio, color, margin, gs)


def _frame_ui(project: Path, images: list[Path], d: dict) -> dict:
    """Frame settings (remembered as defaults) + live preview + re-upload prompt."""
    base = {**igf.load_defaults(), **d.get("frame", {})}
    with st.expander("🖼 Frame for Instagram — no cropping", expanded=True):
        c1, c2, c3, c4 = st.columns([1, 2, 1, 2])
        on = c1.toggle("Frame", value=base["on"], key="igf_on")
        names = list(igf.RATIOS)
        ratio = c2.selectbox("Aspect ratio", names, index=names.index(base["ratio"]),
                             key="igf_ratio", disabled=not on)
        color = c3.color_picker("Fill", base["color"], key="igf_color", disabled=not on)
        margin = c4.slider("Extra border %", 0, 15, int(base["margin"]), key="igf_margin",
                           disabled=not on,
                           help="0 = pad only where needed. >0 = uniform frame on all sides.")
        gs = st.checkbox("Grid-safe (photo stays fully visible in the 3:4 profile grid)",
                         value=base.get("grid_safe", True), key="igf_grid", disabled=not on)
        cfg = {"on": on, "ratio": ratio, "color": color, "margin": margin, "grid_safe": gs}
        if on:
            r = igf.target_ratio(cfg, images[0])
            st.caption(f"All {len(images)} slide(s) → {r:.2f}:1 at 1440 px wide"
                       + (" (Instagram shows every carousel slide at the first slide's ratio)"
                          if len(images) > 1 else ""))
            cols = st.columns(4)   # fixed width → sharp previews
            for i, img in enumerate(images[:8]):
                cols[i % len(cols)].image(_preview(str(img), img.stat().st_mtime, r, color,
                                                   margin, gs), width="stretch")
        elif len(images) > 1:
            st.caption("⚠️ Unframed carousel: Instagram crops slides to the first slide's ratio.")
    d["frame"] = cfg
    igf.save_defaults(cfg)
    if any(d["urls"].values()) and d.get("frame_sig") != _sig(cfg, images):
        st.warning("Frame settings changed since the images were uploaded.")
        if st.button("🖼 Re-frame & upload", key="igf_reupload", type="primary"):
            st.session_state.ig_automating = True
            start_publish_thread(fn=lambda: _run_upload_only(project, list(images), cfg),
                                 platform="ig_automation")
            st.rerun()
    return cfg


@st.cache_data(max_entries=200, show_spinner=False)
def _thumb(path: str, mtime: float, size: int) -> bytes:
    """Small cached JPEG so the browser isn't sent full-size camera files each rerun."""
    import io
    from PIL import ImageOps
    im = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    im.thumbnail((size, size))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return buf.getvalue()


def _move(d: dict, name: str, to: int):
    order = d["order"]
    order.remove(name)
    order.insert(max(0, min(to, len(order))), name)
    _save()
    st.rerun()


def _slides_ui(project: Path, all_imgs: list[Path], d: dict) -> list[Path]:
    """Carousel order + selection (persisted in the draft). Returns the slides to post."""
    names = [i.name for i in all_imgs]
    d["order"] = [n for n in d.get("order", []) if n in names] + \
                 [n for n in names if n not in d.get("order", [])]
    excl = set(d.get("exclude", [])) & set(names)
    if not d.get("exclude_init") and len(names) > 10:        # pre-select the first 10
        excl = set(d["order"][10:])
        d["exclude_init"] = True
    d["exclude"] = sorted(excl)
    picked = [n for n in d["order"] if n not in excl]
    with st.expander(f"🔀 Slide order & selection — {len(picked)}/{len(names)} selected "
                     "(max 10, first = cover)", expanded=len(names) > 10 or len(picked) > 10):
        if len(picked) > 10:
            st.warning(f"Instagram allows 10 slides — untick {len(picked) - 10}.")
        cols = st.columns(6)
        for i, n in enumerate(d["order"]):
            with cols[i % 6].container(border=True):
                st.image(_thumb(str(project / n), (project / n).stat().st_mtime, 300),
                         width="stretch")
                pos = d["order"].index(n)
                on = st.checkbox(f"#{picked.index(n) + 1}" if n in picked else "excluded",
                                 value=n not in excl, key=f"igs_on_{n}")
                if on == (n in excl):
                    d["exclude"] = sorted((excl - {n}) if on else (excl | {n}))
                    _save()
                    st.rerun()
                b1, b2, b3 = st.columns(3)
                if b1.button("◀", key=f"igs_l_{n}", disabled=pos == 0, help="Move earlier"):
                    _move(d, n, pos - 1)
                if b2.button("★", key=f"igs_c_{n}", disabled=pos == 0, help="Make cover (first)"):
                    _move(d, n, 0)
                if b3.button("▶", key=f"igs_r_{n}", disabled=pos == len(names) - 1,
                             help="Move later"):
                    _move(d, n, pos + 1)
        if st.button("↺ Reset to filename order", key="igs_reset"):
            d["order"], d["exclude"] = names, []
            d["exclude_init"] = False
            for k in [k for k in st.session_state if str(k).startswith("igs_on_")]:
                del st.session_state[k]
            _save()
            st.rerun()
    from src import ig_archive as ia
    if ia.ARCHIVE.exists() and picked:
        dups = _dups(tuple(str(project / n) for n in picked), ia.ARCHIVE.stat().st_mtime)
        for name, info in dups.items():
            st.warning(f"**{name}** looks already posted on {info['date']} — "
                       f"[open post]({info['permalink']})", icon="🔁")
    return [project / n for n in picked]


@st.fragment(run_every=30)
def _schedule_panel():
    import pandas as pd
    data = igs.read()
    if data.get("alert"):
        st.error(data["alert"], icon="🔐")
    items = sorted(data["items"], key=lambda i: i["when"])
    live = [i for i in items if i["status"] != "done"]
    done = [i for i in items if i["status"] == "done"][-5:]
    if not live and not done:
        return
    icon = {"scheduled": "📅", "publishing": "🔄", "failed": "❌", "done": "✅"}
    with st.expander(f"📅 Instagram schedule — {sum(i['status'] == 'scheduled' for i in items)} upcoming",
                     expanded=bool(live)):
        rows = live + done
        df = pd.DataFrame([{"When": igs.local_str(i["when"]), "Project": i["name"],
                            "Type": {"reel": "🎬 reel", "carousel": "🖼 carousel"}.get(i.get("type"), "🖼 image"),
                            "Slides": str(len(i["urls"])) if i["urls"] else "—", "Status": f"{icon[i['status']]} {i['status']}",
                            "Note": (i.get("error") or i.get("post_id") or "")[:80]} for i in rows])
        ev = st.dataframe(df, hide_index=True, width="stretch", on_select="rerun",
                          selection_mode="multi-row", key="igsch_tbl")
        sel = [rows[r] for r in (ev.selection.rows if ev else [])]
        c1, c2, c3, c4, c5 = st.columns(5)
        t = c1.time_input("Default time", value=__import__("datetime").time(
            *map(int, data["settings"]["time"].split(":"))), key="igsch_def", step=900)
        if t.strftime("%H:%M") != data["settings"]["time"]:
            igs.set_default_time(t.strftime("%H:%M"))
        if sel and c2.button(f"🚀 Post {len(sel)} now", width="stretch"):
            import datetime as _dt
            for i in sel:
                igs.set_when(i["id"], _dt.datetime.now(igs.tz()))
            st.toast("Will post within a minute.")
            st.rerun()
        if sel and c3.button(f"🗑 Remove {len(sel)}", width="stretch"):
            igs.remove([i["id"] for i in sel])
            st.rerun()
        if len(sel) == 1 and sel[0]["status"] in ("scheduled", "failed"):
            with c4.popover("🕒 Move"):
                import datetime as _dt
                cur = igs.parse(sel[0]["when"]).astimezone(igs.tz())
                nd = st.date_input("Date", cur.date(), key="igsch_md")
                nt = st.time_input("Time", cur.time(), key="igsch_mt", step=900)
                if st.button("Save", key="igsch_ms"):
                    igs.set_when(sel[0]["id"], _dt.datetime.combine(nd, nt).replace(tzinfo=igs.tz()))
                    st.rerun()
        if len(sel) == 1 and sel[0]["status"] in ("scheduled", "failed"):
            with c5.popover("✏️ Edit"):
                it = sel[0]
                cap = st.text_area("Caption (incl. signature & hashtags)", it["caption"], height=220,
                                   key=f"igsch_cap_{it['id']}")
                n_tags = len(set(re.findall(r"#\w+", cap.lower())))
                st.caption(f"{len(cap):,}/2,200 · {n_tags} hashtags")
                alts = list(it.get("alts") or [])
                if it.get("type") != "reel" and alts:
                    with st.expander("Alt text per slide"):
                        alts = [st.text_input(f"Slide {n + 1}", a, max_chars=1000,
                                              key=f"igsch_alt_{it['id']}_{n}") for n, a in enumerate(alts)]
                es = st.session_state.setdefault(f"igsch_mu_{it['id']}", {"music": it.get("music")})
                if it.get("type") == "reel":
                    from ui.music_ui import music_picker
                    music_picker(es, f"igsch_m_{it['id']}")
                if st.button("💾 Save changes", key=f"igsch_save_{it['id']}", type="primary",
                             disabled=not cap.strip() or len(cap) > 2200):
                    kw = {"caption": cap.strip(), "alts": alts}
                    if it.get("type") == "reel":
                        kw["music"] = es.get("music")
                    igs.update(it["id"], **kw)
                    st.session_state.pop(f"igsch_mu_{it['id']}", None)
                    st.toast("Saved — will post with the new version.")
                    st.rerun()
        st.caption("Select a row to edit, move, post now or remove. Published posts: edit in the "
                   "Instagram app (the API can't change them). Failed posts retry 3× (10 min apart).")


def _posts_hub():
    """One panel for everything about past posts — reads the local archive (instant)."""
    import pandas as pd
    from datetime import datetime, timezone
    from src import ig_archive as ia
    from src.fsutil import load_json
    arch = load_json(ia.ARCHIVE, {})
    posts = sorted(arch.get("posts", {}).values(), key=lambda p: p["date"], reverse=True)
    with st.expander(f"📊 Posts & archive — {len(posts)} posts"
                     + (f" · synced {arch['synced']}" if arch.get("synced") else "")):
        c1, c2 = st.columns([1, 3])
        if c1.button("🔄 Sync from Instagram", width="stretch",
                     disabled=st.session_state.get("ig_archiving", False)):
            st.session_state.ig_archiving = True
            start_publish_thread(fn=ia.sync, platform="ig_archive")
            st.rerun()
        c2.caption("Also syncs automatically once a day. First sync downloads each post once; "
                   "later syncs only fetch new posts and refresh likes/comments.")
        if not posts:
            st.info("No archive yet — click Sync.")
            return
        t1, t2, t3, t4 = st.tabs(["📈 Consistency", "🖼 Recent posts", "⚠️ Missing originals", "🗂 Archive file"])
        with t1:
            ts = sorted(datetime.fromisoformat(p["date"].replace("+0000", "+00:00")) for p in posts)
            now = datetime.now(timezone.utc)
            gaps = [(b - a).days for a, b in zip(ts, ts[1:])]
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Posts, last 30 days", sum((now - t).days < 30 for t in ts))
            m2.metric("Avg days between", f"{sum(gaps[-20:]) / len(gaps[-20:]):.1f}" if gaps else "—",
                      help="Over your last 20 gaps")
            m3.metric("Longest gap (days)", max(gaps) if gaps else "—")
            m4.metric("Days since last post", (now - ts[-1]).days)
            wk = pd.Series(1, index=pd.to_datetime(ts)).resample("W").sum().tail(26)
            st.bar_chart(wk.rename("posts per week"), height=170)
        with t2:
            cols = st.columns(6)
            for i, p in enumerate(posts[:36]):
                th = ia.THUMBS / f"{p['id']}_0.jpg"
                with cols[i % 6]:
                    if th.exists():
                        st.image(str(th), width="stretch")
                    st.caption(f"{p['date'][:10]} · ❤️ {p.get('likes', '–')} · 💬 {p.get('comments', '–')}")
                    with st.popover("Details"):
                        text, tags = ia.split_caption(p["caption"])
                        st.markdown(f"**Files:** {', '.join(s.get('file') or '—' for s in p['slides'])}")
                        st.text(text or "(no caption)")
                        st.caption(" ".join(tags))
                        st.link_button("Open on Instagram ↗", p["permalink"] or "https://instagram.com")
        with t3:
            with st.container(border=True):
                st.markdown("**Bulk import:** export every photo you've posted (extras are fine; "
                            "~2048 px JPEG is plenty), then copy them to the VM:")
                st.code('rsync -avP ~/Exports/ig/ ubuntu@143.47.244.207:~/cms-local/published/instagram/_import/',
                        language="bash")
                st.caption("Then click Sync — each photo is matched to its post by image fingerprint "
                           "and filed into published/instagram/<date>_<post>/ in slide order.")
                left = arch.get("import_leftovers", [])
                if left:
                    st.warning(f"{len(left)} exported photo(s) matched no post (probably never posted).")
                    if st.button(f"🗑 Delete the {len(left)} unmatched export(s)", key="ig_left_del"):
                        st.toast(f"Deleted {ia.delete_leftovers()} file(s)")
                        st.rerun()
            miss = ia.missing(arch)
            if not miss:
                st.success("Every posted image has its original on the server ✓")
            else:
                st.markdown(f"**{len(miss)} posted image(s) without a matched original.** For each, pick "
                            "the right photo from the closest candidates — or mark it as not available.")
                d1, d2 = st.columns(2)
                busy = st.session_state.get("ig_deep", False)
                if d1.button("🔬 Deep match — unmatched exports (~1–3 min)", width="stretch",
                             disabled=busy, type="primary"):
                    from src import ig_deepmatch
                    st.session_state.ig_deep = True
                    start_publish_thread(fn=lambda: ig_deepmatch.run("leftovers"), platform="ig_deep")
                    st.rerun()
                if d2.button("🔬 Deep match — every photo on the server (slow)", width="stretch", disabled=busy):
                    from src import ig_deepmatch
                    st.session_state.ig_deep = True
                    start_publish_thread(fn=lambda: ig_deepmatch.run("all"), platform="ig_deep")
                    st.rerun()
                st.caption("Deep match compares distinctive points (edges, textures) instead of the whole "
                           "image, so padding, crops and colour edits don't fool it. Clear winners are "
                           "matched automatically; the rest show better-ranked candidates below.")
                if busy:
                    live_progress("ig_deep", "🔬 Deep matching…", _on_deep_done)
                hide = st.toggle("Hide reel covers", True, key="ig_miss_photos")
                shown = [m for m in miss if not (hide and m["type"] == "VIDEO")]
                mode = st.segmented_control("How to match", ["Upload originals", "Pick from all exports",
                                                             "Suggestions (top 4)"],
                                            default="Upload originals", key="ig_match_mode")
                if mode == "Upload originals" and shown:
                    _upload_originals(ia, shown)
                    shown = []
                elif mode == "Pick from all exports" and shown:
                    _manual_matcher(ia, arch, shown)
                    shown = []
                local = _local_index_cached(ia.ARCHIVE.stat().st_mtime)
                posts_by_id = arch.get("posts", {})
                PER = 8
                pages = max(1, -(-len(shown) // PER))
                pg = st.number_input(f"Page (of {pages})", 1, pages, 1, key="ig_miss_pg") if pages > 1 else 1
                for m in shown[(pg - 1) * PER: pg * PER]:
                    slide = posts_by_id[m["id"]]["slides"][m["idx"]]
                    with st.container(border=True):
                        cols = st.columns([1.2, 1, 1, 1, 1])
                        with cols[0]:
                            if Path(m["thumb"]).exists():
                                st.image(m["thumb"], width="stretch")
                            st.caption(f"**Posted** {m['date']} · {m['slide']}/{m['of']} · [open]({m['permalink']})")
                            if st.button("✗ Not on server", key=f"na_{m['id']}_{m['idx']}"):
                                ia.assign(m["id"], m["idx"], None)
                                st.rerun()
                        deep = slide.get("deep")
                        cands = ([(sc, pth) for sc, pth in deep if Path(pth).exists()][:4] if deep
                                 else ia.candidates(slide, local, 4) if slide.get("hash") else [])
                        for ci, (col, (sc, path)) in enumerate(zip(cols[1:], cands)):
                            with col:
                                fp = Path(path)
                                if fp.exists():
                                    st.image(_thumb(path, fp.stat().st_mtime, 300), width="stretch")
                                st.caption(f"{fp.parent.name}/{fp.name} · "
                                           + (f"{sc} matching points" if deep else f"score {sc}"))
                                if st.button("✓ This one", key=f"ok_{m['id']}_{m['idx']}_{ci}",
                                             type="primary" if (sc >= 25 if deep else sc <= 30) else "secondary"):
                                    ia.assign(m["id"], m["idx"], path)
                                    st.toast(f"Matched {fp.name}")
                                    st.rerun()
        with t4:
            if ia.MD.exists():
                if st.button("♻️ Rebuild summary", key="ig_md_rebuild"):
                    ia.write_md()
                    st.rerun()
                d1, d2 = st.columns(2)
                d1.download_button("⬇️ Download .zip (md + thumbnails — opens with previews)",
                                   _md_zip(ia.MD.stat().st_mtime), file_name="instagram_posts.zip",
                                   mime="application/zip", width="stretch")
                d2.download_button("⬇️ Download .md only", ia.MD.read_bytes(),
                                   file_name="instagram_posts.md", mime="text/markdown", width="stretch")
                st.markdown(ia.preview_html(ia.MD.read_text(encoding="utf-8")), unsafe_allow_html=True)


@st.cache_data(max_entries=5000, show_spinner=False)
def _capture_date(path: str, mtime: float) -> str:
    """EXIF DateTimeOriginal → 'YYYY-MM-DD' ('' if absent)."""
    try:
        with Image.open(path) as im:
            ex = im.getexif()
            raw = ex.get_ifd(0x8769).get(36867) or ex.get(306) or ""
        return str(raw)[:10].replace(":", "-") if raw else ""
    except Exception:
        return ""


@st.cache_data(max_entries=2, show_spinner="Packing…")
def _md_zip(mtime: float) -> bytes:
    from src import ig_archive as ia
    return ia.export_zip()


def _upload_originals(ia, shown: list):
    """One row per posted image without an original: drop the original in, done."""
    st.caption("Drop the original photo into the box next to each posted image. It's attached to that "
               "exact post, filed into its folder and removed from this list.")
    PER = 10
    pages = max(1, -(-len(shown) // PER))
    pg = st.number_input(f"Page (of {pages})", 1, pages, 1, key="ig_up_pg") if pages > 1 else 1
    for m in shown[(pg - 1) * PER: pg * PER]:
        with st.container(border=True):
            c1, c2, c3 = st.columns([1, 3, 1])
            with c1:
                if Path(m["thumb"]).exists():
                    st.image(m["thumb"], width="stretch")
            with c2:
                st.markdown(f"**Posted {m['date']}** · slide {m['slide']} of {m['of']} · "
                            f"[open on Instagram]({m['permalink']})")
                f = st.file_uploader("Original", type=["jpg", "jpeg", "png", "webp"],
                                     key=f"up_{m['id']}_{m['idx']}", label_visibility="collapsed")
                if f is not None:
                    dest = ia.attach(m["id"], m["idx"], f.name, f.getvalue())
                    st.toast(f"Attached {dest.name}")
                    st.rerun()
            with c3:
                if st.button("✗ Not available", key=f"upna_{m['id']}_{m['idx']}", width="stretch"):
                    ia.assign(m["id"], m["idx"], None)
                    st.rerun()


def _manual_matcher(ia, arch: dict, shown: list):
    """One posted image at a time vs. every unmatched export. Pool shrinks as you match."""
    i = min(st.session_state.get("ig_mm_i", 0), len(shown) - 1)
    m = shown[i]
    slide = arch["posts"][m["id"]]["slides"][m["idx"]]
    pool = [x for x in arch.get("import_leftovers", []) if Path(x).exists()]
    if st.toggle("Also show photos already filed under other posts", key="ig_mm_all"):
        pool += [x for x in _local_index_cached(ia.ARCHIVE.stat().st_mtime) if x not in pool]
    deep = {pth: sc for sc, pth in slide.get("deep", []) if sc >= 15}   # only real matches
    # one entry per distinct image (same photo can sit in several folders)
    norm = lambda q: str(Path(q).resolve())
    idx = {norm(k): v for k, v in _local_index_cached(ia.ARCHIVE.stat().st_mtime).items()}
    pool = list(dict.fromkeys(norm(x) for x in pool))
    deep = {norm(k): v for k, v in deep.items()}
    seen, uniq = [], []
    for x in sorted(pool, key=lambda q: ("_import" not in q, len(q))):
        h = (idx.get(x) or [None])[0]
        if h and any(ia.distance(h, o) <= 6 for o in seen):
            continue
        if h:
            seen.append(h)
        uniq.append(x)
    from datetime import datetime as _dt
    post_day = _dt.fromisoformat(m["date"])
    def _key(x):
        if x in deep:
            return (0, -deep[x])
        cd = _capture_date(x, Path(x).stat().st_mtime)
        if not cd:
            return (3, 0)
        d = (post_day - _dt.fromisoformat(cd)).days
        return (1, d) if d >= 0 else (2, -d)              # taken before the post first, nearest first
    pool = sorted(uniq, key=_key)
    left, right = st.columns([1, 3], gap="large")
    with left:
        st.markdown(f"**Posted image {i + 1} of {len(shown)}**")
        if Path(m["thumb"]).exists():
            st.image(m["thumb"], width="stretch")
        st.caption(f"{m['date']} · slide {m['slide']}/{m['of']} · [open on Instagram]({m['permalink']})")
        n1, n2 = st.columns(2)
        if n1.button("◀ Prev", disabled=i == 0, width="stretch", key="ig_mm_prev"):
            st.session_state.ig_mm_i = i - 1
            st.rerun()
        if n2.button("Skip ▶", disabled=i >= len(shown) - 1, width="stretch", key="ig_mm_next"):
            st.session_state.ig_mm_i = i + 1
            st.rerun()
        if st.button("✗ Original not on server", width="stretch", key="ig_mm_na"):
            ia.assign(m["id"], m["idx"], None)
            st.rerun()
    with right:
        st.markdown(f"**Click the matching photo** — {len(pool)} candidate(s), "
                    "sorted by capture date closest before the post date")
        per = 30
        pages = max(1, -(-len(pool) // per))
        pg = st.number_input(f"Photos page (of {pages})", 1, pages, 1, key="ig_mm_pg") if pages > 1 else 1
        cols = st.columns(5)
        for j, path in enumerate(pool[(pg - 1) * per: pg * per]):
            fp = Path(path)
            with cols[j % 5]:
                st.image(_thumb(path, fp.stat().st_mtime, 300), width="stretch")
                cd = _capture_date(path, fp.stat().st_mtime)
                tag = f" · ⭐ {deep[path]} pts" if path in deep else (f" · 📷 {cd}" if cd else "")
                if st.button(f"✓ {fp.name}{tag}", key=f"ig_mm_{m['id']}_{m['idx']}_{pg}_{j}", width="stretch"):
                    ia.assign(m["id"], m["idx"], path)
                    st.toast(f"Matched {fp.name}")
                    st.rerun()


def _on_deep_done(result: dict):
    st.session_state.ig_deep = False
    st.session_state.ig_toast = (f"🔬 Deep match: {result.get('matched', 0)} matched automatically "
                                 f"({result.get('checked', 0)} photos checked)" if result.get("success")
                                 else f"Deep match failed: {result.get('error')}")


def _on_archive_done(result: dict):
    st.session_state.ig_archiving = False
    st.session_state.ig_toast = (f"🗂 Archive synced — {result.get('posts')} posts "
                                 f"({result.get('new')} new)" if result.get("success")
                                 else f"Archive sync failed: {result.get('error')}")


@st.cache_data(max_entries=3, show_spinner="Indexing photos on the server…")
def _local_index_cached(archive_mtime: float) -> dict:
    from src import ig_archive as ia
    return ia.local_index()


@st.cache_data(max_entries=20, show_spinner=False)
def _dups(paths: tuple, archive_mtime: float) -> dict:
    from src import ig_archive as ia
    return ia.find_duplicates([Path(x) for x in paths])


def _defaults_editor():
    from src.fsutil import atomic_write_json
    with st.expander("✏️ Defaults for new posts (hashtags & signature)"):
        td = _text_defaults()
        with st.form("ig_defaults"):
            h = st.text_area("Default hashtags", td["hashtags"], height=70)
            sgn = st.text_area("Default signature", td["signature"], height=70)
            if st.form_submit_button("Save defaults"):
                atomic_write_json(TEXT_DEFAULTS_FILE, {"hashtags": h.strip(), "signature": sgn.strip()})
                st.toast("Saved — applies to newly opened projects")


# ── Reels ─────────────────────────────────────────────────────────────────────
@st.cache_data(max_entries=50, show_spinner=False)
def _probe_cached(path: str, mtime: float) -> dict:
    from src import ig_reel
    return ig_reel.probe(Path(path))


@st.cache_data(max_entries=40, show_spinner=False)
def _frame_cached(path: str, mtime: float, sec: float) -> bytes | None:
    from src import ig_reel
    return ig_reel.frame_at(Path(path), sec, width=480)


def _reel_prepare(project: Path, video: Path, r: dict) -> tuple[str, str | None]:
    from src import ig_reel
    from src.uploader import upload_image, upload_video
    c = project / ".ig_ready" / "cover.jpg"
    c.parent.mkdir(exist_ok=True)
    c.unlink(missing_ok=True)
    if r.get("cover_mode") == "image" and r.get("cover_file"):
        import shutil
        shutil.copy2(project / r["cover_file"], c)
    else:
        ig_reel.frame_at(video, float(r.get("cover_second", 1.0)), c)
    cover_url = upload_image(c, project.name) if c.exists() else None
    return upload_video(video, project.name), cover_url


def _reel_publish_job(project: Path, video: Path, r: dict, caption: str):
    from src.instagram_publisher import publish_reel
    progress.start("ig", ["☁️ Upload video & cover"])
    try:
        vurl, curl = _reel_prepare(project, video, r)
    except Exception as e:
        progress.fail("ig", f"Upload failed: {e}")
        return
    if publish_reel(vurl, caption, curl, r.get("share_to_feed", True), music=r.get("music")).get("success"):
        archive_project("instagram", project)


def _reel_schedule_job(project: Path, video: Path, r: dict, caption: str, when):
    ns = "ig_automation"
    progress.start(ns, ["☁️ Upload video & cover", "📅 Add to schedule"])
    try:
        progress.update(ns, 0, "active", f"{video.stat().st_size / 1_048_576:.0f} MB…")
        vurl, curl = _reel_prepare(project, video, r)
        progress.update(ns, 0, "done", "Uploaded")
        igs.add(project, [], caption, [], when, extra={
            "type": "reel", "video_url": vurl, "cover_url": curl,
            "share_to_feed": r.get("share_to_feed", True), "music": r.get("music")})
        progress.update(ns, 1, "done", igs.local_str(igs._utc(when)))
        progress.finish(ns, {"success": True, "scheduled": igs.local_str(igs._utc(when))})
    except Exception:
        progress.fail(ns, traceback.format_exc())


def _reel_editor(project: Path, vids: list[Path], images: list[Path]):
    from src import ig_reel
    d = st.session_state.ig_d
    r = d.setdefault("reel", {})
    r.setdefault("video", vids[0].name)
    r.setdefault("cover_mode", "frame")
    r.setdefault("cover_second", 1.0)
    r.setdefault("share_to_feed", True)
    if r["video"] not in [v.name for v in vids]:
        r["video"] = vids[0].name

    h1, h2 = st.columns([5, 1])
    h1.subheader(f"🎬 Reel · {project.name}")
    if h2.button("← Projects", key="igr_back", width="stretch"):
        _close()
        st.rerun()

    left, right = st.columns([2, 3], gap="large")
    with left:
        if len(vids) > 1:
            names = [v.name for v in vids]
            r["video"] = st.selectbox("Video", names, index=names.index(r["video"]), key="igr_vid")
        video = project / r["video"]
        info = _probe_cached(str(video), video.stat().st_mtime)
        chk = ig_reel.checks(info)
        with st.container(border=True):
            st.markdown("**Pre-flight checks**")
            for lvl, msg in chk:
                st.markdown(f"{ {'ok': '✅', 'warn': '⚠️', 'error': '⛔'}[lvl] } {msg}")
        if st.toggle("▶ Preview video", key="igr_prev"):
            st.video(str(video))
        st.markdown("**Cover**")
        modes = ["Frame from video"] + (["Image in folder"] if images else [])
        m = st.segmented_control("Cover source", modes, key="igr_cmode", label_visibility="collapsed",
                                 default=modes[1] if r["cover_mode"] == "image" and images else modes[0])
        r["cover_mode"] = "image" if m == "Image in folder" else "frame"
        if r["cover_mode"] == "frame":
            dur = max(0.5, info["duration"] or 60.0)
            r["cover_second"] = st.slider("Frame at (seconds)", 0.0, float(dur),
                                          min(float(r["cover_second"]), float(dur)), 0.1, key="igr_sec")
            fb = _frame_cached(str(video), video.stat().st_mtime, r["cover_second"])
            if fb:
                st.image(fb, width=220)
            else:
                st.caption("Install ffmpeg on the server to preview / extract cover frames.")
        else:
            names = [i.name for i in images]
            cur = r.get("cover_file") if r.get("cover_file") in names else names[0]
            r["cover_file"] = st.selectbox("Cover image", names, index=names.index(cur), key="igr_cfile")
            st.image(_thumb(str(project / r["cover_file"]), (project / r["cover_file"]).stat().st_mtime, 400),
                     width=220)
        r["share_to_feed"] = st.toggle("Also show on the profile grid & feed", r["share_to_feed"],
                                       key="igr_feed")
        from ui.music_ui import music_picker
        music_picker(r, "igr")

    with right:
        d["title"] = st.text_area("Caption", key=_seed("igw_title", d["title"]), height=170,
                                  placeholder="Hook first — only the first line shows before '…more'")
        d["signature"] = st.text_area("Signature", key=_seed("igw_sig", d.get("signature", "")), height=70)
        d["footer"] = st.text_area("Hashtags", key=_seed("igw_footer", d["footer"]), height=70)
        caption = "\n\n".join(x for x in [d["title"].strip(), d.get("signature", "").strip(),
                                            d["footer"].strip()] if x)
        tags = re.findall(r"#\w+", caption)
        st.caption(f"{len(caption):,}/2,200 characters · {len(set(t.lower() for t in tags))} hashtags (max 30)")
        _save()
        problems = [m for lvl, m in chk if lvl == "error"] \
            + (["Caption is empty"] if not caption else []) \
            + (["Caption over 2,200 characters"] if len(caption) > 2200 else []) \
            + (["More than 30 hashtags"] if len(tags) > 30 else [])
        for x in problems:
            st.caption(f"⛔ {x}")
        with st.container(border=True):
            slot = igs.next_free_slot()
            a1, a2, a3 = st.columns(3)
            day = a1.date_input("Date", slot.date(), key="igr_d")
            tm = a2.time_input("Time", slot.time(), key="igr_t", step=900)
            if a3.button("📅 Schedule", width="stretch", disabled=bool(problems), type="primary"):
                import datetime as _dt
                when = _dt.datetime.combine(day, tm).replace(tzinfo=igs.tz())
                save_entry("instagram", project.name, caption, [str(video)])
                st.session_state.ig_automating = True
                start_publish_thread(fn=lambda: _reel_schedule_job(project, video, dict(r), caption, when),
                                     platform="ig_automation")
                st.rerun()
            st.caption("The video is uploaded now; the server posts it at that time.")
        if st.button("🚀 Publish reel now", width="stretch", disabled=bool(problems)):
            save_entry("instagram", project.name, caption, [str(video)])
            st.session_state.ig_publishing = True
            start_publish_thread(fn=lambda: _reel_publish_job(project, video, dict(r), caption),
                                 platform="ig")
            st.rerun()
        st.caption("Instagram processes the video for 1–5 minutes before it goes live.")


def _on_automation_done(result: dict):
    st.session_state.ig_automating = False
    if result.get("scheduled"):
        st.session_state.ig_toast = f"📅 Reel scheduled for {result['scheduled']}"
        _close()
        return
    if result.get("success"):
        _open(st.session_state.ig_project)
        st.session_state.ig_toast = "✅ AI copy ready — review and publish."
    else:
        st.session_state.ig_error = f"Automation failed: {result.get('error', '')[-500:]}"


def _on_publish_done(result: dict):
    st.session_state.ig_publishing = False
    if result.get("success"):
        st.session_state.ig_toast = f"✅ Published to Instagram (post {result.get('post_id')})"
        _close()
    else:
        st.session_state.ig_error = f"Publish failed: {result.get('error')}"


# ── Page ──────────────────────────────────────────────────────────────────────

def render_instagram_ui():
    _init()
    if st.session_state.ig_toast:
        st.toast(st.session_state.ig_toast)
        st.session_state.ig_toast = ""
    if st.session_state.ig_error:
        e1, e2 = st.columns([10, 1])
        e1.error(st.session_state.ig_error)
        if e2.button("✕", key="ig_err_x"):
            st.session_state.ig_error = ""
            st.rerun()

    if st.session_state.ig_automating:
        live_progress("ig_automation", "🤖 Uploading & writing copy…", _on_automation_done)
        st.stop()
    if st.session_state.ig_publishing:
        live_progress("ig", "🚀 Publishing to Instagram…", _on_publish_done)
        st.stop()

    _schedule_panel()

    project = st.session_state.ig_project
    if not project or not Path(project).exists():
        if st.session_state.get("ig_archiving"):
            live_progress("ig_archive", "🗂 Syncing posts archive…", _on_archive_done)
        _posts_hub()
        _defaults_editor()
        if st.segmented_control("Plan", ["One post", "🗓 Week planner"], default="One post",
                                key="ig_mode") == "🗓 Week planner":
            from ui.week_ui import render_week_planner
            render_week_planner()
            return
        picked = render_project_picker("instagram")
        if picked:
            _open(picked)
            st.rerun()
        return

    project = Path(project)
    images = media_files(project, "instagram")
    from src.ig_reel import videos_in
    vids = videos_in(project)
    if vids:                                       # a video makes this a Reel project
        _reel_editor(project, vids, images)
        return
    d = st.session_state.ig_d
    if not _has_content(d):
        last = next((e for e in reversed(get_last_n("instagram", 50))
                     if e.get("project") == project.name), None)
        if last:
            st.info(f"A publish attempt for this project was saved on {last['timestamp'][:16]}. "
                    "Restore its caption and uploaded image URLs?")
            if st.button("↩ Restore from last publish attempt", key="ig_restore", type="primary"):
                d["title"] = last.get("content", "")
                urls = [u for u in last.get("images", []) if str(u).startswith("http")]
                for img, u in zip(images, urls):
                    d["urls"][img.name] = u
                _drop_widgets()
                st.session_state.ig_d = d
                data = load_draft(project); data["instagram"] = d; save_draft(project, data)
                st.rerun()
    h1, h2 = st.columns([5, 1])
    h1.subheader(f"📸 {project.name} — {len(images)} image(s)")
    if h2.button("← Projects", key="ig_back", width="stretch"):
        _close()
        st.rerun()
    if not images:
        st.error("No images in this folder.")
        return
    images = _slides_ui(project, images, d)      # user order + include/exclude

    cfg = _frame_ui(project, images, d)
    notes = st.text_input("🗒️ AI context", key="ig_notes", placeholder="e.g. minimal architecture")
    b1, b2 = st.columns(2)
    if b2.button("☁️ Upload images only (no AI)", width="stretch"):
        _save()
        st.session_state.ig_automating = True
        start_publish_thread(fn=lambda: _run_upload_only(project, list(images), cfg),
                             platform="ig_automation")
        st.rerun()
    if b1.button("🤖 Automate (upload + AI fills empty fields)", type="primary",
                 width="stretch"):
        _save()
        st.session_state.ig_automating = True
        start_publish_thread(fn=lambda: _run_automation(project, list(images), notes, cfg),
                             platform="ig_automation")
        st.rerun()

    d["title"] = st.text_area("Hook", key=_seed("igw_title", d["title"]))
    for i, img in enumerate(images):
        c1, c2 = st.columns([1, 3])
        with c1:
            st.image(_thumb(str(img), img.stat().st_mtime, 900), width="stretch")
        with c2:
            st.markdown(f"**Slide {i + 1}** · `{img.name}`")
            d["texts"][img.name] = st.text_area(
                "Caption line (all slides are joined into ONE post caption)", key=_seed(f"igw_cap_{img.name}", d["texts"].get(img.name, "")), height=90)
            d["alts"][img.name] = st.text_input(
                "Alt text", key=_seed(f"igw_alt_{img.name}", d["alts"].get(img.name, "")))
    d["signature"] = st.text_area("Signature (added after the caption lines, before hashtags)",
                                  key=_seed("igw_sig", d.get("signature", "")), height=70)
    d["footer"] = st.text_area("Hashtags", key=_seed("igw_footer", d["footer"]))

    with st.expander("Advanced: image URLs (filled automatically by Automate)"):
        for i, img in enumerate(images):
            d["urls"][img.name] = st.text_input(
                img.name, key=_seed(f"igw_url_{img.name}", d["urls"].get(img.name, "")))
    _save()

    caption = "\n\n".join(x for x in [d["title"].strip()]
                          + [d["texts"].get(img.name, "").strip() for img in images]
                          + [d.get("signature", "").strip(), d["footer"].strip()] if x)
    urls = [d["urls"].get(img.name, "").strip() for img in images]
    missing = sum(1 for u in urls if not u.startswith("http"))

    st.divider()
    with st.expander(f"🔍 Caption preview ({len(caption):,}/{CHAR_LIMIT:,})", expanded=True):
        st.text(caption or "(empty)")
    tags = re.findall(r"#(\w+)", caption)
    seen, dups = set(), []
    for t in tags:
        (dups.append(t) if t.lower() in seen else seen.add(t.lower()))
    if dups:
        c1, c2 = st.columns([3, 1])
        c1.caption(f"⚠️ Duplicate hashtags: {', '.join('#' + t for t in dict.fromkeys(dups))}")
        if c2.button("Remove duplicates", key="ig_dedupe"):
            out, seen2 = [], set()
            for w in d["footer"].split():
                k = w.lower()
                if w.startswith("#") and k in seen2:
                    continue
                seen2.add(k)
                out.append(w)
            d["footer"] = " ".join(out)
            st.session_state.pop("igw_footer", None)
            _save()
            st.rerun()
    st.caption(f"{len(seen)} unique hashtag(s) · Instagram rejects posts with more than 30")
    problems = ([f"{len(tags)} hashtags — Instagram allows max 30"] if len(tags) > 30 else []) + ([f"{missing} image(s) not uploaded yet — click Upload images only (or Automate)"] if missing else []) \
        + (["Caption is empty"] if not caption else []) \
        + ([f"Caption too long ({len(caption):,}/{CHAR_LIMIT:,})"] if len(caption) > CHAR_LIMIT else []) \
        + (["Max 10 images"] if len(images) > 10 else [])
    for p in problems:
        st.caption(f"⚠️ {p}")
    alts = [d["alts"].get(img.name, "") for img in images]
    with st.container(border=True):
        st.markdown("**📅 Schedule instead of posting now**")
        slot = igs.next_free_slot()
        s1, s2, s3 = st.columns([1, 1, 1])
        day = s1.date_input("Date", slot.date(), key="igsch_d")
        tm = s2.time_input("Time", slot.time(), key="igsch_t", step=900)
        if s3.button("📅 Add to schedule", width="stretch", disabled=bool(problems)):
            import datetime as _dt
            when = _dt.datetime.combine(day, tm).replace(tzinfo=igs.tz())
            save_entry("instagram", project.name, caption, urls)
            igs.add(project, urls, caption, alts, when)
            st.session_state.ig_toast = f"📅 Scheduled {project.name} for {when.strftime('%a %d %b, %H:%M')}"
            _close()
            st.rerun()
        st.caption("Images are already uploaded; the server posts automatically at that time "
                   "(your device can be off). Edits after scheduling aren't applied — remove "
                   "and re-add instead.")
    if st.button("🚀 APPROVE & PUBLISH NOW", type="primary", width="stretch",
                 disabled=bool(problems)):
        save_entry("instagram", project.name, caption, urls)
        st.session_state.ig_publishing = True
        start_publish_thread(fn=lambda: _publish_job(project, urls, caption, alts),
                             platform="ig")
        st.rerun()
