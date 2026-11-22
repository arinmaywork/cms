"""
src/ig_slideshow.py — photos → a 1080×1920 reel (for Instagram music, which only works on reels).

Frames are drawn with Pillow (sub-pixel crop boxes → smooth, jitter-free motion), piped to
ffmpeg per photo, then joined with ffmpeg `xfade` transitions and a colour "look".

opts (all optional):
  layout     auto | fill | blur | gallery | polaroid | cinema | diptych
  motion     kenburns | zoomin | zoomout | float | pan | still
  transition mix | cut | <any xfade name>
  sec        seconds each photo is on screen       trans   transition length (s)
  rhythm     even | accelerate | pulse
  look       natural | grain | film | warm | mono | matte
  bg         background colour for gallery / polaroid / cards (hex)
  intro, outro  text for title / end cards ("" = none)    font  serif | sans
"""

import math
import random
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

FPS = 30
LAYOUTS = {"auto": "✨ Auto — portraits fill, landscapes float (best for groups)",
           "fill": "Full screen (crops to 9:16)", "blur": "Floating on blurred photo",
           "gallery": "Gallery wall (photo on colour + shadow)", "polaroid": "Polaroid prints (tilted)",
           "cinema": "Cinematic widescreen band", "diptych": "Diptych — two photos stacked"}
MOTIONS = {"kenburns": "Ken Burns (varied, slow)", "zoomin": "Slow zoom in", "zoomout": "Slow zoom out",
           "float": "Float / drift", "pan": "Pan across (shows whole group)", "still": "Still"}
TRANSITIONS = ["mix", "cut", "fade", "dissolve", "fadeblack", "fadewhite", "smoothleft", "smoothup",
               "slideleft", "slideup", "wipeleft", "circleopen", "circleclose", "radial", "hblur",
               "pixelize", "squeezeh", "distance", "vertopen", "horzopen", "diagtl", "hlslice",
               "fadegrays", "rectcrop"]
MIX = ["fade", "smoothleft", "hblur", "dissolve", "circleopen", "smoothup", "fadeblack", "radial",
       "slideleft", "distance"]
LOOKS = {"natural": "Natural", "grain": "Film grain", "film": "Film (grain + vignette + soft contrast)",
         "warm": "Warm golden", "mono": "Black & white", "matte": "Faded matte"}
LOOK_VF = {"natural": "", "grain": "noise=alls=9:allf=t+u",
           "film": "curves=all='0/0.03 0.5/0.5 1/0.97',vignette=PI/5,noise=alls=10:allf=t+u",
           "warm": "colorbalance=rs=0.06:bs=-0.06:rm=0.04:bm=-0.04,eq=saturation=1.06,vignette=PI/6",
           "mono": "hue=s=0,eq=contrast=1.1,noise=alls=7:allf=t+u",
           "matte": "curves=all='0/0.08 1/0.92',eq=saturation=0.85"}
DEFAULTS = {"layout": "auto", "motion": "kenburns", "transition": "mix", "sec": 2.8, "trans": 0.6,
            "rhythm": "even", "look": "film", "bg": "#101010", "intro": "", "outro": "", "font": "serif"}


def _ease(t: float) -> float:
    return 0.5 - 0.5 * math.cos(math.pi * max(0.0, min(1.0, t)))


def _font(kind: str, size: int):
    names = (["DejaVuSerif.ttf", "LiberationSerif-Regular.ttf"] if kind == "serif"
             else ["DejaVuSans.ttf", "LiberationSans-Regular.ttf"])
    for n in names:
        for d in ["/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/truetype/liberation2",
                  "/usr/share/fonts/truetype/liberation", "/Library/Fonts", "/System/Library/Fonts"]:
            if (Path(d) / n).exists():
                return ImageFont.truetype(str(Path(d) / n), size)
    return ImageFont.load_default()


def _load(p: Path, max_side: int) -> Image.Image:
    im = ImageOps.exif_transpose(Image.open(p)).convert("RGB")
    im.thumbnail((max_side, max_side), Image.LANCZOS)
    return im


def _cover(im: Image.Image, W: int, H: int) -> Image.Image:
    return ImageOps.fit(im, (W, H), Image.LANCZOS)


def _shadowed(fg: Image.Image, r: int) -> Image.Image:
    """RGBA: fg with a soft drop shadow."""
    pad = r * 3
    out = Image.new("RGBA", (fg.width + 2 * pad, fg.height + 2 * pad), (0, 0, 0, 0))
    sh = Image.new("RGBA", out.size, (0, 0, 0, 0))
    ImageDraw.Draw(sh).rectangle([pad, pad + r // 2, pad + fg.width, pad + fg.height + r // 2],
                                 fill=(0, 0, 0, 150))
    out = Image.alpha_composite(out, sh.filter(ImageFilter.GaussianBlur(r)))
    out.paste(fg.convert("RGBA"), (pad, pad))
    return out


def _fit(im: Image.Image, w: int, h: int) -> Image.Image:
    s = min(w / im.width, h / im.height)
    return im.resize((max(1, round(im.width * s)), max(1, round(im.height * s))), Image.LANCZOS)


def _blur_bg(im: Image.Image, W: int, H: int) -> Image.Image:
    bg = _cover(im, W // 4, H // 4).filter(ImageFilter.GaussianBlur(10)).resize((W, H), Image.BICUBIC)
    return Image.eval(bg, lambda v: int(v * 0.62))


# ── One "scene" = background + optional floating foreground, or a full-bleed source ──
def _scene(ims: list[Image.Image], layout: str, W: int, H: int, bg: str, idx: int) -> dict:
    im = ims[0]
    if layout == "auto":
        layout = "fill" if im.height >= im.width * 1.15 else "blur"
    if layout == "fill":
        return {"src": _cover(im, int(W * 1.25), int(H * 1.25)) if im.height >= im.width
                else _fit(im, 10 ** 6, int(H * 1.12)), "box": (W, H)}
    if layout == "cinema":
        bh = int(W / 2.39)
        return {"src": _cover(im, int(W * 1.3), int(bh * 1.3)) if im.width / im.height < 2.39 * 1.3
                else _fit(im, 10 ** 6, int(bh * 1.15)), "box": (W, bh), "band": True}
    if layout == "blur":
        return {"bg": _blur_bg(im, W, H), "fg": _shadowed(_fit(im, int(W * 0.92), int(H * 0.78)), W // 60)}
    colour = Image.new("RGB", (W, H), bg)
    if layout == "gallery":
        f = ImageOps.expand(_fit(im, int(W * 0.8), int(H * 0.66)), border=max(2, W // 120), fill="white")
        return {"bg": colour, "fg": _shadowed(f, W // 40)}
    if layout == "polaroid":
        f = _fit(im, int(W * 0.78), int(H * 0.55))
        m = int(f.width * 0.05)
        card = Image.new("RGB", (f.width + 2 * m, f.height + m + int(f.width * 0.18)), "#f7f5ef")
        card.paste(f, (m, m))
        rot = (-2.5, 2.0, -1.2, 2.8)[idx % 4]
        fg = _shadowed(card, W // 45).rotate(rot, resample=Image.BICUBIC, expand=True)
        return {"bg": _blur_bg(im, W, H) if bg == "blur" else colour, "fg": fg}
    if layout == "diptych":
        gap = W // 30
        cells = [_fit(x, W - 2 * gap, (H - 3 * gap) // 2) for x in ims[:2]]
        canvas = Image.new("RGB", (W, H), bg)
        y = (H - sum(c.height for c in cells) - gap * (len(cells) - 1)) // 2
        for c in cells:
            canvas.paste(c, ((W - c.width) // 2, y))
            y += c.height + gap
        return {"bg": canvas, "fg": None}
    raise ValueError(layout)


def _motion(kind: str, idx: int, rnd: random.Random) -> str:
    if kind == "kenburns":
        return ["zoomin", "panr", "zoomout", "panl", "zoomin_up"][idx % 5]
    if kind == "pan":
        return "panr" if idx % 2 == 0 else "panl"
    return kind


def _frame(sc: dict, mv: str, t: float, W: int, H: int) -> Image.Image:
    e = _ease(t)
    if "src" in sc:                                       # full-bleed: move a sub-pixel crop box
        src, (bw, bh) = sc["src"], sc["box"]
        ar = bw / bh
        sw, sh = src.size
        base_h = min(sh, sw / ar)
        z = {"zoomin": 1 - 0.12 * e, "zoomin_up": 1 - 0.1 * e, "zoomout": 0.88 + 0.12 * e}.get(mv, 0.9)
        if mv == "still":
            z = 1.0
        ch = base_h * z
        cw = ch * ar
        fx = {"panr": e, "panl": 1 - e}.get(mv, 0.5)
        fy = 0.6 - 0.2 * e if mv == "zoomin_up" else 0.5
        x0, y0 = (sw - cw) * fx, (sh - ch) * fy
        img = src.resize((bw, bh), Image.BICUBIC, box=(x0, y0, x0 + cw, y0 + ch))
        if sc.get("band"):
            out = Image.new("RGB", (W, H), "black")
            out.paste(img, (0, (H - bh) // 2))
            return out
        return img
    out = sc["bg"].copy()
    fg = sc.get("fg")
    if fg is None:
        if mv == "still":
            return out
        s = 1 + 0.06 * (e if mv != "zoomout" else 1 - e)
        w, h = W / s, H / s
        return out.resize((W, H), Image.BICUBIC, box=((W - w) / 2, (H - h) / 2, (W + w) / 2, (H + h) / 2))
    s = {"zoomin": 0.97 + 0.06 * e, "zoomin_up": 0.97 + 0.05 * e, "zoomout": 1.03 - 0.06 * e,
         "still": 1.0}.get(mv, 1.0)
    dx = {"panr": -0.025 + 0.05 * e, "panl": 0.025 - 0.05 * e}.get(mv, 0.0) * W
    dy = (0.012 - 0.024 * e) * H if mv in ("float", "zoomin_up") else 0.0
    if mv == "float":
        s = 1.0 + 0.025 * math.sin(math.pi * t)
    f = fg.resize((max(1, round(fg.width * s)), max(1, round(fg.height * s))), Image.BICUBIC)
    out.paste(f, (round((W - f.width) / 2 + dx), round((H - f.height) / 2 + dy)), f)
    return out


def _card(text: str, W: int, H: int, bg: str, font: str, under: Image.Image | None) -> dict:
    canvas = (_blur_bg(under, W, H) if under is not None and bg == "blur"
              else Image.new("RGB", (W, H), bg if bg != "blur" else "#101010"))
    d = ImageDraw.Draw(canvas)
    light = sum(canvas.resize((1, 1)).getpixel((0, 0))) > 380
    ink = "#141414" if light else "#f2efe8"
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()][:4]
    sizes = [int(W * 0.075)] + [int(W * 0.04)] * 3
    y = H / 2 - sum(sizes[i] * 1.5 for i in range(len(lines))) / 2
    for i, ln in enumerate(lines):
        f = _font(font, sizes[i])
        track = sizes[i] * (0.12 if i else 0.04)          # letter-spacing
        widths = [d.textlength(ch, font=f) for ch in ln]
        x = (W - (sum(widths) + track * (len(ln) - 1))) / 2
        for ch, w in zip(ln, widths):
            d.text((x, y), ch, font=f, fill=ink)
            x += w + track
        y += sizes[i] * 1.5
        if i == 0 and len(lines) > 1:
            d.line([(W * 0.44, y - sizes[0] * 0.2), (W * 0.56, y - sizes[0] * 0.2)], fill=ink, width=2)
    return {"bg": canvas, "fg": None}


def durations(n: int, o: dict) -> list[float]:
    sec = float(o["sec"])
    if o["rhythm"] == "accelerate":
        return [max(0.9, sec * (1.35 - 0.7 * i / max(1, n - 1))) for i in range(n)]
    if o["rhythm"] == "pulse":
        return [sec * (1.3 if i % 2 == 0 else 0.7) for i in range(n)]
    return [sec] * n


def plan_length(n_photos: int, o: dict) -> float:
    o = {**DEFAULTS, **o}
    n = math.ceil(n_photos / 2) if o["layout"] == "diptych" else n_photos
    ds = durations(n, o) + [2.2] * (bool(o["intro"].strip()) + bool(o["outro"].strip()))
    return sum(ds)


def render(photos: list[Path], out: Path, opts: dict, preview: bool = False,
           cb: Callable[[str, float], None] | None = None) -> Path:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is not installed on the server")
    o = {**DEFAULTS, **opts}
    W, H = (540, 960) if preview else (1080, 1920)
    cb = cb or (lambda m, f: None)
    rnd = random.Random(7)
    ims = [_load(p, 1400 if preview else 2400) for p in photos]
    groups = [ims[i:i + 2] for i in range(0, len(ims), 2)] if o["layout"] == "diptych" else [[x] for x in ims]
    scenes = [(_scene(g, o["layout"], W, H, o["bg"], i), _motion(o["motion"], i, rnd)) for i, g in enumerate(groups)]
    ds = durations(len(scenes), o)
    if o["intro"].strip():
        scenes.insert(0, (_card(o["intro"], W, H, o["bg"], o["font"], ims[0]), "zoomout"))
        ds.insert(0, 2.2)
    if o["outro"].strip():
        scenes.append((_card(o["outro"], W, H, o["bg"], o["font"], ims[-1]), "zoomin"))
        ds.append(2.2)
    cut = o["transition"] == "cut" or len(scenes) == 1
    tr = 0.0 if cut else min(float(o["trans"]), min(ds) * 0.45)
    enc = ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast",
           "-crf", "26" if preview else "14", "-r", str(FPS)]
    with tempfile.TemporaryDirectory() as td:
        clips = []
        for i, ((sc, mv), d) in enumerate(zip(scenes, ds)):
            cb(f"Drawing scene {i + 1}/{len(scenes)}", i / (len(scenes) + 1))
            n = max(2, round((d + tr) * FPS))
            p = Path(td) / f"c{i:03d}.mp4"
            ff = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
                                   "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", *enc, str(p)],
                                  stdin=subprocess.PIPE, stderr=subprocess.PIPE)
            for k in range(n):
                ff.stdin.write(_frame(sc, mv, k / (n - 1), W, H).tobytes())
            ff.stdin.close()
            if ff.wait() != 0:
                raise RuntimeError(ff.stderr.read().decode()[-400:])
            clips.append((p, n / FPS))
        cb("Joining with transitions + look…", len(scenes) / (len(scenes) + 1))
        inputs = sum([["-i", str(p)] for p, _ in clips], [])
        if cut:
            chain = "".join(f"[{i}:v]" for i in range(len(clips))) + f"concat=n={len(clips)}:v=1:a=0[v0]"
        else:
            parts, last, off = [], "[0:v]", 0.0
            seq = MIX if o["transition"] == "mix" else [o["transition"]]
            for i in range(1, len(clips)):
                off += clips[i - 1][1] - tr
                name = seq[(i - 1) % len(seq)]
                parts.append(f"{last}[{i}:v]xfade=transition={name}:duration={tr:.3f}:offset={off:.3f}[x{i}]")
                last = f"[x{i}]"
            chain = ";".join(parts) + f";{last}null[v0]"
        total = sum(l for _, l in clips) - tr * (len(clips) - 1)
        vf = ",".join(x for x in [LOOK_VF.get(o["look"], ""), "fade=t=in:st=0:d=0.4",
                                  f"fade=t=out:st={max(0, total - 0.5):.3f}:d=0.5", "format=yuv420p"] if x)
        cmd = ["ffmpeg", "-y", "-loglevel", "error", *inputs,
               "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
               "-filter_complex", f"{chain};[v0]{vf}[v]", "-map", "[v]", "-map", f"{len(clips)}:a",
               "-c:v", "libx264", "-profile:v", "high", "-pix_fmt", "yuv420p", "-r", str(FPS),
               "-preset", "ultrafast" if preview else "medium", "-crf", "28" if preview else "17",
               "-maxrate", "14M", "-bufsize", "28M", "-c:a", "aac", "-b:a", "128k", "-shortest",
               "-movflags", "+faststart", str(out)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(r.stderr[-600:])
    cb("Done", 1.0)
    return out
