"""ui/music_ui.py — 🎵 pick an Instagram-library track for a reel (reel editor + week planner)."""

import streamlit as st

import src.ig_music as ig_music


@st.cache_data(ttl=3600, show_spinner="Searching Instagram music…")
def _search(q: str) -> list[dict]:
    return ig_music.search(q)


def music_picker(state: dict, key: str, on_change=None) -> None:
    """Edits state["music"] in place (None = no music)."""
    with st.expander(ig_music.label(state.get("music")) or "🎵 Add Instagram music",
                     expanded=False):
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
            if c3.button("✕ Remove", key=f"{key}_rm", width="stretch"):
                state["music"] = None
                on_change and on_change()
                st.rerun()
            st.caption("Music starts from Instagram's default point; it can't be previewed on the "
                       "reel before posting.")
            return
        q = st.text_input("Search song or artist (empty = trending)", key=f"{key}_q")
        try:
            res = _search(q)
        except Exception as e:
            st.error(f"Music search failed: {e}")
            return
        if not res:
            st.caption("No licensed tracks found — try another search.")
        for i, a in enumerate(res[:15]):
            c1, c2, c3 = st.columns([3, 3, 1])
            c1.markdown(f"**{a['title']}**  \n{a['artist']} · {a['seconds'] // 60}:{a['seconds'] % 60:02d}")
            if a["preview"]:
                c2.audio(a["preview"])
            elif a["link"]:
                c2.markdown(f"[▶ preview]({a['link']})")
            if c3.button("Use", key=f"{key}_use_{i}", width="stretch"):
                state["music"] = {"audio_id": a["audio_id"], "title": a["title"],
                                  "artist": a["artist"], "audio_volume": 100, "video_volume": 0}
                on_change and on_change()
                st.rerun()
