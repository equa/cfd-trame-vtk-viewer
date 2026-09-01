# Pending improvements to the viz UX

Claude: You may edit this file. Short comments on progress like "done" or "refused" for example.

Sometimes I (Niklas) want to extend on a topic that is marked as **done**. I will then keep
the original and add a **new request**, that you (Claude) should change to **done** or just remove
if we agree to discard the idea.

## General

When my requests require big changes, ask before. I might be asking for silly things.

Always make sure to reuse existing functions, classes. I know how much easier it
is to write a new function instead of searching for existing implementations.
Better to generalize existing functions, as long as the argument list does not
grow (too much).

Prefer classes before passing arguments through several function calls and
before very long argument lists.

### Performance


## Vector actor (Arrows)

- Extra: Replace "In volume" option with "On isosurface"
  — **done**. "On isosurface" seeds arrows off the isosurface (contour output)
    works even when the isosurface actor itself is hidden.
  - **new request**. How is the seeding done over the iso-surface? Is it evenly
    spaced just like for the cut plane arrow seeding? If not evenly distributed,
    then please make it evenly distributed.
    — **answer/discuss**: NOT as even as the plane. The plane uses a regular grid
      (vtkPlaneSource→probe). The isosurface uses vtkMaskPoints, which random-
      samples the surface's existing MESH VERTICES → density follows the
      triangulation (finer mesh = more arrows), spread but not uniform. Truly
      even is non-trivial: vtkPolyDataPointSampler only densifies (wrong way) and
      interior-only sampling returned 0 pts; VTK has no clean "N even points on a
      surface" filter. Options: (a) accept current; (b) MaskPoints
      SPATIALLY_STRATIFIED mode (a bit more even); (c) a real surface resampler
      (more code). Awaiting your pick before changing.

## Iso surfaces


## Fields


## Color map and color range options

- Move the bands input into the options dialog — **done**
- Move the Auto-range toggle to the top toolbar, just left of the Rescale button — **done**
- Add an Apply button to the options dialog and defer all settings in the
  options dialog til Apply is pressed. Currently, for non-small cases, things
  stack up in a queue. — **done** (colour Options popover; drafts + Apply,
    open-sync). Streamlines Apply done too.
- A very nice featyre of ParaView is color-map weigthed opacity. Is is this
  available? If so, please add a toggle in the options. Linear only.
  — **done server-side, NEEDS BROWSER CHECK**: "Opacity by value (linear)" toggle
    in the Options popover. LUT is now a discretizable CTF with a linear alpha
    ramp. Verified the alpha ramps 0..1 on the server; UNKNOWN whether vtk.js
    local mode renders per-scalar surface opacity. Please try it in the browser
    and tell me if surfaces fade by value — if not, we drop/rework it.

## Boundary

- Default to full opacity 1 — **done** (surface_cull on keeps the interior visible)

## Streamlines

- Default to line representation, line width 1, (as opposed to Tubes). Toggle
  for tubes (off by default). — **done**
- Heavy operation, so needs an Apply button and defer streamline changes. There
  is no need to remember un-applied changes. — **done** (Seeds/Tubes/width/length
    defer to an Apply button; drafts refresh from current when the tool opens).
    Vector field + the eye visibility stay live.
- **Discuss** Could it be a good idea to seed on isosurfaces? Seeding strls is a
  general problem. Now cut plane is the seed base, which is pretty good, but
  lacks precision.
  — **discuss**: yes, plausible and not much new plumbing — the isosurface
    (contour) output already exists and the glyphs already seed off it. A stream
    "seed source" toggle (plane | isosurface) would reuse that. Precision-wise
    it lets you seed exactly on a feature (e.g. a velocity isosurface). Worth a
    small experiment if you want it.


## Camera and scene persistence

**Deferred — design agreed-ish, implementation on hold.** Two sub-features:

### B. Scene state export / load  (the easy, robust half — do first)
- `server.state` is a dict. Whitelist the viz vars (field/component/preset/
  n_colors/auto_range/robust_range/range_min/max/use_cell_data, plane_axis +
  plane_x/y/z, every `*_visible`, the per-tool settings incl. the new
  contour_value/min/max + glyph_source, lighting, ui_theme).
- Export = dump that subset to JSON (download). Load = read JSON → set the vars
  → `update_scene()`. Rides the existing change-handler path → low risk, no
  camera-sync involvement.

### A. Camera slots 1–4  (the tricky half)
- **Recall** is trivial & proven: set the server camera params + `view_push_camera`
  (same mechanism as the X/Y/Z/Iso view buttons).
- **Save** is the catch: in local (vtk.js) mode the *client* owns the camera,
  and `push_remote_camera_on_end_interaction` was removed (it caused the COR
  resets), so there is no live client→server camera sync now. To capture the
  current view, reuse the **F-key client→server bridge** (client JS reads the
  vtk.js camera → `window.trame.trigger` → a server handler stores it). Same
  pattern already working for F-key COR picking.
- If the 4 slots live in `state`, a scene export (B) bundles them for free.

### Open decisions (answer before implementing A)
1. Camera capture via the client-JS→trigger bridge (like F-key)? — the only
   reliable way to read the live client camera. (Recommended.)
2. Persistence model: cameras 1–4 as in-session quick-save buttons + export/load
   as a downloaded JSON bundling cameras + all settings? OR persist saved scenes
   server-side per case (a file in the case dir) so they survive without a
   download?
3. Scope of "scene state": just viz settings (+cameras), or also the selected
   case/time step?

Recommendation when resumed: build B first (safe), then A once (1)/(2) decided.
