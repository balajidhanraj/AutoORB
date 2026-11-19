"""Compress >90 KB RGB/RGBA PNG assets to spec-compliant indexed PNGs under 95 KB."""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import numpy as np
from scipy.cluster.vq import kmeans2

ROOT_DIR = Path(__file__).resolve().parent.parent
TARGET_DIRS = (
    ROOT_DIR / "ui" / "static",
    ROOT_DIR / "mobile" / "android" / "app" / "src" / "main" / "res",
)
PNG_SIG = b"\x89PNG\r\n\x1a\n"
SIZE_THRESHOLD = 90_000
MAX_FINAL_SIZE = 95_000


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def decode_png_rgba_or_rgb(raw: bytes) -> tuple[np.ndarray, int]:
    """Decode an 8-bit non-interlaced RGB (2) or RGBA (6) PNG into HxWxC uint8."""
    if not raw.startswith(PNG_SIG):
        raise ValueError("Invalid PNG signature")

    pos = 8
    width = height = bit_depth = color_type = interlace = None
    idat_parts: list[bytes] = []

    while pos + 8 <= len(raw):
        length = struct.unpack(">I", raw[pos : pos + 4])[0]
        ctype = raw[pos + 4 : pos + 8]
        cdata = raw[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if ctype == b"IHDR":
            width, height, bit_depth, color_type, _comp, _filt, interlace = struct.unpack(
                ">IIBBBBB", cdata
            )
        elif ctype == b"IDAT":
            idat_parts.append(cdata)
        elif ctype == b"IEND":
            break

    if width is None or height is None:
        raise ValueError("Missing IHDR chunk")
    if bit_depth != 8 or interlace != 0 or color_type not in (2, 6):
        raise ValueError(
            f"Unsupported PNG format: bit_depth={bit_depth}, color_type={color_type}, interlace={interlace}"
        )

    bpp = 3 if color_type == 2 else 4
    stride = width * bpp
    decompressed = zlib.decompress(b"".join(idat_parts))
    expected_len = height * (1 + stride)
    if len(decompressed) != expected_len:
        raise ValueError(f"Unexpected decompressed length {len(decompressed)} != {expected_len}")

    out = np.zeros((height, stride), dtype=np.uint8)
    raw_arr = np.frombuffer(decompressed, dtype=np.uint8).reshape(height, 1 + stride)

    for y in range(height):
        f = int(raw_arr[y, 0])
        scan = raw_arr[y, 1:]
        prev = out[y - 1] if y > 0 else np.zeros(stride, dtype=np.uint8)
        if f == 0:
            out[y] = scan
        elif f == 2:
            out[y] = (scan.astype(np.uint16) + prev.astype(np.uint16)) & 0xFF
        elif f == 1:
            row = bytearray(scan.tobytes())
            for i in range(bpp, stride):
                row[i] = (row[i] + row[i - bpp]) & 0xFF
            out[y] = np.frombuffer(row, dtype=np.uint8)
        elif f == 3:
            row = bytearray(scan.tobytes())
            prev_b = prev.tobytes()
            for i in range(stride):
                left = row[i - bpp] if i >= bpp else 0
                row[i] = (row[i] + ((left + prev_b[i]) >> 1)) & 0xFF
            out[y] = np.frombuffer(row, dtype=np.uint8)
        elif f == 4:
            row = bytearray(scan.tobytes())
            prev_b = prev.tobytes()
            for i in range(stride):
                a = row[i - bpp] if i >= bpp else 0
                b = prev_b[i]
                c = prev_b[i - bpp] if i >= bpp else 0
                row[i] = (row[i] + _paeth(a, b, c)) & 0xFF
            out[y] = np.frombuffer(row, dtype=np.uint8)
        else:
            raise ValueError(f"Unknown PNG filter byte: {f}")

    return out.reshape(height, width, bpp), color_type


def _png_chunk(ctype: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(ctype + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + ctype + data + struct.pack(">I", crc)


def encode_indexed_png(indices: np.ndarray, palette_rgb: np.ndarray) -> bytes:
    """Encode a HxW uint8 index map + Kx3 uint8 RGB palette as a valid PNG."""
    h, w = indices.shape
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 3, 0, 0, 0)
    plte = palette_rgb.astype(np.uint8).tobytes()

    scanlines = bytearray()
    prev_row = np.zeros(w, dtype=np.uint8)
    for y in range(h):
        row = indices[y]
        left = np.empty_like(row)
        left[0] = 0
        left[1:] = row[:-1]

        f0 = row
        f1 = ((row.astype(np.int16) - left.astype(np.int16)) & 0xFF).astype(np.uint8)
        f2 = ((row.astype(np.int16) - prev_row.astype(np.int16)) & 0xFF).astype(np.uint8)

        # PNG filter selection heuristic: minimize sum of signed deltas
        s0 = int(np.abs(f0.view(np.int8)).sum())
        s1 = int(np.abs(f1.view(np.int8)).sum())
        s2 = int(np.abs(f2.view(np.int8)).sum())

        if s2 <= s0 and s2 <= s1:
            scanlines.append(2)
            scanlines.extend(f2.tobytes())
        elif s1 <= s0:
            scanlines.append(1)
            scanlines.extend(f1.tobytes())
        else:
            scanlines.append(0)
            scanlines.extend(f0.tobytes())

        prev_row = row

    compressed = zlib.compress(bytes(scanlines), level=9)
    return PNG_SIG + _png_chunk(b"IHDR", ihdr) + _png_chunk(b"PLTE", plte) + _png_chunk(b"IDAT", compressed) + _png_chunk(b"IEND", b"")


def quantize_opaque_image(rgb: np.ndarray, k_fg: int) -> tuple[np.ndarray, np.ndarray]:
    """Flatten obsidian navy background and k-means quantize foreground into <=256 palette."""
    h, w, _ = rgb.shape
    bg_rgb = rgb[0, 0].astype(np.int16)
    flat = rgb.reshape(-1, 3)

    l1_dist = np.abs(flat.astype(np.int16) - bg_rgb).sum(axis=1)
    bg_mask = l1_dist <= 12
    fg_pixels = flat[~bg_mask]

    if len(fg_pixels) == 0:
        indices = np.zeros((h, w), dtype=np.uint8)
        palette = bg_rgb.astype(np.uint8).reshape(1, 3)
        return indices, palette

    unique_fg = np.unique(fg_pixels, axis=0)
    if len(unique_fg) <= k_fg:
        centroids = unique_fg.astype(np.float64)
    else:
        rng = np.random.default_rng(42)
        if len(fg_pixels) > 40_000:
            sel = rng.choice(len(fg_pixels), size=40_000, replace=False)
            sample = fg_pixels[sel].astype(np.float64)
        else:
            sample = fg_pixels.astype(np.float64)

        init_sel = rng.choice(len(unique_fg), size=min(k_fg, len(unique_fg)), replace=False)
        init_centers = unique_fg[init_sel].astype(np.float64)
        centroids, _ = kmeans2(sample, init_centers, minit="matrix", iter=18)

    centroids_u8 = np.clip(np.rint(centroids), 0, 255).astype(np.uint8)
    palette = np.vstack([bg_rgb.astype(np.uint8).reshape(1, 3), centroids_u8])

    out_flat = np.zeros(flat.shape[0], dtype=np.uint8)
    fg_idx_positions = np.flatnonzero(~bg_mask)
    centers_i32 = centroids_u8.astype(np.int32)

    chunk_size = 200_000
    for start in range(0, len(fg_pixels), chunk_size):
        end = min(len(fg_pixels), start + chunk_size)
        pts = fg_pixels[start:end].astype(np.int32)
        diff = pts[:, None, :] - centers_i32[None, :, :]
        d2 = (diff * diff).sum(axis=2)
        nearest = np.argmin(d2, axis=1) + 1
        out_flat[fg_idx_positions[start:end]] = nearest.astype(np.uint8)

    return out_flat.reshape(h, w), palette


def optimize_file(path: Path) -> bool:
    raw = path.read_bytes()
    orig_len = len(raw)
    if orig_len <= SIZE_THRESHOLD:
        return False

    arr, color_type = decode_png_rgba_or_rgb(raw)
    if color_type == 6:
        if int(arr[:, :, 3].min()) < 250:
            raise ValueError(f"{path} has semi-transparent pixels; cannot drop alpha safely")
        rgb = arr[:, :, :3]
    else:
        rgb = arr

    best_bytes: bytes | None = None
    for k_fg in (255, 127, 63):
        indices, palette = quantize_opaque_image(rgb, k_fg=k_fg)
        candidate = encode_indexed_png(indices, palette)
        if best_bytes is None or len(candidate) < len(best_bytes):
            best_bytes = candidate
        if len(candidate) <= 92_000:
            break

    assert best_bytes is not None
    if len(best_bytes) < orig_len and len(best_bytes) < MAX_FINAL_SIZE:
        path.write_bytes(best_bytes)
        print(f"OPTIMIZED {path.relative_to(ROOT_DIR)}: {orig_len} -> {len(best_bytes)} bytes")
        return True

    raise RuntimeError(
        f"Failed to compress {path} under {MAX_FINAL_SIZE} bytes (orig={orig_len}, best={len(best_bytes)})"
    )


def main() -> None:
    count = 0
    for base in TARGET_DIRS:
        if not base.exists():
            continue
        for png_path in sorted(base.rglob("*.png")):
            if png_path.stat().st_size > SIZE_THRESHOLD:
                if optimize_file(png_path):
                    count += 1
    print(f"Completed PNG optimization: {count} file(s) compressed.")


if __name__ == "__main__":
    main()
