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
