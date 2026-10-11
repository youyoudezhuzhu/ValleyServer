#!/usr/bin/env python3
"""生成 FPK 所需的图标（纯标准库，不依赖 Pillow）。

产出：
  fnpack/ICON.PNG                 64x64   应用中心图标
  fnpack/ICON_256.PNG             256x256 应用中心图标（大）
  fnpack/app/ui/images/icon_64.png   桌面入口图标
  fnpack/app/ui/images/icon_256.png  桌面入口图标（大）
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def png_bytes(width: int, height: int, pixels: bytes) -> bytes:
    """把 RGBA 像素数据编码为 PNG。"""
    raw = bytearray()
    stride = width * 4
    for y in range(height):
        raw.append(0)  # filter type: none
        raw += pixels[y * stride:(y + 1) * stride]

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))


def render(size: int) -> bytes:
    """画一个圆角深色方块 + 绿色渐变 + 白色「VS」字样。"""
    buf = bytearray(size * size * 4)
    radius = size * 0.22
    pad = size * 0.04

    # 5x7 点阵字模（V / S），用于在任意尺寸下画出可辨识的标记
    GLYPHS = {
        "V": ["10001", "10001", "10001", "10001", "10001", "01010", "00100"],
        "S": ["01111", "10000", "10000", "01110", "00001", "00001", "11110"],
    }

    def set_px(x: int, y: int, r: int, g: int, b: int, a: int = 255) -> None:
        if 0 <= x < size and 0 <= y < size:
            i = (y * size + x) * 4
            # 简单 source-over 合成
            ba = buf[i + 3]
            na = a + ba * (255 - a) // 255
            if na == 0:
                return
            for ch, val in enumerate((r, g, b)):
                old = buf[i + ch]
                buf[i + ch] = (val * a + old * ba * (255 - a) // 255) // na
            buf[i + 3] = na

    # 圆角方块背景（深绿→亮绿纵向渐变）
    for y in range(size):
        for x in range(size):
            cx = min(x + 0.5, size - x - 0.5)
            cy = min(y + 0.5, size - y - 0.5)
            if cx < pad or cy < pad:
                continue
            inside = True
            if cx < pad + radius and cy < pad + radius:
                dx = (pad + radius) - cx
                dy = (pad + radius) - cy
                inside = (dx * dx + dy * dy) <= radius * radius
            if not inside:
                continue
            t = y / max(1, size - 1)
            r = int(34 + 40 * t)
            g = int(120 + 60 * t)
            b = int(62 + 30 * t)
            set_px(x, y, r, g, b)

    # 中央「VS」
    gw, gh = 5, 7
    scale = max(1, int(size * 0.052))
    text_w = gw * scale * 2 + scale * 2
    x0 = (size - text_w) // 2
    y0 = (size - gh * scale) // 2 + int(size * 0.01)
    for gi, ch in enumerate(("V", "S")):
        gx = x0 + gi * (gw * scale + scale * 2)
        for row, line in enumerate(GLYPHS[ch]):
            for col, bit in enumerate(line):
                if bit != "1":
                    continue
                for dy in range(scale):
                    for dx in range(scale):
                        set_px(gx + col * scale + dx, y0 + row * scale + dy,
                               236, 248, 238)
    return bytes(buf)


def downsample(pixels: bytes, size: int, target: int) -> bytes:
    if target == size:
        return pixels
    factor = size // target
    out = bytearray(target * target * 4)
    for y in range(target):
        for x in range(target):
            acc = [0, 0, 0, 0]
            for dy in range(factor):
                for dx in range(factor):
                    i = ((y * factor + dy) * size + (x * factor + dx)) * 4
                    for c in range(4):
                        acc[c] += pixels[i + c]
            n = factor * factor
            j = (y * target + x) * 4
            for c in range(4):
                out[j + c] = acc[c] // n
    return bytes(out)


def main() -> None:
    base = render(256)
    outputs = [
        (ROOT / "fnpack" / "ICON.PNG", 64, downsample(base, 256, 64)),
        (ROOT / "fnpack" / "ICON_256.PNG", 256, base),
        (ROOT / "fnpack" / "app" / "ui" / "images" / "icon_64.png", 64, downsample(base, 256, 64)),
        (ROOT / "fnpack" / "app" / "ui" / "images" / "icon_256.png", 256, base),
    ]
    for path, side, pixels in outputs:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(png_bytes(side, side, pixels))
        print(f"written {path} ({side}x{side}, {path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
