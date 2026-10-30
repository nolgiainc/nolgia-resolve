# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 NOLGIA Inc
"""The keeper: keeps this Resolve's NOLGIA session live while DaVinci Resolve
runs no script.

While Resolve plays (or is busy otherwise) it answers no script: every call
into it waits until playback stops, and the NOLGIA window waits inside
Resolve's scripting library holding Python's GIL, so the plugin's own network
threads stop as well. Measured in Resolve Studio 21.1.1 on Windows on
2026-10-30: a plain Python thread beside a UIManager window logged nothing for
the 20.6 s a timeline played. In the owner's Mac test 5 the session then fell
off NOLGIA's list (it heartbeats every 20 s; NOLGIA counts a session live for
60 s after it was last seen) and commands sent to it were refused as not
connected.

So a small program, the keeper, runs beside the plugin. It is the same Python
started the same way: `sys.executable` is Resolve's script runner (fuscript),
which runs a .py file it is given. The plugin writes KEEPER_FILE, which only
imports this module from where the plugin itself was loaded, starts it with
a pipe to its input, and sends it, one JSON line each:

- {"t": "config", ...}: NOLGIA's address, the sign in and the heartbeat's
  body (the one the plugin sends, so the same session);
- {"t": "beat"}: every BEAT_SECONDS from a thread of the plugin's own, which
  stops whenever the plugin is held up;
- {"t": "body", "body": {...}} when the heartbeat's body changes;
- {"t": "stop"} when the plugin switches off.

When beats stop for TAKEOVER_SECONDS the keeper sends the heartbeat itself,
every HEARTBEAT_SECONDS, until they come back. So the session stays live and
a command sent meanwhile waits for the plugin instead of being refused; it
runs once Resolve answers again, unless its time ran out by then. The keeper
never asks for commands (Resolve could not run them) and never closes the
session (the plugin does). It ends on stop, when its input closes (the
plugin ended) or when a heartbeat is refused for the sign in.
"""

import json
import os
import subprocess
import sys
import threading
import time

KEEPER_FILE = "keeper.py"
BEAT_SECONDS = 1.0
TAKEOVER_SECONDS = 5.0
HEARTBEAT_SECONDS = 20.0
RETRY_SECONDS = 10.0
STOP_WAIT_SECONDS = 2.0

BOOTSTRAP = """# NOLGIA for DaVinci Resolve: the keeper, which keeps this Resolve's NOLGIA
# session live while Resolve plays. The plugin writes this file each time it
# starts it; see keeper.py in nolgia_resolve.zip.
import sys
sys.path.insert(0, %r)
from nolgia_resolve import keeper
keeper.main()
"""


def package_root():
    """Where nolgia_resolve was loaded from: nolgia_resolve.zip, or the
    folder that holds the package."""
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.dirname(here)


# ---------------------------------------------------------------- the keeper


def keep(lines, log, client_for, clock=time.monotonic, takeover=TAKEOVER_SECONDS):
    """Run the keeper until stop or the end of `lines` (the plugin's
    messages, as bytes or text lines). `client_for(config)` makes the API
    client a heartbeat goes through."""
    from .core.api import ApiError, NetworkError, Unauthorized

    state = {"config": None, "client": None, "beat": clock(), "stop": False}
    lock = threading.Lock()
    wake = threading.Event()

    def read():
        try:
            for line in lines:
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                kind = message.get("t")
                with lock:
                    if kind == "config":
                        state["config"], state["client"] = message, None
                        state["beat"] = clock()
                    elif kind == "beat":
                        state["beat"] = clock()
                    elif kind == "body" and state["config"] is not None and isinstance(message.get("body"), dict):
                        state["config"] = dict(state["config"], body=message["body"])
                    elif kind == "stop":
                        state["stop"] = True
                wake.set()
                if kind == "stop":
                    return
        except Exception:
            pass
        with lock:
            state["stop"] = True
        wake.set()

    threading.Thread(target=read, name="nolgia-keeper-read", daemon=True).start()
    covering_since = None
    next_heartbeat = 0.0
    failing = False
    while True:
        wake.wait(0.5)
        wake.clear()
        with lock:
            if state["stop"]:
                break
            config, last = state["config"], state["beat"]
        now = clock()
        if config is None:
            continue
        quiet = now - last
        if quiet < config.get("takeover", takeover):
            if covering_since is not None:
                log("The plugin answers again; the keeper kept its session live for %.0f s." % (now - covering_since))
                covering_since = None
            continue
        if covering_since is None:
            covering_since = now
            next_heartbeat = now
            failing = False
            log("The plugin has not answered for %.0f s (DaVinci Resolve runs no script while it plays); the keeper "
                "keeps its session live meanwhile." % quiet)
        if now < next_heartbeat:
            continue
        with lock:
            if state["client"] is None:
                state["client"] = client_for(config)
            client = state["client"]
        try:
            client.register_session(config["body"])
            next_heartbeat = now + config.get("heartbeat", HEARTBEAT_SECONDS)
            if failing:
                log("The keeper reached NOLGIA again.")
            failing = False
        except Unauthorized as err:
            log("NOLGIA did not take the keeper's heartbeat (%s); the keeper stops." % err)
            break
        except (ApiError, NetworkError) as err:
            next_heartbeat = now + RETRY_SECONDS
            if not failing:
                log("The keeper could not reach NOLGIA (%s); trying again every %d s." % (err, RETRY_SECONDS))
            failing = True


def main():
    """The keeper's program (KEEPER_FILE runs it): read the plugin's lines
    on standard input."""
    from . import paths
    from .core.api import ApiClient
    from .settings import Log

    log = Log(os.environ.get("NOLGIA_CONFIG_DIR") or paths.config_dir(), echo=False)

    def client_for(config):
        return ApiClient(config["base_url"], config["token"], user_agent=config.get("user_agent"))

    stream = getattr(sys.stdin, "buffer", sys.stdin)
    try:
        keep(stream, lambda text: log("Keeper: " + text), client_for)
    except Exception as err:
        log("Keeper: stopped by a bug: %r" % err)


# ------------------------------------------------------------- the plugin's end


class Keeper:
    """The plugin's end: starts the keeper and keeps telling it the plugin is
    alive (see the module's notes)."""

    def __init__(self, config_dir, log, executable=None, popen=subprocess.Popen):
        self.config_dir = config_dir
        self.log = log
        self.executable = executable or sys.executable
        self._popen = popen
        self._process = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = None

    @property
    def running(self):
        return self._process is not None and self._process.poll() is None

    def start(self, base_url, token, user_agent, body_of):
        """Start the keeper for one connection. `body_of()` gives the
        heartbeat's body as it is now (the worker's payload). Returns True
        when it started; never raises."""
        self.stop()
        try:
            path = os.path.join(self.config_dir, KEEPER_FILE)
            os.makedirs(self.config_dir, exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(BOOTSTRAP % package_root())
            kwargs = {"stdin": subprocess.PIPE, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                      "cwd": self.config_dir}
            kwargs["env"] = dict(os.environ, NOLGIA_CONFIG_DIR=self.config_dir)
            if sys.platform.startswith("win"):
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            process = self._popen([self.executable, path], **kwargs)
        except Exception as err:
            self.log("Could not start the keeper (%s); while DaVinci Resolve plays, NOLGIA may show this Resolve "
                     "as not connected." % err)
            return False
        self._stop = threading.Event()
        self._process = process
        body = body_of()
        config = {"t": "config", "base_url": base_url, "token": token, "user_agent": user_agent, "body": body,
                  "takeover": TAKEOVER_SECONDS, "heartbeat": HEARTBEAT_SECONDS}
        if not self._send(config):
            return False
        self._thread = threading.Thread(target=self._beat, args=(process, self._stop, body_of, body),
                                        name="nolgia-keeper-beat", daemon=True)
        self._thread.start()
        self.log("Started the keeper (process %s), which keeps this session live while DaVinci Resolve plays."
                 % getattr(process, "pid", "?"))
        return True

    def stop(self):
        """Stop the keeper, before the session closes. Never raises."""
        process, self._process = self._process, None
        self._stop.set()
        if process is None:
            return
        try:
            self._write(process, {"t": "stop"})
            process.stdin.close()
        except Exception:
            pass
        try:
            process.wait(STOP_WAIT_SECONDS)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

    def _beat(self, process, stop, body_of, sent):
        while not stop.wait(BEAT_SECONDS):
            try:
                body = body_of()
            except Exception:
                body = sent
            message = {"t": "beat"}
            if body != sent:
                if not self._write(process, {"t": "body", "body": body}):
                    break
                sent = body
            if not self._write(process, message):
                break

    def _send(self, message):
        process = self._process
        return process is not None and self._write(process, message)

    def _write(self, process, message):
        try:
            with self._lock:
                process.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
                process.stdin.flush()
            return True
        except Exception as err:
            if process is self._process:
                self._process = None
                self.log("The keeper stopped (%s); while DaVinci Resolve plays, NOLGIA may show this Resolve as not "
                         "connected." % err)
            return False
