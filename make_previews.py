"""Genera imágenes comparativas a partir de salida/*.json."""
import glob
import json
import os
import sys

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, "src")
from pixelfont.prep import load_source  # noqa: E402
from pixelfont.render import font_bytes, render_line, upscale  # noqa: E402
from run_tests import CONFIGS  # noqa: E402

LABEL = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 18)
TITLE = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 24)
ZOOM_EM = 36  # tamaño aproximado del em al ampliar


def label(text, font=LABEL, color=(90, 90, 90)):
    w = int(font.getlength(text)) + 8
    img = Image.new("RGB", (w, font.size + 10), "white")
    ImageDraw.Draw(img).text((4, 2), text, fill=color, font=font)
    return img


def stack(parts, gap=6):
    W = max(p.width for p in parts) + 40
    H = sum(p.height + gap for p in parts) + 30
    out = Image.new("RGB", (W, H), "white")
    y = 15
    for p in parts:
        out.paste(p.convert("RGB"), (20, y))
        y += p.height + gap
    return out


def main():
    cfgs = {c["id"]: c for c in CONFIGS}
    os.makedirs("salida/previews", exist_ok=True)
    overview = [label("Tamaño mínimo encontrado por idioma (ampliado, sin suavizado)", TITLE, (0, 0, 0))]
    for path in sorted(glob.glob("salida/*.json")):
        d = json.load(open(path))
        cfg = cfgs[d["id"]]
        best = d["min_size"]
        src = font_bytes(load_source(cfg["font"], cfg["script"], cfg.get("font_number", 0), "".join(cfg["lines"])))
        parts = [label(f"{d['name']} — mínimo: {best}px" if best else f"{d['name']} — sin mínimo", TITLE, (0, 0, 0)),
                 label("Original (vectorial, suavizado):")]
        parts += [render_line(src, ln, ZOOM_EM, False) for ln in cfg["lines"]]
        if best:
            data = open(d["files"][str(best)], "rb").read()
            f = max(1, round(ZOOM_EM / best))
            parts.append(label(f"Pixel {best}px (ampliado ×{f}):"))
            parts += [upscale(render_line(data, ln, best, True), f) for ln in cfg["lines"]]
            parts.append(label(f"Pixel {best}px a tamaño real:"))
            parts += [render_line(data, ln, best, True) for ln in cfg["lines"][:1]]
            prev = [r for r in d["results"] if r["size"] < best]
            if prev:
                r = prev[-1]
                pdata = open(f"salida/pruebas/{d['id']}-{r['size']}px.ttf", "rb").read()
                f2 = max(1, round(ZOOM_EM / r["size"]))
                why = []
                if r["collisions"]:
                    why.append(f"{r['collisions']} pares de glifos idénticos")
                if r["ocr"] < 0.9 * r["ocr_base"]:
                    why.append(f"OCR {r['ocr']:.2f} vs {r['ocr_base']:.2f}")
                parts.append(label(f"Pixel {r['size']}px — no pasa ({', '.join(why) or 'el siguiente tamaño falla'}):",
                                   color=(170, 60, 40)))
                parts.append(upscale(render_line(pdata, cfg["lines"][0], r["size"], True), f2))
            overview.append(label(f"{d['name']} · {best}px", color=(40, 40, 40)))
            overview.append(upscale(render_line(data, cfg["lines"][0], best, True), 3))
        stack(parts).save(f"salida/previews/{d['id']}.png")
    stack(overview).save("salida/previews/resumen.png")


if __name__ == "__main__":
    main()
