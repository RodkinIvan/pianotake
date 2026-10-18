"""Draw the pianotake icon (piano keys + red record dot) with numpy.

Usage: python make_icon.py OUT_DIR
Writes OUT_DIR/icon_1024.png and pianotake.ico (Windows), plus pianotake.icns on macOS
(via iconutil). `python make_icon.py --package` refreshes pianotake/icon.png, the window icon.
"""
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import numpy as np

N = 2048  # drawn at 2x and averaged down to 1024 for smooth edges


def png_bytes(rgba):
    h, w, _ = rgba.shape
    raw = b"".join(b"\x00" + rgba[y].tobytes() for y in range(h))

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def draw():
    """The icon as float RGBA in [0, 1], 1024x1024."""
    y, x = np.mgrid[0:N, 0:N].astype(np.float64) / N   # 0..1
    img = np.zeros((N, N, 4))

    def paint(mask, color):
        a = mask[..., None] * (color[3] if len(color) == 4 else 1.0)
        img[..., :3] = img[..., :3] * (1 - a) + np.array(color[:3]) * a
        img[..., 3:] = np.maximum(img[..., 3:], a)

    # macOS-style rounded square (superellipse) inside the standard 10% margin
    m, k = 0.10, 5.0
    u, v = (x - 0.5) / (0.5 - m), (y - 0.5) / (0.5 - m)
    tile = (np.abs(u) ** k + np.abs(v) ** k) <= 1.0
    shade = 0.16 - 0.06 * y                                 # gentle top-to-bottom gradient
    paint(tile, (0, 0, 0))
    img[..., :3] = np.where(tile[..., None], np.stack([shade, shade, shade * 1.15], -1), img[..., :3])

    # keyboard: 7 white keys with 5 black keys, lower part of the tile
    kx0, kx1, ky0, ky1 = 0.20, 0.80, 0.55, 0.80
    kw = (kx1 - kx0) / 7
    gap = 0.006
    for i in range(7):
        a0 = kx0 + i * kw + gap
        a1 = kx0 + (i + 1) * kw - gap
        paint((x >= a0) & (x <= a1) & (y >= ky0) & (y <= ky1) & tile, (0.96, 0.96, 0.94))
    for i in (0, 1, 3, 4, 5):                               # C# D# F# G# A#
        c = kx0 + (i + 1) * kw
        paint((np.abs(x - c) <= kw * 0.30) & (y >= ky0) & (y <= ky0 + (ky1 - ky0) * 0.6), (0.08, 0.08, 0.1))

    # record dot with a soft ring
    cx, cy, r = 0.5, 0.36, 0.13
    d = np.hypot(x - cx, y - cy)
    paint((d <= r * 1.28) & (d > r * 1.12), (0.86, 0.17, 0.17, 0.35))
    paint(d <= r, (0.86, 0.16, 0.16))
    paint((d <= r * 0.42) & (np.hypot(x - (cx - r * 0.35), y - (cy - r * 0.35)) <= r * 0.42) & (d <= r),
          (1, 1, 1, 0.12))                                  # highlight

    return np.clip(img.reshape(N // 2, 2, N // 2, 2, 4).mean(axis=(1, 3)), 0, 1)


def sized(icon, px):
    """Box-filter the 1024 px icon down to px (a power of two) as uint8 RGBA."""
    f = icon.shape[0] // px
    return (icon.reshape(px, f, px, f, 4).mean(axis=(1, 3)) * 255).round().astype(np.uint8)


def ico_bytes(icon, sizes=(16, 32, 64, 128, 256)):
    """Windows .ico holding PNG images (supported since Windows Vista)."""
    images = [png_bytes(sized(icon, s)) for s in sizes]
    out = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    for s, data in zip(sizes, images):
        out += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    return out + b"".join(images)


def main(out_dir):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    icon = draw()
    (out / "icon_1024.png").write_bytes(png_bytes(sized(icon, 1024)))
    (out / "pianotake.ico").write_bytes(ico_bytes(icon))
    if sys.platform == "darwin":
        iconset = out / "pianotake.iconset"
        iconset.mkdir(exist_ok=True)
        for size in (16, 32, 128, 256, 512):
            (iconset / f"icon_{size}x{size}.png").write_bytes(png_bytes(sized(icon, size)))
            (iconset / f"icon_{size}x{size}@2x.png").write_bytes(png_bytes(sized(icon, size * 2)))
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(out / "pianotake.icns")], check=True)
    print(out)


if __name__ == "__main__":
    if sys.argv[1:] == ["--package"]:
        target = Path(__file__).resolve().parents[1] / "pianotake" / "icon.png"
        target.write_bytes(png_bytes(sized(draw(), 256)))
        print(target)
    else:
        main(sys.argv[1] if len(sys.argv) > 1 else ".")
