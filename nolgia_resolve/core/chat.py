# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 NOLGIA Inc
"""The chat in the NOLGIA panel: what the person types goes to their NOLGIA
Agent, which works in this DaVinci Resolve through the plugin, and its replies come
back here.

It uses the routes nolgia.ai's own chat uses (POST /agent/messages, then the
pending reply row read until it settles), so a chat started here is the same
chat on nolgia.ai/agent. One chat per DaVinci Resolve install: its session id is
remembered, "New chat" starts another. The session is pinned to this DaVinci Resolve
(`app` on POST /agent/sessions: the app and this install's instance id), so
every turn tells the Agent the conversation is about this window. A pin
cannot be added later, so a session without this install's pin is never
used here.

One message is one turn: while the Agent works, the box waits (Stop ends the
turn). A message sent mid-turn would either reach the running turn or become
a turn of its own, billed on its own, depending on the Agent's pod, and the
person could not tell which before sending.

Threads: the network work runs on one chat thread (or inline, for tests,
with threaded=False). DaVinci Resolve's main thread only reads `view()` and calls the
methods that queue work; nothing here imports bpy.
"""

import json
import os
import queue
import re
import threading
import time
from datetime import datetime, timezone

from . import AGENT_URL, APP, BILLING_URL, COST_LINE, COST_LINE_RATE
from .api import ApiError, NetworkError, Unauthorized

MAX_CHARS = 8000  # SubmitAgentMessageRequest.content
SESSION_NAME_MAX = 80
ROWS_SHOWN = 6  # the last three turns; the sidebar stays short, the rest is on nolgia.ai
WAKE_EVERY = 10 * 60  # seconds between wake calls, like the site's chat
APP_NAME = "DaVinci Resolve"
SIGN_IN_EXPIRED = "Your NOLGIA sign in expired. Sign in again."


def poll_delay(pending_for):
    """Seconds before the next read of a pending reply: quick at first,
    slower once a turn has run for a while."""
    if pending_for < 30:
        return 1.5
    if pending_for < 120:
        return 3.0
    return 5.0


def session_name(text):
    """The session's name in the chat list on nolgia.ai: "DaVinci Resolve: " and the
    message's first six words."""
    words = " ".join(str(text or "").split()[:6])
    name = "%s: %s" % (APP_NAME, words or "New chat")
    if len(name) > SESSION_NAME_MAX:
        name = name[: SESSION_NAME_MAX - 3].rstrip() + "..."
    return name


_TIME_RE = re.compile(r"^(.*?T\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)$")


def time_key(stamp):
    """An RFC 3339 time as a string that sorts in time order, to the
    microsecond the API serves ("...01Z" sorts after "...01.5Z" as plain
    text)."""
    m = _TIME_RE.match(str(stamp or ""))
    if not m:
        return "0" * 21
    zone = "+00:00" if m.group(3) == "Z" else m.group(3)
    try:
        seconds = int(datetime.fromisoformat(m.group(1) + zone).timestamp())
    except ValueError:
        return "0" * 21
    return str(seconds).zfill(15) + ((m.group(2) or "") + "000000")[:6]


def _sort_key(row):
    return (time_key(row.get("created_at")), str(row.get("id") or ""))


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
        elif not agent.get("model") and model.get("default"):
            chosen = model
        if chosen is not None:
            break
    credits = chosen.get("turn_credits") if chosen else None
    if isinstance(credits, int) and not isinstance(credits, bool) and credits > 0:
        return credits
    return None


def cost_line(rate):
    """The line under the box."""
    return COST_LINE_RATE.replace("{credits}", str(rate)) if rate else COST_LINE


def failure_text(row):
    """A failed or stopped reply, in words for the panel."""
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


# ----------------------------------------------------------- reply formatting

_LINK_RE = re.compile(
    r"\[([^\]\n]+)\]\((https://[^\s)]+)\)|(https://[^\s<>()]+[^\s<>().,;:!?'\"])"
)
_BOLD_RE = re.compile(r"(\*\*[^*]+\*\*)")
_EMPHASIS_RE = re.compile(r"(^|\s)\*(\S[^*]*\S|\S)\*(?=\s|$)")


def inline_parts(line):
    """One line of a reply as text pieces: plain, bold, or a link. Markdown
    emphasis and code marks are dropped; only https links become links."""
    parts = []

    def push_text(text):
        for piece in _BOLD_RE.split(text or ""):
            if not piece:
                continue
            bold = piece.startswith("**") and piece.endswith("**") and len(piece) > 4
            clean = (piece[2:-2] if bold else piece).replace("`", "")
            clean = _EMPHASIS_RE.sub(r"\1\2", clean)
            if clean:
                parts.append({"text": clean, "bold": True} if bold else {"text": clean})

    last = 0
    for m in _LINK_RE.finditer(line):
        push_text(line[last : m.start()])
        if m.group(2):
            parts.append({"text": m.group(1), "href": m.group(2)})
        else:
            parts.append({"text": m.group(3), "href": m.group(3)})
        last = m.end()
    push_text(line[last:])
    return parts


def format_reply(text):
    """A reply as blocks the panel draws: paragraphs and list items."""
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


def _rfc3339(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class ChatStore:
    """Where the chat's session id is remembered: a small JSON file in the
    add-on's own folder (safe from any thread, unlike DaVinci Resolve's settings)."""

    def __init__(self, path):
        self.path = path

    def recall(self):
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def remember(self, value):
        try:
            if value is None:
                if os.path.exists(self.path):
                    os.remove(self.path)
                return
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(value, handle)
            os.replace(tmp, self.path)
        except OSError:
            pass  # a convenience only


class Link:
    """What the chat needs from the plugin, captured on DaVinci Resolve's main thread
    (the chat thread never reads DaVinci Resolve's settings)."""

    def __init__(self, api, instance_id, api_url, email="", store=None):
        self.api = api
        self.instance_id = instance_id
        self.api_url = api_url
        self.email = email or ""
        self.store = store


# ------------------------------------------------------------------- the chat


class AgentChat:
    """`owner` gives bump() (redraw), log(text) and auth_failed(text), all
    safe from any thread. `begin(link)` gives the API client, this install's
    instance id and where to remember the chat."""

    def __init__(self, owner, clock=time.time, threaded=True):
        self.owner = owner
        self.clock = clock
        self.threaded = threaded
        self._lock = threading.RLock()
        self._jobs = queue.Queue()
        self._thread = None
        self._generation = 0
        self.link = None
        self._clear()

    def _clear(self):
        self.session_id = None
        self.rows = {}  # id -> AgentMessage
        self.sending = False
        self.stopping = False
        self.notice = None  # {kind, text, action: {label, url} or None}
        self.agent = Ellipsis  # GET /agent: Ellipsis unread, None none
        self.rate = None
        self.loaded = False
        self.restore = None  # text a failed send hands back to the box
        self._generation += 1
        self._pending_since = {}
        self._last_wake = 0.0
        self._soon = None

    # ------------------------------------------------------------- lifecycle

    def begin(self, link):
        """Signed in and connected: use this API client and instance id. A
        different account, API or install starts over."""
        with self._lock:
            old = self.link
            same = (
                old is not None
                and old.api_url == link.api_url
                and old.instance_id == link.instance_id
                and (old.email == link.email or not old.email or not link.email)
                and getattr(old.api, "token", None) == getattr(link.api, "token", None)
            )
            self.link = link
            if same and self.loaded and self.notice is None:
                return
            if old is not None and not same:
                self._clear()
        self._post(("start",))

    def reset(self, forget=True):
        """Signed out, or the sign in changed: forget this chat (on this
        computer too, unless forget is False)."""
        with self._lock:
            link = self.link
            self._clear()
            self.link = None if forget else link
        if forget and link is not None and link.store is not None:
            link.store.remember(None)
        self.owner.bump()

    def new_chat(self):
        """"New chat": the next message starts a fresh session."""
        with self._lock:
            agent, rate = self.agent, self.rate
            self._clear()
            self.agent, self.rate = agent, rate
            self.loaded = True
            link = self.link
        if link is not None and link.store is not None:
            link.store.remember(None)
        self.owner.bump()

    def close(self):
        """DaVinci Resolve is closing or the add-on is switched off: stop the thread."""
        with self._lock:
            self._generation += 1
        self._jobs.put(None)

    # ------------------------------------------------------- the chat thread

    def _post(self, job):
        if not self.threaded:
            self._do(job)
            return
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._loop, name="nolgia-chat", daemon=True)
            self._thread.start()
        self._jobs.put(job)

    def _loop(self):
        while True:
            try:
                job = self._jobs.get(timeout=self._next_delay())
            except queue.Empty:
                job = ("tick",)
            if job is None:
                return
            try:
                self._do(job)
            except Exception as err:  # never let the thread die
                self.owner.log("Chat: %s" % err)

    def _next_delay(self):
        with self._lock:
            if self._soon is not None:
                return self._soon
            if not self.session_id or not self._pending_rows():
                return None
            oldest = min(list(self._pending_since.values()) + [self.clock()])
            return poll_delay(self.clock() - oldest)

    def _do(self, job):
        kind = job[0]
        if kind == "start":
            self._start()
        elif kind == "send":
            self._send(job[1], job[2])
        elif kind == "tick":
            self._tick()
        elif kind == "stop":
            self._stop()
        elif kind == "wake":
            self._wake()

    # ----------------------------------------------------------------- reads

    def _start(self):
        with self._lock:
            gen = self._generation
            link = self.link
        if link is None:
            return
        try:
            session_id = None
            saved = self._recall(link)
            if saved:
                session_id = self._pinned_here(link, saved["id"])
            with self._lock:
                if gen != self._generation:
                    return
                self.session_id = session_id
            self._read_agent(link, gen)
            if session_id:
                self._load_transcript(link, gen)
            with self._lock:
                if gen != self._generation:
                    return
                self.loaded = True
        except Exception as err:
            self._read_failed(err, gen)
            return
        self.owner.bump()

    def _recall(self, link):
        saved = link.store.recall() if link.store is not None else None
        if not saved or not saved.get("id"):
            return None
        if saved.get("api") and saved["api"] != link.api_url:
            return None
        if saved.get("email") and link.email and saved["email"] != link.email:
            return None
        return saved

    def _remember(self, link, session_id):
        if link.store is None:
            return
        link.store.remember(
            {"id": session_id, "api": link.api_url, "email": link.email} if session_id else None
        )

    def _pinned_here(self, link, session_id):
        """The remembered session's id when it is still there and pinned to
        this install, else None (and forgotten)."""
        try:
            session = link.api.get_agent_session(session_id)
        except ApiError as err:
            if not isinstance(err, Unauthorized) and err.status in (400, 404):
                self._remember(link, None)
                return None
            raise
        pin = (session or {}).get("app") or {}
        if pin.get("app") != APP or pin.get("instance_id") != link.instance_id or session.get("archived_at"):
            self._remember(link, None)
            return None
        return session.get("id")

    def _read_agent(self, link, gen):
        try:
            agent = link.api.get_agent()
        except Unauthorized:
            raise
        except Exception:
            return  # the chat still works; this only names a sleeping agent
        with self._lock:
            if gen != self._generation:
                return
            self.agent = agent
        if not agent:
            return
        try:
            rate = turn_rate(agent, link.api.list_agent_models())
        except Exception:
            return  # the cost line falls back to the site's words
        with self._lock:
            if gen == self._generation:
                self.rate = rate

    def _load_transcript(self, link, gen):
        since = None
        for _ in range(20):
            with self._lock:
                session_id = self.session_id
            try:
                rows = link.api.list_agent_messages(session_id, since=since, limit=200)
            except ApiError as err:
                if not isinstance(err, Unauthorized) and err.status in (400, 404):
                    # The remembered chat is gone (deleted on nolgia.ai): start a new one.
                    with self._lock:
                        if gen == self._generation:
                            self.session_id = None
                    self._remember(link, None)
                    return
                raise
            with self._lock:
                if gen != self._generation:
                    return
                self._merge(rows)
            if len(rows) < 200:
                return
            since = rows[-1].get("created_at")

    def _merge(self, rows, local=False):
        """Rows from NOLGIA, or (`local`) the ones a send just made, shown
        until NOLGIA's own copies replace them. Only NOLGIA's times are read
        back as `since`, so this computer's clock never hides a row. Call
        with the lock held."""
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

    def _read_failed(self, err, gen):
        with self._lock:
            if gen != self._generation:
                return
            self.loaded = True
            if not isinstance(err, Unauthorized):
                self.notice = {"kind": "error", "text": self._error_text(err), "action": None}
        if isinstance(err, Unauthorized):
            self.owner.auth_failed(SIGN_IN_EXPIRED)
            return
        self.owner.log("Chat: %s" % err)
        self.owner.bump()

    # ---------------------------------------------------------------- polling

    def _since(self):
        """The newest time NOLGIA served, for `since`. Call with the lock held."""
        best = None
        for row in self.rows.values():
            if row.get("local"):
                continue
            if best is None or time_key(row.get("created_at")) > time_key(best):
                best = row.get("created_at")
        return best

    def messages(self):
        """The rows, oldest first."""
        with self._lock:
            return sorted(self.rows.values(), key=_sort_key)

    def _pending_rows(self):
        return [r for r in sorted(self.rows.values(), key=_sort_key)
                if r.get("role") == "agent" and r.get("status") == "pending"]

    def pending(self):
        with self._lock:
            return self._pending_rows()

    @property
    def busy(self):
        with self._lock:
            return self.sending or bool(self._pending_rows())

    def tick(self):
        """Read the pending replies now (the chat thread does this by itself)."""
        self._post(("tick",))

    def _tick(self):
        with self._lock:
            self._soon = None
            gen = self._generation
            link = self.link
            session_id = self.session_id
            since = self._since()
            pending = [r["id"] for r in self._pending_rows()]
        if link is None or not session_id:
            return
        # New rows, then each pending reply on its own, so one failed read
        # never keeps a finished reply off the panel.
        reads = [lambda: link.api.list_agent_messages(session_id, since=since, limit=200)]
        for message_id in pending:
            reads.append(lambda message_id=message_id: [link.api.get_agent_message(message_id)])
        failure = None
        for read in reads:
            try:
                rows = read()
            except Unauthorized:
                with self._lock:
                    if gen != self._generation:
                        return
                self.owner.auth_failed(SIGN_IN_EXPIRED)
                return
            except Exception as err:
                failure = failure or err
                continue
            with self._lock:
                if gen != self._generation:
                    return
                self._merge(rows)
        if self._agent_sleeping() and self.pending():
            self._read_agent(link, gen)
        with self._lock:
            if gen != self._generation:
                return
            if failure is not None:
                self.notice = {"kind": "network", "text": self._error_text(failure), "action": None}
            elif self.notice and self.notice.get("kind") == "network":
                self.notice = None
        self.owner.bump()

    def _agent_sleeping(self):
        with self._lock:
            agent = self.agent
        return bool(agent) and agent is not Ellipsis and agent.get("sleep_state") in ("asleep", "waking")

    def wake(self):
        """The person is about to type: wake a sleeping agent early, so it is
        ready by the time they send (the site's chat does the same)."""
        if self._agent_sleeping() and self.clock() - self._last_wake >= WAKE_EVERY:
            self._last_wake = self.clock()
            self._post(("wake",))

    def _wake(self):
        with self._lock:
            link = self.link
            gen = self._generation
        if link is None:
            return
        try:
            agent = link.api.wake_agent()
        except Exception:
            return  # the message wakes it anyway
        with self._lock:
            if gen == self._generation and agent:
                self.agent = agent
        self.owner.bump()

    # -------------------------------------------------------------------- send

    def send(self, text):
        """Send a message. Returns True when it went off to NOLGIA (the panel
        clears the box), False when it did not start (busy, empty, too
        long). If NOLGIA then refuses it, `restore` hands the text back."""
        content = str(text or "").strip()
        with self._lock:
            if not content or self.sending or self._pending_rows() or self.link is None:
                return False
            if len(content) > MAX_CHARS:
                self.notice = {
                    "kind": "error",
                    "text": "That message is too long. Keep it under %d characters." % MAX_CHARS,
                    "action": None,
                }
                self.owner.bump()
                return False
            self.sending = True
            self.notice = None
            gen = self._generation
        self.owner.bump()
        self._post(("send", content, gen))
        return True

    def _send(self, content, gen):
        with self._lock:
            link = self.link
        if link is None or gen != self._generation:
            return
        try:
            # No NOLGIA Agent on the account (read at start; read again, it
            # may have been set up on nolgia.ai since): say so before
            # starting a chat.
            with self._lock:
                unread = self.agent is None or self.agent is Ellipsis
            if unread:
                self._read_agent(link, gen)
            with self._lock:
                if gen != self._generation:
                    return
                no_agent = self.agent is None
            if no_agent:
                with self._lock:
                    self.sending = False
                    self.restore = content
                    self.notice = self._submit_notice(
                        ApiError(404, "Not Found", "no agent is provisioned for this account")
                    )
                self.owner.bump()
                return
            accepted = self._submit(link, content, gen)
            with self._lock:
                if gen != self._generation:
                    return
                if accepted.get("session_id") and accepted["session_id"] != self.session_id:
                    self.session_id = accepted["session_id"]
                    remember = self.session_id
                else:
                    remember = None
                at = _rfc3339(self.clock())
                self._merge([{"id": accepted.get("user_message_id"), "role": "user", "content": content,
                              "status": "complete", "session_id": self.session_id, "created_at": at}], local=True)
                if accepted.get("agent_message_id"):
                    self._merge([{"id": accepted["agent_message_id"], "role": "agent", "content": "",
                                  "status": "pending", "session_id": self.session_id, "created_at": at,
                                  "user_message_id": accepted.get("user_message_id")}], local=True)
                self.loaded = True
                self.sending = False
                self._soon = 0.4
            if remember:
                self._remember(link, remember)
            self.owner.bump()
        except Exception as err:
            with self._lock:
                if gen != self._generation:
                    return
                self.sending = False
            if isinstance(err, Unauthorized):
                self.owner.auth_failed(SIGN_IN_EXPIRED)
                return
            with self._lock:
                # A turn started elsewhere (this chat on nolgia.ai) is still
                # running: follow it, so the box waits for it like for its own.
                body = err.body if isinstance(err, ApiError) and isinstance(err.body, dict) else {}
                pending_id = body.get("pending_agent_message_id") if getattr(err, "status", None) == 409 else None
                if pending_id and self.session_id:
                    self._merge([{"id": pending_id, "role": "agent", "content": "", "status": "pending",
                                  "session_id": self.session_id, "created_at": _rfc3339(self.clock())}], local=True)
                    self._soon = 0.3
                self.notice = self._submit_notice(err)
                self.restore = content
            self.owner.log("Chat: could not send (%s)." % err)
            self.owner.bump()

    def _submit(self, link, content, gen):
        with self._lock:
            session_id = self.session_id
        if session_id:
            try:
                return link.api.submit_agent_message({"content": content, "session_id": session_id})
            except ApiError as err:
                # The remembered chat was deleted on nolgia.ai: carry on in a new one.
                if not (err.status == 404 and "session" in (err.detail or "").lower()):
                    raise
                with self._lock:
                    if gen == self._generation:
                        self.session_id = None
                        self.rows = {}
                self._remember(link, None)
        # A new chat: the session first, pinned to this DaVinci Resolve (the pin
        # rides only on POST /agent/sessions), then the message into it.
        if not link.instance_id:
            raise RuntimeError("this DaVinci Resolve has no NOLGIA instance id yet; turn on Connected and try again")
        session = link.api.create_agent_session(
            {"name": session_name(content), "app": {"app": APP, "instance_id": link.instance_id}}
        )
        pin = (session or {}).get("app") or {}
        if not session or not session.get("id") or pin.get("instance_id") != link.instance_id:
            # Never chat in a session that is not pinned here: the pin cannot be added later.
            raise ApiError(500, "NOLGIA did not pin the new chat to this DaVinci Resolve",
                           "Update NOLGIA for DaVinci Resolve, or try again later.")
        with self._lock:
            if gen == self._generation:
                self.session_id = session["id"]
        self._remember(link, session["id"])
        return link.api.submit_agent_message({"content": content, "session_id": session["id"]})

    def _submit_notice(self, err):
        if isinstance(err, ApiError):
            detail = str(err.detail or "")
            if err.status == 404 and "no agent" in detail.lower():
                return {
                    "kind": "no_agent",
                    "text": "Your NOLGIA Agent is not set up yet. Open NOLGIA Agent on nolgia.ai once "
                            "to start it, then come back here.",
                    "action": {"label": "Open NOLGIA Agent", "url": AGENT_URL},
                }
            if err.status == 402 or err.code == "insufficient_credits":
                return {
                    "kind": "credits",
                    "text": "You are out of credits for NOLGIA Agent. Add credits on nolgia.ai to keep going.",
                    "action": {"label": "Add credits", "url": BILLING_URL},
                }
            if err.status == 429:
                return {"kind": "error", "text": "NOLGIA Agent has too many turns waiting. Try again when one finishes.",
                        "action": None}
            if err.status == 409:
                return {"kind": "error", "text": "NOLGIA Agent is still working on this chat. Wait for it, or press Stop.",
                        "action": None}
            if err.status == 403:
                return {"kind": "error", "text": detail or "Your account cannot use NOLGIA Agent here.",
                        "action": {"label": "Open NOLGIA Agent", "url": AGENT_URL}}
        return {"kind": "error", "text": self._error_text(err), "action": None}

    @staticmethod
    def _error_text(err):
        if isinstance(err, NetworkError):
            return "NOLGIA could not be reached (%s). Check your connection and try again." % err
        if isinstance(err, ApiError):
            return "NOLGIA answered: %s." % (err.detail or err.title or "error %d" % err.status)
        return "Something went wrong: %s." % err

    def take_restore(self):
        """The text of a send NOLGIA refused, once, for the box."""
        with self._lock:
            text, self.restore = self.restore, None
            return text

    # -------------------------------------------------------------------- stop

    def stop(self):
        """Stop the turn that is running. Generations it already started
        keep running on NOLGIA, as on nolgia.ai."""
        with self._lock:
            if not self.session_id or self.stopping:
                return
            self.stopping = True
        self.owner.bump()
        self._post(("stop",))

    def _stop(self):
        with self._lock:
            link = self.link
            gen = self._generation
            session_id = self.session_id
        try:
            if link is not None and session_id:
                link.api.interrupt_agent_session(session_id)
        except Exception as err:
            with self._lock:
                if gen == self._generation:
                    self.notice = {"kind": "error", "text": self._error_text(err), "action": None}
        with self._lock:
            if gen != self._generation:
                return
            self.stopping = False
            self._soon = 0.3
        self.owner.bump()
        if not self.threaded:
            self._tick()

    # -------------------------------------------------------------------- view

    def view(self):
        """What the panel draws."""
        with self._lock:
            every = sorted(self.rows.values(), key=_sort_key)
            # The newest reply failed for want of credits: say it once, in
            # the notice with Add credits, not again as a reply in the thread.
            newest = every[-1] if every else None
            out_of_credits = bool(
                newest and newest.get("role") == "agent" and newest.get("status") == "failed"
                and newest.get("error_code") == "insufficient_credits"
            )
            shown = every[:-1] if out_of_credits else every
            rows = []
            for row in shown[-ROWS_SHOWN:]:
                agent = row.get("role") == "agent"
                question = row.get("question")
                cost = row.get("cost") or {}
                rows.append({
                    "id": row.get("id"),
                    "role": row.get("role"),
                    "status": row.get("status"),
                    "background": row.get("origin") == "background",
                    "queued": bool(row.get("queued")),
                    "text": row.get("content") or "",
                    "blocks": format_reply(row.get("content")) if agent and row.get("status") != "failed" else None,
                    "failure": failure_text(row) if agent and row.get("status") in ("failed", "interrupted") else None,
                    "question": question if (agent and row.get("status") == "complete" and isinstance(question, dict)
                                             and isinstance(question.get("options"), list)) else None,
                    "credits": cost.get("credits") if isinstance(cost.get("credits"), (int, float)) else None,
                })
            # Only the newest reply's question takes answers.
            for row in rows[:-1]:
                row["question"] = None
            working = None
            pending = self._pending_rows()
            if self.sending:
                working = "Sending..."
            elif pending:
                if pending[-1].get("queued"):
                    working = "Waiting for a free turn. Your other NOLGIA Agent chats are still running."
                elif self._agent_sleeping():
                    working = "Waking up NOLGIA Agent. This takes about a minute."
                else:
                    working = "NOLGIA Agent is working. What it does in DaVinci Resolve shows in Activity below."
            notice = self.notice
            if notice is None and out_of_credits:
                notice = {"kind": "credits", "text": failure_text(newest),
                          "action": {"label": "Add credits", "url": BILLING_URL}}
            return {
                "loaded": self.loaded,
                "empty": not every,
                "hidden": max(0, len(shown) - len(rows)),
                "rows": rows,
                "working": working,
                "busy": self.sending or bool(pending),
                "can_stop": bool(self.session_id and pending and not self.stopping),
                "notice": notice,
                "session_url": AGENT_URL + "/" + self.session_id if self.session_id and every else None,
                "cost_line": cost_line(self.rate),
            }
