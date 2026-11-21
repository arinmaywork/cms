"""
src/ig_deepmatch.py — thorough matching for posted images whose original wasn't found.

Global fingerprints break when a photo was padded/cropped/tinted after export.
This uses local features (SIFT keypoints) + a RANSAC similarity transform: it
counts how many distinctive points line up geometrically between the posted
image and each candidate. Survives padding, crops, resizing and colour edits.

Memory-safe for a 1 GB VM: posted features stay in RAM (one set per missing
slide); local photos are streamed one at a time, never cached.
"""

import io
import time
from pathlib import Path

import requests
from PIL import Image, ImageOps

import src.ig_archive as ia
import src.progress as progress
from src.fsutil import atomic_write_json, load_json

NS = "ig_deep"
AUTO_MIN, AUTO_RATIO = 25, 2.0          # auto-accept: ≥25 inliers and ≥2× the runner-up


def _feats(pil: Image.Image, sift):
    import cv2
    import numpy as np
    g = np.asarray(ImageOps.exif_transpose(pil).convert("L"))
    s = 700 / max(g.shape)
    if s < 1:
        g = cv2.resize(g, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    k, d = sift.detectAndCompute(g, None)
    return np.float32([p.pt for p in k]), d


def _inliers(a, b, bf) -> int:
    import cv2
    (pa, da), (pb, db) = a, b
    if da is None or db is None or len(da) < 2 or len(db) < 2:
        return 0
    good = [m for m, n in (p for p in bf.knnMatch(da, db, k=2) if len(p) == 2) if m.distance < 0.75 * n.distance]
    if len(good) < 6:
        return 0
    _, mask = cv2.estimateAffinePartial2D(pa[[m.queryIdx for m in good]], pb[[m.trainIdx for m in good]],
                                          method=cv2.RANSAC, ransacReprojThreshold=8)
    return int(mask.sum()) if mask is not None else 0


def run(scope: str = "leftovers") -> dict:
    """scope: 'leftovers' (unmatched exports in _import — fast) or 'all' (every photo on the VM)."""
    import cv2
    progress.start(NS, ["Download posted images", "Compare with photos on the server", "Save results"])
    try:
        sift, bf = cv2.SIFT_create(800), cv2.BFMatcher(cv2.NORM_L2)
        arch = load_json(ia.ARCHIVE, {})
        posts = arch.get("posts", {})
        todo = [(pid, i) for pid, p in posts.items() for i, s in enumerate(p["slides"])
                if not s.get("file") and not s.get("unavailable") and s.get("type") != "VIDEO"]
        if not todo:
            progress.finish(NS, {"success": True, "matched": 0, "checked": 0})
            return {"success": True, "matched": 0}

        media = {m["id"]: m for m in ia._fetch_all()}
        posted = {}
        for n, (pid, i) in enumerate(todo, 1):
            progress.update(NS, 0, "active", f"{n}/{len(todo)}")
            m = media.get(pid) or {}
            kids = (m.get("children") or {}).get("data") or [m]
            if i >= len(kids) or not kids[i].get("media_url"):
                continue
            try:
                r = requests.get(kids[i]["media_url"], timeout=30)
                r.raise_for_status()
                posted[(pid, i)] = _feats(ia._trim(Image.open(io.BytesIO(r.content)).convert("RGB")), sift)
            except Exception as e:
                print(f"  [ig:deep] download failed {pid}/{i}: {e}")
        progress.update(NS, 0, "done", f"{len(posted)} posted images")

        if scope == "all":
            paths = list(ia.local_index().keys())
        else:
            paths = [p for p in arch.get("import_leftovers", []) if Path(p).exists()]
        top: dict = {k: [] for k in posted}
        t0 = time.time()
        for n, path in enumerate(paths, 1):
            try:
                with Image.open(path) as im:
                    im.draft("RGB", (900, 900))
                    f = _feats(im, sift)
            except Exception:
                continue
            for key, pf in posted.items():
                sc = _inliers(pf, f, bf)
                if sc:
                    top[key] = sorted(top[key] + [(sc, path)], reverse=True)[:4]
            if n % 5 == 0 or n == len(paths):
                eta = (time.time() - t0) / n * (len(paths) - n)
                progress.update(NS, 1, "active", f"{n}/{len(paths)} photos · ~{eta / 60:.0f} min left")
        progress.update(NS, 1, "done", f"{len(paths)} photos compared")

        progress.update(NS, 2, "active", "Saving…")
        arch = load_json(ia.ARCHIVE, {})                   # re-read: a sync may have run meanwhile
        matched = 0
        for (pid, i), cands in top.items():
            s = arch["posts"][pid]["slides"][i]
            if s.get("file"):
                continue
            s["deep"] = [[sc, p] for sc, p in cands]
            if cands and cands[0][0] >= AUTO_MIN and (len(cands) == 1 or cands[0][0] >= AUTO_RATIO * cands[1][0]):
                s.update(file=Path(cands[0][1]).name, path=cands[0][1], manual=True, nearest={})
                matched += 1
        arch["import_leftovers"] = ia._organize(arch["posts"])
        atomic_write_json(ia.ARCHIVE, arch)
        ia.write_md(arch)
        progress.update(NS, 2, "done", f"{matched} matched automatically")
        res = {"success": True, "matched": matched, "checked": len(paths), "missing": len(todo)}
        progress.finish(NS, res)
        return res
    except Exception as e:
        progress.fail(NS, str(e))
        return {"success": False, "error": str(e)}
