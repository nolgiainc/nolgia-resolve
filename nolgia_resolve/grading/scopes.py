# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 NOLGIA Inc
"""The finished picture, read as a colourist's scopes would, in plain Python.

After the grade, each clip's frame is exported with every node on: CST OUT has
made it a Rec.709 gamma 2.4 picture, what the viewer shows on a display. It is
read against NOLGIA's colour standard's table for the final picture:

  * shadows that hold detail sit about 5 to 10% above black;
  * highlights stop under about 90%: the top tenth is for the sun, lamps,
    windows and speculars, a small share of the frame (2%);
  * clipping: none unless meant, at most 0.1% of a channel's pixels at its very
    bottom or top;
  * a neutral object is level across red, green and blue. The parade, each
    channel's bottom and top, is reported only: the check cannot find a
    neutral object in a frame, and on a whole frame warm light that was meant,
    a coloured thing or a look's own tint parts the channels;
  * skin highlights about 50 to 75% (one official source);
  * saturation, reported only (the table has no row for it).

The maths and the bounds are those of scopecheck.py, the tool the standard names
for this check (numpy and OpenCV, run outside Resolve), ported to the standard
library. A number outside the table is a flag for a person to look at, never a
target to grade toward. Nothing here changes a picture.

What each row rests on:
  * the waveform, the top tenth, the parade and saturation: the grid the grade
    measures on (measure.GRID, 160 x 90 points a frame), all frames pooled;
  * clipping: every pixel of every frame, counted on the still's bytes. On the
    grid one point is 0.007% of the frame, so 0.1% is 14 points, and a thin
    clipped edge or a small lamp can fall between them;
  * skin: the points the grade's own measurement took as skin (found by colour
    after CST IN, with the white's cast taken out; the plugin has no face
    finder), read at the same places in the finished picture. Sand, wood or a
    tan wall can pass for skin.
"""

import bisect
import math

from . import measure as M

# The standard's table, as scopecheck.py holds it.
SHADOW_LO, SHADOW_HI = 5.0, 10.0      # shadows that hold detail sit about 5 to 10% above black
SHADOW_FLAG_UNDER = 2.0               # flagged under 3% (scopecheck: "crushed, unless meant")
SHADOW_FLAG_OVER = 5.0                # flagged over 15% (scopecheck: "lifted or flat, unless meant")
HIGHLIGHT_MAX = 90.0                  # highlights stop under about 90%
SPECULAR_SHARE_MAX = 2.0              # the top tenth is for the sun, lamps, windows, speculars: a small share
SKIN_LO, SKIN_HI = 50.0, 75.0         # skin highlights about 50 to 75% (one official source)
CLIP_SHARE_MAX = 0.1                  # percent of pixels at the very end of a channel
SKIN_LINE_DEG = 123.0                 # the vectorscope's skin line, degrees in the Cb/Cr plane (Cr up, Cb right)
SKIN_MIN_POINTS = 30

WAVEFORM = (("p0.1", 0.001), ("p1", 0.01), ("p5", 0.05), ("p50", 0.5), ("p95", 0.95), ("p99", 0.99),
            ("p99.9", 0.999))
ROWS = ("shadows", "highlights", "clipping", "parade", "skin", "saturation")
REPORTED = ("parade", "saturation")   # shown, never flagged
CHANNELS = (("r", "red"), ("g", "green"), ("b", "blue"))

# What each row reads and its target, said once on the answer (the clips carry only numbers).
TABLE = {
    "shadows": "value: where the darkest 5% of the frame sit, in % of the range (the waveform's p5). Shadows that "
               "hold detail sit about 5 to 10% above black; flagged under 3% or over 15%.",
    "highlights": "value: the % of the frame over 90% of the range; reach: where the brightest 1% start (p99). "
                  "Highlights stop under about 90%; the top tenth is for the sun, lamps, windows and speculars, "
                  "about 2% of the frame at most.",
    "clipping": "value: the most of any channel's pixels at its very bottom or top (at or under 1/255 of the range, "
                "at or over 254/255), in %, every pixel of the frames read; channels: [at black, at white] per "
                "channel. None unless meant: at most 0.1%.",
    "parade": "tops and bottoms: each channel's 99.5th and 0.5th percentile (red, green, blue), in % of the range; "
              "tops_apart and bottoms_apart: the highest channel less the lowest, in points. A neutral object is "
              "level across red, green and blue. Reported only: the check cannot find a neutral object in a frame, "
              "and on a whole frame warm light that was meant, a coloured thing or a look's own tint parts the "
              "channels. To read it: tops apart while the bottoms stay level is light (warm sun, practicals) or a "
              "coloured thing in the frame; bottoms apart too is colour reaching into the blacks.",
    "skin": "value: skin highlights, the 90th percentile of the skin's level, in % of the range: about 50 to 75% "
            "(one official source). Skin is the points the grade's own measurement took as skin, by colour (the "
            "plugin has no face finder), read in the finished picture; sand, wood or a tan wall can pass for skin. "
            "hue_off_skin_line_deg: the median distance from the vectorscope's skin line, a guide, not a rule.",
    "saturation": "mean and p95 of (max - min) / max per point, in %. The table has no row for it: reported only.",
    "flags": "A number outside the table is a flag for a person to look at, never a target to grade toward: a night "
             "scene, a bright window, a backlit face or a look can sit outside it on purpose. The check reads the "
             "finished picture only; it changes nothing in the grade.",
}

FLAG_NOTE = ("These are flags for you to look at on the scopes, not targets: a night scene, a bright window, a backlit "
             "face or a look can sit outside the table on purpose. The check only read the finished picture; it "
             "changed nothing in the grade.")
SAY_CLIPS = 10  # sentences on the answer; every clip outside keeps its own


# ------------------------------------------------------------------ reading a still

def read_still(path):
    """One exported still: the grid (display code values 0..1), and per channel how many of all its pixels sit at
    the very bottom and the very top of the range."""
    with open(path, "rb") as handle:
        data = handle.read()
    layout = M.tiff_layout(data)
    return {"grid": M.grid_of(data, layout), "ends": channel_ends(data, layout),
            "pixels": layout["width"] * layout["height"]}


def channel_ends(data, layout):
    """{channel: (pixels at the very bottom, pixels at the very top)}, over every pixel of the still.

    The ends are scopecheck's: at or under 1/255 of the range and at or over 254/255 (8-bit code values 0 and 1,
    254 and 255). A 16-bit still is counted on each value's high byte, which is 0 at or under 255 of 65535 and 255 at
    or over 65280: 2 code values inside scopecheck's edges (257 and 65278). The counting is done by bytes.count, so a
    4K frame takes a few hundredths of a second."""
    spp, step = layout["spp"], layout["bits"] // 8
    if spp < 3 or step not in (1, 2):
        raise ValueError("not an 8 or 16-bit RGB still")
    start = layout["start"]
    size = layout["width"] * layout["height"] * spp * step
    if start + size > len(data):
        raise ValueError("the still is cut short")
    if step == 2:
        high = data[start + (1 if layout["order"] == "<" else 0):start + size:2]
        lows, highs = (0,), (255,)
    else:
        high = data[start:start + size]
        lows, highs = (0, 1), (254, 255)
    out = {}
    for k, (c, _) in enumerate(CHANNELS):
        values = high[k::spp]
        out[c] = (sum(values.count(v) for v in lows), sum(values.count(v) for v in highs))
    return out


# ------------------------------------------------------------------ the numbers

def _luma(p):
    return 0.2126 * p[0] + 0.7152 * p[1] + 0.0722 * p[2]


def _pc(sorted_values, q):
    return round(M._percentile(sorted_values, q) * 100.0, 1)


def numbers(stills):
    """The scopes of one clip, all its frames pooled (as scopecheck pools a video's frames)."""
    points = [p for s in stills for p in s["grid"]]
    if not points:
        raise ValueError("no points")
    n = float(len(points))
    lum = sorted(_luma(p) for p in points)
    out = {"frames": len(stills), "points": len(points),
           "waveform": dict((k, _pc(lum, q)) for k, q in WAVEFORM),
           "over_90_share": round((len(lum) - bisect.bisect_right(lum, HIGHLIGHT_MAX / 100.0)) * 100.0 / n, 2)}
    parade = {}
    for k, (c, _) in enumerate(CHANNELS):
        values = sorted(p[k] for p in points)
        parade[c] = {"bottom": _pc(values, 0.005), "top": _pc(values, 0.995)}
    out["parade"] = parade
    pixels = sum(s["pixels"] for s in stills)
    out["pixels"] = pixels
    out["clip"] = dict((c, {"at_black": round(sum(s["ends"][c][0] for s in stills) * 100.0 / pixels, 3),
                            "at_white": round(sum(s["ends"][c][1] for s in stills) * 100.0 / pixels, 3)})
                       for c, _ in CHANNELS)
    sats = sorted(((max(p) - min(p)) / max(p)) if max(p) > 1e-6 else 0.0 for p in points)
    out["saturation"] = {"mean": round(sum(sats) * 100.0 / n, 1), "p95": _pc(sats, 0.95)}
    return out


def skin_numbers(values, share=None):
    """Skin's level and hue in the finished picture, from the display values of the points taken as skin."""
    lum = sorted(_luma(p) for p in values)
    offsets = []
    for r, g, b in values:
        # The vectorscope's plane as scopecheck reads it (OpenCV's YCrCb, BT.601 weights); grey points have no hue.
        y = 0.299 * r + 0.587 * g + 0.114 * b
        cr, cb = (r - y) * 0.713, (b - y) * 0.564
        if math.hypot(cr, cb) * 255.0 < 8.0:
            continue
        angle = math.degrees(math.atan2(cr, cb)) % 360.0
        offsets.append(((angle - SKIN_LINE_DEG + 180.0) % 360.0) - 180.0)
    out = {"points": len(values), "highlights_p90": _pc(lum, 0.9), "median": _pc(lum, 0.5),
           "hue_off_skin_line_deg": round(M._median(offsets), 1) if len(offsets) >= SKIN_MIN_POINTS else None}
    if share is not None:
        out["share"] = round(share, 2)
    return out


# ------------------------------------------------------------------ the table's rows

def rows(m, skin=None, skin_why=None):
    """{row: {value..., status, by?}}: status is inside, outside (by how much, in the row's unit), reported (shown,
    never flagged: the parade and saturation) or not_read."""
    out = {}
    p5 = m["waveform"]["p5"]
    if p5 < SHADOW_LO - SHADOW_FLAG_UNDER:
        out["shadows"] = {"value": p5, "status": "outside", "side": "under", "by": round(SHADOW_LO - p5, 1)}
    elif p5 > SHADOW_HI + SHADOW_FLAG_OVER:
        out["shadows"] = {"value": p5, "status": "outside", "side": "over", "by": round(p5 - SHADOW_HI, 1)}
    else:
        out["shadows"] = {"value": p5, "status": "inside"}
    share = m["over_90_share"]
    out["highlights"] = {"value": share, "reach": m["waveform"]["p99"], "status": "inside"}
    if share > SPECULAR_SHARE_MAX:
        out["highlights"].update(status="outside", by=round(share - SPECULAR_SHARE_MAX, 2))
    clip = m["clip"]
    worst = max(max(clip[c]["at_black"], clip[c]["at_white"]) for c, _ in CHANNELS)
    out["clipping"] = {"value": worst, "status": "inside", "pixels": m["pixels"],
                       "channels": dict((c, [clip[c]["at_black"], clip[c]["at_white"]]) for c, _ in CHANNELS)}
    if worst > CLIP_SHARE_MAX:
        out["clipping"].update(status="outside", by=round(worst - CLIP_SHARE_MAX, 3))
    tops = [m["parade"][c]["top"] for c, _ in CHANNELS]
    bottoms = [m["parade"][c]["bottom"] for c, _ in CHANNELS]
    out["parade"] = {"tops": tops, "bottoms": bottoms, "tops_apart": round(max(tops) - min(tops), 1),
                     "bottoms_apart": round(max(bottoms) - min(bottoms), 1), "status": "reported"}
    if skin and skin["points"] >= SKIN_MIN_POINTS:
        level = skin["highlights_p90"]
        row = {"value": level, "status": "inside"}
        row.update((k, v) for k, v in skin.items() if k != "highlights_p90")
        if level < SKIN_LO:
            row.update(status="outside", side="under", by=round(SKIN_LO - level, 1))
        elif level > SKIN_HI:
            row.update(status="outside", side="over", by=round(level - SKIN_HI, 1))
        out["skin"] = row
    else:
        out["skin"] = {"status": "not_read",
                       "why": skin_why or "too few points were taken as skin to read their level"}
    out["saturation"] = dict(m["saturation"], status="reported")
    return out


def check(stills, skin=None, skin_why=None):
    """One clip's finished picture against the table: its stills (read_still), and the finished picture's values at
    the points the grade measured as skin."""
    m = numbers(stills)
    s = None
    if skin:
        s = skin_numbers(skin, share=len(skin) * 100.0 / m["points"])
    r = rows(m, s, skin_why)
    return {"read": True, "frames": m["frames"], "points": m["points"], "waveform": m["waveform"], "rows": r,
            "outside": [name for name in ROWS if name not in REPORTED and r[name]["status"] == "outside"]}


# ------------------------------------------------------------------ the words

def _n(value, places=2):
    """97.0 -> 97, 1.80 -> 1.8, 0.125 -> 0.13."""
    text = "%.*f" % (places, value)
    return text.rstrip("0").rstrip(".") if "." in text else text


def sentence(clip, result):
    """One plain sentence for a clip whose finished picture sits outside the table, or None. It says where the
    picture sits and what the table says; it never says the grade is wrong."""
    if not result.get("read") or not result.get("outside"):
        return None
    r = result["rows"]
    parts = []
    sh = r["shadows"]
    if sh["status"] == "outside" and sh["side"] == "under":
        parts.append("the darkest 5%% sit at %s%%, under the 5 to 10%% where shadows hold detail" % _n(sh["value"], 1))
    elif sh["status"] == "outside":
        parts.append("the darkest 5%% sit at %s%%, over the 10%% where shadows that hold detail sit (a lifted or flat "
                     "picture, unless meant)" % _n(sh["value"], 1))
    hl = r["highlights"]
    if hl["status"] == "outside":
        parts.append("highlights reach %s%%, the table stops near 90 (%s%% of the frame is over 90%%, where the sun, "
                     "lamps, windows and speculars take about 2%%)" % (_n(hl["reach"], 0), _n(hl["value"])))
    cl = r["clipping"]
    if cl["status"] == "outside":
        ends = sorted(((share, c, end) for c, name in CHANNELS
                       for share, end in zip(cl["channels"][c], ("black", "white")) if share > CLIP_SHARE_MAX),
                      reverse=True)
        names = dict(CHANNELS)
        said = ["%s%% of the %s channel is clipped at %s" % (_n(ends[0][0]), names[ends[0][1]], ends[0][2])]
        said += ["%s%% of the %s at %s" % (_n(s), names[c], end) for s, c, end in ends[1:]]
        parts.append(said[0] if len(said) == 1 else ", ".join(said[:-1]) + " and " + said[-1])
    sk = r["skin"]
    if sk["status"] == "outside":
        parts.append("skin highlights sit at %s%%, %s the 50 to 75%% of one official source (on %d points the grade "
                     "measured as skin, by colour: check that they are skin)"
                     % (_n(sk["value"], 1), sk["side"], sk["points"]))
    return "Clip %d: %s." % (clip, "; ".join(parts))


def _clip_list(numbers_):
    if len(numbers_) == 1:
        return "clip %d" % numbers_[0]
    return "clips %s and %d" % (", ".join(str(n) for n in numbers_[:-1]), numbers_[-1])


def summary(per, order):
    """The answer's finished_picture: which clips sit outside the table, which could not be read, a text for the
    person and the table's wording."""
    outside = [n for n in order if per.get(n, {}).get("read") and per[n].get("outside")]
    unread = [n for n in order if n in per and not per[n].get("read")]
    read = [n for n in order if per.get(n, {}).get("read")]
    text = []
    if outside:
        text += [per[n]["say"] for n in outside[:SAY_CLIPS]]
        if len(outside) > SAY_CLIPS:
            rest = outside[SAY_CLIPS:]
            text.append("%s also %s outside the table; each clip's finished_picture says how."
                        % (_clip_list(rest).capitalize(), "sits" if len(rest) == 1 else "sit"))
        text.append(FLAG_NOTE)
    elif read:
        text.append("The finished picture of %s sits inside the scopes' table (shadows, highlights, clipping, and "
                    "skin where the grade found it)."
                    % ("the clip" if len(read) == 1 else "every clip read"))
    if unread:
        whys = sorted(set(per[n].get("why") or "" for n in unread))
        if not read:
            text.append("The finished picture was not read (%s). The grade itself is done." % "; ".join(whys))
        else:
            text.append("The finished picture of %s could not be read (%s), so %s not checked. The grade itself is "
                        "done." % (_clip_list(unread), "; ".join(whys), "it was" if len(unread) == 1 else "they were"))
    return {"outside": outside, "unread": unread, "say": " ".join(text), "table": TABLE}
