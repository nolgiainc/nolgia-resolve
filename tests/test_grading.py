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


TRADEMARKS = ("kodak", "fuji", "ilford", "agfa", "cinestill", "portra", "ektar", "vision3", "eterna", "velvia",
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
        tinted = {"warm-print", "cool-print", "consumer-neg-400", "cross-process", "teal-orange", "day-for-night",
                  "golden-hour", "faded-film", "portrait-neg-400", "classic-print"}
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
        path = library.install_look(self.root, looks.BY_SLUG["warm-print"], 0.6)
        self.assertEqual(path, "NOLGIA Grade/Looks/Print/Warm Print 60%.dctl")
        folder = os.path.join(self.root, "NOLGIA Grade", "Looks", "Print")
        names = sorted(os.listdir(folder))
        self.assertIn("Warm Print.cube", names)
        self.assertIn("Warm Print 10%.dctl", names)
        with open(os.path.join(folder, "Warm Print 60%.dctl"), encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("DEFINE_LUT(look, Warm Print.cube)", text)
        self.assertIn("0.60", text)
        self.assertEqual(library.install_look(self.root, looks.BY_SLUG["warm-print"], 1.0),
                         "NOLGIA Grade/Looks/Print/Warm Print.cube")
        self.assertEqual(library.install_look(self.root, looks.BY_SLUG["warm-print"], 0.37),
                         "NOLGIA Grade/Looks/Print/Warm Print 37%.dctl")


def write_tiff(path, w, h, pixels):
    data = b"".join(struct.pack("<HHH", *(max(0, min(65535, int(round(c * 65535)))) for c in p)) for p in pixels)
    off = 8 + len(data)
    entries = [(256, 4, 1, w), (257, 4, 1, h), (258, 3, 1, 16), (259, 3, 1, 1), (262, 3, 1, 2), (273, 4, 1, 8),
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


class ArgsTest(unittest.TestCase):
    def test_defaults(self):
        args = validate("grade", {})
        self.assertEqual(args["action"], "apply")
        self.assertTrue(args["correct"])
        self.assertEqual(args["clips"], "all")
        self.assertEqual(args["camera"], "auto")
        self.assertEqual(args["strength"], 1.0)

    def test_values(self):
        args = engine.validate_args({"clips": [1, 3], "match": 3, "look": "Warm Print", "strength": 60, "camera": "S-Log3"})
        self.assertEqual((args["clips"], args["match"], args["look"], args["strength"], args["camera"]),
                         ([1, 3], 3, "warm-print", 0.6, "slog3"))

    def test_refusals(self):
        for bad in ({"action": "paint"}, {"look": "Kodak 2383"}, {"strength": 300}, {"clips": [0]}, {"match": "3"},
                    {"camera": "red"}, {"action": "look"}, {"contrast": 5}):
            with self.assertRaises(CommandError, msg=bad):
                engine.validate_args(bad)

    def test_describe_says_what_and_what_it_costs(self):
        title, lines = engine.describe(engine.validate_args({"match": 3, "look": "warm-print", "strength": 0.6}))
        text = " ".join(lines)
        self.assertIn("matched to clip 3", text)
        self.assertIn("Warm Print at 60%", text)
        self.assertIn("no NOLGIA credits", text)
        self.assertIn("Versions menu", text)

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
        self.item.nodes[i - 1]["on"] = on
        return True

    def SetLUT(self, i, path):
        self.item.nodes[i - 1]["params"][drx.LUT_PATH] = path.replace("/", "\\")
        return True


class FakeItem(object):
    def __init__(self, name, start, end, gains, stops):
        self.name, self.start, self.end, self.gains, self.stops = name, start, end, gains, stops
        self.nodes = []
        self.versions = ["Version 1"]
        self.current = "Version 1"

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

    def frame(self):
        """The clip as exported: scene in DI, through the enabled BALANCE and EXPOSURE offsets."""
        pixels = scene(self.gains, self.stops)
        add = [0.0, 0.0, 0.0]
        for node in self.nodes:
            if not node["on"]:
                continue
            for k, pid in enumerate(drx.OFFSET):
                add[k] += node["params"].get(pid, 0.0) * drx.OFFSET_CODE
        return [[v + a for v, a in zip(p, add)] for p in pixels]


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
        write_tiff(path, 64, 36, self.tl.at().frame())
        return True

    def current_item(self, tl):
        return tl.at()

    # project
    def GetName(self):
        return "Rooftop"

    def GetSetting(self, key):
        return "davinciYRGB" if key == "colorScienceMode" else ""

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
        args = engine.validate_args({"match": 3, "look": "warm-print", "strength": 0.6})
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
            self.assertEqual(look, "NOLGIA Grade\\Looks\\Print\\Warm Print 60%.dctl")
        self.assertTrue(os.path.isfile(os.path.join(self.root, "NOLGIA Grade", "Looks", "Print", "Warm Print.cube")))
        self.assertEqual(result["cost"], "free: it ran in DaVinci Resolve")

    def test_a_second_grade_keeps_the_first(self):
        engine.Grader(self.ops).run(engine.validate_args({"clips": [3], "correct": False}))
        engine.Grader(self.ops).run(engine.validate_args({"clips": [3], "correct": False}))
        self.assertEqual(self.items[2].versions, ["Version 1", "NOLGIA grade", "NOLGIA grade 2"])

    def test_change_look_only(self):
        engine.Grader(self.ops).run(engine.validate_args({"clips": [1], "look": "warm-print"}))
        out = engine.Grader(self.ops).run(engine.validate_args({"action": "look", "clips": [1], "look": "bw-pan-400",
                                                                "strength": 0.3}))
        self.assertEqual(len(out["changed"]), 1)
        self.assertEqual(self.items[0].nodes[6]["params"][drx.LUT_PATH],
                         "NOLGIA Grade\\Looks\\Black and white\\B&W Pan 400 30%.dctl")

    def test_unknown_camera_asks(self):
        self.items[0].name = "interview.mov"
        with self.assertRaises(CommandError) as caught:
            engine.Grader(self.ops).run(engine.validate_args({}))
        self.assertIn("needs_camera", caught.exception.result)

    def test_looks_lists_the_library(self):
        out = engine.Grader(self.ops).run(engine.validate_args({"action": "looks"}))
        self.assertEqual(len(out["looks"]), 24)


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
        self.assertEqual(len(tree.rows), 25)
        tree.__dict__["current"] = 3  # Warm Print
        window.items["Strength"].Value = 60
        window.items["Hero"].Value = 3
        args = engine.validate_args(window.args("apply"))
        self.assertEqual((args["look"], args["strength"], args["match"]), ("warm-print", 0.6, 3))


if __name__ == "__main__":
    unittest.main()
