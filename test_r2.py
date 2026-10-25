"""Offline check for src/r2.py image prep: python test_r2.py"""
import io
import tempfile
from pathlib import Path

from PIL import Image

from src.r2 import image_key, prepare_image, specs_line


def make(path, fmt="JPEG", orient=1):
    im = Image.new("RGB", (60, 40), "red")
    ex = Image.Exif()
    ex[0x0110] = "X-T30"
    ex[0x0112] = orient
    ex.get_ifd(0x8769)[0x9003] = "2025:01:14 09:30:00"
    ex.get_ifd(0x8825)[2] = (64.0, 8.0, 0.0)          # GPS latitude
    im.save(path, fmt, exif=ex, icc_profile=b"icc-stub" if fmt == "JPEG" else None)


def scan_data(b: bytes) -> bytes:
    return b[b.index(b"\xff\xda"):]                   # entropy-coded pixels, from start of scan


with tempfile.TemporaryDirectory() as d:
    d = Path(d)
    make(d / "a.jpg")
    raw = (d / "a.jpg").read_bytes()
    p = prepare_image(d / "a.jpg")
    out = Image.open(io.BytesIO(p["data"]))
    assert out.format == "JPEG" and not out.getexif() and b"Exif" not in p["data"], "metadata leaked"
    assert scan_data(p["data"]) == scan_data(raw), "pixels were touched"
    assert out.info.get("icc_profile") == b"icc-stub"
    assert (p["width"], p["height"]) == (60, 40)
    assert p["exif"]["camera"] == "X-T30" and p["exif"]["taken_at"] == "2025-01-14T09:30:00"

    make(d / "b.jpg", orient=6)                       # rotated: keep only Orientation
    raw = (d / "b.jpg").read_bytes()
    p = prepare_image(d / "b.jpg")
    ex = Image.open(io.BytesIO(p["data"])).getexif()
    assert dict(ex) == {0x0112: 6}, f"expected only Orientation, got {dict(ex)}"
    assert scan_data(p["data"]) == scan_data(raw), "pixels were touched"
    assert (p["width"], p["height"]) == (40, 60)

    make(d / "c.webp", "WEBP")
    try:
        prepare_image(d / "c.webp")
        raise AssertionError("non-JPEG should be refused")
    except ValueError as e:
        assert "JPEG" in str(e)

k = image_key(b"abc", "My Photo (1).JPG")
assert k.startswith("img/My-Photo-1-") and k.endswith(".jpg") and len(k.split("-")[-1]) == 16, k
assert image_key(b"abc", "x.jpg") == image_key(b"abc", "x.jpeg") and image_key(b"abc") != image_key(b"abd")
assert specs_line({"camera": "X-T30", "iso": 200}) == "X-T30 · ISO 200"
print("ok")
