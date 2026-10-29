# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 NOLGIA Inc
"""The chat in the NOLGIA window: what the person types goes to their NOLGIA
Agent, which works in this DaVinci Resolve through the plugin, and its
replies come back here.

It uses the routes nolgia.ai's own chat uses (POST /agent/messages, then the
pending reply row read until it settles), so a chat started here is the same
chat on nolgia.ai/agent. One chat per install: its session id is
remembered, "New chat" starts another. The session is pinned to this
Resolve (`app` on POST /agent/sessions: the app and this install's instance
id), so every turn tells the Agent the conversation is about this window. A
pin cannot be added later, so a session without this install's pin is never
used here.

One message is one turn: while the Agent works, the box waits (Stop ends
the turn). A message sent mid-turn would either reach the running turn or
become a turn of its own, billed on its own, depending on the Agent's pod,
and the person could not tell which before sending.

Threads: every API call runs on a short-lived thread; its answer is handed
to the main thread through the controller's `events` queue, so all of this
object's state is read and changed on the main thread only (the window's
timer drains the queue).

The same behaviour as the chat in the NOLGIA panel in Photoshop
(nolgiainc/nolgia-photoshop core/chat.js), in Python.
"""

import datetime
import html
import re
import threading
import time
import urllib.parse

from .core.api import ApiError, NetworkError, Unauthorized

APP = "resolve"
APP_NAME = "DaVinci Resolve"
AGENT_URL = "https://nolgia.ai/agent"
BILLING_URL = "https://nolgia.ai/billing"

MAX_CHARS = 8000  # SubmitAgentMessageRequest.content
SESSION_NAME_MAX = 80
ROWS_SHOWN = 40
WAKE_EVERY = 10 * 60  # seconds between wake calls, like the site's chat
PAGE = 200

# The line under the chat box, the same as the Photoshop panel's: NOLGIA Agent
# first (the person never needs to leave NOLGIA to get the work done), with
# what a turn costs on the person's own model (GET /agent/models for the
# Agent's brain), or the lowest rate when NOLGIA does not say; then the agents
# NOLGIA's MCP page documents (nolgia.ai/mcp: Claude Code, Cursor and Codex).
COST_LINE = ("NOLGIA Agent works right here, each turn from 10 credits. "
             "You can also use Claude Code, Cursor, Codex or another agent with NOLGIA connected.")
COST_LINE_RATE = ("NOLGIA Agent works right here, each turn from {credits} credits on your model. "
                  "You can also use Claude Code, Cursor, Codex or another agent with NOLGIA connected.")

# Before the first message: things this plugin and NOLGIA's DaVinci Resolve
# workflows do. A click puts the whole sentence in the box.
EXAMPLES = (
    ("Add my newest NOLGIA clip to the timeline", "Add my newest NOLGIA clip to the end of the timeline."),
    ("Give every clip the Kodak Portra 400 look", "Give every clip on this timeline the Kodak Portra 400 look."),
    ("Send this frame to my NOLGIA library", "Export the current frame as a PNG to my NOLGIA library."),
)


def poll_delay(pending_for):
    """Seconds before the next read of a pending reply: quick at first,
    slower once a turn has run for a while."""
    if pending_for < 30:
        return 1.5
    if pending_for < 120:
        return 3.0
    return 5.0


def session_name(text):
    """The session's name in the chat list on nolgia.ai: the app and the
    message's first six words."""
    words = " ".join(str(text or "").split()[:6])
    name = "%s: %s" % (APP_NAME, words or "New chat")
    if len(name) > SESSION_NAME_MAX:
        name = name[:SESSION_NAME_MAX - 3].rstrip() + "..."
    return name


_STAMP = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)$")


def time_key(stamp):
    """An RFC 3339 time as a string that sorts in time order, to the
    microsecond the API serves ("...01Z" sorts after "...01.5Z" as text)."""
    m = _STAMP.match(str(stamp or ""))
    if not m:
        return "0" * 21
    zone = "+00:00" if m.group(3) == "Z" else m.group(3)
    try:
        when = datetime.datetime.fromisoformat(m.group(1) + zone)
    except ValueError:
        return "0" * 21
    return "%015d%s" % (int(when.timestamp()), ((m.group(2) or "") + "000000")[:6])


def now_stamp(clock):
    when = datetime.datetime.fromtimestamp(clock(), datetime.timezone.utc)
    return when.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def turn_rate(agent, models):
    """What one turn costs on the Agent's brain: its catalogue rate (the
    default brain's when none is chosen), or None when unknown."""
    if not agent or not isinstance(models, list):
        return None
    chosen = None
    for model in models:
        if not isinstance(model, dict):
            continue
        if agent.get("model") and model.get("model") == agent.get("model"):
            chosen = model
            break
        if not agent.get("model") and model.get("default"):
            chosen = model
            break
    credits = chosen.get("turn_credits") if chosen else None
    return credits if isinstance(credits, int) and not isinstance(credits, bool) and credits > 0 else None


def cost_line(rate):
    return COST_LINE_RATE.format(credits=rate) if rate else COST_LINE


def failure_text(row):
    """A failed or stopped reply, in words for the window."""
    if row.get("status") == "interrupted":
        return "Stopped."
    detail = str(row.get("error") or "").strip()
    code = row.get("error_code")
    if code == "insufficient_credits":
        return "You are out of credits for NOLGIA Agent. Add credits on nolgia.ai to keep going."
    if code == "timeout":
        return "NOLGIA Agent ran out of time on this one." + (" " + detail if detail else "")
    if code == "access_denied":
        return detail or "Your account cannot use NOLGIA Agent for this."
    return "NOLGIA Agent could not finish this." + (" " + detail if detail else "")


# ------------------------------------------------------------ reply formatting

_LINK = re.compile(r"\[([^\]\n]+)\]\((https://[^\s)]+)\)|(https://[^\s<>()]+[^\s<>().,;:!?'\"])")
_BOLD = re.compile(r"(\*\*[^*]+\*\*)")
_EM = re.compile(r"(^|\s)\*(\S[^*]*\S|\S)\*(?=\s|$)")


def inline_parts(line):
    """One line of a reply as pieces: plain, bold, or a link. Markdown
    emphasis and code marks are dropped; only https links become links."""
    parts = []

    def push_text(text):
        for piece in _BOLD.split(text or ""):
            if not piece:
                continue
            bold = piece.startswith("**") and piece.endswith("**") and len(piece) > 4
            clean = _EM.sub(r"\1\2", (piece[2:-2] if bold else piece).replace("`", ""))
            if clean:
                parts.append({"text": clean, "bold": True} if bold else {"text": clean})

    last = 0
    for m in _LINK.finditer(line):
        push_text(line[last:m.start()])
        if m.group(2):
            parts.append({"text": m.group(1), "href": m.group(2)})
        else:
            parts.append({"text": m.group(3), "href": m.group(3)})
        last = m.end()
    push_text(line[last:])
    return parts


def format_reply(text):
    """A reply as blocks the window draws: paragraphs and list items."""
    blocks = []
    paragraph = []

    def flush():
        if paragraph:
            blocks.append({"type": "p", "parts": inline_parts(" ".join(paragraph))})
            del paragraph[:]

    for raw in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.strip()
        if not line or line.startswith("```") or re.match(r"^(-{3,}|\*{3,})$", line):
            flush()
            continue
        heading = re.match(r"^#{1,6}\s+(.*)$", line)
        if heading:
            flush()
            blocks.append({"type": "p", "parts": [{"text": heading.group(1).replace("**", ""), "bold": True}]})
            continue
        item = re.match(r"^(?:[-*+]|\d+[.)])\s+(.*)$", line)
        if item:
            flush()
            blocks.append({"type": "li", "parts": inline_parts(item.group(1))})
            continue
        paragraph.append(line)
    flush()
    return blocks


# ---------------------------------------------------------------- the chat

class AgentChat:
    """`owner` is the Controller: chat_api() (an ApiClient with the sign in,
    or None), instance_id() (this install's id while connected, or None),
    base_url(), account_email(), settings (chat_* keys), events (a queue the
    main thread drains), bump(), log(text) and chat_auth_failed(text)."""

    def __init__(self, owner, clock=time.time, spawn=None):
        self.owner = owner
        self.clock = clock
        self._spawn_thread = spawn or _spawn
        self._generation = 0
        self._clear()

    def _clear(self):
        self.session_id = None
        self.rows = {}  # id -> AgentMessage
        self.sending = False
        self.stopping = False
        self.notice = None  # {kind, text, action: {label, url} | None}
        self.agent = None  # GET /agent; None until read
        self.agent_read = False
        self.rate = None  # credits a turn costs on the Agent's brain, when known
        self.loaded = False
        self.started_for = None  # the instance id start() ran for
        self._generation += 1
        self._pending_since = {}
        self._last_wake = 0.0
        self._next_poll = None  # clock() time of the next read of pending replies
        self._reading = False

    # ---------------------------------------------------------- threads

    def _run(self, work, done, gen=None):
        """Run `work()` (network) on a thread; `done(value, error)` on the
        main thread afterwards, unless the chat was reset meanwhile."""
        gen = self._generation if gen is None else gen
        events = self.owner.events

        def body():
            value, error = None, None
            try:
                value = work()
            except BaseException as err:  # handed to the main thread
                error = err

            def finish():
                if gen == self._generation:
                    done(value, error)

            events.put(finish)

        self._spawn_thread(body)

    # -------------------------------------------------------- lifecycle

    def start(self):
        """Connected: read the remembered chat (if it is this account's and
        pinned to this install) and what the Agent costs. Safe to call again:
        it runs once per instance id."""
        instance = self.owner.instance_id()
        api = self.owner.chat_api()
        if not instance or api is None or self.started_for == instance:
            return
        self.started_for = instance
        saved = self._recall(instance)
        gen = self._generation

        def work():
            out = {"session": None, "rows": [], "agent": None, "agent_read": False, "rate": None}
            if saved:
                out["session"] = self._pinned_here(api, saved, instance)
            out["agent"], out["agent_read"], out["rate"] = self._read_agent(api)
            if out["session"]:
                rows = self._read_transcript(api, out["session"])
                if rows is None:  # deleted on nolgia.ai
                    out["session"] = None
                    out["forget"] = True
                else:
                    out["rows"] = rows
            return out

        def done(out, error):
            if error is not None:
                self._read_failed(error)
                return
            if out.get("forget") or (saved and not out["session"]):
                self._remember(None)
            self.session_id = out["session"]
            if out["agent_read"]:
                self.agent, self.agent_read, self.rate = out["agent"], True, out["rate"]
            self._merge(out["rows"])
            self.loaded = True
            self._schedule_poll()
            self.owner.bump()

        self._run(work, done, gen)

    def reset(self, forget=True):
        """Signed out, or the sign in changed: forget this chat on this
        computer (forget=False keeps it for the same account's next sign in)."""
        if forget:
            self._remember(None)
        self._clear()
        self.owner.bump()

    def new_chat(self):
        """"New chat": the next message starts a fresh session."""
        self._remember(None)
        agent, agent_read, rate, started = self.agent, self.agent_read, self.rate, self.started_for
        self._clear()
        self.agent, self.agent_read, self.rate, self.started_for = agent, agent_read, rate, started
        self.loaded = True
        self.owner.bump()

    def _recall(self, instance):
        settings = self.owner.settings
        session = settings.get("chat_session_id")
        if not session:
            return None
        if settings.get("chat_api_url") and settings.get("chat_api_url") != self.owner.base_url():
            return None
        email = self.owner.account_email()
        if settings.get("chat_email") and email and settings.get("chat_email") != email:
            return None
        if settings.get("chat_instance_id") and settings.get("chat_instance_id") != instance:
            return None
        return session

    def _remember(self, session_id):
        try:
            if session_id:
                self.owner.settings.update(chat_session_id=session_id, chat_api_url=self.owner.base_url(),
                                           chat_email=self.owner.account_email() or "",
                                           chat_instance_id=self.owner.instance_id() or "")
            else:
                self.owner.settings.update(chat_session_id="", chat_api_url="", chat_email="", chat_instance_id="")
        except Exception as err:  # a convenience only
            self.owner.log("Chat: could not remember the chat (%s)." % err)

    # ------------------------------------------------------- network reads
    # (on a thread; they return values and never touch this object's state)

    @staticmethod
    def _pinned_here(api, session_id, instance):
        """The remembered session's id when it is still there and pinned to
        this install, else None."""
        try:
            session = api.request("GET", "/agent/sessions/%s" % urllib.parse.quote(session_id)).json() or {}
        except ApiError as err:
            if not isinstance(err, Unauthorized) and err.status in (400, 404):
                return None
            raise
        pin = session.get("app") or {}
        if pin.get("app") != APP or pin.get("instance_id") != instance or session.get("archived_at"):
            return None
        return session.get("id")

    @staticmethod
    def _read_agent(api):
        """(agent, read, rate): GET /agent and what a turn costs on its
        brain. A missing agent (404) is a read None; a failed read is not."""
        try:
            agent = api.request("GET", "/agent").json() or None
        except Unauthorized:
            raise
        except ApiError as err:
            if err.status == 404:
                return None, True, None
            return None, False, None
        except NetworkError:
            return None, False, None
        rate = None
        try:
            models = (api.request("GET", "/agent/models").json() or {}).get("models")
            rate = turn_rate(agent, models)
        except Unauthorized:
            raise
        except Exception:
            pass  # the cost line falls back to the lowest rate
        return agent, True, rate

    @staticmethod
    def _list_messages(api, session_id, since=None):
        query = {"session_id": session_id, "limit": PAGE}
        if since:
            query["since"] = since
        data = api.request("GET", "/agent/messages", query=query).json() or {}
        rows = data.get("messages")
        return rows if isinstance(rows, list) else []

    @classmethod
    def _read_transcript(cls, api, session_id):
        """Every row of the session, oldest first, or None when it is gone."""
        out, since = [], None
        for _ in range(20):
            try:
                rows = cls._list_messages(api, session_id, since)
            except ApiError as err:
                if not isinstance(err, Unauthorized) and err.status in (400, 404):
                    return None
                raise
            out.extend(rows)
            if len(rows) < PAGE:
                break
            since = rows[-1].get("created_at")
        return out

    # ---------------------------------------------------------- state

    def _merge(self, rows, local=False):
        """Rows from NOLGIA, or (`local`) the ones a send just made, shown
        until NOLGIA's own copies replace them. Only NOLGIA's times are read
        back as `since`, so this computer's clock never hides a row."""
        for row in rows or []:
            if not isinstance(row, dict) or not row.get("id"):
                continue
            if self.session_id and row.get("session_id") and row["session_id"] != self.session_id:
                continue
            merged = dict(self.rows.get(row["id"]) or {})
            merged.update(row)
            merged["local"] = local
            self.rows[row["id"]] = merged
            if row.get("status") == "pending":
                self._pending_since.setdefault(row["id"], self.clock())
            else:
                self._pending_since.pop(row["id"], None)

    def messages(self):
        """The rows, oldest first."""
        return sorted(self.rows.values(), key=lambda r: (time_key(r.get("created_at")), str(r.get("id"))))

    def pending(self):
        return [r for r in self.messages() if r.get("role") == "agent" and r.get("status") == "pending"]

    @property
    def busy(self):
        return self.sending or bool(self.pending())

    def _since(self):
        best = None
        for row in self.rows.values():
            if row.get("local"):
                continue
            if best is None or time_key(row.get("created_at")) > time_key(best):
                best = row.get("created_at")
        return best

    def _read_failed(self, error):
        self.loaded = True
        if isinstance(error, Unauthorized):
            self.owner.chat_auth_failed("Your NOLGIA sign in expired. Sign in again.")
            return
        self.notice = {"kind": "error", "text": _error_text(error), "action": None}
        self.owner.log("Chat: %s" % error)
        self.owner.bump()

    # -------------------------------------------------------- polling

    def _schedule_poll(self, delay=None):
        if not self.session_id or (not self.pending() and delay is None):
            self._next_poll = None
            return
        if delay is None:
            oldest = min(list(self._pending_since.values()) + [self.clock()])
            delay = poll_delay(self.clock() - oldest)
        self._next_poll = self.clock() + delay

    def tick(self):
        """From the window's timer: read pending replies when it is time."""
        if self._next_poll is None or self._reading or self.clock() < self._next_poll:
            return
        api = self.owner.chat_api()
        if api is None:
            return
        self._next_poll = None
        self._reading = True
        session_id = self.session_id
        since = self._since()
        pending_ids = [r["id"] for r in self.pending()]
        sleeping = self._agent_sleeping()

        def work():
            # New rows, then each pending reply on its own, so one failed read
            # never keeps a finished reply off the window.
            got, failure = [], None
            reads = [lambda: got.extend(self._list_messages(api, session_id, since))]
            for message_id in pending_ids:
                reads.append(lambda m=message_id: got.append(
                    api.request("GET", "/agent/messages/%s" % urllib.parse.quote(m)).json()))
            for read in reads:
                try:
                    read()
                except Unauthorized:
                    raise
                except Exception as err:
                    failure = failure or err
            agent = None
            if sleeping:
                try:
                    agent = api.request("GET", "/agent").json()
                except Unauthorized:
                    raise
                except Exception:
                    pass  # only names a sleeping agent
            return got, failure, agent

        def done(out, error):
            self._reading = False
            if isinstance(error, Unauthorized):
                self.owner.chat_auth_failed("Your NOLGIA sign in expired. Sign in again.")
                return
            if error is not None:
                out = ([], error, None)
            got, failure, agent = out
            self._merge(got)
            if agent:
                self.agent = agent
            if failure is not None:
                self.notice = {"kind": "network", "text": _error_text(failure), "action": None}
            elif self.notice and self.notice.get("kind") == "network":
                self.notice = None
            self._schedule_poll()
            self.owner.bump()

        self._run(work, done)

    def _agent_sleeping(self):
        return bool(self.agent and self.agent.get("sleep_state") in ("asleep", "waking"))

    def wake(self):
        """The person started typing: wake a sleeping agent early, so it is
        ready by the time they send (the site's chat does the same)."""
        if not self._agent_sleeping() or self.clock() - self._last_wake < WAKE_EVERY:
            return
        api = self.owner.chat_api()
        if api is None:
            return
        self._last_wake = self.clock()

        def done(agent, error):
            if error is None and isinstance(agent, dict):
                self.agent = agent
                self.owner.bump()

        self._run(lambda: api.request("POST", "/agent/wake").json(), done)

    # ----------------------------------------------------------- send

    def send(self, text, on_sent=None):
        """Send a message. Returns False when it cannot go now (the box
        keeps its text); else True, and `on_sent(accepted)` runs on the main
        thread when NOLGIA answered (accepted True: the window clears the box)."""
        content = str(text or "").strip()
        if not content or self.busy:
            return False
        if len(content) > MAX_CHARS:
            self.notice = {"kind": "error", "action": None,
                           "text": "That message is too long. Keep it under %d characters." % MAX_CHARS}
            self.owner.bump()
            return False
        api = self.owner.chat_api()
        instance = self.owner.instance_id()
        if api is None or not instance:
            self.notice = {"kind": "error", "action": None,
                           "text": "Turn on Connected so NOLGIA Agent can work in this %s." % APP_NAME}
            self.owner.bump()
            return False
        self.sending = True
        self.notice = None
        self.owner.bump()
        session_id = self.session_id
        agent_known = self.agent_read and self.agent is not None

        def work():
            out = {"agent": None, "agent_read": False, "rate": None}
            # No NOLGIA Agent on the account (read at start; read again, it may
            # have been set up on nolgia.ai since): say so before starting a chat.
            if not agent_known:
                out["agent"], out["agent_read"], out["rate"] = self._read_agent(api)
                if out["agent_read"] and out["agent"] is None:
                    raise ApiError(404, "Not Found", "no agent is provisioned for this account")
            sid = session_id
            if sid:
                try:
                    out["accepted"] = api.request("POST", "/agent/messages",
                                                  {"content": content, "session_id": sid}).json() or {}
                    out["session"] = sid
                    return out
                except ApiError as err:
                    # The remembered chat was deleted on nolgia.ai: carry on in a new one.
                    if isinstance(err, Unauthorized) or err.status != 404 or "session" not in err.detail.lower():
                        raise
                    out["dropped"] = True
            # A new chat: the session first, pinned to this Resolve (the pin
            # rides only on POST /agent/sessions), then the message into it.
            session = api.request("POST", "/agent/sessions", {
                "name": session_name(content), "app": {"app": APP, "instance_id": instance}}).json() or {}
            pin = session.get("app") or {}
            if not session.get("id") or pin.get("app") != APP or pin.get("instance_id") != instance:
                # Never chat in a session that is not pinned here: the pin cannot be added later.
                raise ApiError(500, "NOLGIA did not pin the new chat to this %s" % APP_NAME,
                               "Update NOLGIA for %s, or try again later." % APP_NAME)
            out["session"] = session["id"]
            out["created"] = True
            out["accepted"] = api.request("POST", "/agent/messages",
                                          {"content": content, "session_id": session["id"]}).json() or {}
            return out

        def done(out, error):
            self.sending = False
            if error is not None:
                self._send_failed(error)
                if on_sent:
                    on_sent(False)
                return
            if out["agent_read"]:
                self.agent, self.agent_read, self.rate = out["agent"], True, out["rate"]
            if out.get("dropped"):
                self.rows = {}
                self._pending_since = {}
            accepted = out["accepted"]
            new_session = accepted.get("session_id") or out["session"]
            if new_session != self.session_id:
                self.session_id = new_session
                self._remember(new_session)
            at = now_stamp(self.clock)
            self._merge([{"id": accepted.get("user_message_id"), "role": "user", "content": content,
                          "status": "complete", "session_id": self.session_id, "created_at": at}], local=True)
            if accepted.get("agent_message_id"):
                self._merge([{"id": accepted["agent_message_id"], "role": "agent", "content": "",
                              "status": "pending", "session_id": self.session_id, "created_at": at,
                              "user_message_id": accepted.get("user_message_id")}], local=True)
            self.loaded = True
            self._schedule_poll(0.4)
            self.owner.bump()
            if on_sent:
                on_sent(True)

        self._run(work, done)
        return True

    def _send_failed(self, error):
        if isinstance(error, Unauthorized):
            self.owner.chat_auth_failed("Your NOLGIA sign in expired. Sign in again.")
            return
        # A turn started elsewhere (this chat on nolgia.ai) is still running:
        # follow it, so the box waits for it like for its own.
        body = getattr(error, "body", None) or {}
        pending_id = body.get("pending_agent_message_id") if isinstance(error, ApiError) and error.status == 409 else None
        if pending_id and self.session_id:
            self._merge([{"id": pending_id, "role": "agent", "content": "", "status": "pending",
                          "session_id": self.session_id, "created_at": now_stamp(self.clock)}], local=True)
            self._schedule_poll(0.3)
        self.notice = _submit_notice(error)
        self.owner.log("Chat: could not send (%s)." % error)
        self.owner.bump()

    # ----------------------------------------------------------- stop

    def stop(self):
        """Stop the turn that is running. Generations it already started keep
        running on NOLGIA, as on nolgia.ai."""
        api = self.owner.chat_api()
        if not self.session_id or self.stopping or api is None:
            return
        self.stopping = True
        self.owner.bump()
        session_id = self.session_id

        def done(_value, error):
            self.stopping = False
            if isinstance(error, Unauthorized):
                self.owner.chat_auth_failed("Your NOLGIA sign in expired. Sign in again.")
                return
            if error is not None:
                self.notice = {"kind": "error", "text": _error_text(error), "action": None}
            self._schedule_poll(0.3)
            self.owner.bump()

        self._run(lambda: api.request("POST", "/agent/sessions/%s/interrupt" % urllib.parse.quote(session_id)),
                  done)

    # ----------------------------------------------------------- view

    def view(self):
        """What the window draws."""
        every = self.messages()
        # The newest reply failed for want of credits: say it once, in the
        # notice with Add credits, not again as a reply in the thread.
        newest = every[-1] if every else None
        out_of_credits = bool(newest and newest.get("role") == "agent" and newest.get("status") == "failed"
                              and newest.get("error_code") == "insufficient_credits")
        shown = every[:-1] if out_of_credits else every
        settled = [r for r in shown if not (r.get("role") == "agent" and r.get("status") == "pending")]
        rows = []
        for row in settled[-ROWS_SHOWN:]:
            agent = row.get("role") == "agent"
            cost = row.get("cost") if isinstance(row.get("cost"), dict) else {}
            question = row.get("question") if isinstance(row.get("question"), dict) else None
            rows.append({
                "id": row.get("id"),
                "role": "agent" if agent else "user",
                "status": row.get("status"),
                "background": row.get("origin") == "background",
                "text": row.get("content") or "",
                "blocks": format_reply(row.get("content")) if agent and row.get("status") == "complete" else None,
                "failure": failure_text(row) if agent and row.get("status") in ("failed", "interrupted") else None,
                "question": question if (agent and row.get("status") == "complete" and question
                                         and isinstance(question.get("options"), list)) else None,
                "credits": cost.get("credits") if isinstance(cost.get("credits"), (int, float)) else None,
            })
        # Only the newest reply's question takes answers, and only while
        # nothing newer is waiting.
        for row in rows[:-1]:
            row["question"] = None
        pending = self.pending()
        if rows and pending:
            rows[-1]["question"] = None
        working = None
        if self.sending:
            working = "Sending..."
        elif pending:
            if pending[-1].get("queued"):
                working = "Waiting for a free turn. Your other NOLGIA Agent chats are still running."
            elif self._agent_sleeping():
                working = "Waking up NOLGIA Agent. This takes about a minute."
            else:
                working = "NOLGIA Agent is working. What it does in %s shows in Activity below." % APP_NAME
        notice = self.notice
        if notice is None and out_of_credits:
            notice = {"kind": "credits", "text": failure_text(newest), "action": {"label": "Add credits", "url": BILLING_URL}}
        return {
            "loaded": self.loaded,
            "empty": not every,
            "hidden": max(0, len(settled) - len(rows)),
            "rows": rows,
            "working": working,
            "busy": self.busy,
            "can_stop": bool(self.session_id and pending and not self.stopping),
            "notice": notice,
            "session_url": (AGENT_URL + "/" + self.session_id) if self.session_id and every else None,
            "cost_line": cost_line(self.rate),
        }


def _submit_notice(error):
    if isinstance(error, ApiError):
        detail = str(error.detail or "")
        if error.status == 404 and "no agent" in detail.lower():
            return {"kind": "no_agent", "action": {"label": "Open NOLGIA Agent", "url": AGENT_URL},
                    "text": "Your NOLGIA Agent is not set up yet. Open NOLGIA Agent on nolgia.ai once to start it, "
                            "then come back here."}
        if error.status == 402 or error.code == "insufficient_credits":
            return {"kind": "credits", "action": {"label": "Add credits", "url": BILLING_URL},
                    "text": "You are out of credits for NOLGIA Agent. Add credits on nolgia.ai to keep going."}
        if error.status == 429:
            return {"kind": "error", "action": None,
                    "text": "NOLGIA Agent has too many turns waiting. Try again when one finishes."}
        if error.status == 409:
            return {"kind": "error", "action": None,
                    "text": "NOLGIA Agent is still working on this chat. Wait for it, or press Stop."}
        if error.status == 403:
            return {"kind": "error", "action": {"label": "Open NOLGIA Agent", "url": AGENT_URL},
                    "text": detail or "Your account cannot use NOLGIA Agent here."}
    return {"kind": "error", "text": _error_text(error), "action": None}


def _error_text(error):
    if isinstance(error, NetworkError):
        return "NOLGIA could not be reached (%s). Check your connection and try again." % error
    if isinstance(error, ApiError):
        return "NOLGIA answered: %s." % (error.detail or error.title or "error %d" % error.status)
    return "Something went wrong: %s." % error


def _spawn(fn):
    threading.Thread(target=fn, name="nolgia-chat", daemon=True).start()


# --------------------------------------------------------------- drawing

def thread_html(view):
    """The thread as the HTML the window's read-only TextEdit shows. Each
    message starts with an anchor named after it (ScrollToAnchor)."""
    out = []
    if view["hidden"]:
        out.append('<p style="color:#8a8a8a">Earlier messages are on nolgia.ai.</p>')
    for row in view["rows"]:
        out.append('<a name="%s"></a>' % anchor(row["id"]))
        if row["role"] == "user":
            who = "BACKGROUND WORK FINISHED" if row["background"] else "YOU"
            text = html.escape(row["text"]).replace("\n", "<br>")
            out.append('<p style="margin-top:10px"><span style="color:#8a8a8a; font-size:small"><b>%s</b></span>'
                       "<br>%s</p>" % (who, text))
            continue
        out.append('<p style="margin-top:10px"><span style="color:#ff6a1a; font-size:small"><b>NOLGIA AGENT</b>'
                   "</span></p>")
        for block in row["blocks"] or []:
            body = "".join(_part_html(p) for p in block["parts"])
            out.append("<p>%s%s</p>" % ("&#8226; " if block["type"] == "li" else "", body))
        if row["failure"]:
            out.append('<p style="color:#ff8a80">%s</p>' % html.escape(row["failure"]))
        if row["question"]:
            labels = [str(o.get("label") or "") for o in row["question"]["options"] if isinstance(o, dict)]
            if labels:
                out.append('<p style="color:#8a8a8a">Answer with a button below, or type your own.</p>')
        if row["credits"] is not None and row["status"] == "complete":
            out.append('<p style="color:#8a8a8a; font-size:small">%s credits</p>' % _number(row["credits"]))
    # The end of the thread, for ScrollToAnchor (an anchor needs a block
    # after it to land on).
    out.append('<p><a name="end"></a>&nbsp;</p>')
    return "".join(out)


def anchor(message_id):
    return "m" + re.sub(r"[^0-9A-Za-z]", "", str(message_id or ""))


def _part_html(part):
    text = html.escape(part["text"])
    if part.get("href"):
        return '<a href="%s">%s</a>' % (html.escape(part["href"], quote=True), text)
    if part.get("bold"):
        return "<b>%s</b>" % text
    return text


def _number(value):
    return str(int(value)) if float(value).is_integer() else str(value)
