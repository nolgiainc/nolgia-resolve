# SPDX-License-Identifier: GPL-3.0-or-later
"""The NOLGIA window, against a stand-in UIManager: every state draws, the
buttons and switches do what they say, and code waits for Approve."""

import shutil
import tempfile
import time
import unittest
from unittest import mock

import support
import fake_resolve as fake
import fake_ui

from nolgia_resolve import panel as panel_module
from nolgia_resolve.runtime import Controller
from nolgia_resolve.settings import Settings


class PanelTest(unittest.TestCase):
    def setUp(self):
        self.server = support.start_mock(poll_wait_seconds=2, device_interval=1, auto_approve_after=1)
        self.tmp = tempfile.mkdtemp(prefix="nolgia-panel-")
        self.env = {"NOLGIA_API_URL": self.server.base_url, "NOLGIA_CONFIG_DIR": self.tmp}
        self.fusion, self.bmd = fake_ui.Fusion(), fake_ui.Bmd()
        self.opened = []
        self.lines = []
        self.controller = Controller(fake.sample(), has_window=True, env=self.env, log=self.lines.append,
                                     open_url=self.opened.append)
        self.panel = panel_module.Panel(None, self.fusion, self.bmd, controller=self.controller)
        self.win = self.panel.build()
        self.disp = self.bmd.dispatchers[0]

    def tearDown(self):
        self.controller.shutdown(wait=5)
        self.server.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def item(self, element_id):
        return self.win.Find(element_id)

    def visible(self, element_id):
        return not self.item(element_id).Hidden

    def tick_until(self, predicate, timeout=30):
        end = time.time() + timeout
        while time.time() < end:
            self.disp.tick(panel_module.TIMER_ID)
            if predicate():
                return
            time.sleep(0.02)
        self.fail("timed out; status: %s" % self.controller.status_line())

    def wait_for_buttons(self, request):
        """Let REQUEST_ARM_SECONDS pass; the next tick turns the buttons on."""
        time.sleep(panel_module.REQUEST_ARM_SECONDS)
        self.disp.tick(panel_module.TIMER_ID)
        self.assertTrue(request.Find("Approve").Enabled and request.Find("Deny").Enabled)

    def sign_in(self):
        self.win.fire("SignIn")
        self.tick_until(lambda: self.controller.connected and self.controller.worker.state == "connected"
                        and self.item("Account").Text == "Signed in as test@nolgia.ai")

    def command(self, kind, args=None):
        status, data = support.call(self.server, "POST", "/v1/bridge/commands",
                                    {"app": "resolve", "kind": kind, "args": args or {}, "timeout_seconds": 60})
        self.assertEqual(status, 201, data)
        return data["id"]

    def status_of(self, command_id):
        return support.call(self.server, "GET", "/v1/bridge/commands/" + command_id)[1]

    def test_a_window_made_too_short_gets_its_height_back(self):
        # The owner's Mac test (2026-10-29): Resolve on macOS let the window be
        # dragged shorter than its minimum, and the text box covered the chat.
        self.win.Geometry = [120, 60, 440, 600]
        for _ in range(panel_module.HEIGHT_CHECK_TICKS):
            self.disp.tick(panel_module.TIMER_ID)
        self.assertEqual(self.win.resized, [[440, panel_module.MIN_HEIGHT]])
        self.win.Geometry = {1.0: 120.0, 2.0: 60.0, 3.0: 400.0, 4.0: 500.0}   # a Lua table reads back as a dict
        for _ in range(panel_module.HEIGHT_CHECK_TICKS):
            self.disp.tick(panel_module.TIMER_ID)
        self.assertEqual(self.win.resized[-1], [panel_module.MIN_WIDTH, panel_module.MIN_HEIGHT])
        tall = len(self.win.resized)
        for _ in range(panel_module.HEIGHT_CHECK_TICKS):
            self.disp.tick(panel_module.TIMER_ID)
        self.assertEqual(len(self.win.resized), tall, "a window at its minimum is left alone")

    def test_nothing_that_can_shrink_has_a_hard_minimum(self):
        # The owner's Mac test 2 (2026-10-29): at 646 pt the text box was drawn
        # over the chat. A UIManager element with a MinimumSize keeps it when
        # its window is too short and spills over the next one, so only labels
        # (their two lines of text) keep one; the thread, the box and Activity
        # give up height and scroll.
        for element in self.win.layout.walk():
            minimum = element.props.get("MinimumSize")
            if minimum and minimum[1] > 0:
                self.assertEqual(element.kind, "Label", "%s has a hard minimum height" % element.props.get("ID"))
        for element_id in ("Thread", "Box", "Activity"):
            self.assertNotIn("MinimumSize", self.item(element_id).props)
        self.assertEqual(self.item("Box").props["MaximumSize"][1], panel_module.BOX_PX)

    def test_the_height_is_checked_every_tick(self):
        self.win.Geometry = [120, 60, 440, 646]
        self.disp.tick(panel_module.TIMER_ID)
        self.assertEqual(self.win.resized, [[440, panel_module.MIN_HEIGHT]])

    def test_the_minimum_size_is_set_again_once_shown(self):
        self.panel._set_min_size()
        self.assertEqual(self.win.MinimumSize, [panel_module.MIN_WIDTH, panel_module.MIN_HEIGHT])

    def _request(self, command_id, label="Copy and verify"):
        from nolgia_resolve.core.mainthread import ApprovalRequest, PendingApproval

        class Command:
            id = command_id
            deadline = time.monotonic() + 600

        class Ticket:
            command = Command

        pending = PendingApproval(Ticket(), ApprovalRequest("Your NOLGIA Agent wants to copy 2 files", ["Copy 2 files"],
                                                            "n/a", approve_label=label))
        self.controller.executor.approvals.append(pending)
        return pending

    def test_the_request_buttons_are_never_default_and_never_take_focus(self):
        # Return and Space never answer a request: no default button, no focus.
        self._request("cmd-1")
        self.panel.show_request()
        for bid in ("Approve", "Deny"):
            props = self.panel.request_win.Find(bid).props
            self.assertIs(props["AutoDefault"], False)
            self.assertIs(props["Default"], False)
            self.assertEqual(props["FocusPolicy"], {"NoFocus": True})

    def test_a_click_as_the_request_opens_is_never_an_answer(self):
        # The owner's Mac test 3: request 74b78e66 was approved about 3 s after
        # it showed, with the pointer resting where its button appears.
        pending = self._request("cmd-dit")
        self.panel.show_request()
        request = self.panel.request_win
        self.assertFalse(request.Find("Approve").Enabled)
        self.assertFalse(request.Find("Deny").Enabled)
        request.fire("Approve")
        request.fire("Deny")
        self.disp.tick(panel_module.TIMER_ID)
        self.assertIsNone(pending.decision)
        self.assertTrue(request.shown)
        self.assertEqual(sum("Ignored a click" in line for line in self.lines), 2)
        time.sleep(panel_module.REQUEST_ARM_SECONDS / 2)
        self.disp.tick(panel_module.TIMER_ID)
        self.assertFalse(request.Find("Approve").Enabled, "still off half way")
        request.fire("Approve")
        self.assertIsNone(pending.decision)

    def test_once_armed_a_click_answers_that_request(self):
        pending = self._request("cmd-dit")
        self.panel.show_request()
        request = self.panel.request_win
        self.wait_for_buttons(request)
        request.fire("Approve")
        self.assertIs(pending.decision, True)
        self.assertFalse(request.shown)
        self.assertIn("The person clicked Copy and verify for request cmd-dit.", self.lines)

    def test_an_answer_goes_only_to_the_request_its_buttons_were_on_for(self):
        first = self._request("cmd-a", "Run code")
        self.panel.show_request()
        request = self.panel.request_win
        self.wait_for_buttons(request)
        # cmd-a goes (it ran out of time), cmd-b takes its place: the buttons
        # go off again, and a click meant for cmd-a never answers cmd-b.
        self.controller.executor.approvals.remove(first)
        second = self._request("cmd-b")
        self.panel.show_request()
        self.assertFalse(request.Find("Approve").Enabled)
        request.fire("Approve")
        self.assertIsNone(first.decision)
        self.assertIsNone(second.decision)
        self.wait_for_buttons(request)
        request.fire("Deny")
        self.assertIs(second.decision, False)
        self.assertIsNone(first.decision)
        self.assertIn("The person clicked Deny for request cmd-b.", self.lines)

    def test_a_closed_request_window_answers_nothing(self):
        pending = self._request("cmd-dit")
        self.panel.show_request()
        request = self.panel.request_win
        self.wait_for_buttons(request)
        request.fire(panel_module.REQUEST_ID, "Close")
        request.fire("Approve")  # a click that was already on its way
        self.assertIsNone(pending.decision)
        self.assertFalse(request.Find("Approve").Enabled)

    def test_a_plain_request_is_shown_as_text_not_code(self):
        # The owner's Mac test 2: the DIT copy's plan was coloured like Python.
        from nolgia_resolve.core.mainthread import ApprovalRequest, PendingApproval

        class Ticket:
            class command:
                id = "cmd-dit"

        plan = ["Copy 10 files (9.3 MB) and check every copy:", "  card A001: /cards/A001 (6 files, 6.1 MB)",
                "The cards are only read. Nothing at a destination is replaced."]
        self.controller.executor.approvals.append(
            PendingApproval(Ticket(), ApprovalRequest("Your NOLGIA Agent wants to copy 10 files on this computer",
                                                      plan, "n/a", approve_label="Copy and verify")))
        self.panel.show_request()
        request = self.panel.request_win
        self.assertTrue(request.shown)
        self.assertEqual(request.Find("RequestText").PlainText, "\n".join(plan))
        self.assertFalse(request.Find("RequestText").Hidden)
        self.assertNotIn("Lexer", request.Find("RequestText").props)
        self.assertTrue(request.Find("RequestCode").Hidden)
        self.assertEqual(request.Find("RequestCode").PlainText, "")
        self.assertTrue(request.Find("CodeNote").Hidden)
        self.assertEqual(request.Find("Approve").Text, "Copy and verify")
        self.assertGreaterEqual(request.relayouts, 1)
        self.controller.executor.approvals.clear()

    def test_no_height_check_where_the_window_cannot_say_its_size(self):
        self.win.Geometry = None
        for _ in range(panel_module.HEIGHT_CHECK_TICKS * 2):
            self.disp.tick(panel_module.TIMER_ID)
        self.assertEqual(self.win.resized, [])

    def test_layout_and_signed_out_state(self):
        self.assertEqual(self.win.props["WindowTitle"], "NOLGIA")
        for element_id in ("Status", "SignIn", "SignOut", "Connected", "AllowAgent", "AskBeforeRun", "Activity",
                           "Pause", "Footer", "Review"):
            self.assertIsNotNone(self.item(element_id), element_id)
        self.assertTrue(self.fusion.UIManager.timers[0].started)
        self.assertEqual(self.item("Status").Text, "Not signed in.")
        self.assertTrue(self.visible("SignIn"))
        for element_id in ("SignOut", "Connected", "AllowAgent", "AskBeforeRun", "Review", "Pause"):
            self.assertFalse(self.visible(element_id), element_id)
        self.assertEqual(self.item("Activity").header, ["Time", "Command", "Status", "Detail"])
        self.assertEqual(self.item("Activity").rows[0].Text[1], "Nothing yet.")
        self.assertIn("this window is open", self.item("Footer").Text)
        # The Status label's minimum width sets the window width (UIManager sizes
        # the window to its widest child), and the wrapping labels have two
        # lines of height: a long retry message once spilled over the title bar
        # in a 230 px wide window.
        self.assertGreaterEqual(self.item("Status").props["MinimumSize"][0], 380)
        self.assertGreaterEqual(self.win.props["MinimumSize"][0], 400)
        for element_id in ("Status", "CodeHint", "Footer"):
            self.assertTrue(self.item(element_id).props["WordWrap"], element_id)
            self.assertGreaterEqual(self.item(element_id).props["MinimumSize"][1], 32, element_id)
            self.assertTrue(self.item(element_id).props["Alignment"]["AlignTop"], element_id)

    def test_sign_in_connect_pause_and_sign_out(self):
        # Resolve lays the window out once, at Show; every change of what is
        # visible has to ask for the layout again or the new elements pile up.
        before = self.win.relayouts
        self.panel.refresh(force=True)
        self.assertEqual(self.win.relayouts, before, "nothing visible changed, no relayout")
        self.win.fire("SignIn")
        self.tick_until(lambda: self.item("Code").Text != "")
        self.assertIn("Enter code", self.item("Status").Text)
        self.assertTrue(self.visible("OpenPage") and self.visible("CancelSignIn"))
        self.assertGreater(self.win.relayouts, before, "the code card appeared: the layout was recalculated")
        self.assertEqual(len(self.opened), 1)
        self.tick_until(lambda: self.controller.connected and self.item("Account").Text.startswith("Signed in as"))
        self.tick_until(lambda: self.item("Status").Text == "Connected. NOLGIA can work in this DaVinci Resolve.")
        self.assertTrue(self.item("Connected").Checked)
        self.assertTrue(self.item("AllowAgent").Checked)
        self.assertFalse(self.item("AskBeforeRun").Checked)
        self.assertEqual(self.item("Pause").Text, "Pause")
        self.assertFalse(self.visible("Code"))
        self.assertGreater(self.win.relayouts, before + 1, "the code card went away: recalculated again")
        self.win.fire("Pause")
        self.tick_until(lambda: self.controller.worker is None)
        self.assertEqual(self.item("Pause").Text, "Resume")
        self.assertFalse(self.item("Connected").Checked)
        self.assertFalse(Settings(self.tmp)["connected"])
        self.item("Connected").Checked = True
        self.win.fire("Connected")
        self.tick_until(lambda: self.controller.connected)
        self.assertTrue(Settings(self.tmp)["connected"])
        self.win.fire("SignOut")
        self.tick_until(lambda: self.controller.worker is None)
        self.assertEqual(self.item("Status").Text, "Signed out.")
        self.assertTrue(self.visible("SignIn"))
        self.assertEqual(Settings(self.tmp)["token"], "")

    def test_ask_before_running_code(self):
        self.sign_in()
        self.item("AskBeforeRun").Checked = True
        self.win.fire("AskBeforeRun")
        self.assertTrue(Settings(self.tmp)["ask_before_run"])
        first = self.command("run", {"code": "result = 6 * 7"})
        self.tick_until(lambda: self.panel.request_win is not None)
        request = self.panel.request_win
        self.assertEqual(request.props["WindowTitle"], "NOLGIA request")
        self.assertEqual(request.Find("RequestTitle").Text, "Your agent wants to run Python in DaVinci Resolve")
        self.assertEqual(request.Find("RequestCode").PlainText, "result = 6 * 7")
        self.assertFalse(request.Find("RequestCode").Hidden)
        self.assertEqual(request.Find("RequestCode").Lexer, "python")
        self.assertTrue(request.Find("RequestText").Hidden)
        self.assertFalse(request.Find("CodeNote").Hidden)
        self.assertEqual(request.Find("Approve").Text, "Run code")
        self.assertTrue(self.visible("Review"))
        self.assertEqual(self.item("Waiting").Text, "1 request waiting for you.")
        self.assertEqual(self.item("Status").Text, "Waiting for you to approve a request.")
        self.assertTrue(request.shown)
        self.assertFalse(request.Find("Approve").Enabled)
        request.fire("Approve")  # a click as the window opens is no answer
        self.disp.tick(panel_module.TIMER_ID)
        self.assertEqual([p.id for p in self.controller.executor.approvals], [first])
        self.assertIn("Ignored a click on Run code that came before the request's buttons were on.", self.lines)
        self.wait_for_buttons(request)
        request.fire("Approve")
        self.tick_until(lambda: self.status_of(first)["status"] == "succeeded")
        self.assertIn("The person clicked Run code for request %s." % first, self.lines)
        self.assertEqual(self.status_of(first)["result"]["value"], 42)
        self.assertFalse(request.shown)

        second = self.command("run", {"code": "result = 1"})
        self.tick_until(lambda: request.shown)
        self.assertEqual(request.Find("RequestCode").PlainText, "result = 1")
        request.fire(panel_module.REQUEST_ID, "Close")  # closing leaves it waiting
        self.assertFalse(request.shown)
        self.disp.tick(panel_module.TIMER_ID)
        self.assertFalse(request.shown, "a closed request must not pop up again by itself")
        self.assertTrue(self.visible("Review"))
        self.win.fire("Review")
        self.assertTrue(request.shown)
        self.assertFalse(request.Find("Deny").Enabled, "showing it again turns the buttons off again")
        self.wait_for_buttons(request)
        request.fire("Deny")
        self.tick_until(lambda: self.status_of(second)["status"] == "failed")
        self.assertEqual(self.status_of(second)["error"], "The person at DaVinci Resolve clicked Deny, so this did not run.")
        self.tick_until(lambda: [r.Text[2] for r in self.item("Activity").rows][:2] == ["Failed", "Done"])
        self.assertEqual([r.Text[1] for r in self.item("Activity").rows][:2], ["run", "run"])

    def test_allow_agent_switch(self):
        self.sign_in()
        self.item("AllowAgent").Checked = False
        self.win.fire("AllowAgent")
        self.assertFalse(Settings(self.tmp)["allow_agent"])
        self.assertFalse(self.controller.snapshot["allow_agent"])

    def test_close_switches_off(self):
        self.sign_in()
        sid = support.call(self.server, "GET", "/v1/bridge/sessions")[1]["sessions"][0]["id"]
        self.win.fire(panel_module.WINDOW_ID, "Close")
        self.assertTrue(self.disp.exited)
        self.assertIsNone(self.controller.worker)
        deleted = support.call(self.server, "GET", "/mock/state", token=None)[1]["deleted_sessions"]
        self.assertIn(sid, [d["session_id"] for d in deleted])
        self.assertTrue(Settings(self.tmp)["connected"], "closing the window keeps Connected for next time")

    def _fake_resolve(self, windows_on_screen=None):
        """A Resolve with a project manager, and resolve_windows_on_screen
        answering windows_on_screen (None, as on macOS, by default)."""
        class Folder:
            def __init__(self, clips, bins):
                self.clips, self.bins = clips, bins

            def GetClipList(self):
                return list(self.clips)

            def GetSubFolderList(self):
                return list(self.bins)

        class Pool:
            def __init__(self, folder):
                self.folder = folder

            def GetRootFolder(self):
                return self.folder

        class Project:
            def __init__(self, uid, name="Client cut", timelines=0, clips=(), bins=()):
                self.uid, self.name, self.timelines = uid, name, timelines
                self.pool = Pool(Folder(clips, bins))

            def GetUniqueId(self):
                return self.uid

            def GetName(self):
                return self.name

            def GetTimelineCount(self):
                return self.timelines

            def GetMediaPool(self):
                return self.pool

        class Manager:
            def __init__(self):
                self.project = Project("project-a", timelines=2, clips=["A001.mov"])
                self.library = {"Client cut": "Wed Oct 28 18:02:11 2026"}

            def GetCurrentProject(self):
                return self.project

            def GetProjectListInCurrentFolder(self):
                return sorted(self.library)

            def GetProjectLastModifiedTime(self, name):
                return self.library.get(name, "")

        class Resolve:
            def __init__(self):
                self.manager = Manager()
                self.quits = 0

            def GetProjectManager(self):
                return self.manager

            def Quit(self):
                self.quits += 1

        fake_resolve = Resolve()
        fake_resolve.Project = Project
        self.panel.resolve = fake_resolve
        self.addCleanup(setattr, panel_module, "resolve_windows_on_screen", panel_module.resolve_windows_on_screen)
        panel_module.resolve_windows_on_screen = lambda: windows_on_screen
        for _ in range(panel_module.PROJECT_CHECK_TICKS):
            self.disp.tick(panel_module.TIMER_ID)
        return fake_resolve

    def _resolve_quits(self, resolve):
        """What Resolve does on a quit: it closes the project for a new empty
        Untitled Project (not in the library), then closes script windows."""
        resolve.manager.project = resolve.Project("untitled-new", panel_module.UNTITLED_PROJECT)
        self.win.fire(panel_module.WINDOW_ID, "Close")

    def _closes_without_quitting(self, resolve):
        self.assertTrue(self.disp.exited)
        self.assertFalse(self.panel._finish_quit)
        self.assertEqual(resolve.quits, 0)
        self.assertIn("The NOLGIA window closed; switching off.", self.lines)

    def test_resolve_quitting_closes_the_window_and_the_quit_is_finished(self):
        # The owner's Mac test 2: with the NOLGIA window open, Quit DaVinci
        # Resolve left Resolve open with no windows.
        resolve = self._fake_resolve()
        self._resolve_quits(resolve)
        self.assertTrue(self.disp.exited)
        self.assertTrue(self.panel._finish_quit)
        self.panel._quit_resolve()
        self.assertEqual(resolve.quits, 1)
        self.assertTrue(any(line.startswith("DaVinci Resolve is quitting (its new Untitled Project opened ")
                            and line.endswith(" s before the window closed); switching off.")
                            for line in self.lines), self.lines)

    def test_resolve_quitting_on_windows_with_its_windows_gone_is_finished(self):
        resolve = self._fake_resolve(windows_on_screen=False)
        with mock.patch("sys.platform", "win32"):
            self._resolve_quits(resolve)
        self.assertTrue(self.panel._finish_quit)

    def test_a_new_untitled_project_then_a_close_after_0_6_s_never_quits_resolve(self):
        # Resolve closes script windows 0.02 to 0.03 s after its new Untitled
        # Project opens; 0.6 s later is a person, never Resolve's quit.
        resolve = self._fake_resolve()
        resolve.manager.project = resolve.Project("untitled-new", panel_module.UNTITLED_PROJECT)
        self.disp.tick(panel_module.TIMER_ID)
        time.sleep(0.6)
        self.win.fire(panel_module.WINDOW_ID, "Close")
        self._closes_without_quitting(resolve)
        self.assertTrue(any(line.startswith("The NOLGIA window closed 0.6") and "DaVinci Resolve stays open" in line
                            for line in self.lines), self.lines)

    def test_the_quit_window_is_half_a_second_and_the_project_is_read_every_tick(self):
        self.assertEqual(panel_module.QUIT_SWITCH_SECONDS, 0.5)
        resolve = self._fake_resolve()
        resolve.manager.project = resolve.Project("project-b", "Ad spot")
        self.disp.tick(panel_module.TIMER_ID)
        self.assertEqual(self.panel._project_seen, "project-b")

    def test_closing_the_window_never_quits_resolve(self):
        resolve = self._fake_resolve()
        self.win.fire(panel_module.WINDOW_ID, "Close")
        self._closes_without_quitting(resolve)

    def test_opening_a_real_project_then_closing_never_quits_resolve(self):
        resolve = self._fake_resolve()
        resolve.manager.library["Ad spot"] = "Thu Oct 29 09:10:00 2026"
        resolve.manager.project = resolve.Project("project-b", "Ad spot", timelines=1, clips=["B001.mov"])
        self.win.fire(panel_module.WINDOW_ID, "Close")
        self._closes_without_quitting(resolve)
        self.assertTrue(any("DaVinci Resolve stays open" in line for line in self.lines))

    def test_opening_an_empty_saved_project_then_closing_never_quits_resolve(self):
        resolve = self._fake_resolve()
        resolve.manager.library["Ideas"] = "Thu Oct 29 09:10:00 2026"
        resolve.manager.project = resolve.Project("project-b", "Ideas")
        self.win.fire(panel_module.WINDOW_ID, "Close")
        self._closes_without_quitting(resolve)

    def test_a_new_project_made_by_hand_then_closing_never_quits_resolve(self):
        # New Project gives a new, empty, unsaved "Untitled Project", just as
        # Resolve's quit does; closing the window later than that is no quit.
        resolve = self._fake_resolve()
        resolve.manager.project = resolve.Project("hand-new", panel_module.UNTITLED_PROJECT)
        for _ in range(panel_module.PROJECT_CHECK_TICKS):
            self.disp.tick(panel_module.TIMER_ID)
        self.panel._project_changed_at -= panel_module.QUIT_SWITCH_SECONDS + 0.1
        self.win.fire(panel_module.WINDOW_ID, "Close")
        self._closes_without_quitting(resolve)

    def test_opening_a_saved_empty_untitled_project_never_quits_resolve(self):
        resolve = self._fake_resolve()
        resolve.manager.library[panel_module.UNTITLED_PROJECT] = "Thu Oct 29 09:10:00 2026"
        resolve.manager.project = resolve.Project("saved-untitled", panel_module.UNTITLED_PROJECT)
        self.win.fire(panel_module.WINDOW_ID, "Close")
        self._closes_without_quitting(resolve)

    def test_a_saved_project_known_only_by_its_modified_time_never_quits_resolve(self):
        resolve = self._fake_resolve()
        resolve.manager.GetProjectListInCurrentFolder = lambda: []
        resolve.manager.library[panel_module.UNTITLED_PROJECT] = "Thu Oct 29 09:10:00 2026"
        resolve.manager.project = resolve.Project("saved-untitled", panel_module.UNTITLED_PROJECT)
        self.win.fire(panel_module.WINDOW_ID, "Close")
        self._closes_without_quitting(resolve)

    def _untitled_with(self, **contents):
        resolve = self._fake_resolve()
        resolve.manager.project = resolve.Project("untitled-new", panel_module.UNTITLED_PROJECT, **contents)
        self.win.fire(panel_module.WINDOW_ID, "Close")
        self._closes_without_quitting(resolve)

    def test_an_untitled_project_with_a_timeline_never_quits_resolve(self):
        self._untitled_with(timelines=1)

    def test_an_untitled_project_with_a_clip_never_quits_resolve(self):
        self._untitled_with(clips=["A001.mov"])

    def test_an_untitled_project_with_a_bin_never_quits_resolve(self):
        self._untitled_with(bins=["Selects"])

    def test_going_back_to_a_project_seen_before_never_quits_resolve(self):
        resolve = self._fake_resolve()
        untitled = resolve.Project("untitled-1", panel_module.UNTITLED_PROJECT)
        resolve.manager.project = untitled
        for _ in range(panel_module.PROJECT_CHECK_TICKS):
            self.disp.tick(panel_module.TIMER_ID)
        resolve.manager.project = resolve.Project("project-a", timelines=2)
        for _ in range(panel_module.PROJECT_CHECK_TICKS):
            self.disp.tick(panel_module.TIMER_ID)
        resolve.manager.project = untitled
        self.win.fire(panel_module.WINDOW_ID, "Close")
        self._closes_without_quitting(resolve)

    def test_a_project_that_cannot_be_read_never_quits_resolve(self):
        resolve = self._fake_resolve()
        project = resolve.Project("untitled-new", panel_module.UNTITLED_PROJECT)
        project.GetMediaPool = lambda: None
        resolve.manager.project = project
        self.win.fire(panel_module.WINDOW_ID, "Close")
        self._closes_without_quitting(resolve)

    def test_resolve_still_on_screen_is_not_a_quit(self):
        resolve = self._fake_resolve(windows_on_screen=True)
        self._resolve_quits(resolve)
        self.assertFalse(self.panel._finish_quit)

    def test_on_windows_a_window_check_that_cannot_be_read_is_not_a_quit(self):
        resolve = self._fake_resolve(windows_on_screen=None)
        with mock.patch("sys.platform", "win32"):
            self._resolve_quits(resolve)
        self.assertFalse(self.panel._finish_quit)

    def test_a_close_long_after_resolve_s_switch_is_not_a_quit(self):
        resolve = self._fake_resolve()
        resolve.manager.project = resolve.Project("untitled-new", panel_module.UNTITLED_PROJECT)
        for _ in range(panel_module.PROJECT_CHECK_TICKS):
            self.disp.tick(panel_module.TIMER_ID)
        self.panel._project_changed_at -= panel_module.QUIT_SWITCH_SECONDS + 0.1
        self.win.fire(panel_module.WINDOW_ID, "Close")
        self._closes_without_quitting(resolve)

    def test_close_window_from_code_never_quits_resolve(self):
        resolve = self._fake_resolve()
        resolve.manager.project = resolve.Project("untitled-new", panel_module.UNTITLED_PROJECT)
        self.panel.close()
        self.assertFalse(self.panel._finish_quit)

    def test_a_quit_right_after_the_window_opens_is_finished(self):
        # Quitting before the first project check: the window noted the
        # project when it opened, so the switch to Untitled is still seen.
        resolve = self._fake_resolve()
        ctl = Controller(fake.sample(), has_window=True, env=self.env, log=self.lines.append)
        fusion, bmd = fake_ui.Fusion(), fake_ui.Bmd()
        panel = panel_module.Panel(resolve, fusion, bmd, controller=ctl)

        def run_loop():
            resolve.manager.project = resolve.Project("untitled-new", panel_module.UNTITLED_PROJECT)
            fusion.UIManager.windows[panel_module.WINDOW_ID].fire(panel_module.WINDOW_ID, "Close")

        panel.build = (lambda build: (lambda: (build(), setattr(bmd.dispatchers[0], "RunLoop", run_loop))[0]))(panel.build)
        self.assertTrue(panel.run())
        self.assertEqual(resolve.quits, 1)
        self.assertIn("Finishing DaVinci Resolve's quit.", self.lines)

    def test_reconnects_when_left_on(self):
        Settings(self.tmp).update(token="unit-token", token_api_url=self.server.base_url, connected=True)
        self.server.state.add_token("unit-token")
        ctl = Controller(fake.sample(), has_window=True, env=self.env, log=self.lines.append)
        fusion, bmd = fake_ui.Fusion(), fake_ui.Bmd()
        panel = panel_module.Panel(None, fusion, bmd, controller=ctl)
        bmd_disp_run = []

        def run_loop():
            bmd_disp_run.append(True)
            win = fusion.UIManager.windows[panel_module.WINDOW_ID]
            self.assertTrue(win.shown)
            self.assertTrue(ctl.connected)
            win.fire(panel_module.WINDOW_ID, "Close")

        panel.build = (lambda build: (lambda: (build(), setattr(bmd.dispatchers[0], "RunLoop", run_loop))[0]))(panel.build)
        self.assertTrue(panel.run())
        self.assertEqual(bmd_disp_run, [True])
        self.assertIsNone(ctl.worker)

    def test_open_window_brings_an_open_one_forward(self):
        import nolgia_resolve

        self.win.Show()
        self.assertFalse(nolgia_resolve.open_window(None, self.fusion, self.bmd))
        self.assertEqual(len(self.bmd.dispatchers), 1, "a second window was made")

    def test_close_window_from_run_code(self):
        self.sign_in()
        cid = self.command("run", {"code": "import nolgia_resolve\nresult = nolgia_resolve.close_window()"})
        self.tick_until(lambda: self.disp.exited)
        self.assertEqual(self.status_of(cid)["status"], "succeeded")
        self.assertIs(self.status_of(cid)["result"]["value"], True)
        self.assertIsNone(self.controller.worker)


if __name__ == "__main__":
    unittest.main()
