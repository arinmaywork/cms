"""
ui/behance_ui.py — Behance page.

The Behance publisher TYPES plain text into the editor (formatting can't
survive that), so sections are plain-text boxes — no HTML snippets.
Section text persists in <project>/.cms.json; published projects move to
archive/behance/.
"""

import html as _html
import pathlib
import threading
import time
from pathlib import Path

import streamlit as st
from PIL import Image

import src.progress as progress
from src.ai_generator import generate_footer_text, generate_image_text, generate_title_text
from src.behance_publisher import publish_to_behance
from src.history_manager import save_entry
from src.projects import archive_project, load_draft, media_files, save_draft
from ui.folder_picker import render_project_picker
from ui.progress_widget import live_progress, start_publish_thread

# ── Slot helpers ──────────────────────────────────────────────────────────────
def _val(slot):  return st.session_state.get(f"bh_{slot}_val", "")
def _gen(slot):  return st.session_state.get(f"bh_{slot}_gen", 0)

def _bump(slot, text):
    st.session_state[f"bh_{slot}_val"] = text
    st.session_state[f"bh_{slot}_gen"] = _gen(slot) + 1

def _reset(slot):
    st.session_state[f"bh_{slot}_val"] = ""
    st.session_state[f"bh_{slot}_gen"] = _gen(slot) + 1


def _get_images(folder: pathlib.Path) -> list[pathlib.Path]:
    return media_files(Path(folder), "behance")


def _init():
    for k, v in {"bh_project": None, "bh_toast": "", "bh_error": "", "bh_publishing": False,
                 "bh_ai_error": "", "bh_ai_running": False, "bh_ai_slot": ""}.items():
        st.session_state.setdefault(k, v)


def _set_status(msg, kind="info"):
    st.session_state["bh_error" if kind == "error" else "bh_toast"] = msg


def _slots(images) -> list[str]:
    return ["title", "footer"] + [f"img_{p.name}" for p in (images or [])]


def _clear(images=None):
    st.session_state.bh_project = None
    st.session_state.bh_ai_error = ""
    st.session_state.bh_ai_running = False
    st.session_state.bh_ai_slot = ""
    for slot in _slots(images):
        _reset(slot)


def _to_text(markup: str) -> str:
    from bs4 import BeautifulSoup
    return BeautifulSoup(markup or "", "html.parser").get_text("\n").strip()


def _assemble(images: list[pathlib.Path]) -> str:
    """Plain text wrapped in tags so behance_publisher._parse_content can split
    header / per-image / footer (it only reads Tag nodes)."""
    esc = lambda t: _html.escape(t.strip())
    parts = [f"<div>{esc(_val('title'))}</div>"] if _val("title").strip() else []
    parts += [f'<div class="img-section">{esc(_val(f"img_{img.name}"))}</div>' for img in images]
    if _val("footer").strip():
        parts.append(f"<div>{esc(_val('footer'))}</div>")
    return "\n".join(parts)


# ── Background AI worker ──────────────────────────────────────────────────────
import time as _time
_AI_LOG_FILE = pathlib.Path(__file__).resolve().parent.parent / ".queue" / "ai_log.txt"

def _ai_log(msg: str):
    ts = _time.strftime("%H:%M:%S")
    line = f"[{ts}] {msg}\n"
    try:
        with open(_AI_LOG_FILE, "a") as f:
            f.write(line)
    except Exception:
        pass
    print(line, end="")


def _run_ai_in_background(slot: str, fn):
    def _worker():
        import json as _json
        _ai_log(f"START slot={slot}")
        try:
            _ai_log("Calling Gemini API…")
            result = fn()
            _ai_log(f"API returned {len(result) if result else 0} chars")
            _safe = slot.replace("/","_").replace(".","_").replace(" ","_")
            _out_file = _AI_LOG_FILE.parent / f"ai_done_{_safe}.json"
            if result and result.strip():
                _out_file.write_text(
                    _json.dumps({"slot": slot, "text": result, "error": ""}),
                    encoding="utf-8"
                )
                _ai_log(f"DONE ✅")
            else:
                _out_file.write_text(
                    _json.dumps({"slot": slot, "text": "", "error": f"Empty response for {slot}"}),
                    encoding="utf-8"
                )
                _ai_log("WARN: empty result")
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            _safe = slot.replace("/","_").replace(".","_").replace(" ","_")
            (_AI_LOG_FILE.parent / f"ai_done_{_safe}.json").write_text(
                _json.dumps({"slot": slot, "text": "", "error": err}),
                encoding="utf-8"
            )
            _ai_log(f"ERROR: {err}")
        finally:
            _ai_log("Thread finished")

    try:
        _AI_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        _AI_LOG_FILE.write_text("")
        _safe_slot = slot.replace("/","_").replace(".","_").replace(" ","_")
        for _old in _AI_LOG_FILE.parent.glob(f"ai_done_{_safe_slot}.json"):
            _old.unlink(missing_ok=True)
    except Exception:
        pass

    st.session_state.bh_ai_running = True
    st.session_state.bh_ai_slot    = slot
    st.session_state.bh_ai_error   = ""
    t = threading.Thread(target=_worker, daemon=True, name=f"ai-gen-{slot}")
    t.start()


# ── Editor ────────────────────────────────────────────────────────────────────
def _editor(slot: str, placeholder: str, height: int = 160) -> str:
    key = f"bh_{slot}_{_gen(slot)}"
    cur = _val(slot)
    res = st.text_area(slot, value=cur, key=key, height=height, placeholder=placeholder,
                       label_visibility="collapsed")
    if res != cur:
        st.session_state[f"bh_{slot}_val"] = res
    return res


def _open_project(path: Path):
    st.session_state.bh_project = path
    for slot, val in load_draft(path).get("behance", {}).get("slots", {}).items():
        _bump(slot, val)


def _save_project(images):
    p = st.session_state.bh_project
    if p and Path(p).exists():
        data = load_draft(p)
        data["behance"] = {"slots": {sl: _val(sl) for sl in _slots(images)}}
        save_draft(p, data)


def _publish_job(project: Path, content: str, images: list):
    r = publish_to_behance(project.name, content, images)
    if r.get("success"):
        archive_project("behance", project)


def _on_publish_done(result: dict):
    st.session_state.bh_publishing = False
    if result.get("success"):
        st.session_state.bh_toast = f"✅ Published to Behance {result.get('url', '')}"
        p = st.session_state.bh_project
        _clear(_get_images(p) if p and Path(p).exists() else [])
    else:
        st.session_state.bh_error = (f"Publish failed: {result.get('error')} — debug "
                                     "screenshots are in .browser_state/")


@st.fragment(run_every=2)
def _ai_poll():
    if list(_AI_LOG_FILE.parent.glob("ai_done_*.json")):
        st.rerun()                                   # full run picks the result up
    slot = st.session_state.get("bh_ai_slot", "").replace("img_", "image: ")
    st.info(f"⏳ Gemini is writing **{slot}**…")


def _consume_ai_results():
    import json as _json
    got = False
    for f in _AI_LOG_FILE.parent.glob("ai_done_*.json"):
        try:
            res = _json.loads(f.read_text(encoding="utf-8"))
            if res.get("text", "").strip():
                _bump(res["slot"], _to_text(res["text"]))
                st.session_state.bh_ai_error = ""
            elif res.get("error"):
                st.session_state.bh_ai_error = res["error"]
            got = True
        except Exception:
            pass
        finally:
            f.unlink(missing_ok=True)
    if got:
        st.session_state.bh_ai_running = False
        st.session_state.bh_ai_slot = ""


# ── Page ──────────────────────────────────────────────────────────────────────
def render_behance_ui():
    _init()
    _consume_ai_results()
    if st.session_state.bh_toast:
        st.toast(st.session_state.bh_toast)
        st.session_state.bh_toast = ""
    for key in ("bh_error", "bh_ai_error"):
        if st.session_state.get(key):
            e1, e2 = st.columns([10, 1])
            e1.error(st.session_state[key])
            if e2.button("✕", key=f"x_{key}"):
                st.session_state[key] = ""
                st.rerun()

    # Resume watching a publish even after a page refresh (ignore stale >30 min)
    ps = progress.read("behance")
    if ps.get("active") and time.time() - ps.get("started", 0) < 1800:
        st.session_state.bh_publishing = True
    if st.session_state.bh_publishing:
        live_progress("behance", "🚀 Publishing to Behance…", _on_publish_done)
        st.stop()

    project = st.session_state.bh_project
    if not project or not Path(project).exists():
        picked = render_project_picker("behance")
        if picked:
            _open_project(picked)
            st.rerun()
        return

    project = Path(project)
    images = _get_images(project)
    h1, h2 = st.columns([5, 1])
    h1.subheader(f"🎨 {project.name} — {len(images)} image(s)")
    if h2.button("← Projects", key="bh_back", width="stretch"):
        _save_project(images)
        _clear(images)
        st.rerun()
    if not images:
        st.error("No images in this folder.")
        return

    if st.session_state.bh_ai_running:
        _ai_poll()
    busy = st.session_state.bh_ai_running
    notes = st.text_input("🗒️ AI context", key="bh_notes",
                          placeholder="e.g. editorial photography portfolio")

    def _ai_button(key, slot, fn):
        if st.button("🤖 AI write", key=key, disabled=busy):
            _run_ai_in_background(slot, fn)
            st.rerun()

    st.markdown("#### 1️⃣ Title & introduction")
    _ai_button("bh_gen_title", "title", lambda: generate_title_text("behance", project.name, notes))
    _editor("title", "Project title and a short introduction")

    st.markdown("#### 2️⃣ Image sections")
    for i, img in enumerate(images):
        c1, c2 = st.columns([1, 2])
        with c1:
            st.image(Image.open(img), width="stretch")
        with c2:
            st.markdown(f"**Image {i + 1}** · `{img.name}`")
            _ai_button(f"bh_gen_img_{i}", f"img_{img.name}",
                       lambda p=img, idx=i: generate_image_text("behance", project.name, p,
                                                                idx, len(images), notes))
            _editor(f"img_{img.name}", "What's in this image (optional)", height=110)

    st.markdown("#### 3️⃣ Closing text")
    _ai_button("bh_gen_footer", "footer", lambda: generate_footer_text("behance", project.name, notes))
    _editor("footer", "Conclusion / credits")
    _save_project(images)

    assembled = _assemble(images)
    with st.expander("👁 Preview"):
        st.text(_to_text(assembled.replace("</div>", "</div>\n\n")) or "(empty)")

    if st.button("🚀 APPROVE & PUBLISH", type="primary", width="stretch",
                 disabled=not assembled.strip() or busy):
        save_entry("behance", project.name, assembled, [str(p) for p in images])
        st.session_state.bh_publishing = True
        start_publish_thread(fn=lambda: _publish_job(project, assembled, list(images)),
                             platform="behance")
        st.rerun()
