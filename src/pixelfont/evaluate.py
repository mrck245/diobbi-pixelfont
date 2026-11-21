"""Pruebas de calidad: unicidad, topología y legibilidad con OCR."""
from __future__ import annotations

import os
import subprocess
import tempfile
from collections import defaultdict

import numpy as np
from PIL import Image, ImageFilter

from .render import render_line, upscale

TESSDATA = os.environ.get("PIXELFONT_TESSDATA", "/home/claude/tessdata")


def collisions(bitmaps, names):
    """Glifos distintos en el original que quedan idénticos en píxeles
    (se ignoran los que ya eran prácticamente iguales en el original)."""
    from .core import same_design
    members = defaultdict(list)
    for n in names:
        b = bitmaps[n]
        if b.bits is None or not b.bits.any():
            continue
        members[b.key()].append(n)
    out = []
    for k, ns in members.items():
        reps = []
        for n in ns:
            g = bitmaps[n]
            if not any(g.hires_hash == bitmaps[r].hires_hash or same_design(g, bitmaps[r]) for r in reps):
                reps.append(n)
        if len(reps) > 1:
            out.append(sorted(reps))
    return out


def topo_failures(bitmaps, names):
    return [n for n in names if bitmaps[n].issues]


def _lev(a, b):
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def similarity(ocr, truth):
    a = "".join(ocr.split())
    b = "".join(truth.split())
    if not b:
        return 1.0
    return max(0.0, 1 - _lev(a, b) / len(b))


def ocr(img: Image.Image, lang: str, psm=7) -> str:
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "in.png")
        img.save(p)
        env = dict(os.environ, TESSDATA_PREFIX=TESSDATA, OMP_THREAD_LIMIT="1")
        r = subprocess.run(["tesseract", p, "stdout", "-l", lang, "--psm", str(psm)],
                           capture_output=True, text=True, env=env)
        return r.stdout.strip()


def ocr_score(font_data, lines, lang, px, mono, target_em=48):
    scores = []
    for line in lines:
        img = render_line(font_data, line, px, mono)
        if mono:
            f = max(1, round(target_em / px))
            img = upscale(img, f).filter(ImageFilter.GaussianBlur(f * 0.35))
        scores.append(similarity(ocr(img, lang), line))
    return float(np.mean(scores))


def line_pieces(font_data, line, px, mono, ref_px=None, dot_triangles=True):
    """Cuenta las piezas separadas (componentes conexos) de una línea
    renderizada. En una fuente pixel, puntos o acentos pegados al cuerpo
    reducen el número de piezas frente al original."""
    from scipy import ndimage
    img = np.array(render_line(font_data, line, px, mono)) < 128
    lab, n = ndimage.label(img, structure=np.ones((3, 3), bool))
    if n == 0:
        return 0
    if mono and dot_triangles:
        # grupos de puntos que solo se tocan en diagonal (p. ej. el triángulo
        # de ث) cuentan como puntos separados
        extra = 0
        for k in range(1, n + 1):
            part = lab == k
            sub, m = ndimage.label(part)
            ys, xs = np.nonzero(part)
            shape = (ys.max() - ys.min() + 1, xs.max() - xs.min() + 1)
            # triángulo de 3 puntos de 1 px (.#. / #.#), forma estándar en pixel art
            if m == 3 and part.sum() == 3 and shape in ((2, 3), (3, 2)):
                extra += 2
        return n + extra
    if ref_px:  # original grande: ignorar motas menores que ~0.3 px del tamaño destino
        areas = ndimage.sum(img, lab, index=np.arange(1, n + 1))
        min_area = 0.3 * (px / ref_px) ** 2
        return int((areas >= min_area).sum())
    return n


def pieces_ratio(pixel_data, src_data, lines, size, ref_px=64):
    """Un triángulo de 3 píxeles puede ser tres puntos (ث) o una breve (ă):
    por línea se usa la lectura que coincide mejor con el original."""
    got = ref = 0
    for ln in lines:
        r = line_pieces(src_data, ln, ref_px, False, ref_px=size)
        a = line_pieces(pixel_data, ln, size, True, dot_triangles=True)
        b = line_pieces(pixel_data, ln, size, True, dot_triangles=False)
        got += a if abs(a - r) <= abs(b - r) else b
        ref += r
    return got / ref if ref else 1.0, got, ref
