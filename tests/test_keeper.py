# SPDX-License-Identifier: GPL-3.0-or-later
"""The keeper (keeper.py): while the plugin is held up (DaVinci Resolve
playing), it sends the session's heartbeat itself, so the session stays live;
while the plugin answers it sends nothing; it never outlives the plugin."""

import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import support
import fake_resolve as fake

from nolgia_resolve import keeper as keeper_module
from nolgia_resolve.core.api import NetworkError, Unauthorized
from nolgia_resolve.runtime import Controller


class Lines:
    """The plugin's side of the pipe, for keep(): put() a message, close()."""

    def __init__(self):
        self.q = queue.Queue()

    def put(self, message):
        self.q.put(json.dumps(message).encode() + b"\n")

    def close(self):
        self.q.put(None)

    def __iter__(self):
        while True:
            line = self.q.get()
            if line is None:
                return
            yield line


class Client:
    def __init__(self, fail=None):
        self.bodies = []
        self.fail = fail

    def register_session(self, body):
        if self.fail is not None:
            raise self.fail
        self.bodies.append(body)
        return {"id": "s-1"}


class KeepTest(unittest.TestCase):
    def run_keep(self, lines, client):
        log = []
        done = threading.Event()

        def run():
            keeper_module.keep(lines, log.append, lambda config: client)
            done.set()

        threading.Thread(target=run, daemon=True).start()
        return log, done

    def config(self, **kw):
        body = {"instance_id": "i-1", "app": "resolve"}
        return dict({"t": "config", "base_url": "http://x/v1", "token": "t", "body": body,
                     "takeover": 0.6, "heartbeat": 0.4}, **kw)

    def test_no_heartbeat_while_the_plugin_beats(self):
        lines, client = Lines(), Client()
        log, done = self.run_keep(lines, client)
        lines.put(self.config())
        end = time.monotonic() + 2.0
        while time.monotonic() < end:
            lines.put({"t": "beat"})
            time.sleep(0.2)
        self.assertEqual(client.bodies, [])
        lines.put({"t": "stop"})
        self.assertTrue(done.wait(3))

    def test_heartbeats_while_the_plugin_is_held_up_and_stops_when_it_answers(self):
        lines, client = Lines(), Client()
        log, done = self.run_keep(lines, client)
        lines.put(self.config())
        time.sleep(2.0)  # no beats: the plugin is held up
        held = len(client.bodies)
        self.assertGreaterEqual(held, 2, log)
        self.assertEqual(client.bodies[0], {"instance_id": "i-1", "app": "resolve"})
        lines.put({"t": "body", "body": {"instance_id": "i-1", "app": "resolve", "document": {"name": "B"}}})
        time.sleep(0.6)
        self.assertEqual(client.bodies[-1]["document"], {"name": "B"}, "the latest body")
        for _ in range(8):
            lines.put({"t": "beat"})
            time.sleep(0.2)
        settled = len(client.bodies)
        for _ in range(6):
            lines.put({"t": "beat"})
            time.sleep(0.2)
        self.assertEqual(len(client.bodies), settled, "no heartbeat once the plugin answers again")
        self.assertTrue(any(line.startswith("The plugin has not answered for ") for line in log), log)
        self.assertTrue(any(line.startswith("The plugin answers again; the keeper kept its session live for ")
                            for line in log), log)
        lines.close()
        self.assertTrue(done.wait(3), "the keeper ends when the plugin's pipe closes")

    def test_it_ends_when_the_plugin_ends(self):
        lines, client = Lines(), Client()
        log, done = self.run_keep(lines, client)
        lines.put(self.config())
        lines.close()
        self.assertTrue(done.wait(3))
        self.assertEqual(client.bodies, [])

    def test_a_refused_sign_in_ends_it(self):
        lines, client = Lines(), Client(fail=Unauthorized(401, "Unauthorized"))
        log, done = self.run_keep(lines, client)
        lines.put(self.config())
        self.assertTrue(done.wait(5), log)
        self.assertTrue(any("the keeper stops" in line for line in log), log)

    def test_a_network_failure_is_tried_again(self):
        lines, client = Lines(), Client(fail=NetworkError("no route"))
        log, done = self.run_keep(lines, client)
        lines.put(self.config())
        time.sleep(1.5)
        self.assertFalse(done.is_set())
        self.assertEqual(sum("could not reach NOLGIA" in line for line in log), 1, log)
        lines.put({"t": "stop"})
        self.assertTrue(done.wait(3))


class KeeperProcessTest(unittest.TestCase):
    """The real thing: the plugin starts the keeper as a program (here with
    this Python; in Resolve with its script runner), against the mock API."""

    def setUp(self):
        self.server = support.start_mock(poll_wait_seconds=2)
        self.tmp = tempfile.mkdtemp(prefix="nolgia-keeper-")
        self.env = {"NOLGIA_API_URL": self.server.base_url, "NOLGIA_CONFIG_DIR": self.tmp,
                    "NOLGIA_TOKEN": support.TOKEN}
        self.lines = []
        patcher = mock.patch.multiple(keeper_module, TAKEOVER_SECONDS=1.0, HEARTBEAT_SECONDS=0.5)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.controller = Controller(fake.sample(), env=self.env, log=self.lines.append)

    def tearDown(self):
        self.controller.shutdown(wait=5)
        self.server.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def state(self):
        return support.call(self.server, "GET", "/mock/state", token=None)[1]

    def connect(self):
        self.assertTrue(self.controller.connect())
        end = time.time() + 20
        while time.time() < end and not self.state()["sessions"]:
            self.controller.tick()
            time.sleep(0.05)
        self.assertTrue(self.state()["sessions"])

    def test_the_keeper_keeps_the_session_live_while_the_plugin_is_held_up(self):
        self.connect()
        keeper = self.controller.keeper
        self.assertTrue(keeper.running)
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, keeper_module.KEEPER_FILE)))
        before = self.state()["heartbeats"]
        time.sleep(2.0)
        self.assertEqual(self.state()["heartbeats"], before, "nothing from the keeper while the plugin beats")
        keeper._stop.set()  # the plugin's threads stop, as while Resolve plays
        time.sleep(3.0)
        state = self.state()
        self.assertGreaterEqual(state["heartbeats"] - before, 2, state)
        self.assertEqual(state["last_heartbeat"]["instance_id"], self.controller.lease.instance_id)
        self.assertEqual(len(state["sessions"]), 1, "the same session")
        self.assertTrue(state["sessions"][0]["live"])
        with open(os.path.join(self.tmp, "nolgia.log"), encoding="utf-8") as handle:
            log = handle.read()
        self.assertIn("Keeper: The plugin has not answered for ", log)

    def test_switching_off_stops_the_keeper_before_the_session_closes(self):
        self.connect()
        process = self.controller.keeper._process
        self.controller.disconnect()
        self.assertIsNotNone(process.poll(), "the keeper ended")
        end = time.time() + 20
        while time.time() < end and self.controller.worker is not None:
            self.controller.tick()
            time.sleep(0.05)
        heartbeats = self.state()["heartbeats"]
        time.sleep(2.0)
        self.assertEqual(self.state()["heartbeats"], heartbeats, "nothing registers the session again")

    def test_the_keeper_ends_when_the_plugin_does(self):
        # The plugin's process ending closes the keeper's input.
        self.connect()
        process = self.controller.keeper._process
        self.controller.keeper._process = None
        self.controller.keeper._stop.set()
        process.stdin.close()
        process.wait(5)
        self.assertIsNotNone(process.poll())

    def test_no_keeper_when_switched_off_by_env(self):
        controller = Controller(fake.sample(), env=dict(self.env, NOLGIA_KEEPER="0"), log=self.lines.append)
        self.addCleanup(controller.shutdown, 5)
        self.assertIsNone(controller.keeper)


class BootstrapTest(unittest.TestCase):
    def test_the_keeper_file_runs_the_keeper_from_where_the_plugin_was_loaded(self):
        tmp = tempfile.mkdtemp(prefix="nolgia-keeper-file-")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, keeper_module.KEEPER_FILE)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(keeper_module.BOOTSTRAP % keeper_module.package_root())
        env = dict(os.environ, NOLGIA_CONFIG_DIR=tmp)
        out = subprocess.run([sys.executable, path], input=b'{"t": "stop"}\n', env=env, cwd=tmp,
                             capture_output=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_a_keeper_that_cannot_start_is_logged_and_nothing_breaks(self):
        lines = []
        k = keeper_module.Keeper(tempfile.gettempdir(), lines.append, executable=os.path.join("no", "such", "fuscript"))
        self.assertFalse(k.start("http://x/v1", "t", "ua", lambda: {}))
        self.assertTrue(lines[0].startswith("Could not start the keeper ("), lines)
        k.stop()


if __name__ == "__main__":
    unittest.main()
