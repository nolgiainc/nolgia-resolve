# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 NOLGIA Inc
"""Hand commands from the network thread to Blender's main thread and back.

bpy is not thread safe, so the network thread never runs a command itself:
it calls `submit()` and waits on the returned ticket. Blender's main thread
calls `pump()` from a bpy.app.timers callback (or from the headless serve
loop), which runs the command and resolves the ticket.

Some commands need the person's OK first (code when "Ask before running code"
is on, opening a file over unsaved changes). Those wait in `approvals` until
the person clicks Approve or Deny, or the command runs out of time.
"""

import contextlib
import queue
import threading
import time


class Quiet:
    """Keeps the plugin's own background threads out of Python while the
    main thread says so. NOLGIA for Blender needs it; the other apps' plugins
    pass NoQuiet.

    Why (the crash of 2026-11-01, and the one of 2026-10-06): Blender 4.2 to
    4.5 run Python 3.11, and Blender lets go of Python's lock (the GIL) while
    an operator runs. Inside `bpy.context.temp_override(...)`, every context
    lookup the operator makes (the active object, the scene) asks Python, and
    Blender only takes the lock back when no other thread holds it: it tells
    by `PyThreadState_GetDict()`, which on Python 3.11 answers for whichever
    thread holds the lock. So while one of our network threads runs Python,
    an operator inside an override uses Python unlocked, at the same time as
    that thread, and Python's memory breaks (EXCEPTION_ACCESS_VIOLATION in
    python311.dll, often a moment later in another thread).

    The main thread `hold()`s while it has an override open. A background
    thread wraps each piece of its work in `work()`: that waits, parked in a
    plain lock wait that runs no Python, while a hold is on. `hold()` waits a
    moment for work already under way to finish. A thread that must not wait
    for ever (the heartbeat keeps the session live) passes `limit`.
    """

    def __init__(self, clock=time.monotonic):
        self._cond = threading.Condition(threading.Lock())
        self._clock = clock
        self._holds = 0
        self._working = 0
        self._closed = False

    @property
    def held(self):
        return self._holds > 0

    def hold(self, settle=2.0):
        """Main thread: no new background work starts until release(). Waits
        up to `settle` seconds for work under way to finish; returns True
        when nothing is running then."""
        with self._cond:
            self._holds += 1
            end = self._clock() + settle
            while self._working and not self._closed:
                left = end - self._clock()
                if left <= 0:
                    return False
                self._cond.wait(left)
            return True

    def release(self):
        with self._cond:
            self._holds = max(0, self._holds - 1)
            if not self._holds:
                self._cond.notify_all()

    @contextlib.contextmanager
    def held_quiet(self, settle=2.0):
        self.hold(settle)
        try:
            yield
        finally:
            self.release()

    @contextlib.contextmanager
    def work(self, limit=None):
        """Background thread: run the block when no hold is on. With `limit`
        (seconds), go ahead anyway once a hold has lasted that long."""
        with self._cond:
            end = None if limit is None else self._clock() + limit
            while self._holds and not self._closed:
                if end is None:
                    self._cond.wait()
                    continue
                left = end - self._clock()
                if left <= 0:
                    break
                self._cond.wait(left)
            self._working += 1
        try:
            yield
        finally:
            with self._cond:
                self._working -= 1
                self._cond.notify_all()

    def close(self):
        """DaVinci Resolve is closing: nothing waits any more."""
        with self._cond:
            self._closed = True
            self._cond.notify_all()


class NoQuiet:
    """For apps whose scripting has no such lock problem: never waits."""

    held = False

    def hold(self, settle=2.0):
        return True

    def release(self):
        pass

    @contextlib.contextmanager
    def held_quiet(self, settle=2.0):
        yield

    @contextlib.contextmanager
    def work(self, limit=None):
        yield

    def close(self):
        pass


class Ticket:
    def __init__(self, command, prepared=None):
        self.command = command
        self.prepared = prepared
        self.ok = None
        self.result = None
        self.error = None
        self._event = threading.Event()

    def resolve(self, ok, result=None, error=None):
        if self._event.is_set():
            return
        self.ok, self.result, self.error = ok, result, error
        self._event.set()

    @property
    def done(self):
        return self._event.is_set()

    def wait(self, timeout=None):
        return self._event.wait(timeout)


class ApprovalRequest:
    """What to ask the person. `headless_error` is the failure to report
    when there is nobody to ask (Blender running without a window)."""

    def __init__(self, title, lines, headless_error, approve_label="Approve"):
        self.title = title
        self.lines = list(lines)
        self.headless_error = headless_error
        self.approve_label = approve_label


class PendingApproval:
    def __init__(self, ticket, request):
        self.ticket = ticket
        self.request = request
        self.decision = None

    @property
    def id(self):
        return self.ticket.command.id


class MainThreadExecutor:
    def __init__(self, run, needs_approval=None, can_ask=None, on_status=None,
                 on_change=None, clock=time.monotonic):
        self._run = run
        self._needs_approval = needs_approval or (lambda ticket: None)
        self._can_ask = can_ask or (lambda: True)
        self._on_status = on_status or (lambda command_id, status, detail="": None)
        self._on_change = on_change or (lambda: None)
        self._clock = clock
        self._queue = queue.Queue()
        self._closed_reason = None
        self.approvals = []

    # ---------------------------------------------------------- any thread

    def submit(self, command, prepared=None):
        ticket = Ticket(command, prepared)
        self._queue.put(ticket)
        return ticket

    def close(self, reason):
        """Stop starting new commands; queued and waiting ones fail with `reason`."""
        self._closed_reason = reason

    def reopen(self):
        self._closed_reason = None

    # ------------------------------------------------------- main thread only

    def approve(self, command_id):
        return self._decide(command_id, True)

    def deny(self, command_id):
        return self._decide(command_id, False)

    def _decide(self, command_id, decision):
        for pending in self.approvals:
            if pending.id == command_id and pending.decision is None:
                pending.decision = decision
                return True
        return False

    def pump(self, max_commands=1):
        """Run due work. Returns True when something changed."""
        changed = self._settle_approvals()
        ran = 0
        while ran < max_commands:
            try:
                ticket = self._queue.get_nowait()
            except queue.Empty:
                break
            changed = True
            if self._closed_reason:
                self._fail(ticket, self._closed_reason)
                continue
            try:
                request = self._needs_approval(ticket)
            except Exception as err:  # a bug in the check must not kill the timer
                self._fail(ticket, "Could not check this command: %s" % err)
                continue
            if request is not None:
                if not self._can_ask():
                    self._fail(ticket, request.headless_error)
                    continue
                self.approvals.append(PendingApproval(ticket, request))
                self._on_status(ticket.command.id, "approval")
                self._on_change()
                continue
            self._execute(ticket)
            ran += 1
        return changed

    def _settle_approvals(self):
        changed = False
        now = self._clock()
        for pending in list(self.approvals):
            ticket = pending.ticket
            if self._closed_reason:
                self.approvals.remove(pending)
                self._fail(ticket, self._closed_reason)
            elif pending.decision is True:
                self.approvals.remove(pending)
                self._execute(ticket)
            elif pending.decision is False:
                self.approvals.remove(pending)
                self._fail(ticket, "The person at DaVinci Resolve clicked Deny, so this did not run.")
            elif now >= ticket.command.deadline:
                self.approvals.remove(pending)
                self._fail(ticket, "Nobody approved this in DaVinci Resolve in time, so it did not run.")
            else:
                continue
            changed = True
        if changed:
            self._on_change()
        return changed

    def _execute(self, ticket):
        self._on_status(ticket.command.id, "running")
        try:
            ok, result, error = self._run(ticket)
        except Exception as err:  # the runner reports its own errors; this is a backstop
            ok, result, error = False, None, "The plugin failed to run this command: %s" % err
        ticket.resolve(ok, result, error)

    def _fail(self, ticket, error):
        ticket.resolve(False, None, error)
