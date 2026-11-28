# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 NOLGIA Inc
"""The grading package: colour science against Resolve's own colour management,
the DCTLs, the DRX writer, the look library, measuring, and the grade command
run against a stand-in Resolve that reads the DRX it is given."""

import json
import math
import os
import re
import shutil
import struct
import tempfile
import unittest

import support  # noqa: F401  (puts the plugin on sys.path)

from nolgia_resolve.core.commands import CommandError, validate
from nolgia_resolve.grading import colour as C
from nolgia_resolve.grading import dctl, drx, engine, library, looks, measure

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


class ColourTest(unittest.TestCase):
    def test_matches_resolves_own_colour_management(self):
        """Resolve Studio 21.1.1's own input transforms, read on a chart (fixture), within 0.0015 DI."""
        with open(os.path.join(FIXTURES, "resolve_cm_21_1_1.json"), encoding="utf-8") as handle:
            fixture = json.load(handle)
        for camera, data in fixture["cameras"].items():
            f = C.acescct_to_dwg if camera == "acescct" else C.camera_to_dwg(camera)
            worst = max(abs(a - b) for p in data["patches"] for a, b in zip(f(p["in"]), p["resolve_di"]))
            self.assertLess(worst, 0.0016, camera)

    def test_curves_round_trip(self):
        for name, (decode, encode) in C.CURVES.items():
            for x in (0.0005, 0.01, 0.18, 1.0, 12.0):
                self.assertAlmostEqual(decode(encode(x)), x, delta=x * 1e-5 + 1e-7, msg=name)

    def test_working_space_grey(self):
        self.assertAlmostEqual(C.di_encode(0.18), 0.336, places=3)
        self.assertAlmostEqual(C.DI_STOP, 0.07329, places=5)

    def test_display_is_exactly_invertible(self):
        for r in (0.0, 0.1, 0.35, 0.6, 0.95):
            for g in (0.05, 0.5, 0.9):
                for b in (0.2, 0.7):
                    back = C.dwg_to_display(C.display_to_dwg([r, g, b]))
                    self.assertLess(max(abs(x - y) for x, y in zip(back, [r, g, b])), 2e-4)

    def test_display_grey(self):
        self.assertAlmostEqual(C.dwg_to_display([C.di_encode(0.18)] * 3)[1], 0.11 ** (1 / 2.4), places=4)


class DctlTest(unittest.TestCase):
    def test_every_file(self):
        files = dctl.all_files()
        self.assertEqual(len(files), len(C.CAMERAS) + 3)
        for name, text in files.items():
            self.assertIn("__DEVICE__ float3 transform(", text, name)
            self.assertEqual(text.count("{"), text.count("}"), name)

    def test_cst_in_carries_the_gamut_matrix(self):
        text = dctl.cst_in("slog3")
        for row in C.gamut_matrix("sgamut3cine", "dwg"):
            for value in row:
                self.assertIn(dctl.f(value), text)

    def test_skin_moves_only_skin(self):
        grey = [0.4, 0.4, 0.4]
        self.assertEqual([round(v, 6) for v in dctl.skin_python(grey)], grey)
        blue = C.display_to_dwg([0.2, 0.3, 0.7])
        self.assertLess(max(abs(a - b) for a, b in zip(dctl.skin_python(blue), blue)), 1e-6)


def decode_drx(text):
    """[(label, {param id: value})] from a .drx this plugin wrote."""
    body = re.search(r"<pClipFullVer>.*?<Body>([0-9a-f]+)</Body>", text, re.S).group(1)
    raw = bytes.fromhex(body)
    assert raw[0] == 0x81
    message = drx.unstored(raw[1:])
    graph = dict((f, v) for f, w, v in drx.decode(message) if f == 1)[1]
    nodes = []
    for field, wire, value in drx.decode(graph):
        if field != 7:
            continue
        node = dict((f, v) for f, w, v in drx.decode(value))
        label = node.get(6, b"").decode("utf-8")
        params = {}
        g1 = [v for f, w, v in drx.decode(node[9]) if f == 1][0]
        g6 = [v for f, w, v in drx.decode(g1) if f == 6][0]
        g2 = [v for f, w, v in drx.decode(g6) if f == 2][0]
        for f, w, v in drx.decode(g2):
            if f != 3:
                continue
            p = dict((ff, vv) for ff, ww, vv in drx.decode(v))
            inner = drx.decode(p[2])[0]
            if inner[0] == 1:
                params[p[1]] = struct.unpack("<f", inner[2])[0]
            elif inner[0] == 2:
                params[p[1]] = inner[2]
            else:
                params[p[1]] = inner[2].decode("utf-8")
        nodes.append((label, params))
    return nodes


class DrxTest(unittest.TestCase):
    def test_stored_frame_round_trip(self):
        for size in (0, 10, 200 * 1024):
            data = bytes(range(256)) * (size // 256) + bytes(size % 256)
            self.assertEqual(drx.unstored(drx.zstd_stored(data)), data)

    def test_tree_reads_back(self):
        nodes = [drx.Node("CST IN", lut="NOLGIA Grade/CST IN/x.dctl"), drx.Node("BALANCE", offset=[0.1, 0, -0.2]),
                 drx.Node("CONTRAST", contrast=1.2, pivot=0.336), drx.Node("SAT", saturation=0.9),
                 drx.Node("LOOK", lut="NOLGIA Grade/Looks/Print/Warm Print 60%.dctl")]
        out = decode_drx(drx.drx_text(nodes))
        self.assertEqual([label for label, _ in out], ["CST IN", "BALANCE", "CONTRAST", "SAT", "LOOK"])
        self.assertEqual(out[0][1][drx.LUT_PATH], "NOLGIA Grade\\CST IN\\x.dctl")
        self.assertEqual(out[0][1][drx.LUT_KIND], 8)
        self.assertAlmostEqual(out[1][1][drx.OFFSET[0]], 0.1, places=6)
        self.assertNotIn(drx.OFFSET[1], out[1][1])  # zero is not written
        self.assertAlmostEqual(out[2][1][drx.CONTRAST], 1.2, places=6)
        self.assertAlmostEqual(out[3][1][drx.SATURATION], 0.9, places=6)

    def test_xml_shape(self):
        text = drx.drx_text([drx.Node("A")], 3840, 2160, "a & b")
        self.assertTrue(text.startswith('<?xml version="1.0" encoding="UTF-8"?>'))
        self.assertIn("<Width>3840</Width>", text)
        self.assertIn("a &amp; b", text)
        self.assertEqual(text.count("<Body>81"), 2)


TRADEMARKS = ("kodak", "fuji", "fujifilm", "ilford", "agfa", "cinestill", "portra", "ektar", "vision3", "eterna", "velvia",
              "provia", "superia", "reala", "tri-x", "hp5", "arri", "alexa", "kodachrome", "2383", "3513", "k1s1")


class LooksTest(unittest.TestCase):
    def test_maker_names_are_marked(self):
        """A look named after a stock or camera says it is NOLGIA's emulation of it, and the library carries the
        non-affiliation line (the industry's style, e.g. a stock's name followed by the plugin's)."""
        self.assertIn("not affiliated with or endorsed by", looks.NON_AFFILIATION)
        for look in looks.LOOKS:
            named = [w for w in TRADEMARKS
                     if re.search(r"(?<![a-z0-9])%s(?![a-z0-9])" % re.escape(w), look.name.lower())]
            if named:
                self.assertTrue(look.name.endswith("(NOLGIA)"), look.name)
                self.assertTrue(look.emulates, look.name)
            else:
                self.assertFalse(look.name.endswith("(NOLGIA)"), look.name)
        self.assertIn(looks.NON_AFFILIATION, looks.cube_text(looks.LOOKS[0], size=2))

    def test_library(self):
        self.assertEqual(len(set(look.slug for look in looks.LOOKS)), len(looks.LOOKS))
        self.assertTrue(set(look.family for look in looks.LOOKS) <= set(looks.FAMILIES))

    def test_every_look_is_sane(self):
        tinted = {"kodak-2383-print-warm", "kodak-2383-print-cool", "cross-process", "teal-orange", "day-for-night",
                  "golden-hour", "faded-film", "kodak-portra-400", "classic-print"}
        for look in looks.LOOKS:
            f = look.function
            grey = C.dwg_to_display(f([C.di_encode(0.18)] * 3))
            if look.slug not in tinted:
                self.assertLess(max(grey) - min(grey), 0.02, look.slug)
            if look.slug != "day-for-night":
                self.assertAlmostEqual(grey[1], 0.399, delta=0.04, msg=look.slug)
            lo = C.dwg_to_display(f([C.di_encode(0.18 / 64)] * 3))
            hi = C.dwg_to_display(f([C.di_encode(0.18 * 64)] * 3))
            self.assertLess(max(lo), 0.2, look.slug)
            self.assertGreater(min(hi), 0.75, look.slug)

    def test_cube(self):
        text = looks.cube_text(looks.BY_SLUG["neutral-film"], size=5)
        lines = [l for l in text.splitlines() if l and l[0].isdigit()]
        self.assertEqual(len(lines), 125)
        self.assertIn("LUT_3D_SIZE 5", text)
        self.assertIn("ACEScct", looks.cube_text(looks.BY_SLUG["neutral-film"], 3, "acescct"))


class LibraryTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="nolgia-lut-")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_install(self):
        self.assertGreater(library.install_base(self.root), 0)
        self.assertEqual(library.install_base(self.root), 0)  # unchanged files are not rewritten
        warm = looks.find("warm-print")
        stem = "Kodak 2383 print warm timing NOLGIA"  # no comma or bracket: a DEFINE_LUT path cannot have them
        path = library.install_look(self.root, warm, 0.6)
        self.assertEqual(path, "NOLGIA Grade/Looks/Print/%s 60%%.dctl" % stem)
        folder = os.path.join(self.root, "NOLGIA Grade", "Looks", "Print")
        names = sorted(os.listdir(folder))
        self.assertIn(stem + ".cube", names)
        self.assertIn(stem + " 10%.dctl", names)
        with open(os.path.join(folder, stem + " 60%.dctl"), encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("DEFINE_LUT(look, %s.cube)" % stem, text)
        self.assertIn("0.60", text)
        self.assertEqual(library.install_look(self.root, warm, 1.0), "NOLGIA Grade/Looks/Print/%s.cube" % stem)
        self.assertEqual(library.install_look(self.root, warm, 0.37), "NOLGIA Grade/Looks/Print/%s 37%%.dctl" % stem)
        for look in looks.LOOKS:
            self.assertFalse(set(looks.file_stem(look)) & set("(),;"), look.name)


def write_tiff(path, w, h, pixels, bits=16):
    top, fmt = (65535, "<HHH") if bits == 16 else (255, "<BBB")
    data = b"".join(struct.pack(fmt, *(max(0, min(top, int(round(c * top)))) for c in p)) for p in pixels)
    off = 8 + len(data)
    entries = [(256, 4, 1, w), (257, 4, 1, h), (258, 3, 1, bits), (259, 3, 1, 1), (262, 3, 1, 2), (273, 4, 1, 8),
               (277, 3, 1, 3), (278, 4, 1, h), (279, 4, 1, len(data))]
    out = b"II*\x00" + struct.pack("<I", off) + data + struct.pack("<H", len(entries))
    for tag, kind, n, value in entries:
        out += struct.pack("<HHIHH", tag, kind, n, value, 0) if kind == 3 else struct.pack("<HHII", tag, kind, n, value)
    with open(path, "wb") as handle:
        handle.write(out + struct.pack("<I", 0))


def scene(gains=(1.0, 1.0, 1.0), stops=0.0, w=64, h=36):
    """A synthetic frame in DI: a dark background, a white shirt, a lit face, as scene light times gains."""
    k = 2.0 ** stops
    face = C.mat_vec(C.REC709_TO_DWG, [0.52, 0.32, 0.22])
    shirt = C.mat_vec(C.REC709_TO_DWG, [0.55, 0.55, 0.55])
    pixels = []
    for y in range(h):
        for x in range(w):
            if x < w // 3:
                lin = shirt
            elif x < 2 * w // 3 and y < h // 2:
                lin = face
            else:
                lin = [0.004 + 0.0005 * ((x * 7 + y * 3) % 11)] * 3
            pixels.append([C.di_encode(max(v * k * g, 0.0)) for v, g in zip(lin, gains)])
    return pixels


class MeasureTest(unittest.TestCase):
    def test_tiff_reader(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "a.tif")
            write_tiff(path, 4, 2, [(0.1, 0.2, 0.3)] * 4 + [(1.0, 0.5, 0.0)] * 4)
            grid = measure.read_tiff_grid(path, cols=4, rows=2)
            self.assertAlmostEqual(grid[0][2], 0.3, places=4)
            self.assertAlmostEqual(grid[7][0], 1.0, places=4)
        finally:
            shutil.rmtree(tmp)

    def test_a_cast_and_exposure_are_found(self):
        base = measure.measure(scene())
        cool = measure.measure(scene(gains=(0.86, 1.0, 1.18), stops=-0.8))
        shift = [a - b for a, b in zip(measure.cast_stops(cool["white"]), measure.cast_stops(base["white"]))]
        expected = [-math.log2(g) for g in (0.86, 1.0, 1.18)]
        mean = sum(expected) / 3
        for a, b in zip(shift, [e - mean for e in expected]):
            self.assertAlmostEqual(a, b, delta=0.01)
        wr, wg, wb = C.npm(*C.GAMUTS["dwg"])[1]
        self.assertAlmostEqual(math.log2(cool["white_y"] / base["white_y"]), -0.8 + math.log2(wr * 0.86 + wg + wb * 1.18),
                               delta=0.02)

    def test_skin_is_found_whatever_the_cast(self):
        """Skin is picked after the white's cast is taken out, so a cast does not change which pixels count."""
        a = measure.measure(scene())
        b = measure.measure(scene(gains=(0.8, 1.0, 1.25), stops=-1.0))
        self.assertIn("skin", a)
        self.assertIn("skin", b)
        self.assertAlmostEqual(a["skin_share"], b["skin_share"], delta=0.01)

    def test_skin_says_where_it_is(self):
        """The points taken as skin are kept by place, for the finished picture's check to read skin there."""
        grid = scene()
        m = measure.measure(grid)
        self.assertEqual(len(m["skin_at"]), round(m["skin_share"] * len(grid)))
        w = 64
        for i in m["skin_at"]:
            x, y = i % w, i // w
            self.assertTrue(w // 3 <= x < 2 * w // 3 and y < 18, (x, y))  # the face, and nothing else


# ------------------------------------------------------------ the finished picture

W, H = measure.GRID  # a still the size of the grid: every pixel is a grid point, so the numbers are exact


def ramp(lo, hi, count, tint=(1.0, 1.0, 1.0)):
    """count display pixels from lo to hi, grey (or tinted)."""
    return [[(lo + (hi - lo) * i / (count - 1.0)) * t for t in tint] for i in range(count)]


def inside_picture():
    return ramp(0.05, 0.85, W * H)


def clipped_picture():
    """The lowest 81 rows a ramp, the top 9 (a tenth of the frame) a window with its red at the very top."""
    return ramp(0.05, 0.85, W * 81) + [[1.0, 0.98, 0.96]] * (W * 9)


def flat_picture():
    return ramp(0.30, 0.60, W * H)


def warm_tops_picture():
    """A grey ramp, and warm light in the brightest tenth: the tops apart, the bottoms level."""
    return ramp(0.05, 0.70, W * 81) + [[0.88, 0.80, 0.70]] * (W * 9)


def blue_blacks_picture():
    return [[0.02, 0.04, 0.08]] * (W * 9) + ramp(0.10, 0.80, W * 81)


class ScopesTest(unittest.TestCase):
    """scopes.py: scopecheck's reading of a finished picture, on stills with known numbers."""

    def setUp(self):
        from nolgia_resolve.grading import scopes
        self.S = scopes
        self.tmp = tempfile.mkdtemp(prefix="nolgia-scopes-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def still(self, pixels, w=W, h=H, bits=16):
        path = os.path.join(self.tmp, "f.tif")
        write_tiff(path, w, h, pixels, bits)
        return self.S.read_still(path)

    def check(self, pixels, skin=None, why=None):
        return self.S.check([self.still(pixels)], skin, why)

    def test_inside_the_table(self):
        out = self.check(inside_picture())
        self.assertTrue(out["read"])
        self.assertEqual(out["outside"], [])
        rows = out["rows"]
        self.assertEqual(rows["shadows"], {"value": 9.0, "status": "inside"})  # 0.05 + 0.8 * 5%
        self.assertEqual((rows["highlights"]["value"], rows["highlights"]["status"]), (0.0, "inside"))
        self.assertEqual(rows["clipping"]["value"], 0.0)
        self.assertEqual(rows["parade"]["tops"], [84.6, 84.6, 84.6])
        self.assertEqual(rows["parade"]["bottoms"], [5.4, 5.4, 5.4])
        self.assertEqual(rows["parade"]["status"], "reported")
        self.assertEqual(rows["saturation"], {"mean": 0.0, "p95": 0.0, "status": "reported"})
        self.assertEqual(rows["skin"]["status"], "not_read")
        self.assertEqual(out["waveform"]["p50"], 45.0)
        self.assertIsNone(self.S.sentence(3, out))

    def test_clipped(self):
        out = self.check(clipped_picture())
        rows = out["rows"]
        self.assertEqual(out["outside"], ["highlights", "clipping"])
        self.assertEqual(rows["highlights"]["value"], 10.0)
        self.assertEqual(rows["highlights"]["by"], 8.0)
        self.assertEqual(rows["highlights"]["reach"], 98.3)  # the window's level
        self.assertEqual(rows["clipping"]["channels"], {"r": [0.0, 10.0], "g": [0.0, 0.0], "b": [0.0, 0.0]})
        self.assertEqual((rows["clipping"]["value"], rows["clipping"]["by"]), (10.0, 9.9))
        self.assertEqual(rows["clipping"]["pixels"], W * H)
        self.assertEqual(rows["shadows"]["status"], "inside")
        self.assertEqual(rows["parade"]["status"], "reported")
        self.assertEqual(rows["parade"]["tops_apart"], 4.0)  # tops 100, 98, 96
        self.assertEqual(self.S.sentence(3, out),
                         "Clip 3: highlights reach 98%, the table stops near 90 (10% of the frame is over 90%, where "
                         "the sun, lamps, windows and speculars take about 2%); 10% of the red channel is clipped at "
                         "white.")

    def test_low_contrast(self):
        out = self.check(flat_picture())
        self.assertEqual(out["outside"], ["shadows"])
        self.assertEqual(out["rows"]["shadows"], {"value": 31.5, "status": "outside", "side": "over", "by": 21.5})
        self.assertEqual(self.S.sentence(2, out),
                         "Clip 2: the darkest 5% sit at 31.5%, over the 10% where shadows that hold detail sit (a "
                         "lifted or flat picture, unless meant).")

    def test_crushed(self):
        out = self.check([[0.0, 0.0, 0.0]] * (W * 18) + ramp(0.05, 0.85, W * 72))
        rows = out["rows"]
        self.assertEqual(rows["shadows"], {"value": 0.0, "status": "outside", "side": "under", "by": 5.0})
        self.assertEqual(rows["clipping"]["channels"]["g"], [20.0, 0.0])
        self.assertTrue(self.S.sentence(1, out).startswith(
            "Clip 1: the darkest 5% sit at 0%, under the 5 to 10% where shadows hold detail; 20% of the red channel "
            "is clipped at black, 20% of the green at black and 20% of the blue at black."))

    def test_tops_apart_are_reported_not_flagged(self):
        """Warm light that was meant parts the tops; the check cannot tell it from a cast, so it only reports."""
        out = self.check(warm_tops_picture())
        pa = out["rows"]["parade"]
        self.assertEqual(pa["status"], "reported")
        self.assertEqual(out["outside"], [])
        self.assertEqual((pa["tops"], pa["tops_apart"], pa["bottoms_apart"]), ([88.0, 80.0, 70.0], 18.0, 0.0))
        self.assertNotIn("apart", pa)
        self.assertNotIn("by", pa)
        self.assertEqual(out["rows"]["highlights"]["status"], "inside")
        self.assertIsNone(self.S.sentence(4, out))

    def test_bottoms_apart_are_reported_not_flagged(self):
        out = self.check(blue_blacks_picture())
        pa = out["rows"]["parade"]
        self.assertEqual(pa["status"], "reported")
        self.assertEqual(out["outside"], [])
        self.assertEqual((pa["bottoms"], pa["bottoms_apart"]), ([2.0, 4.0, 8.0], 6.0))
        self.assertNotIn("apart", pa)
        self.assertNotIn("by", pa)
        self.assertIsNone(self.S.sentence(5, out))

    def test_tops_apart_with_the_rest_inside_sit_inside_the_table(self):
        out = self.check(warm_tops_picture(), skin=[(0.70, 0.55, 0.45)] * 200)
        self.assertEqual(out["rows"]["parade"]["tops_apart"], 18.0)
        self.assertEqual(dict((r, out["rows"][r]["status"]) for r in self.S.ROWS),
                         {"shadows": "inside", "highlights": "inside", "clipping": "inside", "parade": "reported",
                          "skin": "inside", "saturation": "reported"})
        said = self.S.summary({1: out}, [1])
        self.assertEqual((said["outside"], said["unread"]), ([], []))
        self.assertEqual(said["say"], "The finished picture of the clip sits inside the scopes' table (shadows, "
                                      "highlights, clipping, and skin where the grade found it).")

    def test_clipping_is_counted_on_every_pixel(self):
        """Two one-pixel columns at white, between the grid's points: the grid sees none of it, the count sees all."""
        w, h = 3 * W, 3 * H
        pixels = [[1.0] * 3 if x in (0, 240) else [0.5] * 3 for y in range(h) for x in range(w)]
        still = self.still(pixels, w, h)
        self.assertFalse(any(max(p) > 0.9 for p in still["grid"]))
        out = self.S.check([still])
        self.assertEqual(out["rows"]["highlights"]["value"], 0.0)
        self.assertEqual(out["rows"]["clipping"]["channels"]["r"], [0.0, round(2 * h * 100.0 / (w * h), 3)])
        self.assertEqual(out["rows"]["clipping"]["status"], "outside")
        self.assertEqual(out["points"], W * H)
        self.assertEqual(out["rows"]["clipping"]["pixels"], w * h)

    def test_the_ends_of_an_8_bit_still(self):
        values = [0.0, 1 / 255.0, 2 / 255.0, 0.5, 253 / 255.0, 254 / 255.0, 1.0]
        still = self.still([[v] * 3 for v in values] * 10, 70, 1, bits=8)
        self.assertEqual(still["ends"]["g"], (20, 20))  # 0 and 1, 254 and 255
        still16 = self.still([[v] * 3 for v in values] * 10, 70, 1)
        # 16 bits: 1/255 and 254/255 are codes 257 and 65278, 2 codes past the count's edges (255 and 65280)
        self.assertEqual(still16["ends"]["g"], (10, 10))

    def test_frames_are_pooled(self):
        a, b = self.still(inside_picture()), self.still(clipped_picture())
        out = self.S.check([a, b])
        self.assertEqual(out["frames"], 2)
        self.assertEqual(out["rows"]["highlights"]["value"], 5.0)
        self.assertEqual(out["rows"]["clipping"]["channels"]["r"], [0.0, 5.0])

    def test_skin(self):
        lit = [(0.70, 0.55, 0.45)] * 200
        out = self.check(inside_picture(), skin=lit)
        sk = out["rows"]["skin"]
        self.assertEqual((sk["value"], sk["status"], sk["points"]), (57.5, "inside", 200))
        self.assertEqual(sk["share"], round(200 * 100.0 / (W * H), 2))
        self.assertIsNotNone(sk["hue_off_skin_line_deg"])
        backlit = self.check(inside_picture(), skin=[(0.45, 0.33, 0.27)] * 200)
        sk = backlit["rows"]["skin"]
        self.assertEqual((sk["value"], sk["status"], sk["side"], sk["by"]), (35.1, "outside", "under", 14.9))
        self.assertEqual(self.S.sentence(2, backlit),
                         "Clip 2: skin highlights sit at 35.1%, under the 50 to 75% of one official source (on 200 "
                         "points the grade measured as skin, by colour: check that they are skin).")
        grey = self.check(inside_picture(), skin=[(0.6, 0.6, 0.6)] * 200)["rows"]["skin"]
        self.assertIsNone(grey["hue_off_skin_line_deg"])  # a black and white look: no hue to read
        few = self.check(inside_picture(), skin=[(0.7, 0.55, 0.45)] * 5)["rows"]["skin"]
        self.assertEqual(few["status"], "not_read")

    def test_the_words_never_say_the_grade_is_wrong(self):
        per = {}
        for n, pixels in enumerate((inside_picture(), clipped_picture(), flat_picture(), warm_tops_picture(),
                                    blue_blacks_picture()), start=1):
            per[n] = self.check(pixels)
            say = self.S.sentence(n, per[n])
            if say:
                per[n]["say"] = say
        per[6] = {"read": False, "why": "DaVinci Resolve exported no frame"}
        out = self.S.summary(per, [1, 2, 3, 4, 5, 6])
        self.assertEqual((out["outside"], out["unread"]), ([2, 3], [6]))
        self.assertIn("not targets", out["say"])
        self.assertIn("it changed nothing in the grade", out["say"])
        self.assertIn("The finished picture of clip 6 could not be read (DaVinci Resolve exported no frame), so it was "
                      "not checked. The grade itself is done.", out["say"])
        words = out["say"].lower() + " ".join(out["table"].values()).lower()
        for bad in ("wrong", "error", "mistake", "fix", "should", "must", "bad", "fail"):
            self.assertNotIn(bad, words, bad)
        self.assertEqual(set(out["table"]), set(self.S.ROWS) | {"flags"})
        inside = self.S.summary({1: per[1]}, [1])
        self.assertEqual(inside["outside"], [])
        self.assertIn("sits inside the scopes' table", inside["say"])
        many = dict((n, dict(per[2], say="Clip %d: x." % n)) for n in range(1, 14))
        said = self.S.summary(many, list(range(1, 14)))["say"]
        self.assertIn("Clip 10: x.", said)
        self.assertNotIn("Clip 11: x.", said)
        self.assertIn("Clips 11, 12 and 13 also sit outside the table", said)


class ArgsTest(unittest.TestCase):
    def test_defaults(self):
        args = validate("grade", {})
        self.assertEqual(args["action"], "apply")
        self.assertTrue(args["correct"])
        self.assertEqual(args["clips"], "all")
        self.assertEqual(args["camera"], "auto")
        self.assertEqual(args["strength"], 1.0)

    def test_values(self):
        args = engine.validate_args({"clips": [1, 3], "match": 3, "look": "Kodak 2383 print, warm timing", "strength": 60,
                                     "camera": "S-Log3"})
        self.assertEqual((args["clips"], args["match"], args["look"], args["strength"], args["camera"]),
                         ([1, 3], 3, "kodak-2383-print-warm", 0.6, "slog3"))
        self.assertEqual(engine.validate_args({"look": "warm-print"})["look"], "kodak-2383-print-warm")  # old slug

    def test_grade_lut_kind(self):
        from nolgia_resolve.core.commands import CAPABILITIES
        from nolgia_resolve.ops import Ops
        self.assertIn("grade_lut", CAPABILITIES)  # NOLGIA's API sends a LUT grade only to a plugin with this kind
        args = validate("grade_lut", {"lut": "Rec709 Kodak 2383 D65", "strength": 50})
        self.assertEqual((args["lut"], args["strength"]), ("Rec709 Kodak 2383 D65", 0.5))
        self.assertTrue(callable(getattr(Ops, "do_grade_lut", None)))

    def test_refusals(self):
        for bad in ({"action": "paint"}, {"look": "Kodak 2383"}, {"strength": 300}, {"clips": [0]}, {"match": "3"},
                    {"camera": "red"}, {"action": "look"}, {"contrast": 5}, {"trust_white": "all"},
                    {"trust_white": [0]}):
            with self.assertRaises(CommandError, msg=bad):
                engine.validate_args(bad)

    def test_describe_says_what_and_what_it_costs(self):
        title, lines = engine.describe(engine.validate_args({"match": 3, "look": "warm-print", "strength": 0.6}))
        text = " ".join(lines)
        self.assertIn("matched to clip 3", text)
        self.assertIn("Kodak 2383 print, warm timing (NOLGIA) at 60%", text)
        self.assertIn("no NOLGIA credits", text)
        self.assertIn("Versions menu", text)
        self.assertIn("left as it was shot", text)
        self.assertNotIn("however far off", text)
        said = " ".join(engine.describe(engine.validate_args({"match": 3, "trust_white": [4]}))[1])
        self.assertIn("Clip 4: balanced on its own white however far off it is", said)

    def test_camera_words(self):
        self.assertEqual(engine.camera_from_text("A001_C002_SLOG3.MXF"), "slog3")
        self.assertEqual(engine.camera_from_text("LogC4"), "logc4")
        self.assertEqual(engine.camera_from_text("Canon Log 3"), "clog3")
        self.assertEqual(engine.camera_from_text("Apple Log"), "applelog")
        self.assertIsNone(engine.camera_from_text("interview.mov"))


# ------------------------------------------------------------ a stand-in Resolve

class FakeGraph(object):
    def __init__(self, item):
        self.item = item

    def ApplyGradeFromDRX(self, path, mode):
        self.item.events.append(("ApplyGradeFromDRX", os.path.basename(path)))
        with open(path, encoding="utf-8") as handle:
            self.item.nodes = [{"label": l, "params": p, "on": True} for l, p in decode_drx(handle.read())]
        return True

    def GetNumNodes(self):
        return len(self.item.nodes)

    def GetNodeLabel(self, i):
        return self.item.nodes[i - 1]["label"]

    def GetLUT(self, i):
        return self.item.nodes[i - 1]["params"].get(drx.LUT_PATH, "")

    def GetToolsInNode(self, i):
        return []

    def SetNodeEnabled(self, i, on):
        self.item.events.append(("SetNodeEnabled", i, on))
        self.item.nodes[i - 1]["on"] = on
        return True

    def SetLUT(self, i, path):
        self.item.events.append(("SetLUT", i, path))
        self.item.nodes[i - 1]["params"][drx.LUT_PATH] = path.replace("/", "\\")
        return True


class FakeItem(object):
    def __init__(self, name, start, end, gains, stops):
        self.name, self.start, self.end, self.gains, self.stops = name, start, end, gains, stops
        self.nodes = []
        self.versions = ["Version 1"]
        self.current = "Version 1"
        self.finished = None  # (width, height, display pixels): the finished picture to serve, else CST OUT's
        self.events = []      # node changes and exports, in order

    def GetName(self):
        return self.name

    def GetStart(self):
        return self.start

    def GetEnd(self):
        return self.end

    def GetUniqueId(self):
        return self.name

    def GetNodeGraph(self):
        return FakeGraph(self)

    def GetVersionNameList(self, kind):
        return list(self.versions)

    def GetCurrentVersion(self):
        return {"versionName": self.current, "versionType": 0}

    def AddVersion(self, name, kind):
        self.versions.append(name)
        return True

    def LoadVersionByName(self, name, kind):
        self.current = name
        self.nodes = []
        return True

    def GetMediaPoolItem(self):
        return FakeMediaItem(self.name)

    def all_on(self):
        return len(self.nodes) == len(engine.LABELS) and all(n["on"] for n in self.nodes)

    def frame(self):
        """The clip as exported: scene in DI, through the enabled BALANCE and EXPOSURE offsets. With every node on,
        the finished picture: the test's own, or that through CST OUT (CONTRAST to LOOK are not modelled)."""
        pixels = scene(self.gains, self.stops)
        add = [0.0, 0.0, 0.0]
        for node in self.nodes:
            if not node["on"]:
                continue
            for k, pid in enumerate(drx.OFFSET):
                add[k] += node["params"].get(pid, 0.0) * drx.OFFSET_CODE
        graded = [[v + a for v, a in zip(p, add)] for p in pixels]
        if self.all_on():
            if self.finished is not None:
                return self.finished
            return 64, 36, [C.dwg_to_display(p) for p in graded]
        return 64, 36, graded


class FakeMediaItem(object):
    def __init__(self, name):
        self.name = name

    def GetClipProperty(self, key=None):
        return {"Clip Name": self.name, "File Path": "/media/" + self.name}


class FakeTimeline(object):
    def __init__(self, items):
        self.items = items
        self.tc = "01:00:00:00"

    def GetName(self):
        return "Scene 4"

    def GetTrackCount(self, kind):
        return 1

    def GetItemListInTrack(self, kind, index):
        return list(self.items)

    def GetCurrentTimecode(self):
        return self.tc

    def at(self):
        h, m, s, f = (int(x) for x in self.tc.split(":"))
        frame = (((h - 1) * 60 + m) * 60 + s) * 24 + f
        return next(i for i in self.items if i.start <= frame < i.end)


class FakeOps(object):
    def __init__(self, root, items):
        self.resolve = self
        self.root = root
        self.tl = FakeTimeline(items)
        self.changed = False
        self.science = "davinciYRGB"
        self.refuse_finished = set()   # clip names whose finished picture Resolve does not export
        self.garble_finished = set()   # ... or exports as a file that is not a TIFF

    # resolve
    def GetCurrentPage(self):
        return "color"

    def OpenPage(self, page):
        return True

    # ops
    def lut_folder(self):
        folder = os.path.join(self.root, "NOLGIA")
        os.makedirs(folder, exist_ok=True)
        return folder

    def project(self):
        return self

    def timeline(self, project):
        return self.tl

    def timeline_facts(self, tl):
        return {"fps": 24.0, "start": 0, "end": 300, "width": 1920, "height": 1080}

    def offset_timecode(self, facts, offset):
        s, f = divmod(offset, 24)
        return "01:00:%02d:%02d" % (s, f)

    def set_playhead(self, tl, text):
        tl.tc = text
        return True

    def _export_still(self, project, path):
        item = self.tl.at()
        finished = item.all_on()
        item.events.append(("export", finished))
        if finished and item.name in self.refuse_finished:
            return False
        if finished and item.name in self.garble_finished:
            with open(path, "wb") as handle:
                handle.write(b"not a still")
            return True
        write_tiff(path, *item.frame())
        return True

    def current_item(self, tl):
        return tl.at()

    # project
    def GetName(self):
        return "Rooftop"

    def GetSetting(self, key):
        return self.science if key == "colorScienceMode" else ""

    def RefreshLUTList(self):
        return True


class GraderTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="nolgia-grade-")
        self.items = [FakeItem("A_SLOG3.mov", 0, 48, (0.86, 1.0, 1.18), -0.8),
                      FakeItem("B_SLOG3.mov", 48, 96, (1.0, 1.12, 0.97), 0.5),
                      FakeItem("C_SLOG3.mov", 96, 144, (1.0, 1.0, 1.0), 0.0)]
        self.ops = FakeOps(self.root, self.items)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_correct_match_and_look(self):
        args = engine.validate_args({"match": 3, "look": "kodak-2383-print-warm", "strength": 0.6})
        result = engine.Grader(self.ops).run(args)
        clips = {c["clip"]: c for c in result["clips"]}
        self.assertEqual([c["camera"] for c in result["clips"]], ["slog3"] * 3)
        # The same scene with a camera's mistakes: matching to C takes them out.
        for n, (gains, stops) in ((1, ((0.86, 1.0, 1.18), -0.8)), (2, ((1.0, 1.12, 0.97), 0.5))):
            logs = [-math.log2(g) for g in gains]
            mean = sum(logs) / 3
            for got, want in zip(clips[n]["balance_stops"], [l - mean for l in logs]):
                self.assertAlmostEqual(got, want, delta=0.03, msg=n)
            self.assertAlmostEqual(clips[n]["exposure_stops"], -stops + mean, delta=0.06, msg=n)
        self.assertEqual(clips[3]["balance_stops"], [0.0, 0.0, 0.0])
        self.assertEqual(clips[3]["exposure_stops"], 0.0)
        for item in self.items:
            self.assertEqual(item.current, "NOLGIA grade")
            self.assertEqual([n["label"] for n in item.nodes], list(engine.LABELS))
            self.assertTrue(all(n["on"] for n in item.nodes))
            look = item.nodes[6]["params"][drx.LUT_PATH]
            self.assertEqual(look, "NOLGIA Grade\\Looks\\Print\\Kodak 2383 print warm timing NOLGIA 60%.dctl")
        self.assertTrue(os.path.isfile(os.path.join(self.root, "NOLGIA Grade", "Looks", "Print",
                                                    "Kodak 2383 print warm timing NOLGIA.cube")))
        self.assertEqual(result["cost"], "free: it ran in DaVinci Resolve")

    # A fourth clip whose "white" is 1.1 stops off (red against blue) and 1.2 stops under: a camera set badly wrong, or
    # a shot with nothing white in it. The measurement is the same for both, so the correction does not guess.
    FAR = ((1.5, 1.0, 0.7), -1.2)

    def with_a_far_clip(self):
        self.items.append(FakeItem("D_SLOG3.mov", 144, 192, *self.FAR))
        self.ops = FakeOps(self.root, self.items)

    def test_a_white_too_far_to_trust_is_left_as_shot(self):
        self.with_a_far_clip()
        result = engine.Grader(self.ops).run(engine.validate_args({"match": 3}))
        clips = {c["clip"]: c for c in result["clips"]}
        far = clips[4]
        self.assertEqual((far["balance_stops"], far["exposure_stops"]), ([0.0, 0.0, 0.0], 0.0))
        self.assertAlmostEqual(far["left_as_shot"]["gap_stops"], math.log2(1.5 / 0.7), delta=0.05)
        self.assertEqual(far["left_as_shot"]["from"], "clip 3's")
        self.assertTrue(far["matched_on"]["colour"].startswith("kept: its white is"))
        self.assertTrue(far["matched_on"]["exposure"].startswith("kept"))
        self.assertNotIn("second_pass", far)
        self.assertNotIn("residual_after_first_pass", far)
        # It still has the labelled nodes, at rest, for the person to set by eye.
        self.assertEqual([n["label"] for n in self.items[3].nodes], list(engine.LABELS))
        self.assertEqual(result["left_as_shot"]["clips"], [4])
        self.assertIn("Clip 4 was left as shot", result["left_as_shot"]["say"])
        self.assertIn("trust_white: [4]", result["left_as_shot"]["say"])
        # The others are corrected as before.
        for n, (gains, stops) in ((1, ((0.86, 1.0, 1.18), -0.8)), (2, ((1.0, 1.12, 0.97), 0.5))):
            logs = [-math.log2(g) for g in gains]
            mean = sum(logs) / 3
            for got, want in zip(clips[n]["balance_stops"], [l - mean for l in logs]):
                self.assertAlmostEqual(got, want, delta=0.03, msg=n)
            self.assertAlmostEqual(clips[n]["exposure_stops"], -stops + mean, delta=0.06, msg=n)
            self.assertNotIn("left_as_shot", clips[n])

    def test_trust_white_corrects_it_all_the_same(self):
        self.with_a_far_clip()
        result = engine.Grader(self.ops).run(engine.validate_args({"match": 3, "trust_white": [4]}))
        far = {c["clip"]: c for c in result["clips"]}[4]
        gains, stops = self.FAR
        logs = [-math.log2(g) for g in gains]
        mean = sum(logs) / 3
        for got, want in zip(far["balance_stops"], [l - mean for l in logs]):
            self.assertAlmostEqual(got, want, delta=0.03)
        self.assertAlmostEqual(far["exposure_stops"], -stops + mean, delta=0.06)
        self.assertNotIn("left_as_shot", far)
        self.assertNotIn("left_as_shot", result)

    def test_with_no_hero_a_far_white_is_not_made_neutral(self):
        self.with_a_far_clip()
        result = engine.Grader(self.ops).run(engine.validate_args({}))
        clips = {c["clip"]: c for c in result["clips"]}
        self.assertEqual(clips[4]["balance_stops"], [0.0, 0.0, 0.0])
        self.assertEqual(clips[4]["left_as_shot"]["from"], "neutral")
        self.assertEqual(result["left_as_shot"]["clips"], [4])
        self.assertNotIn(" against clip", result["left_as_shot"]["say"])
        self.assertNotEqual(clips[1]["balance_stops"], [0.0, 0.0, 0.0])  # an ordinary cast is still taken out

    def test_a_far_hero_is_not_made_neutral(self):
        self.with_a_far_clip()
        result = engine.Grader(self.ops).run(engine.validate_args({"clips": [3, 4], "match": 4, "neutral_hero": True}))
        clips = {c["clip"]: c for c in result["clips"]}
        self.assertEqual(clips[4]["balance_stops"], [0.0, 0.0, 0.0])
        self.assertEqual(clips[4]["left_as_shot"]["from"], "neutral")
        # ... and nothing is matched to a white that far away either.
        self.assertEqual(clips[3]["balance_stops"], [0.0, 0.0, 0.0])
        self.assertEqual(clips[3]["left_as_shot"]["from"], "clip 4's")
        self.assertEqual(result["left_as_shot"]["clips"], [3, 4])

    def test_a_second_grade_keeps_the_first(self):
        engine.Grader(self.ops).run(engine.validate_args({"clips": [3], "correct": False}))
        engine.Grader(self.ops).run(engine.validate_args({"clips": [3], "correct": False}))
        self.assertEqual(self.items[2].versions, ["Version 1", "NOLGIA grade", "NOLGIA grade 2"])

    def test_change_look_only(self):
        engine.Grader(self.ops).run(engine.validate_args({"clips": [1], "look": "kodak-2383-print-warm"}))
        out = engine.Grader(self.ops).run(engine.validate_args({"action": "look", "clips": [1], "look": "ilford-hp5-plus",
                                                                "strength": 0.3}))
        self.assertEqual(len(out["changed"]), 1)
        self.assertEqual(self.items[0].nodes[6]["params"][drx.LUT_PATH],
                         "NOLGIA Grade\\Looks\\Black and white\\Ilford HP5 Plus NOLGIA 30%.dctl")

    def test_unknown_camera_asks(self):
        self.items[0].name = "interview.mov"
        with self.assertRaises(CommandError) as caught:
            engine.Grader(self.ops).run(engine.validate_args({}))
        self.assertIn("needs_camera", caught.exception.result)

    def test_looks_lists_the_library(self):
        out = engine.Grader(self.ops).run(engine.validate_args({"action": "looks"}))
        self.assertEqual(len(out["looks"]), len(looks.LOOKS))

    # .............................................................. the finished picture

    def test_the_finished_picture_says_what_sits_outside(self):
        self.items[0].finished = (W, H, flat_picture())
        self.items[1].finished = (W, H, inside_picture())
        self.items[2].finished = (W, H, clipped_picture())
        result = engine.Grader(self.ops).run(engine.validate_args({"correct": False, "look": "kodak-2383-print"}))
        clips = {c["clip"]: c["finished_picture"] for c in result["clips"]}
        self.assertEqual([clips[n]["outside"] for n in (1, 2, 3)], [["shadows"], [], ["highlights", "clipping"]])
        self.assertEqual(clips[3]["frames"], 3)  # the grade's own frames, no more
        self.assertEqual(clips[3]["rows"]["highlights"]["by"], 8.0)
        self.assertEqual(clips[3]["rows"]["clipping"]["channels"]["r"], [0.0, 10.0])
        self.assertTrue(clips[3]["say"].startswith("Clip 3: highlights reach 98%, the table stops near 90"))
        self.assertNotIn("say", clips[2])
        self.assertIn("correct was off", clips[3]["rows"]["skin"]["why"])
        summary = result["finished_picture"]
        self.assertEqual((summary["outside"], summary["unread"]), ([1, 3], []))
        self.assertTrue(summary["say"].startswith(clips[1]["say"] + " " + clips[3]["say"] + " These are flags"))
        self.assertIn("shadows", summary["table"])
        json.dumps(result)  # plain data for the NOLGIA Agent
        exports = [e for e in self.items[2].events if e[0] == "export"]
        self.assertEqual(exports, [("export", True)] * 3)  # no measurement with correct off: only the finished frames

    def test_skin_is_read_where_the_grade_found_it(self):
        result = engine.Grader(self.ops).run(engine.validate_args({"match": 3}))
        hero = {c["clip"]: c for c in result["clips"]}[3]["finished_picture"]
        skin = hero["rows"]["skin"]
        self.assertNotEqual(skin["status"], "not_read")
        face = C.mat_vec(C.REC709_TO_DWG, [0.52, 0.32, 0.22])
        shown = C.dwg_to_display([C.di_encode(v) for v in face])  # the hero is not moved: its face through CST OUT
        self.assertAlmostEqual(skin["value"], 100 * (0.2126 * shown[0] + 0.7152 * shown[1] + 0.0722 * shown[2]),
                               delta=0.1)
        self.assertGreater(skin["points"], 1000)
        self.assertAlmostEqual(skin["share"], skin["points"] * 100.0 / hero["points"], places=2)
        self.assertEqual(skin["status"], "inside" if 50 <= skin["value"] <= 75 else "outside")

    def test_a_finished_picture_that_cannot_be_read_does_not_fail_the_grade(self):
        clean = engine.Grader(self.ops).run(engine.validate_args({"match": 3}))
        self.ops.refuse_finished = {"A_SLOG3.mov"}
        self.ops.garble_finished = {"B_SLOG3.mov"}
        result = engine.Grader(self.ops).run(engine.validate_args({"match": 3}))
        clips = {c["clip"]: c for c in result["clips"]}
        self.assertEqual(clips[1]["finished_picture"], {"read": False, "why": "DaVinci Resolve exported no frame"})
        self.assertFalse(clips[2]["finished_picture"]["read"])
        self.assertTrue(clips[2]["finished_picture"]["why"].startswith("its still could not be read"))
        self.assertTrue(clips[3]["finished_picture"]["read"])
        self.assertEqual(result["finished_picture"]["unread"], [1, 2])
        self.assertIn("The finished picture of clips 1 and 2 could not be read", result["finished_picture"]["say"])
        self.assertIn("The grade itself is done.", result["finished_picture"]["say"])
        for a, b in zip(clean["clips"], result["clips"]):
            self.assertEqual((a["balance_stops"], a["exposure_stops"]), (b["balance_stops"], b["exposure_stops"]))
        for item in self.items:
            self.assertEqual([n["label"] for n in item.nodes], list(engine.LABELS))
            self.assertTrue(all(n["on"] for n in item.nodes))

    def test_the_check_stopping_does_not_fail_the_grade(self):
        def broken(*args, **kwargs):
            raise RuntimeError("a bug")
        for name, why in (("read_still", "its still could not be read: a bug"), ("check", "the check stopped: a bug"),
                          ("summary", "the check stopped: a bug")):
            original = getattr(engine.S, name)
            setattr(engine.S, name, broken)
            try:
                result = engine.Grader(self.ops).run(engine.validate_args({"clips": [3], "correct": False}))
            finally:
                setattr(engine.S, name, original)
            self.assertEqual(result["clips"][0]["finished_picture"], {"read": False, "why": why}, name)
            self.assertEqual(result["finished_picture"]["unread"], [3], name)
            self.assertEqual([n["label"] for n in self.items[2].nodes], list(engine.LABELS), name)

    def test_the_check_changes_nothing_in_the_node_tree(self):
        import copy
        seen = {}
        original = engine.Grader.finished_pictures

        def watched(grader, clips, *args):
            seen["before"] = [copy.deepcopy(item.nodes) for item in self.items]
            marks = [len(item.events) for item in self.items]
            out = original(grader, clips, *args)
            seen["after"] = [copy.deepcopy(item.nodes) for item in self.items]
            seen["during"] = [item.events[m:] for item, m in zip(self.items, marks)]
            return out

        engine.Grader.finished_pictures = watched
        try:
            result = engine.Grader(self.ops).run(engine.validate_args({"match": 3, "look": "kodak-2383-print-warm"}))
        finally:
            engine.Grader.finished_pictures = original
        self.assertEqual(seen["before"], seen["after"])
        for during in seen["during"]:
            self.assertEqual(during, [("export", True)] * 3)  # every node on at each export, and no node touched
        # ... and the grade it leaves is the grade without the check.
        trees = [copy.deepcopy(item.nodes) for item in self.items]
        engine.Grader.finished_pictures = lambda grader, clips, *args: dict(
            (n, {"read": False, "why": "off"}) for n, _ in clips)
        try:
            without = engine.Grader(self.ops).run(engine.validate_args({"match": 3, "look": "kodak-2383-print-warm"}))
        finally:
            engine.Grader.finished_pictures = original
        self.assertEqual(trees, [item.nodes for item in self.items])
        for a, b in zip(result["clips"], without["clips"]):
            self.assertEqual((a["balance_stops"], a["exposure_stops"], a["nodes"]),
                             (b["balance_stops"], b["exposure_stops"], b["nodes"]))

    def test_a_colour_managed_project_is_not_read(self):
        self.ops.science = "davinciYRGBColorManagedv2"
        result = engine.Grader(self.ops).run(engine.validate_args({"look": "kodak-2383-print"}))
        for clip in result["clips"]:
            self.assertFalse(clip["finished_picture"]["read"])
            self.assertIn("colour managed", clip["finished_picture"]["why"])
        self.assertEqual(result["finished_picture"]["unread"], [1, 2, 3])
        self.assertTrue(result["finished_picture"]["say"].startswith("The finished picture was not read (the project "
                                                                     "is colour managed"))
        self.assertFalse(any(e[0] == "export" for item in self.items for e in item.events))

    def test_a_long_answer_keeps_what_sits_outside(self):
        self.items[2].finished = (W, H, clipped_picture())
        room = engine.RESULT_ROOM
        engine.RESULT_ROOM = 1000
        try:
            result = engine.Grader(self.ops).run(engine.validate_args({"correct": False}))
        finally:
            engine.RESULT_ROOM = room
        self.assertEqual(result["clips"][2]["finished_picture"], {"read": True, "outside": ["highlights", "clipping"]})
        self.assertIn("too long", result["finished_picture"]["trimmed"])
        self.assertIn("Clip 3: highlights reach 98%", result["finished_picture"]["say"])

    def test_the_approval_says_the_check_changes_nothing(self):
        lines = " ".join(engine.describe(engine.validate_args({}))[1])
        self.assertIn("finished picture", lines)
        self.assertIn("that check changes nothing", lines)
        self.assertNotIn("finished picture", " ".join(
            engine.describe(engine.validate_args({"action": "look", "look": "none"}))[1]))


class GradeWindowTest(unittest.TestCase):
    def test_builds_and_reads_the_form(self):
        import fake_ui
        from nolgia_resolve.gradewin import GradeWindow, NO_LOOK

        class Panel(object):
            def __init__(self):
                self.ui = fake_ui.UIManager()
                self.disp = fake_ui.Dispatcher(self.ui)
                self.log = lambda message: None

            def _guard(self, fn):
                return fn

            def _click(self, element_id, fn):
                return fn

        window = GradeWindow(Panel())
        window.show()
        tree = window.items["Looks"]
        self.assertEqual(tree.rows[0].Text[1], NO_LOOK)
        from nolgia_resolve.grading import userluts
        self.assertEqual(len(tree.rows), 1 + len(looks.LOOKS) + len(userluts.OFFICIAL))
        tree.__dict__["current"] = 2  # Kodak 2383 print, warm timing
        window.items["Strength"].Value = 60
        window.items["Hero"].Value = 3
        args = engine.validate_args(window.args("apply"))
        self.assertEqual((args["look"], args["strength"], args["match"]), ("kodak-2383-print-warm", 0.6, 3))


if __name__ == "__main__":
    unittest.main()


class UserLutsTest(unittest.TestCase):
    """0.3.1: the LUTs already on the computer, under their own names, at the strength dial; never copied."""

    def setUp(self):
        from nolgia_resolve.grading import userluts
        self.U = userluts
        self.root = tempfile.mkdtemp(prefix="nolgia-luts-")
        self.cfg = tempfile.mkdtemp(prefix="nolgia-cfg-")
        self.env = dict(os.environ)
        os.environ["NOLGIA_LUT_DIR"] = self.root
        os.environ["NOLGIA_CONFIG_DIR"] = self.cfg

        def cube(rel, header="", size=2):
            path = os.path.join(self.root, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            n = size - 1
            rows = ["%f %f %f" % (r / n, g / n, b / n) for b in range(size) for g in range(size) for r in range(size)]
            with open(path, "w") as handle:
                handle.write(header + "LUT_3D_SIZE %d\n" % size + "\n".join(rows) + "\n")
            return path
        self.cube = cube
        self.print_lut = cube("Film Looks/Rec709 Kodak 2383 D65.cube", "# Resolve Film Look LUT\n#   Input: Cineon Log\n")
        cube("Film Looks/DCI-P3 Kodak 2383 D65.cube", "#   Input: Cineon Log\n")
        cube("Sony/SLog3SGamut3.CineToLC-709.cube", "TITLE \"SLog3SGamut3.CineToLC_709\"\n")
        cube("VFX IO/Linear to Cineon Log.cube")
        cube("NOLGIA Grade/Looks/Print/x.cube")
        cube("Blackmagic Design/Blackmagic 4.6K Film to Video v4.cube", "BMD_TITLE \"4.6K Film to Video\"\n")
        self.mine = cube("My Pack/Film Emulation 5219.cube")
        self.bad = cube("Pack (2024), v2/Night.cube")

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env)
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.cfg, ignore_errors=True)

    def test_detects_what_a_lut_takes(self):
        d = self.U.detect_input
        self.assertEqual(d("XH2S_FLog2_to_ETERNA_33grid", "")[0], "flog2")
        self.assertEqual(d("FLog_FGamut_to_PROVIA", "")[0], "flog")
        self.assertEqual(d("GFX100II_FLog2C_to_ETERNA", "")[0], "flog2c")
        self.assertEqual(d("Mavic3_DLogM_to_Rec709", "")[0], "unsupported")
        self.assertEqual(d("LogC4_to_Rec709", "")[0], "logc4")
        self.assertEqual(d("x", "#   Input: Cineon Log\n")[0], "cineon")
        self.assertEqual(d("Teal Summer", "")[0], "rec709")
        self.assertEqual(d("Leica L-Log to Rec.709", "")[0], "unsupported")  # a log with no transform here
        self.assertEqual(d("Olympus OM-Log400_to_BT.709", "")[0], "unsupported")
        self.assertEqual(d("Catalog Warm", "")[0], "rec709")

    def test_index_groups_and_skips(self):
        found = {e["name"]: e for e in self.U.index()}
        self.assertEqual(found["Rec709 Kodak 2383 D65"]["group"], self.U.INSTALLED)
        self.assertEqual(found["Rec709 Kodak 2383 D65"]["input"], "cineon")
        self.assertEqual(found["DCI-P3 Kodak 2383 D65"]["output"], "dci-p3")
        self.assertEqual(found["SLog3SGamut3.CineToLC-709"]["input"], "slog3")
        self.assertEqual(found["Blackmagic 4.6K Film to Video v4"]["input"], "unsupported")
        self.assertEqual(found["Film Emulation 5219"]["group"], self.U.YOURS)
        self.assertNotIn("Linear to Cineon Log", found)
        self.assertEqual(found["Night"]["input"], "unsupported")  # a DCTL cannot point at a path with ( ) or ,
        self.assertIn("bracket", found["Night"]["note"])
        self.assertNotIn("x", found)
        extra = tempfile.mkdtemp(prefix="nolgia-extra-")
        try:
            with open(os.path.join(extra, "A_FLog2_to_ETERNA.cube"), "w") as handle:
                handle.write("LUT_3D_SIZE 2\n" + "0 0 0\n" * 8)
            self.U.save_folders([extra])
            names = {e["name"]: e for e in self.U.index()}
            self.assertEqual(names["A_FLog2_to_ETERNA"]["input"], "flog2")
        finally:
            shutil.rmtree(extra)

    def test_the_wrapper_points_at_the_file_and_never_copies_it(self):
        entry = self.U.find("Rec709 Kodak 2383 D65", self.U.index())
        path = library.install_user_lut(self.root, entry, 0.6)
        self.assertEqual(path, "NOLGIA Grade/LUTs at strength/Installed with DaVinci Resolve/Rec709 Kodak 2383 D65/"
                               "Rec709 Kodak 2383 D65 60%.dctl")
        with open(os.path.join(self.root, path), encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("DEFINE_LUT(lut, %s)" % self.print_lut.replace("\\", "/"), text)
        self.assertIn("0.60", text)
        self.assertEqual(text.count("{"), text.count("}"))
        copies = [f for base, _d, files in os.walk(os.path.join(self.root, "NOLGIA Grade")) for f in files
                  if f.endswith(".cube")]
        self.assertEqual(copies, ["x.cube"])  # only the fixture's own file: nothing was copied in

    def test_every_input_space_makes_a_dctl(self):
        for space in self.U.INPUTS:
            if space == "auto":
                continue
            text = dctl.lut_at_strength("C:/LUTs/a b.cube", space, 0.5, "a b")
            self.assertEqual(text.count("{"), text.count("}"), space)
            self.assertIn("DEFINE_LUT(lut, C:/LUTs/a b.cube)", text)

    def test_python_twin(self):
        ident = lambda rgb: list(rgb)
        x = [C.di_encode(0.18), C.di_encode(0.3), C.di_encode(0.05)]
        for space in ("dwg", "rec709"):
            out = self.U.wrap_python(ident, space, 1.0)(x)
            self.assertLess(max(abs(a - b) for a, b in zip(out, x)), 2e-4, space)
        half = self.U.wrap_python(lambda rgb: [v + 0.1 for v in rgb], "dwg", 0.5)(x)
        self.assertAlmostEqual(half[0], x[0] + 0.05, places=6)

    def test_grade_with_a_lut(self):
        items = [FakeItem("C_SLOG3.mov", 0, 48, (1.0, 1.0, 1.0), 0.0)]
        ops = FakeOps(self.root, items)
        ops.lut_folder = lambda: os.path.join(self.root, "NOLGIA")
        out = engine.Grader(ops).run(engine.validate_args({"lut": "Rec709 Kodak 2383 D65", "strength": 0.6}))
        self.assertEqual(out["lut"]["name"], "Rec709 Kodak 2383 D65")
        self.assertEqual(items[0].nodes[6]["label"], "LOOK")
        self.assertTrue(items[0].nodes[6]["params"][drx.LUT_PATH].endswith("Rec709 Kodak 2383 D65 60%.dctl"))
        with self.assertRaises(CommandError):
            engine.Grader(ops).run(engine.validate_args({"lut": "Blackmagic 4.6K Film to Video v4"}))
        with self.assertRaises(CommandError):
            engine.Grader(ops).run(engine.validate_args({"lut": "Night"}))
        with self.assertRaises(CommandError):
            engine.validate_args({"lut": "a", "look": "warm-print"})
        listing = engine.Grader(ops).run(engine.validate_args({"action": "looks"}))
        self.assertIn("Rec709 Kodak 2383 D65", [e["name"] for e in listing["installed_with_resolve"]])
        self.assertIn("Film Emulation 5219", [e["name"] for e in listing["your_luts"]])

    def test_window_add_folder_lists_once(self):
        import fake_ui
        from nolgia_resolve.gradewin import GradeWindow

        class Panel(object):
            def __init__(self):
                self.ui = fake_ui.UIManager()
                self.disp = fake_ui.Dispatcher(self.ui)
                self.log = lambda message: None

            def _guard(self, fn):
                return fn

            def _click(self, element_id, fn):
                return fn

        window = GradeWindow(Panel())
        window.show()
        tree = window.items["Looks"]
        before = len(tree.rows)
        extra = tempfile.mkdtemp(prefix="nolgia-mine-")
        try:
            with open(os.path.join(extra, "Warm Dusk.cube"), "w") as handle:
                handle.write("LUT_3D_SIZE 2\n" + "0 0 0\n" * 8)
            window.items["Folder"].Text = extra
            window._add_folder()
            families = [row.Text[0] for row in tree.rows]
            self.assertEqual(len(tree.rows), before + 1)
            self.assertEqual(families.count("Free from the maker"), len(self.U.OFFICIAL))
            self.assertEqual(families[-len(self.U.OFFICIAL):], ["Free from the maker"] * len(self.U.OFFICIAL))
            self.assertIn(("Your LUTs", "Warm Dusk"), [(row.Text[0], row.Text[1]) for row in tree.rows])
        finally:
            shutil.rmtree(extra)
