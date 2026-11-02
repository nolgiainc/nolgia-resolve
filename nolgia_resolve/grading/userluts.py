# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 NOLGIA Inc
"""The LUTs already on the person's computer, in the same library and on the same strength dial.

Two groups, each LUT under its own file name:
  * "Installed with DaVinci Resolve": the LUTs Resolve ships in its own LUT folder (Film Looks such as
    "Rec709 Kodak 2383 D65", and the camera makers' LUTs Resolve includes);
  * "Your LUTs": every other .cube in Resolve's LUT folders and in folders the person adds (paid film
    emulation packs, camera makers' official LUTs, their own).
NOLGIA never copies, converts, uploads or ships any of them. A LUT goes on the LOOK node through a small
DCTL NOLGIA writes for it, which points at the person's own file with DEFINE_LUT, takes the working space
(DaVinci Wide Gamut / Intermediate) to what the LUT expects, and mixes the result at the chosen strength.
"""

import hashlib
import json
import os
import re

from .. import paths

SHIPPED = ("Film Looks", "Arri", "Astrodesign", "Blackmagic Design", "Canon", "DJI", "Leica", "Olympus",
           "Panasonic", "RED", "Samsung", "Sony", "Fujifilm", "Nikon", "Apple", "GoPro")
SKIP = ("NOLGIA", "NOLGIA Grade", "VFX IO", "HDR ST 2084", "HDR Hybrid Log-Gamma", "ACES", "DCI",
        "ACES Transforms")
INPUTS = ("auto", "cineon", "rec709", "dwg", "acescct", "slog3", "logc3", "logc4", "vlog", "clog2", "clog3",
          "bmdfilm5", "applelog", "flog", "flog2", "dlog", "nlog")
INSTALLED = "Installed with DaVinci Resolve"
YOURS = "Your LUTs"
MAX_FILES = 3000
HEADER_BYTES = 2048

# Camera makers' own free LUTs a person can download themselves: NOLGIA only links to the maker's page.
# Filled from each maker's own page and terms (research/OFFICIAL-LUTS.md).
OFFICIAL = []


def _settings_path():
    return os.path.join(paths.config_dir(), "lut-folders.json")


def extra_folders():
    """The folders the person added (NOLGIA Grade window, or an agent's `folders`)."""
    try:
        with open(_settings_path(), encoding="utf-8") as handle:
            data = json.load(handle)
        return [f for f in data.get("folders", []) if isinstance(f, str)]
    except (OSError, ValueError):
        return []


def save_folders(folders):
    os.makedirs(os.path.dirname(_settings_path()), exist_ok=True)
    unique = []
    for f in folders:
        if f and f not in unique:
            unique.append(f)
    with open(_settings_path(), "w", encoding="utf-8") as handle:
        json.dump({"folders": unique}, handle, indent=1)
    return unique


def _header(path):
    try:
        with open(path, "rb") as handle:
            head = handle.read(HEADER_BYTES).decode("utf-8", "replace")
    except OSError:
        return "", False
    lines = [l.strip() for l in head.splitlines()]
    text = "\n".join(l for l in lines if l and not re.match(r"^[-0-9.eE+\s]+$", l))
    is3d = "LUT_3D_SIZE" in head
    return text, is3d


KEYS = (
    ("cineon", (r"input:\s*cineon", r"cineon")),
    ("logc4", (r"logc4", r"log\s?c4", r"awg4")),
    ("logc3", (r"logc3", r"log\s?c3", r"logc", r"log-c", r"alexa")),
    ("slog3", (r"s-?log3",)),
    ("vlog", (r"v-?log",)),
    ("clog3", (r"c-?log\s?3", r"canon\s?log\s?3", r"canonlog3")),
    ("clog2", (r"c-?log\s?2", r"canon\s?log\s?2", r"canonlog2")),
    ("bmdfilm5", (r"gen\s?5 film", r"film gen\s?5", r"gen5", r"generation 5")),
    ("applelog", (r"apple\s?log",)),
    ("flog2", (r"f-?log\s?2(?!\s?c)", r"flog2(?!c)")),
    ("unsupported", (r"f-?log\s?2\s?c", r"flog2c", r"d-?log\s?m", r"dlogm", r"log3g10", r"redlog", r"\bhlg\b",
                     r"\bpq\b", r"st\s?2084", r"blackmagic .*film", r"\bbmd .*film", r"\bfilm to (video|rec|extended)")),
    ("flog", (r"f-?log",)),
    ("dlog", (r"d-?log",)),
    ("nlog", (r"n-?log",)),
    ("acescct", (r"acescct",)),
    ("dwg", (r"davinci\s?wide\s?gamut", r"dwg", r"davinci intermediate")),
)


def detect_input(name, header):
    """What a LUT takes in, from its header, title and file name: (input, how)."""
    m = re.search(r"input\s*:\s*([^\n]+)", header, re.I)
    sources = []
    if m:
        sources.append(("its header", m.group(1)))
    for key in ("TITLE", "BMD_TITLE"):
        t = re.search(key + r'\s+"?([^"\n]+)', header)
        if t:
            sources.append(("its title", t.group(1)))
    sources.append(("its file name", name))
    sources.append(("its comments", header))
    for how, text in sources:
        low = text.lower()
        # "X to Y": only the part before " to " names the input
        part = re.split(r"\s(?:to|2)\s|to(?=[A-Z0-9])", text, maxsplit=1)[0].lower() if how != "its comments" else low
        for space, patterns in KEYS:
            if any(re.search(p, part) for p in patterns):
                return space, how
        if how != "its comments" and re.search(r"rec\.?\s?709|bt\.?709|\b709\b", part):
            return "rec709", how
    return "rec709", "assumed: nothing in the LUT names a log input, so it is taken as a Rec.709 look"


def _display_output(header, name):
    low = (header + " " + name).lower()
    if "dci-p3" in low or "p3" in name.lower().split()[0:1]:
        return "dci-p3"
    return "rec709"


def _entry(path, root, group):
    name = os.path.splitext(os.path.basename(path))[0]
    header, is3d = _header(path)
    if not is3d:
        return None
    space, how = detect_input(name, header)
    rel = os.path.relpath(path, root).replace("\\", "/") if root else path
    entry = {"id": "lut-" + hashlib.sha1(path.encode("utf-8")).hexdigest()[:10], "name": name, "group": group,
             "folder": os.path.dirname(rel) if root else os.path.dirname(path), "path": path, "input": space,
             "input_found_by": how, "output": "dwg" if space == "dwg" else ("acescct" if space == "acescct" else
                                                                             _display_output(header, name))}
    if group == INSTALLED and name.lower().startswith("dci-p3"):
        entry["output"] = "dci-p3"
    if space == "unsupported":
        entry["input"] = "unsupported"
        entry["note"] = ("takes a log NOLGIA cannot convert to yet (an older Blackmagic Film, F-Log2 C, D-Log M, RED "
                         "Log3G10 or an HDR signal); give `lut_input` if you know it is one NOLGIA has")
    if entry["output"] == "dci-p3":
        entry["note"] = "made for a DCI-P3 display; NOLGIA's grade ends in Rec.709, so use the Rec709 version"
    return entry


def _walk(folder, out, root, group_for, limit):
    for base, dirs, files in os.walk(folder):
        rel_top = os.path.relpath(base, root).replace("\\", "/").split("/")[0] if root else ""
        if root and rel_top in SKIP:
            dirs[:] = []
            continue
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and not (root and base == root and d in SKIP))
        for f in sorted(files):
            if not f.lower().endswith(".cube"):
                continue
            if len(out) >= limit:
                return
            path = os.path.join(base, f)
            entry = _entry(path, root, group_for(rel_top))
            if entry:
                out.append(entry)


def index(lut_dirs=None, folders=None, limit=MAX_FILES):
    """Every 3D .cube LUT in Resolve's LUT folders and the added folders, grouped."""
    out = []
    seen = set()
    for root in (lut_dirs if lut_dirs is not None else paths.lut_dirs()):
        if not os.path.isdir(root) or root in seen:
            continue
        seen.add(root)
        _walk(root, out, root, lambda top: INSTALLED if top in SHIPPED else YOURS, limit)
    for folder in (folders if folders is not None else extra_folders()):
        if os.path.isdir(folder) and folder not in seen:
            seen.add(folder)
            _walk(folder, out, None, lambda top: YOURS, limit)
    out.sort(key=lambda e: (e["group"] != INSTALLED, e["folder"].lower(), e["name"].lower()))
    return out


def find(key, entries):
    """A LUT by id, name, relative or absolute path."""
    if not key:
        return None
    k = key.strip()
    for e in entries:
        if k in (e["id"], e["name"], e["path"]) or k.replace("\\", "/") == (e["folder"] + "/" + e["name"]).lstrip("/") \
                or k.replace("\\", "/").lower().endswith((e["folder"] + "/" + e["name"] + ".cube").lower()):
            return e
    if os.path.isfile(k) and k.lower().endswith(".cube"):
        return _entry(k, None, YOURS)
    return None


# ---------------------------------------------------------------- the same in Python (tests, previews)

def read_cube(path):
    """(size, [(r, g, b)] in red-fastest order, domain min, domain max) of a 3D .cube."""
    size, data, lo, hi = None, [], [0.0, 0.0, 0.0], [1.0, 1.0, 1.0]
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if s.startswith("LUT_3D_SIZE"):
                size = int(s.split()[1])
            elif s.startswith("DOMAIN_MIN"):
                lo = [float(v) for v in s.split()[1:4]]
            elif s.startswith("DOMAIN_MAX"):
                hi = [float(v) for v in s.split()[1:4]]
            elif s.startswith("LUT_3D_INPUT_RANGE"):
                a, b = (float(v) for v in s.split()[1:3])
                lo, hi = [a] * 3, [b] * 3
            elif s[0].isdigit() or s[0] in "-.":
                parts = s.split()
                if len(parts) == 3:
                    data.append(tuple(float(v) for v in parts))
    return size, data, lo, hi


def cube_function(path):
    size, data, lo, hi = read_cube(path)
    n = size - 1

    def at(i, j, k):
        return data[i + j * size + k * size * size]

    def f(rgb):
        idx = [max(0.0, min(1.0, (c - lo[d]) / (hi[d] - lo[d]))) * n for d, c in enumerate(rgb)]
        i0 = [min(int(x), n - 1) for x in idx]
        t = [x - i for x, i in zip(idx, i0)]
        out = [0.0, 0.0, 0.0]
        for dz in (0, 1):
            for dy in (0, 1):
                for dx in (0, 1):
                    w = (t[0] if dx else 1 - t[0]) * (t[1] if dy else 1 - t[1]) * (t[2] if dz else 1 - t[2])
                    if w:
                        v = at(i0[0] + dx, i0[1] + dy, i0[2] + dz)
                        out = [o + w * c for o, c in zip(out, v)]
        return out
    return f


def wrap_python(lut, space, strength):
    """The strength DCTL in Python: DWG/DI in and out (lut: a function on the LUT's own input)."""
    from . import colour as C
    enc = {"cineon": C.cineon_encode}
    gam = {"cineon": "rec709"}
    for cam, (_label, curve, gamut) in C.CAMERAS.items():
        enc[cam] = C.CURVES[curve][1]
        gam[cam] = gamut

    def f(rgb):
        if space == "dwg":
            o = lut(rgb)
        elif space == "acescct":
            o = C.acescct_to_dwg(lut(C.dwg_to_acescct(rgb)))
        else:
            if space == "rec709":
                code = C.dwg_to_display(rgb)
            else:
                lin = C.mat_vec(C.gamut_matrix("dwg", gam[space]), [C.di_decode(c) for c in rgb])
                if space == "cineon":
                    lin = [max(v, 0.0) for v in C.gamut_compress(lin)]
                code = [enc[space](v) for v in lin]
            o = C.display_to_dwg(lut(code))
        return [a + (b - a) * strength for a, b in zip(rgb, o)]
    return f
