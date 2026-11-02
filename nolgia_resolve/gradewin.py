# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 NOLGIA Inc
"""The "NOLGIA Grade" window: the film look library and one-click correction, run
by the person in this DaVinci Resolve (no agent, no NOLGIA credits).

It runs the same `grade` command an agent sends (grading/engine.py), on
Resolve's main thread, from the person's own click. A click that waited while
Resolve was busy does nothing, as everywhere in the NOLGIA window.
"""

import time
import uuid

from .core.commands import CommandError
from .grading import colour as C
from .grading import engine, looks

GRADE_ID = "com.nolgia.resolve.grade"
NO_FOCUS = {"NoFocus": True}
CAMERAS = [("auto", "Auto (from each clip)")] + [(k, v[0]) for k, v in C.CAMERAS.items()] + [("rec709", "Rec.709 (not log)")]
SCOPES = [("all", "All clips on the timeline"), ("selected", "Selected clips"), ("current", "The clip under the playhead")]
NO_LOOK = "No look (correction only)"
FOOT = ("Runs in DaVinci Resolve on this computer: no NOLGIA credits. Each clip gets a new color version, "
        "NOLGIA grade, with labelled nodes you can adjust; your own grade stays in the clip's Versions menu.")


class GradeWindow(object):
    def __init__(self, panel):
        self.panel = panel
        self.ui = panel.ui
        self.disp = panel.disp
        self.win = None

    def show(self):
        if self.win is None:
            self.build()
        self.win.Show()
        try:
            self.win.Raise()
        except Exception:
            pass

    def hide(self):
        if self.win is not None:
            try:
                self.win.Hide()
            except Exception:
                pass

    def build(self):
        ui = self.ui
        bold = ui.Font({"Bold": True})
        layout = ui.VGroup({"Spacing": 6}, [
            ui.Label({"Text": "Film look library", "Font": bold, "Weight": 0}),
            ui.Tree({"ID": "Looks", "Weight": 1, "RootIsDecorated": False, "AlternatingRowColors": True,
                     "SelectionMode": "SingleSelection", "FocusPolicy": NO_FOCUS}),
            ui.HGroup({"Weight": 0}, [
                ui.Label({"Text": "Strength", "Weight": 0, "MinimumSize": [90, 0]}),
                ui.Slider({"ID": "Strength", "Minimum": 0, "Maximum": 100, "Value": 100, "Orientation": "Horizontal",
                           "FocusPolicy": NO_FOCUS}),
                ui.Label({"ID": "StrengthValue", "Text": "100%", "Weight": 0, "MinimumSize": [44, 0]}),
            ]),
            ui.Label({"Text": "Correction", "Font": bold, "Weight": 0}),
            ui.HGroup({"Weight": 0}, [
                ui.Label({"Text": "Camera", "Weight": 0, "MinimumSize": [90, 0]}),
                ui.ComboBox({"ID": "Camera", "FocusPolicy": NO_FOCUS}),
            ]),
            ui.HGroup({"Weight": 0}, [
                ui.Label({"Text": "Clips", "Weight": 0, "MinimumSize": [90, 0]}),
                ui.ComboBox({"ID": "Clips", "FocusPolicy": NO_FOCUS}),
            ]),
            ui.HGroup({"Weight": 0}, [
                ui.Label({"Text": "Match to clip", "Weight": 0, "MinimumSize": [90, 0]}),
                ui.SpinBox({"ID": "Hero", "Minimum": 0, "Maximum": 999, "Value": 0, "FocusPolicy": NO_FOCUS,
                            "SpecialValueText": "none (balance each clip)"}),
            ]),
            ui.HGroup({"Weight": 0}, [
                ui.CheckBox({"ID": "Correct", "Text": "Balance and expose each clip", "Checked": True,
                             "FocusPolicy": NO_FOCUS}),
                ui.CheckBox({"ID": "Skin", "Text": "Skin line", "Checked": True, "FocusPolicy": NO_FOCUS}),
            ]),
            ui.HGroup({"Weight": 0}, [
                ui.Button({"ID": "Apply", "Text": "Correct and grade", "FocusPolicy": NO_FOCUS}),
                ui.Button({"ID": "LookOnly", "Text": "Change look only", "FocusPolicy": NO_FOCUS}),
                ui.Button({"ID": "InstallAll", "Text": "Add all looks to the LUT list", "FocusPolicy": NO_FOCUS}),
            ]),
            ui.Label({"ID": "GradeStatus", "Text": "Pick a look, or none, then Correct and grade.", "WordWrap": True,
                      "Weight": 0, "MinimumSize": [0, 54]}),
            ui.Label({"Text": FOOT, "WordWrap": True, "Weight": 0, "MinimumSize": [0, 54]}),
        ])
        self.win = self.disp.AddWindow({"ID": GRADE_ID, "WindowTitle": "NOLGIA Grade",
                                        "Geometry": [580, 80, 620, 760], "MinimumSize": [520, 600]}, layout)
        items = self.items = self.win.GetItems()
        tree = items["Looks"]
        tree.ColumnCount = 3
        tree.SetHeaderLabels(["Family", "Look", "What it does"])
        for column, width in enumerate((110, 150, 320)):
            tree.ColumnWidth[column] = width
        row = tree.NewItem()
        row.Text[0] = ""
        row.Text[1] = NO_LOOK
        row.Text[2] = "Colour management and correction, no look."
        tree.AddTopLevelItem(row)
        for look in looks.LOOKS:
            row = tree.NewItem()
            row.Text[0] = look.family
            row.Text[1] = look.name
            row.Text[2] = look.description
            row.ToolTip[2] = look.description
            tree.AddTopLevelItem(row)
        for _key, label in CAMERAS:
            items["Camera"].AddItem(label)
        for _key, label in SCOPES:
            items["Clips"].AddItem(label)
        on = self.win.On
        click = self.panel._click
        on.Strength.ValueChanged = self.panel._guard(self._on_strength)
        on.Apply.Clicked = click("Apply", lambda ev: self._run("apply"))
        on.LookOnly.Clicked = click("LookOnly", lambda ev: self._run("look"))
        on.InstallAll.Clicked = click("InstallAll", lambda ev: self._install_all())
        on[GRADE_ID].Close = self.panel._guard(lambda ev: self.hide())
        return self.win

    # ......................................................... reading the form

    def _on_strength(self, ev):
        self.items["StrengthValue"].Text = "%d%%" % int(self.items["Strength"].Value)

    def selected_look(self):
        row = self.items["Looks"].CurrentItem()
        if row is None:
            return None
        name = row.Text[1]
        if name == NO_LOOK:
            return "none"
        found = looks.find(name)
        return found.slug if found else None

    def args(self, action):
        it = self.items
        out = {"action": action,
               "clips": SCOPES[max(0, int(it["Clips"].CurrentIndex))][0],
               "camera": CAMERAS[max(0, int(it["Camera"].CurrentIndex))][0],
               "correct": bool(it["Correct"].Checked),
               "skin": bool(it["Skin"].Checked),
               "strength": int(it["Strength"].Value) / 100.0}
        hero = int(it["Hero"].Value)
        if hero:
            out["match"] = hero
        look = self.selected_look()
        if look:
            out["look"] = look
        elif action == "look":
            raise CommandError("Pick a look in the list first (or No look).")
        return out

    # ......................................................... running

    def _status(self, text):
        self.items["GradeStatus"].Text = text

    def _run(self, action):
        ops = self.panel.controller.ops
        activity = self.panel.controller.activity
        record = "local-" + uuid.uuid4().hex[:8]
        try:
            args = engine.validate_args(self.args(action))
        except CommandError as err:
            self._status(str(err))
            return
        activity.add(record, "grade", "you")
        activity.update(record, "running", "Correct and grade" if action == "apply" else "Change look")
        self._status("Grading... DaVinci Resolve is busy for a few seconds per clip.")
        try:
            self.win.RecalcLayout()
        except Exception:
            pass
        started = time.time()
        try:
            result = engine.Grader(ops, self.panel.log).run(args)
        except CommandError as err:
            activity.update(record, "failed", str(err)[:200])
            self._status(str(err))
            self.panel.log("Grade from the NOLGIA Grade window failed: %s" % err)
            return
        except Exception as err:  # shown, logged, never raised into Resolve
            activity.update(record, "failed", str(err)[:200])
            self._status("DaVinci Resolve raised an error: %s" % err)
            self.panel.log("Grade from the NOLGIA Grade window raised: %r" % err)
            return
        text = summary(result, action, time.time() - started)
        activity.update(record, "succeeded", text[:200])
        self._status(text)
        self.panel.log("Grade from the NOLGIA Grade window: %s" % text)

    def _install_all(self):
        from .grading import library
        try:
            root = engine.Grader(self.panel.controller.ops).lut_root()
            library.install_base(root)
            paths = library.install_all_looks(root)
            project = self.panel.controller.ops.project()
            project.RefreshLUTList()
        except CommandError as err:
            self._status(str(err))
            return
        self._status("Added %d looks (and their 10%% strength steps) to Resolve's LUT list, under NOLGIA Grade > "
                     "Looks." % len(paths))


def summary(result, action, seconds):
    if action == "look":
        n = len(result.get("changed") or [])
        look = looks.BY_SLUG.get(result.get("look") or "")
        what = "%s at %d%%" % (look.name, round((result.get("strength") or 1) * 100)) if look else "no look"
        text = "Set %s on %d clip%s." % (what, n, "" if n == 1 else "s")
        if result.get("skipped"):
            text += " Skipped %d: %s." % (len(result["skipped"]), result["skipped"][0].get("why"))
        return text
    clips = result.get("clips") or []
    look = looks.BY_SLUG.get(result.get("look") or "")
    parts = ["Graded %d clip%s in %.0f s" % (len(clips), "" if len(clips) == 1 else "s", seconds)]
    if result.get("hero"):
        parts.append("matched to clip %d" % result["hero"])
    if look:
        parts.append("%s at %d%%" % (look.name, round((result.get("strength") or 1) * 100)))
    text = ", ".join(parts) + "."
    moved = ["clip %d: exposure %+.2f stop" % (c["clip"], c.get("exposure_stops") or 0) for c in clips
             if abs(c.get("exposure_stops") or 0) >= 0.05]
    if moved:
        text += " " + "; ".join(moved[:4]) + "."
    if result.get("note"):
        text += " " + result["note"]
    return text
