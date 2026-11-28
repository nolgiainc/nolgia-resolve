---
slug: resolve-color-grading
name: Grade a timeline in DaVinci Resolve
category: Film assistant
app: resolve
min_app_version: "21.1"
beta: true
starter_prompt: "Use NOLGIA to grade the timeline open in DaVinci Resolve: convert the camera log, correct every clip, match them to the shot I pick, then add a film look from NOLGIA's library at the strength I choose. Show me before and after stills from the same frame and keep my own grade in each clip's versions."
---

# Grade a timeline in DaVinci Resolve

Grade the clips on the person's timeline in the order a colorist works: the
camera's log converted first, white balance and exposure corrected on every
clip from its own frames, the clips of a scene matched to one hero shot, and
only then a film look at a strength. One tool does all of it,
`nolgia_app_grade` (NOLGIA for DaVinci Resolve 0.3.0 and newer). It lands as
eight labelled nodes in a new color version of each clip, named
`NOLGIA grade` (01 CST IN, 02 BALANCE, 03 EXPOSURE, 04 CONTRAST, 05 SAT,
06 SKIN, 07 LOOK, 08 CST OUT), so the person keeps working on it by hand and
their own grade stays in the clip's Versions menu. The look can be swapped,
turned down or switched off at any time. The grading costs no NOLGIA credits.

Do not do this job by hand with `nolgia_app_run` (a film stock LUT set on an
empty node with `SetLUT`). One LUT that converts the log and adds the look in
the same node leaves no place to correct or match a shot before the look, and
a correction made after it works on a finished picture. Those were this
workflow's steps before the plugin had `grade`; `nolgia_app_grade` replaces
them.

## Ask

1. Which clips: every clip on the current timeline (default), the clips
   selected in the timeline, the clip under the playhead, or clip numbers as
   the Color page numbers them (1 is the first clip in timeline order).
2. The hero clip to match the others to, if any. Offer a wide or establishing
   shot of the scene, well exposed, from its still: a wide shows the scene's
   light, its shadows and something white or grey, which is what the other
   shots are matched on. Never offer an insert or a close-up as the hero; it
   shares too little with the rest. A timeline that holds more than one scene
   is graded scene by scene, each with its own hero (see Build, step 4).
   (Default: the wide you suggest when the clips are one scene; no hero, each
   clip corrected on its own, when they are not.)
3. The look and its strength. Offer two or three from NOLGIA's film look
   library by their names, with one line each from its description
   (`nolgia_app_grade` with `action: "looks"`); the default strength is full
   (100%), or what the person said ("60%", "a bit less"). When they name a LUT
   they already have (DaVinci Resolve's own Kodak 2383 or Fujifilm 3513DI
   Film Looks, a camera maker's LUT, their own show LUT), use it instead:
   `lut` with its id, name or path from `installed_with_resolve` or
   `your_luts` (plugin 0.3.1 and newer), and `folders` when it lives in a
   folder NOLGIA does not list yet. A maker's free LUT they do not have yet:
   give them its page from `official_free_luts`; NOLGIA never downloads or
   ships it.
4. Only when the inspect (Build, step 1) cannot tell: which camera the clips
   were recorded with (Sony S-Log3, ARRI LogC3 or LogC4, Panasonic V-Log,
   Canon Log 2 or 3, Blackmagic Film, Apple Log, Fujifilm F-Log, DJI D-Log,
   Nikon N-Log, or a normal picture that is not log).

## Build

1. **Check the plugin and read the timeline.** `nolgia_app_status` must list
   DaVinci Resolve with `grade` among its commands. Listed without `grade`:
   the plugin is older than 0.3.0; ask the person to update NOLGIA for DaVinci
   Resolve from nolgia.ai/plugins/davinci-resolve and wait, because this
   workflow needs it. Then `nolgia_app_grade` with `action: "inspect"` (it
   changes nothing and needs no click): the camera log found for each clip
   and how it was found, each clip's versions and nodes, and the project's
   color science. Say in one line what it found. A color managed or ACES
   project is fine: say that Resolve's color management stays in charge of
   the conversion and that nothing is measured there.
2. **The before stills.** `nolgia_app_preview` at a frame with skin or the
   main subject in good light, in the middle of the hero clip (frames count
   from 0 at the start of the timeline). Keep the frame number: the after
   still is taken from the same one. To offer a hero, look at a still of each
   candidate first and pick the wide.
3. **The looks that suit it.** Unless the person named one, `nolgia_app_grade`
   with `action: "looks"` once, and choose two or three by their library
   names that suit the still (daylight or tungsten, how much contrast, whether
   skin is in the frame). Wait for a choice unless they said "go", then take
   your first suggestion.
4. **Grade.** Tell the person that the NOLGIA window in Resolve will ask them
   to click **Grade clips**; it changes nothing until they do, and with no
   NOLGIA window open it does not run (Workspace > Scripts > NOLGIA). Then
   one `nolgia_app_grade` with everything they chose, for example
   `{"app": "resolve", "action": "apply", "clips": "all", "match": 2,
   "look": "kodak-2383-print", "strength": 1}`. For a timeline with more than
   one scene, send one apply per scene, with that scene's clip numbers in
   `clips` and its own hero in `match`. If a tool hands back a `command_id`,
   follow it with `nolgia_app_command` instead of starting it again. If it
   answers `needs_camera`, ask which camera and send the same apply again
   with `camera`.
5. **The after stills.** `nolgia_app_preview` at the same frame as the before
   still, and one frame from every other clip the grade touched (or, on a
   long timeline, from each scene and from every clip the answer names).
   Show before and after and say in two lines what moved (skin, shadows,
   highlights, saturation). Give the per clip `balance_stops` and
   `exposure_stops` from the answer.
6. **A shot left as shot** (plugin 0.3.5 and newer). When the answer has
   `left_as_shot`, say which clips and why in its own words (`say`): the
   plugin leaves a shot's color and exposure alone when it cannot tell a
   camera's mistake from the scene's own color (a close-up with nothing
   white or grey in it, the ground in late sun), as a colorist would before
   guessing. Show that clip's still and ask: is the palest bright thing in it
   really white or grey (a shirt, paper, a wall)? If yes and your
   `nolgia_app_grade` tool lists `trust_white`, send the apply again for that
   clip with `trust_white` and its number. If it does not list it, or the
   answer is no, the clip is theirs to set by eye against the hero on
   02 BALANCE and 03 EXPOSURE (the Offset wheel of each); offer to go through
   it with them, a tenth of a stop at a time.
   On a plugin from 0.3.0 to 0.3.4 no clip is left as shot: a clip whose
   `balance_stops` differ by more than 0.6 between two channels was moved a
   long way on what it took for white. Look at its after still; if the color
   is wrong (a blue or orange wash), tell the person, grade that clip again
   alone with `correct: false`, and suggest the update.

## Change it

- Another look, or the same look weaker or stronger: `nolgia_app_grade` with
  `action: "look"`, `look` and `strength`. Only the 07 LOOK node changes;
  what the person changed on the other nodes stays. `look: "none"` switches
  it off. It asks for the Grade clips click again.
- A LUT on their computer works the same way with `lut`. When the picture
  comes out wrong (washed out or crushed), the LUT expects another input:
  send it again with `lut_input` (`cineon`, `rec709`, or the camera log).
- Less or more color or contrast under the look: 05 SAT (Primaries
  Saturation) and 04 CONTRAST (Primaries Contrast) by hand, or an apply with
  `saturation` or `contrast` (1 is none, 0.85 is a little less), which makes
  a new version next to the first.
- By hand: 02 BALANCE and 03 EXPOSURE are the Offset wheel; the look's
  strength steps are in the 07 LOOK node's LUT menu (NOLGIA Grade > Looks >
  the look's family).

## Check

- The apply's answer: every clip in scope is listed with a `version`. A clip
  whose `measured` says `skipped` was not measured: say which and why.
  `residual_after_first_pass` is what the second measurement found left after
  the first correction; more than half a stop there means the clip was hard
  to read (night, no white or skin in the frame): look at that clip's still
  and name it in the reply.
- The finished picture, when the answer has `finished_picture`: the plugin
  read each clip's frames with every node on, the picture the viewer shows,
  against the scopes' table (shadows about 5 to 10% above black, highlights
  under about 90% with the top tenth for the sun, lamps, windows and glints,
  no clipping, skin highlights about 50 to 75%). The parade, each channel's
  top and bottom, is reported, never flagged: the plugin cannot find a
  neutral object in the frame, and warm light that was meant, a coloured
  thing or a look's own tint part the channels. For every clip in
  `outside`, give `say` in your own words next to that clip's after still
  and ask the person whether it is meant (a night scene, a bright window, a
  backlit face sit outside on purpose). These are flags for the person,
  never targets: never call the grade wrong for them, and never change it
  to bring a number inside on your own. A clip in `unread` was not checked:
  say which and why.
- The after stills, with the look on, at the timeline's width (the plugin
  caps `width` at it). Skin reads as skin, with no green or magenta cast;
  shadows still hold detail a little above black; highlights stay under
  white, with only the sun, lamps, windows and glints at the very top; a
  white or grey object reads neutral; nothing clips that did not clip before.
  Log footage now looks like a finished picture, not flat. When a still fails
  one of these, say which and what you would change (a weaker look, less
  saturation, a clip set by eye); do not call it done.
- The match, judged on the cut: put the after stills of neighbouring clips
  side by side. Skin and the same objects should sit at the same color and
  level from shot to shot.
- The person's own grade: each clip's own version is still in its Versions
  menu (`your_version` in the answer), unchanged.

## Deliver

Save the project with `nolgia_app_save`. Export the after frame with
`nolgia_app_export` `png`, and offer an `mp4` of a short range (`frames` such
as `0-119`) so they can see it in motion. Tell the person: the hero, the look
and its strength, the table of corrections per clip, which clips were left
as shot and why, which clips' finished picture sits outside the scopes' table
and on which rows, the version name on each clip (`NOLGIA grade`) and that
their own grade is in the clip's Versions menu, the eight nodes they can
adjust, how to change the look or switch it off (the 07 LOOK node, or ask
for another), that NOLGIA's look files are in NOLGIA Grade in Resolve's LUT
folder, and the path of the safety copy.

What this workflow never does: it never changes, resets or deletes the
person's own nodes, LUTs, versions or settings; it never puts a look before
the corrections or corrects a clip after the look; it never grades a
timeline they did not ask about; and it never renders, overwrites or deletes
anything without asking first.
