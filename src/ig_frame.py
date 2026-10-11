"""
src/ig_frame.py — fit photos into an Instagram-legal frame WITHOUT cropping.

The image is scaled to fit inside the target canvas and the rest is filled
with a solid colour (your white-border look). Optional extra margin gives a
uniform frame even when the photo already fits.

Instagram API feed limits: aspect 4:5 (0.8) … 1.91:1. In a carousel every
slide is shown at the FIRST slide's ratio, so all slides share one ratio.
Output: 1440 px wide JPEG q95 (Instagram's max width), EXIF-rotated, ICC kept,
written to <project>/.ig_ready/ (originals untouched).
"""

import io
import json
from pathlib import Path

from PIL import Image, ImageOps

from src.fsutil import atomic_write_json, load_json

ROOT = Path(__file__).resolve().parent.parent
DEFAULTS_FILE = ROOT / ".queue" / "ig_frame.json"
RATIOS = {"4:5 portrait": 4 / 5, "1:1 square": 1.0, "1.91:1 landscape": 1.91,
          "Auto (closest allowed)": None}
MIN_R, MAX_R, WIDTH = 0.8, 1.91, 1440
DEFAULT = {"on": True, "ratio": "4:5 portrait", "color": "#FFFFFF", "margin": 0}


def load_defaults() -> dict:
    d = load_json(DEFAULTS_FILE, {})
    cfg = {**DEFAULT, **(d if isinstance(d, dict) else {})}
    if cfg["ratio"] not in RATIOS:
        cfg["ratio"] = DEFAULT["ratio"]
    return cfg


def save_defaults(cfg: dict) -> None:
    if cfg != load_defaults():
        atomic_write_json(DEFAULTS_FILE, cfg)


def signature(cfg: dict) -> str:
    return json.dumps(cfg, sort_keys=True)


def _open(path: Path):
    img = ImageOps.exif_transpose(Image.open(path))
    return img.convert("RGB"), img.info.get("icc_profile")


def target_ratio(cfg: dict, first: Path) -> float:
    r = RATIOS.get(cfg["ratio"])
    if r is None:
        with Image.open(first) as im:
            w, h = ImageOps.exif_transpose(im).size
        r = min(MAX_R, max(MIN_R, w / h))
    return r


def frame_image(img: Image.Image, ratio: float, color: str, margin_pct: float,
                width: int = WIDTH) -> Image.Image:
    W, H = width, round(width / ratio)
    m = round(min(W, H) * margin_pct / 100)
    s = min((W - 2 * m) / img.width, (H - 2 * m) / img.height)
    nw, nh = max(1, round(img.width * s)), max(1, round(img.height * s))
    canvas = Image.new("RGB", (W, H), color)
    canvas.paste(img.resize((nw, nh), Image.LANCZOS), ((W - nw) // 2, (H - nh) // 2))
    return canvas


def preview_jpeg(path: Path, ratio: float, color: str, margin: float) -> bytes:
    img, _ = _open(path)
    img.thumbnail((800, 800))
    buf = io.BytesIO()
    frame_image(img, ratio, color, margin, width=400).save(buf, "JPEG", quality=85)
    return buf.getvalue()


def frame_files(paths: list[Path], cfg: dict, project: Path) -> list[Path]:
    ratio = target_ratio(cfg, paths[0])
    out = project / ".ig_ready"
    out.mkdir(exist_ok=True)
    result = []
    for p in paths:
        img, icc = _open(p)
        dest = out / f"{p.stem}.jpg"
        kw = {"icc_profile": icc} if icc else {}
        frame_image(img, ratio, cfg["color"], cfg["margin"]).save(
            dest, "JPEG", quality=95, optimize=True, **kw)
        result.append(dest)
    return result
