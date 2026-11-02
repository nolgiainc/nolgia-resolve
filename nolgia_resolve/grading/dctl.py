# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 NOLGIA Inc
"""DCTL sources for the NOLGIA grade's transform nodes, generated from colour.py.

Each DCTL is plain text the person can read and change. Resolve runs it on the
GPU in 32-bit float, so it is exact (no LUT interpolation). The Python twins
in colour.py compute the same numbers; tests compare the two, and the in-app
check compares Resolve's output with them.
"""

import math

from . import colour as C

HEADER = "// NOLGIA {name}\n// {what}\n// Made by NOLGIA for DaVinci Resolve (nolgia.ai). GPL-3.0-or-later.\n"


def f(v):
    text = "%.10g" % v
    if "e" not in text and "." not in text:
        text += ".0"
    return text + "f"


def _matrix_lines(m, src, dst):
    return "\n".join("    float {d} = {a} * {s0} + {b} * {s1} + {c} * {s2};".format(
        d=dst[i], a=f(m[i][0]), b=f(m[i][1]), c=f(m[i][2]), s0=src[0], s1=src[1], s2=src[2]) for i in range(3))


DI_ENCODE = """__DEVICE__ float di_encode(float x)
{{
    return x <= {cut} ? x * {m} : (_log2f(x + {a}) + {b}) * {c};
}}
""".format(cut=f(C.DI_LIN_CUT), m=f(C.DI_M), a=f(C.DI_A), b=f(C.DI_B), c=f(C.DI_C))

DI_DECODE = """__DEVICE__ float di_decode(float y)
{{
    return y <= {cut} ? y / {m} : _exp2f(y / {c} - {b}) - {a};
}}
""".format(cut=f(C.DI_LOG_CUT), m=f(C.DI_M), a=f(C.DI_A), b=f(C.DI_B), c=f(C.DI_C))

L3 = C.LC3
DECODERS = {
    "slog3": """__DEVICE__ float cam_decode(float y)
{{
    return y >= {cut} ? _exp10f((y * 1023.0f - 420.0f) / 261.5f) * 0.19f - 0.01f
                      : (y * 1023.0f - 95.0f) * 0.01125f / {span};
}}
""".format(cut=f(171.2102946929 / 1023.0), span=f(171.2102946929 - 95.0)),
    "logc3": """__DEVICE__ float cam_decode(float y)
{{
    return y > {cut} ? (_exp10f((y - {d}) / {c}) - {b}) / {a} : (y - {ff}) / {e};
}}
""".format(cut=f(L3["e"] * L3["cut"] + L3["f"]), d=f(L3["d"]), c=f(L3["c"]), b=f(L3["b"]), a=f(L3["a"]), ff=f(L3["f"]), e=f(L3["e"])),
    "logc4": """__DEVICE__ float cam_decode(float y)
{{
    return y >= 0.0f ? (_exp2f(14.0f * (y - {c}) / {b} + 6.0f) - 64.0f) / {a} : y * {s} + {t};
}}
""".format(c=f(C.LC4_C), b=f(C.LC4_B), a=f(C.LC4_A), s=f(C.LC4_S), t=f(C.LC4_T)),
    "vlog": """__DEVICE__ float cam_decode(float y)
{
    return y < 0.181f ? (y - 0.125f) / 5.6f : _exp10f((y - 0.598206f) / 0.241514f) - 0.00873f;
}
""",
    "clog2": """__DEVICE__ float cam_decode(float y)
{
    return y < 0.092864125f ? -(_exp10f((0.092864125f - y) / 0.24136077f) - 1.0f) / 87.09937546f
                            : (_exp10f((y - 0.092864125f) / 0.24136077f) - 1.0f) / 87.09937546f;
}
""",
    "clog3": """__DEVICE__ float cam_decode(float y)
{
    if (y < 0.097465473f) return -(_exp10f((0.12783901f - y) / 0.36726845f) - 1.0f) / 14.98325f;
    if (y <= 0.15277891f) return (y - 0.12512219f) / 1.9754798f;
    return (_exp10f((y - 0.12240537f) / 0.36726845f) - 1.0f) / 14.98325f;
}
""",
    "bmdfilm5": """__DEVICE__ float cam_decode(float y)
{{
    return y < {cut} ? (y - {E}) / {D} : _expf((y - {Cc}) / {A}) - {B};
}}
""".format(cut=f(C.BMD5["D"] * C.BMD5["cut"] + C.BMD5["E"]), E=f(C.BMD5["E"]), D=f(C.BMD5["D"]),
           Cc=f(C.BMD5["C"]), A=f(C.BMD5["A"]), B=f(C.BMD5["B"])),
    "applelog": """__DEVICE__ float cam_decode(float y)
{{
    if (y >= {pt}) return _exp2f((y - {delta}) / {gamma}) - {beta};
    if (y >= 0.0f) return _sqrtf(y / {c}) + {r0};
    return {r0};
}}
""".format(pt=f(C.AL_PT), delta=f(C.AL["delta"]), gamma=f(C.AL["gamma"]), beta=f(C.AL["beta"]), c=f(C.AL["c"]), r0=f(C.AL["R0"])),
    "flog": """__DEVICE__ float cam_decode(float y)
{{
    return y < {cut2} ? (y - {f}) / {e} : (_exp10f((y - {d}) / {c}) - {b}) / {a};
}}
""".format(cut2=f(C.FL["cut2"]), f=f(C.FL["f"]), e=f(C.FL["e"]), d=f(C.FL["d"]), c=f(C.FL["c"]), b=f(C.FL["b"]), a=f(C.FL["a"])),
    "flog2": """__DEVICE__ float cam_decode(float y)
{{
    return y < {cut2} ? (y - {f}) / {e} : (_exp10f((y - {d}) / {c}) - {b}) / {a};
}}
""".format(cut2=f(C.FL2["cut2"]), f=f(C.FL2["f"]), e=f(C.FL2["e"]), d=f(C.FL2["d"]), c=f(C.FL2["c"]), b=f(C.FL2["b"]), a=f(C.FL2["a"])),
    "dlog": """__DEVICE__ float cam_decode(float y)
{
    return y <= 0.14f ? (y - 0.0929f) / 6.025f : (_exp10f(3.89616f * y - 2.27752f) - 0.0108f) / 0.9892f;
}
""",
    "nlog": """__DEVICE__ float cam_decode(float y)
{{
    return y < {cut2} ? _powf(y / {a}, 3.0f) - {b} : _expf((y - {d}) / {c});
}}
""".format(cut2=f(C.NL["cut2"]), a=f(C.NL["a"]), b=f(C.NL["b"]), d=f(C.NL["d"]), c=f(C.NL["c"])),
}


def cst_in(camera):
    """CST IN: a camera log to DWG/DI."""
    label, curve, gamut = C.CAMERAS[camera]
    m = C.gamut_matrix(gamut, "dwg")
    return (HEADER.format(name="CST IN " + label, what=label + " to DaVinci Wide Gamut / DaVinci Intermediate, from the maker's published curve and primaries.")
            + "\n" + DECODERS[curve] + "\n" + DI_ENCODE + """
__DEVICE__ float3 transform(int p_Width, int p_Height, int p_X, int p_Y, float p_R, float p_G, float p_B)
{
    float r = cam_decode(p_R);
    float g = cam_decode(p_G);
    float b = cam_decode(p_B);
""" + _matrix_lines(m, ("r", "g", "b"), ("R", "G", "B")) + """
    return make_float3(di_encode(R), di_encode(G), di_encode(B));
}
""")


DISPLAY_FUNCS = """__DEVICE__ float tone(float x)
{{
    if (x <= 0.0f) return 0.0f;
    float p = _powf(x, {g});
    return {m} * p / (p + {k});
}}

__DEVICE__ float tone_inverse(float y)
{{
    if (y <= 0.0f) return 0.0f;
    y = _fminf(y, {m} * 0.999999f);
    return _powf({k} * y / ({m} - y), 1.0f / {g});
}}

__DEVICE__ float gc(float d)
{{
    if (d < {t}) return d;
    float s = (d - {t}) / {sc};
    return {t} + {sc} * s / _powf(1.0f + _powf(s, {p}), 1.0f / {p});
}}

__DEVICE__ float gc_inverse(float d)
{{
    if (d < {t}) return d;
    float s = (d - {t}) / {sc};
    float q = _powf(s, {p});
    if (q >= 1.0f) return {lim} * 4.0f;
    return {t} + {sc} * s / _powf(1.0f - q, 1.0f / {p});
}}
""".format(g=f(C.TONE_G), m=f(C.TONE_M), k=f(C.TONE_K), t=f(C.GC_THRESH), sc=f(C._GC_SCALE), p=f(C.GC_POWER), lim=f(C.GC_LIMIT))


def cst_out():
    return (HEADER.format(name="CST OUT DWG to Rec.709", what="DaVinci Wide Gamut / Intermediate to Rec.709 gamma 2.4: soft gamut compression, a filmic tone curve (mid grey to 0.399), exactly invertible.")
            + "\n" + DI_DECODE + "\n" + DISPLAY_FUNCS + """
__DEVICE__ float3 transform(int p_Width, int p_Height, int p_X, int p_Y, float p_R, float p_G, float p_B)
{
    float r = di_decode(p_R);
    float g = di_decode(p_G);
    float b = di_decode(p_B);
""" + _matrix_lines(C.DWG_TO_709, ("r", "g", "b"), ("R", "G", "B")) + """
    float a = _fmaxf(R, _fmaxf(G, B));
    if (a <= 0.0f) return make_float3(0.0f, 0.0f, 0.0f);
    R = a - gc((a - R) / a) * a;
    G = a - gc((a - G) / a) * a;
    B = a - gc((a - B) / a) * a;
    return make_float3(_fminf(1.0f, _powf(tone(R), 1.0f / 2.4f)), _fminf(1.0f, _powf(tone(G), 1.0f / 2.4f)),
                       _fminf(1.0f, _powf(tone(B), 1.0f / 2.4f)));
}
""")


def cst_in_rec709():
    return (HEADER.format(name="CST IN Rec.709", what="Rec.709 gamma 2.4 to DaVinci Wide Gamut / Intermediate: the exact inverse of NOLGIA CST OUT, so an untouched grade gives the clip back unchanged.")
            + "\n" + DI_ENCODE + "\n" + DISPLAY_FUNCS + """
__DEVICE__ float3 transform(int p_Width, int p_Height, int p_X, int p_Y, float p_R, float p_G, float p_B)
{{
    float R = tone_inverse(_powf(_clampf(p_R, 0.0f, {mx}), 2.4f));
    float G = tone_inverse(_powf(_clampf(p_G, 0.0f, {mx}), 2.4f));
    float B = tone_inverse(_powf(_clampf(p_B, 0.0f, {mx}), 2.4f));
    float a = _fmaxf(R, _fmaxf(G, B));
    if (a > 0.0f) {{
        R = a - gc_inverse((a - R) / a) * a;
        G = a - gc_inverse((a - G) / a) * a;
        B = a - gc_inverse((a - B) / a) * a;
    }}
""".format(mx=f(C.DISPLAY_MAX)) + _matrix_lines(C.REC709_TO_DWG, ("R", "G", "B"), ("r", "g", "b")) + """
    return make_float3(di_encode(r), di_encode(g), di_encode(b));
}
""")


# ---------------------------------------------------------------- SKIN

def dwg_luma_weights():
    m = C.npm(*C.GAMUTS["dwg"])
    return m[1]


def skin_angle():
    """Hue angle of skin in DI's (B - Y, R - Y) plane, from the ColorChecker light and dark skin patches."""
    angles = []
    for srgb in ((115, 82, 68), (194, 150, 130)):
        lin = [((c / 255.0 + 0.055) / 1.055) ** 2.4 for c in srgb]
        di = [C.di_encode(c * 0.18 / 0.2) for c in C.mat_vec(C.REC709_TO_DWG, lin)]
        w = dwg_luma_weights()
        y = sum(a * b for a, b in zip(w, di))
        angles.append(math.atan2(di[0] - y, di[2] - y))
    return sum(angles) / len(angles)


def skin():
    w = dwg_luma_weights()
    return (HEADER.format(name="SKIN", what="Pulls skin hues onto the skin line and evens red and green blotches, in DaVinci Wide Gamut / Intermediate. Only skin hues move. Turn it down with the node's Key Output Gain.")
            + """DEFINE_UI_PARAMS(pull, Hue Pull, DCTLUI_SLIDER_FLOAT, 0.45, 0.0, 1.0, 0.01)
DEFINE_UI_PARAMS(warmth, Warmth, DCTLUI_SLIDER_FLOAT, 0.004, 0.0, 0.03, 0.001)
DEFINE_UI_PARAMS(skinang, Skin Line, DCTLUI_SLIDER_FLOAT, {ang}, {lo}, {hi}, 0.01)
DEFINE_UI_PARAMS(width, Hue Width, DCTLUI_SLIDER_FLOAT, 0.40, 0.15, 0.9, 0.01)

__DEVICE__ float3 transform(int p_Width, int p_Height, int p_X, int p_Y, float p_R, float p_G, float p_B)
{{
    float Y = {wr} * p_R + {wg} * p_G + {wb} * p_B;
    float u = p_B - Y;
    float v = p_R - Y;
    float c = _sqrtf(u * u + v * v);
    float h = _atan2f(v, u);
    float dh = h - skinang;
    if (dh > 3.14159265f) dh -= 6.2831853f;
    if (dh < -3.14159265f) dh += 6.2831853f;
    float wh = _expf(-(dh * dh) / (width * width));
    float wc = _clampf((c - 0.006f) / 0.025f, 0.0f, 1.0f) * _clampf((0.22f - c) / 0.08f, 0.0f, 1.0f);
    float wy = _clampf((Y - 0.14f) / 0.08f, 0.0f, 1.0f) * _clampf((0.80f - Y) / 0.1f, 0.0f, 1.0f);
    float k = wh * wc * wy;
    float nh = h - dh * pull * k;
    float nc = c * (1.0f + 0.03f * k);
    float r = Y + nc * _sinf(nh) + warmth * k;
    float b = Y + nc * _cosf(nh) - warmth * 0.8f * k;
    float g = (Y - {wr} * r - {wb} * b) / {wg};
    return make_float3(r, g, b);
}}
""".format(ang="%.3f" % skin_angle(), lo="%.2f" % (skin_angle() - 0.4), hi="%.2f" % (skin_angle() + 0.4),
           wr=f(w[0]), wg=f(w[1]), wb=f(w[2])))


def skin_python(rgb, pull=0.45, warmth=0.004, width=0.40):
    """The SKIN DCTL in Python, for previews and tests."""
    wr, wg, wb = dwg_luma_weights()
    ang = round(skin_angle(), 3)
    r0, g0, b0 = rgb
    y = wr * r0 + wg * g0 + wb * b0
    u, v = b0 - y, r0 - y
    c = math.hypot(u, v)
    h = math.atan2(v, u)
    dh = h - ang
    if dh > math.pi:
        dh -= 2 * math.pi
    if dh < -math.pi:
        dh += 2 * math.pi
    clamp = lambda x: max(0.0, min(1.0, x))
    k = math.exp(-(dh * dh) / (width * width)) * clamp((c - 0.006) / 0.025) * clamp((0.22 - c) / 0.08) * clamp((y - 0.14) / 0.08) * clamp((0.80 - y) / 0.1)
    nh = h - dh * pull * k
    nc = c * (1 + 0.03 * k)
    r = y + nc * math.sin(nh) + warmth * k
    b = y + nc * math.cos(nh) - warmth * 0.8 * k
    g = (y - wr * r - wb * b) / wg
    return [r, g, b]


def all_files():
    """{relative file name: DCTL text} for the NOLGIA Grade folder in Resolve's LUT folder."""
    out = {"CST IN/%s.dctl" % C.CAMERAS[cam][0].replace("/", "-"): cst_in(cam) for cam in C.CAMERAS}
    out["CST IN/Rec.709 (NOLGIA display).dctl"] = cst_in_rec709()
    out["CST OUT/DWG to Rec.709 (NOLGIA display).dctl"] = cst_out()
    out["Corrections/Skin.dctl"] = skin()
    return out


# ---------------------------------------------------------------- someone's own LUT, at a strength

ENCODERS = {
    "slog3": """__DEVICE__ float lut_encode(float x)
{{
    return x >= 0.01125f ? (420.0f + _log10f((x + 0.01f) / 0.19f) * 261.5f) / 1023.0f
                         : (x * {span} / 0.01125f + 95.0f) / 1023.0f;
}}
""".format(span=f(171.2102946929 - 95.0)),
    "logc3": """__DEVICE__ float lut_encode(float x)
{{
    return x > {cut} ? {c} * _log10f({a} * x + {b}) + {d} : {e} * x + {ff};
}}
""".format(cut=f(L3["cut"]), c=f(L3["c"]), a=f(L3["a"]), b=f(L3["b"]), d=f(L3["d"]), e=f(L3["e"]), ff=f(L3["f"])),
    "logc4": """__DEVICE__ float lut_encode(float x)
{{
    return x >= {t} ? (_log2f({a} * x + 64.0f) - 6.0f) / 14.0f * {b} + {c} : (x - {t}) / {s};
}}
""".format(t=f(C.LC4_T), a=f(C.LC4_A), b=f(C.LC4_B), c=f(C.LC4_C), s=f(C.LC4_S)),
    "vlog": """__DEVICE__ float lut_encode(float x)
{
    return x < 0.01f ? 5.6f * x + 0.125f : 0.241514f * _log10f(x + 0.00873f) + 0.598206f;
}
""",
    "clog2": """__DEVICE__ float lut_encode(float x)
{
    return x < 0.0f ? -0.24136077f * _log10f(-x * 87.09937546f + 1.0f) + 0.092864125f
                    : 0.24136077f * _log10f(x * 87.09937546f + 1.0f) + 0.092864125f;
}
""",
    "clog3": """__DEVICE__ float lut_encode(float x)
{
    if (x < -0.014f) return -0.36726845f * _log10f(-x * 14.98325f + 1.0f) + 0.12783901f;
    if (x <= 0.014f) return 1.9754798f * x + 0.12512219f;
    return 0.36726845f * _log10f(x * 14.98325f + 1.0f) + 0.12240537f;
}
""",
    "bmdfilm5": """__DEVICE__ float lut_encode(float x)
{{
    return x < {cut} ? {D} * x + {E} : {A} * _logf(x + {B}) + {Cc};
}}
""".format(cut=f(C.BMD5["cut"]), D=f(C.BMD5["D"]), E=f(C.BMD5["E"]), A=f(C.BMD5["A"]), B=f(C.BMD5["B"]),
           Cc=f(C.BMD5["C"])),
    "applelog": """__DEVICE__ float lut_encode(float x)
{{
    if (x >= {rt}) return {gamma} * _log2f(x + {beta}) + {delta};
    if (x >= {r0}) return {c} * (x - {r0}) * (x - {r0});
    return 0.0f;
}}
""".format(rt=f(C.AL["Rt"]), gamma=f(C.AL["gamma"]), beta=f(C.AL["beta"]), delta=f(C.AL["delta"]), r0=f(C.AL["R0"]),
           c=f(C.AL["c"])),
    "flog": """__DEVICE__ float lut_encode(float x)
{{
    return x < {cut1} ? {e} * x + {f} : {c} * _log10f({a} * x + {b}) + {d};
}}
""".format(cut1=f(C.FL["cut1"]), e=f(C.FL["e"]), f=f(C.FL["f"]), c=f(C.FL["c"]), a=f(C.FL["a"]), b=f(C.FL["b"]), d=f(C.FL["d"])),
    "flog2": """__DEVICE__ float lut_encode(float x)
{{
    return x < {cut1} ? {e} * x + {f} : {c} * _log10f({a} * x + {b}) + {d};
}}
""".format(cut1=f(C.FL2["cut1"]), e=f(C.FL2["e"]), f=f(C.FL2["f"]), c=f(C.FL2["c"]), a=f(C.FL2["a"]), b=f(C.FL2["b"]), d=f(C.FL2["d"])),
    "dlog": """__DEVICE__ float lut_encode(float x)
{
    return x <= 0.0078f ? 6.025f * x + 0.0929f : _log10f(x * 0.9892f + 0.0108f) * 0.256663f + 0.584555f;
}
""",
    "nlog": """__DEVICE__ float lut_encode(float x)
{{
    if (x < {cut1}) {{ float v = x + {b}; return {a} * (v >= 0.0f ? _powf(v, 1.0f / 3.0f) : -_powf(-v, 1.0f / 3.0f)); }}
    return {c} * _logf(x) + {d};
}}
""".format(cut1=f(C.NL["cut1"]), b=f(C.NL["b"]), a=f(C.NL["a"]), c=f(C.NL["c"]), d=f(C.NL["d"])),
    "cineon": """__DEVICE__ float lut_encode(float x)
{{
    return (685.0f + 300.0f * _log10f(_fmaxf(x * {k} + {off}, 1e-10f))) / 1023.0f;
}}
""".format(k=f(1.0 - C.CINEON_OFFSET), off=f(C.CINEON_OFFSET)),
    "acescct": """__DEVICE__ float lut_encode(float x)
{{
    return x <= 0.0078125f ? {a} * x + {b} : (_log2f(x) + 9.72f) / 17.52f;
}}

__DEVICE__ float lut_decode(float y)
{{
    return y <= 0.155251141552511f ? (y - {b}) / {a} : _exp2f(y * 17.52f - 9.72f);
}}
""".format(a=f(C.ACC_A), b=f(C.ACC_B)),
}

LUT_GAMUT = {"cineon": "rec709", "slog3": "sgamut3cine", "logc3": "awg3", "logc4": "awg4", "vlog": "vgamut",
             "clog2": "cinemagamut", "clog3": "cinemagamut", "bmdfilm5": "bmdwg5", "applelog": "rec2020",
             "acescct": "ap1", "flog": "rec2020", "flog2": "rec2020", "flog2c": "fgamutc", "dlog": "dgamut",
             "nlog": "rec2020"}


def lut_at_strength(lut_path, space, strength, name):
    """A DCTL that puts someone's own LUT on the LOOK node at a strength.

    space is what the LUT takes in: dwg (a look in the working space), acescct, rec709 (a Rec.709 look on
    NOLGIA's display rendering), cineon or a camera log (a print or camera LUT that ends on a Rec.709
    display). A LUT that ends on a display is taken back through the exact inverse of CST OUT, so LOOK then
    CST OUT shows it as it was made, and the strength mixes it in the working space."""
    pct = int(round(strength * 100))
    path = lut_path.replace("\\", "/")
    head = HEADER.format(name="%s %d%%" % (name, pct),
                         what="The LUT at %s, at %d%%: it takes DaVinci Wide Gamut / Intermediate to the LUT's input "
                              "(%s) and back. NOLGIA points at your own file and never copies it." % (path, pct, space))
    parts = [head, "DEFINE_LUT(lut, %s)\n" % path,
             "DEFINE_UI_PARAMS(strength, Strength, DCTLUI_SLIDER_FLOAT, %.2f, 0.0, 1.0, 0.01)\n\n" % strength]
    if space == "dwg":
        body = "    float3 o = APPLY_LUT(r, g, b, lut);\n"
    elif space == "acescct":
        parts += [DI_DECODE, DI_ENCODE, ENCODERS["acescct"]]
        m_in, m_out = C.gamut_matrix("dwg", "ap1"), C.gamut_matrix("ap1", "dwg")
        body = ("    float lr = di_decode(r), lg = di_decode(g), lb = di_decode(b);\n"
                + _matrix_lines(m_in, ("lr", "lg", "lb"), ("ar", "ag", "ab")) + "\n"
                + "    float3 c = APPLY_LUT(lut_encode(ar), lut_encode(ag), lut_encode(ab), lut);\n"
                + "    float xr = lut_decode(c.x), xg = lut_decode(c.y), xb = lut_decode(c.z);\n"
                + _matrix_lines(m_out, ("xr", "xg", "xb"), ("qr", "qg", "qb")) + "\n"
                + "    float3 o = make_float3(di_encode(qr), di_encode(qg), di_encode(qb));\n")
    else:
        parts += [DI_DECODE, DI_ENCODE, DISPLAY_FUNCS]
        back = ("    float R = tone_inverse(_powf(_clampf(c.x, 0.0f, {mx}), 2.4f));\n"
                "    float G = tone_inverse(_powf(_clampf(c.y, 0.0f, {mx}), 2.4f));\n"
                "    float B = tone_inverse(_powf(_clampf(c.z, 0.0f, {mx}), 2.4f));\n"
                "    float a2 = _fmaxf(R, _fmaxf(G, B));\n"
                "    if (a2 > 0.0f) {{\n"
                "        R = a2 - gc_inverse((a2 - R) / a2) * a2;\n"
                "        G = a2 - gc_inverse((a2 - G) / a2) * a2;\n"
                "        B = a2 - gc_inverse((a2 - B) / a2) * a2;\n"
                "    }}\n").format(mx=f(C.DISPLAY_MAX)) + _matrix_lines(C.REC709_TO_DWG, ("R", "G", "B"), ("qr", "qg", "qb")) + \
               "\n    float3 o = make_float3(di_encode(qr), di_encode(qg), di_encode(qb));\n"
        lin = "    float lr = di_decode(r), lg = di_decode(g), lb = di_decode(b);\n"
        if space == "rec709":
            to = (lin + _matrix_lines(C.DWG_TO_709, ("lr", "lg", "lb"), ("vr", "vg", "vb")) + "\n"
                  + "    float a = _fmaxf(vr, _fmaxf(vg, vb));\n"
                  + "    if (a > 0.0f) { vr = a - gc((a - vr) / a) * a; vg = a - gc((a - vg) / a) * a; vb = a - gc((a - vb) / a) * a; }\n"
                  + "    float3 c = APPLY_LUT(_fminf(1.0f, _powf(tone(vr), 1.0f / 2.4f)), _fminf(1.0f, _powf(tone(vg), 1.0f / 2.4f)),\n"
                  + "                         _fminf(1.0f, _powf(tone(vb), 1.0f / 2.4f)), lut);\n")
        else:
            parts.append(ENCODERS["flog2" if space == "flog2c" else space])
            m = C.gamut_matrix("dwg", LUT_GAMUT[space])
            compress = ""
            if space == "cineon":
                compress = ("    float a = _fmaxf(vr, _fmaxf(vg, vb));\n"
                            "    if (a > 0.0f) { vr = a - gc((a - vr) / a) * a; vg = a - gc((a - vg) / a) * a; vb = a - gc((a - vb) / a) * a; }\n"
                            "    vr = _fmaxf(vr, 0.0f); vg = _fmaxf(vg, 0.0f); vb = _fmaxf(vb, 0.0f);\n")
            to = (lin + _matrix_lines(m, ("lr", "lg", "lb"), ("vr", "vg", "vb")) + "\n" + compress
                  + "    float3 c = APPLY_LUT(lut_encode(vr), lut_encode(vg), lut_encode(vb), lut);\n")
        body = to + back
    parts.append("""
__DEVICE__ float3 transform(int p_Width, int p_Height, int p_X, int p_Y, float p_R, float p_G, float p_B)
{
    float r = p_R, g = p_G, b = p_B;
""" + body + """    return make_float3(r + (o.x - r) * strength, g + (o.y - g) * strength, b + (o.z - b) * strength);
}
""")
    return "\n".join(parts)
