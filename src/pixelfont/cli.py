"""Uso:
  python -m pixelfont convert fuente.ttf --script latin --size 11 -o salida.ttf
  python -m pixelfont search  fuente.ttf --script tamil --ocr tam --text "வணக்கம்" --sizes 7-18
Escrituras: latin, japanese, tamil, arabic, tibetan (ver prep.SCRIPTS)."""
import argparse
import os

from .core import detect_zones, extract_outlines, pixelize
from .prep import load_source
from .search import find_min_size


def main():
    ap = argparse.ArgumentParser(prog="pixelfont")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("convert")
    c.add_argument("font")
    c.add_argument("--script", required=True)
    c.add_argument("--size", type=int, required=True)
    c.add_argument("--index", type=int, default=0, help="fuente dentro de un .ttc")
    c.add_argument("-o", "--output", required=True)
    c.add_argument("--family", help="nombre nuevo (obligatorio si la licencia reserva el original)")
    s = sub.add_parser("search")
    s.add_argument("font")
    s.add_argument("--script", required=True)
    s.add_argument("--ocr", required=True, help="idioma de Tesseract (ron, jpn, tam, urd, dzo...)")
    s.add_argument("--text", action="append", required=True, help="línea de prueba (repetible)")
    s.add_argument("--sizes", default="6-24")
    s.add_argument("--index", type=int, default=0)
    s.add_argument("--out", default="salida")
    s.add_argument("--family")
    a = ap.parse_args()
    if a.cmd == "convert":
        src = load_source(a.font, a.script, a.index)
        ol = extract_outlines(src)
        zones = detect_zones(src, ol, sorted(set(src.getBestCmap().values())))
        r = pixelize(src, ol, a.size, zones=zones, family=a.family)
        r.font.save(a.output)
        bad = sum(1 for b in r.bitmaps.values() if b.issues)
        print(f"{a.output}: {len(r.bitmaps)} glifos, umbral {r.threshold:.2f}, {bad} con avisos de topología")
    else:
        lo, hi = (int(x) for x in a.sizes.split("-"))
        os.makedirs(a.out, exist_ok=True)
        cfg = dict(id=os.path.splitext(os.path.basename(a.font))[0], name=a.font, font=a.font,
                   script=a.script, ocr=a.ocr, lines=a.text, sizes=list(range(lo, hi + 1)),
                   font_number=a.index, family=a.family)
        r = find_min_size(cfg, a.out)
        print("tamaño mínimo:", r["min_size"])


if __name__ == "__main__":
    main()
