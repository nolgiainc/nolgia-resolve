# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 NOLGIA Inc
"""Colour science for NOLGIA grading, in plain Python (no numpy in Resolve's Python).

The working space is DaVinci Wide Gamut / DaVinci Intermediate (DWG/DI), from
Blackmagic Design's published white paper. Each camera curve is its maker's
published formula, read on Resolve's normalised code value (0..1): checked
against Resolve Studio 21.1.1's own colour management on a 144 patch chart
(every camera within 0.0014 DI, about 0.02 stop; see tests/test_grading_colour.py).
Resolve decodes Canon Log 2 and 3 (v1.2) without the white paper's 0.9
reflection factor, and adapts ACES AP1 to D65 with CAT02; so does this module.
"""

import math

# ---------------------------------------------------------------- 3x3 helpers

def mat_mul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]

def mat_vec(m, v):
    return [m[0][0] * v[0] + m[0][1] * v[1] + m[0][2] * v[2],
            m[1][0] * v[0] + m[1][1] * v[1] + m[1][2] * v[2],
            m[2][0] * v[0] + m[2][1] * v[1] + m[2][2] * v[2]]

def mat_inv(m):
    a, b, c = m[0]; d, e, f = m[1]; g, h, i = m[2]
    det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    return [[(e * i - f * h) / det, (c * h - b * i) / det, (b * f - c * e) / det],
            [(f * g - d * i) / det, (a * i - c * g) / det, (c * d - a * f) / det],
            [(d * h - e * g) / det, (b * g - a * h) / det, (a * e - b * d) / det]]

def xy_to_XYZ(x, y):
    return [x / y, 1.0, (1.0 - x - y) / y]

def npm(primaries, white):
    """RGB to XYZ matrix from xy primaries and white."""
    cols = [xy_to_XYZ(*p) for p in primaries]
    m = [[cols[j][i] for j in range(3)] for i in range(3)]
    s = mat_vec(mat_inv(m), xy_to_XYZ(*white))
    return [[m[i][j] * s[j] for j in range(3)] for i in range(3)]

CAT02 = [[0.7328, 0.4296, -0.1624], [-0.7036, 1.6975, 0.0061], [0.0030, 0.0136, 0.9834]]

def cat(src_white, dst_white):
    s = mat_vec(CAT02, xy_to_XYZ(*src_white)); d = mat_vec(CAT02, xy_to_XYZ(*dst_white))
    scale = [[d[0] / s[0], 0, 0], [0, d[1] / s[1], 0], [0, 0, d[2] / s[2]]]
    return mat_mul(mat_inv(CAT02), mat_mul(scale, CAT02))

D65 = (0.3127, 0.3290)
ACES_WHITE = (0.32168, 0.33767)

GAMUTS = {
    "dwg": ([(0.8000, 0.3130), (0.1682, 0.9877), (0.0790, -0.1155)], D65),
    "rec709": ([(0.64, 0.33), (0.30, 0.60), (0.15, 0.06)], D65),
    "rec2020": ([(0.708, 0.292), (0.170, 0.797), (0.131, 0.046)], D65),
    "sgamut3cine": ([(0.766, 0.275), (0.225, 0.800), (0.089, -0.087)], D65),
    "awg3": ([(0.6840, 0.3130), (0.2210, 0.8480), (0.0861, -0.1020)], D65),
    "awg4": ([(0.7347, 0.2653), (0.1424, 0.8576), (0.0991, -0.0308)], D65),
    "vgamut": ([(0.730, 0.280), (0.165, 0.840), (0.100, -0.030)], D65),
    "cinemagamut": ([(0.74, 0.27), (0.17, 1.14), (0.08, -0.10)], D65),
    "bmdwg5": ([(0.7177215, 0.3171181), (0.2280410, 0.8615690), (0.1005841, -0.0820452)], (0.3127170, 0.3290312)),
    "ap1": ([(0.713, 0.293), (0.165, 0.830), (0.128, 0.044)], ACES_WHITE),
}

def gamut_matrix(src, dst):
    """Linear RGB in gamut src to linear RGB in gamut dst (Bradford when the whites differ)."""
    sp, sw = GAMUTS[src]; dp, dw = GAMUTS[dst]
    m = npm(sp, sw)
    if abs(sw[0] - dw[0]) > 1e-6 or abs(sw[1] - dw[1]) > 1e-6:
        m = mat_mul(cat(sw, dw), m)
    return mat_mul(mat_inv(npm(dp, dw)), m)

# ---------------------------------------------------------------- transfer functions
# decode: code value (0..1) -> scene linear (0.18 = mid grey); encode: the inverse.

DI_A, DI_B, DI_C, DI_M = 0.0075, 7.0, 0.07329248, 10.44426855
DI_LIN_CUT, DI_LOG_CUT = 0.00262409, 0.02740668

def di_encode(x):
    return x * DI_M if x <= DI_LIN_CUT else (math.log2(x + DI_A) + DI_B) * DI_C

def di_decode(y):
    return y / DI_M if y <= DI_LOG_CUT else 2.0 ** (y / DI_C - DI_B) - DI_A

DI_STOP = DI_C  # one stop in DaVinci Intermediate code, above the linear toe

def slog3_decode(y):
    if y >= 171.2102946929 / 1023.0:
        return (10.0 ** ((y * 1023.0 - 420.0) / 261.5)) * 0.19 - 0.01
    return (y * 1023.0 - 95.0) * 0.01125 / (171.2102946929 - 95.0)

def slog3_encode(x):
    if x >= 0.01125:
        return (420.0 + math.log10((x + 0.01) / 0.19) * 261.5) / 1023.0
    return (x * (171.2102946929 - 95.0) / 0.01125 + 95.0) / 1023.0

LC3 = dict(cut=0.010591, a=5.555556, b=0.052272, c=0.247190, d=0.385537, e=5.367655, f=0.092809)

def logc3_decode(y):
    p = LC3
    if y > p["e"] * p["cut"] + p["f"]:
        return (10.0 ** ((y - p["d"]) / p["c"]) - p["b"]) / p["a"]
    return (y - p["f"]) / p["e"]

def logc3_encode(x):
    p = LC3
    if x > p["cut"]:
        return p["c"] * math.log10(p["a"] * x + p["b"]) + p["d"]
    return p["e"] * x + p["f"]

LC4_A = (2.0 ** 18 - 16.0) / 117.45
LC4_B = (1023.0 - 95.0) / 1023.0
LC4_C = 95.0 / 1023.0
LC4_S = (7.0 * math.log(2.0) * 2.0 ** (7.0 - 14.0 * LC4_C / LC4_B)) / (LC4_A * LC4_B)
LC4_T = (2.0 ** (14.0 * (-LC4_C / LC4_B) + 6.0) - 64.0) / LC4_A

def logc4_decode(y):
    if y >= 0.0:
        return (2.0 ** (14.0 * (y - LC4_C) / LC4_B + 6.0) - 64.0) / LC4_A
    return y * LC4_S + LC4_T

def logc4_encode(x):
    if x >= LC4_T:
        return (math.log2(LC4_A * x + 64.0) - 6.0) / 14.0 * LC4_B + LC4_C
    return (x - LC4_T) / LC4_S

def vlog_decode(y):
    if y < 0.181:
        return (y - 0.125) / 5.6
    return 10.0 ** ((y - 0.598206) / 0.241514) - 0.00873

def vlog_encode(x):
    if x < 0.01:
        return 5.6 * x + 0.125
    return 0.241514 * math.log10(x + 0.00873) + 0.598206

def _legal_to_full(y):  # normalised 10-bit code value -> IRE (0..1)
    return (y * 1023.0 - 64.0) / 876.0

def _full_to_legal(v):
    return (v * 876.0 + 64.0) / 1023.0

def clog2_decode(y):  # Canon Log 2 v1.2 (constants are normalised code values)
    v = y
    if v < 0.092864125:
        x = -(10.0 ** ((0.092864125 - v) / 0.24136077) - 1.0) / 87.09937546
    else:
        x = (10.0 ** ((v - 0.092864125) / 0.24136077) - 1.0) / 87.09937546
    return x

def clog2_encode(x):
    if x < 0:
        v = -0.24136077 * math.log10(-x * 87.09937546 + 1.0) + 0.092864125
    else:
        v = 0.24136077 * math.log10(x * 87.09937546 + 1.0) + 0.092864125
    return v

def clog3_decode(y):  # Canon Log 3 v1.2
    v = y
    if v < 0.097465473:
        x = -(10.0 ** ((0.12783901 - v) / 0.36726845) - 1.0) / 14.98325
    elif v <= 0.15277891:
        x = (v - 0.12512219) / 1.9754798
    else:
        x = (10.0 ** ((v - 0.12240537) / 0.36726845) - 1.0) / 14.98325
    return x

def clog3_encode(x):
    if x < -0.014:
        v = -0.36726845 * math.log10(-x * 14.98325 + 1.0) + 0.12783901
    elif x <= 0.014:
        v = 1.9754798 * x + 0.12512219
    else:
        v = 0.36726845 * math.log10(x * 14.98325 + 1.0) + 0.12240537
    return v

BMD5 = dict(A=0.08692876065491224, B=0.005494072432257808, C=0.5300133392291939,
            D=8.283605932402494, E=0.09246575342465753, cut=0.005)

def bmdfilm5_decode(y):
    p = BMD5
    if y < p["D"] * p["cut"] + p["E"]:
        return (y - p["E"]) / p["D"]
    return math.exp((y - p["C"]) / p["A"]) - p["B"]

def bmdfilm5_encode(x):
    p = BMD5
    if x < p["cut"]:
        return p["D"] * x + p["E"]
    return p["A"] * math.log(x + p["B"]) + p["C"]

AL = dict(R0=-0.05641088, Rt=0.01, c=47.28711236, beta=0.00964052, gamma=0.08550479, delta=0.69336945)
AL_PT = AL["c"] * (AL["Rt"] - AL["R0"]) ** 2

def applelog_decode(y):
    p = AL
    if y >= AL_PT:
        return 2.0 ** ((y - p["delta"]) / p["gamma"]) - p["beta"]
    if y >= 0.0:
        return math.sqrt(y / p["c"]) + p["R0"]
    return p["R0"]

def applelog_encode(x):
    p = AL
    if x >= p["Rt"]:
        return p["gamma"] * math.log2(x + p["beta"]) + p["delta"]
    if x >= p["R0"]:
        return p["c"] * (x - p["R0"]) ** 2
    return 0.0

ACC_A, ACC_B = 10.5402377416545, 0.0729055341958355

def acescct_decode(y):
    if y <= 0.155251141552511:
        return (y - ACC_B) / ACC_A
    return 2.0 ** (y * 17.52 - 9.72)

def acescct_encode(x):
    if x <= 0.0078125:
        return ACC_A * x + ACC_B
    return (math.log2(x) + 9.72) / 17.52

FL = dict(cut1=0.00089, cut2=0.100537775223865, a=0.555556, b=0.009468, c=0.344676, d=0.790453, e=8.735631, f=0.092864)
FL2 = dict(cut1=0.000889, cut2=0.100686685370811, a=5.555556, b=0.064829, c=0.245281, d=0.384316, e=8.799461, f=0.092864)

def _flog_encode(x, k):
    return k["e"] * x + k["f"] if x < k["cut1"] else k["c"] * math.log10(k["a"] * x + k["b"]) + k["d"]

def _flog_decode(y, k):
    return (y - k["f"]) / k["e"] if y < k["cut2"] else (10.0 ** ((y - k["d"]) / k["c"]) - k["b"]) / k["a"]

def flog_encode(x):
    return _flog_encode(x, FL)

def flog_decode(y):
    return _flog_decode(y, FL)

def flog2_encode(x):
    return _flog_encode(x, FL2)

def flog2_decode(y):
    return _flog_decode(y, FL2)

def dlog_encode(x):  # DJI D-Log
    return 6.025 * x + 0.0929 if x <= 0.0078 else math.log10(x * 0.9892 + 0.0108) * 0.256663 + 0.584555

def dlog_decode(y):
    return (y - 0.0929) / 6.025 if y <= 0.14 else (10.0 ** (3.89616 * y - 2.27752) - 0.0108) / 0.9892

NL = dict(cut1=0.328, cut2=452.0 / 1023.0, a=650.0 / 1023.0, b=0.0075, c=150.0 / 1023.0, d=619.0 / 1023.0)

def nlog_encode(x):  # Nikon N-Log
    if x < NL["cut1"]:
        v = x + NL["b"]
        return NL["a"] * (v ** (1.0 / 3.0) if v >= 0 else -((-v) ** (1.0 / 3.0)))
    return NL["c"] * math.log(x) + NL["d"]

def nlog_decode(y):
    if y < NL["cut2"]:
        return (y / NL["a"]) ** 3 - NL["b"]
    return math.exp((y - NL["d"]) / NL["c"])

GAMUTS["dgamut"] = ([(0.71, 0.31), (0.21, 0.88), (0.09, -0.08)], D65)
# F-Gamut C, as DaVinci Resolve 21.1.1's own "Fujifilm F-Log2 C" input transform has it (fitted on a 144 patch
# chart, 0.3% residual), so a Fujifilm F-Log2 C LUT gets the picture Resolve would give it.
GAMUTS["fgamutc"] = ([(0.7347, 0.2653), (0.0254, 0.9741), (0.1172, -0.0225)], D65)

# camera id -> (label, curve name, gamut)
CAMERAS = {
    "slog3": ("Sony S-Log3 / S-Gamut3.Cine", "slog3", "sgamut3cine"),
    "logc3": ("ARRI LogC3 (EI 800) / ARRI Wide Gamut 3", "logc3", "awg3"),
    "logc4": ("ARRI LogC4 / ARRI Wide Gamut 4", "logc4", "awg4"),
    "vlog": ("Panasonic V-Log / V-Gamut", "vlog", "vgamut"),
    "clog2": ("Canon Log 2 / Cinema Gamut", "clog2", "cinemagamut"),
    "clog3": ("Canon Log 3 / Cinema Gamut", "clog3", "cinemagamut"),
    "bmdfilm5": ("Blackmagic Film Gen 5 / Blackmagic Wide Gamut", "bmdfilm5", "bmdwg5"),
    "applelog": ("Apple Log / Rec.2020", "applelog", "rec2020"),
    "flog": ("Fujifilm F-Log / F-Gamut", "flog", "rec2020"),
    "flog2": ("Fujifilm F-Log2 / F-Gamut", "flog2", "rec2020"),
    "flog2c": ("Fujifilm F-Log2 C / F-Gamut C", "flog2", "fgamutc"),
    "dlog": ("DJI D-Log / D-Gamut", "dlog", "dgamut"),
    "nlog": ("Nikon N-Log / N-Gamut", "nlog", "rec2020"),
}

CURVES = {
    "di": (di_decode, di_encode), "slog3": (slog3_decode, slog3_encode), "logc3": (logc3_decode, logc3_encode),
    "logc4": (logc4_decode, logc4_encode), "vlog": (vlog_decode, vlog_encode), "clog2": (clog2_decode, clog2_encode),
    "clog3": (clog3_decode, clog3_encode), "bmdfilm5": (bmdfilm5_decode, bmdfilm5_encode),
    "applelog": (applelog_decode, applelog_encode), "acescct": (acescct_decode, acescct_encode),
    "flog": (flog_decode, flog_encode), "flog2": (flog2_decode, flog2_encode), "dlog": (dlog_decode, dlog_encode),
    "nlog": (nlog_decode, nlog_encode),
}

def camera_to_dwg(camera):
    label, curve, gamut = CAMERAS[camera]
    dec = CURVES[curve][0]
    m = gamut_matrix(gamut, "dwg")
    def f(rgb):
        lin = mat_vec(m, [dec(c) for c in rgb])
        return [di_encode(c) for c in lin]
    return f

def dwg_to_camera(camera):
    label, curve, gamut = CAMERAS[camera]
    enc = CURVES[curve][1]
    m = gamut_matrix("dwg", gamut)
    def f(rgb_di):
        lin = mat_vec(m, [di_decode(c) for c in rgb_di])
        return [enc(c) for c in lin]
    return f

# ---------------------------------------------------------------- display (CST OUT)
# NOLGIA's own display rendering, DWG/DI to Rec.709 (BT.1886 gamma 2.4):
#   1. DWG linear to Rec.709 linear (matrix);
#   2. a soft gamut compression toward the achromatic axis, so saturated colours
#      outside Rec.709 keep their hue instead of clipping (distance from the
#      axis compressed above THRESH, LIMIT lands on the gamut boundary);
#   3. per-channel tone curve h(x) = M x^G / (x^G + K): a toe, mid grey 0.18 to
#      display 0.11 (code 0.399), highlights rolling off to white at +6.5 stops;
#   4. gamma 2.4 encode.
# Every step has an exact inverse (display_to_dwg), so a Rec.709 clip read in
# with CST IN "Rec.709" and put out with CST OUT comes back unchanged.

TONE_G = 1.18
GREY_IN, GREY_OUT, WHITE_IN = 0.18, 0.11, 0.18 * 2 ** 6.5

def _tone_params():
    # solve M, K: h(GREY_IN) = GREY_OUT and h(WHITE_IN) = 1
    a, b = GREY_IN ** TONE_G, WHITE_IN ** TONE_G
    # M a / (a + K) = GREY_OUT ; M b / (b + K) = 1  ->  K = (M - 1) b ; M a = GREY_OUT (a + (M - 1) b)
    m = GREY_OUT * (a - b) / (a - GREY_OUT * b)
    return m, (m - 1.0) * b

TONE_M, TONE_K = _tone_params()
GC_THRESH, GC_LIMIT, GC_POWER = 0.80, 1.25, 1.2
_GC_SCALE = (GC_LIMIT - GC_THRESH) / (((1.0 - GC_THRESH) / (GC_LIMIT - GC_THRESH)) ** -GC_POWER - 1.0) ** (1.0 / GC_POWER)
DWG_TO_709 = gamut_matrix("dwg", "rec709")
REC709_TO_DWG = gamut_matrix("rec709", "dwg")
DISPLAY_MAX = 0.9999  # display code values at and above this read as this

def tone(x):
    if x <= 0.0:
        return 0.0
    p = x ** TONE_G
    return TONE_M * p / (p + TONE_K)

def tone_inverse(y):
    if y <= 0.0:
        return 0.0
    y = min(y, TONE_M * 0.999999)
    return (TONE_K * y / (TONE_M - y)) ** (1.0 / TONE_G)

def _compress(d):
    if d < GC_THRESH:
        return d
    s = (d - GC_THRESH) / _GC_SCALE
    return GC_THRESH + _GC_SCALE * s / (1.0 + s ** GC_POWER) ** (1.0 / GC_POWER)

def _uncompress(d):
    if d < GC_THRESH:
        return d
    s = (d - GC_THRESH) / _GC_SCALE
    t = s ** GC_POWER
    if t >= 1.0:
        return GC_LIMIT * 4.0
    return GC_THRESH + _GC_SCALE * s / (1.0 - t) ** (1.0 / GC_POWER)

def gamut_compress(rgb):
    ach = max(rgb)
    if ach <= 0.0:
        return [0.0, 0.0, 0.0]
    return [ach - _compress((ach - c) / ach) * ach for c in rgb]

def gamut_uncompress(rgb):
    ach = max(rgb)
    if ach <= 0.0:
        return [0.0, 0.0, 0.0]
    return [ach - _uncompress((ach - c) / ach) * ach for c in rgb]

def dwg_to_display(rgb_di):
    """CST OUT: DWG/DI code values to Rec.709 gamma 2.4 code values."""
    lin = mat_vec(DWG_TO_709, [di_decode(c) for c in rgb_di])
    lin = gamut_compress(lin)
    return [min(1.0, tone(c) ** (1.0 / 2.4)) for c in lin]

def display_to_dwg(rgb):
    """The exact inverse of dwg_to_display (CST IN for Rec.709 footage, and how looks are built)."""
    lin = [tone_inverse(max(0.0, min(DISPLAY_MAX, c)) ** 2.4) for c in rgb]
    lin = gamut_uncompress(lin)
    return [di_encode(c) for c in mat_vec(REC709_TO_DWG, lin)]

def acescct_to_dwg(rgb):
    m = gamut_matrix("ap1", "dwg")
    return [di_encode(c) for c in mat_vec(m, [acescct_decode(c) for c in rgb])]

def dwg_to_acescct(rgb):
    m = gamut_matrix("dwg", "ap1")
    return [acescct_encode(max(c, 1e-10)) if c > 0 else acescct_encode(c) for c in mat_vec(m, [di_decode(c) for c in rgb])]

# Cineon, as DaVinci Resolve's own "Linear to Cineon Log" LUT defines it (scene linear 1.0 at code 685).
CINEON_OFFSET = 10.0 ** ((95.0 - 685.0) / 300.0)

def cineon_encode(x):
    return (685.0 + 300.0 * math.log10(max(x * (1.0 - CINEON_OFFSET) + CINEON_OFFSET, 1e-10))) / 1023.0

def cineon_decode(y):
    return (10.0 ** ((y * 1023.0 - 685.0) / 300.0) - CINEON_OFFSET) / (1.0 - CINEON_OFFSET)
