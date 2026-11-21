"""
pixelfont.core — convierte una fuente vectorial en una fuente pixel art
conservando su "motor" (cmap, GSUB, GPOS, GDEF): solo se reemplazan los
contornos de los glifos por cuadritos alineados a una rejilla de N px por em,
y todas las posiciones (avances, kerning, anclajes de marcas, uniones
cursivas) se ajustan a la misma rejilla.

Pipeline por tamaño N:
  1. Zonas verticales: se detectan alturas clave de la escritura (línea base,
     altura de x, mayúsculas, línea superior, descendentes...) y se aplica una
     deformación vertical por tramos para que caigan en píxeles enteros.
  2. Rasterizado supermuestreado (SSxSS subpíxeles por píxel) con regla de
     devanado no nulo -> mapa de cobertura por píxel.
  3. Umbral global por fuente, elegido para conservar la "tinta" total
     (así una fuente gruesa sigue gruesa y una fina sigue fina).
  4. Reparación topológica contra la versión en alta resolución: piezas
     perdidas (puntos, diacríticos), piezas fusionadas, huecos tapados,
     píxeles huérfanos.
  5. Reconstrucción de la fuente: upem = N*64, cada píxel = 64 unidades,
     tablas de posicionamiento redondeadas a la rejilla.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from fontTools.pens.basePen import BasePen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont, newTable
from fontTools.ttLib.scaleUpem import scale_upem
from scipy import ndimage
from skimage.draw import polygon as sk_polygon
from skimage.morphology import skeletonize
import heapq

PX = 64  # unidades de fuente por píxel en la fuente de salida

DROP_TABLES = ["fpgm", "prep", "cvt ", "gasp", "hdmx", "LTSH", "VDMX", "kern",
               "DSIG", "MATH", "VORG", "vhea", "vmtx", "BASE", "JSTF", "CFF ",
               "CFF2", "gvar", "fvar", "avar", "HVAR", "MVAR", "STAT", "cvar"]


# ---------------------------------------------------------------- contornos
class FlattenPen(BasePen):
    """Convierte contornos (cuadráticos o cúbicos) en polígonos."""

    def __init__(self, glyphSet, steps=8):
        super().__init__(glyphSet)
        self.contours, self.cur, self.steps = [], [], steps

    def _moveTo(self, p):
        self.cur = [p]

    def _lineTo(self, p):
        self.cur.append(p)

    def _curveToOne(self, p1, p2, p3):
        p0 = self.cur[-1]
        for i in range(1, self.steps + 1):
            t = i / self.steps
            mt = 1 - t
            self.cur.append((mt**3 * p0[0] + 3 * mt * mt * t * p1[0] + 3 * mt * t * t * p2[0] + t**3 * p3[0],
                             mt**3 * p0[1] + 3 * mt * mt * t * p1[1] + 3 * mt * t * t * p2[1] + t**3 * p3[1]))

    def _qCurveToOne(self, p1, p2):
        p0 = self.cur[-1]
        for i in range(1, self.steps + 1):
            t = i / self.steps
            mt = 1 - t
            self.cur.append((mt * mt * p0[0] + 2 * mt * t * p1[0] + t * t * p2[0],
                             mt * mt * p0[1] + 2 * mt * t * p1[1] + t * t * p2[1]))

    def _closePath(self):
        if len(self.cur) > 2:
            self.contours.append(np.array(self.cur, dtype=float))
        self.cur = []

    _endPath = _closePath


def extract_outlines(font: TTFont) -> dict[str, list[np.ndarray]]:
    gs = font.getGlyphSet()
    out = {}
    for name in font.getGlyphOrder():
        pen = FlattenPen(gs)
        gs[name].draw(pen)
        out[name] = pen.contours
    return out


# ---------------------------------------------------------------- zonas
def detect_zones(font: TTFont, outlines, glyph_names) -> list[float]:
    """Alturas clave (unidades originales) que deben caer en píxel entero."""
    zones = {0.0}
    os2 = font["OS/2"]
    for attr in ("sxHeight", "sCapHeight"):
        v = getattr(os2, attr, 0) or 0
        if v > 0:
            zones.add(float(v))
    tops, bots = [], []
    for n in glyph_names:
        cs = outlines.get(n)
        if not cs:
            continue
        pts = np.concatenate(cs)
        tops.append(pts[:, 1].max())
        bots.append(pts[:, 1].min())
    upem = font["head"].unitsPerEm
    q = upem / 100.0  # cuantización 1% del em para encontrar modas
    for arr in (tops, bots):
        if not arr:
            continue
        vals = np.round(np.array(arr) / q) * q
        uniq, cnt = np.unique(vals, return_counts=True)
        order = np.argsort(-cnt)
        for i in order[:2]:  # las 2 alturas más frecuentes
            if cnt[i] >= max(5, 0.08 * len(arr)):
                zones.add(float(uniq[i]))
    # quitar zonas casi duplicadas
    zs = sorted(zones)
    merged = []
    for z in zs:
        if merged and abs(z - merged[-1]) < upem * 0.03:
            if z == 0.0:
                merged[-1] = 0.0
            continue
        merged.append(z)
    return merged


class VWarp:
    """Deformación vertical por tramos: zonas -> múltiplos de píxel."""

    def __init__(self, zones, s):
        self.s = s
        src = sorted(zones)
        dst = [round(z * s) / s for z in src]
        # evitar tramos colapsados
        for i in range(1, len(dst)):
            if dst[i] <= dst[i - 1]:
                dst[i] = dst[i - 1] + 1 / s
        self.src, self.dst = np.array(src), np.array(dst)

    def __call__(self, y):
        y = np.asarray(y, dtype=float)
        src, dst = self.src, self.dst
        out = np.interp(y, src, dst)
        lo, hi = y < src[0], y > src[-1]
        out = np.where(lo, dst[0] + (y - src[0]), out)
        out = np.where(hi, dst[-1] + (y - src[-1]), out)
        return out


# ---------------------------------------------------------------- raster
@dataclass
class GlyphBitmap:
    bits: np.ndarray | None  # bool [h, w], fila 0 = arriba
    x0: int = 0              # columna del píxel izquierdo
    ytop: int = 0            # fila superior en píxeles sobre la línea base
    hires_hash: int = 0
    topo_ok: bool = True
    issues: list = field(default_factory=list)
    cov: np.ndarray | None = None

    def key(self):
        return (self.x0, self.ytop, self.bits.shape, np.packbits(self.bits).tobytes())


def same_design(a: "GlyphBitmap", b: "GlyphBitmap", tol=0.8):
    """True si los originales ya son prácticamente iguales a este tamaño
    (p. ej. coma y comilla baja, guion y guion no separable)."""
    if a.cov is None or b.cov is None:
        return False
    x0 = min(a.x0, b.x0)
    yt = max(a.ytop, b.ytop)
    w = max(a.x0 + a.cov.shape[1], b.x0 + b.cov.shape[1]) - x0
    h = yt - min(a.ytop - a.cov.shape[0], b.ytop - b.cov.shape[0])
    def place(g):
        c = np.zeros((h, w), np.float32)
        r, k = yt - g.ytop, g.x0 - x0
        c[r:r + g.cov.shape[0], k:k + g.cov.shape[1]] = g.cov
        return c
    return float(np.abs(place(a) - place(b)).sum()) < tol


def disambiguate(bitmaps, names, max_rounds=6):
    """Si dos glifos distintos quedan idénticos, cambia en uno de ellos el
    píxel donde más se diferencian los originales (p. ej. el centro de una
    breve frente a una diéresis)."""
    names = [n for n in names if bitmaps[n].bits is not None and bitmaps[n].bits.any()]
    for _ in range(max_rounds):
        index = {}
        fixed_any = False
        for n in names:
            g = bitmaps[n]
            k = g.key()
            if k not in index:
                index[k] = n
                continue
            r = bitmaps[index[k]]
            if g.hires_hash == r.hires_hash or same_design(g, r):
                continue
            done = False
            for target, other in ((g, r), (r, g)):
                D = target.cov - other.cov  # misma caja (misma clave)
                # encender donde el original tiene más tinta, apagar donde menos
                score = np.where(target.bits, -D, D)
                for flat in np.argsort(-score, axis=None)[:15]:
                    idx = np.unravel_index(flat, D.shape)
                    if score[idx] <= 0.05:
                        break
                    before = pixel_topology(target.bits)
                    target.bits[idx] = not target.bits[idx]
                    if pixel_topology(target.bits)[0] <= before[0] + 1 and target.key() not in index:
                        done = True
                        break
                    target.bits[idx] = not target.bits[idx]
                if done:
                    if target is r:  # el glifo indexado cambió de clave; g ocupa la vieja
                        index[r.key()] = index[k]
                        index[k] = n
                    fixed_any = True
                    break
            index.setdefault(g.key(), n)
        if not fixed_any:
            break


def rasterize(contours, s, warp, SS):
    """Devuelve (mask_hi, x0, ytop, w, h) o None si vacío."""
    if not contours:
        return None
    cs = [np.column_stack([c[:, 0], warp(c[:, 1])]) for c in contours]
    pts = np.concatenate(cs)
    xmin, xmax = pts[:, 0].min() * s, pts[:, 0].max() * s
    ymin, ymax = pts[:, 1].min() * s, pts[:, 1].max() * s
    x0, x1 = math.floor(xmin + 1e-6), math.ceil(xmax - 1e-6)
    y0, y1 = math.floor(ymin + 1e-6), math.ceil(ymax - 1e-6)
    w, h = max(1, x1 - x0), max(1, y1 - y0)
    H, W = h * SS, w * SS
    wind = np.zeros((H, W), dtype=np.int16)
    for c in cs:
        X = (c[:, 0] * s - x0) * SS - 0.5
        Y = (y1 - c[:, 1] * s) * SS - 0.5
        area = 0.5 * np.sum(X * np.roll(Y, -1) - np.roll(X, -1) * Y)
        if abs(area) < 1e-9:
            continue
        rr, cc = sk_polygon(Y, X, shape=(H, W))
        wind[rr, cc] += 1 if area > 0 else -1
    mask = wind != 0
    if not mask.any():
        return None
    return mask, x0, y1, w, h


def coverage(mask, SS):
    H, W = mask.shape
    return mask.reshape(H // SS, SS, W // SS, SS).mean(axis=(1, 3))


EIGHT = np.ones((3, 3), dtype=bool)
FOUR = ndimage.generate_binary_structure(2, 1)


def _holes(b, struct=FOUR):
    pad = np.pad(~b, 1, constant_values=True)
    lab, n = ndimage.label(pad, structure=struct)
    outside = lab[0, 0]
    lab = lab[1:-1, 1:-1]
    ids = [i for i in range(1, n + 1) if i != outside]
    return lab, ids


def topo_ref(mask, SS):
    """Componentes y huecos significativos en alta resolución."""
    min_area = 0.15 * SS * SS
    lab, n = ndimage.label(mask, structure=EIGHT)
    comps = []
    if n:
        areas = ndimage.sum(np.ones_like(mask), lab, index=np.arange(1, n + 1))
        comps = [i + 1 for i, a in enumerate(areas) if a >= min_area]
    hlab, hids = _holes(mask)
    holes = [i for i in hids if (hlab == i).sum() >= 0.35 * SS * SS]
    return lab, comps, hlab, holes


def skeleton_pixels(mask, SS):
    """Píxeles por donde pasa la línea central del trazo: garantizan que los
    trazos finos (arcos de n, rayas, curvas) no desaparezcan."""
    sk = skeletonize(mask)
    H, W = mask.shape
    cnt = sk.reshape(H // SS, SS, W // SS, SS).sum(axis=(1, 3))
    return cnt >= SS * 0.4


def _is_simple(b, y, x):
    """True si quitar (y,x) no cambia la conectividad local (8-vecinos)."""
    win = np.pad(b, 1)[y:y + 3, x:x + 3].copy()
    win[1, 1] = False
    if win.sum() <= 1:
        return False  # extremo de trazo: no se quita
    _, n = ndimage.label(win, structure=EIGHT)
    return n == 1


def thin_added(b, added, cov):
    """Quita esquinas en L sobrantes de los píxeles añadidos (líneas pixel-perfect)."""
    ys, xs = np.nonzero(added)
    order = np.argsort(cov[ys, xs])
    for i in order:
        y, x = ys[i], xs[i]
        if b[y, x] and _is_simple(b, y, x):
            # solo si es una esquina: vecino horizontal y vertical presentes
            hn = (x > 0 and b[y, x - 1]) or (x + 1 < b.shape[1] and b[y, x + 1])
            vn = (y > 0 and b[y - 1, x]) or (y + 1 < b.shape[0] and b[y + 1, x])
            if hn and vn:
                b[y, x] = False
    return b


def _connect(b, ck):
    """Une piezas separadas de un mismo trazo por el camino de mayor cobertura."""
    for _ in range(6):
        lab, n = ndimage.label(b & (ck > 0.0), structure=EIGHT)
        if n <= 1:
            return b
        h, w = b.shape
        src = lab == 1
        dist = np.full((h, w), np.inf)
        prev = {}
        pq = []
        for y, x in zip(*np.nonzero(src)):
            dist[y, x] = 0
            heapq.heappush(pq, (0.0, y, x))
        target = None
        while pq:
            d, y, x = heapq.heappop(pq)
            if d > dist[y, x]:
                continue
            if lab[y, x] > 1:
                target = (y, x)
                break
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    ny, nx = y + dy, x + dx
                    if (dy or dx) and 0 <= ny < h and 0 <= nx < w and ck[ny, nx] > 0:
                        nd = d + (0 if lab[ny, nx] else 1.05 - ck[ny, nx])
                        if nd < dist[ny, nx]:
                            dist[ny, nx] = nd
                            prev[(ny, nx)] = (y, x)
                            heapq.heappush(pq, (nd, ny, nx))
        if target is None:
            return b
        p = target
        while p in prev:
            b[p] = True
            p = prev[p]
    return b


def smooth_bumps(b, cov):
    """Mete hacia dentro los píxeles que sobresalen de un trazo recto
    (patrón #.  /  .#  /  #. -> columna recta), sin cambiar la topología."""
    h, w = b.shape
    base_topo = pixel_topology(b)
    for _ in range(2):
        changed = False
        for y, x in zip(*np.nonzero(b)):
            if not b[y, x]:
                continue
            for dy, dx in ((0, -1), (0, 1), (-1, 0), (1, 0)):
                qy, qx = y + dy, x + dx
                if not (0 <= qy < h and 0 <= qx < w) or b[qy, qx]:
                    continue
                py, px_ = dx, dy  # perpendicular
                a1 = (qy + py, qx + px_)
                a2 = (qy - py, qx - px_)
                if not all(0 <= a[0] < h and 0 <= a[1] < w and b[a] for a in (a1, a2)):
                    continue
                # el píxel no debe tener otros vecinos fuera del trazo
                c1 = (y + py, x + px_)
                c2 = (y - py, x - px_)
                if any(0 <= c[0] < h and 0 <= c[1] < w and b[c] for c in (c1, c2)):
                    continue
                if cov[qy, qx] < 0.15:
                    continue
                b[y, x], b[qy, qx] = False, True
                if pixel_topology(b) != base_topo:
                    b[y, x], b[qy, qx] = True, False
                    continue
                changed = True
                break
        if not changed:
            break
    return b


def relayout_small(b, cov, mask, SS, threshold, max_small=3.0):
    """Si piezas pequeñas (puntos, diacríticos) quedaron pegadas entre sí o al
    cuerpo, las vuelve a colocar con al menos 1 px de separación, moviéndolas
    lo menos posible. Devuelve (bits, dx, dy) con el desplazamiento del lienzo."""
    lab, comps, _, _ = topo_ref(mask, SS)
    if len(comps) < 2 or pixel_topology(b)[0] >= len(comps):
        return b, 0, 0
    areas = {k: (lab == k).sum() / (SS * SS) for k in comps}
    small = [k for k in comps if areas[k] <= max_small]
    big = [k for k in comps if areas[k] > max_small]
    if not small:
        return b, 0, 0
    P = 3
    h, w = b.shape
    canvas = np.zeros((h + 2 * P, w + 2 * P), bool)
    ccov = {k: _cov_of(lab, k, SS) for k in comps}
    # cuerpo: píxeles actuales que pertenecen a piezas grandes
    if big:
        stack = np.stack([ccov[k] for k in comps])
        owner = np.array(comps)[np.argmax(stack, axis=0)]
        body = b & np.isin(owner, big)
    else:
        body = np.zeros_like(b)
    canvas[P:P + h, P:P + w] = body
    centre_y = (np.nonzero(body)[0].mean() if body.any() else h / 2)
    for k in sorted(small, key=lambda k: -areas[k]):
        piece = ccov[k] >= threshold
        if not piece.any():
            piece[np.unravel_index(np.argmax(ccov[k]), piece.shape)] = True
        ys, xs = np.nonzero(piece)
        up = ys.mean() < centre_y  # la pieza está por encima del cuerpo
        best = None
        for dist in range(0, P + 1):
            cands = []
            for dy in range(-dist, dist + 1):
                for dx in range(-dist, dist + 1):
                    if max(abs(dy), abs(dx)) != dist:
                        continue
                    # preferir alejarse del cuerpo en vertical
                    pref = (dy > 0) if up else (dy < 0)
                    cands.append((pref, abs(dx), dy, dx))
            for _, _, dy, dx in sorted(cands):
                Y, X = ys + P + dy, xs + P + dx
                if Y.min() < 0 or X.min() < 0 or Y.max() >= canvas.shape[0] or X.max() >= canvas.shape[1]:
                    continue
                near = ndimage.binary_dilation(canvas, structure=EIGHT)
                if not near[Y, X].any():
                    best = (Y, X)
                    break
            if best:
                break
        if best is None:
            best = (ys + P, xs + P)
        canvas[best] = True
    rows, cols = np.nonzero(canvas)
    r0, r1, c0, c1 = rows.min(), rows.max() + 1, cols.min(), cols.max() + 1
    return canvas[r0:r1, c0:c1], c0 - P, P - r0


def _cov_of(labels, k, SS):
    return coverage(labels == k, SS)


def pixel_topology(b):
    _, nc = ndimage.label(b, structure=EIGHT)
    _, hids = _holes(b)
    return nc, len(hids)


def repair(b, cov, mask, SS):
    """Corrige la topología del bitmap b contra la máscara en alta resolución."""
    lab, comps, hlab, holes = topo_ref(mask, SS)
    issues = []
    comp_cov = {k: _cov_of(lab, k, SS) for k in comps}
    hole_cov = {k: _cov_of(hlab, k, SS) for k in holes}

    # 1. piezas perdidas (puntos, diacríticos, trazos finos)
    for k, ck in comp_cov.items():
        if not (b & (ck > 0.02)).any():
            idx = np.unravel_index(np.argmax(ck), ck.shape)
            b[idx] = True

    # 1b. trazos partidos: una pieza real no debe quedar en varios pedazos
    for k, ck in comp_cov.items():
        b = _connect(b, ck)

    # 2. huecos tapados (contraformas)
    blab, bh = _holes(b)
    for k, hk in hole_cov.items():
        if any(((blab == i) & (hk > 0.25)).any() for i in bh):
            continue
        cand = np.argsort(-hk, axis=None)[:3]
        fixed = False
        for flat in cand:
            idx = np.unravel_index(flat, hk.shape)
            if hk[idx] < 0.2 or not b[idx]:
                continue
            b[idx] = False
            blab2, bh2 = _holes(b)
            if any(blab2[idx] == i for i in bh2):
                fixed = True
                break
            b[idx] = True
        if not fixed:
            issues.append("hueco")

    # 3. piezas fusionadas: separar dueños distintos que se tocan
    if len(comps) > 1:
        stack = np.stack([comp_cov[k] for k in comps])
        owner = np.where(b, np.argmax(stack, axis=0), -1)
        for _ in range(4):
            nc, _ = pixel_topology(b)
            if nc >= len(comps):
                break
            changed = False
            ys, xs = np.nonzero(b)
            for y, x in zip(ys, xs):
                a = owner[y, x]
                y0, y1, x0, x1 = max(0, y - 1), y + 2, max(0, x - 1), x + 2
                nb = owner[y0:y1, x0:x1]
                if ((nb >= 0) & (nb != a)).any():
                    own_cov = stack[a, y, x]
                    # elimina el píxel puente si es el más débil y su pieza sobrevive
                    if own_cov < 0.55 and (owner == a).sum() > 1:
                        others = nb[(nb >= 0) & (nb != a)]
                        if all(stack[o].max() > 0 for o in others):
                            b[y, x] = False
                            owner[y, x] = -1
                            changed = True
            if not changed:
                break

    b = smooth_bumps(b, cov)

    # 4. huérfanos: píxeles que no corresponden a ninguna pieza real
    plab, pn = ndimage.label(b, structure=EIGHT)
    for i in range(1, pn + 1):
        m = plab == i
        if cov[m].sum() < 0.25 and m.sum() <= 2:
            b[m] = False

    nc, nh = pixel_topology(b)
    if nc != len(comps):
        issues.append(f"piezas {nc}/{len(comps)}")
    if nh != len(holes):
        issues.append(f"huecos {nh}/{len(holes)}")
    return b, issues


def choose_threshold(samples):
    """Umbral que conserva la tinta total (estilo/peso de la fuente)."""
    if not samples:
        return 0.5
    lo, hi = 0.3, 0.62
    total = sum(c.sum() for c in samples)
    for _ in range(18):
        mid = (lo + hi) / 2
        ink = sum((c >= mid).sum() for c in samples)
        if ink > total:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


# ---------------------------------------------------------------- GPOS snap
def _walk(obj, fn, seen):
    if obj is None or id(obj) in seen or isinstance(obj, (int, float, str, bytes)):
        return
    seen.add(id(obj))
    if isinstance(obj, (list, tuple)):
        for x in obj:
            _walk(x, fn, seen)
        return
    if isinstance(obj, dict):
        for x in obj.values():
            _walk(x, fn, seen)
        return
    if not hasattr(obj, "__dict__"):
        return
    fn(obj)
    for v in list(vars(obj).values()):
        _walk(v, fn, seen)


def snap_layout(font, ywarp_units):
    """Redondea a la rejilla avances/colocaciones de GPOS y anclajes."""
    def rnd(v):
        return int(round(v / PX)) * PX

    def fn(o):
        cls = type(o).__name__
        for a in ("ValueFormat", "ValueFormat1", "ValueFormat2"):
            if isinstance(getattr(o, a, None), int):
                setattr(o, a, getattr(o, a) & 0x0F)
        if cls == "ValueRecord":
            for a in ("XPlacement", "YPlacement", "XAdvance", "YAdvance"):
                if hasattr(o, a):
                    setattr(o, a, rnd(getattr(o, a)))
            for a in ("XPlaDevice", "YPlaDevice", "XAdvDevice", "YAdvDevice"):
                if hasattr(o, a):
                    delattr(o, a)
        elif cls == "Anchor":
            o.XCoordinate = rnd(o.XCoordinate)
            o.YCoordinate = rnd(float(ywarp_units(o.YCoordinate)))
            o.Format = 1
            for a in ("AnchorPoint", "XDeviceTable", "YDeviceTable"):
                if hasattr(o, a):
                    delattr(o, a)
        elif cls == "CaretValue":
            if hasattr(o, "Coordinate"):
                o.Coordinate = rnd(o.Coordinate)
            o.Format = 1
            for a in ("CaretValuePoint", "DeviceTable"):
                if hasattr(o, a):
                    delattr(o, a)

    for tag in ("GPOS", "GDEF"):
        if tag in font:
            _walk(font[tag].table, fn, set())


def _cells(gb):
    if gb is None or gb.bits is None or not gb.bits.any():
        return None
    ys, xs = np.nonzero(gb.bits)
    return xs + gb.x0, gb.ytop - 1 - ys  # columna, fila (fila 0 = sobre línea base)


def separate_marks(font, bitmaps, important=frozenset(), max_steps=3):
    """Revisa cada par base+marca de GPOS (MarkBase, MarkLig, MarkMark). Si la
    marca toca la base en píxeles, aleja el anclaje de la base de a 1 px. Como
    un anclaje lo comparten todas las marcas de su clase, se elige el
    desplazamiento que deja separadas las marcas que importan (los puntos que
    forman letras, en `important`) moviendo lo menos posible."""
    if "GPOS" not in font:
        return 0
    moved = 0
    cell_cache = {}

    def cells(n):
        if n not in cell_cache:
            cell_cache[n] = _cells(bitmaps.get(n))
        return cell_cache[n]

    def touching(occ, anchor, marks, shift):
        out = []
        for m, manchor in marks:
            mc = cells(m)
            if mc is None:
                continue
            ox = (anchor.XCoordinate - manchor.XCoordinate) // PX
            oy = (anchor.YCoordinate - manchor.YCoordinate) // PX + shift
            if any((x, y) in occ for x, y in zip(mc[0] + ox, mc[1] + oy)):
                out.append((m, (mc[1] + oy).mean()))
        return out

    def fix(base, anchor, marks):
        nonlocal moved
        bc = cells(base)
        if bc is None or anchor is None or not marks:
            return
        occ = {(x + dx, y + dy) for x, y in zip(*bc) for dx in (-1, 0, 1) for dy in (-1, 0, 1)}
        t0 = touching(occ, anchor, marks, 0)
        if not t0:
            return
        t0.sort(key=lambda t: t[0] not in important)
        direction = 1 if t0[0][1] >= bc[1].mean() else -1
        def cost(k):
            t = touching(occ, anchor, marks, k * direction)
            return sum(10 if m in important else 1 for m, _ in t) + 0.6 * k
        best = min(range(max_steps + 1), key=cost)
        if best:
            anchor.YCoordinate += best * direction * PX
            moved += 1

    for lk in font["GPOS"].table.LookupList.Lookup:
        for st in lk.SubTable:
            st = getattr(st, "ExtSubTable", st)
            t = getattr(st, "LookupType", lk.LookupType)
            if t not in (4, 5, 6):
                continue
            mcov = st.MarkCoverage.glyphs if t != 6 else st.Mark1Coverage.glyphs
            marr = st.MarkArray.MarkRecord if t != 6 else st.Mark1Array.MarkRecord
            by_class = {}
            for g, rec in zip(mcov, marr):
                by_class.setdefault(rec.Class, []).append((g, rec.MarkAnchor))
            if t == 4:
                for g, rec in zip(st.BaseCoverage.glyphs, st.BaseArray.BaseRecord):
                    for c, a in enumerate(rec.BaseAnchor):
                        fix(g, a, by_class.get(c, []))
            elif t == 6:
                for g, rec in zip(st.Mark2Coverage.glyphs, st.Mark2Array.Mark2Record):
                    for c, a in enumerate(rec.Mark2Anchor):
                        fix(g, a, by_class.get(c, []))
            else:
                for g, att in zip(st.LigatureCoverage.glyphs, st.LigatureArray.LigatureAttach):
                    for comp in att.ComponentRecord:
                        for c, a in enumerate(comp.LigatureAnchor):
                            fix(g, a, by_class.get(c, []))
    return moved


# ---------------------------------------------------------------- conversión
@dataclass
class PixelResult:
    size: int
    font: TTFont
    bitmaps: dict
    threshold: float
    zones_px: list


def bitmap_to_glyph(gb: GlyphBitmap):
    pen = TTGlyphPen(None)
    if gb.bits is not None and gb.bits.any():
        h, w = gb.bits.shape
        # runs horizontales fusionados verticalmente
        prev = {}
        rects = []
        for r in range(h):
            row = gb.bits[r]
            runs, c = [], 0
            while c < w:
                if row[c]:
                    s0 = c
                    while c < w and row[c]:
                        c += 1
                    runs.append((s0, c))
                else:
                    c += 1
            cur = {}
            for run in runs:
                if run in prev:
                    cur[run] = prev.pop(run)
                else:
                    cur[run] = r
            for run, r0 in prev.items():
                rects.append((run[0], run[1], r0, r))
            prev = cur
        for run, r0 in prev.items():
            rects.append((run[0], run[1], r0, h))
        for c0, c1, r0, r1 in rects:
            xa, xb = (gb.x0 + c0) * PX, (gb.x0 + c1) * PX
            ya, yb = (gb.ytop - r1) * PX, (gb.ytop - r0) * PX
            pen.moveTo((xa, ya))
            pen.lineTo((xa, yb))
            pen.lineTo((xb, yb))
            pen.lineTo((xb, ya))
            pen.closePath()
    return pen.glyph()


def pixelize(src: TTFont, outlines, size: int, zones=None, glyph_names=None,
             threshold=None, SS=None, only=None, family=None):
    upem = src["head"].unitsPerEm
    s = size / upem
    SS = SS or max(6, min(16, 256 // size))
    order = src.getGlyphOrder()
    if zones is None:
        zones = detect_zones(src, outlines, glyph_names or order)
    warp = VWarp(zones, s)

    rasters = {}
    for n in order:
        if only is not None and n not in only:
            rasters[n] = None
            continue
        rasters[n] = rasterize(outlines.get(n), s, warp, SS)

    if threshold is None:
        samp = [coverage(r[0], SS) for n, r in list(rasters.items())[:: max(1, len(rasters) // 400)] if r]
        threshold = choose_threshold(samp)

    bitmaps = {}
    for n, r in rasters.items():
        if r is None:
            bitmaps[n] = GlyphBitmap(None)
            continue
        mask, x0, ytop, w, h = r
        cov = coverage(mask, SS)
        b = cov >= threshold
        sk = skeleton_pixels(mask, SS) & ~b
        b |= sk
        b = thin_added(b, sk, cov)
        b, issues = repair(b, cov, mask, SS)
        if any(i.startswith("piezas") for i in issues):
            nb, dx, dy = relayout_small(b, cov, mask, SS, threshold)
            if nb is not b:
                b, x0, ytop = nb, x0 + dx, ytop + dy
                cov = np.zeros(b.shape, np.float32)  # la caja cambió
                lab_, comps_, _, _ = topo_ref(mask, SS)
                issues = [i for i in issues if not i.startswith("piezas")]
                if pixel_topology(b)[0] != len(comps_):
                    issues.append(f"piezas {pixel_topology(b)[0]}/{len(comps_)}")
        hh = hash((x0, ytop, mask.shape, np.packbits(mask).tobytes()))
        bitmaps[n] = GlyphBitmap(b, x0, ytop, hh, not issues, issues, cov.astype(np.float32))

    disambiguate(bitmaps, [n for n in order if bitmaps[n].bits is not None])

    if not hasattr(src, "_letter_marks"):
        src._letter_marks = letter_marks(src)
    bounds = {}
    for n in order:
        cs = outlines.get(n)
        if cs:
            pts = np.concatenate(cs)
            bounds[n] = (pts[:, 0].min(), pts[:, 0].max())
    font = build_font(src, bitmaps, size, warp, family, src._letter_marks, bounds)
    return PixelResult(size, font, bitmaps, threshold, [float(z * s) for z in warp.dst])


def letter_marks(src: TTFont):
    """Marcas que aparecen al dar forma a cada carácter solo y en contexto
    (p. ej. los puntos que forman las letras árabes)."""
    from .render import font_bytes, shape
    data = font_bytes(src)
    order = src.getGlyphOrder()
    gdef = src["GDEF"].table if "GDEF" in src else None
    classes = gdef.GlyphClassDef.classDefs if gdef and gdef.GlyphClassDef else {}
    out = set()
    for cp in src.getBestCmap():
        c = chr(cp)
        for t in (c, "\u0640" + c, c + "\u0640", "\u0640" + c + "\u0640"):
            infos, _, _ = shape(data, t)
            for i in infos:
                n = order[i.codepoint]
                if classes.get(n) == 3:
                    out.add(n)
    return frozenset(out)


def build_font(src: TTFont, bitmaps, size, warp, family=None, important=frozenset(), bounds=None):
    import copy, io
    buf = io.BytesIO()
    src.save(buf)
    buf.seek(0)
    f = TTFont(buf)
    order = f.getGlyphOrder()
    old_upem = f["head"].unitsPerEm
    new_upem = size * PX

    # reemplazar contornos por glifos vacíos antes de escalar (más rápido)
    for t in DROP_TABLES:
        if t in f:
            del f[t]
    glyf = newTable("glyf")
    glyf.glyphOrder = order
    from fontTools.ttLib.tables._g_l_y_f import Glyph
    glyf.glyphs = {n: Glyph() for n in order}
    f["glyf"] = glyf
    f["loca"] = newTable("loca")
    f.sfntVersion = "\x00\x01\x00\x00"
    maxp = f["maxp"]
    maxp.tableVersion = 0x00010000
    for a in ("maxZones", "maxTwilightPoints", "maxStorage", "maxFunctionDefs",
              "maxInstructionDefs", "maxStackElements", "maxSizeOfInstructions",
              "maxComponentElements", "maxComponentDepth", "maxPoints",
              "maxContours", "maxCompositePoints", "maxCompositeContours"):
        if not hasattr(maxp, a):
            setattr(maxp, a, 1 if a == "maxZones" else 0)
    f["head"].glyphDataFormat = 0
    if f["post"].formatType not in (2.0, 3.0):
        f["post"].formatType = 3.0
    if f["post"].formatType == 2.0 and not hasattr(f["post"], "extraNames"):
        f["post"].formatType = 3.0

    scale_upem(f, new_upem)
    k = new_upem / old_upem

    def ywarp_units(y):
        return warp(y / k) * k

    for n in order:
        glyf.glyphs[n] = bitmap_to_glyph(bitmaps[n])
    hmtx = f["hmtx"].metrics
    s_px = size / old_upem
    for n in order:
        adv, _ = hmtx[n]
        g = glyf.glyphs[n]
        g.recalcBounds(glyf)
        lsb = getattr(g, "xMin", 0) if getattr(g, "numberOfContours", 0) else 0
        adv_px = int(round(adv / PX))
        gb = bitmaps[n]
        # espacio entre letras: si el original deja aire a la derecha, la
        # versión pixel deja al menos 1 px (no aplica a lados que se unen,
        # como en árabe, donde el original no deja aire)
        if bounds and n in bounds and adv_px > 0 and gb.bits is not None and gb.bits.any():
            rsb_orig = (adv / k - bounds[n][1]) * s_px
            right = gb.x0 + gb.bits.shape[1]
            if rsb_orig > 0.05 and adv_px - right < 1:
                adv_px = right + 1
        hmtx[n] = (adv_px * PX, lsb)

    snap_layout(f, ywarp_units)
    f.marks_moved = separate_marks(f, bitmaps, important)

    def up(v):
        return int(math.ceil(v / PX)) * PX

    hh, os2 = f["hhea"], f["OS/2"]
    hh.ascent, hh.descent, hh.lineGap = up(hh.ascent), -up(-hh.descent), up(hh.lineGap)
    os2.sTypoAscender = up(os2.sTypoAscender)
    os2.sTypoDescender = -up(-os2.sTypoDescender)
    os2.sTypoLineGap = up(os2.sTypoLineGap)
    os2.usWinAscent, os2.usWinDescent = up(os2.usWinAscent), up(os2.usWinDescent)
    for a in ("sxHeight", "sCapHeight"):
        if hasattr(os2, a) and getattr(os2, a):
            setattr(os2, a, int(round(float(ywarp_units(getattr(os2, a))) / PX)) * PX)
    # nombre: por defecto "<familia> Pixel N"; si la licencia reserva el
    # nombre original (OFL con Reserved Font Name) se pasa family= nuevo.
    name = f["name"]
    if family:
        name.names = [r for r in name.names if r.nameID not in (3, 7, 16, 17, 21, 22)]
        name.setName(f"{family} {size}", 1, 3, 1, 0x409)
        name.setName("Regular", 2, 3, 1, 0x409)
        name.setName(f"{family} {size};pixelfont", 3, 3, 1, 0x409)
        name.setName(f"{family} {size}", 4, 3, 1, 0x409)
        name.setName(f"{family.replace(' ', '')}-{size}", 6, 3, 1, 0x409)
        for r in list(name.names):
            if r.nameID in (1, 4, 6) and not (r.platformID == 3 and r.langID == 0x409):
                name.names.remove(r)
    else:
        for rec in name.names:
            if rec.nameID in (1, 4, 6, 16):
                try:
                    base = rec.toUnicode()
                except Exception:
                    continue
                suffix = f" Pixel {size}" if rec.nameID != 6 else f"-Pixel{size}"
                rec.string = (base + suffix)[:63]
    return f
