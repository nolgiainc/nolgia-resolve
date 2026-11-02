---
slug: resolve-grade
name: Correct, match and grade in DaVinci Resolve
category: Film assistant
app: resolve
min_app_version: "21.1"
beta: true
starter_prompt: "Use NOLGIA to grade my DaVinci Resolve timeline: colour manage the camera log, correct every clip, match them to the clip I pick, then give it a film look from NOLGIA's library at the strength I choose. Keep my own grade in each clip's versions."
---

# Correct, match and grade in DaVinci Resolve

Do a colourist's first pass and the look in one go, with the plugin's own
`grade` command (NOLGIA for DaVinci Resolve 0.3.0 and newer): colour
management from the camera's log, white balance and exposure measured from
each clip, every clip matched to a hero, and a look from NOLGIA's film look
library at a strength. It lands as eight labelled nodes in a new color version
of each clip (CST IN, BALANCE, EXPOSURE, CONTRAST, SAT, SKIN, LOOK, CST OUT),
so the person keeps working on it by hand and their own grade stays in the
clip's Versions menu. The grading itself costs no NOLGIA credits.

## Ask

1. Which clips: every clip on the timeline (default), the selected ones, or
   clip numbers as the Color page numbers them.
2. The hero clip to match to, if any. Offer the best exposed clip with skin in
   the scene's main light, from its still.
3. The look and its strength. Offer two or three from the library by name,
   with one line each from its description (`grade` with `action: "looks"`);
   default strength 100%, or what the person said ("60%"). When they name a
   LUT they have (Resolve's own Kodak 2383 or Fujifilm 3513DI Film Looks, a
   camera maker's LUT, their own show LUT), use it instead: give `lut` with
   its id, name or path from `installed_with_resolve` or `your_luts` (NOLGIA
   for DaVinci Resolve 0.3.1 and newer), and `folders` when it lives in a
   folder NOLGIA does not list yet. A maker's free LUT they do not have yet:
   give its page from `official_free_luts`; NOLGIA never downloads or ships
   it.
4. Only if `inspect` cannot tell: which camera log the clips are in.

## Build

1. `grade` with `action: "inspect"`: the camera log found per clip and how,
   their versions and nodes, and the project's colour science. A colour
   managed or ACES project is fine: say that Resolve's colour management
   stays in charge and nothing is measured.
2. A before still: `preview` at a frame with the subject, in the hero clip.
3. `grade` with `action: "apply"`, `match`, `look` and `strength` (and
   `camera` when step 1 could not tell). Tell the person to look at Resolve:
   it waits for their **Grade clips** click. If it answers `needs_camera`,
   ask which camera and send it again with `camera`.
4. The after still at the same frame. Say in two lines what moved, and give
   the per clip `balance_stops` and `exposure_stops`.

## Change it

- Another look or strength: `grade` with `action: "look"` (only the LOOK node
  changes; their own changes to the other nodes stay). `look: "none"` switches
  it off. A LUT on their computer works the same way with `lut`; when the
  picture comes out wrong (washed out or crushed), the LUT expects another
  input: send it again with `lut_input` (`cineon`, `rec709`, or the camera
  log).
- By hand: the strength steps are in the LOOK node's LUT menu (NOLGIA Grade >
  Looks > the family), BALANCE and EXPOSURE are the Offset wheel, CONTRAST and
  SAT the Primaries.

## Deliver

Save the project. Tell the person: the hero, the look and strength, the table
of corrections per clip, the version name on each clip and where their own
grade is, and that the files are in NOLGIA Grade in Resolve's LUT folder.
