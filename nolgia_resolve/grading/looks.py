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

NON_AFFILIATION = ("Film stock and camera names are trademarks of their owners and describe the look each emulates. "
                   "NOLGIA is not affiliated with or endorsed by them.")

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
                 scan_gamma=None, black_comp=1.0, toe_k=2.6, shoulder_k=1.6, scan_per_channel=False,
                 scan_ref=0.55, view=1.0):
        self.mode = mode
        self.neg_gamma = neg_gamma
        self.toe_k, self.shoulder_k = toe_k, shoulder_k
        # A scan normalises each layer by its own gamma (a neutral grey scale), and keeps the stock's overall
        # contrast against a reference negative (scan_ref).
        self.scan_per_channel, self.scan_ref = scan_per_channel, scan_ref
        self.view = view  # viewing compensation: display ** view (a print made for a dark theatre, on a video display)
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
        toe = self.toe if isinstance(self.toe, (tuple, list)) else (self.toe,) * 3
        sh = self.shoulder if isinstance(self.shoulder, (tuple, list)) else (self.shoulder,) * 3
        d = [neg_curve(le[i], self.neg_gamma[i], toe[i], self.toe_k, sh[i], self.shoulder_k) for i in range(3)]
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
        if self.view != 1.0:
            t = [max(v, 0.0) ** self.view for v in t]
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
            if self.scan_per_channel:
                # A scan with its black at the film's base (Cineon's black offset): base density is black, mid
                # grey stays 0.18, each layer by its own gamma, the stock's contrast against the reference.
                a = (sum(self.neg_gamma) / 3.0) / self.scan_ref
                out = []
                for i in range(3):
                    x = 10.0 ** (max(dn[i], 0.0) * a / self.neg_gamma[i]) - 1.0
                    xg = 10.0 ** (self.dn_grey[i] * a / self.neg_gamma[i]) - 1.0
                    out.append(0.18 * x / xg)
                return out, False
            else:
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
    def __init__(self, slug, name, family, description, build, aliases=(), emulates=None):
        self.slug, self.name, self.family, self.description, self._build = slug, name, family, description, build
        self.aliases = tuple(aliases)
        self.emulates = emulates  # the stock the look is modelled on, from its maker's published data; None if not
        self._f = None

    @property
    def function(self):
        if self._f is None:
            self._f = self._build()
        return self._f

    def as_dict(self):
        out = {"slug": self.slug, "name": self.name, "family": self.family, "description": self.description}
        if self.emulates:
            out["emulates"] = self.emulates
        return out


PRINT_DYE = ((1.0, 0.03, 0.01), (0.07, 1.0, 0.03), (0.03, 0.12, 1.0))
SOFT_DYE = ((1.0, 0.04, 0.01), (0.10, 1.0, 0.04), (0.05, 0.16, 1.0))
TEAL = None  # filled lazily


def _teal_orange():
    skin = hue_of_rec709((205, 150, 120))
    teal = skin + math.pi
    return chain(film_lmt(Film(mode="print", print_k=(2.6, 2.6, 2.6), interlayer=0.3)),
                 lambda c: op_hue_place(c, [skin, teal], 0.35, 0.10),
                 lambda c: op_split(c, (-0.012, 0.004, 0.016), (0.012, 0.002, -0.012)))


# Characteristic data from the makers' published technical data sheets (research/STOCK-DATA.md in the build
# notes): per-layer gamma and density range read off each sheet's curves, latitude from where the curves
# bend, colour behaviour as each sheet describes it. Negatives are modelled as scanned (each layer
# normalised by its own gamma, so the grey scale is neutral) and keep their own contrast, toe, shoulder
# and saturation; prints are the 5219 negative printed on the print stock, with a viewing compensation of
# 0.9 for a Rec.709 display.
V3_500T, V3_250D, V3_200T, V3_50D = (0.455, 0.53, 0.52), (0.47, 0.55, 0.54), (0.48, 0.56, 0.53), (0.47, 0.56, 0.54)
P2383 = dict(print_k=(4.45, 4.0, 3.95), dmin=(0.05, 0.05, 0.10), dmax=(4.12, 4.10, 4.09))
P3513 = dict(print_k=(4.7, 3.6, 3.8), dmin=(0.082, 0.093, 0.154), dmax=(3.86, 4.05, 3.94))
NEG_5219 = dict(neg_gamma=V3_500T, toe=-1.25, shoulder=2.7, interlayer=0.2, cross=0.03)
VIEW = 0.9


def _neg(gamma, interlayer, cross, toe=-1.25, shoulder=2.7, toe_k=2.6):
    return Film(mode="negative", neg_gamma=gamma, interlayer=interlayer, cross=cross, toe=toe, shoulder=shoulder,
                toe_k=toe_k, scan_per_channel=True)


def _print(stock, grey_shift=(0.0, 0.0, 0.0)):
    return Film(mode="print", view=VIEW, grey_shift=grey_shift, **dict(NEG_5219, **stock))


def _mono(weights, gamma, toe, toe_k, print_k, shoulder=2.7):
    return Film(mode="print", mono=weights, neg_gamma=(gamma,) * 3, toe=toe, toe_k=toe_k, shoulder=shoulder,
                print_k=(print_k,) * 3, dye=((1, 0, 0), (0, 1, 0), (0, 0, 1)), dmin=(0.08,) * 3, dmax=(3.6,) * 3,
                cross=0.0, interlayer=0.0, view=VIEW)


LOOKS = [
    # Print film: Kodak VISION3 5219 printed on the print stock
    Look("kodak-2383-print", "Kodak 2383 print (NOLGIA)", "Print",
         "The cinema print: 5219 printed on 2383 from Kodak's published curves (H-1-2383): dense rich blacks, "
         "neutral highlights, the red layer steepest.",
         lambda: film_lmt(_print(P2383)), aliases=("theatrical-print",),
         emulates="Kodak VISION Color Print Film 2383 (Kodak H-1-2383, H-61B)"),
    Look("kodak-2383-print-warm", "Kodak 2383 print, warm timing (NOLGIA)", "Print",
         "The 2383 print timed warm, as a lab would for a golden interior or late sun.",
         lambda: film_lmt(_print(P2383, (-0.035, -0.005, 0.045))), aliases=("warm-print",),
         emulates="Kodak VISION Color Print Film 2383 (Kodak H-1-2383)"),
    Look("kodak-2383-print-cool", "Kodak 2383 print, cool timing (NOLGIA)", "Print",
         "The 2383 print timed cool for night and blue hour.",
         lambda: film_lmt(_print(P2383, (0.035, 0.0, -0.035))), aliases=("cool-print",),
         emulates="Kodak VISION Color Print Film 2383 (Kodak H-1-2383)"),
    Look("fujifilm-3513di-print", "Fujifilm Eterna-CP 3513DI print (NOLGIA)", "Print",
         "5219 printed on Fujifilm's DI print stock, from Fujifilm's published curves (KB-0707E): the red layer "
         "steepest, green softest, a slightly lower black.",
         lambda: film_lmt(_print(P3513)), aliases=("soft-print",),
         emulates="Fujifilm ETERNA-CP 3513DI (Fujifilm KB-0707E)"),
    Look("classic-print", "Classic Print", "Print",
         "An older, denser print with more saturated dyes: deep reds, rich greens, dense warm-black shadows.",
         lambda: film_lmt(Film(mode="print", print_k=(3.0, 2.95, 2.9), cross=0.02, interlayer=0.44,
                               dye=((1.0, 0.05, 0.02), (0.10, 1.0, 0.05), (0.04, 0.18, 1.0)),
                               grey_shift=(-0.01, 0.0, 0.015)))),
    # Negative film, scanned
    Look("kodak-vision3-50d-5203", "Kodak Vision3 50D 5203 (NOLGIA)", "Negative",
         "The slow daylight negative, from Kodak's curves (H-1-5203): finest grain, full colour, long latitude.",
         lambda: film_lmt(_neg(V3_50D, 0.24, 0.03)), aliases=("daylight-neg-50",),
         emulates="Kodak VISION3 50D 5203 (Kodak H-1-5203)"),
    Look("kodak-vision3-250d-5207", "Kodak Vision3 250D 5207 (NOLGIA)", "Negative",
         "The daylight negative, from Kodak's curves (H-1-5207): even layers, natural skin, soft contrast.",
         lambda: film_lmt(_neg(V3_250D, 0.22, 0.03)), aliases=("daylight-neg-250",),
         emulates="Kodak VISION3 250D 5207 (Kodak H-1-5207)"),
    Look("kodak-vision3-200t-5213", "Kodak Vision3 200T 5213 (NOLGIA)", "Negative",
         "The medium speed tungsten negative, from Kodak's curves (H-1-5213): a sharper toe and clean shadows.",
         lambda: film_lmt(_neg(V3_200T, 0.22, 0.03, toe=-1.2, toe_k=3.5)), aliases=("tungsten-neg-200",),
         emulates="Kodak VISION3 200T 5213 (Kodak H-1-5213)"),
    Look("kodak-vision3-500t-5219", "Kodak Vision3 500T 5219 (NOLGIA)", "Negative",
         "The fast tungsten workhorse, from Kodak's curves (H-1-5219): the softest contrast of the four, long "
         "highlight latitude, a little less saturation.",
         lambda: film_lmt(_neg(V3_500T, 0.2, 0.03)), aliases=("tungsten-neg-500",),
         emulates="Kodak VISION3 500T 5219 (Kodak H-1-5219)"),
    Look("fujifilm-eterna-250d-8563", "Fujifilm Eterna 250D 8563 (NOLGIA)", "Negative",
         "Fujifilm's daylight negative, from its published curves (KB-0707E): a subtle palette with restrained "
         "saturation, natural skin.",
         lambda: film_lmt(_neg((0.47, 0.50, 0.56), 0.08, 0.04, toe=-1.35, shoulder=3.0)),
         emulates="Fujifilm ETERNA 250D 8563 (Fujifilm KB-0707E)"),
    Look("fujifilm-eterna-vivid-160", "Fujifilm Eterna Vivid 160 (NOLGIA)", "Negative",
         "The highest contrast Eterna, from Fujifilm's curves (KB-0707E, KB-0701E): a saturated palette, crisp deep "
         "blacks.",
         lambda: film_lmt(_neg((0.48, 0.55, 0.57), 0.36, 0.03, toe=-1.2, shoulder=3.0)),
         emulates="Fujifilm ETERNA Vivid 160 8543 (Fujifilm KB-0707E, KB-0701E)"),
    Look("kodak-portra-400", "Kodak Portra 400 (NOLGIA)", "Negative",
         "The portrait still negative, from Kodak Alaris's curves (E-4050): broad overlapping layers, warm forgiving "
         "skin, very long latitude.",
         lambda: chain(film_lmt(_neg((0.54, 0.56, 0.63), 0.26, 0.06, toe=-1.4, shoulder=3.0)),
                       lambda c: op_split(c, (0.0, 0.002, 0.004), (0.006, 0.002, -0.006))),
         aliases=("portrait-neg-400",), emulates="Kodak Portra 400 (Kodak Alaris E-4050)"),
    Look("kodak-ektar-100", "Kodak Ektar 100 (NOLGIA)", "Negative",
         "The saturated still negative, from Kodak Alaris's curves (E-4046): high saturation, crisp contrast, and "
         "red and green rolling off above about 3.5 stops over grey, so the brightest highlights cool.",
         lambda: film_lmt(_neg((0.56, 0.57, 0.66), 0.42, 0.02, toe=-1.4, shoulder=(1.25, 1.25, 3.0))),
         aliases=("vivid-neg-100",), emulates="Kodak Ektar 100 (Kodak Alaris E-4046)"),
    Look("fujifilm-superia-xtra-400", "Fujifilm Superia X-TRA 400 (NOLGIA)", "Negative",
         "The consumer still negative, from Fujifilm's curves (AF3-0217E): the most contrast of the negatives, "
         "natural skin, grey balance held.",
         lambda: film_lmt(_neg((0.61, 0.67, 0.70), 0.22, 0.04, toe=-1.2)), aliases=("consumer-neg-400",),
         emulates="Fujifilm Superia X-TRA 400 (Fujifilm AF3-0217E)"),
    # Reversal (slide) film
    Look("fujifilm-velvia-50", "Fujifilm Velvia 50 (NOLGIA)", "Reversal",
         "The landscape slide film, from Fujifilm's curves (AF3-0221E2): very high saturation, dense blacks, short "
         "latitude. Expose it well.",
         lambda: film_lmt(Film(mode="reversal", neg_gamma=(0.6, 0.6, 0.6), cross=0.02, interlayer=0.38,
                               print_k=(2.6, 2.6, 2.6), dmin=(0.15, 0.17, 0.17), dmax=(3.34, 3.80, 3.70), view=VIEW)),
         aliases=("vivid-slide",), emulates="Fujifilm Velvia 50 (Fujifilm AF3-0221E2)"),
    Look("fujifilm-provia-100f", "Fujifilm Provia 100F (NOLGIA)", "Reversal",
         "The neutral slide film, from Fujifilm's curves (AF3-036E): brilliant primaries without losing pastels.",
         lambda: film_lmt(Film(mode="reversal", neg_gamma=(0.6, 0.6, 0.6), cross=0.03, interlayer=0.18,
                               print_k=(2.35, 2.35, 2.35), dmin=(0.08, 0.08, 0.08), dmax=(3.29, 3.42, 3.33), view=VIEW,
                               dye=SOFT_DYE)),
         aliases=("neutral-slide",), emulates="Fujifilm Provia 100F (Fujifilm AF3-036E)"),
    # Lab processes
    Look("bleach-bypass", "Bleach Bypass", "Process",
         "Skip-bleach print: silver left in with the dyes, so contrast climbs, colour drains and highlights turn metallic.",
         lambda: film_lmt(Film(mode="print", silver=0.55, print_k=(2.9, 2.9, 2.9), interlayer=0.2))),
    Look("cross-process", "Cross Process", "Process",
         "Slide film developed as a negative: hard contrast, yellow-green highlights, blue shadows, wild saturated colour.",
         lambda: chain(film_lmt(Film(mode="reversal", neg_gamma=(0.70, 0.65, 0.56), cross=0.02, interlayer=0.3,
                                     print_k=(2.0, 1.9, 1.75), grey_shift=(-0.01, -0.02, 0.03))),
                       lambda c: op_split(c, (-0.006, 0.0, 0.015), (0.0, 0.004, -0.015)))),
    # Black and white
    Look("kodak-tri-x-400", "Kodak Tri-X 400 (NOLGIA)", "Black and white",
         "The reportage black and white, from Kodak Alaris's curves (F-4017): panchromatic with a blue lean, a long "
         "soft toe, printed punchy.",
         lambda: film_lmt(_mono((0.24, 0.46, 0.30), 0.56, -1.3, 1.6, 3.6)), aliases=("bw-high-contrast",),
         emulates="Kodak Tri-X 400 (Kodak Alaris F-4017)"),
    Look("ilford-hp5-plus", "Ilford HP5 Plus (NOLGIA)", "Black and white",
         "The documentary black and white, from HARMAN's curves: panchromatic and green-peaked, generous shadows.",
         lambda: film_lmt(_mono((0.24, 0.56, 0.20), 0.645, -1.4, 2.6, 3.1)), aliases=("bw-pan-400",),
         emulates="Ilford HP5 Plus (HARMAN technical information sheet)"),
    Look("ilford-ortho-plus-80", "Ilford Ortho Plus 80 (NOLGIA)", "Black and white",
         "Orthochromatic, blind to red (HARMAN's sheet: reds come out much darker): darker lips and skin, luminous "
         "skies, an early cinema feel.",
         lambda: film_lmt(_mono((0.0, 0.45, 0.55), 0.66, -1.3, 2.6, 3.1, shoulder=1.75)), aliases=("bw-ortho",),
         emulates="Ilford Ortho Plus 80 (HARMAN technical information sheet)"),
    # Creative looks
    Look("neutral-film", "Neutral Film", "Creative",
         "A gentle film curve with no colour shift: a little print contrast and roll-off, colour left as balanced.",
         lambda: film_lmt(Film(mode="print", print_k=(2.45, 2.45, 2.45), dye=((1, 0.02, 0.0), (0.03, 1, 0.02), (0.01, 0.05, 1)),
                               dmax=(3.5, 3.5, 3.5), neg_gamma=(0.61, 0.61, 0.61), cross=0.02, interlayer=0.16))),
    Look("teal-orange", "Teal and Orange", "Creative",
         "Blockbuster complementary colour: skin and warm lights pushed to orange, the rest of the frame to teal, on a print curve.",
         _teal_orange),
    Look("faded-film", "Faded Film", "Creative",
         "An aged print: milky lifted blacks, lower contrast, muted warm colour.",
         lambda: chain(film_lmt(Film(mode="print", print_k=(2.0, 2.0, 2.0), dmax=(2.9, 2.95, 2.8), interlayer=0.2,
                                     black_comp=0.0, grey_shift=(-0.01, 0.0, 0.02))),
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
        if look.name.lower() == key or key in look.aliases:
            return look
    for look in LOOKS:  # "Kodak 2383 print" finds "Kodak 2383 print (NOLGIA)"
        if look.name.lower().replace(" (nolgia)", "") == key.replace(" (nolgia)", ""):
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
             "# " + NON_AFFILIATION,
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


def file_stem(look):
    """The look's name as a file name a DCTL can point at: DaVinci Resolve 21.1.1 stops with an error on a
    DEFINE_LUT path that has a comma or a bracket in it."""
    stem = look.name.replace("(NOLGIA)", "NOLGIA")
    return " ".join("".join(c for c in stem if c not in "(),;\"'").split())


def file_name(look, space="dwg"):
    folder = "Looks" if space == "dwg" else "Looks ACEScct"
    return "%s/%s/%s.cube" % (folder, look.family, file_stem(look))
