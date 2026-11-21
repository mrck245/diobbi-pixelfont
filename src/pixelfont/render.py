"""Renderizado de texto real: HarfBuzz para el shaping (usa GSUB/GPOS de la
propia fuente) y FreeType para dibujar los glifos."""
from __future__ import annotations

import io

import freetype
import numpy as np
import uharfbuzz as hb
from PIL import Image


def font_bytes(ttfont):
    buf = io.BytesIO()
    ttfont.save(buf)
    return buf.getvalue()


def shape(data: bytes, text: str, features=None):
    face = hb.Face(data)
    font = hb.Font(face)
    upem = face.upem
    font.scale = (upem, upem)
    buf = hb.Buffer()
    buf.add_str(text)
    buf.guess_segment_properties()
    hb.shape(font, buf, features or {})
    return buf.glyph_infos, buf.glyph_positions, upem


def render_line(data: bytes, text: str, px: int, mono: bool, pad=None):
    """Devuelve imagen L (0=tinta, 255=fondo) de una línea de texto."""
    infos, poss, upem = shape(data, text)
    face = freetype.Face(io.BytesIO(data))
    face.set_pixel_sizes(0, px)
    scale = px / upem
    flags = freetype.FT_LOAD_RENDER | freetype.FT_LOAD_NO_HINTING
    if mono:
        flags |= freetype.FT_LOAD_TARGET_MONO
    asc = face.size.ascender / 64
    desc = -face.size.descender / 64
    pad = pad if pad is not None else max(2, px // 3)
    # calcular ancho total y extensión vertical real
    pen_x = 0.0
    items = []
    top, bot = asc, desc
    for info, pos in zip(infos, poss):
        face.load_glyph(info.codepoint, flags)
        g = face.glyph
        bm = g.bitmap
        if bm.width and bm.rows:
            if mono:
                arr = np.unpackbits(np.array(bm.buffer, dtype=np.uint8).reshape(bm.rows, bm.pitch), axis=1)[:, : bm.width] * 255
            else:
                arr = np.array(bm.buffer, dtype=np.uint8).reshape(bm.rows, bm.pitch)[:, : bm.width]
            gx = pen_x + pos.x_offset * scale + g.bitmap_left
            gy = pos.y_offset * scale + g.bitmap_top  # altura sobre línea base del borde superior
            items.append((arr, gx, gy))
            top = max(top, gy)
            bot = max(bot, bm.rows - gy)
        pen_x += pos.x_advance * scale
    W = int(np.ceil(pen_x)) + 2 * pad + 4
    H = int(np.ceil(top + bot)) + 2 * pad
    canvas = np.zeros((H, W), dtype=np.float32)
    base = pad + int(np.ceil(top))
    for arr, gx, gy in items:
        x = int(round(gx)) + pad + 2
        y = base - int(round(gy))
        h, w = arr.shape
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(W, x + w), min(H, y + h)
        if x1 <= x0 or y1 <= y0:
            continue
        sub = arr[y0 - y: y1 - y, x0 - x: x1 - x].astype(np.float32)
        canvas[y0:y1, x0:x1] = np.maximum(canvas[y0:y1, x0:x1], sub)
    return Image.fromarray((255 - canvas).astype(np.uint8), "L")


def upscale(img, factor):
    return img.resize((img.width * factor, img.height * factor), Image.NEAREST)
