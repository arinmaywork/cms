"""
ui/components/quill_editor/__init__.py
Custom Streamlit component wrapping Quill.js.

Usage:
    from ui.components.quill_editor import quill_editor

    html = quill_editor(
        value="<p>Initial content</p>",
        key="my_editor",
        height=250,
        placeholder="Start typing…",
    )
    # html is the current editor content as an HTML string (or "" if empty)

Why a custom component?
    streamlit-quill (the pip package) ignores programmatic value= updates
    after the first render because it uses React internal state instead of
    controlled props.  This component uses raw Quill.js via window.postMessage
    and always responds to Python-sent values — making AI injection reliable.
"""

from pathlib import Path
import streamlit.components.v1 as components

_COMPONENT_DIR = Path(__file__).parent

# Declare the component once (Streamlit caches this automatically)
_quill_component = components.declare_component(
    "quill_editor",
    path=str(_COMPONENT_DIR),
)


def quill_editor(
    value: str = "",
    key: str | None = None,
    height: int = 250,
    placeholder: str = "Write here…",
) -> str:
    """
    Render a Quill rich-text editor and return the current HTML content.

    Parameters
    ----------
    value : str
        HTML content to display.  Updated reliably even after first render —
        this is the main advantage over streamlit-quill.
    key : str
        Streamlit widget key.  Use a versioned key (e.g. f"editor_{gen}")
        to force a full remount when you inject new AI content.
    height : int
        Total component height in pixels (includes toolbar ~44px).
    placeholder : str
        Placeholder shown when editor is empty.

    Returns
    -------
    str  — current HTML content, or "" if empty.
    """
    result = _quill_component(
        value=value,
        height=height,
        placeholder=placeholder,
        key=key,
        default="",
    )
    return result or ""
