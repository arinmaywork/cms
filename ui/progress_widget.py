"""
ui/progress_widget.py
Renders a live publish-progress panel and manages the publisher background thread.

Pattern (works with st_autorefresh every 3s):
  1. User clicks Publish → thread starts, session_state.{platform}_publishing = True
  2. Every Streamlit rerun: render_progress() paints the current step list
  3. When progress.read()["active"] is False → thread is done → show result
"""

import threading
from pathlib import Path
from typing import Callable, Any

import streamlit as st
import src.progress as progress

_ICON  = {"pending": "⬜", "active": "🔄", "done": "✅", "error": "❌"}
_COLOR = {"pending": "#666", "active": "#4da6ff", "done": "#2ecc71", "error": "#e74c3c"}


def _draw(platform: str, placeholder: Any) -> None:
    """Repaint steps inside *placeholder* from the platform's progress file."""
    state = progress.read(platform)
    steps = state.get("steps", [])
    if not steps:
        placeholder.info("⏳ Initialising publisher…")
        return

    done_n  = sum(1 for s in steps if s["status"] == "done")
    error_n = sum(1 for s in steps if s["status"] == "error")
    total   = len(steps)
    pct     = done_n / total if total else 0

    bar_col = "#e74c3c" if error_n else ("#2ecc71" if pct == 1.0 else "#4da6ff")
    bar_w   = int(pct * 100)

    rows = []
    for s in steps:
        icon   = _ICON.get(s["status"], "⬜")
        color  = _COLOR.get(s["status"], "#666")
        label  = s["label"]
        detail = s.get("detail", "")
        pulse  = " class='pulse'" if s["status"] == "active" else ""
        detail_html = (
            f"<div style='font-size:0.8em;color:#999;margin-left:1.8em'>{detail}</div>"
            if detail else ""
        )
        rows.append(
            f"<div{pulse} style='padding:5px 0;color:{color}'>"
            f"  {icon}&nbsp;<b>{label}</b>{detail_html}"
            f"</div>"
        )

    elapsed = int(state.get("started", 0) and (__import__("time").time() - state["started"]))
    elapsed_str = f"{elapsed}s" if elapsed else ""

    html = f"""
<style>
  @keyframes spin {{
    0%  {{ content:"🔄"; }}
    50% {{ content:"⏳"; }}
  }}
</style>
<div style='background:#1a1a2e;border-radius:10px;padding:16px 20px;
            font-family:monospace;border:1px solid #333'>
  <div style='font-size:0.78em;color:#888;margin-bottom:10px'>
    Publishing — {elapsed_str}
  </div>
  <div style='background:#111;border-radius:4px;height:6px;
              margin-bottom:14px;overflow:hidden'>
    <div style='width:{bar_w}%;height:100%;background:{bar_col};
                transition:width 0.5s ease'></div>
  </div>
  {''.join(rows)}
</div>"""
    placeholder.markdown(html, unsafe_allow_html=True)


def start_publish_thread(fn: Callable, platform: str) -> None:
    """
    Start *fn* in a daemon thread and record that publishing is active
    in both src.progress and st.session_state.
    Note: do NOT call progress.clear() here — the caller may have already
    written a sentinel "Launching..." step so the UI guard works correctly.
    """
    # Only clear if there are stale completed steps from a previous run
    state = progress.read(platform)
    if not state.get("active", False):
        progress.clear(platform)
    key = f"{platform}_publishing"
    st.session_state[key] = True

    # Write a file flag so the UI can detect publishing even if session state is lost
    _flag = Path(__file__).resolve().parent.parent / ".queue" / f"{platform}_publishing.flag"
    _flag.parent.mkdir(parents=True, exist_ok=True)
    _flag.write_text("publishing")

    def _run():
        try:
            fn()
        except Exception as exc:
            progress.fail(platform, str(exc))
        finally:
            # Remove flag when done
            try: _flag.unlink(missing_ok=True)
            except Exception: pass

    t = threading.Thread(target=_run, daemon=True, name=f"publisher-{platform}")
    t.start()


def render_progress(platform: str, placeholder: Any) -> dict | None:
    """
    Call on every Streamlit rerun while publishing is active.
    Draws the step list into *placeholder*.
    Returns the result dict when publishing finishes, None while still running.
    """
    state = progress.read(platform)
    _draw(platform, placeholder)

    if state.get("active", False):
        return None  # still running

    # Finished — clear session flag
    st.session_state[f"{platform}_publishing"] = False

    if state.get("error"):
        return {"success": False, "error": state["error"]}
    return state.get("result") or {"success": False, "error": "No result returned"}
