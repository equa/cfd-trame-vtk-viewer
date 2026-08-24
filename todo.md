# Pending improvements to the viz UX

Claude: You may edit this file. Short comments on progress like "done" or "refused" for example.

## Vector actor (Arrows)

- Make the glyph distribution uniform over the source plane
  — **done**. "On plane" now lays a regular grid over the cut plane and probes
  the volume (vtkPlaneSource → vtkProbeFilter → vtkThresholdPoints), so arrows
  are evenly spaced regardless of mesh density. Count ≈ total grid points.
- Extra: Replace "In volume" option with "On isosurface"
  — **done**. "On isosurface" seeds arrows off the isosurface (contour output);
  works even when the isosurface actor itself is hidden.

## Iso surfaces

- Change default number of surfaces to 1 (slider). — **done**
- The Count slider to only allow 1, 3 and 5 — **done** (slider min 1, max 5, step 2).
- The default values calculated as presently — **done** (interior fractions across the range).
- Add value input field and add a range input field, defaulting to the colour range
  — **done**. Value field shown for 1 surface; Min/Max for 3/5. Both seed from
  (track) the colour range on field change / rescale.

## Fields

- Convert temperature field to Celsius — **done**. Converted K→°C once at read
  time (cheap: one field-sized copy per read, not in place). Legend unit is [°C].
- Skip loading p, alphat, omega, epsilon, rho — **done**. Disabled at the reader
  (never read or interpolated); absent ones ignored.

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
