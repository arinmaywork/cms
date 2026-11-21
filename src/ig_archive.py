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
LOCAL_CACHE = ROOT / ".queue" / "ig_local_hashes_v3.json"
THUMBS = ROOT / "history" / "ig_thumbs"
IMPORT = ROOT / "published" / "instagram" / "_import"   # drop bulk exports here
POSTS_DIR = ROOT / "published" / "instagram"
NS = "ig_archive"
DUP_DISTANCE = 22         # combined dHash+pHash score (0–127) that counts as "same photo"
IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


# ── Fingerprint ───────────────────────────────────────────────────────────────

def _trim(img: Image.Image) -> Image.Image:
    """Strip frame bars: only perfectly flat edge rows/columns (std < 6) of the corner
    colour are removed — dark-but-textured photo content is never cut."""
    import numpy as np
    small = img.convert("L")
    small.thumbnail((400, 400))
    a = np.asarray(small, dtype=float)
    h, w = a.shape
    c = float(np.median([a[0, 0], a[0, -1], a[-1, 0], a[-1, -1]]))
    flat = lambda v: abs(v.mean() - c) < 14 and v.std() < 6
    top = next((y for y in range(h) if not flat(a[y, :])), h)
    bot = next((y for y in range(h - 1, -1, -1) if not flat(a[y, :])), -1)
    left = next((x for x in range(w) if not flat(a[:, x])), w)
    right = next((x for x in range(w - 1, -1, -1) if not flat(a[:, x])), -1)
    if bot - top < 20 or right - left < 20 or (top, left, h - 1 - bot, w - 1 - right) == (0, 0, 0, 0):
        return img
    sx, sy = img.width / w, img.height / h
    return img.crop((int((left + 1) * sx), int((top + 1) * sy), int(right * sx), int(bot * sy)))


def _dh(g: Image.Image) -> str:
    px = list(g.resize((9, 8), Image.LANCZOS).getdata())
    bits = "".join("1" if px[r * 9 + c] > px[r * 9 + c + 1] else "0" for r in range(8) for c in range(8))
    return f"{int(bits, 2):016x}"


def _ph(g: Image.Image) -> str:
    import numpy as np
    a = np.asarray(g.resize((32, 32), Image.LANCZOS), dtype=float)
    k = np.arange(32)
    C = np.cos(np.pi * (2 * k[None, :] + 1) * k[:, None] / 64)
    low = (C @ a @ C.T)[:8, :8].flatten()[1:]
    bits = "".join("1" if v > np.median(low) else "0" for v in low)
    return f"{int(bits, 2):016x}"


def dhash(img: Image.Image, trim: bool = True) -> str:
    """Fingerprint 'dhash:phash' (two 64-bit hashes)."""
    img = ImageOps.exif_transpose(img).convert("RGB")
    g = (_trim(img) if trim else img).convert("L")
    return f"{_dh(g)}:{_ph(g)}"


def distance(a: str, b: str) -> int:
    """Combined score 0–127 (lower = more alike)."""
    if not a or not b or ":" not in a or ":" not in b:
        return 99
    (ad, ap), (bd, bp) = a.split(":"), b.split(":")
    return bin(int(ad, 16) ^ int(bd, 16)).count("1") + bin(int(ap, 16) ^ int(bp, 16)).count("1")


def hash_file(path: Path) -> list[str] | None:
    """[raw, bar-trimmed] fingerprints of a local photo."""
    try:
        with Image.open(path) as im:
            im.draft("RGB", (640, 640))
            return [dhash(im, trim=False), dhash(im, trim=True), center_fp(im, trim=False)]
    except Exception:
        return None


def _center(img: Image.Image, f: float = 0.7) -> Image.Image:
    w, h = img.size
    return img.crop((int(w * (1 - f) / 2), int(h * (1 - f) / 2), int(w * (1 + f) / 2), int(h * (1 + f) / 2)))


def center_fp(img: Image.Image, trim: bool = True) -> str:
    """Fingerprint of the central 70% — tolerant of borders/crops added in manual edits."""
    img = ImageOps.exif_transpose(img).convert("RGB")
    return dhash(_center(_trim(img) if trim else img), trim=False)


def slide_score(s: dict, pair: list[str]) -> int:
    sc = min(distance(s["hash"], h) for h in pair[:2])
    if s.get("hash_c") and len(pair) > 2:
        sc = min(sc, distance(s["hash_c"], pair[2]))
    return sc


def candidates(s: dict, local: dict, k: int = 4) -> list[tuple[int, str]]:
    return sorted((slide_score(s, pair), path) for path, pair in local.items())[:k]


def assign(post_id: str, idx: int, path: str | None) -> None:
    """Manual confirmation from the UI (path=None → mark 'original not available')."""
    arch = load_json(ARCHIVE, {})
    s = arch["posts"][post_id]["slides"][idx]
    if path:
        s.update(file=Path(path).name, path=path, manual=True, nearest={})
        s.pop("unavailable", None)
    else:
        s.update(unavailable=True, file="", path="")
    arch["import_leftovers"] = _organize(arch["posts"])
    atomic_write_json(ARCHIVE, arch)
    write_md(arch)


def attach(post_id: str, idx: int, filename: str, data: bytes) -> Path:
    """Store an original uploaded for a specific post/slide and record it."""
    arch = load_json(ARCHIVE, {})
    p = arch["posts"][post_id]
    folder = POSTS_DIR / f"{p['date'][:10]}_{post_id[-6:]}"
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"{idx + 1:02d}_{Path(filename).name}"
    dest.write_bytes(data)
    p["slides"][idx].update(file=dest.name, path=str(dest), manual=True, nearest={})
    p["slides"][idx].pop("unavailable", None)
    atomic_write_json(ARCHIVE, arch)
    write_md(arch)
    return dest


def best_match(fp: str, local: dict) -> tuple[str | None, int]:
    best, score = None, 999
    for path, pair in local.items():
        d = min(distance(fp, h) for h in pair)
        if d < score:
            best, score = path, d
    return best, score


# ── Local library index (cached by path + mtime) ──────────────────────────────

def local_index() -> dict[str, list[str]]:
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
              "like_count,comments_count,children{media_type,media_url,thumbnail_url}")
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

        if arch.get("hash_v") not in (2, 3):                   # fingerprint format changed → recompute
            for p in posts.values():
                for i, sl in enumerate(p["slides"]):
                    th = THUMBS / f"{p['id']}_{i}.jpg"
                    sl["hash"] = dhash(Image.open(th)) if th.exists() else None
            arch["hash_v"] = 2
        if arch.get("hash_v") != 3:                   # add centre fingerprints (from thumbnails)
            for p in posts.values():
                for i, sl in enumerate(p["slides"]):
                    th = THUMBS / f"{p['id']}_{i}.jpg"
                    if th.exists():
                        sl["hash_c"] = center_fp(Image.open(th))
            arch["hash_v"] = 3
        by_id0 = {m["id"]: m for m in media}
        for p in posts.values():                      # missing fingerprints → re-download
            kids = ((by_id0.get(p["id"]) or {}).get("children") or {}).get("data") or [by_id0.get(p["id"]) or {}]
            for i, sl in enumerate(p["slides"]):
                if not sl.get("hash") and i < len(kids):
                    k = kids[i]
                    src = k.get("media_url") if k.get("media_type") == "IMAGE" else k.get("thumbnail_url")
                    if src:
                        sl["hash"] = _download_hash(src, THUMBS / f"{p['id']}_{i}.jpg")
        new = [m for m in media if m["id"] not in posts]
        for n, m in enumerate(new, 1):
            progress.update(NS, 1, "active", f"{n}/{len(new)} · {m['timestamp'][:10]}")
            kids = (m.get("children") or {}).get("data") or [m]
            slides = []
            for i, k in enumerate(kids):
                src = k.get("media_url") if k.get("media_type") == "IMAGE" else k.get("thumbnail_url")
                th = THUMBS / f"{m['id']}_{i}.jpg"
                h = _download_hash(src, th) if src else None
                slides.append({"type": k.get("media_type", ""), "thumb": th.name, "hash": h,
                               "hash_c": center_fp(Image.open(th)) if h and th.exists() else None})
            posts[m["id"]] = {"id": m["id"], "date": m["timestamp"], "type": m.get("media_type", ""),
                              "permalink": m.get("permalink", ""), "caption": m.get("caption") or "",
                              "slides": slides}
            if n % 10 == 0:
                atomic_write_json(ARCHIVE, arch)       # resumable if interrupted
        for m in media:                                # captions may have been edited
            if m["id"] in posts:
                posts[m["id"]].update(caption=m.get("caption") or "", likes=m.get("like_count"),
                                      comments=m.get("comments_count"))
        arch["synced"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        progress.update(NS, 1, "done", f"{len(new)} new")

        progress.update(NS, 2, "active", "Hashing photos on the server…")
        local = local_index()
        for p in posts.values():
            for s in p["slides"]:
                if s.get("hash"):
                    if s.get("manual") and s.get("path") and Path(s["path"]).exists():
                        continue                              # confirmed by you — keep
                    top = candidates(s, local, 6)
                    path, score = (top[0][1], top[0][0]) if top else (None, 999)
                    # next-best that is a *different* photo (not the same image filed twice)
                    rest = [sc for sc, pth in top[1:] if slide_score(s, local[pth]) - score > 0
                            and min(distance(a, b) for a in local[pth][:2] for b in local[path][:2]) > DUP_DISTANCE]
                    margin = (rest[0] - score) if rest else 99
                    ok = path is not None and (score <= DUP_DISTANCE or (score <= 30 and margin >= 12))
                    s["file"] = Path(path).name if ok else ""
                    s["path"] = path if ok else ""
                    s["nearest"] = {} if ok or not path else {"file": Path(path).name, "score": score}
        arch["import_leftovers"] = _organize(posts)
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


def _organize(posts: dict) -> list[str]:
    """File matched exports from _import/ into one folder per post (slides in order).
    Returns the exports that matched no post."""
    import shutil
    if not IMPORT.exists():
        return []
    moved: dict[str, str] = {}
    for p in posts.values():
        folder = POSTS_DIR / f"{p['date'][:10]}_{p['id'][-6:]}"
        for i, s in enumerate(p["slides"]):
            src = s.get("path", "")
            if not src or Path(IMPORT) not in Path(src).parents:
                continue
            dest = folder / f"{i + 1:02d}_{Path(src).name}"
            folder.mkdir(parents=True, exist_ok=True)
            if Path(src).exists():
                shutil.move(src, dest)
                moved[src] = str(dest)
            elif src in moved and not dest.exists():      # same photo used in two posts
                shutil.copy2(moved[src], dest)
            s["path"], s["file"] = str(dest), dest.name
    leftovers = sorted(str(f) for f in IMPORT.rglob("*") if f.is_file()
                       and f.suffix.lower() in IMG_EXTS)
    if moved:
        print(f"  [ig:archive] filed {len(moved)} export(s) into per-post folders; "
              f"{len(leftovers)} unmatched left in _import")
    return leftovers


def delete_leftovers() -> int:
    from src.fsutil import load_json
    n = 0
    for f in load_json(ARCHIVE, {}).get("import_leftovers", []):
        fp = Path(f)
        if fp.exists() and Path(IMPORT) in fp.parents:
            fp.unlink()
            n += 1
    return n


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
            if not s.get("file") and not s.get("unavailable"):
                out.append({"id": p["id"], "idx": i, "date": p["date"][:10], "slide": i + 1, "of": len(p["slides"]),
                            "permalink": p["permalink"], "type": s.get("type", ""),
                            "nearest": s.get("nearest") or {},
                            "thumb": str(THUMBS / f"{p['id']}_{i}.jpg")})
    return out


def find_duplicates(paths: list[Path]) -> dict[str, dict]:
    """{file name: {date, permalink, distance}} for photos that look already posted."""
    posts = load_json(ARCHIVE, {}).get("posts", {}).values()
    known = [(s["hash"], p) for p in posts for s in p["slides"] if s.get("hash")]
    out = {}
    for path in paths:
        pair = hash_file(path)
        if not pair or not known:
            continue
        hit, p = min(((min(distance(h, kh) for h in pair), pp) for kh, pp in known), key=lambda t: t[0])
        if hit <= DUP_DISTANCE:
            out[path.name] = {"date": p["date"][:10], "permalink": p["permalink"], "distance": hit}
    return out
