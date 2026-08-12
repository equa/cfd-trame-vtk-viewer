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

- venv at `.venv`. Python 3.12, Ubuntu 24.04 container, **no root, no sudo**.
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
5. **Decomposed cases — addressed (UNTESTED).** `case.py` now picks
   `vtkPOpenFOAMReader` in `DECOMPOSED_CASE` mode when `_is_decomposed()` finds
   the newest time only in `processor*` (mirrors backend `time_in == 'parallel'`;
   detected from the filesystem, not the API, to keep cfd-viz decoupled).
   `vtkPOpenFOAMReader` is a `vtkOpenFOAMReader` subclass, so the rest is
   unchanged. **Verify in a real env:** that `vtk.vtkPOpenFOAMReader` exists in
   the wheel and reads decomposed dirs serially (no MPI controller); the drawer
   caption shows "· decomposed" when active. Alternative signal if wanted:
   GET the backend `/api/caseinfo` `time_in`/`n_procs`.
6. **Render mode default.** Settled enough for now: Niklas reports client-side
   (vtk.js) rendering is "impressive already" at 12 M cells, so default to
   `local` and treat server mode as the fallback for GPU-less clients rather
   than the other way round.
7. **Comfort metrics.** Draught rate, PMV/PPD, operative temperature are what
   the IDA ICE side actually reports, and none are OpenFOAM fields. They would
   be derived arrays computed at load — the same mechanism as `FoamVizColor`,
   so the hook already exists (`pipeline.apply_color_array`).

## Known work, deferred

### Sliders re-render live — debounce them

Requested by Niklas 2026-08-11, explicitly **for later**. Do not start it
without asking; it is the one agreed defect, not a discovered one.

Every slider binds `v_model` straight to the state variable, so dragging fires
a state flush per tick and each flush runs the whole of `update_scene()`. At
32 000 cells that is invisible; at 12 M it is not. The expensive ones are
`plane_position` (recuts the slice, then reseeds streamlines *and* glyphs),
`stream_seeds`, `glyph_count` and `contour_count`. Opacity and tube width are
cheap and can stay live.

The clean fix keeps the drag entirely client-side. Vuetify's `VSlider` emits
`start` and `end` (both confirmed present in `trame_vuetify`'s
`_event_names`), so bind the slider to a draft variable and copy it into the
real one on release:

```python
v3.VSlider(
    v_model=("plane_position_draft", 0.5),
    end="plane_position = plane_position_draft",   # JS: one flush, on release
    ...
)
html.Span("{{ plane_position_draft }}")            # label still tracks the drag
```

No server round trip while dragging, one `update_scene()` at the end. Watch
two things: the draft must be re-synced if the real variable changes from
elsewhere (case load, reset), and `_slider()` in `app.py` is shared by every
panel, so it needs a flag rather than a blanket change — the cheap sliders are
nicer live.

Alternatives considered and rejected during the build: server-side
throttle/debounce (still pays the round trip and makes the UI feel laggy rather
than stepped), and simply lowering default counts (treats the symptom).
