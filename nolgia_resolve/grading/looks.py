# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 NOLGIA Inc
"""The NOLGIA film look library: our own looks, written as code, baked to .cube LUTs.

Every look is a function from DaVinci Wide Gamut / Intermediate to DaVinci Wide
Gamut / Intermediate (a look in the working space, the way colourists build
them), placed on the LOOK node before CST OUT. Its strength is the LOOK node's
Key Output Gain, which mixes the look with the picture under it in the working
space, so 0.6 is "the look at 60%".

Film looks run a small film model built from how film stock behaves, not from
any maker's data or LUTs:
  * a negative: per-layer characteristic curves (density against log exposure,
    with a toe, a straight line of slope gamma and a shoulder), layer
    sensitivities that overlap (crosstalk), and interlayer effects;
  * a print (for print looks): printer lights timed so mid grey prints neutral,
    a steep S-shaped print curve with a dense black (Dmax), and dye crosstalk
    (each dye also absorbs a little of the other colours), which is where film's
    denser, richer saturated colours come from;
  * the print projected (10^-density), then taken back through the exact inverse
    of CST OUT, so LOOK then CST OUT shows the print as modelled.
Negative looks stop at the negative and scan it back to scene light with a
straight-line inversion, so CST OUT renders it; what is left is the stock's
own curve shapes, crossovers and colour.
Creative looks are operations in the working space (saturation, split tones,
hue placement, fade).
"""

import math

from . import colour as C

LOOK_GREY = 0.11  # display linear of mid grey, the same as CST OUT

# ---------------------------------------------------------------- small helpers

LUMA = C.npm(*C.GAMUTS["rec709"])[1]
DWG_LUMA = C.npm(*C.GAMUTS["dwg"])[1]


def softplus(x):
    if x > 30:
        return x
    if x < -30:
        return math.exp(x)
    return math.log1p(math.exp(x))


def sigmoid(x):
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


def smoothstep(a, b, x):
    t = max(0.0, min(1.0, (x - a) / (b - a)))
    return t * t * (3 - 2 * t)


def mix(a, b, t):
    return [x + (y - x) * t for x, y in zip(a, b)]


def neg_curve(le, gamma, toe=-1.9, toe_k=2.6, shoulder=2.7, shoulder_k=1.6):
    """Negative density above base for log10 exposure le (0 = mid grey)."""
    return gamma * (softplus(toe_k * (le - toe)) / toe_k - softplus(shoulder_k * (le - shoulder)) / shoulder_k)


def print_curve(p, dmin, dmax, k):
    """Print density for log10 print exposure p (0 = the curve's centre)."""
    return dmin + (dmax - dmin) * sigmoid(k * p)


def print_curve_inverse(d, dmin, dmax, k):
    s = (d - dmin) / (dmax - dmin)
    s = min(max(s, 1e-9), 1 - 1e-9)
    return math.log(s / (1 - s)) / k


def sensitivity(cross):
    """Overlapping layer sensitivities: each layer sees cross of its neighbours."""
    a = cross
    return [[1 - 2 * a, a * 1.3, a * 0.7], [a, 1 - 2 * a, a], [a * 0.5, a * 1.5, 1 - 2 * a]]


# ---------------------------------------------------------------- the film model

class Film(object):
    """A film model with its parameters. mode: negative (scanned), print, reversal."""

    def __init__(self, mode="print", neg_gamma=(0.64, 0.62, 0.60), cross=0.03, interlayer=0.30,
                 toe=-2.2, shoulder=2.7, print_k=(2.8, 2.8, 2.8), dmin=(0.07, 0.07, 0.08), dmax=(3.75, 3.8, 3.65),
                 dye=None, grey_shift=(0.0, 0.0, 0.0), grey=LOOK_GREY, mono=None, silver=0.0, exposure=0.0,
                 scan_gamma=None, black_comp=1.0):
        self.mode = mode
        self.neg_gamma = neg_gamma
        self.S = sensitivity(cross)
        self.interlayer = interlayer
        self.toe, self.shoulder = toe, shoulder
        self.print_k, self.dmin, self.dmax = print_k, dmin, dmax
        self.dye = [list(r) for r in (dye or PRINT_DYE)]
        self.grey_shift = grey_shift  # display density added to grey per channel: the timing's colour
        self.grey = grey
        self.mono = mono  # spectral weights (r, g, b) for black and white
        self.silver = silver  # bleach bypass: retained silver, as a share of the mean dye density
        self.exposure = exposure  # stops, before the film
        self.scan_gamma = scan_gamma or sum(neg_gamma) / 3.0
        self.black_comp = black_comp  # 1: the print's black is the display's black (projector black adaptation)
        self._calibrate()

    def exposures(self, lin):
        k = 2.0 ** self.exposure
        if self.mono is not None:
            e = sum(w * c for w, c in zip(self.mono, lin)) * k
            return [e, e, e]
        return [max(1e-7, v * k) for v in C.mat_vec(self.S, lin)]

    def negative(self, lin):
        """Negative densities above base for one scene linear Rec.709 colour."""
        le = [math.log10(max(v, 1e-7) / 0.18) for v in self.exposures(lin)]
        d = [neg_curve(le[i], self.neg_gamma[i], self.toe, 2.6, self.shoulder, 1.6) for i in range(3)]
        if self.interlayer:
            m = sum(d) / 3.0
            d = [v + self.interlayer * (v - m) for v in d]
        return d

    def reversal_le(self, lin):
        le = [math.log10(max(v, 1e-7) / 0.18) * self.neg_gamma[i] / 0.6 for i, v in enumerate(self.exposures(lin))]
        if self.interlayer:
            m = sum(le) / 3.0
            le = [v + self.interlayer * (v - m) for v in le]
        return le

    def _dens(self, dn, lights):
        dye = [print_curve(-dn[i] + lights[i], self.dmin[i], self.dmax[i], self.print_k[i]) for i in range(3)]
        dens = C.mat_vec(self.dye, dye)
        if self.silver:
            m = sum(dye) / 3.0
            dens = [v + self.silver * m for v in dens]
        return dens

    def _display(self, dn, lights):
        dens = self._dens(dn, lights)
        t = [10.0 ** -(dens[i] - self.white[i]) for i in range(3)]
        if self.black_comp:
            t = [(t[i] - self.black[i] * self.black_comp) / (1.0 - self.black[i] * self.black_comp) for i in range(3)]
        return t

    def _calibrate(self):
        """Printer lights that put scene mid grey at the look's grey, neutral (as a lab times a print)."""
        self.dn_grey = self.negative([0.18, 0.18, 0.18])
        if self.mode == "negative":
            return
        self.white = C.mat_vec(self.dye, list(self.dmin))
        dmax_dens = C.mat_vec(self.dye, list(self.dmax))
        if self.silver:
            m = sum(self.dmax) / 3.0
            dmax_dens = [v + self.silver * m for v in dmax_dens]
        self.black = [10.0 ** -(dmax_dens[i] - self.white[i]) for i in range(3)]
        base = [0.0, 0.0, 0.0] if self.mode == "reversal" else self.dn_grey
        target = [math.log10(self.grey) - sh for sh in self.grey_shift]
        lights = [0.0, 0.0, 0.0]
        for _ in range(300):
            disp = self._display(base, lights)
            err = [math.log10(max(disp[i], 1e-9)) - target[i] for i in range(3)]
            if max(abs(e) for e in err) < 1e-8:
                break
            slopes = []
            for i in range(3):
                l2 = list(lights); l2[i] += 1e-4
                d2 = self._display(base, l2)[i]
                slopes.append((math.log10(max(d2, 1e-9)) - math.log10(max(disp[i], 1e-9))) / 1e-4)
            # more printer light, denser print, darker display: slopes are negative
            lights = [lights[i] - max(-0.3, min(0.3, err[i] / min(slopes[i], -0.05) * 0.8)) for i in range(3)]
        self.lights = lights

    def render(self, lin):
        """Scene linear Rec.709 -> display linear Rec.709 (print, reversal) or scene linear (negative)."""
        if self.mode == "negative":
            dn = self.negative(lin)
            le = [(dn[i] - self.dn_grey[i]) / self.scan_gamma for i in range(3)]
            return [0.18 * 10.0 ** v for v in le], False
        dn = self.reversal_le(lin) if self.mode == "reversal" else self.negative(lin)
        return [max(0.0, v) for v in self._display(dn, self.lights)], True


def film_lmt(film):
    """A Film as a look: DWG/DI -> DWG/DI."""
    def f(rgb_di):
        lin = C.mat_vec(C.DWG_TO_709, [C.di_decode(c) for c in rgb_di])
        lin = [max(v, 0.0) for v in C.gamut_compress(lin)]
        out, display = film.render(lin)
        if display:
            code = [min(C.DISPLAY_MAX, max(0.0, v)) ** (1.0 / 2.4) for v in out]
            return C.display_to_dwg(code)
        return [C.di_encode(v) for v in C.mat_vec(C.REC709_TO_DWG, out)]
    return f


# ---------------------------------------------------------------- creative operations on DI

def op_sat(rgb, s, lo=0.0):
    y = sum(w * c for w, c in zip(DWG_LUMA, rgb))
    return [y + (c - y) * s for c in rgb]


def op_split(rgb, shadow, highlight, pivot=0.36, width=0.18):
    y = sum(w * c for w, c in zip(DWG_LUMA, rgb))
    hs = smoothstep(pivot + width, pivot - width, y)
    hh = smoothstep(pivot - width, pivot + width, y)
    return [c + shadow[i] * hs + highlight[i] * hh for i, c in enumerate(rgb)]


def op_contrast(rgb, k, pivot=0.336):
    return [pivot + (c - pivot) * k for c in rgb]


def op_fade(rgb, amount, tint=(0.0, 0.0, 0.0)):
    out = []
    for i, c in enumerate(rgb):
        lift = amount + tint[i]
        w = 1.0 - smoothstep(0.05, 0.42, c)
        out.append(c + lift * w)
    return out


def op_offset(rgb, stops, tint=(0.0, 0.0, 0.0)):
    return [c + stops * C.DI_STOP + tint[i] for i, c in enumerate(rgb)]


def op_hue_place(rgb, targets, strength, sat_boost=0.0):
    """Pull each hue toward the nearest of targets (angles in DI's B-Y, R-Y plane)."""
    y = sum(w * c for w, c in zip(DWG_LUMA, rgb))
    u, v = rgb[2] - y, rgb[0] - y
    ch = math.hypot(u, v)
    if ch < 1e-6:
        return rgb
    h = math.atan2(v, u)
    best = None
    for t in targets:
        d = (h - t + math.pi) % (2 * math.pi) - math.pi
        if best is None or abs(d) < abs(best):
            best = d
    nh = h - best * strength
    nc = ch * (1.0 + sat_boost)
    nu, nv = nc * math.cos(nh), nc * math.sin(nh)
    r, b = y + nv, y + nu
    wr, wg, wb = DWG_LUMA
    g = (y - wr * r - wb * b) / wg
    return [r, g, b]


def hue_of_rec709(rgb_srgb):
    lin = [((c / 255.0 + 0.055) / 1.055) ** 2.4 for c in rgb_srgb]
    di = [C.di_encode(c * 0.9) for c in C.mat_vec(C.REC709_TO_DWG, lin)]
    y = sum(w * c for w, c in zip(DWG_LUMA, di))
    return math.atan2(di[0] - y, di[2] - y)


def chain(*fs):
    def f(rgb):
        for g in fs:
            rgb = g(rgb)
        return rgb
    return f


# ---------------------------------------------------------------- the library

class Look(object):
    def __init__(self, slug, name, family, description, build):
        self.slug, self.name, self.family, self.description, self._build = slug, name, family, description, build
        self._f = None

    @property
    def function(self):
        if self._f is None:
            self._f = self._build()
        return self._f

    def as_dict(self):
        return {"slug": self.slug, "name": self.name, "family": self.family, "description": self.description}


PRINT_DYE = ((1.0, 0.03, 0.01), (0.07, 1.0, 0.03), (0.03, 0.12, 1.0))
SOFT_DYE = ((1.0, 0.04, 0.01), (0.10, 1.0, 0.04), (0.05, 0.16, 1.0))
TEAL = None  # filled lazily


def _teal_orange():
    skin = hue_of_rec709((205, 150, 120))
    teal = skin + math.pi
    return chain(film_lmt(Film(mode="print", print_k=(2.6, 2.6, 2.6), interlayer=0.3)),
                 lambda c: op_hue_place(c, [skin, teal], 0.35, 0.10),
                 lambda c: op_split(c, (-0.012, 0.004, 0.016), (0.012, 0.002, -0.012)))


LOOKS = [
    # Print film: a negative printed onto print stock
    Look("theatrical-print", "Theatrical Print", "Print",
         "A modern cinema print: dense rich blacks, cool cyan-leaning shadows, warm highlights that roll off softly, saturated colours that get deeper rather than louder.",
         lambda: film_lmt(Film(mode="print"))),
    Look("soft-print", "Soft Print", "Print",
         "A lower contrast print made for digital finishing: the print's colour and roll-off with clean neutrals and open shadows.",
         lambda: film_lmt(Film(mode="print", print_k=(2.3, 2.3, 2.3), dmax=(3.3, 3.35, 3.2), interlayer=0.24))),
    Look("warm-print", "Warm Print", "Print",
         "The cinema print timed warm, as a lab would for a golden interior or late sun: amber mids, honeyed skin, shadows kept neutral to cool.",
         lambda: film_lmt(Film(mode="print", grey_shift=(-0.035, -0.005, 0.045)))),
    Look("cool-print", "Cool Print", "Print",
         "The cinema print timed cool for night and blue hour: steel blue mids, clean whites, deep cyan-black shadows.",
         lambda: film_lmt(Film(mode="print", grey_shift=(0.035, 0.0, -0.035)))),
    Look("classic-print", "Classic Print", "Print",
         "An older print look with more saturated dyes: deep reds, rich greens, dense warm-black shadows, like a well kept repertory print.",
         lambda: film_lmt(Film(mode="print", print_k=(3.0, 2.95, 2.9), cross=0.02, interlayer=0.44,
                               dye=((1.0, 0.05, 0.02), (0.10, 1.0, 0.05), (0.04, 0.18, 1.0)), grey_shift=(-0.01, 0.0, 0.015)))),
    # Negative film, scanned
    Look("daylight-neg-50", "Daylight Neg 50", "Negative",
         "A slow daylight negative: very fine, crisp, fuller colour and a firmer curve, for bright exteriors.",
         lambda: film_lmt(Film(mode="negative", neg_gamma=(0.66, 0.65, 0.64), cross=0.02, interlayer=0.34, toe=-2.0, shoulder=2.4, scan_gamma=0.6))),
    Look("daylight-neg-250", "Daylight Neg 250", "Negative",
         "A daylight negative: clean neutral skin, gentle contrast, long highlight latitude.",
         lambda: film_lmt(Film(mode="negative", neg_gamma=(0.615, 0.61, 0.605), cross=0.03, interlayer=0.24, scan_gamma=0.6))),
    Look("tungsten-neg-200", "Tungsten Neg 200", "Negative",
         "A medium speed tungsten negative: warm interiors, a tighter cleaner curve, slightly cool shadows.",
         lambda: film_lmt(Film(mode="negative", neg_gamma=(0.64, 0.61, 0.58), cross=0.03, interlayer=0.24, toe=-2.1, scan_gamma=0.6))),
    Look("tungsten-neg-500", "Tungsten Neg 500", "Negative",
         "The fast tungsten workhorse: soft highlight roll, warm mids, teal leaning shadows, a little less saturation.",
         lambda: film_lmt(Film(mode="negative", neg_gamma=(0.65, 0.60, 0.56), cross=0.05, interlayer=0.16, toe=-2.0, shoulder=2.5, scan_gamma=0.6))),
    Look("portrait-neg-400", "Portrait Neg 400", "Negative",
         "A portrait still negative: pastel, low contrast, creamy highlights and forgiving warm skin.",
         lambda: chain(film_lmt(Film(mode="negative", neg_gamma=(0.55, 0.56, 0.57), cross=0.05, interlayer=0.14, shoulder=2.3, scan_gamma=0.6)),
                       lambda c: op_split(c, (0.0, 0.002, 0.004), (0.008, 0.002, -0.008)), lambda c: op_sat(c, 0.95))),
    Look("vivid-neg-100", "Vivid Neg 100", "Negative",
         "A saturated still negative: punchy reds and blues, crisp contrast, fine grain feel.",
         lambda: chain(film_lmt(Film(mode="negative", neg_gamma=(0.66, 0.67, 0.68), cross=0.02, interlayer=0.4, toe=-1.9, scan_gamma=0.6)),
                       lambda c: op_contrast(c, 1.06))),
    Look("consumer-neg-400", "Consumer Neg 400", "Negative",
         "A 90s consumer negative: green tinted shadows, a magenta bias in skin, cheerful saturation.",
         lambda: chain(film_lmt(Film(mode="negative", neg_gamma=(0.62, 0.58, 0.63), cross=0.03, interlayer=0.3, scan_gamma=0.6)),
                       lambda c: op_split(c, (-0.004, 0.007, -0.002), (0.004, -0.004, 0.003)))),
    # Reversal (slide) film
    Look("vivid-slide", "Vivid Slide", "Reversal",
         "A landscape slide film: high saturation, deep blues and greens, dense contrast. Expose it well.",
         lambda: film_lmt(Film(mode="reversal", neg_gamma=(0.66, 0.66, 0.66), cross=0.02, interlayer=0.32, print_k=(2.05, 2.05, 2.05), dmax=(3.4, 3.45, 3.35)))),
    Look("neutral-slide", "Neutral Slide", "Reversal",
         "A neutral slide film: accurate colour with the snap of reversal, nothing pushed.",
         lambda: film_lmt(Film(mode="reversal", neg_gamma=(0.62, 0.62, 0.62), cross=0.03, interlayer=0.16, print_k=(1.95, 1.95, 1.95), dmax=(3.3, 3.3, 3.25)))),
    # Lab processes
    Look("bleach-bypass", "Bleach Bypass", "Process",
         "Skip-bleach print: silver left in with the dyes, so contrast climbs, colour drains and highlights turn metallic.",
         lambda: film_lmt(Film(mode="print", silver=0.55, print_k=(2.9, 2.9, 2.9), interlayer=0.2))),
    Look("cross-process", "Cross Process", "Process",
         "Slide film developed as a negative: hard contrast, yellow-green highlights, blue shadows, wild saturated colour.",
         lambda: chain(film_lmt(Film(mode="reversal", neg_gamma=(0.70, 0.65, 0.56), cross=0.02, interlayer=0.3, print_k=(2.0, 1.9, 1.75),
                                     grey_shift=(-0.01, -0.02, 0.03))),
                       lambda c: op_split(c, (-0.006, 0.0, 0.015), (0.0, 0.004, -0.015)))),
    # Black and white
    Look("bw-pan-400", "B&W Pan 400", "Black and white",
         "A documentary black and white: panchromatic, soft contrast, generous shadows.",
         lambda: film_lmt(Film(mode="print", mono=(0.25, 0.55, 0.20), print_k=(2.55, 2.55, 2.55), dye=((1, 0, 0), (0, 1, 0), (0, 0, 1)),
                               dmin=(0.08, 0.08, 0.08), dmax=(3.6, 3.6, 3.6), cross=0.0, interlayer=0.0))),
    Look("bw-high-contrast", "B&W High Contrast", "Black and white",
         "A punchy reportage black and white: deep blacks, bright whites, bite in the mids.",
         lambda: film_lmt(Film(mode="print", mono=(0.30, 0.55, 0.15), print_k=(3.0, 3.0, 3.0), dye=((1, 0, 0), (0, 1, 0), (0, 0, 1)),
                               neg_gamma=(0.68, 0.68, 0.68), dmin=(0.06, 0.06, 0.06), dmax=(3.9, 3.9, 3.9), cross=0.0, interlayer=0.0))),
    Look("bw-ortho", "B&W Ortho", "Black and white",
         "An orthochromatic black and white, blind to red: darker lips and skin, luminous skies, an early cinema feel.",
         lambda: film_lmt(Film(mode="print", mono=(0.02, 0.55, 0.43), print_k=(2.7, 2.7, 2.7), dye=((1, 0, 0), (0, 1, 0), (0, 0, 1)),
                               dmin=(0.08, 0.08, 0.08), dmax=(3.5, 3.5, 3.5), cross=0.0, interlayer=0.0))),
    # Creative looks
    Look("neutral-film", "Neutral Film", "Creative",
         "A gentle film curve with no colour shift: a little print contrast and roll-off, colour left as balanced.",
         lambda: film_lmt(Film(mode="print", print_k=(2.45, 2.45, 2.45), dye=((1, 0.02, 0.0), (0.03, 1, 0.02), (0.01, 0.05, 1)), dmax=(3.5, 3.5, 3.5),
                               neg_gamma=(0.61, 0.61, 0.61), cross=0.02, interlayer=0.16))),
    Look("teal-orange", "Teal and Orange", "Creative",
         "Blockbuster complementary colour: skin and warm lights pushed to orange, the rest of the frame to teal, on a print curve.",
         _teal_orange),
    Look("faded-film", "Faded Film", "Creative",
         "An aged print: milky lifted blacks, lower contrast, muted warm colour.",
         lambda: chain(film_lmt(Film(mode="print", print_k=(2.0, 2.0, 2.0), dmax=(2.9, 2.95, 2.8), interlayer=0.2, black_comp=0.0, grey_shift=(-0.01, 0.0, 0.02))),
                       lambda c: op_fade(c, 0.025, (0.004, 0.002, -0.002)), lambda c: op_sat(c, 0.85))),
    Look("day-for-night", "Day for Night", "Creative",
         "Daylight made into moonlight: two stops down, cool blue, low saturation, highlights held.",
         lambda: chain(lambda c: op_offset(c, -1.8), lambda c: op_sat(c, 0.55),
                       film_lmt(Film(mode="print", print_k=(2.4, 2.4, 2.4), interlayer=0.15, grey_shift=(0.06, 0.01, -0.05))))),
    Look("golden-hour", "Golden Hour", "Creative",
         "Late sun: warm gold mids and highlights, soft contrast, glowing skin, shadows kept a touch cool.",
         lambda: chain(film_lmt(Film(mode="print", print_k=(2.5, 2.5, 2.5), interlayer=0.3, grey_shift=(-0.045, -0.015, 0.06))),
                       lambda c: op_split(c, (-0.004, 0.0, 0.006), (0.008, 0.003, -0.01)), lambda c: op_sat(c, 1.03))),
]

BY_SLUG = {look.slug: look for look in LOOKS}
FAMILIES = ["Print", "Negative", "Reversal", "Process", "Black and white", "Creative"]


def find(slug_or_name):
    key = (slug_or_name or "").strip().lower()
    if key in BY_SLUG:
        return BY_SLUG[key]
    for look in LOOKS:
        if look.name.lower() == key:
            return look
    return None


# ---------------------------------------------------------------- cubes

SPACES = {
    "dwg": ("DaVinci Wide Gamut / Intermediate", None, None),
    "acescct": ("ACEScct (AP1)", C.acescct_to_dwg, C.dwg_to_acescct),
}


def cube_text(look, size=33, space="dwg"):
    """The look as a .cube LUT for a working space (dwg or acescct)."""
    f = look.function
    label, to_dwg, from_dwg = SPACES[space]
    lines = ['TITLE "NOLGIA %s (%s)"' % (look.name, label),
             "# NOLGIA film look library, %s. %s" % (look.family, look.description),
             "# In and out: %s. Made by NOLGIA (nolgia.ai), our own look; GPL-3.0-or-later." % label,
             "LUT_3D_SIZE %d" % size, "DOMAIN_MIN 0 0 0", "DOMAIN_MAX 1 1 1"]
    n = size - 1
    for b in range(size):
        for g in range(size):
            for r in range(size):
                rgb = [r / n, g / n, b / n]
                if to_dwg:
                    rgb = to_dwg(rgb)
                out = f(rgb)
                if from_dwg:
                    out = from_dwg(out)
                lines.append("%.6f %.6f %.6f" % tuple(min(1.0, max(0.0, v)) for v in out))
    return "\n".join(lines) + "\n"


def file_name(look, space="dwg"):
    folder = "Looks" if space == "dwg" else "Looks ACEScct"
    return "%s/%s/%s.cube" % (folder, look.family, look.name)
