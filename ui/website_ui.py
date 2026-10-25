"""
ui/website_ui.py — publish a photo story to the portfolio website (Cloudflare R2).

Source: an input_website/<project>/ folder. Publishing uploads GPS-free images
and stories/<slug>.json + stories/index.json (see src/r2.py), then moves the
project to published/website/. Re-publishing the same slug updates the story in place.
"""

import datetime
import re
import shutil
import traceback
from pathlib import Path
from urllib.parse import urlparse

import streamlit as st

import src.progress as progress
import src.r2 as r2
from src.ai_generator import generate_alt_text, generate_story_draft
from src.projects import ARCHIVE_DIR, INPUT_DIRS, archive_project, load_draft, media_files, save_draft
from ui.folder_picker import render_project_picker
from ui.progress_widget import live_progress, start_publish_thread

SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
DESC_MAX = 160


def _init():
    for k, v in {"ws_project": None, "ws_toast": "", "ws_error": "",
                 "ws_publishing": False, "ws_ai": False, "ws_edit": None}.items():
        st.session_state.setdefault(k, v)


def _open(p: Path | None):
    st.session_state.ws_project = p
    for k in [k for k in st.session_state if str(k).startswith("wsw_")]:
        del st.session_state[k]


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _draft(p: Path) -> dict:
    return load_draft(p).get("website", {})


def _save(p: Path, d: dict):
    data = load_draft(p)
    data["website"] = d
    save_draft(p, data)


@st.cache_data(ttl=30, show_spinner=False)
def _published() -> list[dict]:
    return r2.list_stories()


def _ai_job(p: Path, names: list[str], notes: str):
    progress.start("ws_ai", ["✍️ Writing the story", "🖼️ Alt text for each image"])
    try:
        d = _draft(p)
        progress.update("ws_ai", 0, "active")
        d.update(generate_story_draft(p.name, p / names[0], notes))
        _save(p, d)
        progress.update("ws_ai", 0, "done")
        alts = d.setdefault("alts", {})
        for i, n in enumerate(names, 1):
            progress.update("ws_ai", 1, "active", f"{i}/{len(names)}")
            if not alts.get(n, "").strip():
                alts[n] = generate_alt_text(p / n)
                _save(p, d)
        progress.update("ws_ai", 1, "done")
        progress.finish("ws_ai", {"success": True})
    except Exception:
        progress.fail("ws_ai", traceback.format_exc())


def _publish_job(p: Path, slug: str, d: dict, names: list[str]):
    progress.start("ws", ["🖼️ Cleaning & uploading images", "📝 Publishing story"])
    progress.update("ws", 0, "active")

    def tick(n, total):
        progress.update("ws", 0, "active" if n < total else "done", f"{n}/{total}")
        if n == total:
            progress.update("ws", 1, "active")

    r2.publish_story(
        slug, d["title"].strip(), d["description"].strip(), d.get("body_md", "").strip(),
        [{"path": p / n, "alt": d["alts"].get(n, "").strip(), "caption": d["captions"].get(n, "").strip(),
          "specs": d.get("specs", {}).get(n, "").strip()}
         for n in names],
        location=d.get("location", "").strip(),
        tags=[t.strip() for t in d.get("tags", "").split(",") if t.strip()],
        published_at=d.get("published_at"), progress=tick)
    progress.update("ws", 1, "done")
    archive_project("website", p)
    progress.finish("ws", {"success": True, "slug": slug})


def _on_ai(result: dict):
    st.session_state.ws_ai = False
    _open(st.session_state.ws_project)
    if not result.get("success"):
        st.session_state.ws_error = f"AI failed: {result.get('error', '')[-300:]}"


def _on_pub(result: dict):
    st.session_state.ws_publishing = False
    _published.clear()
    if result.get("success"):
        st.session_state.ws_toast = f"✅ Published /stories/{result['slug']} (page updates within ~10 min)"
        _open(None)
    else:
        st.session_state.ws_error = f"Publish failed: {result.get('error')}"


def _edit_open(slug: str | None):
    st.session_state.ws_edit = slug
    for k in [k for k in st.session_state if str(k).startswith("wse_")]:
        del st.session_state[k]


def _thumb(url: str) -> str:
    """The 480px variant the site's own image loader requests (so it costs no extra transformation)."""
    u = urlparse(url)
    return f"{u.scheme}://{u.netloc}/cdn-cgi/image/width=480,quality=80,format=auto{u.path}"


def _edit_view(slug: str):
    """Edit a published story in place: text, tags, per-image alt/caption/specs, order, removal.
    Images themselves can't change here — re-publish a folder with the same slug for that."""
    s = r2.get_story(slug)
    if s is None:
        st.session_state.ws_error = f"`{slug}` isn't published any more."
        _edit_open(None)
        st.rerun()
    h1, h2 = st.columns([5, 1])
    h1.subheader(f"✏️ Editing /stories/{slug}")
    if h2.button("← Back", key="wse_back", width="stretch"):
        _edit_open(None)
        st.rerun()

    def field(key, label, default="", **kw):
        st.session_state.setdefault(key, default)
        return st.text_input(label, key=key, **kw)

    title = field("wse_title", "Title", s["title"])
    desc = field("wse_description", f"Description (search results & link previews, max {DESC_MAX})", s["description"])
    c1, c2, c3 = st.columns(3)
    with c1:
        loc = field("wse_location", "Location (text only — no coordinates)", s.get("location", ""))
    with c2:
        tags = field("wse_tags", "Tags (comma-separated; the first one is the home-page filter)", ", ".join(s.get("tags", [])))
    with c3:
        st.session_state.setdefault("wse_date", datetime.date.fromisoformat(s["published_at"]))
        pub_date = st.date_input("Date", key="wse_date")
    st.session_state.setdefault("wse_body", s.get("body_md", ""))
    body = st.text_area("Story (Markdown)", key="wse_body", height=260)

    label = lambda im: Path(urlparse(im["url"]).path).name
    by_label = {label(im): im for im in s["images"]}
    order = st.multiselect("Images — in display order (the first is the cover; remove one to drop it from the story)",
                           list(by_label), default=list(by_label), key="wse_order")
    images = []
    with st.expander(f"🖼️ Alt text, captions & specs ({len(order)})", expanded=True):
        for n in order:
            im = by_label[n]
            ic, tc = st.columns([1, 6])
            ic.image(_thumb(im["url"]), width="stretch")
            st.session_state.setdefault(f"wse_alt_{n}", im.get("alt", ""))
            st.session_state.setdefault(f"wse_spc_{n}", im.get("specs", ""))
            st.session_state.setdefault(f"wse_cap_{n}", im.get("caption", ""))
            images.append({"url": im["url"],
                           "alt": tc.text_input("Alt text (required)", key=f"wse_alt_{n}"),
                           "specs": tc.text_input("Specs line", key=f"wse_spc_{n}"),
                           "caption": tc.text_input("Caption (optional)", key=f"wse_cap_{n}")})

    problems = ([] if images else ["Keep at least one image"]) \
        + ([] if title.strip() else ["Title is empty"]) \
        + ([] if desc.strip() else ["Description is empty"]) \
        + ([f"Description too long ({len(desc)}/{DESC_MAX})"] if len(desc) > DESC_MAX else []) \
        + (["Some images are missing alt text"] if any(not i["alt"].strip() for i in images) else [])
    for x in problems:
        st.caption(f"⚠️ {x}")
    if st.button("💾 SAVE & REPUBLISH", type="primary", width="stretch", disabled=bool(problems)):
        try:
            r2.update_story(slug, {
                "title": title.strip(), "description": desc.strip(), "body_md": body.strip(),
                "location": loc.strip(), "tags": [t.strip() for t in tags.split(",") if t.strip()],
                "published_at": pub_date.isoformat()},
                [{**i, "alt": i["alt"].strip(), "specs": i["specs"].strip(), "caption": i["caption"].strip()}
                 for i in images])
        except Exception as e:
            st.session_state.ws_error = f"Save failed: {e}"
            st.rerun()
        _published.clear()
        st.session_state.ws_toast = f"✅ Updated /stories/{slug} (page updates within ~10 min)"
        _edit_open(None)
        st.rerun()


def _unpublish(slug: str):
    """Take a story off the site and put it back among the projects (images + current text), so it
    can be edited, republished or deleted. R2 images stay until cleanup_r2.py."""
    dest = INPUT_DIRS["website"] / slug
    done = ARCHIVE_DIR / "website" / slug
    if done.exists() and not dest.exists():
        shutil.move(str(done), str(dest))
    r2.export_story(slug, dest)
    r2.unpublish_story(slug)
    _published.clear()
    st.session_state.ws_toast = f"↩️ `{slug}` is off the site and back in your projects below."


def _published_panel():
    stories = _published()
    with st.expander(f"📚 Published stories ({len(stories)})"):
        for s in stories:
            c1, c2, c3 = st.columns([6, 1, 1])
            c1.markdown(f"**{s['title']}** · `{s['slug']}` · {s['image_count']} images · {s['published_at']}")
            if c2.button("Edit", key=f"ws_edit_{s['slug']}"):
                _edit_open(s["slug"])
                st.rerun()
            with c3.popover("Unpublish"):
                st.caption("Takes it off the site and moves it back to your projects, to republish or delete.")
                if st.button("Confirm unpublish", key=f"ws_unpub_{s['slug']}", type="primary"):
                    _unpublish(s["slug"])
                    st.rerun()


def render_website_ui():
    _init()
    if st.session_state.ws_toast:
        st.toast(st.session_state.ws_toast)
        st.session_state.ws_toast = ""
    if st.session_state.ws_error:
        e1, e2 = st.columns([10, 1])
        e1.error(st.session_state.ws_error)
        if e2.button("✕", key="ws_err_x"):
            st.session_state.ws_error = ""
            st.rerun()
    if not r2.configured():
        st.warning("R2 isn't configured — fill in the `R2_*` values in `.env` (see `.env.example`).", icon="🔐")
        return
    if st.session_state.ws_ai:
        live_progress("ws_ai", "🤖 Drafting your story…", _on_ai)
        st.stop()
    if st.session_state.ws_publishing:
        live_progress("ws", "🚀 Publishing to the website…", _on_pub)
        st.stop()

    if st.session_state.ws_edit:
        _edit_view(st.session_state.ws_edit)
        return

    p = st.session_state.ws_project
    if not p or not Path(p).exists():
        picked = render_project_picker("website")
        if picked:
            _open(picked)
            st.rerun()
        try:
            _published_panel()
        except Exception as e:
            st.caption(f"Couldn't load published stories: {e}")
        return

    p = Path(p)
    d = _draft(p)
    imgs = [i.name for i in media_files(p, "website")]
    h1, h3, h2 = st.columns([5, 1, 1])
    h1.subheader(f"🌐 {p.name}")
    with h3.popover("🗑 Delete"):
        st.caption("Deletes this project folder and its images from this computer (not from the site).")
        if st.button("Confirm delete", key="ws_del", type="primary"):
            shutil.rmtree(p, ignore_errors=True)
            _open(None)
            st.session_state.ws_toast = f"🗑 Deleted {p.name}"
            st.rerun()
    if h2.button("← Back", key="ws_back", width="stretch"):
        _open(None)
        st.rerun()

    names = st.multiselect("Images — in display order (the first is the cover)", imgs,
                           default=[n for n in d.get("names", []) if n in imgs] or imgs, key="wsw_names")
    cols = st.columns(6)
    for i, n in enumerate(names[:12]):
        cols[i % 6].image(str(p / n), width="stretch")

    notes = st.text_input("🗒️ AI context", key="wsw_notes",
                          placeholder="where, when, what the story is about — facts the AI shouldn't invent")
    st.caption("Fields below are always editable; the AI button fills title, description, story and any empty alt texts.")
    ai = st.button("🤖 Draft with AI (replaces title, description and story)", disabled=not names)

    def field(key, label, default="", **kw):
        st.session_state.setdefault(key, d.get(key[4:], default))
        return st.text_input(label, key=key, **kw)

    title = field("wsw_title", "Title")
    slug = field("wsw_slug", "URL slug", _slugify(p.name), help="Becomes /stories/<slug>. Re-using a published slug updates that story.")
    desc = field("wsw_description", f"Description (search results & link previews, max {DESC_MAX})")
    c1, c2, c3 = st.columns(3)
    with c1:
        loc = field("wsw_location", "Location (text only — no coordinates)")
    with c2:
        tags = field("wsw_tags", "Tags (comma-separated)")
    with c3:
        st.session_state.setdefault("wsw_published_at", datetime.date.fromisoformat(
            d.get("published_at") or datetime.date.today().isoformat()))
        pub_date = st.date_input("Date", key="wsw_published_at")
    st.session_state.setdefault("wsw_body_md", d.get("body_md", ""))
    body = st.text_area("Story (Markdown)", key="wsw_body_md", height=260)

    alts, caps, specs = {}, {}, {}
    with st.expander(f"🖼️ Alt text & captions ({len(names)})", expanded=True):
        for n in names:
            ic, tc = st.columns([1, 6])
            ic.image(str(p / n), width="stretch")
            st.session_state.setdefault(f"wsw_alt_{n}", d.get("alts", {}).get(n, ""))
            st.session_state.setdefault(f"wsw_cap_{n}", d.get("captions", {}).get(n, ""))
            st.session_state.setdefault(f"wsw_spc_{n}", d.get("specs", {}).get(n, ""))
            alts[n] = tc.text_input("Alt text (required)", key=f"wsw_alt_{n}")
            specs[n] = tc.text_input("Specs line (blank = from the photo's EXIF)", key=f"wsw_spc_{n}")
            caps[n] = tc.text_input("Caption (optional)", key=f"wsw_cap_{n}")

    cur = {"names": names, "title": title, "slug": slug, "description": desc, "location": loc,
           "tags": tags, "published_at": pub_date.isoformat(), "body_md": body, "alts": alts, "captions": caps, "specs": specs}
    _save(p, cur)

    if ai:
        st.session_state.ws_ai = True
        start_publish_thread(fn=lambda: _ai_job(p, names, notes), platform="ws_ai")
        st.rerun()

    problems = ([] if names else ["Pick at least one image"]) \
        + ([] if title.strip() else ["Title is empty"]) \
        + ([] if SLUG_RE.match(slug) else ["Slug must be lowercase letters, digits and dashes (e.g. iceland-2025)"]) \
        + ([] if desc.strip() else ["Description is empty"]) \
        + ([f"Description too long ({len(desc)}/{DESC_MAX})"] if len(desc) > DESC_MAX else []) \
        + ([f"{sum(1 for n in names if not alts[n].strip())} image(s) missing alt text"]
           if any(not alts[n].strip() for n in names) else [])
    for x in problems:
        st.caption(f"⚠️ {x}")
    try:
        if any(s["slug"] == slug for s in _published()):
            st.info(f"`{slug}` is already published — publishing will update it.", icon="♻️")
    except Exception:
        pass
    if st.button("🚀 PUBLISH TO WEBSITE", type="primary", width="stretch", disabled=bool(problems)):
        st.session_state.ws_publishing = True
        start_publish_thread(fn=lambda: _publish_job(p, slug, cur, names), platform="ws")
        st.rerun()
