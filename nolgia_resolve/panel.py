# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 NOLGIA Inc
"""The NOLGIA window inside DaVinci Resolve (NOLGIA in Workspace > Scripts),
built with Resolve's UIManager.

The window lives as long as the script: closing it switches NOLGIA off for
this Resolve. A UIManager timer calls Controller.tick() every 100 ms on the
script's thread, which runs the commands, and redraws what changed.
"""

import os
import sys
import time
import traceback

from . import chatview
from .core import AGENT_URL
from .core.commands import ActivityLog
from .core.worker import Status
from .runtime import Controller, env_flag

WINDOW_ID = "com.nolgia.resolve.panel"
REQUEST_ID = "com.nolgia.resolve.request"
TIMER_ID = "NolgiaTick"
TICK_MS = 50
# UIManager's RunLoop keeps Python's GIL most of the time, so NOLGIA's network
# and upload threads run only while the script itself runs Python. Measured in
# Resolve 21.1.1, a busy thread got about 21% of its speed with nothing given
# back, 46% when each 50 ms tick slept 5 ms and 73% with 25 ms. So each tick
# sleeps a little, and more while a command or a sign in is under way.
YIELD_IDLE = 0.005
YIELD_BUSY = 0.030
MAX_CODE_CHARS = 200000
FOOTER = "NOLGIA works in this DaVinci Resolve while this window is open and Connected is on."
# Sizing, measured in Resolve 21.1.1: a UIManager window takes the width of
# the widest Weight-0 child of its VGroup and cannot be dragged wider (its
# own MinimumSize width loses to that; the height is free). So the Status
# label's minimum width is what sets the window width, and it is wide
# enough for every status line to fit in two lines. Word-wrapped labels keep
# a one-line height unless given one, so the three that wrap get two lines
# of Resolve's UI font with some air.
CONTENT_WIDTH = 400
MIN_WIDTH = 420
# Tall enough for every part of the window, the NOLGIA Agent chat included:
# UIManager windows do not scroll as a whole, so nothing may need to. The
# chat's thread and the Activity list scroll on their own.
# Measured on Resolve 21.1.1 (Windows, 125%): the most the window shows at
# once (the first look with a notice, or a thread with a question's answers
# and a request waiting) fits in MIN_HEIGHT, which still fits a 13 inch
# laptop screen.
MIN_HEIGHT = 780
DEFAULT_HEIGHT = 860
# Resolve on macOS lets the window be dragged shorter than its MinimumSize
# (the owner's Mac tests, 2026-10-29: to about 646 pt, where the text box sat
# over the chat). Two things keep that from showing:
# - The parts that can give up height (the chat's thread, the text box and
#   the Activity list) have no hard minimum. A UIManager element with a
#   MinimumSize keeps it when its window is too short and spills over the
#   next one (the thread under the text box); without one it shrinks, and the
#   thread and Activity scroll on their own. Only labels keep a minimum (the
#   height of their two lines), so they never cut a word off.
# - Every tick the height is read and, when it is under MIN_HEIGHT, put back.
HEIGHT_CHECK_TICKS = 1
# Quitting Resolve (File > Quit, Cmd+Q, its main window's close button) while a
# script's window is open does not finish (the owner's Mac test 2, 2026-10-29;
# the same on Windows 11): Resolve saves and closes the project, opens a new
# Untitled Project, asks script windows to close, and gives the quit up because
# the script is still running then, leaving itself open with no windows. Only
# resolve.Quit() finishes a quit while a script runs. Quitting someone's
# Resolve by mistake is worse than that, so a Close counts as Resolve quitting
# only when all of these hold, and then, once the window is closed, NOLGIA
# finishes the quit with resolve.Quit():
# - the window first saw the current project change less than
#   QUIT_SWITCH_SECONDS ago (it reads the project when it opens and every
#   PROJECT_CHECK_TICKS ticks),
# - to a project this window has not seen before,
# - named UNTITLED_PROJECT, with no timelines, no clips and no bins,
# - not saved in the project library (the one Resolve opens while quitting
#   never is),
# - and, where that can be read (Windows), none of Resolve's own windows is
#   left on screen.
# Anything that cannot be read counts against quitting. On macOS a new
# project made with New Project still looks like that (it is not in the
# library until it is saved), so a NOLGIA window closed within
# QUIT_SWITCH_SECONDS of one opening would quit Resolve there.
#
# In Resolve Studio 21.1.1 on Windows 11 the window first saw Resolve's new
# Untitled Project 0.02 to 0.03 s before Resolve closed it. The window allows
# 0.5 s: a quit it misses only leaves Resolve open as before 0.2.3, a wrong
# quit is worse. The project is read on every tick (about 3 ms), so a change
# is seen within a tick of Resolve showing it. (While Resolve saves the
# project on a quit its ticks stop for about 1.6 s, so the last read of the
# old project tells nothing.)
PROJECT_CHECK_TICKS = 1
QUIT_SWITCH_SECONDS = 0.5
# A Close later than QUIT_SWITCH_SECONDS but within this many seconds of such
# a project opening logs the gap, so a quit the window missed shows why.
QUIT_NOTE_SECONDS = 10.0
UNTITLED_PROJECT = "Untitled Project"
STATUS_LINES_PX = 40
FOOTER_LINES_PX = 36
COST_LINES_PX = 54
BOX_PX = 56
ACTIVITY_MAX_PX = 220
# Button text sets a minimum width: kept short so no button widens the window.
EXAMPLE_CHARS = 44
OPTION_CHARS = 26
OPTIONS = 4

# The open window, for nolgia_resolve.close_window().
current = None


def _window_size(geometry):
    """(width, height) from a UIManager Geometry, which reads back as a list
    [x, y, width, height] or as the Lua table's dict {1: x, 2: y, 3: width,
    4: height}; None when it is neither."""
    try:
        if isinstance(geometry, dict):
            width, height = geometry.get(3, geometry.get(3.0)), geometry.get(4, geometry.get(4.0))
        else:
            width, height = geometry[2], geometry[3]
        return float(width), float(height)
    except (TypeError, ValueError, IndexError, KeyError):
        return None


def _window_origin(geometry):
    """(x, y) from a Geometry read like _window_size's."""
    if isinstance(geometry, dict):
        return float(geometry.get(1, geometry.get(1.0))), float(geometry.get(2, geometry.get(2.0)))
    return float(geometry[0]), float(geometry[1])


def find_open_window(fusion):
    """The NOLGIA window when it is already open (the script runs again)."""
    try:
        ui = fusion.UIManager
        win = ui.FindWindow(WINDOW_ID)
    except Exception:
        return None
    return win or None


class Panel:
    def __init__(self, resolve, fusion, bmd, controller=None, log=None):
        self.resolve = resolve
        self.fusion = fusion
        self.ui = fusion.UIManager
        self.disp = bmd.UIDispatcher(self.ui)
        self.controller = controller or Controller(resolve, has_window=True, log=log)
        self.controller.on_approval = self._approvals_changed
        self.log = self.controller.log
        self.win = None
        self.request_win = None
        self.request_shown = None
        self.timer = None
        self._drawn = None
        self._activity_drawn = -1
        self._thread_drawn = None
        self._scroll_to = None
        self._scroll_ticks = 0
        self._options = []
        self._box_text = ""
        self._box_quiet = False
        self._closed = False
        self._ticks = 0
        self._height_check_off = False
        self._pm = None
        self._project_seen = None
        self._projects_seen = set()
        self._project_changed_at = None
        self._project_is_new = False
        self._finish_quit = False
        global current
        current = self

    # ------------------------------------------------------------- layout

    def build(self):
        ui = self.ui
        big = ui.Font({"PointSize": 18, "Bold": True})
        bold = ui.Font({"Bold": True})
        layout = ui.VGroup({"Spacing": 6}, [
            ui.Label({"ID": "Status", "Text": "", "WordWrap": True, "Weight": 0, "Font": bold,
                      "MinimumSize": [CONTENT_WIDTH, STATUS_LINES_PX],
                      "Alignment": {"AlignTop": True, "AlignLeft": True}}),
            ui.Label({"ID": "Account", "Text": "", "WordWrap": True, "Weight": 0}),
            ui.Label({"ID": "Code", "Text": "", "Weight": 0, "Font": big, "Alignment": {"AlignHCenter": True}}),
            ui.Label({"ID": "CodeHint", "Text": "", "WordWrap": True, "Weight": 0,
                      "MinimumSize": [0, FOOTER_LINES_PX], "Alignment": {"AlignTop": True, "AlignLeft": True}}),
            ui.HGroup({"Weight": 0}, [
                ui.Button({"ID": "SignIn", "Text": "Sign in"}),
                ui.Button({"ID": "OpenPage", "Text": "Open page again"}),
                ui.Button({"ID": "CancelSignIn", "Text": "Cancel"}),
                ui.Button({"ID": "SignOut", "Text": "Sign out"}),
            ]),
            ui.CheckBox({"ID": "Connected", "Text": "Connected", "Weight": 0,
                         "ToolTip": "Let NOLGIA send commands to this DaVinci Resolve. Turn off to pause."}),
            ui.CheckBox({"ID": "AllowAgent", "Text": "Allow NOLGIA Agent", "Weight": 0,
                         "ToolTip": "Let your NOLGIA Agent in the cloud work here too, not only the agent apps "
                                    "you run yourself (Claude, ChatGPT and others)."}),
            ui.CheckBox({"ID": "AskBeforeRun", "Text": "Ask before running code", "Weight": 0,
                         "ToolTip": "Show each piece of Python NOLGIA wants to run and wait for you to approve it."}),
            ui.HGroup({"Weight": 0}, [
                ui.Label({"ID": "Waiting", "Text": "", "WordWrap": True}),
                ui.Button({"ID": "Review", "Text": "Show request", "Weight": 0}),
            ]),
            # The NOLGIA Agent chat: what the person types goes to their NOLGIA
            # Agent, which works in this Resolve (chat.py).
            ui.HGroup({"Weight": 0}, [
                ui.Label({"ID": "ChatTitle", "Text": "NOLGIA Agent", "Font": bold}),
                ui.HGap(0, 1),
                ui.Button({"ID": "NewChat", "Text": "New chat", "Weight": 0}),
            ]),
            ui.Label({"ID": "ChatIntro", "Text": "Tell NOLGIA Agent what to do in this DaVinci Resolve. Try one of these:",
                      "WordWrap": True, "Weight": 0, "MinimumSize": [0, FOOTER_LINES_PX],
                      "Alignment": {"AlignTop": True, "AlignLeft": True}}),
        ] + [
            ui.Button({"ID": "Example%d" % i, "Text": short, "Weight": 0, "ToolTip": full})
            for i, (short, full) in enumerate(chatview.EXAMPLES)
        ] + [
            ui.TextEdit({"ID": "Thread", "ReadOnly": True, "Weight": 3}),
            # A question's answers, two to a row (hidden while a notice shows:
            # the person can always type the answer instead).
            ui.HGroup({"Weight": 0}, [ui.Button({"ID": "Option0", "Text": ""}), ui.Button({"ID": "Option1", "Text": ""})]),
            ui.HGroup({"Weight": 0}, [ui.Button({"ID": "Option2", "Text": ""}), ui.Button({"ID": "Option3", "Text": ""})]),
            ui.Label({"ID": "ChatWorking", "Text": "", "WordWrap": True, "Weight": 0,
                      "MinimumSize": [0, FOOTER_LINES_PX], "Alignment": {"AlignTop": True, "AlignLeft": True}}),
            # One notice at a time, with the one click that helps: what keeps the
            # chat from sending (turn on Connected, Allow NOLGIA Agent) first,
            # else what NOLGIA answered (out of credits, no agent yet...).
            ui.Label({"ID": "ChatNotice", "Text": "", "WordWrap": True, "Weight": 0,
                      "MinimumSize": [0, FOOTER_LINES_PX], "Alignment": {"AlignTop": True, "AlignLeft": True}}),
            ui.Button({"ID": "ChatAction", "Text": "", "Weight": 0}),
            ui.TextEdit({"ID": "Box", "Weight": 0, "PlaceholderText": "Ask NOLGIA Agent to make or change something here",
                         "MaximumSize": [16777215, BOX_PX]}),
            ui.HGroup({"Weight": 0}, [
                ui.Button({"ID": "Stop", "Text": "Stop", "Weight": 0}),
                ui.HGap(0, 1),
                ui.Button({"ID": "OpenWeb", "Text": "Open on nolgia.ai", "Weight": 0,
                           "ToolTip": "This chat on nolgia.ai, in your browser."}),
                ui.Button({"ID": "Send", "Text": "Send", "Weight": 0}),
            ]),
            ui.Label({"ID": "ChatCost", "Text": "", "WordWrap": True, "Weight": 0,
                      "MinimumSize": [0, COST_LINES_PX], "Alignment": {"AlignTop": True, "AlignLeft": True}}),
            ui.HGroup({"Weight": 0}, [
                ui.Label({"Text": "Activity", "Font": bold}),
                ui.HGap(0, 1),
                ui.Button({"ID": "Pause", "Text": "Pause", "Weight": 0}),
            ]),
            ui.Tree({"ID": "Activity", "Weight": 1, "RootIsDecorated": False, "AlternatingRowColors": True,
                     "SelectionMode": "NoSelection", "MaximumSize": [16777215, ACTIVITY_MAX_PX]}),
            ui.Label({"ID": "Footer", "Text": FOOTER, "WordWrap": True, "Weight": 0,
                      "MinimumSize": [0, FOOTER_LINES_PX], "Alignment": {"AlignTop": True, "AlignLeft": True}}),
        ])
        self.win = self.disp.AddWindow({"ID": WINDOW_ID, "WindowTitle": "NOLGIA",
                                        "Geometry": [120, 60, 440, DEFAULT_HEIGHT],
                                        "MinimumSize": [MIN_WIDTH, MIN_HEIGHT]}, layout)
        self.items = self.win.GetItems()
        tree = self.items["Activity"]
        tree.ColumnCount = 4
        tree.SetHeaderLabels(["Time", "Command", "Status", "Detail"])
        for column, width in enumerate((70, 90, 110, 160)):
            tree.ColumnWidth[column] = width
        on = self.win.On
        on[WINDOW_ID].Close = self._on_close
        on.SignIn.Clicked = self._guard(lambda ev: self.controller.sign_in())
        on.OpenPage.Clicked = self._guard(lambda ev: self.controller.open_sign_in_page())
        on.CancelSignIn.Clicked = self._guard(lambda ev: self.controller.cancel_sign_in())
        on.SignOut.Clicked = self._guard(lambda ev: self.controller.sign_out())
        on.Connected.Clicked = self._guard(self._on_connected)
        on.AllowAgent.Clicked = self._guard(
            lambda ev: self.controller.set_setting("allow_agent", bool(self.items["AllowAgent"].Checked)))
        on.AskBeforeRun.Clicked = self._guard(
            lambda ev: self.controller.set_setting("ask_before_run", bool(self.items["AskBeforeRun"].Checked)))
        on.Pause.Clicked = self._guard(self._on_pause)
        on.Review.Clicked = self._guard(lambda ev: self.show_request(force=True))
        chat = self.controller.chat
        on.Send.Clicked = self._guard(lambda ev: self._send_from_box())
        on.Stop.Clicked = self._guard(lambda ev: chat.stop())
        on.NewChat.Clicked = self._guard(lambda ev: chat.new_chat())
        on.OpenWeb.Clicked = self._guard(lambda ev: self._open(chat.view()["session_url"] or AGENT_URL))
        on.ChatAction.Clicked = self._guard(self._on_chat_action)
        on.Box.TextChanged = self._guard(self._on_box_changed)
        for i, (_short, full) in enumerate(chatview.EXAMPLES):
            on["Example%d" % i].Clicked = self._guard(lambda ev, text=full: self._put_in_box(text))
        for i in range(OPTIONS):
            on["Option%d" % i].Clicked = self._guard(lambda ev, n=i: self._on_option(n))
        self._start_timer()
        self.refresh(force=True)
        return self.win

    def _start_timer(self):
        self.timer = self.ui.Timer({"ID": TIMER_ID, "Interval": TICK_MS, "SingleShot": False})
        tick = self._guard(self._on_tick)

        def on_timeout(ev):
            # Resolve 21.1.1 delivers every timer's Timeout to the dispatcher's
            # own On.Timeout, naming the timer in ev["who"] (a handler set on
            # On[<timer id>] is never called).
            if _who(ev) in (TIMER_ID, None):
                tick(ev)

        self.disp.On.Timeout = on_timeout
        self.timer.Start()

    def _guard(self, fn):
        def handler(ev):
            try:
                fn(ev)
            except Exception:
                self.log("The NOLGIA window hit a bug:\n" + traceback.format_exc())
            self.refresh()
        return handler

    # ------------------------------------------------------------- events

    def _on_tick(self, ev):
        self._ticks += 1
        if self._ticks == 1:
            self.log("The NOLGIA window is running (timer ticks).")
        if self._ticks % HEIGHT_CHECK_TICKS == 0:
            self._keep_min_height()
        if self._ticks % PROJECT_CHECK_TICKS == 0:
            self._note_project()
        ctl = self.controller
        ctl.tick()
        if self._scroll_to:
            self._scroll_thread()
        busy = (ctl.login is not None or (ctl.worker is not None and ctl.worker.busy_with is not None)
                or ctl.chat.busy)
        time.sleep(YIELD_BUSY if busy else YIELD_IDLE)  # let the network threads run

    def _on_connected(self, ev):
        if self.items["Connected"].Checked:
            self.controller.connect()
        else:
            self.controller.disconnect()

    # ------------------------------------------------------------- chat

    def _open(self, url):
        if isinstance(url, str) and url.startswith("https://"):
            self.controller.open_url(url)

    def _set_box(self, text):
        self._box_quiet = True
        try:
            self.items["Box"].PlainText = text
        finally:
            self._box_quiet = False
        self._box_text = text

    def _put_in_box(self, text):
        self._set_box(text)
        try:
            self.items["Box"].SetFocus()
        except Exception:
            pass

    def _send_from_box(self):
        ctl = self.controller
        if ctl.chat_blocker() or ctl.chat.busy:
            return
        # The box empties as the message goes; if NOLGIA refuses it, the
        # chat hands the text back (take_restore, in _draw_chat).
        if ctl.chat.send(str(self.items["Box"].PlainText or "")):
            self._set_box("")

    def _on_box_changed(self, ev):
        """Enter at the end of the message sends it (as in the chat on
        nolgia.ai): the box then holds what it held plus one newline."""
        if self._box_quiet:
            return
        text = str(self.items["Box"].PlainText or "")
        before, self._box_text = self._box_text, text
        self.controller.chat.wake()
        if text.endswith("\n") and text[:-1] == before and before.strip():
            self._set_box(before)
            self._send_from_box()

    def _on_option(self, n):
        if n < len(self._options) and not self.controller.chat_blocker() and not self.controller.chat.busy:
            self.controller.chat.send(self._options[n])

    def _chat_notice(self, view):
        """What the notice line says, with its button: a blocker first."""
        blocker = self.controller.chat_blocker()
        if blocker:
            return {"text": blocker["text"], "label": blocker["label"], "fix": True}
        notice = view["notice"]
        if notice:
            action = notice.get("action") or {}
            return {"text": notice["text"], "label": action.get("label") or "", "url": action.get("url"), "fix": False}
        return None

    def _on_chat_action(self, ev):
        notice = self._chat_notice(self.controller.chat.view())
        if not notice:
            return
        if notice["fix"]:
            self.controller.fix_chat_blocker()
        elif notice.get("url"):
            self._open(notice["url"])

    def _on_pause(self, ev):
        if self.controller.connected:
            self.controller.disconnect()
        else:
            self.controller.connect()

    def close(self):
        """Close the window, as its close button does."""
        self._on_close(None)

    def _on_close(self, ev):
        if self._closed:
            return
        self._closed = True
        self._finish_quit = ev is not None and self._resolve_is_quitting()
        if self._finish_quit:
            self.log("DaVinci Resolve is quitting (its new Untitled Project opened %.3f s before the window "
                     "closed); switching off." % (time.monotonic() - self._project_changed_at))
        else:
            self.log("The NOLGIA window closed; switching off.")
        try:
            if self.timer is not None:
                self.timer.Stop()
        except Exception:
            pass
        self.controller.shutdown(wait=3.0)
        self._close_request()
        self.disp.ExitLoop()

    def _note_project(self):
        """Remember the current project's id, when it last changed and whether
        it changed to one this window had not seen (a moment with no project
        open does not count)."""
        try:
            if self._pm is None:
                self._pm = self.resolve.GetProjectManager()
            project = self._pm.GetCurrentProject()
            seen = project.GetUniqueId() if project is not None else None
        except Exception:
            return
        if not seen:
            return
        if seen != self._project_seen:
            if self._project_seen is not None:
                self._project_changed_at = time.monotonic()
                self._project_is_new = seen not in self._projects_seen
            self._project_seen = seen
        self._projects_seen.add(seen)

    def _resolve_is_quitting(self):
        """True when this Close is Resolve quitting (see QUIT_SWITCH_SECONDS),
        not the person closing the NOLGIA window."""
        self._note_project()
        changed = self._project_changed_at
        if changed is None:
            return False
        since = time.monotonic() - changed
        if since > QUIT_SWITCH_SECONDS:
            if since <= QUIT_NOTE_SECONDS and self._project_is_new and self._is_quit_project():
                self.log("The NOLGIA window closed %.3f s after a new empty Untitled Project opened, later than "
                         "Resolve's quit closes it (%.1f s); DaVinci Resolve stays open." % (since, QUIT_SWITCH_SECONDS))
            return False
        if not (self._project_is_new and self._is_quit_project()):
            self.log("The project changed just before the NOLGIA window closed, but not to the new empty "
                     "Untitled Project Resolve opens when it quits; DaVinci Resolve stays open.")
            return False
        on_screen = resolve_windows_on_screen()
        if sys.platform.startswith("win"):
            return on_screen is False
        return on_screen is not True

    def _is_quit_project(self):
        """Whether the current project is the one Resolve opens while quitting:
        new, named UNTITLED_PROJECT, empty and not in the project library."""
        try:
            pm = self.resolve.GetProjectManager()
            project = pm.GetCurrentProject()
            if project is None or project.GetUniqueId() != self._project_seen:
                return False
            name = project.GetName()
            if name != UNTITLED_PROJECT or project.GetTimelineCount() != 0:
                return False
            root = project.GetMediaPool().GetRootFolder()
            clips, bins = root.GetClipList(), root.GetSubFolderList()
            if not isinstance(clips, (list, dict)) or not isinstance(bins, (list, dict)) or clips or bins:
                return False
            names = pm.GetProjectListInCurrentFolder()
            if not isinstance(names, (list, dict)) or name in names:
                return False
            return not pm.GetProjectLastModifiedTime(name)
        except Exception:
            return False

    def _approvals_changed(self):
        self.show_request()

    # ------------------------------------------------------------- request

    def show_request(self, force=False):
        pending = list(self.controller.executor.approvals)
        if not pending:
            self._close_request()
            return
        first = pending[0]
        if self.request_shown == first.id and not force:
            return
        win = self.request_win or self._build_request()
        request = first.request
        items = win.GetItems()
        code = bool(getattr(request, "is_code", False))
        text = "\n".join(request.lines)[:MAX_CODE_CHARS]
        items["RequestTitle"].Text = request.title
        # Code is shown as Python; a plain-English plan (a DIT copy, opening a
        # project) as plain text, never coloured like code.
        items["RequestCode"].PlainText = text if code else ""
        items["RequestText"].PlainText = "" if code else text
        changed = _show(items["RequestCode"], code)
        changed = _show(items["CodeNote"], code) or changed
        changed = _show(items["RequestText"], not code) or changed
        if changed:
            try:
                win.RecalcLayout()
            except Exception:
                pass
        items["Approve"].Text = request.approve_label
        win.Show()
        try:
            win.Raise()
        except Exception:
            pass
        self.request_shown = first.id
        self.log("Asked the person: %s." % request.title)

    def _build_request(self):
        """The "NOLGIA request" window, made once and reused."""
        ui = self.ui
        mono = ui.Font({"Family": "Consolas", "PointSize": 10, "MonoSpaced": True})
        layout = ui.VGroup({"Spacing": 6}, [
            ui.Label({"ID": "RequestTitle", "Text": "", "WordWrap": True, "Weight": 0,
                      "Font": ui.Font({"Bold": True})}),
            ui.TextEdit({"ID": "RequestCode", "PlainText": "", "ReadOnly": True, "Font": mono,
                         "LineWrapMode": "NoWrap", "Lexer": "python", "Weight": 1}),
            ui.TextEdit({"ID": "RequestText", "PlainText": "", "ReadOnly": True, "Weight": 1}),
            ui.Label({"ID": "CodeNote", "Text": "Code runs on this computer with your permissions.", "Weight": 0}),
            ui.HGroup({"Weight": 0}, [
                ui.HGap(0, 1),
                ui.Button({"ID": "Deny", "Text": "Deny"}),
                ui.Button({"ID": "Approve", "Text": "Approve"}),
            ]),
        ])
        win = self.disp.AddWindow({"ID": REQUEST_ID, "WindowTitle": "NOLGIA request",
                                   "Geometry": [180, 160, 680, 460]}, layout)

        def decide(approve):
            def handler(ev):
                command_id = self.request_shown
                if command_id is not None:
                    if approve:
                        self.controller.executor.approve(command_id)
                    else:
                        self.controller.executor.deny(command_id)
                self.controller.bump()
                self._close_request()
            return self._guard(handler)

        win.On.Approve.Clicked = decide(True)
        win.On.Deny.Clicked = decide(False)
        # Closing the request window leaves the request waiting in the NOLGIA window.
        win.On[REQUEST_ID].Close = self._guard(lambda ev: self._close_request())
        self.request_win = win
        return win

    def _close_request(self):
        win = self.request_win
        if win is not None:
            try:
                win.Hide()
            except Exception:
                pass
        self.request_shown = None

    # ------------------------------------------------------------- drawing

    def refresh(self, force=False):
        if self.win is None or self._closed:
            return
        ctl = self.controller
        state = (ctl.version, ctl.activity.version, len(ctl.executor.approvals))
        if state == self._drawn and not force:
            return
        self._drawn = state
        it = self.items
        it["Status"].Text = ctl.status_line()
        signing_in = ctl.login is not None
        signed_in = ctl.signed_in
        email = ctl.account_email()
        if signed_in and not signing_in:
            it["Account"].Text = ("Signed in as %s" % email) if email else (
                "Signed in with NOLGIA_TOKEN" if ctl.env_token else "Signed in")
        else:
            it["Account"].Text = ""
        prompt = ctl.login.prompt if signing_in else None
        it["Code"].Text = prompt.user_code if prompt else ""
        it["CodeHint"].Text = "Approve this code in the browser page that opened." if prompt else ""
        shown = [
            _show(it["Code"], bool(prompt)),
            _show(it["CodeHint"], bool(prompt)),
            _show(it["Account"], bool(it["Account"].Text)),
            _show(it["SignIn"], not signed_in and not signing_in),
            _show(it["OpenPage"], bool(prompt)),
            _show(it["CancelSignIn"], signing_in),
            _show(it["SignOut"], signed_in and not signing_in),
        ]
        for key in ("Connected", "AllowAgent", "AskBeforeRun"):
            shown.append(_show(it[key], signed_in and not signing_in))
        it["Connected"].Checked = bool(ctl.connected)
        it["AllowAgent"].Checked = bool(ctl.setting("allow_agent"))
        it["AskBeforeRun"].Checked = bool(ctl.setting("ask_before_run"))
        waiting = len(ctl.executor.approvals)
        it["Waiting"].Text = ("%d request%s waiting for you." % (waiting, "" if waiting == 1 else "s")) if waiting else ""
        shown.append(_show(it["Waiting"], bool(waiting)))
        shown.append(_show(it["Review"], bool(waiting)))
        it["Pause"].Text = "Pause" if ctl.connected else "Resume"
        shown.append(_show(it["Pause"], signed_in))
        shown.extend(self._draw_chat(signed_in and not signing_in))
        if any(shown):
            self._relayout()
        self._scroll_thread()
        if ctl.activity.version != self._activity_drawn:
            self._activity_drawn = ctl.activity.version
            self._draw_activity()

    def _draw_chat(self, visible):
        """Draw the NOLGIA Agent chat. Returns what _show returned for each
        element (True where that changed)."""
        it = self.items
        ctl = self.controller
        view = ctl.chat.view()
        notice = self._chat_notice(view) if visible else None
        intro = visible and view["loaded"] and view["empty"]
        rows = view["rows"] if visible else []
        question = rows[-1]["question"] if rows and not notice and not view["busy"] else None
        options = [str(o.get("label") or "") for o in (question or {}).get("options", []) if isinstance(o, dict)]
        self._options = [o for o in options if o][:OPTIONS]
        shown = [
            _show(it["ChatTitle"], visible),
            _show(it["NewChat"], visible and not view["empty"]),
            _show(it["ChatIntro"], intro),
            _show(it["Thread"], visible and bool(rows)),
            _show(it["ChatWorking"], visible and bool(view["working"])),
            _show(it["ChatNotice"], bool(notice)),
            _show(it["ChatAction"], bool(notice and notice["label"])),
            _show(it["Box"], visible),
            _show(it["Send"], visible),
            _show(it["Stop"], visible and view["can_stop"]),
            _show(it["OpenWeb"], visible),
            _show(it["ChatCost"], visible),
        ]
        for i in range(len(chatview.EXAMPLES)):
            shown.append(_show(it["Example%d" % i], intro))
        for i in range(OPTIONS):
            label = self._options[i] if i < len(self._options) else ""
            if label:
                it["Option%d" % i].Text = _shorten(label, OPTION_CHARS)
                it["Option%d" % i].ToolTip = label
            shown.append(_show(it["Option%d" % i], bool(label)))
        if not visible:
            self._thread_drawn = None
            return shown
        restore = ctl.chat.take_restore()
        if restore and not str(it["Box"].PlainText or "").strip():
            self._set_box(restore)
        it["ChatWorking"].Text = view["working"] or ""
        it["ChatNotice"].Text = notice["text"] if notice else ""
        it["ChatAction"].Text = notice["label"] if notice else ""
        it["ChatCost"].Text = view["cost_line"]
        it["OpenWeb"].ToolTip = ("This chat on nolgia.ai, in your browser." if view["session_url"]
                                 else "NOLGIA Agent on nolgia.ai, in your browser.")
        # One message is one turn: Send waits while the Agent works.
        blocked = bool(ctl.chat_blocker())
        it["Send"].Enabled = not blocked and not view["busy"]
        it["Send"].Text = "Sending..." if ctl.chat.sending else "Send"
        signature = repr((view["hidden"], rows))
        if signature != self._thread_drawn:
            self._thread_drawn = signature
            self.items["Thread"].HTML = chatview.thread_html(view)
            self._scroll_to = chatview.scroll_target(view)
            self._scroll_ticks = 3
        return shown

    def _scroll_thread(self):
        """Scroll the thread to where _draw_chat asked, after the window laid
        itself out again (a notice or answer buttons appearing change the
        thread's height, and with it how far it can scroll). UIManager's
        TextEdit has no scroll position to read; ScrollToAnchor moves it
        without selecting text (MoveCursor selects)."""
        if not self._scroll_to:
            return
        try:
            self.items["Thread"].ScrollToAnchor(self._scroll_to)
        except Exception:
            pass  # scrolling is a nicety
        self._scroll_ticks -= 1
        if self._scroll_ticks <= 0:
            self._scroll_to = None

    def _set_min_size(self):
        """Give the shown window its MinimumSize again: set before the window
        exists, macOS may not apply it to the window it makes."""
        try:
            self.win.MinimumSize = [MIN_WIDTH, MIN_HEIGHT]
        except Exception:
            pass

    def _keep_min_height(self):
        """Put the window back to MIN_HEIGHT when it was made shorter (see
        HEIGHT_CHECK_TICKS). Windows keeps the minimum itself, so this does
        nothing there; a UIManager that cannot tell the window's size or
        resize it turns the check off (the layout then only shrinks the
        thread, the box and Activity)."""
        if self._height_check_off:
            return
        try:
            geometry = None
            try:
                geometry = self.win.GetGeometry()
            except Exception:
                pass
            if _window_size(geometry) is None:
                geometry = self.win.Geometry
            size = _window_size(geometry)
            if size is None or size[1] >= MIN_HEIGHT:
                return
            width = max(size[0], MIN_WIDTH)
            try:
                self.win.Resize([width, MIN_HEIGHT])
            except Exception:
                x, y = _window_origin(geometry)
                self.win.Geometry = [x, y, width, MIN_HEIGHT]
        except Exception:
            self._height_check_off = True
            return
        self._relayout()

    def _relayout(self):
        """UIManager lays a window out when it is shown. After that an element
        hidden or shown through Hidden keeps the geometry it had (one shown for
        the first time, like the sign-in code card, is drawn over the top of the
        window) until the window recalculates its layout."""
        try:
            self.win.RecalcLayout()
        except Exception:
            pass

    def _draw_activity(self):
        tree = self.items["Activity"]
        tree.Clear()
        items = self.controller.activity.items()
        if not items:
            row = tree.NewItem()
            row.Text[0] = ""
            row.Text[1] = "Nothing yet."
            tree.AddTopLevelItem(row)
            return
        for item in items:
            row = tree.NewItem()
            row.Text[0] = time.strftime("%H:%M:%S", time.localtime(item["time"]))
            row.Text[1] = item["kind"]
            row.Text[2] = ActivityLog.STATUS_LABELS.get(item["status"], item["status"])
            row.Text[3] = item.get("detail") or ""
            tree.AddTopLevelItem(row)

    # ------------------------------------------------------------- run

    def run(self):
        """Show the window and process its events until it closes."""
        self.build()
        ctl = self.controller
        ctl.apply_env()
        wanted = env_flag("NOLGIA_BRIDGE_AUTOCONNECT", ctl.env) or ctl.settings.get("connected")
        if wanted and ctl.signed_in:
            ctl.connect()
        elif ctl.settings.get("connected") and not ctl.signed_in:
            ctl.settings.update(connected=False)
        self.refresh(force=True)
        self.win.Show()
        self._set_min_size()
        self._note_project()
        self.log("NOLGIA window open (plugin %s, DaVinci Resolve %s, Python %s at %s)."
                 % (_plugin_version(), ctl.ops.app_version(), sys.version.split()[0], sys.executable))
        try:
            self.disp.RunLoop()
        finally:
            if not self._closed:
                self._closed = True
                ctl.shutdown(wait=3.0)
            try:
                self.win.Hide()
            except Exception:
                pass
            self._close_request()
            if self._finish_quit:
                self._quit_resolve()
        return True

    def _quit_resolve(self):
        """Finish the quit Resolve gave up while this window was open."""
        self.log("Finishing DaVinci Resolve's quit.")
        try:
            self.resolve.Quit()
        except Exception:
            self.log("DaVinci Resolve did not take the quit:\n" + traceback.format_exc())


# The titles of NOLGIA's own windows, which do not count as Resolve's.
OWN_TITLES = ("NOLGIA", "NOLGIA request")


def resolve_windows_on_screen():
    """True when DaVinci Resolve (the program that started this script) shows
    a window of its own, False when it shows none, None when that cannot be
    read here (macOS and Linux)."""
    if not sys.platform.startswith("win"):
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        resolve_pid = os.getppid()
        found = []
        proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def visit(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value != resolve_pid:
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            title = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, title, length + 1)
            if title.value in OWN_TITLES:
                return True
            found.append(title.value)
            return False

        user32.EnumWindows(proc(visit), 0)
        return bool(found)
    except Exception:
        return None


def _who(ev):
    try:
        return ev["who"]
    except Exception:
        return None


def _show(element, visible):
    """Show or hide an element. True when that changed its state."""
    try:
        hidden = not visible
        if bool(element.Hidden) == hidden:
            return False
        element.Hidden = hidden
        return True
    except Exception:
        return False


def _shorten(text, limit):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 3].rstrip() + "..."


def _plugin_version():
    from .core import PLUGIN_VERSION

    return PLUGIN_VERSION


def describe(controller):
    """A one-line state for logs and tests."""
    worker = controller.worker
    return "%s; %s" % (controller.status_line(), worker.state if worker is not None else Status.OFF)
