"""ui/music_ui.py — 🎵 pick an Instagram-library track for a reel (AI suggestions + search)."""

import streamlit as st

import src.ig_music as ig_music


@st.cache_data(ttl=3600, show_spinner="Searching Instagram music…")
def _search(q: str) -> list[dict]:
    return ig_music.search(q)


def video_frames(video, duration: float) -> list:
    """3 frames (25/50/75 %) as JPEG files next to the video, for ✨ suggestions."""
    from pathlib import Path
    from src.ig_reel import frame_at
    video = Path(video)
    out = []
    for f in (0.25, 0.5, 0.75):
        p = video.parent / ".ig_ready" / f"{video.stem}_f{int(f * 100)}.jpg"
        if not p.exists():
            p.parent.mkdir(exist_ok=True)
            frame_at(video, max(0.1, (duration or 6) * f), p, width=720)
        if p.exists():
            out.append(p)
    return out


def _row(a: dict, key: str, i: int, state: dict, on_change) -> None:
    c1, c2, c3 = st.columns([3, 3, 1])
    score = f" · {a['score']}/10" if a.get("score") else ""
    c1.markdown(f"**{a['title']}**  \n{a['artist']} · {a['seconds'] // 60}:{a['seconds'] % 60:02d}{score}"
                + (f"  \n_{a['why']}_" if a.get("why") else ""))
    if a.get("preview"):
        c2.audio(a["preview"])
    elif a.get("link"):
        c2.markdown(f"[▶ preview on Instagram]({a['link']})")
    if c3.button("Use", key=f"{key}_use_{i}", width="stretch"):
        state["music"] = {"audio_id": a["audio_id"], "title": a["title"], "artist": a["artist"],
                          "audio_volume": 100, "video_volume": 0}
        on_change and on_change()
        st.rerun()


def music_picker(state: dict, key: str, on_change=None, sources: list | None = None,
                 caption: str = "", seconds: float = 15.0) -> None:
    """Edits state["music"] in place. sources = photo paths / frame paths / image URLs for ✨ suggestions."""
    with st.expander(ig_music.label(state.get("music")) or "🎵 Add Instagram music", expanded=False):
        if not ig_music.configured():
            st.caption("Needs a one-time Facebook Login token — follow **MUSIC_SETUP.md**, "
                       "then restart the CMS.")
            return
        m = state.get("music")
        if m:
            c1, c2, c3 = st.columns([3, 2, 1])
            c1.markdown(f"**{m['title']}** — {m['artist']}")
            m["audio_volume"] = c2.slider("Music volume", 0, 100, int(m.get("audio_volume", 100)),
                                          5, key=f"{key}_av")
            m["video_volume"] = c2.slider("Video's own sound", 0, 100, int(m.get("video_volume", 0)),
                                          5, key=f"{key}_vv", help="0 = silent reel, music only")
            if c3.button("✕ Change", key=f"{key}_rm", width="stretch"):
                state["music"] = None
                on_change and on_change()
                st.rerun()
            st.caption("Music starts from Instagram's default point; it can't be previewed on the "
                       "reel before posting.")
            return
        tabs = st.tabs(["✨ Suggested for this reel", "🔍 Search"] if sources else ["🔍 Search"])
        if sources:
            with tabs[0]:
                sk = f"{key}_ai"
                h1, h2 = st.columns([3, 1])
                want = h1.text_input("Optional direction", key=f"{key}_want", label_visibility="collapsed",
                                     placeholder="Optional: e.g. 'warm acoustic', 'moody piano', 'upbeat indie'")
                if h2.button("✨ Suggest music", key=f"{key}_sug", type="primary", width="stretch"):
                    from src import ig_music_ai
                    with st.spinner("Looking at the reel, then searching Instagram's library…"):
                        try:
                            st.session_state[sk] = ig_music_ai.suggest(sources, caption, seconds, want)
                        except Exception as e:
                            st.error(f"Suggestion failed: {e}")
                res = st.session_state.get(sk)
                if res:
                    a = res["analysis"]
                    st.info(f"**Mood:** {a.get('mood', '—')} · energy {a.get('energy', '—')} · "
                            f"pace {a.get('pace', '—')}  \n{a.get('why', '')}"
                            + (f"  \n_Avoid: {a['avoid']}_" if a.get("avoid") else ""), icon="🎧")
                    if not res["tracks"]:
                        st.caption("Nothing fitting in the licensed library — try a direction above "
                                   "or Search.")
                    for i, t in enumerate(res["tracks"]):
                        _row(t, f"{key}_s", i, state, on_change)
                else:
                    st.caption("AI looks at your photos & caption, decides the mood, searches Meta's "
                               "licensed library and ranks what fits — with a reason for each.")
        with tabs[-1]:
            q = st.text_input("Search song, artist, mood or genre (empty = trending)", key=f"{key}_q")
            try:
                res = _search(q)
            except Exception as e:
                st.error(f"Music search failed: {e}")
                return
            if not res:
                st.caption("No licensed tracks found — try another search.")
            for i, a in enumerate(res[:15]):
                _row(a, key, i, state, on_change)
