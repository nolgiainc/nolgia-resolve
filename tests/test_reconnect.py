# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 NOLGIA Inc
"""Reconnecting (the shared core's worker, the same tests as nolgia-blender's
test_worker.ReconnectTest): after a network failure the plugin is back within
NETWORK_RETRY_MAX seconds of the network, and a due heartbeat goes out when a
poll comes back."""

import socket
import threading
import time
import unittest

import support

from nolgia_resolve.core import util
from nolgia_resolve.core import worker as worker_module
from nolgia_resolve.core.api import ApiClient
from nolgia_resolve.core.commands import ActivityLog
from nolgia_resolve.core.mainthread import MainThreadExecutor
from nolgia_resolve.core.worker import BridgeWorker, Status


def until(predicate, timeout=10.0, step=0.02):
    end = time.time() + timeout
    while time.time() < end:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    raise AssertionError("condition not met within %.1fs" % timeout)


def enqueue(server, kind, args=None, timeout=120):
    status, data = support.call(server, "POST", "/v1/bridge/commands",
                                {"app": "resolve", "kind": kind, "args": args or {}, "timeout_seconds": timeout})
    assert status == 201, (status, data)
    return data["id"]


def wait_command(server, command_id, wait=10):
    status, data = support.call(server, "GET", "/v1/bridge/commands/%s?wait=%d" % (command_id, wait))
    assert status == 200, (status, data)
    return data


class Harness:
    def __init__(self, server_url, heartbeat=0.2):
        self.activity = ActivityLog()
        self.executor = MainThreadExecutor(run=lambda ticket: (True, {"kind": ticket.command.kind}, None),
                                           on_status=self.activity.update)
        self.log = []
        self.worker = BridgeWorker(
            ApiClient(server_url, support.TOKEN), self.executor, self.activity, "inst-test",
            lambda: {"document": {"name": ""}, "allow_agent": True, "app_version": "21.1.1"},
            log=self.log.append, heartbeat_interval=heartbeat, machine_name="test-box",
        )
        self._stop = threading.Event()
        self._pump = threading.Thread(target=self._loop, daemon=True)

    def _loop(self):
        while not self._stop.is_set():
            self.executor.pump()
            time.sleep(0.005)

    def start(self):
        self._pump.start()
        self.worker.start()
        return self

    def close(self):
        self.worker.stop()
        self.worker.finished.wait(10)
        self._stop.set()


class Relay:
    """A TCP relay in front of the mock that can break every open connection
    (the plugin sees ECONNRESET) or refuse new ones (an outage). One thread
    and a selector, so a break reaches every socket at once."""

    def __init__(self, target_url):
        import selectors
        from urllib.parse import urlparse
        u = urlparse(target_url)
        self.target = (u.hostname, u.port)
        self.refusing = False
        self.pairs = {}  # socket -> its other end
        self.sel = selectors.DefaultSelector()
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(64)
        self.srv.setblocking(False)
        self.base_url = "http://127.0.0.1:%d/v1" % self.srv.getsockname()[1]
        self.wake_r, self.wake_w = socket.socketpair()
        self.wake_r.setblocking(False)
        self.sel.register(self.srv, selectors.EVENT_READ, "accept")
        self.sel.register(self.wake_r, selectors.EVENT_READ, "wake")
        self.orders = []
        self.closed = False
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _reset(self, sock):
        try:
            self.sel.unregister(sock)
        except (KeyError, ValueError):
            pass
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
        except OSError:
            pass
        sock.close()

    def _drop(self, sock):
        other = self.pairs.pop(sock, None)
        self._reset(sock)
        if other is not None:
            self.pairs.pop(other, None)
            self._reset(other)

    def _run(self):
        import selectors
        while not self.closed:
            for key, _ in self.sel.select(0.2):
                if key.data == "wake":
                    try:
                        self.wake_r.recv(64)
                    except OSError:
                        pass
                elif key.data == "accept":
                    try:
                        client, _ = self.srv.accept()
                    except OSError:
                        continue
                    if self.refusing:
                        self._reset(client)
                        continue
                    try:
                        upstream = socket.create_connection(self.target, timeout=5)
                    except OSError:
                        self._reset(client)
                        continue
                    for a, b in ((client, upstream), (upstream, client)):
                        a.setblocking(False)
                        self.pairs[a] = b
                        self.sel.register(a, selectors.EVENT_READ, "pipe")
                else:
                    sock = key.fileobj
                    try:
                        data = sock.recv(65536)
                    except (BlockingIOError, InterruptedError):
                        continue
                    except OSError:
                        data = b""
                    other = self.pairs.get(sock)
                    if not data or other is None:
                        self._drop(sock)
                        continue
                    try:
                        other.setblocking(True)
                        other.sendall(data)
                        other.setblocking(False)
                    except OSError:
                        self._drop(sock)
            while self.orders:
                order = self.orders.pop(0)
                if isinstance(order, threading.Event):
                    order.set()
                elif order == "break":
                    for sock in list(self.pairs):
                        if sock in self.pairs:
                            self._drop(sock)
        for sock in list(self.pairs):
            if sock in self.pairs:
                self._drop(sock)
        self.srv.close()

    def _order(self, order):
        done = threading.Event()
        self.orders.append(order)
        self.orders.append(done)
        self.wake_w.send(b"x")
        # Wait until the relay thread has carried it out.
        end = time.time() + 5
        while done in self.orders and time.time() < end:
            time.sleep(0.01)

    def break_all(self):
        self._order("break")

    def outage(self, on):
        self.refusing = on
        if on:
            self.break_all()

    def close(self):
        self.closed = True
        self.wake_w.send(b"x")
        self.thread.join(5)


class ReconnectTest(unittest.TestCase):
    """The owner's Mac test 4: a broken socket, then 5 minutes silent."""

    def setUp(self):
        self.server = support.start_mock(poll_wait_seconds=2)
        self.relay = Relay(self.server.base_url)
        self.harness = None

    def tearDown(self):
        if self.harness is not None:
            self.harness.worker.kill()
            self.harness.close()
        self.relay.close()
        self.server.stop()

    def harness_on(self, url, **kwargs):
        self.harness = Harness(url, **kwargs)
        for key, value in kwargs.pop("worker", {}).items():
            setattr(self.harness.worker, key, value)
        return self.harness

    def test_a_connection_that_breaks_mid_request_is_tried_again_at_once(self):
        h = self.harness_on(self.relay.base_url, heartbeat=20).start()
        until(lambda: h.worker.state == Status.CONNECTED)
        sid = h.worker.session_id
        self.relay.break_all()
        t0 = time.monotonic()
        # The mock still holds the broken poll until its wait ends (2 s) and could hand it the
        # command; the API's poll generation drops such a poll at once.
        time.sleep(2.5)
        cid = enqueue(self.server, "info")
        self.assertEqual(wait_command(self.server, cid)["status"], "succeeded")
        self.assertLess(time.monotonic() - t0, 4.5)
        self.assertEqual(h.worker.session_id, sid)
        self.assertNotIn(Status.RETRYING, [state for state in getattr(h, "states", [])])

    def test_back_within_the_retry_limit_once_an_outage_ends_however_long(self):
        h = self.harness_on(self.relay.base_url, heartbeat=20)
        h.worker.network_retry_max = 2.0
        h.start()
        until(lambda: h.worker.state == Status.CONNECTED)
        self.relay.outage(True)
        until(lambda: h.worker.state == Status.RETRYING)
        time.sleep(14)  # long enough for the backoff to reach its limit (and well past 2 s without one)
        self.relay.outage(False)
        t0 = time.monotonic()
        until(lambda: h.worker.state == Status.CONNECTED, timeout=10)
        back = time.monotonic() - t0
        self.assertLessEqual(back, 3.5, "back after %.1f s" % back)
        cid = enqueue(self.server, "info")
        self.assertEqual(wait_command(self.server, cid)["status"], "succeeded")
        self.assertTrue(any(("Trying again in 1 s." in line or "Trying again in 2 s." in line) for line in h.log), h.log)

    def test_network_waits_stop_at_the_limit_but_slow_down_answers_do_not(self):
        w = BridgeWorker(ApiClient(self.server.base_url, support.TOKEN), None, ActivityLog(), "x", lambda: {},
                         machine_name="m")
        backoff = util.Backoff(1.0, 60.0)
        streak = {"first": True}
        from nolgia_resolve.core.api import ApiError, NetworkError
        self.assertIsNone(w._retry_delay(NetworkError("broken"), backoff, streak), "the first one at once")
        delays = [w._retry_delay(NetworkError("down"), backoff, streak) for _ in range(12)]
        self.assertTrue(all(d <= worker_module.NETWORK_RETRY_MAX for d in delays), delays)
        slow = ApiError(503, title="busy")
        later = [w._retry_delay(slow, backoff, streak) for _ in range(3)]
        self.assertTrue(any(d > worker_module.NETWORK_RETRY_MAX for d in later), later)

    def test_a_due_heartbeat_goes_out_when_a_poll_comes_back(self):
        h = self.harness_on(self.server.base_url, heartbeat=1000).start()  # a timer that, as under App Nap, does not come in time
        until(lambda: h.worker.state == Status.CONNECTED)
        first = h.worker.last_heartbeat_at
        h.worker.heartbeat_interval = 2.0  # due after 2 s; only a poll's answer can wake it now
        until(lambda: h.worker.last_heartbeat_at > first, timeout=8)


if __name__ == "__main__":
    unittest.main()
