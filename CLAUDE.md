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

### Widget re-arrangement

- Move the viewport buttons (X / Y / Z / Iso) and the time control to a bottom
  bar.
- Add widget-type buttons (slice, isosurfaces, streamlines, boundary) to the top
  bar. Clicking one reveals that widget's submenu — currently a side-bar
  dropdown — in the side bar. This should give a clearer interface.
    - Merge the current "Room shell" and "Boundary patches" under one top-bar
      button, "Boundary".
    - Merge the current "Cut plane" and "Slice" content under one top-bar button,
      "Cut plane".
- The colour settings (field selector, colour map, etc.) stay permanently in the
  side bar, so the submenus above appear below the ever-present colour settings.
  (The auto-range and 1–99 % toggle are great — keep them!)
    - Also add an integer input for the number of colours to show ("banded"
      colouring).

### Slider behaviour

- ~~Delay slider actions until the slider is released.~~ **Done 2026-08-14** for
  the heavy geometry sliders (see "Slider debounce — DONE" above). Remaining
  refinements:
    - Draw a plane outline that follows the slider during the drag (live preview
      without a recut).
    - Add a numeric input for the slider position, in actual plane-point
      coordinates (X Y Z).
    - (Optional) debounce the time slider too — see the note in that section.

### Visualisation options

- ~~Boundary visualisation: default to "cull front face", with a toggle.~~
  **Done 2026-08-14** — "Cull near walls" switch in the Room shell panel,
  default on (`surface_cull` → `SetFrontfaceCulling`).
- ~~Toggle between point-interpolated values and true cell values.~~
  **Done 2026-08-14** — "True cell values" switch in the Colour panel
  (`use_cell_data`); bakes a cell `FoamVizColor` and switches the surface/slice
  mapper association. Contour/streamlines/glyphs stay on point data.
- Slice-plane visualisation: when the mesh is shown, switch to a "crinkle slice"
  — show the whole layer of intersected cells with the mesh, i.e. the true mesh,
  not a triangulated slice.

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

- **Plane outline following the drag:** draw a cheap outline actor — just the
  plane rectangle at the *draft* position, no cutter, no recut — updated live on
  the draft var, while the real slice/streamlines/glyphs recompute only on
  release. An outline is cheap enough to stay live even in server render mode.
- **Numeric XYZ input:** `plane_position` is currently normalised 0..1 along
  `plane_axis`. Exposing world coordinates means converting to/from the mesh
  bounds. Keep the plane axis-aligned for now and show the world coordinate along
  the active axis (metres); a fully free plane (arbitrary normal) is a much
  bigger change and not what's asked. The numeric field binds the real var and
  commits once, exactly like the slider release.

### Widget re-arrangement

- The layout (`SinglePageWithDrawerLayout`) exposes a `footer` slot (verified) —
  use it for the bottom bar: move the camera buttons (`VIEW_BUTTONS`) and the
  whole time group out of `_toolbar()` into `with self.ui.footer:`.
- Top-bar tool buttons: a `VBtnToggle` bound to a new `active_tool` state var
  (e.g. `'boundary' | 'cutplane' | 'contour' | 'stream' | 'glyph'`). The drawer
  then becomes: `_panel_colour()` always shown, then the active tool's block via
  `v_show`/`v_if` on `active_tool` — most likely replacing the current
  always-open `VExpansionPanels` accordion in `_drawer()`.
- Merges: "Boundary" = `_panel_surface()` + `_panel_patches()`; "Cut plane" =
  `_panel_plane()` + `_panel_slice()`.
- **Watch out — the cut plane is the seeding hub.** The slice, streamline seeds
  *and* glyph seeds all derive from `plane_axis`/`plane_position` (see the
  architecture paragraph). If the tools are mutually exclusive tabs, the plane
  controls must stay reachable while on the Streamlines/Glyphs tool — either keep
  axis/position in a shared, always-visible spot, or repeat them in those tools.
  Decide this before starting the refactor; it shapes the whole layout.
- **Banded colouring:** add an `n_colors` state var (blank/0 = continuous).
  `colors.py` feeds *both* the VTK transfer function and the HTML legend gradient
  from the same `_samples()`, so band there (a discrete `vtkLookupTable` with
  `SetNumberOfColors(n)` over the range, plus a stepped `css_gradient`) and the
  legend bands to match for free. Small and self-contained — can piggyback on
  this pass.

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
- **Crinkle slice:** the biggest lift. The slice is a `vtkCutter` (triangulated
  planar cut); a crinkle slice keeps whole cells the plane passes through.
  Implement by evaluating the plane's signed distance at each cell's points and
  extracting the cells with a sign change (straddling), e.g. via
  `vtkExtractGeometry` with the plane implicit function, then draw with edges.
  Gate it on "mesh shown" (`slice_edges`). Standalone; do it last.

### Suggested order

1. ~~**Slider debounce**~~ — **done** (core; plane outline + numeric XYZ remain).
2. ~~**Cull-front-face** and **point/cell toggle**~~ — **done 2026-08-14**.
3. **Widget re-arrangement** (+ banded colouring) — one focused UI pass; settle
   the cut-plane-hub question first. **← next**
4. **Crinkle slice** — self-contained but the largest single feature.
 
