"""
src/ig_music_ai.py — ✨ AI music suggestions for a reel.

1. Gemini looks at the reel (contact sheet of up to 6 photos / frames) + caption and
   describes mood, energy and pace, and gives search terms suited to a licensed library.
2. Each term (plus trending) is searched with Meta's Audio API.
3. Gemini ranks the real candidates and says in one line why each fits.
"""

import io
import json
import re
import tempfile
from pathlib import Path

import requests
from PIL import Image, ImageOps

import src.ig_music as ig_music


def _sheet(sources: list) -> Path:
    ims = []
    for s in sources[:6]:
        try:
            if str(s).startswith("http"):
                im = Image.open(io.BytesIO(requests.get(str(s), timeout=20).content))
            else:
                im = Image.open(s)
            im = ImageOps.exif_transpose(im).convert("RGB")
            im.thumbnail((512, 512))
            ims.append(im)
        except Exception:
            continue
    if not ims:
        raise RuntimeError("No images to analyse")
    cols = min(3, len(ims))
    rows = (len(ims) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * 512, rows * 512), "black")
    for i, im in enumerate(ims):
        sheet.paste(im, ((i % cols) * 512 + (512 - im.width) // 2, (i // cols) * 512 + (512 - im.height) // 2))
    p = Path(tempfile.mkstemp(suffix=".jpg")[1])
    sheet.save(p, quality=85)
    return p


def _json(text: str):
    m = re.search(r"[\[{].*[\]}]", text, re.S)
    return json.loads(m.group(0)) if m else {}


def analyse(sources: list, caption: str, seconds: float) -> dict:
    from src.ai_generator import _call_vision
    sheet = _sheet(sources)
    try:
        txt, _ = _call_vision(
            "You are a music supervisor for Instagram Reels by a portrait / street photographer "
            "(Fujifilm, Capture One look). You pick music that elevates the photos, never cheesy.",
            f"These frames are from one reel (~{seconds:.0f}s). Caption: {caption[:400]!r}.\n"
            "Return ONLY JSON: {\"mood\": \"3-6 words\", \"energy\": \"low|medium|high\", "
            "\"pace\": \"slow|mid|fast\", \"why\": \"one sentence\", "
            "\"queries\": [8 short search terms that would find fitting tracks in a licensed music "
            "library — mix genres (e.g. lo-fi, ambient piano, indie folk, cinematic, jazz, "
            "synthwave), moods (e.g. nostalgic, dreamy, uplifting) and instrument words; no artist "
            "names], \"avoid\": \"one short phrase\"}", sheet, max_tokens=700)
    finally:
        sheet.unlink(missing_ok=True)
    a = _json(txt) or {}
    a.setdefault("queries", ["cinematic", "lo-fi", "ambient piano", "indie"])
    return a


def suggest(sources: list, caption: str = "", seconds: float = 15.0, extra: str = "") -> dict:
    """→ {"analysis": {...}, "tracks": [{...track, "why"}]} — tracks already exist in Meta's library."""
    a = analyse(sources, caption, seconds)
    queries = ([extra] if extra.strip() else []) + list(a["queries"])[:8] + [""]
    pool, hits = {}, {}
    for q in queries:
        try:
            for t in ig_music.search(q):
                if t["seconds"] and t["seconds"] < min(seconds, 20) * 0.8:
                    continue                                  # too short to cover the reel
                pool.setdefault(t["audio_id"], {**t, "found_by": q or "trending"})
                hits[t["audio_id"]] = hits.get(t["audio_id"], 0) + 1
        except Exception:
            continue
    cands = sorted(pool.values(), key=lambda t: -hits[t["audio_id"]])[:40]
    if not cands:
        return {"analysis": a, "tracks": []}
    from src.ai_generator import _call_text
    lst = "\n".join(f"{i}. {t['title']} — {t['artist']} ({t['seconds']}s, via '{t['found_by']}')"
                    for i, t in enumerate(cands))
    try:
        txt, _ = _call_text(
            "You are a music supervisor for Instagram Reels. Be selective and honest.",
            f"Reel mood: {a.get('mood')}; energy {a.get('energy')}; pace {a.get('pace')}; "
            f"avoid: {a.get('avoid')}. Length ~{seconds:.0f}s.\n"
            + (f"User wants: {extra}\n" if extra.strip() else "")
            + f"Candidate tracks:\n{lst}\n\nPick the best 8 (fewer if few fit). Return ONLY JSON "
            "list: [{\"i\": index, \"why\": \"max 12 words\", \"score\": 1-10}] best first.",
            max_tokens=900)
        ranked = [r for r in _json(txt) if isinstance(r, dict) and 0 <= int(r.get("i", -1)) < len(cands)]
        tracks = [{**cands[int(r["i"])], "why": r.get("why", ""), "score": r.get("score")} for r in ranked]
    except Exception:
        tracks = [{**t, "why": f"matches '{t['found_by']}'", "score": None} for t in cands[:8]]
    return {"analysis": a, "tracks": tracks}
