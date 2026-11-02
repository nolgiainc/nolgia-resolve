# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 NOLGIA Inc
"""Read a frame Resolve exported and measure it, in plain Python.

ExportCurrentFrameAsStill writes a .tif as uncompressed 16-bit RGB in one strip
(Resolve Studio 21.1.1). The frame is read on a coarse grid in DaVinci
Intermediate (the grade's working space, after CST IN) and measured in scene
light:
  * white: the median red, green and blue of the least saturated 30 percent of
    the pixels between the 40th and 97th brightness percentile, which is what
    should be white or grey (shirts, walls, paper, sky);
  * key: the geometric mean brightness between the 20th and 95th percentile.
"""

import math
import struct

from . import colour as C

GRID = (160, 90)


def read_tiff_grid(path, cols=GRID[0], rows=GRID[1]):
    """(r, g, b) code values 0..1 on a cols x rows grid of an uncompressed 8 or 16-bit RGB TIFF."""
    with open(path, "rb") as handle:
        data = handle.read()
    order = "<" if data[:2] == b"II" else ">"
    (ifd,) = struct.unpack(order + "I", data[4:8])
    (count,) = struct.unpack(order + "H", data[ifd:ifd + 2])
    tags = {}
    for i in range(count):
        entry = data[ifd + 2 + i * 12: ifd + 14 + i * 12]
        tag, kind, n = struct.unpack(order + "HHI", entry[:8])
        if kind == 3 and n <= 2:
            value = struct.unpack(order + "H", entry[8:10])[0]
        elif kind == 3:
            (offset,) = struct.unpack(order + "I", entry[8:12])
            value = struct.unpack(order + "H", data[offset:offset + 2])[0]
        else:
            (value,) = struct.unpack(order + "I", entry[8:12])
        tags[tag] = value
    width, height = tags[256], tags[257]
    bits = tags.get(258, 8)
    if tags.get(259, 1) != 1:
        raise ValueError("compressed TIFF")
    spp = tags.get(277, 3)
    start = tags[273]
    step = bits // 8
    scale = float((1 << bits) - 1)
    fmt = order + ("H" if step == 2 else "B")
    stride = width * spp * step
    grid = []
    for j in range(rows):
        y = int((j + 0.5) * height / rows)
        row = start + y * stride
        for i in range(cols):
            p = row + int((i + 0.5) * width / cols) * spp * step
            grid.append(tuple(struct.unpack_from(fmt, data, p + k * step)[0] / scale for k in range(3)))
    return grid


def _percentile(sorted_values, q):
    if not sorted_values:
        return 0.0
    k = (len(sorted_values) - 1) * q
    lo = int(math.floor(k))
    hi = min(lo + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (k - lo)


def _median(values):
    return _percentile(sorted(values), 0.5)


def measure(grid_di):
    """Stats of a frame in DaVinci Intermediate: white (linear r, g, b), key (linear), clip share."""
    wr, wg, wb = C.npm(*C.GAMUTS["dwg"])[1]
    pixels = []
    clipped = 0
    for r, g, b in grid_di:
        if max(r, g, b) >= 0.995:
            clipped += 1
            continue
        lin = (C.di_decode(r), C.di_decode(g), C.di_decode(b))
        y = wr * lin[0] + wg * lin[1] + wb * lin[2]
        if y <= 1e-5:
            continue
        pixels.append((y, lin))
    if len(pixels) < 50:
        return {"ok": False, "why": "the frame is almost all black or clipped"}
    lum = sorted(p[0] for p in pixels)
    lo, hi = _percentile(lum, 0.40), _percentile(lum, 0.97)
    band = [lin for y, lin in pixels if lo <= y <= hi and min(lin) > 0]
    band.sort(key=lambda lin: (max(lin) - min(lin)) / max(lin))
    whites = band[:max(10, int(len(band) * 0.3))]
    white = [_median([lin[k] for lin in whites]) for k in range(3)]
    k_lo, k_hi = _percentile(lum, 0.20), _percentile(lum, 0.95)
    mids = [y for y in lum if k_lo <= y <= k_hi]
    key = math.exp(sum(math.log(y) for y in mids) / len(mids))
    white_sat = _median([(max(l) - min(l)) / max(l) for l in whites])
    out = {"ok": True, "white": white, "white_y": wr * white[0] + wg * white[1] + wb * white[2], "key": key,
           "white_saturation": white_sat, "clipped": clipped / float(len(grid_di)), "samples": len(pixels)}
    # Find skin on the picture with its white cast taken out, so a cast does not change which pixels count.
    cast = cast_stops(white)
    # ... and its level taken to a fixed white, so exposure does not either.
    lift = 0.5 / max(out["white_y"], 1e-6)
    skin = skin_pixels(grid_di, [2.0 ** c * lift for c in cast])
    out["skin_share"] = len(skin) / float(len(grid_di))
    if len(skin) >= max(30, len(grid_di) * 0.01):
        rgb = [_median([p[k] for p in skin]) for k in range(3)]
        out["skin"] = rgb
        out["skin_y"] = wr * rgb[0] + wg * rgb[1] + wb * rgb[2]
    return out


def skin_pixels(grid_di, gains=(1.0, 1.0, 1.0)):
    """Linear RGB of the pixels whose hue sits near the skin line (in DI), lit (not in deep shadow).
    gains balance the picture first (the white's cast taken out); the pixels come back as they were."""
    from .dctl import dwg_luma_weights, skin_angle
    wr, wg, wb = dwg_luma_weights()
    angle = skin_angle()
    out = []
    for r0, g0, b0 in grid_di:
        r, g, b = (C.di_encode(C.di_decode(v) * k) for v, k in zip((r0, g0, b0), gains))
        y = wr * r + wg * g + wb * b
        if not 0.38 <= y <= 0.68:  # white at 0.5 linear: DI 0.57; skin sits from about 3 stops under it to near it
            continue
        u, v = b - y, r - y
        c = math.hypot(u, v)
        if not 0.012 <= c <= 0.2:
            continue
        d = (math.atan2(v, u) - angle + math.pi) % (2 * math.pi) - math.pi
        if abs(d) <= 0.38:
            out.append((C.di_decode(r0), C.di_decode(g0), C.di_decode(b0)))
    return out


def combine(stats_list):
    """Average stats of several frames of one clip (in log)."""
    good = [s for s in stats_list if s.get("ok")]
    if not good:
        return stats_list[0] if stats_list else {"ok": False, "why": "no frame"}
    geo = lambda xs: math.exp(sum(math.log(max(x, 1e-9)) for x in xs) / len(xs))
    out = {"ok": True, "frames": len(good)}
    out["white"] = [geo([s["white"][k] for s in good]) for k in range(3)]
    out["white_y"] = geo([s["white_y"] for s in good])
    out["key"] = geo([s["key"] for s in good])
    out["white_saturation"] = sum(s["white_saturation"] for s in good) / len(good)
    out["clipped"] = max(s["clipped"] for s in good)
    skins = [s for s in good if "skin" in s]
    if skins and len(skins) * 2 >= len(good):
        out["skin"] = [geo([s["skin"][k] for s in skins]) for k in range(3)]
        out["skin_y"] = geo([s["skin_y"] for s in skins])
    out["skin_share"] = sum(s.get("skin_share", 0) for s in good) / len(good)
    return out


def cast_stops(white):
    """Per channel stops that take this white to neutral (zero mean)."""
    logs = [math.log2(max(v, 1e-9)) for v in white]
    mean = sum(logs) / 3.0
    return [mean - v for v in logs]
