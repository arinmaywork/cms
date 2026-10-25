"""
src/r2.py — publish photo stories to Cloudflare R2 (S3 API).

Layout in the bucket:
  img/<original-name>-<sha256[:12]>.jpg   images, Cache-Control immutable
  stories/<slug>.json                     one story
  stories/index.json                      all stories, newest first (gallery + sitemap)

Image keys end in a hash of the *uploaded* bytes: same file → same key (upload
skipped), edited file → new key (no stale-cache risk under immutable). The
readable prefix is your original file name, for browsing the bucket.
Images are never converted or re-encoded: you supply JPEGs, and the upload only
drops metadata segments (EXIF, XMP, IPTC, GPS) in place; the ICC profile and the
pixels are untouched, and if the photo is rotated only its Orientation tag is
kept. Camera/exposure facts go into the story JSON instead (specs line).
Cloudflare's format=auto serves AVIF/WebP to browsers that support them.

Env: R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET,
     R2_PUBLIC_BASE (e.g. https://media.example.com)
"""

import hashlib
import io
import json
import os
import re
import struct
import time
from pathlib import Path
from urllib.parse import urlparse

from PIL import Image

IMMUTABLE = "public, max-age=31536000, immutable"
JSON_CACHE = "public, max-age=60"
SCHEMA = 1
_client = None


# ── image processing ─────────────────────────────────────────────────────────
def _clean_jpeg(data: bytes, orient: int) -> bytes:
    """Lossless: drop APP1 (EXIF/XMP) and APP13 (IPTC) segments, keep the rest.
    If the photo is rotated, put back a minimal EXIF holding only Orientation."""
    out, i = [data[:2]], 2
    if orient != 1:
        ex = Image.Exif()
        ex[0x0112] = orient
        blob = ex.tobytes()
        out.append(b"\xff\xe1" + struct.pack(">H", len(blob) + 2) + blob)
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            break
        marker, (n,) = data[i + 1], struct.unpack(">H", data[i + 2:i + 4])
        if marker == 0xDA:                        # start of scan: copy the rest
            break
        if marker not in (0xE1, 0xED):
            out.append(data[i:i + 2 + n])
        i += 2 + n
    out.append(data[i:])
    return b"".join(out)


def _facts(img: Image.Image) -> dict:
    ex = img.getexif()
    sub = ex.get_ifd(0x8769)
    f = {
        "camera": " ".join(filter(None, [str(ex.get(0x010F, "")).strip(), str(ex.get(0x0110, "")).strip()])) or None,
        "lens": str(sub.get(0xA434, "")).strip() or None,
        "focal_mm": float(sub[0x920A]) if 0x920A in sub else None,
        "aperture": round(float(sub[0x829D]), 1) if 0x829D in sub else None,
        "iso": sub.get(0x8827),
        "taken_at": str(sub.get(0x9003, "")).replace(":", "-", 2).replace(" ", "T") or None,
    }
    if 0x829A in sub:
        t = float(sub[0x829A])
        f["shutter"] = f"1/{round(1 / t)}" if 0 < t < 1 else f"{t:g}"
    return {k: v for k, v in f.items() if v not in (None, "")}


def specs_line(e: dict) -> str:
    """One display line for a page, e.g. 'X-T30 · XF 16-55mm · 35mm · ƒ/5.6 · 1/250s · ISO 200'."""
    parts = [e.get("camera"), e.get("lens"),
             f"{e['focal_mm']:g}mm" if "focal_mm" in e else None,
             f"ƒ/{e['aperture']:g}" if "aperture" in e else None,
             f"{e['shutter']}s" if "shutter" in e else None,
             f"ISO {e['iso']}" if "iso" in e else None]
    return " · ".join(x for x in parts if x)


def prepare_image(path) -> dict:
    """→ {data, width, height, exif}: the same JPEG with all metadata (incl. GPS) removed."""
    raw = Path(path).read_bytes()
    img = Image.open(io.BytesIO(raw))
    if img.format != "JPEG":
        raise ValueError(f"{Path(path).name} is {img.format}, not JPEG — export it as a JPEG first "
                         "(images are uploaded as-is, never converted)")
    orient = img.getexif().get(0x0112, 1)
    w, h = img.size
    if orient in (5, 6, 7, 8):                    # displayed rotated by 90°
        w, h = h, w
    return {"data": _clean_jpeg(raw, orient), "width": w, "height": h, "exif": _facts(img)}


# ── R2 ───────────────────────────────────────────────────────────────────────
def configured() -> bool:
    return all(os.getenv(k) for k in (
        "R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET", "R2_PUBLIC_BASE"))


def _s3():
    global _client
    if _client is None:
        import boto3
        _client = boto3.client(
            "s3", region_name="auto",
            endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
            aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"])
    return _client


def _url(key: str) -> str:
    return f"{os.environ['R2_PUBLIC_BASE'].rstrip('/')}/{key}"


def _exists(key: str) -> bool:
    from botocore.exceptions import ClientError
    try:
        _s3().head_object(Bucket=os.environ["R2_BUCKET"], Key=key)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def image_key(data: bytes, name: str = "") -> str:
    """img/<original-name>-<sha12>.jpg — readable in the R2 browser, unique per content."""
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(name).stem).strip("-._")[:60]
    return f"img/{stem + '-' if stem else ''}{hashlib.sha256(data).hexdigest()[:12]}.jpg"


def _find_by_hash(sha12: str) -> str | None:
    """An existing img/ key holding these bytes under any file name (old hash-only naming included)."""
    pages = _s3().get_paginator("list_objects_v2").paginate(Bucket=os.environ["R2_BUCKET"], Prefix="img/")
    for page in pages:
        for o in page.get("Contents", []):
            stem = o["Key"][len("img/"):-len(".jpg")]
            if o["Key"].endswith(".jpg") and (stem == sha12 or stem.endswith("-" + sha12)):
                return o["Key"]
    return None


def put_image(data: bytes, name: str = "") -> str:
    """Upload once per content: identical bytes reuse the existing file whatever it is called."""
    key = image_key(data, name)
    existing = None if _exists(key) else _find_by_hash(hashlib.sha256(data).hexdigest()[:12])
    if existing:
        return _url(existing)
    if not _exists(key):
        _s3().put_object(Bucket=os.environ["R2_BUCKET"], Key=key, Body=data,
                         ContentType="image/jpeg", CacheControl=IMMUTABLE)
    return _url(key)


def put_json(key: str, obj) -> None:
    _s3().put_object(Bucket=os.environ["R2_BUCKET"], Key=key, ContentType="application/json",
                     Body=json.dumps(obj, ensure_ascii=False, indent=2).encode(),
                     CacheControl=JSON_CACHE)


def _get_json(key: str, default):
    from botocore.exceptions import ClientError
    try:
        return json.loads(_s3().get_object(Bucket=os.environ["R2_BUCKET"], Key=key)["Body"].read())
    except ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return default
        raise


# ── stories ──────────────────────────────────────────────────────────────────
def publish_story(slug: str, title: str, description: str, body_md: str, images: list[dict],
                  location: str = "", tags: list[str] | None = None,
                  published_at: str | None = None, progress=None) -> dict:
    """images: [{path, alt, caption}] in display order; the first is the cover."""
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    out = []
    for n, im in enumerate(images, 1):
        p = prepare_image(im["path"])
        out.append({"url": put_image(p["data"], Path(im["path"]).name), "width": p["width"], "height": p["height"],
                    "alt": im.get("alt", ""), "caption": im.get("caption", ""),
                    "specs": im.get("specs") or specs_line(p["exif"]), "exif": p["exif"]})
        if progress:
            progress(n, len(images))
    story = {
        "schema": SCHEMA, "slug": slug, "title": title, "description": description,
        "body_md": body_md, "location": location, "tags": tags or [],
        "published_at": published_at or now[:10], "updated_at": now,
        "cover": out[0], "images": out,
    }
    put_json(f"stories/{slug}.json", story)
    _index_upsert(story)
    return story


def _index_upsert(story: dict) -> None:
    # ponytail: read-modify-write, safe for one publisher; add locking if CMS becomes multi-user
    idx = _get_json("stories/index.json", {"schema": SCHEMA, "stories": []})
    entry = {k: story[k] for k in ("slug", "title", "description", "location", "tags",
                                    "published_at", "updated_at")}
    entry["image_count"] = len(story["images"])
    entry["cover"] = {k: story["cover"][k] for k in ("url", "width", "height", "alt")}
    idx["stories"] = sorted([s for s in idx["stories"] if s["slug"] != story["slug"]] + [entry],
                            key=lambda s: s["published_at"], reverse=True)
    idx["updated_at"] = story["updated_at"]
    put_json("stories/index.json", idx)


def get_story(slug: str) -> dict | None:
    return _get_json(f"stories/{slug}.json", None)


def update_story(slug: str, fields: dict, images: list[dict] | None = None) -> dict:
    """Edit a published story in place (no image uploads): text fields, and the image list
    as {url, alt, caption, specs} entries in the new order (drop one to remove it)."""
    story = get_story(slug)
    if story is None:
        raise KeyError(f"story {slug!r} is not published")
    story.update({k: v for k, v in fields.items()
                  if k in ("title", "description", "body_md", "location", "tags", "published_at")})
    if images is not None:
        by_url = {im["url"]: im for im in story["images"]}
        story["images"] = [{**by_url[e["url"]], **{k: e[k] for k in ("alt", "caption", "specs") if k in e}}
                           for e in images if e["url"] in by_url]
        if not story["images"]:
            raise ValueError("a story needs at least one image")
        story["cover"] = story["images"][0]
    story["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    put_json(f"stories/{slug}.json", story)
    _index_upsert(story)
    return story


def list_stories() -> list[dict]:
    return _get_json("stories/index.json", {"stories": []})["stories"]


def export_story(slug: str, dest: Path) -> list[str]:
    """Rebuild an editable CMS project in `dest` from the published story: download any image not
    already there (named like the original file) and write the draft from the live story text."""
    from src.projects import save_draft
    s = get_story(slug)
    if s is None:
        raise KeyError(f"story {slug!r} is not published")
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    names, alts, caps, specs = [], {}, {}, {}
    for n, im in enumerate(s["images"], 1):
        key = urlparse(im["url"]).path.lstrip("/")
        stem = re.sub(r"-?[0-9a-f]{12}$", "", Path(key).stem) or f"{slug}-{n}"
        name = stem + ".jpg"
        if not (dest / name).exists():
            body = _s3().get_object(Bucket=os.environ["R2_BUCKET"], Key=key)["Body"].read()
            (dest / name).write_bytes(body)
        names.append(name)
        alts[name], caps[name], specs[name] = im.get("alt", ""), im.get("caption", ""), im.get("specs", "")
    save_draft(dest, {"website": {
        "names": names, "title": s["title"], "slug": slug, "description": s["description"],
        "location": s.get("location", ""), "tags": ", ".join(s.get("tags", [])),
        "published_at": s["published_at"], "body_md": s.get("body_md", ""),
        "alts": alts, "captions": caps, "specs": specs}})
    return names


def unpublish_story(slug: str) -> None:
    _s3().delete_object(Bucket=os.environ["R2_BUCKET"], Key=f"stories/{slug}.json")
    idx = _get_json("stories/index.json", {"schema": SCHEMA, "stories": []})
    idx["stories"] = [s for s in idx["stories"] if s["slug"] != slug]
    idx["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    put_json("stories/index.json", idx)
