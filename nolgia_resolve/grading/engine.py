# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 NOLGIA Inc
"""The `grade` command: colour managed, corrected, matched and graded clips, as
labelled nodes a colourist can keep working on.

Every graded clip gets a new local color version (NOLGIA grade, NOLGIA grade 2,
...), so the person's own grade stays in the clip's Versions menu, and in it
this node tree, applied with Graph.ApplyGradeFromDRX:

    01 CST IN    camera log to DaVinci Wide Gamut / Intermediate (a DCTL)
    02 BALANCE   white balance: the Offset wheel, per channel (CDL offset, in log)
    03 EXPOSURE  exposure: the Offset wheel, all channels (one stop = 0.0733 in DI)
    04 CONTRAST  Primaries Contrast around DI mid grey (pivot 0.336)
    05 SAT       Primaries Saturation
    06 SKIN      the skin line DCTL
    07 LOOK      a look from the NOLGIA library at a strength (a DCTL around its LUT)
    08 CST OUT   DWG/DI to Rec.709 gamma 2.4 (a DCTL)

Balance and exposure are measured from the clip's own frames, in scene light,
after CST IN: the white of the least saturated bright pixels, and the key
(the mid-tones' geometric mean brightness). Matching moves each clip's white
and level to the hero clip's.
"""

import math
import os
import shutil
import tempfile
import time

from ..core.commands import CommandError
from . import colour as C
from . import drx
from . import library
from . import looks as L
from . import measure as M

ACTIONS = ("looks", "inspect", "apply", "look")
CLIP_SCOPES = ("all", "selected", "current")
CAMERA_CHOICES = tuple(C.CAMERAS) + ("rec709",)
EXPOSURE_MODES = ("auto", "match", "even", "keep")
LABELS = ("CST IN", "BALANCE", "EXPOSURE", "CONTRAST", "SAT", "SKIN", "LOOK", "CST OUT")
VERSION_PREFIX = "NOLGIA grade"
PIVOT_DI = 0.336
MAX_BALANCE_STOPS = 1.5
MAX_EXPOSURE_STOPS = 2.0


def call(obj, method, *args, **kw):
    default = kw.get("default")
    if obj is None:
        return default
    fn = getattr(obj, method, None)
    if fn is None:
        return default
    try:
        value = fn(*args)
    except Exception:
        return default
    return default if value is None else value


# ------------------------------------------------------------------ arguments

def _number(args, key, lo, hi, default=None):
    value = args.get(key)
    if value is None or value == "":
        return default
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise CommandError("`%s` must be a number from %s to %s." % (key, lo, hi))
    if not lo <= value <= hi:
        raise CommandError("`%s` must be from %s to %s." % (key, lo, hi))
    return value


def validate_args(args):
    """The `grade` command's arguments, checked and with defaults filled in."""
    args = args or {}
    action = str(args.get("action") or "apply").strip().lower()
    if action not in ACTIONS:
        raise CommandError("`action` must be one of: %s." % ", ".join(ACTIONS))
    out = {"action": action}
    clips = args.get("clips", "all")
    if isinstance(clips, (list, tuple)):
        if not clips or len(clips) > 500 or not all(isinstance(c, int) and not isinstance(c, bool) and c >= 1 for c in clips):
            raise CommandError("`clips` is \"all\", \"selected\", \"current\" or a list of clip numbers (1 is the "
                               "first clip on the timeline, as the Color page numbers them).")
        out["clips"] = list(clips)
    else:
        clips = str(clips or "all").strip().lower()
        if clips not in CLIP_SCOPES:
            raise CommandError("`clips` is \"all\", \"selected\", \"current\" or a list of clip numbers.")
        out["clips"] = clips
    camera = str(args.get("camera") or "auto").strip().lower().replace("-", "").replace(" ", "")
    aliases = {"slog3": "slog3", "logc3": "logc3", "logc": "logc3", "logc4": "logc4", "vlog": "vlog", "clog2": "clog2",
               "canonlog2": "clog2", "clog3": "clog3", "canonlog3": "clog3", "bmdfilm5": "bmdfilm5",
               "bmdfilm": "bmdfilm5", "blackmagicfilm": "bmdfilm5", "applelog": "applelog", "rec709": "rec709",
               "rec.709": "rec709", "auto": "auto"}
    if camera not in aliases:
        raise CommandError("`camera` is auto or one of: %s." % ", ".join(CAMERA_CHOICES))
    out["camera"] = aliases[camera]
    out["correct"] = bool(args.get("correct", action == "apply"))
    match = args.get("match")
    if match not in (None, "", False):
        if isinstance(match, bool) or not isinstance(match, int) or match < 1:
            raise CommandError("`match` is the number of the hero clip to match the others to (1 is the first clip).")
        out["match"] = match
    else:
        out["match"] = None
    exposure = str(args.get("exposure") or "auto").strip().lower()
    if exposure not in EXPOSURE_MODES:
        raise CommandError("`exposure` is one of: %s." % ", ".join(EXPOSURE_MODES))
    out["exposure"] = exposure
    look = args.get("look")
    if look not in (None, ""):
        look = str(look).strip()
        if look.lower() in ("none", "off"):
            out["look"] = "none"
        else:
            found = L.find(look)
            if found is None:
                raise CommandError("There is no look called %r. Ask with action \"looks\" for the list." % look)
            out["look"] = found.slug
    else:
        out["look"] = None
    strength = args.get("strength")
    if strength is not None:
        strength = _number(args, "strength", 0, 100)
        if strength > 1.0:
            strength /= 100.0
    out["strength"] = 1.0 if strength is None else round(strength, 3)
    out["contrast"] = _number(args, "contrast", 0.5, 2.0, 1.0)
    out["saturation"] = _number(args, "saturation", 0.0, 2.0, 1.0)
    out["skin"] = bool(args.get("skin", True))
    out["neutral_hero"] = bool(args.get("neutral_hero", False))
    frames = _number(args, "frames", 1, 5, 3)
    out["frames"] = int(frames)
    if action == "look" and out["look"] is None:
        raise CommandError("action \"look\" needs `look` (a look from the library, or \"none\").")
    return out


def describe(args, caller="NOLGIA Agent"):
    """The lines a person reads before approving a grade."""
    scope = args["clips"]
    if isinstance(scope, list):
        what = "clip%s %s" % ("" if len(scope) == 1 else "s", ", ".join(str(c) for c in scope))
    else:
        what = {"all": "every clip on the current timeline", "selected": "the selected clips",
                "current": "the clip under the playhead"}[scope]
    lines = ["Clips: %s." % what]
    if args["action"] == "apply":
        cam = "found from each clip" if args["camera"] == "auto" else (
            C.CAMERAS[args["camera"]][0] if args["camera"] in C.CAMERAS else "Rec.709")
        lines.append("Colour management: CST IN from the camera (%s) to DaVinci Wide Gamut / Intermediate, CST OUT "
                     "to Rec.709." % cam)
        if args["correct"]:
            if args["match"]:
                lines.append("Correction: white balance and exposure measured from each clip, and matched to clip %d."
                             % args["match"])
            else:
                lines.append("Correction: white balance and exposure measured from each clip.")
        if args["contrast"] != 1.0 or args["saturation"] != 1.0:
            lines.append("Contrast %.2f, saturation %.2f." % (args["contrast"], args["saturation"]))
    if args["look"] and args["look"] != "none":
        look = L.BY_SLUG[args["look"]]
        lines.append("Look: %s at %d%% (NOLGIA's own film look library)." % (look.name, round(args["strength"] * 100)))
    elif args["look"] == "none":
        lines.append("Look: none (the LOOK node is switched off).")
    lines.append("Every step lands as labelled nodes you can adjust. Each clip gets a new color version, "
                 "\"NOLGIA grade\"; your own grade stays in the clip's Versions menu.")
    if "NOLGIA Agent" in (caller or ""):
        lines.append("The grading runs in DaVinci Resolve on this computer and costs no NOLGIA credits; the NOLGIA "
                     "Agent turn that asked for it is billed as usual.")
    else:
        lines.append("It runs in DaVinci Resolve on this computer and costs no NOLGIA credits.")
    title = "%s wants to grade clips in DaVinci Resolve" % caller
    return title, lines


# ------------------------------------------------------------------ camera

KEYWORDS = (
    ("logc4", ("logc4", "log c4", "logc 4", "awg4")),
    ("logc3", ("logc3", "log c3", "logc", "log-c", "awg3", "alexa")),
    ("slog3", ("s-log3", "slog3", "s-log 3", "slog 3", "sgamut3")),
    ("vlog", ("v-log", "vlog", "v-gamut")),
    ("clog3", ("c-log3", "clog3", "canon log 3", "c-log 3", "canonlog3")),
    ("clog2", ("c-log2", "clog2", "canon log 2", "c-log 2", "canonlog2")),
    ("bmdfilm5", ("blackmagic design film", "film gen 5", "bmdfilm", "bmd film", "blackmagic film")),
    ("applelog", ("apple log", "applelog")),
    ("rec709", ("rec709", "rec.709", "bt.709", "bt709")),
)


def camera_from_text(text):
    text = (text or "").lower()
    for camera, words in KEYWORDS:
        if any(w in text for w in words):
            return camera
    return None


def sony_sidecar(path):
    """The camera's own sidecar next to a Sony clip (NonRealTimeMeta XML)."""
    if not path:
        return ""
    stem, _ = os.path.splitext(path)
    for candidate in (stem + "M01.XML", stem + ".XML", stem + "M01.xml", stem + ".xml"):
        try:
            with open(candidate, "r", encoding="utf-8", errors="replace") as handle:
                return handle.read(200000)
        except OSError:
            continue
    return ""


def detect_camera(item):
    """(camera id or None, how it was found)."""
    mpi = call(item, "GetMediaPoolItem")
    props = call(mpi, "GetClipProperty", default={}) or {}
    if not isinstance(props, dict):
        props = {}
    # A log name anywhere counts first. "Input Color Space" reads Rec.709 (or Project) on every clip of a project
    # that is not colour managed, so Rec.709 only counts from the camera's own notes.
    for key in ("Gamma Notes", "Color Space Notes", "Input Color Space", "Camera Type", "Clip Name", "File Name"):
        camera = camera_from_text(str(props.get(key) or ""))
        if camera and camera != "rec709":
            return camera, "%s: %s" % (key, props.get(key))
    path = str(props.get("File Path") or "")
    side = sony_sidecar(path)
    if side:
        low = side.lower()
        if "s-log3" in low or "slog3" in low:
            return "slog3", "the Sony sidecar XML says S-Log3"
    if path.lower().endswith(".braw"):
        return "bmdfilm5", "a Blackmagic RAW clip, decoded to Blackmagic Film Gen 5"
    camera = camera_from_text(os.path.basename(path))
    if camera:
        return camera, "the file name %s" % os.path.basename(path)
    for key in ("Gamma Notes", "Color Space Notes"):
        if camera_from_text(str(props.get(key) or "")) == "rec709":
            return "rec709", "%s: %s" % (key, props.get(key))
    return None, "no camera log in the clip's metadata, sidecar or name"


# ------------------------------------------------------------------ the grader

class Grader(object):
    def __init__(self, ops, log=lambda message: None):
        self.ops = ops
        self.resolve = ops.resolve
        self.log = log

    # .......................................................... helpers

    def lut_root(self):
        folder = self.ops.lut_folder()  # <LUT>/NOLGIA, proven writable
        return os.path.dirname(folder)

    def project_science(self, project):
        mode = str(call(project, "GetSetting", "colorScienceMode", default="") or "")
        if mode.startswith("aces"):
            return "aces", mode
        if "ColorManaged" in mode:
            return "rcm", mode
        return "yrgb", mode or "davinciYRGB"

    def clips(self, tl, scope):
        items = []
        tracks = int(call(tl, "GetTrackCount", "video", default=0) or 0)
        for index in range(1, tracks + 1):
            for item in call(tl, "GetItemListInTrack", "video", index, default=[]) or []:
                items.append((call(item, "GetStart", default=0), index, item))
        items.sort(key=lambda t: (t[0], t[1]))
        numbered = [(n, item) for n, (_, _, item) in enumerate(items, start=1)]
        if isinstance(scope, list):
            wanted = set(scope)
            missing = [c for c in scope if c > len(numbered)]
            if missing:
                raise CommandError("The timeline has %d clip%s; there is no clip %s." % (
                    len(numbered), "" if len(numbered) == 1 else "s", ", ".join(str(c) for c in missing)))
            return [(n, item) for n, item in numbered if n in wanted]
        if scope == "selected":
            ids = set(call(i, "GetUniqueId") for i in call(tl, "GetSelectedClips", default=[]) or [])
            chosen = [(n, item) for n, item in numbered if call(item, "GetUniqueId") in ids]
            if not chosen:
                raise CommandError("No clips are selected in the timeline. Select clips, or use clips: \"all\".")
            return chosen
        if scope == "current":
            current = self.ops.current_item(tl)
            uid = call(current, "GetUniqueId")
            chosen = [(n, item) for n, item in numbered if call(item, "GetUniqueId") == uid]
            if not chosen:
                raise CommandError("There is no video clip under the playhead.")
            return chosen
        if not numbered:
            raise CommandError("The timeline %s has no video clips." % call(tl, "GetName"))
        return numbered

    def new_version(self, item):
        names = list(call(item, "GetVersionNameList", 0, default=[]) or [])
        name, n = VERSION_PREFIX, 2
        while name in names:
            name = "%s %d" % (VERSION_PREFIX, n)
            n += 1
        before = call(item, "GetCurrentVersion", default={}) or {}
        if not call(item, "AddVersion", name, 0, default=False):
            raise CommandError("DaVinci Resolve did not add a color version to %s." % call(item, "GetName"))
        if not call(item, "LoadVersionByName", name, 0, default=False):
            raise CommandError("DaVinci Resolve did not switch %s to its new version." % call(item, "GetName"))
        return name, before.get("versionName") or ""

    def apply_nodes(self, item, nodes, facts, folder, tag):
        path = os.path.join(folder, "%s.drx" % tag)
        drx.write(path, nodes, facts.get("width") or 1920, facts.get("height") or 1080)
        graph = call(item, "GetNodeGraph")
        if graph is None or not call(graph, "ApplyGradeFromDRX", path, 0, default=False):
            raise CommandError("DaVinci Resolve did not apply the NOLGIA node tree to %s." % call(item, "GetName"))
        return call(item, "GetNodeGraph")

    def read_graph(self, item):
        g = call(item, "GetNodeGraph")
        n = int(call(g, "GetNumNodes", default=0) or 0)
        return [{"node": i, "label": call(g, "GetNodeLabel", i, default=""), "lut": call(g, "GetLUT", i, default=""),
                 "tools": call(g, "GetToolsInNode", i, default=None) or []} for i in range(1, n + 1)]

    def frame_offsets(self, item, facts, count):
        start = int(call(item, "GetStart", default=facts["start"])) - facts["start"]
        end = int(call(item, "GetEnd", default=start + 1)) - facts["start"]
        length = max(1, end - start)
        if count == 1:
            return [start + length // 2]
        return [start + int(length * (i + 1) / (count + 1)) for i in range(count)]

    def still_stats(self, project, tl, facts, offsets, folder):
        stats = []
        for k, offset in enumerate(offsets):
            path = os.path.join(folder, "m%d.tif" % k)
            target = self.ops.offset_timecode(facts, offset)
            if not self.ops.set_playhead(tl, target):
                continue
            if not self.ops._export_still(project, path):
                continue
            try:
                stats.append(M.measure(M.read_tiff_grid(path)))
            finally:
                try:
                    os.remove(path)
                except OSError:
                    pass
        return M.combine(stats) if stats else {"ok": False, "why": "DaVinci Resolve exported no frame"}

    def tree(self, camera, science, balance=(0, 0, 0), exposure=0.0, args=None, look_path=None):
        args = args or {}
        managed = science != "yrgb"
        unit = C.DI_STOP / drx.OFFSET_CODE
        cst_in = None if managed else library.rel(library.CAMERA_FILES[camera])
        cst_out = None if managed else library.rel(library.CST_OUT_FILE)
        return [
            drx.Node(LABELS[0], lut=cst_in),
            drx.Node(LABELS[1], offset=[b * unit for b in balance]),
            drx.Node(LABELS[2], offset=[exposure * unit] * 3),
            drx.Node(LABELS[3], contrast=args.get("contrast", 1.0), pivot=PIVOT_DI),
            drx.Node(LABELS[4], saturation=args.get("saturation", 1.0)),
            # The skin DCTL works in DWG/DI: an ACES project's nodes are in ACEScct, so SKIN stays empty there.
            drx.Node(LABELS[5], lut=library.rel(library.SKIN_FILE) if args.get("skin", True) and science != "aces" else None),
            drx.Node(LABELS[6], lut=look_path),
            drx.Node(LABELS[7], lut=cst_out),
        ]

    # .......................................................... actions

    def looks(self):
        resolve_own = []
        for base in self.ops_lut_dirs():
            folder = os.path.join(base, "Film Looks")
            if os.path.isdir(folder):
                resolve_own = sorted(f for f in os.listdir(folder) if f.lower().endswith(".cube"))
                break
        return {
            "looks": [l.as_dict() for l in L.LOOKS],
            "families": L.FAMILIES,
            "strength": "0 to 1 (or a percent); the LOOK node holds the look at that strength, and the same folder has "
                        "10% steps to pick from Resolve's LUT menu.",
            "resolve_film_looks": {"files": resolve_own, "note": "DaVinci Resolve's own Film Looks LUTs, on this "
                                   "computer. They expect Cineon log in and ship with Resolve; NOLGIA only points a "
                                   "node at them, never copies them."},
            "cameras": {k: v[0] for k, v in C.CAMERAS.items()},
        }

    def ops_lut_dirs(self):
        from .. import paths
        return paths.lut_dirs()

    def inspect(self, args):
        project = self.ops.project()
        tl = self.ops.timeline(project)
        science, mode = self.project_science(project)
        out = []
        for n, item in self.clips(tl, args["clips"]):
            camera, why = detect_camera(item)
            version = call(item, "GetCurrentVersion", default={}) or {}
            out.append({"clip": n, "name": call(item, "GetName"), "camera": camera, "camera_found_by": why,
                        "version": version.get("versionName"), "nodes": self.read_graph(item)})
        return {"project": call(project, "GetName"), "timeline": call(tl, "GetName"), "color_science": mode,
                "managed": science != "yrgb", "clips": out}

    def run(self, args):
        if args["action"] == "looks":
            return self.looks()
        if args["action"] == "inspect":
            return self.inspect(args)
        page = call(self.resolve, "GetCurrentPage")
        if page != "color":
            call(self.resolve, "OpenPage", "color")
        try:
            if args["action"] == "look":
                return self.change_look(args)
            return self.apply(args)
        finally:
            if page and page != "color":
                call(self.resolve, "OpenPage", page)

    def look_path(self, root, args, science):
        if not args["look"] or args["look"] == "none":
            return None
        space = "acescct" if science == "aces" else "dwg"
        return library.install_look(root, L.BY_SLUG[args["look"]], args["strength"], space)

    def change_look(self, args):
        project = self.ops.project()
        tl = self.ops.timeline(project)
        science, _ = self.project_science(project)
        root = self.lut_root()
        path = self.look_path(root, args, science)
        call(project, "RefreshLUTList")
        done, skipped = [], []
        for n, item in self.clips(tl, args["clips"]):
            g = call(item, "GetNodeGraph")
            count = int(call(g, "GetNumNodes", default=0) or 0)
            node = next((i for i in range(1, count + 1) if call(g, "GetNodeLabel", i, default="") == "LOOK"), None)
            if node is None:
                skipped.append({"clip": n, "name": call(item, "GetName"), "why": "no node labelled LOOK (grade it first)"})
                continue
            if path is None:
                ok = call(g, "SetNodeEnabled", node, False, default=False)
            else:
                call(g, "SetNodeEnabled", node, True)
                ok = call(g, "SetLUT", node, path, default=False)
            (done if ok else skipped).append({"clip": n, "name": call(item, "GetName"), "node": node} if ok else
                                             {"clip": n, "name": call(item, "GetName"), "why": "SetLUT returned false"})
        self.ops.changed = True
        return {"look": args["look"], "strength": args["strength"], "lut": path, "changed": done, "skipped": skipped}

    def apply(self, args):
        started = time.time()
        project = self.ops.project()
        tl = self.ops.timeline(project)
        facts = self.ops.timeline_facts(tl)
        science, mode = self.project_science(project)
        clips = self.clips(tl, args["clips"])
        if args["match"] and args["match"] not in [n for n, _ in clips]:
            clips_all = self.clips(tl, "all")
            hero = [(n, i) for n, i in clips_all if n == args["match"]]
            if not hero:
                raise CommandError("There is no clip %d on the timeline to match to." % args["match"])
            clips = sorted(clips + hero, key=lambda t: t[0])
        cameras = {}
        unknown = []
        for n, item in clips:
            if args["camera"] != "auto":
                cameras[n] = (args["camera"], "given")
            else:
                cam, why = detect_camera(item)
                if cam is None:
                    unknown.append({"clip": n, "name": call(item, "GetName"), "why": why})
                cameras[n] = (cam, why)
        if unknown and science == "yrgb":
            raise CommandError(
                "NOLGIA could not tell which camera log %s recorded in (%s). Say which with `camera`: %s."
                % (", ".join("clip %d (%s)" % (u["clip"], u["name"]) for u in unknown[:6]), unknown[0]["why"],
                   ", ".join(CAMERA_CHOICES)), result={"needs_camera": unknown, "choices": list(CAMERA_CHOICES)})
        root = self.lut_root()
        library.install_base(root)
        look_path = self.look_path(root, args, science)
        call(project, "RefreshLUTList")
        folder = tempfile.mkdtemp(prefix="nolgia-grade-")
        before_tc = call(tl, "GetCurrentTimecode")
        results = {}
        try:
            stats = {}
            for n, item in clips:
                version, previous = self.new_version(item)
                results[n] = {"clip": n, "name": call(item, "GetName"), "camera": cameras[n][0],
                              "camera_found_by": cameras[n][1], "version": version, "your_version": previous}
                camera = cameras[n][0] or "rec709"
                if args["correct"] and science == "yrgb":
                    self.apply_nodes(item, self.tree(camera, science, args=args), facts, folder, "measure%d" % n)
                    g = call(item, "GetNodeGraph")
                    for i in range(4, 9):
                        call(g, "SetNodeEnabled", i, False)
                    stats[n] = self.still_stats(project, tl, facts, self.frame_offsets(item, facts, args["frames"]),
                                                folder)
            corrections = self.corrections(clips, stats, args) if stats else {}
            for n, item in clips:
                camera = cameras[n][0] or "rec709"
                bal, exp, matched_on = corrections.get(n, ((0.0, 0.0, 0.0), 0.0, {}))
                self.apply_nodes(item, self.tree(camera, science, bal, exp, args, look_path), facts, folder, "grade%d" % n)
                results[n]["balance_stops"] = [round(b, 3) for b in bal]
                results[n]["exposure_stops"] = round(exp, 3)
                if matched_on:
                    results[n]["matched_on"] = matched_on
                if n in stats and stats[n].get("ok"):
                    st = stats[n]
                    results[n]["measured"] = {"white_cast_stops": [round(c, 3) for c in M.cast_stops(st["white"])],
                                              "key": round(st["key"], 4), "white_level": round(st["white_y"], 4),
                                              "skin_share": round(st.get("skin_share", 0.0), 3)}
                    if "skin" in st:
                        results[n]["measured"]["skin_cast_stops"] = [round(c, 3) for c in M.cast_stops(st["skin"])]
                        results[n]["measured"]["skin_level"] = round(st["skin_y"], 4)
                elif n in stats:
                    results[n]["measured"] = {"skipped": stats[n].get("why")}
            if corrections:
                self.verify(clips, project, tl, facts, folder, results, args, corrections, cameras, science, look_path)
            for n, item in clips:
                results[n]["nodes"] = [{"node": d["node"], "label": d["label"], "lut": d["lut"]} for d in self.read_graph(item)]
        finally:
            if before_tc:
                self.ops.set_playhead(tl, before_tc)
            shutil.rmtree(folder, ignore_errors=True)
        self.ops.changed = True
        out = {"project": call(project, "GetName"), "timeline": call(tl, "GetName"), "color_science": mode,
               "clips": [results[n] for n, _ in clips], "hero": args["match"], "seconds": round(time.time() - started, 1),
               "look": args["look"], "strength": args["strength"] if args["look"] not in (None, "none") else None,
               "lut_folder": os.path.join(root, library.SUBFOLDER), "cost": "free: it ran in DaVinci Resolve"}
        if science == "rcm":
            space = str(call(project, "GetSetting", "colorSpaceTimeline", default="") or "")
            if space and "DaVinci WG" not in space and "DaVinci Wide Gamut" not in space:
                out["warning"] = ("The timeline colour space is %s. NOLGIA's looks are made for DaVinci WG/Intermediate: "
                                  "set it in Project Settings > Color Management, or use a project that is not colour "
                                  "managed, for the looks to look as they should." % space)
        if science != "yrgb":
            out["note"] = ("The project is colour managed (%s), so CST IN and CST OUT are left to Resolve and "
                           "correction was not measured; the look matches the project's working space%s." % (
                               mode, " (ACEScct), and SKIN is left empty, as it works in DaVinci Wide Gamut"
                               if science == "aces" else ""))
        return out

    def corrections(self, clips, stats, args, prefer=None):
        """{clip: ((r, g, b) balance stops, exposure stops, what it matched on)}.

        Colour: the clip's white (the least saturated bright pixels) and its skin (found after the white's cast is
        taken out, so a cast cannot change which pixels count), averaged when both are there. Exposure: the median
        of what white, skin and key each say, so one of them caught out by the framing cannot decide alone.
        Matching takes each clip's colour and level to the hero's; the hero keeps its own unless neutral_hero."""
        good = {n: s for n, s in stats.items() if s.get("ok")}
        hero = args["match"] if args["match"] in good else None
        mode = args["exposure"]
        if mode == "auto":
            mode = "match" if hero else "even"
        clamp = lambda v, m: max(-m, min(m, v))

        def casts(s):
            out = {"white": M.cast_stops(s["white"])}
            if "skin" in s:
                out["skin"] = M.cast_stops(s["skin"])
            return out

        def levels(s):
            out = {"white": math.log2(max(s["white_y"], 1e-6)), "key": math.log2(max(s["key"], 1e-6))}
            if "skin" in s:
                out["skin"] = math.log2(max(s["skin_y"], 1e-6))
            return out

        out = {}
        if hero:
            h = good[hero]
            hero_bal = M.cast_stops(h["white"]) if args["neutral_hero"] else [0.0, 0.0, 0.0]
        whites = sorted(levels(s)["white"] for s in good.values())
        median_white = whites[len(whites) // 2] if whites else 0.0
        for n, _ in clips:
            s = good.get(n)
            if s is None:
                continue
            if hero and n != hero:
                mine, theirs = casts(s), casts(good[hero])
                keys = [k for k in ("white", "skin") if k in mine and k in theirs]
                bal = [sum(mine[k][c] - theirs[k][c] for k in keys) / len(keys) + hero_bal[c] for c in range(3)]
                on = "+".join(keys)
            elif hero:
                bal, on = hero_bal, "hero (kept)" if not args["neutral_hero"] else "hero (white made neutral)"
            else:
                bal, on = M.cast_stops(s["white"]), "white"
            bal = [clamp(b, MAX_BALANCE_STOPS) for b in bal]
            if mode == "keep" or (hero and n == hero):
                exp, lon = 0.0, "kept"
            elif mode == "match" and hero:
                exp, lon = self.level_match(s, good[hero], (prefer or {}).get(n))
                exp = clamp(exp, MAX_EXPOSURE_STOPS)
            else:  # even: halfway to the clips' median white level, at most a stop
                exp, lon = clamp(0.5 * (median_white - levels(s)["white"]), 1.0), "white, halfway to the clips' median"
            out[n] = (bal, exp, {"colour": on, "exposure": lon})
        return out

    @staticmethod
    def level_match(s, hero, prefer=None):
        """Stops that take clip s to the hero's level. The white leads (bright neutral things are the steadiest
        across framings); skin is averaged in when it agrees within half a stop; the key only when neither white
        is usable, and never when the frame is mostly near black (night), where it reads noise."""
        white_ok = s["white_saturation"] < 0.25 and hero["white_saturation"] < 0.25
        white = math.log2(max(hero["white_y"], 1e-6) / max(s["white_y"], 1e-6))
        skin = None
        if "skin_y" in s and "skin_y" in hero:
            skin = math.log2(hero["skin_y"] / s["skin_y"])
        if prefer:  # the check after a pass measures the way the pass did
            if prefer == "white and skin" and skin is not None:
                return (white + skin) / 2.0, prefer
            if prefer == "skin" and skin is not None:
                return skin, prefer
            if prefer == "key":
                return math.log2(hero["key"] / s["key"]), prefer
            return white, prefer
        if white_ok:
            if skin is not None and abs(skin - white) <= 0.5:
                return (white + skin) / 2.0, "white and skin"
            return white, "white"
        if skin is not None:
            return skin, "skin"
        if min(s["key"], hero["key"]) > 0.01:
            return math.log2(hero["key"] / s["key"]), "key"
        return white, "white (low confidence)"

    def verify(self, clips, project, tl, facts, folder, results, args, corrections, cameras, science, look_path):
        """Measure each clip again through CST IN, BALANCE and EXPOSURE; correct once more what is left."""
        after = {}
        for n, item in clips:
            if n not in corrections:
                continue
            g = call(item, "GetNodeGraph")
            for i in range(4, 9):
                call(g, "SetNodeEnabled", i, False)
            after[n] = self.still_stats(project, tl, facts, self.frame_offsets(item, facts, args["frames"]), folder)
            for i in range(4, 9):
                call(g, "SetNodeEnabled", i, True)
        target = self.corrections(clips, {n: s for n, s in after.items()}, args,
                                  prefer={n: c[2].get("exposure") for n, c in corrections.items()})
        hero = args["match"]
        for n, item in clips:
            if n not in target or n not in corrections:
                continue
            residual_bal, residual_exp, _ = target[n]
            if not (hero and args["exposure"] in ("auto", "match")):
                residual_exp = 0.0  # "even" and "keep" set exposure once; only a match has a target to settle on
            if hero and n == hero:
                residual_bal, residual_exp = ((0.0, 0.0, 0.0) if not args["neutral_hero"] else residual_bal), 0.0
            bal, exp, _ = corrections[n]
            results[n]["residual_after_first_pass"] = {"balance_stops": [round(b, 3) for b in residual_bal],
                                                       "exposure_stops": round(residual_exp, 3),
                                                       "matched_on": target[n][2]}
            if after.get(n, {}).get("ok"):
                st = after[n]
                results[n]["residual_after_first_pass"]["measured"] = {
                    "white_level": round(st["white_y"], 4), "white_saturation": round(st["white_saturation"], 3),
                    "skin_level": round(st.get("skin_y", 0), 4), "key": round(st["key"], 4)}
            if max(abs(b) for b in residual_bal) > 0.03 or abs(residual_exp) > 0.03:
                bal = [max(-MAX_BALANCE_STOPS, min(MAX_BALANCE_STOPS, b + r)) for b, r in zip(bal, residual_bal)]
                exp = max(-MAX_EXPOSURE_STOPS, min(MAX_EXPOSURE_STOPS, exp + residual_exp))
                camera = cameras[n][0] or "rec709"
                self.apply_nodes(item, self.tree(camera, science, bal, exp, args, look_path), facts, folder, "fix%d" % n)
                results[n]["balance_stops"] = [round(b, 3) for b in bal]
                results[n]["exposure_stops"] = round(exp, 3)
                results[n]["second_pass"] = True
