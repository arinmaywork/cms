"""src/ig_text.py — default hashtags & signature for new Instagram posts (editable in the UI)."""

from pathlib import Path

from src.fsutil import load_json

TEXT_DEFAULTS_FILE = Path(__file__).resolve().parent.parent / ".queue" / "ig_text_defaults.json"
TEXT_DEFAULTS = {
    "hashtags": "#photography #fujifilm #captureone #portrait #portraiture #people #life",
    "signature": "More images @ 𝙗𝙚𝙝𝙖𝙣𝙘𝙚.𝙣𝙚𝙩/𝙖𝙧𝙞𝙣𝙢𝙖𝙮\nExplore the site @ 𝙘𝙖𝙥𝙩𝙪𝙧𝙚.𝙖𝙙𝙖𝙩𝙩𝙖.𝙞𝙣",
}


def text_defaults() -> dict:
    d = load_json(TEXT_DEFAULTS_FILE, {})
    return {**TEXT_DEFAULTS, **(d if isinstance(d, dict) else {})}


def append_defaults(caption: str) -> str:
    td = text_defaults()
    return "\n\n".join(x for x in [caption.strip(), td["signature"].strip(), td["hashtags"].strip()] if x)
