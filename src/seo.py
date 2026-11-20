"""
src/seo.py — search-friendly text for the portfolio site, shared by the CMS UI, r2.py and seo_backfill.py.

A story carries an artistic `title` ("Generations") plus plain-language facts Google can match:
  summary   what is in the photo         "an elderly man and his grandson"
  location  where                        "Pune, India"
  captured  when (free text)             "2023"
  → subtitle  (derived)  "An elderly man and his grandson, Pune, India, 2023."   shown under the title
  → seo_title (derived)  "Generations: an elderly man and his grandson, Pune | Captured by Arinmay"
description is ONE full sentence (≤ DESC_MAX); alt text describes the photo, never just repeats the title.
"""

import re

DESC_MAX = 160
TITLE_MAX = 70          # Google shows ~60 chars; a little slack for the site name
ALT_MIN = 20            # shorter than this can't describe a photograph
_END = (".", "!", "?")


def build_subtitle(summary: str = "", location: str = "", captured: str = "") -> str:
    """'An elderly man and his grandson, Pune, 2023.'  ('' when there is nothing to say)."""
    parts = [p.strip().rstrip(".,;") for p in (summary, location, captured) if p and p.strip()]
    if not parts:
        return ""
    s = ", ".join(parts)
    return s[0].upper() + s[1:] + "."


def build_seo_title(title: str, summary: str = "", location: str = "", site_name: str = "") -> str:
    """Most informative '<title>: <what>, <where> | <site>' that fits TITLE_MAX; never cut mid-phrase."""
    title = title.strip()
    tail = f" | {site_name.strip()}" if site_name and site_name.strip() else ""
    what, where = (x.strip().rstrip(".,;") for x in (summary, location))
    for detail in (f"{what}, {where}" if what and where else "", what, where):
        for t in (tail, ""):
            cand = f"{title}: {detail}{t}" if detail else f"{title}{t}"
            if len(cand) <= TITLE_MAX:
                return cand
    return title


def derive(story: dict, site_name: str = "") -> dict:
    """Fields computed from the editable ones; call before saving a story."""
    story["subtitle"] = build_subtitle(story.get("summary", ""), story.get("location", ""), story.get("captured", ""))
    story["seo_title"] = build_seo_title(story.get("title", ""), story.get("summary", ""),
                                         story.get("location", ""), site_name)
    return story


def sentence(text: str, limit: int = DESC_MAX) -> str:
    """Trim `text` to ≤ limit chars ending on a sentence boundary (never '…'); '' if no sentence fits."""
    text = re.sub(r"\s+", " ", text or "").strip()
    out = ""
    for m in re.finditer(r"[^.!?]+[.!?]+(?:\s|$)", text):
        cand = (out + " " + m.group().strip()).strip()
        if len(cand) > limit:
            break
        out = cand
    return out


def check(title: str, description: str, alts: list[tuple[str, str]], location: str = "",
          summary: str = "", captured: str = "") -> tuple[list[str], list[str]]:
    """→ (blocking problems, soft warnings). alts: [(image label, alt text)]."""
    bad, warn = [], []
    d = (description or "").strip()
    if not title.strip():
        bad.append("Title is empty")
    if not d:
        bad.append("Description is empty")
    else:
        if len(d) > DESC_MAX:
            bad.append(f"Description too long ({len(d)}/{DESC_MAX})")
        if d.endswith(("...", "…")):
            bad.append("Description is cut off (ends with …) — write one full sentence")
        elif not d.endswith(_END):
            bad.append("Description should be one full sentence ending in . ! or ?")
    t = title.strip().lower()
    for label, alt in alts:
        a = (alt or "").strip()
        if not a:
            bad.append(f"{label}: alt text is missing")
        elif a.lower() == t or a.lower().rstrip(".") == t:
            bad.append(f"{label}: alt text just repeats the title — describe what is in the photo")
        elif len(a) < ALT_MIN:
            warn.append(f"{label}: alt text is very short ({len(a)} chars) — describe the photo")
    if not (location or "").strip():
        warn.append("No location — 'street photographer <city>' is what people search for")
    if not (summary or "").strip():
        warn.append("No summary line (what is in the photo)")
    if not (captured or "").strip():
        warn.append("No capture date/year")
    return bad, warn
