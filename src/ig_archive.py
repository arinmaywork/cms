"""
src/ig_archive.py — archive of everything posted on Instagram + duplicate detection.

The Graph API doesn't return original file names, so each posted image (or
reel thumbnail) is downloaded once and fingerprinted (dHash after trimming
solid frame borders — so black/white padding added by the framer doesn't
matter). Local photos under input_instagram/ and published/instagram/ get the
same fingerprint, which recovers file names and powers "already posted" checks.

Outputs: history/ig_archive.json (cache) and history/instagram_posts.md.
Incremental: already-processed posts are skipped on the next sync.
"""

import io
import re
from datetime import datetime
from pathlib import Path

import requests
from PIL import Image, ImageChops, ImageOps

import src.progress as progress
from src.fsutil import atomic_write_json, atomic_write_text, load_json

ROOT = Path(__file__).resolve().parent.parent
ARCHIVE = ROOT / "history" / "ig_archive.json"
MD = ROOT / "history" / "instagram_posts.md"
LOCAL_CACHE = ROOT / ".queue" / "ig_local_hashes.json"
THUMBS = ROOT / "history" / "ig_thumbs"
NS = "ig_archive"
DUP_DISTANCE = 8          # Hamming distance (of 64 bits) that counts as "same photo"
IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


# ── Fingerprint ───────────────────────────────────────────────────────────────

def _trim(img: Image.Image) -> Image.Image:
    """Remove a uniform border (frame padding) using the corner colour."""
    g = img.convert("L")
    w, h = g.size
    corners = [g.getpixel(xy) for xy in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1))]
    c = corners[0]
    # only frame-like borders (near-black / near-white, same in all corners) get trimmed
    if not (c < 30 or c > 225) or max(corners) - min(corners) > 20:
        return img
    bg = Image.new("L", g.size, c)
    bbox = ImageChops.difference(g, bg).point(lambda v: 255 if v > 24 else 0).getbbox()
    return img.crop(bbox) if bbox and (bbox[2] - bbox[0]) > 20 and (bbox[3] - bbox[1]) > 20 else img


def dhash(img: Image.Image) -> str:
    g = _trim(ImageOps.exif_transpose(img)).convert("L").resize((9, 8), Image.LANCZOS)
    px = list(g.getdata())
    bits = "".join("1" if px[r * 9 + c] > px[r * 9 + c + 1] else "0" for r in range(8) for c in range(8))
    return f"{int(bits, 2):016x}"


def distance(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def hash_file(path: Path) -> str | None:
    try:
        with Image.open(path) as im:
            im.draft("RGB", (512, 512))          # fast JPEG decode at reduced size
            return dhash(im)
    except Exception:
        return None


# ── Local library index (cached by path + mtime) ──────────────────────────────

def local_index() -> dict[str, str]:
    """{path: hash} for photos on the VM (input_instagram + published/instagram)."""
    cache = load_json(LOCAL_CACHE, {})
    out, changed = {}, False
    roots = [ROOT / "input_instagram", ROOT / "published" / "instagram"]
    for root in roots:
        if not root.exists():
            continue
        for f in root.rglob("*"):
            if f.suffix.lower() not in IMG_EXTS or ".ig_ready" in f.parts or f.name.startswith("."):
                continue
            key = f"{f}|{f.stat().st_mtime:.0f}"
            h = cache.get(key) or hash_file(f)
            if h:
                out[str(f)] = h
                if key not in cache:
                    cache[key], changed = h, True
    if changed:
        atomic_write_json(LOCAL_CACHE, cache)
    return out


# ── Sync from Instagram ───────────────────────────────────────────────────────

def _fetch_all() -> list[dict]:
    from src.instagram_publisher import GRAPH_BASE, _creds, _get
    c = _creds()
    fields = ("id,caption,media_type,media_url,thumbnail_url,permalink,timestamp,"
              "children{media_type,media_url,thumbnail_url}")
    out, url, params = [], f"{GRAPH_BASE}/{c['ig_user_id']}/media", {"fields": fields, "limit": 50}
    while url:
        data = _get(url, {**params, "access_token": c["access_token"]})
        out += data.get("data", [])
        url, params = data.get("paging", {}).get("next"), {}
    return out


def _download_hash(url: str, thumb: Path | None = None) -> str | None:
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        im = Image.open(io.BytesIO(r.content))
        if thumb:
            THUMBS.mkdir(parents=True, exist_ok=True)
            t = im.convert("RGB")
            t.thumbnail((240, 240))
            t.save(thumb, "JPEG", quality=80)
        return dhash(im)
    except Exception as e:
        print(f"  [ig:archive] image fetch failed: {e}")
        return None


def split_caption(caption: str) -> tuple[str, list[str]]:
    tags = re.findall(r"#\w+", caption or "")
    text = re.sub(r"(\s*#\w+)+\s*$", "", caption or "").strip()
    return text, list(dict.fromkeys(tags))


def sync() -> dict:
    """Fetch posts, fingerprint new ones, match local files, write the .md. Background-safe."""
    progress.start(NS, ["Fetch post list", "Fingerprint new posts", "Match local files", "Write markdown"])
    try:
        arch = load_json(ARCHIVE, {})
        arch = arch if isinstance(arch, dict) else {}
        posts = arch.setdefault("posts", {})
        progress.update(NS, 0, "active", "Asking Instagram…")
        media = _fetch_all()
        progress.update(NS, 0, "done", f"{len(media)} posts")

        new = [m for m in media if m["id"] not in posts]
        for n, m in enumerate(new, 1):
            progress.update(NS, 1, "active", f"{n}/{len(new)} · {m['timestamp'][:10]}")
            kids = (m.get("children") or {}).get("data") or [m]
            slides = []
            for i, k in enumerate(kids):
                src = k.get("media_url") if k.get("media_type") == "IMAGE" else k.get("thumbnail_url")
                th = THUMBS / f"{m['id']}_{i}.jpg"
                slides.append({"type": k.get("media_type", ""), "thumb": th.name,
                               "hash": _download_hash(src, th) if src else None})
            posts[m["id"]] = {"id": m["id"], "date": m["timestamp"], "type": m.get("media_type", ""),
                              "permalink": m.get("permalink", ""), "caption": m.get("caption") or "",
                              "slides": slides}
            if n % 10 == 0:
                atomic_write_json(ARCHIVE, arch)       # resumable if interrupted
        for m in media:                                # captions may have been edited
            if m["id"] in posts:
                posts[m["id"]]["caption"] = m.get("caption") or ""
        progress.update(NS, 1, "done", f"{len(new)} new")

        progress.update(NS, 2, "active", "Hashing photos on the server…")
        local = local_index()
        for p in posts.values():
            for s in p["slides"]:
                if s.get("hash"):
                    best = min(local.items(), key=lambda kv: distance(kv[1], s["hash"]), default=None)
                    s["file"] = (Path(best[0]).name if best and distance(best[1], s["hash"]) <= DUP_DISTANCE
                                 else s.get("file", ""))
        # thumbnails for older entries whose originals are missing (to recognise them)
        by_id = {m["id"]: m for m in media}
        for p in posts.values():
            kids = ((by_id.get(p["id"]) or {}).get("children") or {}).get("data") or [by_id.get(p["id"]) or {}]
            for i, s in enumerate(p["slides"]):
                th = THUMBS / f"{p['id']}_{i}.jpg"
                if not s.get("file") and not th.exists() and i < len(kids):
                    k = kids[i]
                    src = k.get("media_url") if k.get("media_type") == "IMAGE" else k.get("thumbnail_url")
                    if src:
                        _download_hash(src, th)
                        s["thumb"] = th.name
        atomic_write_json(ARCHIVE, arch)
        progress.update(NS, 2, "done", f"{len(local)} local photos indexed")

        progress.update(NS, 3, "active", "Writing…")
        write_md(arch)
        progress.update(NS, 3, "done", str(MD.relative_to(ROOT)))
        res = {"success": True, "posts": len(posts), "new": len(new)}
        progress.finish(NS, res)
        return res
    except Exception as e:
        progress.fail(NS, str(e))
        return {"success": False, "error": str(e)}


def write_md(arch: dict | None = None) -> Path:
    arch = arch or load_json(ARCHIVE, {})
    posts = sorted(arch.get("posts", {}).values(), key=lambda p: p["date"], reverse=True)
    kind = {"CAROUSEL_ALBUM": "Carousel", "IMAGE": "Image", "VIDEO": "Reel"}
    lines = [f"# Instagram posts — archive", "",
             f"_Synced {datetime.now():%Y-%m-%d %H:%M} · {len(posts)} posts · newest first. "
             "File names are matched by image fingerprint against photos on the server; "
             "`—` = original not on the server._", ""]
    miss = missing(arch)
    if miss:
        lines += [f"## ⚠️ Originals not on the server ({len(miss)})", "",
                  "Export these and copy them into a folder under `published/instagram/`, "
                  "then sync again. Thumbnails: CMS → Instagram → 🗂 Posts archive.", ""]
        lines += [f"- {x['date']} · slide {x['slide']} of {x['of']} · [open]({x['permalink']})" for x in miss]
        lines += [""]
    for p in posts:
        text, tags = split_caption(p["caption"])
        n = len(p["slides"])
        lines += [f"## {p['date'][:10]} · {kind.get(p['type'], p['type'].title())}"
                  + (f" ({n})" if n > 1 else "") + f" · [open]({p['permalink']})", ""]
        names = [s.get("file") or "—" for s in p["slides"]]
        lines += [f"**Files:** {', '.join(names)}", ""]
        if text:
            lines += ["**Caption:**", "", *[f"> {ln}" if ln else ">" for ln in text.splitlines()], ""]
        if tags:
            lines += [f"**Hashtags ({len(tags)}):** {' '.join(tags)}", ""]
    atomic_write_text(MD, "\n".join(lines) + "\n")
    return MD


def missing(arch: dict | None = None) -> list[dict]:
    """Posted images whose original isn't on the VM, newest first."""
    arch = arch or load_json(ARCHIVE, {})
    out = []
    for p in sorted(arch.get("posts", {}).values(), key=lambda p: p["date"], reverse=True):
        for i, s in enumerate(p["slides"]):
            if not s.get("file"):
                out.append({"date": p["date"][:10], "slide": i + 1, "of": len(p["slides"]),
                            "permalink": p["permalink"], "type": s.get("type", ""),
                            "thumb": str(THUMBS / f"{p['id']}_{i}.jpg")})
    return out


def find_duplicates(paths: list[Path]) -> dict[str, dict]:
    """{file name: {date, permalink, distance}} for photos that look already posted."""
    posts = load_json(ARCHIVE, {}).get("posts", {}).values()
    known = [(s["hash"], p) for p in posts for s in p["slides"] if s.get("hash")]
    out = {}
    for path in paths:
        h = hash_file(path)
        if not h or not known:
            continue
        hit, p = min(((distance(h, kh), pp) for kh, pp in known), key=lambda t: t[0])
        if hit <= DUP_DISTANCE:
            out[path.name] = {"date": p["date"][:10], "permalink": p["permalink"], "distance": hit}
    return out
