"""Búsqueda del tamaño mínimo por idioma/escritura y construcción final."""
from __future__ import annotations

import json
import os
import time

from .core import detect_zones, extract_outlines, pixelize
from .evaluate import collisions, ocr_score, pieces_ratio, topo_failures
from .prep import load_source
from .render import font_bytes, shape


def shaped_glyphs(src, lines):
    data = font_bytes(src)
    order = src.getGlyphOrder()
    used = set()
    for line in lines:
        infos, _, _ = shape(data, line)
        used |= {order[i.codepoint] for i in infos}
    return used


def find_min_size(cfg, outdir, log=print, ocr_ratio=0.9, pieces_min=0.95):
    t0 = time.time()
    lines = cfg["lines"]
    src = load_source(cfg["font"], cfg["script"], cfg.get("font_number", 0), "".join(lines))
    outlines = extract_outlines(src)
    cmap_names = sorted(set(src.getBestCmap().values()))
    zones = detect_zones(src, outlines, cmap_names)
    search_set = set(cmap_names) | shaped_glyphs(src, lines) | {".notdef"}
    # solo se usan para OCR las líneas que el OCR lee bien con la fuente original
    sdata = font_bytes(src)
    per_line = {ln: ocr_score(sdata, [ln], cfg["ocr"], 48, False) for ln in lines}
    ocr_lines = [ln for ln, v in per_line.items() if v >= 0.75] or lines
    dropped = [ln for ln in lines if ln not in ocr_lines]
    if dropped:
        log(f"[{cfg['id']}] líneas que el OCR no lee ni en el original (se excluyen): {dropped}")
    base = sum(per_line[ln] for ln in ocr_lines) / len(ocr_lines)
    log(f"[{cfg['id']}] glifos={len(src.getGlyphOrder())} búsqueda={len(search_set)} "
        f"zonas={[round(z) for z in zones]} OCR original={base:.2f}")
    results = []
    best = None
    for size in cfg["sizes"]:
        r = pixelize(src, outlines, size, zones=zones, only=search_set)
        data = font_bytes(r.font)
        os.makedirs(os.path.join(outdir, "pruebas"), exist_ok=True)
        with open(os.path.join(outdir, "pruebas", f"{cfg['id']}-{size}px.ttf"), "wb") as fh:
            fh.write(data)
        col = collisions(r.bitmaps, cmap_names)
        topo = topo_failures(r.bitmaps, cmap_names)
        score = ocr_score(data, ocr_lines, cfg["ocr"], size, True)
        pr, got, ref = pieces_ratio(data, sdata, lines, size)
        ok = not col and score >= ocr_ratio * base and pieces_min <= pr <= 2 - pieces_min
        res = dict(size=size, collisions=len(col), collision_examples=col[:6],
                   topo_fail=len(topo), topo_total=len(cmap_names), ocr=round(score, 3),
                   ocr_base=round(base, 3), pieces=f"{got}/{ref}", ok=ok, threshold=round(r.threshold, 3))
        results.append(res)
        log(f"[{cfg['id']}] {size}px colisiones={len(col)} topología={len(topo)}/{len(cmap_names)} "
            f"OCR={score:.2f} piezas={got}/{ref} {'OK' if ok else 'falla'} ({time.time() - t0:.0f}s)")
        # mínimo = primer tamaño que pasa y cuyo siguiente también pasa
        if ok and len(results) >= 2 and results[-2]["ok"]:
            best = results[-2]["size"]
            break
    final = {}
    if best is not None:
        r = pixelize(src, outlines, best, zones=zones, family=cfg.get("family"))
        path = os.path.join(outdir, f"{cfg['id']}-{best}px.ttf")
        r.font.save(path)
        final[best] = path
    summary = dict(id=cfg["id"], name=cfg["name"], font=os.path.basename(cfg["font"]),
                   min_size=best, results=results, files=final, zones=zones,
                   seconds=round(time.time() - t0))
    with open(os.path.join(outdir, f"{cfg['id']}.json"), "w") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=1, default=str)
    return summary
