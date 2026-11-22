"""ui/reelmaker_ui.py — 🎬 photos → reel (style, motion, transitions, look, cards, preview)."""

import traceback
from pathlib import Path
from typing import Callable

import streamlit as st

import src.ig_slideshow as ss
import src.progress as progress
from ui.progress_widget import live_progress, start_publish_thread

NS = "ig_reelmaker"


def _job(photos: list[Path], out: Path, opts: dict, after: Callable[[], None] | None):
    progress.start(NS, ["🎨 Draw scenes", "🎞 Join + encode (full quality)"])
    try:
        def cb(msg, f):
            progress.update(NS, 0 if f < 0.99 and not msg.startswith("Joining") else 1, "active", msg)
        tmp = out.with_name("." + out.name)
        ss.render(photos, tmp, opts, cb=cb)
        tmp.replace(out)
        progress.update(NS, 0, "done", f"{len(photos)} photo(s)")
        if after:
            after()
        progress.update(NS, 1, "done", out.name)
        progress.finish(NS, {"success": True, "file": str(out)})
    except Exception:
        progress.fail(NS, traceback.format_exc()[-600:])


def _done(result: dict):
    st.session_state.rm_busy = False
    st.toast("🎬 Reel ready — add music & schedule it" if result.get("success")
             else f"Reel failed: {str(result.get('error', ''))[-200:]}")


def reel_maker(photos: list[Path], state: dict, out: Path, key: str,
               after: Callable[[], None] | None = None) -> None:
    """state: dict persisted by the caller (opts saved in state['reelmaker'])."""
    if st.session_state.get("rm_busy"):
        live_progress(NS, "🎬 Making your reel…", _done)
        st.stop()
    o = {**ss.DEFAULTS, **state.setdefault("reelmaker", {})}
    k = lambda s: f"{key}_{s}"
    c1, c2, c3 = st.columns(3)
    o["layout"] = c1.selectbox("Layout", list(ss.LAYOUTS), list(ss.LAYOUTS).index(o["layout"]),
                               format_func=ss.LAYOUTS.get, key=k("lay"))
    o["motion"] = c2.selectbox("Motion", list(ss.MOTIONS), list(ss.MOTIONS).index(o["motion"]),
                               format_func=ss.MOTIONS.get, key=k("mot"))
    o["look"] = c3.selectbox("Look", list(ss.LOOKS), list(ss.LOOKS).index(o["look"]),
                             format_func=ss.LOOKS.get, key=k("look"))
    c1, c2, c3, c4 = st.columns(4)
    o["transition"] = c1.selectbox("Transition", ss.TRANSITIONS, ss.TRANSITIONS.index(o["transition"]),
                                   format_func=lambda t: {"mix": "✨ Curated mix", "cut": "Hard cut"}.get(t, t),
                                   key=k("tr"))
    o["sec"] = c2.slider("Seconds per photo", 1.0, 6.0, float(o["sec"]), 0.1, key=k("sec"))
    o["trans"] = c3.slider("Transition length", 0.2, 1.5, float(o["trans"]), 0.1, key=k("trl"),
                           disabled=o["transition"] == "cut")
    o["rhythm"] = c4.selectbox("Rhythm", ["even", "accelerate", "pulse"],
                               ["even", "accelerate", "pulse"].index(o["rhythm"]), key=k("rh"),
                               format_func={"even": "Even", "accelerate": "Accelerate (builds up)",
                                            "pulse": "Pulse (long–short)"}.get)
    with st.expander("Title / end cards & background"):
        a1, a2 = st.columns(2)
        o["intro"] = a1.text_area("Opening card (empty = none)", o["intro"], height=80, key=k("in"),
                                  placeholder="Week 47\nFaces of the city")
        o["outro"] = a2.text_area("End card (empty = none)", o["outro"], height=80, key=k("out"),
                                  placeholder="capture.adatta.in")
        b1, b2, b3 = st.columns(3)
        o["font"] = b1.selectbox("Card font", ["serif", "sans"], ["serif", "sans"].index(o["font"]), key=k("f"))
        blur = b2.toggle("Blurred photo behind cards / polaroids", o["bg"] == "blur", key=k("bb"))
        o["bg"] = "blur" if blur else b3.color_picker("Background colour", o["bg"] if o["bg"] != "blur"
                                                      else "#101010", key=k("bg"))
    state["reelmaker"] = o
    n = len(photos)
    length = ss.plan_length(n, o)
    st.caption(f"{n} photo(s) · ≈ {length:.0f} s · 1080×1920, 30 fps, high quality H.264"
               + (" · ⚠️ Instagram needs ≥ 3 s" if length < 3 else "")
               + (" · tip: reels under ~30 s get re-watched more" if length > 45 else ""))
    p1, p2 = st.columns(2)
    prev = out.with_name(f".preview_{out.stem}.mp4")
    if p1.button("👁 Quick preview (low-res)", key=k("pv"), width="stretch", disabled=n == 0):
        with st.spinner("Rendering preview…"):
            try:
                ss.render(photos, prev, o, preview=True)
            except Exception as e:
                st.error(f"Preview failed: {e}")
    if prev.exists():
        st.video(str(prev))
    if p2.button("🎬 Make the reel (full quality)", key=k("go"), type="primary", width="stretch",
                 disabled=n == 0 or length < 3):
        prev.unlink(missing_ok=True)
        st.session_state.rm_busy = True
        start_publish_thread(fn=lambda: _job(list(photos), out, dict(o), after), platform=NS)
        st.rerun()
    st.caption("Full render takes ~20–60 s per photo on the server. Afterwards you pick the 🎵 music, "
               "cover and time like any reel.")
