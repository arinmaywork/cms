"""
ui/behance_ui.py
"""

import pathlib
import threading
import subprocess
import sys
import os
import streamlit as st
from PIL import Image

from src.ai_generator import (
    generate_title_text, generate_image_text, generate_footer_text,
)
from src.history_manager import get_last_n, save_entry
from src.behance_publisher import publish_to_behance
from src.file_queue import pop_one as fq_pop_one
from ui.progress_widget import start_publish_thread, render_progress
from ui.folder_picker import render_folder_picker
import src.progress as progress

VALID_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

COLOURS = {
    "Grey":   "#7f8c8d", "Black":  "#000000", "White":  "#ffffff",
    "Red":    "#e74c3c", "Orange": "#e67e22", "Yellow": "#f1c40f",
    "Green":  "#27ae60", "Teal":   "#16a085", "Blue":   "#2980b9",
    "Purple": "#8e44ad", "Pink":   "#e91e63", "Brown":  "#795548",
}
FONTS = ["Helvetica", "Arial", "Georgia", "Courier New",
         "Trebuchet MS", "Verdana", "Times New Roman"]
SIZES = ["20pt", "16pt", "10pt", "12pt", "14pt", "18pt",
         "22pt", "26pt", "32pt", "40pt"]

# Per-slot defaults: (font, size, colour_name)
SLOT_DEFAULTS: dict[str, tuple[str, str, str]] = {
    "title":  ("Helvetica", "20pt", "Grey"),
    "footer": ("Helvetica", "20pt", "Grey"),
    "_img":   ("Helvetica", "12pt", "Grey"),
}

def _slot_defaults(slot: str) -> tuple[str, str, str]:
    if slot in SLOT_DEFAULTS:
        return SLOT_DEFAULTS[slot]
    if slot.startswith("img_"):
        return SLOT_DEFAULTS["_img"]
    return ("Helvetica", "16pt", "Grey")


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
    from src.natsort import natkey
    try:
        return sorted((f for f in folder.iterdir() if f.suffix.lower() in VALID_EXTS),
                      key=lambda f: natkey(f.name))
    except OSError:
        return []


def _init():
    for k, v in {
        "bh_project": None,
        "bh_status": "", "bh_status_type": "info",
        "bh_publishing": False,
        "bh_ai_error": "",
        "bh_ai_running": False,
        "bh_ai_slot": "",
        "bh_queue": [],
    }.items():
        if k not in st.session_state:
            st.session_state[k] = v


def _set_status(msg, kind="info"):
    st.session_state.bh_status      = msg
    st.session_state.bh_status_type = kind


def _clear(images=None):
    st.session_state.bh_project     = None
    st.session_state.bh_status      = ""
    st.session_state.bh_publishing  = False
    st.session_state.bh_ai_error    = ""
    st.session_state.bh_ai_running  = False
    st.session_state.bh_ai_slot     = ""
    for slot in ["title", "footer"] + ([f"img_{p.name}" for p in (images or [])]):
        _reset(slot)


def _assemble(images: list[pathlib.Path]) -> str:
    parts = []
    v = _val("title").strip()
    if v: parts.append(v)
    for img in images:
        v = _val(f"img_{img.name}").strip()
        parts.append(f'<div class="img-section">{v}</div>')
    v = _val("footer").strip()
    if v: parts.append(v)
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


# ── Format panel ──────────────────────────────────────────────────────────────
def _format_panel(slot: str):
    def _append(snippet):
        cur = _val(slot)
        st.session_state[f"bh_{slot}_val"] = cur + ("\n" if cur.strip() else "") + snippet
        st.session_state[f"bh_{slot}_gen"] = _gen(slot) + 1

    c = st.columns([1,1,1,1,1,1,1,2,1])
    _df, _ds, _dc = _slot_defaults(slot)
    _dhex = COLOURS[_dc]
    _base_style = f"font-family:{_df};font-size:{_ds};color:{_dhex}"
    labels_snippets = [
        ("H1", f'<h1 style="{_base_style}">Heading 1</h1>'),
        ("H2", f'<h2 style="{_base_style}">Heading 2</h2>'),
        ("H3", f'<h3 style="{_base_style}">Heading 3</h3>'),
        ("¶",  f'<p style="{_base_style}">Paragraph text here.</p>'),
        ("B",  f'<strong style="{_base_style}">bold text</strong>'),
        ("I",  f'<em style="{_base_style}">italic text</em>'),
        ("U",  f'<span style="{_base_style};text-decoration:underline">underlined text</span>'),
        ("• List", f'<ul style="{_base_style}">\n  <li>Item one</li>\n  <li>Item two</li>\n</ul>'),
        ("─",  "<hr>"),
    ]
    for col, (label, snippet) in zip(c, labels_snippets):
        if col.button(label, key=f"fmt_{slot}_{label}", width="stretch"):
            _append(snippet); st.rerun()

    _def_font, _def_size, _def_col = _slot_defaults(slot)
    sc = st.columns([2, 2, 2, 2])
    with sc[0]:
        font = st.selectbox("Font", FONTS, index=FONTS.index(_def_font), key=f"fmt_{slot}_font", label_visibility="collapsed")
    with sc[1]:
        size = st.selectbox("Size", SIZES, index=SIZES.index(_def_size), key=f"fmt_{slot}_size", label_visibility="collapsed")
    with sc[2]:
        colour_name = st.selectbox("Colour", list(COLOURS.keys()), index=list(COLOURS.keys()).index(_def_col), key=f"fmt_{slot}_col", label_visibility="collapsed")
    with sc[3]:
        if st.button("Insert styled text →", key=f"fmt_{slot}_ins", width="stretch"):
            hex_ = COLOURS[colour_name]
            parts = [f"color:{hex_}", f"font-family:{font}", f"font-size:{size}"]
            _append(f'<p><span style="{";".join(parts)}">Your styled text here</span></p>')
            st.rerun()

    swatch = "".join(f'<span title="{n}" style="display:inline-block;width:18px;height:12px;background:{h};border-radius:2px;margin:1px"></span>' for n, h in COLOURS.items())
    st.markdown(f'<div style="margin:2px 0 6px">{swatch}</div>', unsafe_allow_html=True)

    with st.expander("🔗 Insert Link", expanded=False):
        lc1, lc2, lc3 = st.columns([2, 3, 1])
        with lc1:
            link_text = st.text_input("Link label", key=f"fmt_{slot}_link_text", placeholder="e.g. Visit my website", label_visibility="collapsed")
        with lc2:
            link_url = st.text_input("URL", key=f"fmt_{slot}_link_url", placeholder="https://example.com", label_visibility="collapsed")
        with lc3:
            if st.button("Insert →", key=f"fmt_{slot}_link_ins", width="stretch"):
                _lt  = link_text.strip() or link_url.strip() or "Link"
                _lu  = link_url.strip()
                if not _lu.startswith(("http://","https://")):
                    _lu = "https://" + _lu if _lu else "#"
                _def_font2, _def_size2, _def_col2 = _slot_defaults(slot)
                _hex2 = COLOURS[_def_col2]
                _style2 = f"font-family:{_def_font2};font-size:{_def_size2};color:{_hex2};text-decoration:underline"
                _append(f'<p><a href="{_lu}" target="_blank" style="{_style2}">{_lt}</a></p>')
                st.rerun()


# ── Editor ────────────────────────────────────────────────────────────────────
def _editor(slot: str, placeholder: str, height: int = 200) -> str:
    _format_panel(slot)
    widget_key = f"bh_{slot}_{_gen(slot)}"
    current    = _val(slot)

    result = st.text_area(label=slot, value=current, key=widget_key, height=height, placeholder=placeholder, label_visibility="collapsed")
    if result != current:
        st.session_state[f"bh_{slot}_val"] = result

    _lh = "1.3" if slot.startswith("img_") else "1.6"
    if result.strip():
        st.components.v1.html(
            f"<div style='font-family:-apple-system,sans-serif;font-size:14px;line-height:{_lh};padding:10px 14px;border:1px solid #e5e7eb;border-radius:6px;background:#fff'>{result}</div>",
            height=min(300, max(80, result.count("<p") * 46 + result.count("<h") * 44 + result.count("<li") * 28 + 60)),
            scrolling=True,
        )
    return result


# ── Main render ───────────────────────────────────────────────────────────────
def render_behance_ui():
    _init()
    st.header("🎨 Behance Publisher")

    while True:
        new_path = fq_pop_one("behance")
        if not new_path: break
        if str(new_path) not in [str(p) for p in st.session_state.bh_queue]:
            st.session_state.bh_queue.append(new_path)

    if not st.session_state.bh_project and st.session_state.bh_queue:
        st.session_state.bh_project = st.session_state.bh_queue.pop(0)
        st.session_state.bh_ai_error = ""
        for slot in ["title", "footer"]:
            _reset(slot)
        _set_status(f"✅ Loaded from queue: **{st.session_state.bh_project.name}**", "info")
        st.rerun()

    if st.session_state.bh_queue:
        with st.sidebar:
            st.divider()
            st.info(f"📁 **{len(st.session_state.bh_queue)}** more projects pending.")
            for i, p in enumerate(st.session_state.bh_queue[:5]):
                st.caption(f"{i+1}. {p.name}")

    with st.sidebar:
        st.divider()
        st.header("🔑 Authentication")
        from src.behance_publisher import STATE_FILE
        if STATE_FILE.exists():
            st.success("Session saved")
        else:
            st.warning("No session found")
        
        if st.button("🔄 Refresh Behance Login", width="stretch"):
            script = pathlib.Path(__file__).resolve().parent.parent / "save_behance_session.py"
            if sys.platform == "darwin":
                 subprocess.Popen(["open", "-a", "Terminal", str(script)])
                 st.info("Check your terminal to complete login.")
            else:
                 subprocess.Popen([sys.executable, str(script)], creationflags=subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0)
                 st.info("A new window should open for login.")

    import json as _json, glob as _glob
    _queue_dir  = _AI_LOG_FILE.parent
    _done_files = list(_queue_dir.glob("ai_done_*.json"))
    if _done_files:
        _got_result = False
        for _df in _done_files:
            try:
                _res = _json.loads(_df.read_text(encoding="utf-8"))
                if _res.get("text", "").strip():
                    _bump(_res["slot"], _res["text"])
                    st.session_state.bh_ai_error = ""
                    _got_result = True
                elif _res.get("error"):
                    st.session_state.bh_ai_error = _res["error"]
                    _got_result = True
            except Exception: pass
            finally:
                try: _df.unlink(missing_ok=True)
                except Exception: pass
        st.session_state.bh_ai_running = False
        st.session_state.bh_ai_slot    = ""
        if _got_result: st.rerun()

    ai_err = st.session_state.get("bh_ai_error", "")
    if ai_err:
        st.error(f"**AI Error:** {ai_err}", icon="❌")
        if st.button("✕ Dismiss", key="bh_dismiss_err"):
            st.session_state.bh_ai_error = ""
            st.rerun()

    if st.session_state.bh_ai_running:
        slot_label = st.session_state.bh_ai_slot.replace("img_", "image: ")
        st.info(f"⏳ Gemini is generating **{slot_label}** — page updates every 3s…")
        try:
            log_lines = _AI_LOG_FILE.read_text().strip().split("\n") if _AI_LOG_FILE.exists() else []
            last_lines = [l for l in log_lines if l.strip()][-8:]
            if last_lines: st.code("\n".join(last_lines), language=None)
        except Exception: pass

    _pub_flag = pathlib.Path(__file__).resolve().parent.parent / ".queue" / "behance_publishing.flag"
    if _pub_flag.exists() and not st.session_state.bh_publishing:
        # Flag exists from a previous run. Check if a real publisher is running
        # by looking at the progress file. If it's not active, the flag is stale
        # (left over from a crash) and would lock the UI indefinitely.
        _prog_state = progress.read("behance")
        if _prog_state.get("active", False):
            st.session_state.bh_publishing = True
        else:
            st.warning(
                "⚠️ A stale publishing flag was found (possibly from a previous crash). "
                "Click **Clear Stuck State** to reset.",
                icon="🛑",
            )
            if st.button("🧹 Clear Stuck State", key="bh_clear_stale_flag"):
                try:
                    _pub_flag.unlink(missing_ok=True)
                except Exception:
                    pass
                progress.clear("behance")
                st.rerun()
    if st.session_state.bh_publishing:
        st.subheader("🚀 Publishing to Behance…")
        state = progress.read("behance")
        steps = state.get("steps", [])
        active = state.get("active", False)
        if not steps and not active:
            st.info("⏳ Publisher starting up… please wait")
            st.stop()
        if steps:
            done_n = sum(1 for s in steps if s["status"] == "done")
            pct    = done_n / len(steps)
            st.progress(pct, text=f"{done_n}/{len(steps)} steps complete")
            for s in steps:
                icon = {"pending":"⬜","active":"🔄","done":"✅","error":"❌"}.get(s["status"],"⬜")
                detail = f" — {s['detail']}" if s.get("detail") else ""
                st.markdown(f"{icon} **{s['label']}**{detail}")
        if not active and steps:
            result = state.get("result")
            error  = state.get("error")
            st.session_state.bh_publishing = False
            if result and result.get("success"):
                _set_status(f"✅ Published! {result.get('url','')}", "success")
                _clear()
            else:
                err_msg = error or (result or {}).get("error", "Unknown error")
                _set_status(f"❌ {err_msg}", "error")
            st.rerun()
        st.stop()

    if st.session_state.bh_status:
        getattr(st, st.session_state.bh_status_type)(st.session_state.bh_status)

    if not st.session_state.bh_project:
        st.markdown("### ⏳ Waiting for a project…")
        render_folder_picker("behance")
        return

    project = st.session_state.bh_project
    images  = _get_images(project)
    if not images:
        st.error(f"No images found in `{project}`")
        if st.button("Clear", key="bh_clear_empty"): _clear(); st.rerun()
        return

    st.subheader(f"`{project.name}`")
    notes = st.text_input("🗒️ AI context:", key="bh_notes", placeholder="e.g. editorial photography portfolio")
    ai_busy = st.session_state.bh_ai_running
    st.divider()

    st.markdown("### 1️⃣ Project Title & Introduction")
    c1, _ = st.columns([1, 5])
    with c1:
        if st.button("🤖 AI Generate", key="bh_gen_title", width="stretch", disabled=ai_busy):
            _run_ai_in_background("title", lambda: generate_title_text("behance", project.name, notes))
            st.rerun()
    _editor("title", "<h1>Title</h1>", height=220)
    st.divider()

    st.markdown(f"### 2️⃣ Image Sections")
    for i, img_path in enumerate(images):
        fname = img_path.name
        slot  = f"img_{fname}"
        st.markdown(f"#### 🖼 Image {i+1} — `{fname}`")
        try:
            pil = Image.open(img_path)
            st.image(pil, width="stretch")
        except Exception: pass
        if ai_busy and st.session_state.bh_ai_slot == slot: st.info(f"⏳ Generating...")
        btn_col, _ = st.columns([1, 5])
        with btn_col:
            if st.button("🤖 AI Generate", key=f"bh_gen_img_{i}", width="stretch", disabled=ai_busy):
                _run_ai_in_background(slot, lambda p=img_path, idx=i, tot=len(images): generate_image_text("behance", project.name, p, idx, tot, notes))
                st.rerun()
        _editor(slot, f"<p>Description...</p>", height=180)
        if i < len(images) - 1: st.divider()
    st.divider()

    st.markdown("### 3️⃣ Footer / Conclusion")
    c2, _ = st.columns([1, 5])
    with c2:
        if st.button("🤖 AI Generate", key="bh_gen_footer", width="stretch", disabled=ai_busy):
            _run_ai_in_background("footer", lambda: generate_footer_text("behance", project.name, notes))
            st.rerun()
    _editor("footer", "<h2>Conclusion</h2>", height=180)
    st.divider()

    assembled = _assemble(images)
    with st.expander("👁 Full HTML Preview"):
        if assembled:
            t1, t2 = st.tabs(["Raw", "Rendered"])
            with t1: st.code(assembled, language="html")
            with t2: st.components.v1.html(f"<div style='font-family:sans-serif;padding:16px'>{assembled}</div>", height=500, scrolling=True)
    st.divider()

    content_ready = bool(assembled.strip())
    cs, cp, cc = st.columns(3)
    with cs:
        if st.button("💾 Save to History", width="stretch", key="bh_save"):
            if content_ready:
                save_entry("behance", project.name, assembled, [str(p) for p in images])
                _set_status("Saved ✓", "success"); st.rerun()
    with cp:
        if st.button("🚀 Approve & Publish", type="primary", width="stretch", key="bh_pub", disabled=not content_ready):
            save_entry("behance", project.name, assembled, [str(p) for p in images])
            progress.start("behance", ["Launching publisher…"])
            start_publish_thread(fn=lambda: publish_to_behance(project.name, assembled, list(images)), platform="behance")
            st.rerun()
    with cc:
        if st.button("🗑️ Clear", width="stretch", key="bh_clear"): _clear(images); st.rerun()
