#!/usr/bin/env python3
"""直接抓取 Xwayland 屏幕 → PNG（无需 VNC 密码的排障工具）。

为什么需要它：
  * 本机没有 `xwd` / `import` / `scrot`，`ffmpeg` 也没有 x11/xcb 抓屏；
  * 但可以直接用 ctypes 调 libX11 的 `XGetImage`（以 root 打开同一个 `:99`），
    自己写 PNG（zlib）——不经过 VNC、不需要随机密码，看画面最快。

用法：
    python3 tools/x11-screenshot.py                       # :99 → /tmp/game_screen.png
    VS_DISPLAY=:99 VS_OUT=/tmp/shot.png python3 tools/x11-screenshot.py

踩坑备注：`XImage` 用 `ctypes.Structure` 读出来是垃圾值，必须用
`struct.unpack_from` 手工偏移（masks 在偏移 56，前面有 4 字节对齐填充）。
"""

import ctypes
import os
import struct
import sys
import zlib
from pathlib import Path

DISPLAY = os.environ.get("VS_DISPLAY", ":99")
OUT = Path(os.environ.get("VS_OUT", "/tmp/game_screen.png"))

lib = ctypes.CDLL("/usr/lib/x86_64-linux-gnu/libX11.so.6")
lib.XOpenDisplay.restype = ctypes.c_void_p
lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
lib.XDefaultRootWindow.restype = ctypes.c_ulong
lib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
lib.XDisplayWidth.argtypes = [ctypes.c_void_p, ctypes.c_int]
lib.XDisplayHeight.argtypes = [ctypes.c_void_p, ctypes.c_int]
lib.XDefaultScreen.argtypes = [ctypes.c_void_p]
lib.XDefaultScreen.restype = ctypes.c_int
lib.XGetImage.restype = ctypes.c_void_p
lib.XGetImage.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int,
                          ctypes.c_uint, ctypes.c_uint, ctypes.c_ulong, ctypes.c_int]
lib.XDestroyImage.argtypes = [ctypes.c_void_p]


def write_png(path: Path, w: int, h: int, rows: list[bytes]) -> None:
    raw = b"".join(b"\x00" + r for r in rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    path.write_bytes(b"\x89PNG\r\n\x1a\n"
                     + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw, 6))
                     + chunk(b"IEND", b""))


dpy = lib.XOpenDisplay(DISPLAY.encode())
if not dpy:
    sys.exit(f"无法打开 {DISPLAY}")
scr = lib.XDefaultScreen(dpy)
tw, th = lib.XDisplayWidth(dpy, scr), lib.XDisplayHeight(dpy, scr)
root = lib.XDefaultRootWindow(dpy)
img = lib.XGetImage(dpy, root, 0, 0, tw, th, 0xFFFFFFFF, 2)
if not img:
    sys.exit("XGetImage 失败")

hdr = ctypes.string_at(img, 80)
(width, height, _xo, _fmt, data_ptr, byte_order, _bu, _bbo, _bp,
 _depth, bpl, bpp) = struct.unpack_from("<4iQ7i", hdr, 0)
rm, gm, bm = struct.unpack_from("<3Q", hdr, 56)   # masks 在偏移 56（前面有 4 字节填充）
buf = ctypes.string_at(data_ptr, bpl * height)

shift = lambda m: (((m & -m).bit_length() - 1), m >> ((m & -m).bit_length() - 1)) if m else (0, 0)
rs, gs, bs = shift(rm), shift(gm), shift(bm)
little = byte_order == 0
step = bpp // 8
rows = []
for y in range(height):
    line = buf[y * bpl:(y + 1) * bpl]
    out = bytearray()
    for x in range(width):
        px = int.from_bytes(line[x * step:x * step + step], "little" if little else "big")
        out += bytes(((px & rm) >> rs[0], (px & gm) >> gs[0], (px & bm) >> bs[0]))
    rows.append(bytes(out))

write_png(OUT, width, height, rows)
lib.XDestroyImage(img)
print(f"已保存 {OUT} ({width}x{height})")
