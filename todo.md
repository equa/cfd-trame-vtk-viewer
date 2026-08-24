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

**Deferred for discussion** (as flagged). See the discussion in chat — the camera
save/recall (slots 1–4) is straightforward but touches the fragile local-mode
camera sync, and scene export/load needs a decision on format/scope. Not
implemented yet.
