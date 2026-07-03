"""
src/ai_generator.py
Uses the NEW google-genai SDK (google.genai), NOT the deprecated google.generativeai.

Install:  pip install google-genai
Free-tier daily limits (as of 2025):
  gemini-2.0-flash        — generous daily quota, good vision support
  gemini-1.5-flash        — high daily limit, reliable vision
  gemini-2.5-flash        — 20 req/day on free tier (hits quickly)
  gemini-2.5-flash-lite   — 10 RPM free

Cascade tries best→fallback automatically on 429/quota errors.
"""

import os
import time
import mimetypes
from pathlib import Path
from src.history_manager import get_last_n

# ── Model cascade: ordered best → most available ──────────────────────────────
# gemini-2.0-flash has the most generous free quota and supports vision
MODEL_CASCADE: list[str] = [
    "gemini-2.0-flash",         # best free quota, full vision support
    "gemini-1.5-flash",         # high daily limit, reliable
    "gemini-2.5-flash-lite",    # 10 RPM free
    "gemini-2.5-flash",         # only 20/day — last resort
]

_client = None


def _get_client():
    global _client
    if _client is None:
        try:
            from google import genai
        except ImportError:
            raise ImportError(
                "google-genai not installed.\n"
                "Run: pip install google-genai"
            )
        api_key = os.getenv("GEMINI_API_KEY", "")
        if not api_key:
            raise EnvironmentError(
                "GEMINI_API_KEY not set in .env\n"
                "Get a free key: https://aistudio.google.com/apikey"
            )
        _client = genai.Client(api_key=api_key)
    return _client


# ── System prompts ────────────────────────────────────────────────────────────
_IG_SYSTEM = (
    "You are an expert Instagram copywriter. "
    "Output ONLY the requested text — no preamble, no markdown fences. "
    "Use emoji naturally and sparingly. Keep language punchy and engaging."
)

_BH_SYSTEM = (
    "You are an expert Behance project copywriter. "
    "Output ONLY valid HTML using: <h1> <h2> <p> <strong> <em>. "
    "You may add inline style=\"color:#hex\" sparingly. "
    "No <html>/<head>/<body>/<style> tags. No markdown fences or preamble."
)

_IG_IMAGE_SYSTEM = (
    "You are an expert Instagram carousel copywriter. "
    "You are given an actual photograph — look at it carefully. "
    "Write 1-3 SHORT punchy sentences (max 80 words) based on what you SEE. "
    "No hashtags. No preamble. Just the slide text. Emoji OK if natural."
)

_IG_ALT_SYSTEM = (
    "You are an accessibility expert. Write a concise, descriptive 'alt text' "
    "for this image. Describe exactly what is in the photo for someone who "
    "cannot see it. Max 100 characters. No 'A photo of...', just the description."
)

_BH_IMAGE_SYSTEM = (
    "You are an expert Behance project copywriter. "
    "You are given an actual photograph — look at it carefully. "
    "Write a short paragraph (2-4 sentences, max 120 words) describing what you SEE: "
    "composition, mood, lighting, subject, technique. "
    "Output ONLY a single <p>...</p> block. <strong> OK for emphasis."
)


# ── History context ───────────────────────────────────────────────────────────
def _history(platform: str) -> str:
    entries = get_last_n(platform, 5)
    if not entries:
        return "No previous posts yet — establish the brand voice freely."
    lines = []
    for e in entries:
        snippet = e["content"][:300].replace("\n", " ")
        lines.append(f"[{e['timestamp'][:10]}] {e['project']}: {snippet}…")
    return "Style reference:\n\n" + "\n\n".join(lines)


# ── Retryable error detection ─────────────────────────────────────────────────
def _is_retryable(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(k in msg for k in [
        "429", "quota", "rate", "resource_exhausted",
        "503", "overloaded", "unavailable", "retry",
        "too many", "limit exceeded",
    ])


# ── Text-only call with cascade ───────────────────────────────────────────────
def _call_text(system: str, prompt: str, max_tokens: int = 2048) -> tuple[str, str]:
    """Call Gemini with text-only content. Returns (text, model_used)."""
    from google.genai import types
    client = _get_client()
    last_err = None

    for model_name in MODEL_CASCADE:
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    temperature=0.8,
                    top_p=0.95,
                    max_output_tokens=max_tokens,
                ),
            )
            text = response.text.strip()
            print(f"  [ai:text] {model_name} → {len(text)} chars")
            return text, model_name

        except Exception as exc:
            if _is_retryable(exc):
                print(f"  [ai:text] {model_name} quota/rate → trying next… ({exc})")
                last_err = exc
                time.sleep(0.5)
            else:
                raise

    raise RuntimeError(
        f"All models exhausted. Last error: {last_err}\n"
        f"Tried: {', '.join(MODEL_CASCADE)}\n"
        "Free tier quota may be used up for today — try again tomorrow or "
        "add billing at https://aistudio.google.com"
    )


# ── Vision call with cascade ──────────────────────────────────────────────────
def _call_vision(system: str, prompt: str, image_path: Path,
                 max_tokens: int = 512) -> tuple[str, str]:
    """Call Gemini Vision with an image + text. Returns (text, model_used)."""
    from google import genai
    from google.genai import types

    client = _get_client()

    # Resize to ≤1024px and compress — keeps payload small and fast
    try:
        from PIL import Image as _PIL
        import io as _io
        img = _PIL.open(image_path).convert("RGB")
        max_side = 1024
        w, h = img.size
        if max(w, h) > max_side:
            scale = max_side / max(w, h)
            img = img.resize((int(w * scale), int(h * scale)), _PIL.LANCZOS)
        buf = _io.BytesIO()
        img.save(buf, format="JPEG", quality=82, optimize=True)
        img_bytes = buf.getvalue()
        mime_type = "image/jpeg"
        print(f"  [ai:vision] image ready: {img.size}, {len(img_bytes)//1024}KB")
    except Exception as e:
        raise RuntimeError(f"Cannot read image {image_path.name}: {e}")

    # Build content parts for the new SDK
    image_part = types.Part.from_bytes(data=img_bytes, mime_type=mime_type)
    text_part  = types.Part.from_text(text=prompt)
    contents   = types.Content(parts=[image_part, text_part], role="user")

    last_err = None
    for model_name in MODEL_CASCADE:
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    temperature=0.75,
                    top_p=0.95,
                    max_output_tokens=max_tokens,
                ),
            )
            text = response.text.strip()
            print(f"  [ai:vision] {model_name} → {image_path.name} → {len(text)} chars")
            return text, model_name

        except Exception as exc:
            if _is_retryable(exc):
                print(f"  [ai:vision] {model_name} quota/rate → trying next… ({exc})")
                last_err = exc
                time.sleep(0.5)
            else:
                raise

    raise RuntimeError(
        f"All vision models exhausted. Last error: {last_err}\n"
        f"Tried: {', '.join(MODEL_CASCADE)}\n"
        "Free tier quota may be used up — try again tomorrow or add billing."
    )


# ── Public API ────────────────────────────────────────────────────────────────

def generate_copy(platform: str, project_name: str,
                  image_count: int, extra_notes: str = "") -> str:
    if platform == "instagram":
        post_type = ("single-image post" if image_count == 1
                     else f"carousel with {image_count} slides")
        prompt = (
            f"Project: {project_name}\nPost type: {post_type}\n"
            + (f"Notes: {extra_notes}\n" if extra_notes else "")
            + f"\n{_history(platform)}\n\n"
            "Write the full Instagram caption + hashtag block.\n"
            "Format: caption body, blank line, hashtags."
        )
        text, _ = _call_text(_IG_SYSTEM, prompt)
        return text

    elif platform == "behance":
        prompt = (
            f"Project: {project_name}\nImages: {image_count}\n"
            + (f"Notes: {extra_notes}\n" if extra_notes else "")
            + f"\n{_history(platform)}\n\n"
            "Write the full Behance description in HTML. "
            "Structure: <h1>title</h1><h2>Overview</h2><p>…</p>"
            "<h2>Process</h2><p>…</p><h2>Key Deliverables</h2><p>…</p>"
            "<h2>Conclusion</h2><p>…</p>"
        )
        html, _ = _call_text(_BH_SYSTEM, prompt)
        return html

    raise ValueError(f"Unknown platform: {platform!r}")


def generate_image_text(platform: str, project_name: str, image_path: Path,
                        idx: int, total: int, extra_notes: str = "") -> str:
    position = (f"slide {idx+1} of {total}" if platform == "instagram"
                else f"image {idx+1} of {total}")

    if platform == "instagram":
        prompt = (
            f"Project: {project_name}\nPosition: {position}\n"
            + (f"Tone/notes: {extra_notes}\n" if extra_notes else "")
            + "\nLook at this photograph carefully. Write the Instagram "
              "carousel slide text based on what you actually see."
        )
        text, _ = _call_vision(_IG_IMAGE_SYSTEM, prompt, image_path, max_tokens=300)
        return text

    elif platform == "behance":
        prompt = (
            f"Project: {project_name}\nPosition: {position}\n"
            + (f"Tone/notes: {extra_notes}\n" if extra_notes else "")
            + "\nLook at this photograph carefully. Write a Behance description "
              "based on what you actually see — composition, mood, subject, lighting."
        )
        html, _ = _call_vision(_BH_IMAGE_SYSTEM, prompt, image_path, max_tokens=400)
        return html

    raise ValueError(f"Unknown platform: {platform!r}")


def generate_alt_text(image_path: Path) -> str:
    """Generate accessibility alt-text for an image."""
    prompt = "Write concise accessibility alt-text for this image."
    text, _ = _call_vision(_IG_ALT_SYSTEM, prompt, image_path, max_tokens=150)
    return text


def generate_title_text(platform: str, project_name: str, extra_notes: str = "") -> str:
    if platform == "instagram":
        prompt = (
            f"Project: {project_name}\n"
            + (f"Notes: {extra_notes}\n" if extra_notes else "")
            + "\nWrite a compelling 1-2 sentence opening hook for an Instagram "
              "carousel. No hashtags. Just the hook."
        )
        text, _ = _call_text(_IG_SYSTEM, prompt, max_tokens=200)
        return text

    elif platform == "behance":
        prompt = (
            f"Project: {project_name}\n"
            + (f"Notes: {extra_notes}\n" if extra_notes else "")
            + "\nWrite: <h1>project title</h1><h2>Overview</h2>"
              "<p>2-3 sentence intro</p>. Keep it concise."
        )
        html, _ = _call_text(_BH_SYSTEM, prompt, max_tokens=400)
        return html

    raise ValueError(f"Unknown platform: {platform!r}")


def generate_footer_text(platform: str, project_name: str, extra_notes: str = "") -> str:
    if platform == "instagram":
        prompt = (
            f"Project: {project_name}\n"
            + (f"Notes: {extra_notes}\n" if extra_notes else "")
            + "\nWrite 10-15 relevant hashtags only. No caption text."
        )
        text, _ = _call_text(_IG_SYSTEM, prompt, max_tokens=200)
        return text

    elif platform == "behance":
        prompt = (
            f"Project: {project_name}\n"
            + (f"Notes: {extra_notes}\n" if extra_notes else "")
            + "\nWrite: <h2>Conclusion</h2><p>2-3 sentence closing paragraph</p>."
        )
        html, _ = _call_text(_BH_SYSTEM, prompt, max_tokens=300)
        return html

    raise ValueError(f"Unknown platform: {platform!r}")


def generate_behance_metadata(
    project_name: str,
    image_paths: list,
    extra_notes: str = "",
) -> dict:
    """
    Analyse up to 4 images with Gemini Vision and return project metadata:
    {
      "title":       str,           # project title
      "description": str,           # short project description (plain text, max 200 chars)
      "tags":        list[str],     # 8-10 relevant tags (no #, lowercase)
      "cover_index": int,           # index of the best cover image (0-based)
    }
    """
    from pathlib import Path as _Path
    import json as _json

    from google.genai import types

    client = _get_client()

    # Sample up to 4 images for analysis (spread evenly)
    paths = [_Path(p) for p in image_paths]
    n = len(paths)
    if n <= 4:
        sample = list(range(n))
    else:
        step = n / 4
        sample = [int(i * step) for i in range(4)]

    # Build multimodal content: all sampled images + prompt
    parts = []
    for idx in sample:
        try:
            from PIL import Image as _PIL
            import io as _io
            img = _PIL.open(paths[idx]).convert("RGB")
            max_side = 768
            w, h = img.size
            if max(w, h) > max_side:
                scale = max_side / max(w, h)
                img = img.resize((int(w * scale), int(h * scale)), _PIL.LANCZOS)
            buf = _io.BytesIO()
            img.save(buf, format="JPEG", quality=80)
            parts.append(types.Part.from_bytes(
                data=buf.getvalue(), mime_type="image/jpeg"
            ))
            parts.append(types.Part.from_text(
                text=f"[Image {idx+1} of {n}: {paths[idx].name}]"
            ))
        except Exception as e:
            print(f"  [ai:metadata] Could not load image {paths[idx].name}: {e}")

    prompt = f"""You are a Behance project metadata expert.
Project folder name: {project_name}
{f"Additional notes: {extra_notes}" if extra_notes else ""}

Analyse the {len(sample)} sample images shown above (from a set of {n} total images).
Return a JSON object with EXACTLY these keys:
{{
  "title": "A compelling project title (5-8 words, title case)",
  "description": "A concise project description (max 200 characters, plain text)",
  "tags": ["tag1", "tag2", "tag3", "tag4", "tag5", "tag6", "tag7", "tag8"],
  "cover_index": 0
}}

Rules:
- tags: 8 relevant lowercase tags without # (e.g. "photography", "portrait", "street")
- cover_index: index (0 to {n-1}) of the image with highest visual impact for a cover
- Output ONLY the JSON object, no markdown fences, no explanation
"""
    parts.append(types.Part.from_text(text=prompt))
    contents = types.Content(parts=parts, role="user")

    last_err = None
    response = None  # initialise so the except fallback can safely reference it
    for model_name in MODEL_CASCADE:
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=contents,
                config=types.GenerateContentConfig(
                    temperature=0.4,
                    max_output_tokens=400,
                ),
            )
            raw = response.text.strip()
            # Strip markdown fences if present
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            data = _json.loads(raw.strip())
            # Validate and sanitise
            title       = str(data.get("title", project_name))[:100]
            description = str(data.get("description", ""))[:200]
            tags        = [str(t).lower().strip() for t in data.get("tags", []) if t][:10]
            cover_index = int(data.get("cover_index", 0))
            cover_index = max(0, min(cover_index, n - 1))
            print(f"  [ai:metadata] {model_name} → title='{title}', cover={cover_index}, tags={tags}")
            return {
                "title":       title,
                "description": description,
                "tags":        tags,
                "cover_index": cover_index,
            }
        except Exception as exc:
            if _is_retryable(exc):
                last_err = exc
                time.sleep(0.5)
                continue
            
            # If it's a non-retryable error, try to extract from response.text if it exists
            # (e.g., the AI returned a response but it wasn't valid JSON)
            try:
                import re
                # response is initialised to None before the loop, so this is always safe
                raw_text = getattr(response, "text", "") if response is not None else ""
                
                if raw_text:
                    title_m  = re.search(r'"title"\s*:\s*"([^"]+)"', raw_text)
                    tags_m   = re.findall(r'"([a-z][a-z\s]+)"', raw_text)
                    return {
                        "title":       title_m.group(1) if title_m else project_name,
                        "description": "",
                        "tags":        tags_m[:8] if tags_m else _default_tags(project_name),
                        "cover_index": 0,
                    }
            except Exception:
                pass
            
            # If manual extraction also fails or response isn't there, re-raise original
            raise exc

    raise RuntimeError(f"All models exhausted for metadata. Last: {last_err}")


def _default_tags(project_name: str) -> list[str]:
    """Generate sensible default tags from project name when AI fails."""
    base = ["photography", "art", "creative", "portfolio", "visual"]
    words = [w.lower() for w in project_name.replace("-","_").replace(" ","_").split("_")
             if len(w) > 2 and w.lower() not in base]
    return (words + base)[:10]


# ═══════════════════════════════════════════════════════════════════════════════
# YouTube
# ═══════════════════════════════════════════════════════════════════════════════

_YT_SYSTEM = (
    "You are an expert YouTube strategist and copywriter. You optimise for "
    "click-through AND watch satisfaction — compelling but never clickbait-dishonest. "
    "Output ONLY the requested JSON. No markdown fences, no preamble."
)


def _extract_video_frames(video_path: Path, n: int = 4) -> list[bytes]:
    """
    Sample *n* JPEG frames spread across the video using ffmpeg.
    Returns [] if ffmpeg/ffprobe are unavailable (caller falls back to
    text-only generation).
    """
    import shutil as _shutil
    import subprocess as _sp
    import tempfile as _tmp

    ffmpeg  = _shutil.which("ffmpeg")
    ffprobe = _shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        print("  [ai:yt] ffmpeg/ffprobe not found — falling back to text-only")
        return []

    try:
        out = _sp.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
            capture_output=True, text=True, timeout=30,
        )
        duration = float(out.stdout.strip())
    except Exception as e:
        print(f"  [ai:yt] ffprobe failed: {e}")
        return []

    frames: list[bytes] = []
    # Sample at 12%, 35%, 60%, 85% — skips intro/outro black frames
    positions = [0.12, 0.35, 0.60, 0.85][:n]
    with _tmp.TemporaryDirectory() as td:
        for i, pos in enumerate(positions):
            ts = max(0.5, duration * pos)
            fp = Path(td) / f"frame_{i}.jpg"
            try:
                _sp.run(
                    [ffmpeg, "-ss", f"{ts:.2f}", "-i", str(video_path),
                     "-frames:v", "1", "-q:v", "4",
                     "-vf", "scale='min(1024,iw)':-2",
                     "-y", str(fp)],
                    capture_output=True, timeout=60,
                )
                if fp.exists() and fp.stat().st_size > 0:
                    frames.append(fp.read_bytes())
            except Exception as e:
                print(f"  [ai:yt] frame extract failed @{ts:.0f}s: {e}")
    print(f"  [ai:yt] extracted {len(frames)} frame(s) from {video_path.name}")
    return frames


def extract_thumbnail_frame(video_path: Path, position: float = 0.35) -> Path | None:
    """
    Extract a single high-quality frame as a thumbnail candidate.
    Saves next to the video as .thumb_auto_{stem}.jpg. Returns path or None.
    """
    import shutil as _shutil
    import subprocess as _sp

    ffmpeg  = _shutil.which("ffmpeg")
    ffprobe = _shutil.which("ffprobe")
    if not ffmpeg:
        return None
    ts = 3.0
    if ffprobe:
        try:
            out = _sp.run(
                [ffprobe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
                capture_output=True, text=True, timeout=30,
            )
            ts = max(0.5, float(out.stdout.strip()) * position)
        except Exception:
            pass
    out_path = video_path.parent / f".thumb_auto_{video_path.stem}.jpg"
    try:
        _sp.run(
            [ffmpeg, "-ss", f"{ts:.2f}", "-i", str(video_path),
             "-frames:v", "1", "-q:v", "2",
             "-vf", "scale='min(1280,iw)':-2", "-y", str(out_path)],
            capture_output=True, timeout=60,
        )
        return out_path if out_path.exists() else None
    except Exception:
        return None


def generate_youtube_metadata(video_path: Path, project_name: str,
                              extra_notes: str = "") -> dict:
    """
    Analyse a video (sampled frames via ffmpeg + Gemini vision) and return:
    {
      "title":       str,        # ≤100 chars
      "description": str,        # ≤5000 bytes, incl. hashtags at the end
      "tags":        list[str],  # ≤500 chars total
    }
    Falls back to text-only generation when ffmpeg is unavailable.
    """
    import json as _json
    from google.genai import types

    client = _get_client()
    frames = _extract_video_frames(video_path)

    parts = []
    for i, fb in enumerate(frames):
        parts.append(types.Part.from_bytes(data=fb, mime_type="image/jpeg"))
        parts.append(types.Part.from_text(text=f"[Frame {i+1} of {len(frames)}]"))

    seen = ("Analyse the video frames shown above (sampled across the video)."
            if frames else
            "No frames available — infer from the file name and notes.")

    prompt = f"""Video file: {video_path.name}
Project: {project_name}
{f"Creator notes: {extra_notes}" if extra_notes else ""}

{seen}
{_history("youtube")}

Write YouTube metadata. Return a JSON object with EXACTLY these keys:
{{
  "title": "Compelling, searchable title — max 95 characters, no clickbait lies",
  "description": "2-4 paragraph description. First 2 lines must hook (they show above the fold). End with 3-5 relevant #hashtags on the final line.",
  "tags": ["tag1", "tag2", "..."]
}}

Rules:
- title: ≤95 characters, front-load keywords, no < or > characters
- description: ≤4500 characters, plain text, natural keyword use
- tags: 10-15 tags, mix of specific and broad, no # symbol, total under 450 characters
- Output ONLY the JSON object, no markdown fences"""

    parts.append(types.Part.from_text(text=prompt))
    contents = types.Content(parts=parts, role="user")

    last_err = None
    for model_name in MODEL_CASCADE:
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=_YT_SYSTEM,
                    temperature=0.7,
                    max_output_tokens=1200,
                ),
            )
            raw = response.text.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            data = _json.loads(raw.strip())
            title = str(data.get("title", project_name))[:100]
            desc  = str(data.get("description", ""))[:4900]
            tags  = [str(t).strip().lstrip("#") for t in data.get("tags", []) if t][:15]
            print(f"  [ai:yt] {model_name} → '{title}' ({len(tags)} tags)")
            return {"title": title, "description": desc, "tags": tags}
        except Exception as exc:
            if _is_retryable(exc):
                last_err = exc
                time.sleep(0.5)
                continue
            print(f"  [ai:yt] {model_name} non-retryable: {exc}")
            last_err = exc
            break

    # Graceful fallback so the user can still publish
    print(f"  [ai:yt] all models failed ({last_err}) — using filename fallback")
    nice = project_name.replace("_", " ").replace("-", " ").title()
    return {
        "title":       nice[:100],
        "description": f"{nice}\n\n{extra_notes}".strip(),
        "tags":        _default_tags(project_name),
    }
