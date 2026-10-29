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

from nolgia_resolve import chat as chat_module
from nolgia_resolve import panel as panel_module
from nolgia_resolve.runtime import Controller
from nolgia_resolve.settings import Settings


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
        for short, full in chat_module.EXAMPLES:
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
                               "You can also use Claude Code, Cursor, Codex or another agent with NOLGIA connected.")
        self.assertNotIn("free", cost.lower())
        self.win.fire("Example0")
        self.assertEqual(self.item("Box").PlainText, chat_module.EXAMPLES[0][1])
        # Before the first message, "Open on nolgia.ai" opens NOLGIA Agent there.
        self.win.fire("OpenWeb")
        self.assertEqual(self.opened[-1], "https://nolgia.ai/agent")

    def test_cost_line_without_a_rate_reads_the_lowest_one(self):
        mock(self.server, "/mock/agent", {"models_error": True})
        self.sign_in()
        self.assertEqual(self.item("ChatCost").Text, chat_module.COST_LINE)
        self.assertIn("each turn from 10 credits.", chat_module.COST_LINE)

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
        self.assertEqual(Settings(self.tmp)["chat_session_id"], session["id"])
        self.assertEqual(Settings(self.tmp)["chat_instance_id"], "resolve-unit-1")
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
        self.assertEqual(self.item("Thread").scrolls[-1], ("anchor", chat_module.anchor(reply["id"])),
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
        self.assertEqual(Settings(self.tmp)["chat_session_id"], "")
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
        self.assertEqual(self.item("Box").PlainText, "Hello", "the message stays in the box")

    def test_never_chats_unpinned(self):
        # An API that does not know `app` yet refuses it: nothing is sent or kept.
        mock(self.server, "/mock/agent", {"app_pin": False})
        self.sign_in()
        self.type_and_send("Hello")
        self.tick_until(lambda: self.visible("ChatNotice"))
        st = self.state()
        self.assertEqual(st["agent_submits"], [])
        self.assertEqual(st["agent_sessions"], [])
        self.assertEqual(Settings(self.tmp)["chat_session_id"], "")
        self.assertIn("unknown field", self.item("ChatNotice").Text)

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
        session_id = Settings(self.tmp)["chat_session_id"]
        self.controller.shutdown(wait=5)

        # The same install opens the window again: the chat comes back.
        again = Controller(fake.sample(), has_window=True, env=self.env, log=self.lines.append, open_url=self.opened.append)
        fusion, bmd = fake_ui.Fusion(), fake_ui.Bmd()
        win = panel_module.Panel(None, fusion, bmd, controller=again).build()
        disp = bmd.dispatchers[0]
        again.connect()
        end = time.time() + 30
        while time.time() < end and not again.chat.loaded:
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
        self.tick_until(lambda: Settings(self.tmp)["chat_session_id"])
        self.win.fire("SignOut")
        self.tick_until(lambda: self.controller.worker is None)
        self.assertEqual(Settings(self.tmp)["chat_session_id"], "")
        self.assertFalse(self.visible("Box"))
        self.assertIsNone(self.controller.chat.session_id)


class ChatTextTest(unittest.TestCase):
    def test_format_reply(self):
        blocks = chat_module.format_reply("# Done\nI **added** `two` *clips* now.\n\n1. one\n- two\n```\ncode\n```\n---\nend")
        self.assertEqual(blocks[0], {"type": "p", "parts": [{"text": "Done", "bold": True}]})
        self.assertEqual(blocks[1]["parts"], [{"text": "I "}, {"text": "added", "bold": True}, {"text": " two clips now."}])
        self.assertEqual([b["type"] for b in blocks[2:4]], ["li", "li"])
        self.assertEqual(blocks[-1]["parts"], [{"text": "end"}])

    def test_links(self):
        parts = chat_module.inline_parts("See [your chat](https://nolgia.ai/agent/x) or https://nolgia.ai/billing.")
        self.assertEqual(parts[1], {"text": "your chat", "href": "https://nolgia.ai/agent/x"})
        self.assertEqual(parts[3], {"text": "https://nolgia.ai/billing", "href": "https://nolgia.ai/billing"})
        self.assertEqual(chat_module.inline_parts("http://plain.example")[0], {"text": "http://plain.example"})

    def test_session_name_and_time_order(self):
        self.assertEqual(chat_module.session_name("  Add   my newest clip to the end of it"),
                         "DaVinci Resolve: Add my newest clip to the")
        self.assertLessEqual(len(chat_module.session_name("x" * 200)), 80)
        self.assertLess(chat_module.time_key("2026-10-29T01:00:01Z"), chat_module.time_key("2026-10-29T01:00:01.5Z"))
        self.assertLess(chat_module.time_key("2026-10-29T01:00:01.999999Z"), chat_module.time_key("2026-10-29T01:00:02Z"))

    def test_turn_rate(self):
        models = [{"model": "a", "turn_credits": 10, "default": True}, {"model": "b", "turn_credits": 70}]
        self.assertEqual(chat_module.turn_rate({"model": None}, models), 10)
        self.assertEqual(chat_module.turn_rate({"model": "b"}, models), 70)
        self.assertIsNone(chat_module.turn_rate(None, models))
        self.assertIsNone(chat_module.turn_rate({"model": "c"}, models))

    def test_copy_has_no_dashes_and_no_free_pitch(self):
        texts = [chat_module.cost_line(70), chat_module.cost_line(None)] + [full for _, full in chat_module.EXAMPLES]
        for row in ({"status": "failed", "error_code": code} for code in ("insufficient_credits", "timeout", "x")):
            texts.append(chat_module.failure_text(row))
        for text in texts:
            self.assertNotIn(" - ", text)
            self.assertNotIn(chr(0x2014), text)
            self.assertNotIn("free", text.lower())
            self.assertNotIn("Nolgia", text)


if __name__ == "__main__":
    unittest.main()
