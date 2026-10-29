# SPDX-License-Identifier: GPL-3.0-or-later
"""The NOLGIA Agent chat in the NOLGIA window, against the mock API and a
stand-in UIManager: a chat pinned to this Resolve, one message per turn,
replies drawn and scrolled to, Stop, questions, the notices and what the
chat remembers."""

import shutil
import tempfile
import time
import unittest

import support
import fake_resolve as fake
import fake_ui

import json
import os

from nolgia_resolve import chatview
from nolgia_resolve import panel as panel_module
from nolgia_resolve.core import COST_LINE
from nolgia_resolve.core import chat as core_chat
from nolgia_resolve.runtime import Controller
from nolgia_resolve.settings import Settings


def remembered(folder):
    """What the chat remembers (core/chat.py's ChatStore, chat.json)."""
    try:
        with open(os.path.join(folder, "chat.json"), encoding="utf-8") as handle:
            return json.load(handle)
    except OSError:
        return None


def mock(server, path, body):
    status, data = support.call(server, "POST", path, body, token=None)
    assert status in (200, 204), (status, data)
    return data


class ChatWindowTest(unittest.TestCase):
    def setUp(self):
        self.server = support.start_mock(poll_wait_seconds=2, device_interval=1, auto_approve_after=1)
        self.tmp = tempfile.mkdtemp(prefix="nolgia-chat-")
        self.env = {"NOLGIA_API_URL": self.server.base_url, "NOLGIA_CONFIG_DIR": self.tmp,
                    "NOLGIA_INSTANCE_ID": "resolve-unit-1"}
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

    # ------------------------------------------------------------ helpers

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
        self.fail("timed out; status: %s; chat: %s" % (self.controller.status_line(), self.controller.chat.view()))

    def state(self):
        return support.call(self.server, "GET", "/mock/state", token=None)[1]

    def sign_in(self):
        self.win.fire("SignIn")
        self.tick_until(lambda: self.controller.connected and self.controller.worker.state == "connected"
                        and self.controller.chat.loaded)

    def type_and_send(self, text):
        self.item("Box").PlainText = text
        self.win.fire("Box", "TextChanged")
        self.win.fire("Send")

    def thread(self):
        return self.item("Thread").HTML

    # ------------------------------------------------------------ tests

    def test_signed_out_the_chat_is_hidden(self):
        for element_id in ("ChatTitle", "ChatIntro", "Thread", "Box", "Send", "ChatCost", "OpenWeb", "Example0"):
            self.assertIsNotNone(self.item(element_id), element_id)
            self.assertFalse(self.visible(element_id), element_id)
        # Every part fits the window's minimum size: UIManager windows do not
        # scroll as a whole, the thread and Activity scroll on their own.
        self.assertGreaterEqual(self.win.props["MinimumSize"][1], 760)
        self.assertGreaterEqual(self.item("Thread").props["MinimumSize"][1], 100)
        self.assertEqual(self.item("Activity").props["MaximumSize"][1], panel_module.ACTIVITY_MAX_PX)
        # Button text sets a minimum width: no example label may widen the window.
        for short, full in chatview.EXAMPLES:
            self.assertLessEqual(len(short), panel_module.EXAMPLE_CHARS, short)
            self.assertTrue(full.endswith("."), full)

    def test_first_look_cost_line_and_examples(self):
        mock(self.server, "/mock/agent", {"model": "claude-opus-5-5"})
        self.sign_in()
        self.tick_until(lambda: self.visible("ChatIntro"))
        for element_id in ("ChatTitle", "ChatIntro", "Example0", "Example1", "Example2", "OpenWeb", "Box", "Send",
                           "ChatCost"):
            self.assertTrue(self.visible(element_id), element_id)
        for element_id in ("Thread", "NewChat", "Stop", "ChatAction", "ChatNotice", "Option0"):
            self.assertFalse(self.visible(element_id), element_id)
        cost = self.item("ChatCost").Text
        self.assertEqual(cost, "NOLGIA Agent works right here, each turn from 70 credits on your model. "
                               "You can also use Claude, Cursor, Codex or another agent with NOLGIA connected.")
        self.assertNotIn("free", cost.lower())
        self.win.fire("Example0")
        self.assertEqual(self.item("Box").PlainText, chatview.EXAMPLES[0][1])
        # Before the first message, "Open on nolgia.ai" opens NOLGIA Agent there.
        self.win.fire("OpenWeb")
        self.assertEqual(self.opened[-1], "https://nolgia.ai/agent")

    def test_cost_line_without_a_rate_reads_the_lowest_one(self):
        mock(self.server, "/mock/agent", {"models_error": True})
        self.sign_in()
        self.assertEqual(self.item("ChatCost").Text, COST_LINE)
        self.assertIn("each turn from 10 credits", COST_LINE)

    def test_send_pins_a_new_chat_to_this_resolve_and_draws_the_reply(self):
        self.sign_in()
        self.type_and_send("Add my newest NOLGIA clip to the end of the timeline.")
        self.tick_until(lambda: self.state()["agent_submits"])
        st = self.state()
        self.assertEqual(st["agent_creates"], [{"name": "DaVinci Resolve: Add my newest NOLGIA clip to",
                                                "app": {"app": "resolve", "instance_id": "resolve-unit-1"}}])
        session = st["agent_sessions"][0]
        self.assertEqual(session["app"], {"app": "resolve", "instance_id": "resolve-unit-1"})
        # Plain messages: one message is one turn (no steering).
        self.assertEqual(st["agent_submits"], [{"content": "Add my newest NOLGIA clip to the end of the timeline.",
                                                "session_id": session["id"]}])
        self.tick_until(lambda: self.item("Box").PlainText == "" and self.visible("ChatWorking"))
        self.assertEqual(self.item("ChatWorking").Text,
                         "NOLGIA Agent is working. What it does in DaVinci Resolve shows in Activity below.")
        self.assertFalse(self.item("Send").Enabled)
        self.assertTrue(self.visible("Stop"))
        self.assertTrue(self.visible("Thread"))
        self.assertIn("Add my newest NOLGIA clip", self.thread())
        self.assertEqual(self.item("Thread").scrolls[-1], ("anchor", "end"), "a new message is shown from the end")
        self.assertEqual(remembered(self.tmp)["id"], session["id"])
        # A second message while the first runs: nothing is sent.
        self.type_and_send("And another thing")
        time.sleep(0.3)
        self.assertEqual(len(self.state()["agent_submits"]), 1)

        reply = mock(self.server, "/mock/agent/reply", {"content": "I added **sunset.mp4** to the end of "
                                                                   "Timeline 1.\n\n- 5 s long\n- on V1\n\n"
                                                                   "See https://nolgia.ai/agent and <b>this</b>."})
        self.tick_until(lambda: not self.visible("ChatWorking"))
        html = self.thread()
        self.assertIn("<b>sunset.mp4</b>", html)
        self.assertIn("&#8226; 5 s long", html)
        self.assertIn('<a href="https://nolgia.ai/agent">https://nolgia.ai/agent</a>', html)
        self.assertIn("&lt;b&gt;this&lt;/b&gt;", html, "reply text is escaped, never drawn as HTML")
        self.assertIn("10 credits", html)
        self.assertEqual(self.item("Thread").scrolls[-1], ("anchor", chatview.anchor(reply["id"])),
                         "a finished reply is shown from its start")
        self.assertTrue(self.item("Send").Enabled)
        self.assertFalse(self.visible("Stop"))
        self.assertTrue(self.visible("NewChat") and self.visible("OpenWeb"))
        self.assertFalse(self.visible("ChatIntro"))
        self.win.fire("OpenWeb")
        self.assertEqual(self.opened[-1], "https://nolgia.ai/agent/" + session["id"])

        # The same chat carries on; New chat starts another, pinned too.
        self.type_and_send("Thanks")
        self.tick_until(lambda: len(self.state()["agent_submits"]) == 2)
        self.assertEqual(self.state()["agent_submits"][1]["session_id"], session["id"])
        mock(self.server, "/mock/agent/reply", {"content": "Done."})
        self.tick_until(lambda: not self.controller.chat.busy and "Done." in self.thread())
        self.win.fire("NewChat")
        self.assertIsNone(remembered(self.tmp))
        self.tick_until(lambda: self.visible("ChatIntro"))
        self.type_and_send("Start over")
        self.tick_until(lambda: len(self.state()["agent_creates"]) == 2)
        self.assertEqual(self.state()["agent_creates"][1]["app"], {"app": "resolve", "instance_id": "resolve-unit-1"})

    def test_enter_at_the_end_sends(self):
        self.sign_in()
        box = self.item("Box")
        box.PlainText = "Give every clip the Kodak Portra 400 look"
        self.win.fire("Box", "TextChanged")
        box.PlainText = "Give every clip the Kodak Portra 400 look\n"
        self.win.fire("Box", "TextChanged")
        self.tick_until(lambda: self.state()["agent_submits"])
        self.assertEqual(self.state()["agent_submits"][0]["content"], "Give every clip the Kodak Portra 400 look")
        # A newline typed in the middle is just a newline.
        mock(self.server, "/mock/agent/reply", {"content": "ok"})
        self.tick_until(lambda: not self.controller.chat.busy)
        box.PlainText = "line one"
        self.win.fire("Box", "TextChanged")
        box.PlainText = "line\n one"
        self.win.fire("Box", "TextChanged")
        time.sleep(0.2)
        self.disp.tick(panel_module.TIMER_ID)
        self.assertEqual(len(self.state()["agent_submits"]), 1)

    def test_stop_ends_the_turn(self):
        self.sign_in()
        self.type_and_send("Make a rough cut")
        self.tick_until(lambda: self.visible("Stop"))
        self.win.fire("Stop")
        self.tick_until(lambda: "Stopped." in self.thread())
        self.assertFalse(self.visible("Stop"))
        self.assertTrue(self.item("Send").Enabled)

    def test_a_question_is_answered_with_its_buttons(self):
        self.sign_in()
        self.type_and_send("Make an abstract piece")
        self.tick_until(lambda: self.state()["agent_submits"])
        mock(self.server, "/mock/agent/reply", {"content": "Where should it go?", "question": {
            "text": "Where should it go?", "options": [
                {"label": "In DaVinci Resolve", "description": "on the timeline"},
                {"label": "A new clip made on NOLGIA and put in my library, priced first", "description": ""}]}})
        self.tick_until(lambda: self.visible("Option0"))
        self.assertEqual(self.item("Option0").Text, "In DaVinci Resolve")
        self.assertLessEqual(len(self.item("Option1").Text), panel_module.OPTION_CHARS)
        self.assertTrue(self.item("Option1").Text.endswith("..."))
        self.assertFalse(self.visible("Option2"))
        self.win.fire("Option0")
        self.tick_until(lambda: len(self.state()["agent_submits"]) == 2)
        self.assertEqual(self.state()["agent_submits"][1]["content"], "In DaVinci Resolve")
        self.tick_until(lambda: not self.visible("Option0"))

    def test_out_of_credits_is_said_once(self):
        self.sign_in()
        self.type_and_send("Make a rough cut")
        self.tick_until(lambda: self.state()["agent_submits"])
        mock(self.server, "/mock/agent/reply", {"status": "failed", "error": "not enough credits",
                                                "error_code": "insufficient_credits"})
        self.tick_until(lambda: self.visible("ChatNotice"))
        self.assertEqual(self.item("ChatNotice").Text,
                         "You are out of credits for NOLGIA Agent. Add credits on nolgia.ai to keep going.")
        self.assertEqual(self.item("ChatAction").Text, "Add credits")
        self.assertNotIn("out of credits", self.thread())
        self.win.fire("ChatAction")
        self.assertEqual(self.opened[-1], "https://nolgia.ai/billing")

    def test_no_agent_yet(self):
        mock(self.server, "/mock/agent", {"provisioned": False})
        self.sign_in()
        self.type_and_send("Hello")
        self.tick_until(lambda: self.visible("ChatNotice"))
        self.assertIn("Your NOLGIA Agent is not set up yet", self.item("ChatNotice").Text)
        self.assertEqual(self.item("ChatAction").Text, "Open NOLGIA Agent")
        self.assertEqual(self.state()["agent_creates"], [], "no chat is started without an agent")
        self.tick_until(lambda: self.item("Box").PlainText == "Hello")  # the message comes back to the box

    def test_never_chats_unpinned(self):
        # An API that does not know `app` yet refuses it: nothing is sent or kept.
        mock(self.server, "/mock/agent", {"app_pin": False})
        self.sign_in()
        self.type_and_send("Hello")
        self.tick_until(lambda: self.visible("ChatNotice"))
        st = self.state()
        self.assertEqual(st["agent_submits"], [])
        self.assertEqual(st["agent_sessions"], [])
        self.assertIsNone(remembered(self.tmp))
        self.assertIn("unknown field", self.item("ChatNotice").Text)
        self.tick_until(lambda: self.item("Box").PlainText == "Hello")

    def test_blockers_and_their_fixes(self):
        self.sign_in()
        self.item("AllowAgent").Checked = False
        self.win.fire("AllowAgent")
        self.tick_until(lambda: self.visible("ChatNotice"))
        self.assertEqual(self.item("ChatNotice").Text, "NOLGIA Agent can work here only while Allow NOLGIA Agent is on.")
        self.assertEqual(self.item("ChatAction").Text, "Allow NOLGIA Agent")
        self.assertFalse(self.item("Send").Enabled)
        self.win.fire("ChatAction")
        self.assertTrue(Settings(self.tmp)["allow_agent"])
        self.tick_until(lambda: not self.visible("ChatNotice"))
        self.win.fire("Pause")
        self.tick_until(lambda: self.controller.worker is None and self.visible("ChatNotice"))
        self.assertEqual(self.item("ChatNotice").Text,
                         "Turn on Connected so NOLGIA Agent can work in this DaVinci Resolve.")
        self.win.fire("ChatAction")
        self.tick_until(lambda: self.controller.connected and not self.visible("ChatNotice"))

    def test_remembered_chat_is_used_only_when_pinned_here(self):
        self.sign_in()
        self.type_and_send("First")
        self.tick_until(lambda: self.state()["agent_submits"])
        mock(self.server, "/mock/agent/reply", {"content": "First reply"})
        self.tick_until(lambda: "First reply" in self.thread())
        session_id = remembered(self.tmp)["id"]
        self.controller.shutdown(wait=5)

        # The same install opens the window again: the chat comes back.
        again = Controller(fake.sample(), has_window=True, env=self.env, log=self.lines.append, open_url=self.opened.append)
        fusion, bmd = fake_ui.Fusion(), fake_ui.Bmd()
        win = panel_module.Panel(None, fusion, bmd, controller=again).build()
        disp = bmd.dispatchers[0]
        again.connect()
        # The chat reads on its own thread; the window draws on its next tick.
        end = time.time() + 30
        while time.time() < end and "First reply" not in win.Find("Thread").HTML:
            disp.tick(panel_module.TIMER_ID)
            time.sleep(0.02)
        self.assertEqual(again.chat.session_id, session_id)
        self.assertIn("First reply", win.Find("Thread").HTML)
        again.shutdown(wait=5)

        # Another install (another instance id) never takes it over.
        env = dict(self.env, NOLGIA_INSTANCE_ID="resolve-unit-2")
        other = Controller(fake.sample(), has_window=True, env=env, log=self.lines.append)
        fusion, bmd = fake_ui.Fusion(), fake_ui.Bmd()
        panel_module.Panel(None, fusion, bmd, controller=other).build()
        disp = bmd.dispatchers[0]
        other.connect()
        end = time.time() + 30
        while time.time() < end and not other.chat.loaded:
            disp.tick(panel_module.TIMER_ID)
            time.sleep(0.02)
        self.assertIsNone(other.chat.session_id)
        self.assertEqual(other.chat.view()["rows"], [])
        other.shutdown(wait=5)

    def test_sign_out_forgets_the_chat(self):
        self.sign_in()
        self.type_and_send("First")
        self.tick_until(lambda: remembered(self.tmp))
        self.win.fire("SignOut")
        self.tick_until(lambda: self.controller.worker is None)
        self.assertIsNone(remembered(self.tmp))
        self.assertFalse(self.visible("Box"))
        self.assertIsNone(self.controller.chat.session_id)


class ChatViewTest(unittest.TestCase):
    def view(self, rows, hidden=0):
        return {"hidden": hidden, "rows": rows}

    def row(self, id_, role, status="complete", text="", **extra):
        out = {"id": id_, "role": role, "status": status, "background": False, "queued": False, "text": text,
               "blocks": core_chat.format_reply(text) if role == "agent" and status == "complete" else None,
               "failure": None, "question": None, "credits": None}
        out.update(extra)
        return out

    def test_thread_html_escapes_and_marks(self):
        view = self.view([self.row("u-1", "user", text="<b>hi</b>\nthere"),
                          self.row("a-1", "agent", text="**Done**: see https://nolgia.ai/agent", credits=70),
                          self.row("a-2", "agent", status="pending")], hidden=2)
        html = chatview.thread_html(view)
        self.assertIn("Earlier messages are on nolgia.ai.", html)
        self.assertIn("&lt;b&gt;hi&lt;/b&gt;<br>there", html)
        self.assertIn('<a name="mu1"></a>', html)
        self.assertIn("<b>Done</b>", html)
        self.assertIn('<a href="https://nolgia.ai/agent">https://nolgia.ai/agent</a>', html)
        self.assertIn("70 credits", html)
        self.assertNotIn("ma2", html, "a reply being worked on is not drawn")
        self.assertTrue(html.endswith('<p><a name="end"></a>&nbsp;</p>'))

    def test_the_button_hint_only_when_there_are_buttons(self):
        hint = "Answer with a button below"
        open_question = {"text": "Switch to vertical?", "options": []}
        with_answers = {"text": "Where?", "options": [{"label": "In DaVinci Resolve", "description": ""}]}
        html = chatview.thread_html(self.view([self.row("a-1", "agent", text="Switch?", question=open_question)]))
        self.assertNotIn(hint, html)
        html = chatview.thread_html(self.view([self.row("a-1", "agent", text="Where?", question=with_answers)]))
        self.assertIn(hint, html)

    def test_scroll_target(self):
        done = self.view([self.row("u-1", "user", text="x"), self.row("a-1", "agent", text="y")])
        self.assertEqual(chatview.scroll_target(done), chatview.anchor("a-1"))
        working = self.view([self.row("u-1", "user", text="x"), self.row("a-1", "agent", status="pending")])
        self.assertEqual(chatview.scroll_target(working), "end")
        self.assertEqual(chatview.scroll_target(self.view([])), "end")

    def test_copy_has_no_dashes_and_no_free_pitch(self):
        texts = [core_chat.cost_line(70), core_chat.cost_line(None)] + [s for pair in chatview.EXAMPLES for s in pair]
        for text in texts:
            self.assertNotIn(" - ", text)
            self.assertNotIn(chr(0x2014), text)
            self.assertNotIn("free", text.lower())
            self.assertNotIn("Nolgia", text)
        self.assertTrue(core_chat.cost_line(70).startswith("NOLGIA Agent works right here"))


if __name__ == "__main__":
    unittest.main()
