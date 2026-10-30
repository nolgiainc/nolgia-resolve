# NOLGIA for DaVinci Resolve

NOLGIA's plugin for DaVinci Resolve. It connects DaVinci Resolve on your computer to
NOLGIA, so NOLGIA Agent can look at your project, make changes in it, render
previews and move files between the app and your NOLGIA library. Tell it what
you want in the NOLGIA window's chat, right inside Resolve. You can also use
Claude, Cursor, Codex or another agent with NOLGIA connected (through NOLGIA's
MCP server).

```
your agent --MCP--> mcp.nolgia.ai --> api.nolgia.ai <--(the plugin connects out)-- DaVinci Resolve on your computer
```

The plugin always connects out to NOLGIA; nothing connects in to your
computer. It only takes commands while you are signed in and have it switched
on, and it lists every command it runs.

A script plugin for DaVinci Resolve Studio 21.1 and newer (since 21.1 Resolve
runs Python scripts only in the Studio edition). It needs Python 3.8 or
newer and nothing outside Python's standard library. It is tested on
Windows and macOS (0.2.1 on macOS 26.6.2 with Resolve Studio 21.1.1). It is
written for Linux too, but has not been run there yet (see
[Tested on](#tested-on)).

Which Python runs it: Resolve runs a Scripts menu script in its own
`fuscript` program with a Python it finds on the computer. On Windows,
Resolve 21.1 took the one named in `PYTHON3HOME` (or `PYTHONHOME`) when set,
else the one in the registry (a per-user install before a machine-wide one;
`PATH` plays no part), not its own bundled Python 3.14. What it picks on a
Windows computer with no Python installed has not been checked. On macOS,
Resolve 21.1.1 ran it on its own bundled Python 3.14 (3.14.4, from
`Contents/Resources/ResolvePython`), not on the Homebrew Python 3.14.7 or
Apple's Python 3.9 also on that Mac, so a Mac needs no Python installed.

## Install and use

1. Get `nolgia-resolve-<version>.zip` from [nolgia.ai/plugins/davinci-resolve](https://nolgia.ai/plugins/davinci-resolve)
   (or the [releases](https://github.com/nolgiainc/nolgia-resolve/releases) here)
   and unzip it. Safari unzips it by itself, into a folder named up to the
   first dot (`nolgia-resolve-0`); `sh ~/Downloads/nolgia-resolve-0*/install.sh`
   finds that folder or the one a double-click makes.
2. Close the NOLGIA window if it is open, quit DaVinci Resolve (DaVinci
   Resolve > Quit DaVinci Resolve, or Command-Q on a Mac) and run the
   installer: on Windows double-click `install.cmd`; on macOS or Linux run
   `sh install.sh` in Terminal, in the unzipped folder. (With NOLGIA 0.2.2 or
   older open, a quit leaves Resolve running with no windows; if that
   happened, force quit it with Option-Command-Esc on a Mac or Task Manager
   on Windows. 0.2.3 and newer finish the quit themselves, see Known limits.
   If Resolve is still open, the installer says so and installs anyway.) It
   copies `NOLGIA.py` and `nolgia_resolve.zip` from the `Utility` folder into
   Resolve's Scripts folder for your user, so it needs no administrator
   rights. To do it by hand, copy those two files into:
   - Windows: `%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility`
   - macOS: `~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility`
   - Linux: `~/.local/share/DaVinciResolve/Fusion/Scripts/Utility`
3. Start Resolve, open the Workspace > Scripts menu and choose NOLGIA. The
   NOLGIA window opens.
4. Click Sign in. Your browser opens a NOLGIA page; check that the code
   matches the one in the NOLGIA window and approve it.
5. Keep the NOLGIA window open with Connected on, and tell NOLGIA Agent what
   to do in the window's chat (or ask your own agent). Each command shows up
   in the Activity list. Click Pause, turn Connected off or close the window
   to stop at any time.

NOLGIA works in Resolve only while its window is open: closing it switches
NOLGIA off. When you open it again it reconnects by itself if you left
Connected on. To remove the plugin, delete the two files.

## The NOLGIA Agent chat

The NOLGIA window has a chat with your NOLGIA Agent: type what you want done in
this Resolve and press Enter (or Send). The Agent works in your open project
through the plugin, so every command it sends shows in the Activity list, and
its reply comes back in the chat. Before the first message the window
suggests a few things to ask; a click puts one in the box.

- **One chat per install.** The chat is a NOLGIA Agent chat like the ones on
  nolgia.ai/agent (Open on nolgia.ai opens it there), pinned to this Resolve:
  every turn tells the Agent the conversation is about this window. New chat
  starts another. The window shows the last three turns; the whole chat is on
  nolgia.ai. It remembers the chat in `chat.json` next to `settings.json`
  (with the account and API it belongs to; a chat pinned to another install
  is never taken over); signing out forgets it.
- **One message is one turn.** While NOLGIA Agent works, Send waits (you can
  type your next message meanwhile); Stop ends the turn. A question from the
  Agent comes with its answers as buttons below the chat.
- **What it costs.** Each turn costs credits on your NOLGIA Agent's model (the
  window says how many, from NOLGIA), and anything the Agent generates with
  NOLGIA is priced as usual. Each reply shows what its turn cost.
- **What it needs.** Connected and Allow NOLGIA Agent on (the window offers
  the one click when either is off), and a NOLGIA Agent on your account (open
  nolgia.ai/agent once to start it).

The chat uses the same API routes as nolgia.ai's chat (`POST /agent/sessions`
with `app: {app: "resolve", instance_id}`, `POST /agent/messages`, the reply
read with `GET /agent/messages/{id}` until it settles, and
`POST /agent/sessions/{id}/interrupt`), with the window's sign in. The chat
itself is [`core/chat.py`](nolgia_resolve/core/chat.py), the same file as the
Blender plugin's (it runs on its own thread and never calls Resolve); how the
window shows it is [`nolgia_resolve/chatview.py`](nolgia_resolve/chatview.py)
and [`nolgia_resolve/panel.py`](nolgia_resolve/panel.py).

## Settings

Settings in the NOLGIA window:

- **Connected**: NOLGIA can send commands to this Resolve only while this is on.
- **Allow NOLGIA Agent**: also let your NOLGIA Agent in the cloud work here, not
  only the agent apps you run yourself.
- **Ask before running code**: show every piece of Python NOLGIA wants to run
  in a NOLGIA request window and wait for you to click Run code or Deny.
  Closing that window leaves the request waiting (Show request brings it back).
  Its buttons turn on 1 second after a request shows, and only a click
  answers: Return and Space never do, and a click counts only for the request
  shown when the buttons turned on.
- **Sign out**: disconnect and forget the sign in on this computer.

The sign in and the switches are kept in `settings.json` in a NOLGIA folder
for your user: `%APPDATA%\NOLGIA\resolve` on Windows,
`~/Library/Application Support/NOLGIA/resolve` on macOS and
`~/.config/nolgia/resolve` on Linux. On macOS and Linux the file is readable
only by you (mode 600); on Windows it sits in your own `%APPDATA%`. Next to it
is a log of what the plugin did (`nolgia.log`, never the token). A saved sign
in is only ever sent to the API it came from.

Python that NOLGIA runs in Resolve runs on your computer with your
permissions, like any script. Turn on Ask before running code if you want to
see it first.

## What your agent can do

The full arguments and results are in
[`COMMANDS.md`](COMMANDS.md). Frames count from 0 at the start
of the timeline (Resolve's own API numbers frames from the start timecode,
01:00:00:00 at 24 fps being frame 86400; `info` gives both).

| Command | Arguments | Result |
|---|---|---|
| `info` | none | Resolve's version and edition, the current page, the open project, the current timeline (frame rate, size, start timecode, length, current timecode, tracks with their clip counts, selected clips, the clip under the playhead), the other timelines, the media pool's bins with clip counts, the render presets, `current_timecode`. Resolve's scripting cannot tell whether a project has unsaved changes, so `unsaved_changes` is null (and `changed_by_nolgia_since_save` says what NOLGIA did). |
| `run` | `language: "python"`, `code`, `timeout_seconds?` | `{value, stdout, stderr}`; `value` is whatever the code puts in `result` (the code sees `resolve`, `project_manager`, `project`, `media_pool`, `timeline` (the current one, or None) and `fusion`; Resolve objects come back as their names). A failure returns the traceback. |
| `preview` | `frame?` or `timecode?`, `timeline?` (or `camera`), `width?` (16 to 1920) | a still of a frame of the timeline (default the current frame of the current timeline), never wider than the timeline, uploaded: `{asset_id, width, height, mime_type, timeline, frame, timecode}`. A PNG; when that would be too big for your agent to see inline (about 3.6 MB), Resolve's own JPEG of the frame when it has the size asked for, else a PNG with fewer colour levels. |
| `import_asset` | one of `asset_id`, `asset_ids` (up to 100, in order), `project_id` (a NOLGIA project's ready video, image and audio, oldest first) or `color_preset` (a NOLGIA color preset's slug); `bin?`, `append?`, `apply_to?`, `node?` | media goes into the `NOLGIA imports` bin (or `bin`), in order, and with `append: true` onto the end of the current timeline (a new `NOLGIA timeline` when none is open): `{imported, asset_ids, assets, kind, bin, appended?}`. A `.cube` file or a color preset is installed as a LUT and, with `apply_to` (`current`, `selected`, `current_track`, `all`), set on grade node `node` (default 1): `{imported, kind: "lut", path, lut, applied_to?}`. |
| `export` | `format` (`png`, `mp4`), `frames?` (`"24"` or `"0-119"`), `filename?`, `timeline?` | renders at the timeline's size and uploads it: `png` one frame (default the current one), `mp4` H.264 with the timeline's audio (default the whole timeline): `{asset_id, format, filename, frames, width, height, timeline}`. It renders into a temporary folder, never over your files, and puts the render queue and the Deliver page's settings back. |
| `save` | none | saves the open project: `{project, saved}` |
| `open` | `project`, `folder?` | opens another project: `{project, folder}`. With the NOLGIA window open it always asks you first (Resolve cannot tell whether there are unsaved changes). |
| `dit` | `subcommand` (`offload`, `verify`, `manifest`, `inventory`, `rename`, `undo`, `report`) and its fields, or `action: "status"`/`"cancel"` with `job_id` | camera-card DIT through the NOLGIA CLI, the same contract as the Adobe plugin's (see above). Writes (`offload`, `rename` with `apply`, `undo`) are planned with the CLI's dry run and wait for your Approve in the NOLGIA window; with no window open they do not run. It hands back a job within about 2 s so Resolve's window stays responsive; follow it with `status`. Workflow: `presets/resolve/dit.md`. |

**LUTs.** NOLGIA's film-look color presets come from the API it uses
(`GET /color-presets`, public). `import_asset` installs a LUT into a `NOLGIA`
folder in the LUT folder DaVinci Resolve reads, named after the preset or the
file (`Kodak Portra 400.cube`), and refreshes Resolve's LUT list. A color
preset replaces its own older copy; a LUT from your library never replaces
another file. The LUT folder:

- Windows: `%PROGRAMDATA%\Blackmagic Design\DaVinci Resolve\Support\LUT\NOLGIA`.
  This is Resolve's LUT folder for all users of the computer; Resolve 21.1
  did not read LUTs from a folder under `%APPDATA%`. Resolve's installer lets
  every user write there, so no administrator rights are needed.
- macOS: `/Library/Application Support/Blackmagic Design/DaVinci Resolve/LUT/NOLGIA`
  (Resolve's documented LUT folder), or, when that cannot be written without
  administrator rights, `~/Library/Application Support/Blackmagic Design/DaVinci Resolve/LUT/NOLGIA`.
  Resolve's installer lets every user write the first one, and Resolve 21.1.1
  read the LUTs from it; the folder in your home has not been tried.
- Linux: `/opt/resolve/LUT/NOLGIA`, else `~/.local/share/DaVinciResolve/LUT/NOLGIA`
  (not yet tried).
- Anywhere: `NOLGIA_LUT_DIR`, or Resolve's own `BMD_RESOLVE_LUT_DIR`, when set.

Imported media stays on disk where Resolve can find it, in
`Documents/NOLGIA imports/<project>/<asset id>/` (or `NOLGIA_IMPORT_DIR`).
Resolve does not import 3D models.

## Without the NOLGIA window (render machines, scripts, tests)

With Resolve open (or started without its window, `Resolve -nogui`) and
Preferences > System > General > External scripting set to Local, run
the installed `NOLGIA.py` with Resolve's own Python:

```bat
rem Windows
set NOLGIA_TOKEN=nol_...
"C:\Program Files\Blackmagic Design\DaVinci Resolve\ResolvePython\ResolvePython.exe" "%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility\NOLGIA.py" --serve
```

```sh
# macOS
NOLGIA_TOKEN=nol_... "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Applications/ResolvePython" \
  ~/"Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility/NOLGIA.py" --serve
# Linux: /opt/resolve/bin/ResolvePython ~/.local/share/DaVinciResolve/Fusion/Scripts/Utility/NOLGIA.py --serve
```

It connects, runs commands until it is switched off (`nolgia_resolve.disconnect()`
from `run` code, or Ctrl+C) or Resolve closes, and exits with 0 after a
normal stop and 3 when it could not connect or NOLGIA stopped accepting the
sign in. Without a window nobody can approve code, so it refuses to connect
while Ask before running code is on, and refuses `open` after NOLGIA changed
the project since it was last saved.

| Variable | Effect |
|---|---|
| `NOLGIA_TOKEN` | use this token instead of signing in (never saved) |
| `NOLGIA_API_URL` | API to talk to (default `https://api.nolgia.ai/v1`) |
| `NOLGIA_BRIDGE_AUTOCONNECT=1` | connect as soon as the NOLGIA window opens |
| `NOLGIA_ASK_BEFORE_RUN=0/1` | set Ask before running code (for this run; not saved) |
| `NOLGIA_ALLOW_AGENT=0/1` | set Allow NOLGIA Agent (for this run; not saved) |
| `NOLGIA_INSTANCE_ID` | fixed session id for this Resolve |
| `NOLGIA_CONFIG_DIR` | where the settings, sign in and log are kept |
| `NOLGIA_IMPORT_DIR` | where imported media is kept |
| `NOLGIA_LUT_DIR` | the LUT folder to install into (Resolve must read it) |
| `NOLGIA_PREVIEW_MAX_BYTES` | the size above which previews are made smaller |

## Known limits

- Resolve's scripting cannot tell whether a project has unsaved changes, so
  `info` says `unsaved_changes: null`, and `open` asks you first.
- Commands run one at a time on the NOLGIA script's own thread, the only one
  that calls Resolve. A long `run` holds the NOLGIA window until it ends; a
  render does not (the plugin checks on it between clicks).
- Rendering opens Resolve's Deliver page and moves the playhead, and about
  half a second after Resolve reports the render complete it moves the
  playhead to the start once more; the plugin waits a second, then puts the
  page and the playhead back. Adding clips to a timeline leaves Resolve's
  playhead at its end. On the Media and Fusion pages Resolve has no timeline
  playhead for scripts, so `preview`, a `png` export and `apply_to:
  "current"` switch to the Edit page for a moment and back. Resolve 21.1.1
  sometimes answers a playhead move with True without moving (right after a
  timeline is made), so the plugin reads the playhead back and sets it again.
- In a fresh Resolve session `ExportCurrentFrameAsStill` answers False until
  the Color page has been shown once. The first time a still export fails,
  the plugin shows the Color page for a moment, comes back and exports
  again, so `preview` and a `png` export work from the first command.
- A project Resolve made itself (New Project, Untitled Project) is not in the
  project library until you save it with a name, and `SaveProject()` on it
  opens Resolve's Save dialog, which blocks every script until someone
  closes it. So `save` refuses such a project and asks you to save it once
  by hand (File > Save Project); `info` reports it as `in_library: false`.
  `open` loads another project over an *empty* unsaved one (Resolve has
  nothing to ask about), but refuses when the unsaved project has content,
  because Resolve could then ask about saving it and a script cannot close
  such a project: in Resolve 21.1.1 `CloseProject()` on it answers False,
  renames it with a timestamp and leaves an "Untitled Project" in the
  library, and `Quit()` opens the save question. Save it with a name or
  close it by hand first.
- When Resolve starts it shows only its Project Manager: no page is open
  (`info` reports `page: null`), and media calls do nothing. `import_asset`,
  `preview` and `export` say so and ask you to open or create a project
  (`open` works from there).
- `append` adds each clip after the timeline's last clip, whatever its
  track, so audio and pictures follow one another rather than overlap.
- Resolve runs a Scripts menu script in its own `fuscript` program, with the
  Python it finds on the computer (on the Windows test machine that was an
  installed Python 3.11, not Resolve's own 3.14). The plugin needs Python
  3.8 or newer: its unit tests pass on 3.8, 3.9, 3.10 and 3.11, and it ran
  under 3.11 inside Resolve and under Resolve's own 3.14 with `--serve`.
- Resolve finds new scripts when it starts, so restart it after installing or
  updating.
- Resolve does not finish a quit (DaVinci Resolve > Quit DaVinci Resolve,
  Command-Q, the close button of its main window) while a script's window is
  open: it closes the project, opens a new Untitled Project, asks script
  windows to close and, as the script is still running at that moment, gives
  the quit up and stays open with no windows. That happens with any script
  window, even a minimal one that closes at once, and never without one
  (Resolve Studio 21.1.1 on Windows 11; the same on the owner's Mac). Only a
  script's own `resolve.Quit()` finishes it. So the NOLGIA window finishes
  the quit itself, but only on Resolve's own quit sequence: a Close less than
  0.5 seconds after the window first saw the project change (it reads the
  project on every tick) to one it had not seen, named "Untitled Project",
  with no timelines, no clips and no bins, not saved in the project library,
  and (on Windows, where it can be read) with none of Resolve's own windows
  left on screen. In Resolve 21.1.1 on Windows 11 that gap was 0.02 to 0.03
  seconds; the window logs it on every quit it finishes, and logs a later
  Close too, so a missed quit shows why. Opening any other project and
  closing the NOLGIA window never quits Resolve, and neither does anything
  the window cannot read. On macOS one case still quits Resolve by mistake:
  New Project (an empty, unsaved "Untitled Project") and the NOLGIA window
  closed less than 0.5 seconds after the new project opens, for example a
  click on its close button while Resolve is still busy making the project.
  On Windows Resolve's main window rules that out. If Resolve's interface is
  in another language its new project has another name, and a quit then
  stays as it was before 0.2.3.

## Tested on

0.2.6 (October 29, 2026), from the owner's third Mac test, where a DIT copy
request was approved about 3 s after the NOLGIA request window opened with
nobody clicking: the request window's buttons stay off for 1 s each time a
request shows (a press on a button that is off never becomes a click, even
if the release comes later), neither button is a default button or takes
keyboard focus, and an answer counts only for the request the buttons were
turned on for; every answer is logged with its request. Sizes in a DIT plan
read as the CLI writes them (601.5 KB, 9.3 MB, not "601500 bytes"). Checked in
Resolve Studio 21.1.1 on Windows 11, with real mouse and keyboard input and
with window messages posted to the request window, at its Copy and verify
button: 0.2.5 approved a click 0.3 s after the window opened and a press at
0.3 s released at 1.5 s (both ways), and Space once an earlier click had
left the reused button with keyboard focus (posted messages); 0.2.6 approved
none of these. Neither approved with the pointer resting on the button, a
press made before the window opened and released on the button, or Return.
0.2.6 approved a click 1.5 s after the window opened (posted messages, and
the in-app test's Run code click). Unit tests and the in-app test (12/12).
On macOS it waits for its re-test.

0.2.5 (October 29, 2026): the NOLGIA window finishes Resolve's quit only
when it closes less than 0.5 s after Resolve's new Untitled Project opens
(1.5 s in 0.2.4), reads the project on every tick, and logs the gap. Checked
in Resolve Studio 21.1.1 on Windows 11, deciding as on Windows and as on
macOS: quitting Resolve with the NOLGIA window open quit it both ways (gap
0.031 and 0.032 s); a new "Untitled Project" made by a script and the window
closed 0.61 s later left Resolve open (the log gives 0.610 s), closed at once
it quit Resolve deciding as on macOS (0.016 s) and left it open on Windows;
opening a saved project and closing the window at once, or closing only the
NOLGIA window, left it open. While Resolve saves the project on a quit the
window's ticks stop for about 1.6 s. Unit tests and the in-app test (12/12).
On macOS it waits for its re-test.

0.2.4 (October 29, 2026): the NOLGIA window finishes Resolve's quit only on
Resolve's own quit sequence (see Known limits); in 0.2.3 on macOS, closing
the NOLGIA window within 1.5 seconds of opening another project would have
quit Resolve too. Checked in Resolve Studio 21.1.1 on Windows 11, once as
the window decides on Windows and once as it decides on macOS (with
Resolve's windows not read): quitting Resolve with the NOLGIA window open
quit it both ways (the new Untitled Project opened 0.02 to 0.03 s before
the window closed), also from Resolve's own Untitled Project at start;
opening a saved project with a timeline and closing the NOLGIA window 0.5 s
later left Resolve open (the log says why); a new "Untitled Project" made
by a script and the window closed 3 s later left it open; on Windows also
at once. Unit tests and the in-app test (12/12). On macOS it waits for its
re-test.

0.2.3 (October 29, 2026), from the owner's second Mac test (of 0.2.2): when the
window is dragged shorter than its minimum, the chat's thread, the text box
and the Activity list give up height and scroll instead of the text box
covering the chat, and the height is put back on every tick (50 ms) instead
of every half second; the NOLGIA request window shows a plain-English plan (a
DIT copy, opening a project) as plain text and only code as Python. Checked
in Resolve Studio 21.1.1 on Windows 11 with the window's minimum lowered to
let it shrink as macOS does (to 628 px there): at every height from 780 down,
0.2.2's thread ran 23 px under the text box and 0.2.3's ended above it; the
height came back in one tick; the in-app test passed (12/12). Quitting
Resolve with the NOLGIA window open now quits it: with 0.2.2's window open,
closing Resolve's main window left Resolve running with no windows (still
after 60 s), and with no script running it quit in 7 s; with 0.2.3's window
open it quit in 8 s, and in 10 s with the NOLGIA window minimized, while
closing only the NOLGIA window, also right after opening another project,
left Resolve open.
On macOS it waits for its re-test.

0.2.2 on macOS 26.6.2 (Apple silicon) with DaVinci Resolve Studio 21.1.1, on
October 29, 2026 (the owner's second Mac test): installed over 0.2.1 with
install.sh; the sign in page named DaVinci Resolve and connected nothing
until Connect; the session carried the Mac's own name and macOS; info and
run commands; a DIT offload approved in the NOLGIA request window and
verified. One failure: dragged below its minimum, the window sat at 646 pt
for about 0.75 s before its height came back, and the text box covered the
chat meanwhile (fixed in 0.2.3).

0.2.1 on macOS 26.6.2 (Apple silicon) with DaVinci Resolve Studio 21.1.1, on
October 29, 2026: installed over 0.1.1 with install.sh, signed in, connected,
and a live NOLGIA Agent turn from the window's chat (it added the newest
NOLGIA clip to the timeline, 70 credits); Enter sends, the thread and
Activity scroll, Open this chat on nolgia.ai opens the browser.

0.2.1 (October 29, 2026): the chat says "Answer with a button below" only
when the Agent's question offers answers (a live turn on 0.2.0 ended with an
open question and the hint showed with no buttons under it). Unit tests.

0.2.0 (the NOLGIA Agent chat) on October 29, 2026, on Windows 11 with
DaVinci Resolve Studio 21.1.1 (build 10): the unit tests; the in-app test
against the mock API, which now also drives the chat in the window (its
first look, a turn working and its reply, a long thread, a question's answer
buttons, out of credits, Allow NOLGIA Agent off); and live NOLGIA Agent
turns against the real API, sent from the window's chat (one on the released
0.2.0: the Agent added a library clip to a new timeline, 70 credits).

Earlier versions:

Windows 11 with DaVinci Resolve Studio 21.1.1 (build 10), on October 5,
2026, against the mock API:

- headless (`NOLGIA.py --serve` under Resolve's own Python 3.14, Resolve open
  with External scripting set to Local): every command, in a throwaway
  project (`tests/e2e_resolve.py`, 31 checks);
- in the app: installed with `install.cmd`, the NOLGIA window run inside
  Resolve the way the Scripts menu runs it, commands run from the window,
  the request window's Run code button, closing the window
  (`tests/inapp_resolve.py`, 10 checks);
- by hand, on October 6, 2026, with External scripting left at None: NOLGIA
  appears at the top level of the Workspace > Scripts menu, choosing it opens
  the NOLGIA window, Sign in opens the browser and the codes match, and the
  connected session then answered `info` through NOLGIA's MCP server. So the
  External scripting preference is needed only to run the plugin from
  outside Resolve, not for the menu.

And against the real NOLGIA API (api.nolgia.ai) the same day, in a
throwaway project, headless and in the app: `info`, `run`, `preview`,
`import_asset` of library assets, of a list of them and of the
`kodak-portra-400` color preset applied to clips, `export` of a frame and
of a two second H.264 MP4, `save`, `open`; `status`, `info`, `run`,
`preview` and `export` went through NOLGIA's MCP server (mcp.nolgia.ai) as
an agent's would, the rest through `POST /bridge/commands`
(`tests/e2e_resolve.py --api prod`, 30 checks;
`tests/inapp_resolve.py --api prod`, 11 checks). Every asset the
runs made was deleted afterwards.

macOS 26.6 (Apple silicon) with DaVinci Resolve Studio 21.1.1 (build 10),
on October 7, 2026, against the mock API: `tests/e2e_resolve.py`
(32 checks) and `tests/inapp_resolve.py` (11 checks), which installs
the download with `install.sh` and runs the NOLGIA window inside the open
Resolve; and against the real API (31 checks headless, 12 in the app). The
in-app test also ran on the published `nolgia-resolve-0.1.0.zip` as a person
gets it: downloaded with the quarantine flag a browser sets, unzipped into
Downloads, installed with `sh ~/Downloads/nolgia-resolve-0.1.0/install.sh`
(11 checks against the mock API, 12 against the real one). Not yet run on
Linux.

DIT in 0.1.1 (the `dit` command kind running the NOLGIA CLI 0.3.1), on
October 15, 2026, against the real API through `POST /bridge/commands`:
on Windows 11 with DaVinci Resolve Studio 21.1.1, a dry-run plan, an
offload approved in the NOLGIA request window, `verify` of the shoot folder,
`inventory` and an import of the copies into a bin; on macOS 26.6.2 (Apple
silicon) with Resolve 21.1.1 and the published `nolgia-resolve-0.1.1.zip`
installed with `install.sh`, the same steps plus a `rename` dry run and
`report --verify`.

## Development

The plugin is standard library Python in
[`nolgia_resolve/`](nolgia_resolve/). Its `core/` is a copy
of the Blender plugin's [`core/`](https://github.com/nolgiainc/nolgia-blender/tree/main/core)
(nolgiainc/nolgia-blender), kept the same by
[`tests/test_core_copy.py`](tests/test_core_copy.py), which
lists the few places they may differ (the app's constants, the argument
checks, the app's name in messages): copy a fix made in one to the other.
The unit tests run with plain Python 3.8 or newer against stand-ins for
Resolve and its UIManager (which follow what Resolve 21.1.1 was seen to do)
and the mock API, including the macOS and Linux paths and installer with the
platform patched:

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
```

On threads: every Resolve call is made from the script's main thread, one
command at a time (the network threads never call Resolve). Simple read
calls from a background Python thread did work in Resolve 21.1.1, both from
ResolvePython and inside the app, but nothing documents the API as thread
safe. Inside the app, UIManager's `RunLoop()` holds Python's lock most of the
time, so the window's timer (every 50 ms, delivered to the dispatcher's
`On.Timeout`) sleeps a few milliseconds each tick to let the network threads
run.

The end-to-end test builds the download, starts Resolve without its window
(or uses the open one with `--use-running`), makes a throwaway project, runs
`NOLGIA.py --serve` under Resolve's own Python against the mock API, drives
every command through it, then deletes the project and the LUTs it
installed. The in-app test installs the download with its installer and runs
the NOLGIA window inside the open Resolve (through Fusion's `RunScript`, as
the Scripts menu does) against the mock API. Both need External scripting
set to Local. From WSL they use the Windows Resolve (paths through
`wslpath`, variables through `WSLENV`):

```sh
python3 tests/e2e_resolve.py
python3 tests/inapp_resolve.py
python3 tests/inapp_resolve.py --download ~/Downloads/nolgia-resolve-0.1.0   # a release, unzipped
```

Build the download (Python 3, standard library). Releases are tagged
`resolve-v<version>` with the zip attached as `nolgia-resolve-<version>.zip`.

```sh
python3 build.py      # dist/nolgia-resolve-0.1.1.zip
```

## This repository

| Path | What it holds |
|---|---|
| [`NOLGIA.py`](NOLGIA.py), [`nolgia_resolve/`](nolgia_resolve/), [`install/`](install/) | The script plugin and its installers |
| [`COMMANDS.md`](COMMANDS.md) | What each command takes and returns |
| [`build.py`](build.py) | Builds the download zip |
| [`tests/`](tests/) | Unit tests, the end-to-end test and the in-app test |
| [`presets/resolve/`](presets/resolve/) | The Film assistant workflows for DaVinci Resolve and the rules they share |
| [`tools/mock_bridge_server.py`](tools/mock_bridge_server.py) | An in-memory mock of the NOLGIA API that the tests run against |

The mock API is the same file in every NOLGIA plugin repository ([Blender](https://github.com/nolgiainc/nolgia-blender), [After Effects, Premiere Pro and Illustrator](https://github.com/nolgiainc/nolgia-adobe), [Photoshop](https://github.com/nolgiainc/nolgia-photoshop) and [DaVinci Resolve](https://github.com/nolgiainc/nolgia-resolve));
copy a change to it into the others.

This plugin used to live in the `resolve/` folder of
[nolgiainc/nolgia-plugins](https://github.com/nolgiainc/nolgia-plugins), with
the other NOLGIA plugins. On October 16, 2026 it moved here with its history,
its branches and its releases. The releases on nolgia-plugins stay where they
are, so links to them keep working. Issues and pull requests belong here.

## License

GPL-3.0-or-later ([`LICENSE`](LICENSE)).
