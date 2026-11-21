"""Carga de la fuente original: instancia variable por defecto y subset por escritura."""
from __future__ import annotations

import io

from fontTools import subset
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

ASCII_PUNCT = list(range(0x20, 0x41)) + list(range(0x5B, 0x61)) + list(range(0x7B, 0x7F))

SCRIPTS = {
    "latin": list(range(0x20, 0x7F)) + list(range(0xA0, 0x180)) + list(range(0x218, 0x21C))
    + list(range(0x2010, 0x2028)) + [0x2039, 0x203A, 0x20AC],
    "japanese": ASCII_PUNCT + list(range(0x3000, 0x3040)) + list(range(0x3040, 0x30A0))
    + list(range(0x30A0, 0x3100)) + list(range(0xFF01, 0xFF5F)),
    "tamil": ASCII_PUNCT + list(range(0x0B80, 0x0C00)) + [0x0964, 0x0965, 0x200C, 0x200D, 0x25CC],
    "arabic": ASCII_PUNCT + list(range(0x0600, 0x0700)) + list(range(0x0750, 0x0780))
    + list(range(0xFB50, 0xFE00)) + list(range(0xFE70, 0xFF00)) + [0x200C, 0x200D, 0x25CC],
    "tibetan": ASCII_PUNCT + list(range(0x0F00, 0x1000)) + [0x200C, 0x200D, 0x25CC],
}


def jis_level1_kanji():
    """Kanji del nivel 1 de JIS X 0208 (~2965, cubre casi todos los jōyō)."""
    out = []
    for row in range(0xB0, 0xD0):
        for col in range(0xA1, 0xFF):
            try:
                ch = bytes([row, col]).decode("euc_jp")
            except UnicodeDecodeError:
                continue
            if len(ch) == 1 and 0x4E00 <= ord(ch) <= 0x9FFF:
                out.append(ord(ch))
    return out


def load_source(path, script, font_number=0, extra_text=""):
    f = TTFont(path, fontNumber=font_number, lazy=False)
    if "fvar" in f:
        loc = {a.axisTag: a.defaultValue for a in f["fvar"].axes}
        f = instancer.instantiateVariableFont(f, loc, updateFontNames=False)
    unicodes = set(SCRIPTS[script])
    if script == "japanese":
        unicodes |= set(jis_level1_kanji())
    unicodes |= {ord(c) for c in extra_text}
    unicodes &= set(f.getBestCmap().keys())
    opts = subset.Options()
    opts.layout_features = ["*"]
    if script == "japanese":  # sin variantes históricas/anchos alternos
        opts.layout_features = ["ccmp", "locl", "kern", "liga", "calt", "palt", "vert", "vrt2"]
    opts.name_IDs = ["*"]
    opts.name_languages = ["*"]
    opts.glyph_names = True
    opts.notdef_outline = True
    opts.hinting = False
    opts.legacy_kern = False
    opts.drop_tables += ["vhea", "vmtx", "VORG", "BASE", "MATH", "STAT", "DSIG"]
    sub = subset.Subsetter(opts)
    sub.populate(unicodes=sorted(unicodes))
    sub.subset(f)
    buf = io.BytesIO()
    f.save(buf)
    buf.seek(0)
    return TTFont(buf)
