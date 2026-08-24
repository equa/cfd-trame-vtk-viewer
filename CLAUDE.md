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

- **Default lighting is a single camera headlight** → faces angled away go
  black (the boundary shell especially). Fixed with a `vtkLightKit` (key + fill
  + back + head, like ParaView) + `TwoSidedLightingOn` on the renderer, plus an
  **ambient floor** on the lit actors. Controls live in a **collapsible
  "Lighting" panel** at the bottom of the side pane (hidden by default): a **Light kit**
  toggle (default on; off → `RemoveAllLights` → VTK's default headlight) and
  Ambient/Diffuse sliders (default 0.3/0.7) → `set_light_kit` / `set_lighting`.
  The ambient floor is the part that reliably survives to the **vtk.js client**
  (local mode does its own lighting; server-side lights may not serialise) — it
  guarantees no face is pure black in either mode, and it lights the
  server-rendered report PNGs. The slice stays `LightingOff` (flat) and is left
  out. Lighting prefs are **persisted globally** to a JSON settings file
  (`_save_settings`/`_load_settings`, default `<case_root>/.foamviz-settings.json`
  — persistent on the CFD_HOME volume — override with `$FOAMVIZ_SETTINGS`), so
  they survive a server restart. (The shared-session server already keeps state
  across page reloads; the file adds cross-restart survival. Not cookies —
  global, server-side, and verifiable.)
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

- **In local (vtk.js) mode the client owns its camera.** A camera set
  server-side (`renderer` camera + `ResetCamera`) is invisible until pushed:
  `view.push_camera()` (wired as `ctrl.view_push_camera`). `view.reset_camera()`
  only *refits* the client's own orientation, so calling it after a preset
  clobbered the orientation — which is why the X/Y/Z/Iso view buttons "never
  worked". `set_view` and the initial `reset_camera=True` load now push instead.
  - **Corollary — never `push_remote_camera_on_end_interaction()` in local
    mode.** That observer fires on every EndInteraction (mouse up / leave) and
    `setCamera()`s the server camera onto the client, which re-applies the focal
    point and **resets the client's centre of rotation** — orbiting felt broken
    and needed constant R. Removed 2026-08-18. Pushing a camera to the client is
    fine on a *deliberate* action (a view button); doing it on every mouse-up is
    not. The server camera already tracks the client in local mode, so it gained
    nothing.
  - **Turntable rotation is NOT available with trame-vtk 2.11.15 (local mode).**
    Rotation is a client (vtk.js) interactor setting, not a server
    `vtkInteractorStyle` (which does nothing in local mode). vtk.js's rotate
    manipulator *does* support turntable via `useWorldUpVec`/`worldUpVec`, but:
    (1) trame's `interactor_settings` applier (client `Md()`) forwards only
    `button/shift/control/alt/scrollEnabled/dragEnabled` and **drops** those
    keys; (2) the interactor/style helper is closure-captured on the client (no
    `expose()`, not global) so it can't be patched from injected JS or `js_call`.
    Tried the reactive-prop path (a `turntable` toggle) — the toggle changed the
    prop but the flag never reached the manipulator, so it did nothing; reverted
    2026-08-21. Re-enable once a trame-vtk forwards manipulator props: bind the
    local view's `interactor_settings` to a state var whose Rotate entry carries
    `useWorldUpVec: True, worldUpVec: [0,0,1]`.
- **Keyboard shortcuts are extensible via `KEY_SHORTCUTS`** (`app.py`): a pressed
  `event.key` → a CSS selector, and one injected `window` keydown listener
  (`client.Script`, `_KEY_JS_TEMPLATE`) clicks the matched element. So a shortcut
  rides an existing button's own click handler — no JS↔Python bridge. To add one:
  give the target element a `js-*` class and add a row. Shift makes an uppercase
  key (shift+x → `-x`). vtk.js already binds `r` to reset the camera.
  (`client.Script` renders as `<trame-script :script="trame__inline_script_N">`;
  the JS lives in that state var and runs client-side, like `client.Style`.)
- **`F` sets the centre of rotation from the point under the cursor**
  (ParaView-style focus). Unlike the axis shortcuts it needs the pointer
  position *and* a server round-trip, so it cannot ride the click-a-button
  bridge. `_FOCUS_JS` tracks the cursor and, on `F` over the 3D `<canvas>`,
  calls `window.trame.trigger('foamviz_pick_cor', [x, y, w, h])` — trame's own
  client→server call (`window.trame` exposes `.trigger(name, args, kwargs)`,
  the general JS→Python path when there's no button to click). The trigger is
  registered imperatively (`self.server.trigger(name)(fn)`; there is no
  `@controller.trigger` decorator). Server side, `pipeline.pick_cor` sizes the
  offscreen window to the client canvas so the projection aspect matches, casts
  a `vtkCellPicker`, and — since vtk.js orbits the *focal point* (no separate
  COR) — sets focal point to the pick and slides the camera along its view
  direction so the point lands at screen centre (view direction + distance
  preserved: no tilt, no zoom, just a pan-to-centre). Then `view_push_camera` +
  `view_update`. A miss (empty space) is a silent no-op.
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
Converges at iteration 1730 and writes 19 time directories.

`constant/triSurface/building.obj` is a small **added fixture** (a box at the
room bounds), not part of the tutorial — it exercises the Geometry tool and its
pipeline test. `Allrun`/`Allclean` leave it alone. A real case ships its own
`building.obj` here. Regenerate the solution (not the OBJ):

```bash
source /opt/cfd/OpenFOAM-13/etc/bashrc
cd data/hotRoom && ./Allclean && ./Allrun
```

Chosen as the nearest tutorial analogue to an IDA ICE `HEATING` case:
buoyancy-driven room airflow with a thermal plume, steady state.

## Geometry tool + outline changes (2026-08-17)

- **Geometry tool** (6th tool): reads a building OBJ from `constant/triSurface/`
  via `vtkOBJReader` (lazily; `set_case` only sets the filename, `has_geometry`
  gates the UI). setupIceCase indexes geometry, so the file is `building.obj`
  **or** `building<N>.obj` (e.g. `building10.obj`) — `set_case` searches
  `building\d*\.obj`, first match (bare name first, then by index).
  - **ONE fixed actor/mapper fed by a single `vtkFeatureEdges`**, rendered as
    flat lines throughout. The mode is a **filter-parameter toggle**, not a
    scene/mapper mutation — so the output re-serialises to the vtk.js client
    cleanly, exactly like changing the contour count. This is the pattern that
    works; the ones that DIDN'T (each corrupted the client, learned the hard
    way): swapping the mapper's input (stale input → filled triangles), changing
    the actor's representation, and actor add/remove (re-added actors lost their
    properties → FE came back shaded, WF as surfaces).
  - **Feature edges** = FeatureEdges on, Manifold off (sharp + boundary — the
    architectural outline). **Wireframe** = FeatureEdges *off*, Manifold on
    (every edge). They are OPPOSITE toggles because `vtkFeatureEdges` quirk:
    feature+manifold *together* yields only the feature edges, but manifold
    *alone* yields all interior edges. Boundary stays on for both.
  - Footprint: only the current mode's edges exist — feature edges are small; the
    full all-edges set is built only when wireframe is actually chosen.
  State `geometry_visible/mode/opacity/line_width`; cheap handler. Only
  `building.obj` (or `building<N>.obj`) for now — more `triSurface` files later.
- **Red plane outline is now drag-only.** Hidden by default; the position
  slider's `start`/`end` show/hide it (`plane_drag_start` /
  `plane_slider_release` + `set_plane_outline_visible`); `_on_plane_slide` moves
  it during the drag. `update_scene` no longer positions it.
- **The always-on domain outline box (`vtkOutlineFilter`) was removed** — the
  building geometry is the context now.

## Light/dark theme (2026-08-17)

The embedding app (cfd-frontend) drives the theme via `?theme=light|dark` on the
iframe URL. FoamViz is a shared single session (UI built once), so the theme
switches **reactively**, not by rebuild:

- `ui_theme` state is bound to `<VApp :theme>` (the layout is built with
  `theme=("ui_theme",)`, which renders `:theme="ui_theme"` — verified), so the
  whole Vuetify chrome (drawer/toolbar/controls) re-themes at runtime.
- The floating overlays (legend, bottom bar, mode switch, section headers) are
  styled with Vuetify's theme CSS vars — `rgba(var(--v-theme-surface), …)` /
  `rgb(var(--v-theme-on-surface))` — so they follow the same switch with no
  per-theme CSS.
- The 3D viewport is VTK, not CSS: `pipeline.set_theme(light)` flips the
  renderer background and **inverts the neutral geometry line colour** (light
  lines on dark, dark on light — field-coloured actors need no change).
- The `?theme` middleware (beside `?case` in `_add_http_routes`) calls
  `_set_theme`, which sets `ui_theme` + calls `pipeline.set_theme` + re-renders.
  Default is dark.

## Architecture in one paragraph

`case.py` wraps `vtkOpenFOAMReader` and hands out **snapshots** — concrete
datasets for one instant — rather than a live pipeline connection.
`pipeline.py` owns the renderer and every representation, and bakes the
selected field/component into a real scalar array (`FoamVizColor`) that
everything colours by. `app.py` is the Trame UI: state dict, change handlers,
one `update_scene()` that pushes all state into the pipeline and redraws.
`colors.py` samples matplotlib colour maps once and serves both the VTK
transfer function and the HTML legend gradient, so they cannot drift apart.
The cut plane is the hub — the slice and the stream-tracer seeds derive from it
(the arrows have their own plane grid, see below).

## Fields, isosurfaces, arrows (reworked 2026-08-24)

- **Field loading is filtered at the reader** (`case.py`): `SKIP_FIELDS`
  (`p`, `alphat`, `omega`, `epsilon`, `rho`) are disabled after
  `EnableAllCellArrays()`, so they are never read or interpolated cell→point
  (memory saving; absent ones are ignored). **Temperature is converted K→°C once
  at read time** (`_to_celsius`, `KELVIN_FIELDS={"T"}`): replaces the `T` array
  with a converted copy (not in place — the reader's cache is shared by the
  shallow copies), so every downstream range/legend/contour value is already °C.
  `_FIELD_UNITS["T"]` is `[°C]`.
- **Isosurfaces**: count is 1/3/5 only (a slider stepping by 2 from 1; default
  1). `_contour_values()` builds the isovalues — the single `contour_value` for
  one surface, else interior fractions across `[contour_min, contour_max]`. All
  three seed from (track) the colour range in `_rescale`. `update_contour` takes
  an explicit values list and **always sets the isovalues** (even when the actor
  is hidden) so the "On isosurface" arrow source can read the contour output.
- **Arrows**: two seed sources. "On plane" lays a **regular grid over the cut
  plane** (`vtkPlaneSource` → `vtkProbeFilter` on the volume → `vtkThresholdPoints`
  on `vtkValidPointMask` to drop grid points outside the mesh) — evenly spaced,
  unlike mask-points on the cut faces which clump where the mesh is fine. "On
  isosurface" seeds off the contour output (mask-points). `update_glyphs` takes
  the plane axis+coord to size the grid.

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

## Case-report figures — in progress (2026-08-16)

Goal (with Niklas): build a scene, "Add to case report", and have it appear in
the cfd-frontend case report — a **frozen** interactive snapshot (rotate/zoom, no
toggles). The headline deliverable is a **single self-contained `.html` export**
of the report with the scenes inlined (opens offline, no running services); the
existing browser print-to-PDF stays (scenes show as their poster PNG there).

Format decided: **vtk.js scene** (`.vtkjs`), not glTF — it is the same vtk.js
renderer the client already uses, so the report looks identical to the live view
(glTF risks PBR-shading the flat CFD field colours). Storage: **inside the case**,
`<case>/report/` (Niklas confirmed cfd-viz has write access to `CFD_HOME`).

**DONE — capture (this repo).** The toolbar "Report" button → `add_to_report`
writes per figure into `<case>/report/`: `figure_NN.png` (poster/print) and
`figure_NN.json` (caption + field/component/range/preset/n_colors + gradient +
ticks, so the report redraws the colour bar from `colors.py` — the poster is the
3D view only, no legend). Caption comes from a state field or `_auto_caption()`.
The server render window's camera already tracks the client's orbit in local mode
(that's also how the Client→Server switch works), so the server-side screenshot
frames what the user set up. (Do NOT use `push_remote_camera_on_end_interaction()`
for this — see the camera trap under "Trame traps": it resets the client's centre
of rotation on every mouse-up. It was removed 2026-08-18.)

**vtk.js scene export is MOTHBALLED (2026-08-17):** `figure_NN.vtkjs`
(`vtkJSONSceneExporter` output, zipped) is no longer written — the interactive
report viewer is shelved, and unused scenes just pile up on disk. Gated behind
`EXPORT_VTKJS = False`; `write_vtkjs` and the zip code are kept, so it's a
one-line flip to re-enable once a viewer is built.

- **Trap:** `vtkJSONSceneExporter` leaves the render window in a state that
  **segfaults** a subsequent `vtkWindowToImageFilter`. So capture the PNG
  *before* the scene export, and `screenshot()` now calls `render_window.Render()`
  first (also needed because, with no live client driving it, the window may be
  unrendered during a headless export).

**TODO — the rest (cfd-backend repo).** (2) backend route to list/serve a case's
`report/` figures; (3) a Figures section in `frontend/src/pages/Report.jsx` (PNG
in print, interactive vtk.js viewer on screen); (4) the single-file HTML export —
assemble the React-rendered tables/charts + one inlined vtk.js viewer +
base64-embedded scenes into a downloadable `.html` (client-side assembly reuses
React's rendering; the viewer is one small vtk.js bundle inlined once).

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
    - ~~Draw a plane outline that follows the slider during the drag.~~ **Done** —
      the red plane frame (`plane_outline`), moved live on the slider by
      `_on_plane_slide` without recutting; also marks the seeding plane from any
      tool.
    - ~~Numeric input for the plane position in world coordinates.~~ **Done, then
      reworked 2026-08-16** into X/Y/Z world-point fields + an Apply button, with
      the world point (not a fraction) as the source of truth — see the plane
      note under implementation notes. Fields are inert until Apply; the slider
      previews live and auto-applies on release; a normal switch keeps X/Y/Z.
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
  tool feeds the slice from a crinkle extractor (`vtkExtractGeometry` since
  2026-08-17) instead of the cutter (see the implementation note below).

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

The cut-plane controls were then **reworked (2026-08-16)** into a single clean
model — the earlier fraction/`plane_coord` bidirectional sync was fiddly. Now:

- **Source of truth = the world point `plane_x`/`plane_y`/`plane_z` + the normal
  `plane_axis`.** Only the active-axis coordinate positions the cut (the plane is
  axis-aligned); the other two are remembered, so switching the normal keeps
  them. `_active_coord()` reads `plane_<axis>`; everything funnels through
  `update_scene()`, which passes it to `update_plane`/`update_plane_outline`
  (both now take a **world coordinate**, not a fraction — no fractions anywhere).
- **The X/Y/Z fields are inert** (no `@change`) until **Apply** (`plane_apply` →
  `_busy_call(_do_plane_apply)`), which clamps into range, reflects the value on
  the slider (`_sync_plane_ui`) and redraws. That is the "debounce" for typed
  input — keystrokes never redraw.
- **The slider is a view** on the active axis (`plane_slider`, ranged by
  `axis_min`/`axis_max`). Dragging fires `_on_plane_slide` → moves the red frame
  only (`ctrl.view_update`, cheap: only 2-D surfaces are ever drawn, the 12 M
  volume is filter *input*). Release (`@end` → `ctrl.plane_slider_release`)
  writes the active coordinate and auto-applies.
- **Axis switch** (`_on_plane_axis`) just calls `plane_apply`, which re-ranges the
  slider to the new axis and redraws — X/Y/Z untouched.
- **`plane_outline`**: four points + a line loop, **red** (legible on a future
  light theme), `LightingOff`, `UseBounds(False)` so its origin-anchored initial
  points can't skew `ResetCamera` (that was making the render flaky). Always-on,
  so it also marks the seeding plane from the Streamlines/Arrows tools.
- Kept axis-aligned; a free plane (arbitrary normal) would be a much bigger
  change and isn't what was asked. Fields take decimals (a slice needs sub-metre
  precision), not integers.

### Widget re-arrangement — DONE 2026-08-15

- **`TOOLS` is the single source** for the six tools (key, title, icon). It
  drives both the side-pane **tool stack** (`_tool_stack()`, bound to
  `active_tool`) and the settings sections, so they can't drift. Each tool has a
  `_tool_<key>(title, icon)` builder; `_drawer()` loops `TOOLS` and wraps each in
  a `v-show` div. `TOOL_VISIBLE` maps each tool key → its actor-visibility state
  var (the eye toggle).
- **Layout (rearranged 2026-08-21):** global **Colour** settings live in the
  **top bar** (essentials inline: Field/Component/Colour map/Bands/Rescale; the
  rest — range mode, min/max, cell values — behind an "Options" `VMenu` with
  `activator="parent"`). The **side pane** holds the vertical tool stack at top
  (each row: a tool button that selects its settings + an **eye toggle**,
  `mdi-eye`/`mdi-eye-off`, that flips the actor's `*_visible` var directly), the
  selected tool's settings below, then the collapsible Lighting panel.
- **Tools are control-only, not visibility.** Selecting a tool only changes which
  settings show (`v-show`, so panels stay mounted and keep their state).
  Visibility is the separate per-row eye toggle, so a slice + streamlines +
  isosurfaces can all be visible while you tweak just one. The eye sets the
  `*_visible` state var client-side; its `@change` handler (`_on_cheap` /
  `_on_heavy`) updates the actor — no per-tool render logic.
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
- ~~**Crinkle slice**~~ — **Done 2026-08-15; extractor swapped 2026-08-17.** The
  "Mesh (crinkle)" switch feeds the slice from a crinkle extractor sharing the
  cutter's `vtkPlane` (position slider drives both), via a `vtkGeometryFilter`
  → polydata for the shared slice mapper; `update_slice` swaps the mapper input
  when `slice_edges` is on (a heavy/overlay handler — real extraction).
  - **Now `vtkExtractGeometry` + `ExtractBoundaryCellsOn`/`ExtractOnlyBoundaryCellsOn`**
    (general, single-threaded, all cell types — ParaView-style).
  - **Was `vtk3DLinearGridCrinkleExtractor`** (threaded 3D-linear fast path). It
    **hung the trame server** — steady memory growth, no output, container
    restart needed — even on small cases, while computing in ~2 ms *headlessly*.
    Could not reproduce headlessly (so unconfirmed), but the threaded fast path
    (vtkSMPTools) inside the async server is the prime suspect, and it is
    linear-cells-only. The general filter removed the thread pool and gained
    all-cell-type robustness. If it *still* hangs deployed, instrument the
    `crinkle_surface.Update()` path (it runs during render, so no app-level log
    today) or consider disabling crinkle behind a flag.

### Suggested order

1. ~~**Slider debounce**~~ — **done** (core; plane outline + numeric XYZ remain).
2. ~~**Cull-front-face** and **point/cell toggle**~~ — **done 2026-08-14**.
3. ~~**Widget re-arrangement** (+ banded colouring)~~ — **done 2026-08-15**.
4. ~~**Crinkle slice**~~ — **done 2026-08-15**.
5. ~~Slider refinements: plane outline during drag, numeric XYZ plane position.~~
   — **done 2026-08-15**.

**The backlog is now empty** (bar the optional time-slider debounce). All of it
is server-side verified but awaits Niklas's browser confirmation.
 
