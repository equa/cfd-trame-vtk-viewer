# FoamViz — notes for future sessions

Trame + VTK web visualiser for OpenFOAM cases. Experiment aimed at an IDA ICE
CFD backend. See `README.md` for what it does and how to run it; this file
records the things that cost time to discover.

## Status

Built 2026-08-11 in one session. Working and verified:

- 29 server-side pipeline checks (`tests/test_pipeline.py`) and a 9-step
  headless-Chromium suite (`tests/browser_check.py`), both green on VTK 9.3.1
  and 9.6.2.
- **Niklas tested up to 12 M cells and reports it "works nicely".** That
  retires the single biggest open question from the build, which was whether
  `vtkOpenFOAMReader` and the client-side (vtk.js) path would survive a real
  case. Assume the architecture scales; stop treating scale as the blocker.
  Still unrecorded, worth asking before optimising anything: which render mode,
  what load/slice/stream-trace timings, what hardware, and whether client mode
  needed decimation at that size.

Not yet done — see "Backend integration" at the end of this file.

## Resuming in a new workspace

The project is **not under version control** and lives at `/workspace/foamviz`.
A `.gitignore` is already in place, so `git init && git add -A` is clean
whenever it needs to move.

What actually needs to travel is about **1 MB**:

```
CLAUDE.md README.md requirements.txt .gitignore main.py foamviz/ tests/ docs/
data/hotRoom/{Allrun,Allclean,system,constant,0}     # the case definition only
```

Everything else rebuilds:

- `.venv/` (763 MB) — `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`
  (read the VTK note below first).
- `data/hotRoom/` (74 MB) — regenerate, see "Demo data" below. Or just point
  `--data` at a real case; nothing depends on the demo except the tests.

`tests/test_pipeline.py` hardcodes the demo case (32 000 cells, 19 time steps,
3 patches, T range 300–600 K). Point it elsewhere and those specific
assertions need updating — the checks are deliberately concrete.

## Environment

- **Persistent venv at `/home/node/.venvs/foamviz`** (survives workspace
  respawns — `/home/node` is a persistent volume, `/workspace` is not). Built
  from this repo's package set with `vtk-osmesa` 9.3.1 (no system Mesa here).
  Use it to actually run things:
  `/home/node/.venvs/foamviz/bin/python tests/test_pipeline.py` (29 checks),
  or a headless app smoke `... -c "from foamviz.app import FoamViz; FoamViz('data/hotRoom')"`.
  A project-local `.venv` (763 MB) may also exist but is wiped on respawn.
- Python 3.12, Ubuntu 24.04 container, **no root, no sudo**.
- `render_window.GetClassName()` should report `vtkOSOpenGLRenderWindow`.

### VTK packaging — checked 2026-08-11, don't re-derive

- `requirements.txt` asks for plain **`vtk>=9.4`**. Since 9.4 the stock PyPI
  wheels contain EGL *and* OSMesa render windows and fall back X11 → EGL →
  OSMesa at runtime. They **dlopen** libEGL/libOSMesa instead of bundling, so a
  slim container still needs `apt-get install libosmesa6`.
- **This container has no system Mesa**, so `.venv` is installed with
  `vtk-osmesa` 9.3.1 (statically bundles OSMesa, works with zero system deps).
  That is a deliberate local deviation from `requirements.txt`, not a mistake.
  With OSMesa staged onto `LD_LIBRARY_PATH`, stock `vtk` 9.6.2 works here too —
  both the 29 pipeline checks and the 9-step browser suite pass on it.
- `vtk-osmesa` is a dead end and should not be the default: not on PyPI
  (`--extra-index-url https://wheels.vtk.org`, which 301s to a GitLab package
  index), frozen at **9.3.1**, wheels only for cp36–cp312 on linux x86_64 and
  win amd64. Python 3.13+/macOS/arm64 cannot resolve it at all.
- **trame does not lag VTK.** `trame-vtk` 2.11.15 requires only `trame-client`
  — no VTK pin anywhere. VTK 9.6.2 was verified end-to-end including vtk.js
  client-side serialisation.
- OpenFOAM 13 at `/opt/cfd/OpenFOAM-13`; `source /opt/cfd/OpenFOAM-13/etc/bashrc`.
- Playwright's Chromium binaries are cached but its **system libraries are
  not installed and cannot be** (no root). They are staged into a user prefix
  and reached via `LD_LIBRARY_PATH` — the recipe is in `README.md`. Without it,
  Chromium dies with `libglib-2.0.so.0: cannot open shared object file`.

## VTK-Python traps hit here

- **Never pass a freshly constructed source inline.**
  `glyph.SetSourceConnection(vtk.vtkArrowSource().GetOutputPort())` **segfaults**
  — Python collects the temporary while the pipeline still references it. Keep an
  attribute for every source. This cost the most time of anything in the build.
- **Actor-level transforms do not survive serialisation to vtk.js.** The
  orientation triad only rendered correctly in one mode until its
  position/scale/rotation were baked into the geometry with
  `vtkTransformPolyDataFilter`.
- **Do not colour by vector mode on a lookup table.** Same reason. `pipeline.py`
  bakes a derived scalar array (`FoamVizColor`) instead, so both render modes
  agree and the data range is trivially correct.
- **Time stepping**: pull with `reader.UpdateTimeStep(t)` and hand downstream
  filters the result via `SetInputData`. A connected pipeline re-negotiates the
  time request on every `Update()` and silently falls back to `t=0`.
- `vtkOpenFOAMReader` needs an empty `*.foam` marker file in the case dir
  (`ensure_foam_stub` creates it). It advertises `patch/*` and `group/*` arrays;
  groups duplicate their member patches, so only `patch/*` is exposed. It also
  advertises fields such as `T.orig` that never appear in the output — read the
  field list from the actual output arrays, not from the reader.

## Trame traps hit here

- **`VBtnToggle(...).add_children([VBtn(...), ...])` renders the buttons twice.**
  A widget constructed while another element is the active parent attaches
  there too. Build children inside `with toggle:`.
- **Vue template expressions cannot see `document`, `window`, or the
  surrounding component's `$refs`.** Unknown identifiers resolve to `undefined`,
  so failures look like `Cannot read properties of undefined`. The PNG download
  therefore uses a real aiohttp route registered through
  `ctrl.on_server_bind` — see `_add_http_routes`. Do not "fix" it back into a
  `data:` URI: Chromium refuses a scripted click on a multi-megabyte data URL.
- **An aiohttp `@web.middleware`'s second parameter must be named `handler`.**
  aiohttp calls middlewares as `partial(mw, handler=next)` — by keyword — so any
  other name (e.g. `next_handler`) raises `got an unexpected keyword argument
  'handler'` on *every* request and 500s the whole app. Bit the `?case=`
  preselect middleware in `_add_http_routes`.
- `html.A` silently drops a `ref=` kwarg.
- `trame-vtk`'s client POSTs `/paraview/` on startup and gets a harmless 405.
  Expected; filtered in `tests/browser_check.py`.

## Testing

- `tests/test_pipeline.py` — 29 checks, no browser, ~15 s. Asserts **output
  counts** for every filter, because an empty VTK filter raises nothing and
  renders as a plausible blank image.
- `tests/browser_check.py` — drives real Chromium through 9 steps, fails on any
  console error. Needs the `LD_LIBRARY_PATH` above.
- When checking whether the 3D view drew anything, screenshot the **page**, not
  the canvas: a WebGL canvas without `preserveDrawingBuffer` reads back blank
  after the frame is presented.
- `js-*` classes in `app.py` exist purely as test hooks; Vuetify's own markup
  has nothing stable to select on, and `get_by_label("Field")` also matches
  "Vector field".

## Demo data

`data/hotRoom` = OpenFOAM 13 `fluid/hotRoomBoussinesqSteady`, copied from
`$FOAM_TUTORIALS/fluid/hotRoomBoussinesqSteady`, with two edits:

- `system/blockMeshDict`: `hex (...) (20 10 20)` → `(40 20 40)` (32 000 cells)
- `system/controlDict`: `writeFormat ascii` → `binary`

10 × 5 × 10 m room, 1 m² of floor held at 600 K, everything else 300 K.
Converges at iteration 1730 and writes 19 time directories. Regenerate:

```bash
source /opt/cfd/OpenFOAM-13/etc/bashrc
cd data/hotRoom && ./Allclean && ./Allrun
```

Chosen as the nearest tutorial analogue to an IDA ICE `HEATING` case:
buoyancy-driven room airflow with a thermal plume, steady state.

## Architecture in one paragraph

`case.py` wraps `vtkOpenFOAMReader` and hands out **snapshots** — concrete
datasets for one instant — rather than a live pipeline connection.
`pipeline.py` owns the renderer and every representation, and bakes the
selected field/component into a real scalar array (`FoamVizColor`) that
everything colours by. `app.py` is the Trame UI: state dict, change handlers,
one `update_scene()` that pushes all state into the pipeline and redraws.
`colors.py` samples matplotlib colour maps once and serves both the VTK
transfer function and the HTML legend gradient, so they cannot drift apart.
The cut plane is the hub — the slice, the stream-tracer seeds and the glyph
seeds all derive from it.

## Backend integration

### Decided (2026-08-11, with Niklas)

Integrating into the EQUA CFD frontend (repo `cfd-restful-backend`, the
`cfd-backend`/`cfd-file-server`/`cfd-frontend` images):

- **Shape:** FoamViz becomes a **4th service, `cfd-viz`**, behind the nginx
  `cfd-frontend`, which proxies `/viz/*` (HTTP **and** WebSocket upgrade) to it.
  It reads cases straight off the shared `CFD_HOME` volume — no OpenFOAM install
  needed, `vtkOpenFOAMReader` reads the case files directly. React embeds it as
  a **full-page** view via `<iframe src="/viz/?case=<id>">`.
- **Process model:** **shared single session** (few users) for now — one
  `main.py --server --data $CFD_HOME` process. Per-session launcher is the later
  productisation, not now.
- **A1 done:** `?case=<name>` deep link — `_preselect()` + an aiohttp request
  middleware in `_add_http_routes` (server-side; window.location is unreachable
  from Vue expressions). Verify with the screenshot filename, see below.
- **A3 done:** service robustness. `main.py --server` no longer exits on an
  empty `--data` (interactive use still does); the app stores `case_root`,
  clears `_loading` on an empty start, and `_preselect()` re-scans the case root
  (`_rescan_cases()`, also refreshing the drawer) when the name is unknown — so
  cases created after startup resolve. Note: only lazy rescan on deep link; the
  drawer does not auto-poll for new cases.
- **A2 pending (needs a live proxy, likely Niklas's env):** serving under the
  `/viz/` base path behind nginx — the wslink client must open its WebSocket and
  load assets relative to the mount.

**Verifying A1 without a browser:** the PNG route names its file
`foamviz-<case_name>-t<time>.png`. So: start `main.py --server --data data`,
`GET /?case=s2`, then `GET /foamviz/screenshot.png` and read the
`Content-Disposition` filename — it should contain `s2`.

### Remaining open questions

Related memories: `project_foamviz`, `project_openfoam_api` (Flask job-control
API on :5001) and `project_iceopenfoam` (EQUA's OpenFOAM-13 extension libs).

1. **Which backend, exactly?** The Flask job-control API, ICEOpenFOAM, or the
   IDA ICE client itself? That decides whether FoamViz is a service the API
   proxies to, or a component the client embeds.
2. **Process model.** A Trame server is one long-lived process holding one VTK
   pipeline and one camera — inherently single-user. Concurrency needs the
   trame launcher (process per session). Deciding this late is painful.
3. **Case discovery.** `find_cases()` scans for `system/controlDict`. The
   backend addresses cases by UUID directory with a `metadata.json`
   (`CASE-ID`, `N-CELLS`, `TURB-MODEL`, `ZONE-NAMES`, `END-ITER`, `CFD-OK`…)
   and a `building.opf` (`GLOBAL`/`MESH`/`SOLVER`/`GEOMETRY` sections). A real
   integration reads those instead — `metadata.json` alone gives the case list,
   cell count and readiness without touching the mesh.
4. **Zones — not a gap. Do not "fix" this.** An IDA ICE *zone* and an OpenFOAM
   *cellZone* are unrelated concepts that share a name. IDA ICE zones are rooms
   in the building selected for CFD analysis; they appear as `ZONES` under
   `GLOBAL` in `building.opf` and as `ZONE-NAMES` in `metadata.json`. They are
   an input-side grouping, not a mesh partition.
   Per Niklas (2026-08-11): **treat cases as single-region, single-zone.**
   The reader's `SetReadZones(1)` / `SetCopyDataToCellZones(1)` exist and work,
   but they address OpenFOAM `cellZones`, which is a different question and not
   one that is being asked. An earlier version of these notes had this wrong and
   called it the top integration gap; it is not.
5. **Decomposed cases — done & VERIFIED.** `case.py` picks `vtkPOpenFOAMReader`
   in `DECOMPOSED_CASE` mode when `_is_decomposed()` finds the newest time only
   in `processor*` (mirrors backend `time_in == 'parallel'`; detected from the
   filesystem, not the API, to keep cfd-viz decoupled). It is a
   `vtkOpenFOAMReader` subclass, so the rest is unchanged. Verified with the
   persistent venv on a real decomposePar'd hotRoom (root time 0, processor0 at
   1730): detection True, `vtkPOpenFOAMReader` present in the wheel, reads all
   processor dirs serially → 32 000 global cells, all fields. Drawer caption
   shows "· decomposed" when active.
6. **Render mode default.** Settled enough for now: Niklas reports client-side
   (vtk.js) rendering is "impressive already" at 12 M cells, so default to
   `local` and treat server mode as the fallback for GPU-less clients rather
   than the other way round.
7. **Comfort metrics.** Draught rate, PMV/PPD, operative temperature are what
   the IDA ICE side actually reports, and none are OpenFOAM fields. They would
   be derived arrays computed at load — the same mechanism as `FoamVizColor`,
   so the hook already exists (`pipeline.apply_color_array`).

## Slider debounce — DONE (2026-08-14)

The heavy sliders no longer re-render on every drag tick. `_slider(debounce=True)`
binds the thumb (and its live label) to a `<name>_draft` mirror and commits the
real state var only on release, via the VSlider `@end` event
(`end="<name> = <name>_draft"` — client-side JS, one flush). The real-var change
then runs the heavy handler once, behind the busy overlay. Debounced:
`plane_position`, `contour_count`, `stream_seeds`, `stream_length`, `glyph_count`
(listed in `_DEBOUNCED`). Cheap render-only sliders (opacity, tube width, glyph
size) stay live. `_sync_drafts()` (called from `load_case`) re-mirrors the drafts
so a slider follows programmatic changes instead of snapping back to a stale
drag value; it's the hook for the reset the To-do list will add.

Note on trame 3.2.5: `VSlider._event_names` is empty at class level — events are
resolved per instance, so `end=`/`start=` bind fine (verified: the template
emits `@end`), the earlier `_event_names` claim was wrong.

The **time** slider is deliberately left live: playback steps it programmatically
and `tests/browser_check.py` step 7 drives it with keyboard arrows expecting a
live label. A mouse-drag of it on a big case would still flood — revisit if it
bites (it would need the same draft treatment plus a per-step draft resync in the
play loop, and a client-side label mapping index→time).

Alternatives rejected during the original build: server-side throttle/debounce
(still pays the round trip, feels laggy not stepped) and lowering default counts
(treats the symptom).

## To-do list

Things for future consideration and work, added by Niklas. Remove items when
implemented, and feel free to fix formatting. We will fix and remove items as we
go, and Niklas may add more. Read the whole list before starting — the ordering
does not necessarily reflect a good implementation order.

### Widget re-arrangement — DONE 2026-08-15

All of the below shipped (see "Widget re-arrangement — DONE" implementation note):

- ~~Move the viewport buttons (X / Y / Z / Iso) and the time control to a bottom
  bar.~~ Floating bottom bar over the 3D view (`_bottom_bar`).
- ~~Add widget-type buttons (slice, isosurfaces, streamlines, boundary) to the top
  bar, revealing that widget's submenu in the side bar.~~ Top-bar tool selector
  (`active_tool`), sections shown via `v-show`.
    - ~~Merge "Room shell" and "Boundary patches" → "Boundary".~~
    - ~~Merge "Cut plane" and "Slice" → "Cut plane".~~
- ~~Colour settings stay permanently in the side bar, submenus below them.~~
    - ~~Integer input for number of colours ("banded" colouring).~~ `n_colors`
      (0 = smooth), banded via flat transfer-function nodes; legend bands too.

### Slider behaviour

- ~~Delay slider actions until the slider is released.~~ **Done 2026-08-14** for
  the heavy geometry sliders (see "Slider debounce — DONE" above).
    - ~~Draw a plane outline that follows the slider during the drag.~~ **Done
      2026-08-15** — amber plane frame (`plane_outline`), moved live on the draft
      by `_on_plane_preview` without recutting; also marks the seeding plane from
      any tool.
    - ~~Numeric input for the plane position in world coordinates.~~ **Done
      2026-08-15** — the "&lt;axis&gt; coordinate [m]" field (`plane_coord`), kept
      in step with the slider both ways.
    - (Optional, not done) debounce the time slider too — see the note in the
      "Slider debounce — DONE" section.

### Visualisation options

- ~~Boundary visualisation: default to "cull front face", with a toggle.~~
  **Done 2026-08-14** — "Cull near walls" switch in the Room shell panel,
  default on (`surface_cull` → `SetFrontfaceCulling`).
- ~~Toggle between point-interpolated values and true cell values.~~
  **Done 2026-08-14** — "True cell values" switch in the Colour panel
  (`use_cell_data`); bakes a cell `FoamVizColor` and switches the surface/slice
  mapper association. Contour/streamlines/glyphs stay on point data.
- ~~Slice-plane visualisation: when the mesh is shown, switch to a "crinkle
  slice".~~ **Done 2026-08-15** — the "Mesh (crinkle)" switch on the Cut plane
  tool feeds the slice from `vtk3DLinearGridCrinkleExtractor` instead of the
  cutter (see the implementation note below).

## To-do — implementation notes (Claude)

Grounding notes for the list above; **not yet implemented**. Code pointers are to
the tree as it stands (line numbers drift). A suggested order is at the end.

### Slider behaviour — do this first

Already scoped under "Known work, deferred" above (the `VSlider` `start`/`end`
draft-variable approach — no server round trip during the drag, one
`update_scene()` on release). Worth doing first: it's the biggest felt win, and
it makes the **new busy overlay** pleasant on heavy sliders — otherwise a drag
flashes the overlay every tick, since `plane_position`/`contour_count`/
`stream_seeds`/`glyph_count` are in the "heavy" handler group now. `_slider()` in
`app.py` is shared by every panel, so add a `debounce=True` parameter to it
rather than editing each slider; keep the cheap sliders live.

- ~~**Plane outline following the drag**~~ — **Done 2026-08-15.** `plane_outline`
  (four points + a line loop, amber, `LightingOff`, `UseBounds(False)` so it
  never skews `ResetCamera` — degenerate at the origin until first positioned).
  `update_plane_outline(axis, fraction)` moves the four corners; `_on_plane_preview`
  (a `@change` on `plane_position_draft`) calls it + `ctrl.view_update()` every
  drag tick — cheap because the rendered scene is only 2-D surfaces (the 12 M
  volume is filter *input*, never drawn), so `view_update` sends a tiny delta.
  Left always-on: it also marks the seeding plane while on the Streamlines/Arrows
  tools, which softens the cut-plane-hub issue.
- ~~**Numeric world-coordinate input**~~ — **Done 2026-08-15.** `plane_coord`
  field, label bound to the axis (`"<AXIS> coordinate [m]"`). `plane_position`
  stays the single source of truth (0..1); `_coord_from_fraction` /
  `_fraction_from_coord` convert against `case.bounds()`. `_on_plane_coord` writes
  `plane_position` from a typed coord (heavy commit via `_on_heavy`);
  `_on_plane_sync` (`@change` on `plane_position`/`plane_axis`) writes the coord
  and the slider draft back. The loop is broken by a value comparison
  (`abs(frac - plane_position) > 1e-4`), not a flag — trame flushes `@change`
  asynchronously, so a `self._syncing` guard set-then-cleared in one call would
  not hold. Kept axis-aligned; a free plane (arbitrary normal) would be a much
  bigger change and isn't what was asked.

### Widget re-arrangement — DONE 2026-08-15

- **`TOOLS` is the single source** for the five tools (key, title, icon). It
  drives both the top-bar `VBtnToggle` (bound to `active_tool`) and the drawer
  sections, so they can't drift. Each tool has a `_tool_<key>(title, icon)`
  builder; `_drawer()` loops `TOOLS` and wraps each in a `v-show` div.
- **Tools are control-only, not visibility.** Selecting a tool only changes which
  controls the drawer shows (`v-show`, so panels stay mounted and keep their
  state). What's drawn is still each representation's own "Show …" switch, so a
  slice + streamlines + isosurfaces can all be visible while you tweak just one.
  This is the key idea that made the refactor clean — no per-tool render logic.
- **Cut-plane-hub question, resolved:** the plane controls live in the "Cut
  plane" tool (merged with the slice, per spec), and the "Slice, stream seeds and
  arrows all sit on this plane" caption stays. Moving the seeding plane while
  configuring streamlines/arrows means a hop to the Cut plane tool — acceptable,
  and it kept the layout duplication-free. If that hop proves annoying, the tidy
  fix is to promote the plane block to a second persistent section (like Colour),
  *not* to repeat the control in three tabs.
- **Bottom bar** is a floating strip inside `.foamviz-stage` (`_bottom_bar`),
  matching the legend/mode overlays — not the Vuetify `footer` (which carries the
  "Powered by trame" branding, kept). Camera presets left, time group right. The
  `js-time-slider` / `js-time-label` / `js-refresh-times` test hooks moved with
  it; the time slider stays live (not debounced) so playback and the keyboard
  browser-test step still work.
- **Sections:** `_panel()` (expansion panel) was replaced by `_section(title,
  icon)` — a plain header + body div — since only one tool shows at a time. The
  browser test's `panel()` accordion helper became `tool()` (clicks the top-bar
  button).
- **Banded colouring:** `n_colors` state (0 = smooth). `colors.color_transfer_function`
  bakes banding into the CTF *nodes* as flat plateaus (two coincident-value-safe
  nodes per band) — NOT `vtkDiscretizableColorTransferFunction`, whose `DeepCopy`
  (used by `set_color_range` on every range change) drops the discretize flag
  (verified). `css_gradient` gained a matching stepped branch, so the legend
  bands too.

### Visualisation options

- ~~**Cull front face**~~ — **Done.** `surface_cull` (default True) →
  `surface_actor.GetProperty().SetFrontfaceCulling`, in the cheap/live handler.
- ~~**Point-interpolated vs true cell values**~~ — **Done.** `use_cell_data`
  (cheap handler). `apply_color_array` bakes `FoamVizColor` into point data
  always, and into cell data too when the toggle is on; `_color_by_association`
  switches the surface/slice mapper between `UsePointFieldData` and
  `UseCellFieldData`. Cell arrays were verified present on the internal mesh,
  boundary patches, and the cutter output. Contour/streamlines/glyphs keep
  reading the point array. (Note: auto-range still samples point data — a cell
  extreme can slightly exceed it; not worth special-casing.)
- ~~**Crinkle slice**~~ — **Done 2026-08-15.** Not home-brewn: VTK ships the
  purpose-built `vtk3DLinearGridCrinkleExtractor` (threaded, meant for large
  grids — ~1 ms on the demo, the right choice for the 12 M case). It shares the
  cutter's `vtkPlane`, so the position slider drives both; a `vtkGeometryFilter`
  turns its unstructured output into polydata for the shared slice mapper, and
  `update_slice` swaps the mapper's input to it when `slice_edges` is on. It
  needs a 3D *linear* grid — fine, the reader decomposes polyhedra by default
  (demo mesh is all hexes). `slice_edges` moved to the heavy/overlay handler
  since it now does real extraction. (`vtkExtractGeometry` +
  `ExtractOnlyBoundaryCells` was the general-cell-type fallback considered — not
  needed here, and slower.)

### Suggested order

1. ~~**Slider debounce**~~ — **done** (core; plane outline + numeric XYZ remain).
2. ~~**Cull-front-face** and **point/cell toggle**~~ — **done 2026-08-14**.
3. ~~**Widget re-arrangement** (+ banded colouring)~~ — **done 2026-08-15**.
4. ~~**Crinkle slice**~~ — **done 2026-08-15**.
5. ~~Slider refinements: plane outline during drag, numeric XYZ plane position.~~
   — **done 2026-08-15**.

**The backlog is now empty** (bar the optional time-slider debounce). All of it
is server-side verified but awaits Niklas's browser confirmation.
 
