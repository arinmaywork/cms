"""
src/ig_reel.py — Reel helpers: ffprobe facts, pre-flight checks, cover frames.

Instagram Reels via API: MP4/MOV (H.264/HEVC + AAC), 3 s – 15 min, ≤ 300 MB,
9:16 recommended (1080×1920). Cloudinary's free plan caps a single video at
100 MB — larger files need a paid plan or a smaller export.
"""

import json
import shutil
import subprocess
from pathlib import Path

VIDEO_EXTS = {".mp4", ".mov", ".m4v"}
CLOUDINARY_FREE_MB = 100


def videos_in(folder: Path) -> list[Path]:
    try:
        return sorted(f for f in Path(folder).iterdir()
                      if f.suffix.lower() in VIDEO_EXTS and not f.name.startswith("."))
    except OSError:
        return []


def probe(path: Path) -> dict:
    """{'duration', 'width', 'height', 'codec', 'audio', 'size_mb'} — empty values if ffprobe is missing."""
    info = {"duration": 0.0, "width": 0, "height": 0, "codec": "", "audio": "",
            "size_mb": Path(path).stat().st_size / 1_048_576}
    if not shutil.which("ffprobe"):
        return info
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format",
                              "-show_streams", str(path)], capture_output=True, text=True, timeout=60)
        data = json.loads(out.stdout or "{}")
        info["duration"] = float(data.get("format", {}).get("duration", 0) or 0)
        for s in data.get("streams", []):
            if s.get("codec_type") == "video" and not info["codec"]:
                w, h = int(s.get("width", 0)), int(s.get("height", 0))
                rot = str(s.get("tags", {}).get("rotate", "")) or str(
                    (s.get("side_data_list") or [{}])[0].get("rotation", ""))
                if rot.strip("-") in ("90", "270"):
                    w, h = h, w
                info.update(width=w, height=h, codec=s.get("codec_name", ""))
            if s.get("codec_type") == "audio" and not info["audio"]:
                info["audio"] = s.get("codec_name", "")
    except Exception as e:
        print(f"  [reel] ffprobe failed: {e}")
    return info


def checks(info: dict) -> list[tuple[str, str]]:
    """[(level, message)] — level: ok | warn | error. Errors block publishing."""
    out: list[tuple[str, str]] = []
    d, w, h, mb = info["duration"], info["width"], info["height"], info["size_mb"]
    if d:
        out.append(("error", f"Duration {d:.1f}s — Reels need 3 s – 15 min")
                   if not 3 <= d <= 900 else ("ok", f"Duration {d:.1f}s"))
    else:
        out.append(("warn", "Duration unknown (install ffmpeg on the server for checks)"))
    if w and h:
        r = w / h
        out.append(("ok", f"{w}×{h} (9:16)") if abs(r - 9 / 16) < 0.02 else
                   ("warn", f"{w}×{h} — not 9:16; Instagram will show it letterboxed or cropped"))
        if max(w, h) < 1080:
            out.append(("warn", "Below 1080p — may look soft"))
    if info["codec"] and info["codec"] not in ("h264", "hevc"):
        out.append(("error", f"Video codec {info['codec']} — export as H.264 or HEVC"))
    if info["codec"] and not info["audio"]:
        out.append(("warn", "No audio track — add music in your editor before exporting "
                            "(the API can't add Instagram's music)"))
    if mb > 300:
        out.append(("error", f"{mb:.0f} MB — Instagram's limit is 300 MB"))
    elif mb > CLOUDINARY_FREE_MB:
        out.append(("warn", f"{mb:.0f} MB — over Cloudinary's free 100 MB per-video cap"))
    else:
        out.append(("ok", f"{mb:.0f} MB"))
    return out


def frame_at(path: Path, second: float, out: Path | None = None, width: int = 1080) -> bytes | None:
    """JPEG of the frame at `second` (also written to `out` if given)."""
    if not shutil.which("ffmpeg"):
        return None
    try:
        r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{max(0, second):.2f}", "-i", str(path),
                            "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "3",
                            "-f", "image2", "pipe:1"], capture_output=True, timeout=60)
        if r.stdout and out:
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_bytes(r.stdout)
        return r.stdout or None
    except Exception:
        return None
