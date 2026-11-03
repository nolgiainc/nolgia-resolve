# NOLGIA for DaVinci Resolve: commands

The command contract of the NOLGIA plugin for DaVinci Resolve Studio
(app id `resolve`): what each command takes and returns. How to install and
use the plugin is in the [README](README.md).

Commands reach the plugin through the NOLGIA API (`POST /bridge/commands`
with `"app": "resolve"`, a `kind` and `args`) and the MCP tools built on it.
Every command answers `{status, result}` or `{status: "failed", error}`; a
failed `run` also carries `result: {value: null, stdout, stderr}`.

Frames count from 0 at the start of the timeline. `info` gives the
timeline's start timecode and start frame: Resolve's own scripting API
numbers frames from the start timecode (01:00:00:00 at 24 fps is frame
86400), so `run` code that calls Resolve directly uses those numbers.

## `info`

No arguments. Result:

```
{
  app: "resolve", app_version, product, studio,  // Resolve's GetVersionString, GetProductName, IsStudio
  page,                                           // media, cut, edit, fusion, color, fairlight, deliver (or photo)
  document: {name},                               // the open project's name
  project: {name, folder, database, width, height, fps,
            in_library: bool,                     // false for a project never saved with a name
            unsaved_changes: null,                // Resolve's scripting cannot tell
            changed_by_nolgia_since_save: bool, note} | null,
  timeline: {name, fps, width, height, start_timecode, start_frame, end_frame, duration_frames,
             current_timecode, current_frame,
             tracks: {video: [{index, name, items, enabled, locked}], audio: [...], subtitle: [...]},
             selected_clips: [{name, track: "V1", start_frame, end_frame}],
             clip_under_playhead: {name, track, start_frame, end_frame} | null,
             markers} | null,
  current_timecode,                               // null without a timeline
  timelines: [{name, current}],
  media_pool: {root, root_clips, current_bin, bins: [{name, clips, bins}]},
  render: {presets: [...], format_and_codec: {format, codec}, jobs, rendering}
}
```

## `run`

| Argument | |
|---|---|
| `language` | `"python"` (the default; anything else fails) |
| `code` | the Python to run |
| `timeout_seconds` | optional; the plugin also stops in time to report before the command expires |

The code sees `resolve`, `project_manager`, `project`, `media_pool`,
`timeline` (the current one, or `None`), `fusion` and `result`. Result:
`{value, stdout, stderr}`, where `value` is what the code put in `result`,
made JSON (Resolve objects become their names). A failure returns the
traceback in `error`. The code runs on the plugin's own thread, one command
at a time; `nolgia_resolve.disconnect()` and `nolgia_resolve.close_window()`
switch NOLGIA off from code.

## `preview`

| Argument | |
|---|---|
| `frame` | optional, from 0 at the start of the timeline |
| `timecode` | optional, instead of `frame`, e.g. `"01:00:05:12"` |
| `timeline` | optional timeline name (`camera` is accepted as the same thing: it is what the MCP preview tool calls it); default the current timeline |
| `width` | optional, 16 to 1920; default 1280, never wider than the timeline |

Default frame: the current one (Resolve has no playhead for scripts on the
Media and Fusion pages, so the plugin switches to the Edit page for the
moment it needs). Result: `{asset_id, width, height,
mime_type, timeline, frame, timecode}`. The still is uploaded to the NOLGIA
library: a PNG; when that would be over about 3.6 MB (too big for an agent
to see inline), Resolve's own JPEG of the frame if it has the size asked
for, else a PNG with fewer colour levels. The playhead and the current
timeline are put back.

## `import_asset`

Exactly one of:

| Argument | |
|---|---|
| `asset_id` | one asset from the NOLGIA library |
| `asset_ids` | several assets, a list of 1 to 100 ids, imported in this order |
| `project_id` | every ready video, image and audio asset of a NOLGIA project, oldest first (at most 200) |
| `color_preset` | a NOLGIA color preset by slug (`GET /color-presets`), e.g. `kodak-portra-400`, installed as a LUT |

and optionally:

| Argument | |
|---|---|
| `bin` | the media pool bin (under the top bin) to import into; made when missing; default `NOLGIA imports` |
| `append` | `true` to add the media to the end of the current timeline, in order (a new timeline named `NOLGIA timeline` when none is open, made from the clips) |
| `apply_to` | for a single LUT: `current` (the video clip under the playhead), `selected` (the clips selected in the timeline), `current_track` (every clip on the video track of the clip under the playhead) or `all` (every video clip in the timeline) |
| `node` | the grade node to set the LUT on, from 1; default 1 |
| `with_matte` | `false` to bring a green screen or keep-me result in without its matte (see below); default `true` |

Images, video and audio are imported into the bin. With `append`, each clip
goes after the timeline's last clip, whatever its track (that is how Resolve
21.1.1 appends), in the order given; Resolve leaves its playhead at the end.
A `.cube` file from the library (by name, type or content) and a color preset
are installed as LUTs instead: into a `NOLGIA` folder in the LUT folder
Resolve reads (on Windows `%PROGRAMDATA%\Blackmagic Design\DaVinci Resolve\Support\LUT`,
the folder for all users, which every user may write to; see the main README
for macOS and Linux), named after the preset or the file, then Resolve's LUT
list is refreshed. A color
preset replaces its own older copy; a library LUT never replaces another
file (it gets a number). 3D models are refused. With `project_id`, other
kinds of assets are skipped and listed in `skipped`.

A green screen or keep-me render's result names the matte its composite used
(`matte_layer_asset_id`, a grey MP4, white = the person). Unless `with_matte`
is `false`, that matte is downloaded too and attached to the clip as its clip
matte (`MediaStorage.AddClipMattesToMediaPool`): on the Color page it sits
under the clip in the Media Pool, and any node takes it as a key (right-click
the node > Add Matte), so the person can go over anything. The clip's entry
carries `matte` (`asset_id`, `path`, `clip_matte: true`). Where Resolve will
not attach it, the matte comes into the bin as its own clip (`clip_matte:
false`, its `name`). A matte that cannot be downloaded never fails the import
(`matte_error`).

Result for media:

```
{
  imported: [clip names, in order],
  asset_ids: [ids, in order],
  assets: [{asset_id, name, kind, path, already_in_bin?}],   // kind: image, video, audio or lut
  kind: "video" | "image" | "audio" | "mixed",
  bin,
  path,                                   // with a single asset only
  skipped?: [{asset_id, name, why}],      // project_id only
  appended?: {timeline, new_timeline, items: [{name, track: "V1" | "A1", start_frame, end_frame}]}
}
```

Result for a LUT (`color_preset`, or a single `.cube` asset):

```
{
  imported: [LUT name], kind: "lut", path, lut: "NOLGIA/Kodak Portra 400.cube",
  color_preset?, asset_ids?: [id],
  applied_to?: [{name, track, start_frame, end_frame}], not_applied?: [{..., why}], node?
}
```

`lut` is the path under the LUT folder (Resolve 21.1.1's own `GetLUT()` then
reports it as `NOLGIA\Kodak Portra 400.cube` on Windows). When `apply_to` is
given and no clip took the LUT, the command fails, with the result above (the
LUT stays installed). `current` and `current_track` need a video clip under
the playhead; on the Media and Fusion pages, which have no playhead for
scripts, the plugin switches to the Edit page for the moment it needs.

## `export`

| Argument | |
|---|---|
| `format` | `png` (one frame) or `mp4` (H.264, with the timeline's audio) |
| `frames` | optional: `"24"` or `"0-119"` (also `24`, `[0, 119]`, `{"start": 0, "end": 119}`); `png` default the current frame, `mp4` default the whole timeline |
| `filename` | optional; default the project's name |
| `timeline` | optional timeline name; default the current timeline |

Renders at the timeline's size and uploads the file. Result: `{asset_id,
format, filename, frames: [first, last], width, height, timeline}`, plus
`timecode` for `png`. Renders go to a temporary folder (never over a file of
yours) that is removed afterwards; the render job is removed from the render
queue, the Deliver page's settings, the page Resolve was on and the
playhead are put back (rendering opens the Deliver page and moves the
playhead).

## `save`

No arguments (a `path` is refused: Resolve keeps projects in its project
library). Saves the open project. Result: `{project, saved: true}`.

A project Resolve made itself (New Project, Untitled Project) is not in the
project library until it is saved with a name (`info` reports `in_library:
false`), and Resolve's `SaveProject()` on it opens the Save dialog, which
blocks every script until someone closes it. `save` refuses such a project
instead, with a message asking the person to save it once by hand (File >
Save Project).

## `open`

| Argument | |
|---|---|
| `project` | the project's name (`name` is accepted too) |
| `folder` | optional project folder path, e.g. `Clients/ACME`; default the current folder |

Result: `{project, folder}`, or `{project, already_open: true}`. Resolve's
scripting cannot tell whether the open project has unsaved changes, so with
the NOLGIA window open the person is asked first (Open project or Deny);
without a window it is refused only when NOLGIA itself changed the project
since it was last saved.

When the open project has never been saved (`in_library: false`): an empty
one (no timelines, clips or bins) is simply loaded over, which Resolve does
not question; one with content is refused in every mode, with a message to
save it with a name (File > Save Project) or close it by hand first, because
loading over it could make Resolve ask about saving it and a script cannot
close such a project (Resolve 21.1.1's `CloseProject()` answers False and
renames it).

`import_asset`, `preview` and `export` fail with a plain message while
Resolve shows only its Project Manager (no page open, `info` says `page:
null`); `open` works from that state.

## `grade`

Colour grading done by the plugin itself, in this Resolve (0.3.0 and newer):
colour management from the camera's log, white balance and exposure measured
from each clip, shot matching to a hero clip, and a look at a strength: from
NOLGIA's own film look library, or (0.3.1 and newer) a LUT already on this
computer, such as DaVinci Resolve's own Film Looks or the person's own LUTs. It
costs no NOLGIA credits.

| Argument | |
|---|---|
| `action` | `apply` (default), `look`, `looks` or `inspect` |
| `clips` | `all` (default), `selected`, `current`, or clip numbers `[1, 3]` (1 is the first clip in timeline order, as the Color page numbers them) |
| `camera` | `auto` (default: the clip's metadata, a Sony sidecar XML or the file name) or `slog3`, `logc3`, `logc4`, `vlog`, `clog2`, `clog3`, `bmdfilm5`, `applelog`, `flog`, `flog2`, `flog2c`, `dlog`, `nlog` (0.3.1), `rec709` |
| `correct` | `apply`: measure and set white balance and exposure (default true) |
| `match` | the hero clip's number: the others' colour and level are matched to it; the hero keeps its own white unless `neutral_hero` |
| `exposure` | `auto` (default: `match` with a hero, else `even`), `match`, `even` (halfway to the clips' median, at most a stop) or `keep` |
| `look` | a look's slug or name from the library, or `none` |
| `lut` | instead of `look` (0.3.1): a LUT on this computer, by the id, name or full path `looks` lists (`installed_with_resolve`, `your_luts`) |
| `lut_input` | what that LUT takes in: `auto` (default: read from its header, title or file name; Resolve's Film Looks take Cineon), or `cineon`, `rec709`, `dwg`, `acescct`, or a camera log above |
| `folders` | more folders of the person's LUTs to look in, up to 20 (0.3.1); the Grade window's **Add folder** remembers them |
| `strength` | 0 to 1, or a percent (60 is 60%); default 1 |
| `contrast`, `saturation` | Primaries Contrast (pivot at DaVinci Intermediate mid grey, 0.336) and Saturation; 1 is none |
| `skin` | the skin line node (default true) |
| `neutral_hero` | also take the hero's white to neutral |
| `frames` | frames measured per clip, 1 to 5 (default 3) |

`apply` and `look` change clips, so they wait in the NOLGIA window for the
person's **Grade clips** click (the request window's rules: buttons on after a
second, only a click made after that counts); with no window open they do not
run. `looks` and `inspect` only read.

**`apply`.** Each clip gets a new local color version, `NOLGIA grade` (then
`NOLGIA grade 2`, ...), so the person's own grade stays in the clip's Versions
menu. In it, through `Graph.ApplyGradeFromDRX`, eight labelled serial nodes:

| Node | Holds |
|---|---|
| 01 CST IN | the camera log to DaVinci Wide Gamut / DaVinci Intermediate (a DCTL; it gives the same picture as Resolve's own colour management) |
| 02 BALANCE | white balance on the Offset wheel, per channel (a CDL offset in log) |
| 03 EXPOSURE | exposure on the Offset wheel (one stop is 0.0733 in DaVinci Intermediate) |
| 04 CONTRAST | Primaries Contrast and Pivot |
| 05 SAT | Primaries Saturation |
| 06 SKIN | the skin line DCTL (skin hues only) |
| 07 LOOK | the look at its strength: a DCTL that applies the look's .cube with `DEFINE_LUT` and mixes it |
| 08 CST OUT | DaVinci Wide Gamut / Intermediate to Rec.709 gamma 2.4 (a DCTL with a filmic tone curve, exactly invertible) |

White balance and exposure are measured on frames exported after CST IN, in
scene light: the white is the least saturated bright pixels; skin is found
after the white's cast is taken out; exposure follows the white, averaged with
skin when the two agree within half a stop. A second measurement after the
first pass corrects what is left. In a colour managed or ACES project CST IN
and CST OUT are left to Resolve and nothing is measured; the look is the
project's working space version.

Result: `{project, timeline, color_science, hero, look, strength, lut_folder,
seconds, cost, clips: [{clip, name, camera, camera_found_by, version,
your_version, balance_stops: [r, g, b], exposure_stops, matched_on, measured,
residual_after_first_pass, second_pass?, nodes: [{node, label, lut}]}],
note?, warning?}`. When `camera` is `auto` and a clip says nothing about its
log, it fails with `result: {needs_camera: [...], choices: [...]}`: ask the
person.

**`look`.** Sets another look or LUT (or strength) on the LOOK node of clips
NOLGIA graded, found by its label, leaving every other node as it is; `none`
switches the node off. Result: `{look, user_lut, strength, lut, changed,
skipped}`.

**A LUT on this computer (`lut`, 0.3.1).** The command kind `grade_lut` is
`grade` with the same arguments; NOLGIA's API sends `grade_lut` when `lut`,
`lut_input` or `folders` is given, so a 0.3.0 plugin (which does not have the
kind) refuses it instead of grading without the LUT. NOLGIA never copies, ships or
changes the file: it writes a small DCTL in `NOLGIA Grade/LUTs at strength`
that points at the file (`DEFINE_LUT` with its full path), takes the clip from
DaVinci Wide Gamut / Intermediate to what the LUT takes in (Cineon for
Resolve's Film Looks, a camera log, Rec.709 through CST OUT, or the working
space as it is), applies it, brings a display-referred result back through
CST OUT's exact inverse, and mixes it at `strength`. Checked in Resolve 21.1.1
on its own Rec709 Kodak 2383 D65 and Fujifilm 3513DI D65 at 100% and 50%: a
144-patch chart matches a Python reading of the same file to 0.0015 (one
saturated patch 0.009). It refuses, by name: a file whose path has a bracket,
comma, semicolon or quote (Resolve stops on such a DCTL with an error dialog
that holds it until someone clicks OK); a LUT for a log NOLGIA has no transform
for (D-Log M, Log3G10, L-Log, OM-Log, HLG, PQ, the older Blackmagic Film); in a
colour managed project, a LUT that is not for the working space; in ACES, one
not for ACEScct. DCI-P3 versions of the Film Looks are not listed.

**`looks`.** The library (`slug`, `name`, `family`, `description`,
`emulates`) with `names_note` (the stock and camera names line), the cameras;
`installed_with_resolve` (the LUTs that come with Resolve, under Resolve's own
names) and `your_luts` (the person's own: any other LUT in Resolve's LUT
folders and in `folders`), each `{id, name, group, folder, input, output,
path}`; and `official_free_luts`: the makers' own free LUT pages (Fujifilm,
ARRI, Panasonic, RED, Nikon), links only, with `official_note`.

**`inspect`.** Per clip: the camera log found and how, the current color
version and its nodes (label, LUT, tools); and the project's colour science.

The files live in `NOLGIA Grade` in Resolve's LUT folder (`CST IN`, `CST OUT`,
`Corrections`, `Looks/<family>`, `Looks ACEScct/<family>`), written by the
plugin when first needed: each look as a 33 point `.cube` and its strengths as
small `.dctl` files at 10% steps; `LUTs at strength` holds the DCTLs that point
at LUTs on this computer.
