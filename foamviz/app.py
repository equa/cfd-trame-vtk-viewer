"""Trame front end: a browser UI over the OpenFOAM VTK pipeline.

Rendering runs in either of two modes, switchable at runtime:

``local``
    Geometry is serialised to the browser and drawn by vtk.js on the client's
    GPU. Camera interaction costs no round trips, which is what you want over a
    slow link -- at the price of shipping the geometry.
``remote``
    The server renders with OSMesa and streams JPEG frames. The client needs no
    GPU and the geometry never leaves the server, which matters for big cases.

Because the colour scalars are baked into a real array by the pipeline (see
:mod:`foamviz.pipeline`), both modes show the same picture.
"""

import asyncio
import logging
import math
import tempfile
from pathlib import Path

from trame.app import get_server
from trame.decorators import TrameApp, change, controller
from trame.ui.vuetify3 import SinglePageWithDrawerLayout
from trame.widgets import client, html
from trame.widgets import vtk as vtk_widgets
from trame.widgets import vuetify3 as v3

from . import colors
from .case import FoamCase, find_cases
from .pipeline import FoamPipeline

log = logging.getLogger("foamviz")

SHOT_ROUTE = "/foamviz/screenshot.png"

COMPONENTS = [
    {"title": "Magnitude", "value": "magnitude"},
    {"title": "X", "value": "x"},
    {"title": "Y", "value": "y"},
    {"title": "Z", "value": "z"},
]

VIEW_BUTTONS = [
    ("+X", "+x"),
    ("-X", "-x"),
    ("+Y", "+y"),
    ("-Y", "-y"),
    ("+Z", "+z"),
    ("-Z", "-z"),
    ("Iso", "iso"),
]

# The drawer's widget tools. One entry drives both the top-bar selector button
# and the matching drawer section (built by ``_tool_<key>``), so the two can't
# drift. Colouring is not here — it stays permanently visible above the tools.
TOOLS = [
    ("cutplane", "Cut plane", "mdi-square-outline"),
    ("boundary", "Boundary", "mdi-cube-outline"),
    ("contour", "Isosurfaces", "mdi-blur"),
    ("stream", "Streamlines", "mdi-vector-polyline"),
    ("glyph", "Arrows", "mdi-arrow-top-right"),
]


@TrameApp()
class FoamViz:
    def __init__(self, case_root, server=None):
        self.server = get_server(server, client_type="vue3")
        self.pipeline = FoamPipeline()
        self.case = None
        self.case_root = case_root  # kept so cases can be re-scanned on demand
        self._player_task = None
        self._busy_count = 0  # >0 while a heavy scene update is running
        self.ctrl.on_server_bind.add(self._add_http_routes)

        # Guards the change handlers while we set many state variables at once
        # during case loading, so the scene is rebuilt once at the end.
        self._loading = True

        self.case_paths = {p.name: str(p) for p in find_cases(case_root)}
        self._init_state()
        self._build_ui()

        if self.case_paths:
            self.load_case(next(iter(self.case_paths)))
        else:
            # Empty case root — e.g. a service started before any case exists.
            # Serve anyway and pick cases up on demand (rescan on deep link).
            # Must clear _loading here, or every change handler stays guarded out.
            self._loading = False

    # ------------------------------------------------------------------ state

    @property
    def state(self):
        return self.server.state

    @property
    def ctrl(self):
        return self.server.controller

    def _init_state(self):
        self.state.trame__title = "FoamViz"
        self.state.update(
            {
                # case / time
                "case_items": sorted(self.case_paths),
                "case_name": next(iter(sorted(self.case_paths)), None),
                "time_index": 0,
                "time_values": [0.0],
                "time_label": "0",
                "n_times": 1,
                "playing": False,
                "case_info": "",
                "busy": False,
                # which widget tool's controls the drawer is showing (see TOOLS)
                "active_tool": "cutplane",
                # colouring
                "field_items": [],
                "color_field": None,
                "color_component": "magnitude",
                "component_enabled": False,
                # Colour by true (flat) cell values instead of point-interpolated.
                "use_cell_data": False,
                "preset": "coolwarm",
                "preset_items": colors.preset_items(),
                "legend_gradient": colors.css_gradient("coolwarm"),
                "legend_ticks": [],
                "legend_title": "",
                # 0 = smooth colour map; >0 bands it into that many colours.
                "n_colors": 0,
                "auto_range": True,
                # Off by default. It is the right tool when a tiny extreme
                # region flattens the map, but it saturates everything above the
                # 99th percentile -- misleading unless you asked for it.
                "robust_range": False,
                "range_min": 0.0,
                "range_max": 1.0,
                # cut plane
                "plane_axis": "z",
                "plane_position": 0.5,
                # world coordinate of the plane along the active axis (metres);
                # a second way to place the slice, kept in step with the slider.
                "plane_coord": 0.0,
                # representations
                "surface_visible": True,
                "surface_colored": False,
                "surface_opacity": 0.12,
                "surface_edges": False,
                "surface_clip": False,
                # Cull camera-facing walls by default, so you see into the room.
                "surface_cull": True,
                "slice_visible": True,
                "slice_edges": False,
                "contour_visible": False,
                "contour_count": 4,
                # Nested translucent shells stack up fast, and the browser-side
                # renderer blends them more aggressively than the server does.
                "contour_opacity": 0.35,
                "stream_visible": False,
                "stream_seeds": 60,
                "stream_radius": 1.4,
                "stream_length": 4.0,
                "vector_items": [],
                "vector_field": None,
                "glyph_visible": False,
                "glyph_source": "slice",
                "glyph_count": 400,
                "glyph_scale": 1.0,
                "glyph_scale_by": False,
                # patches
                "patch_items": [],
                "selected_patches": [],
            }
        )
        # Debounced sliders (see _slider(debounce=True)) bind their thumb to a
        # `<name>_draft` mirror during the drag and only commit the real var on
        # release, so their heavy change handler runs once, not every tick.
        self.state.update({f"{n}_draft": getattr(self.state, n) for n in _DEBOUNCED})

    # ------------------------------------------------------------- case load

    def load_case(self, name):
        path = self.case_paths.get(name)
        if path is None:
            return

        self._loading = True
        self.case = FoamCase(path)
        self.case.load(self.case.times[-1])
        self.pipeline.set_case(self.case)

        fields = sorted(self.case.fields)
        self.state.update(
            {
                "case_name": name,
                "time_values": self.case.times,
                "n_times": len(self.case.times),
                "time_index": len(self.case.times) - 1,
                "time_label": _fmt_time(self.case.times[-1]),
                "field_items": fields,
                "color_field": self.pipeline.color_field,
                "component_enabled": self.case.fields.get(self.pipeline.color_field) == 3,
                "vector_items": self.case.vector_fields,
                "vector_field": self.pipeline.vector_field,
                "patch_items": self.case.patches,
                "selected_patches": list(self.case.patches),
                "case_info": self._case_info_text(),
            }
        )

        self._sync_drafts()  # keep debounced sliders' *_draft in step with the case
        self._sync_plane_coord()  # bounds changed -> refresh the world-coord field
        self.pipeline.update_data()
        self._loading = False
        self._rescale()
        self.update_scene(reset_camera=True)
        log.info("loaded case %s: %d cells, %d step(s)%s",
                 name, self.case.n_cells(), len(self.case.times),
                 " (decomposed)" if self.case.decomposed else "")

    def _case_info_text(self):
        """Drawer caption: cell count, steps, patches, and the reader mode
        (``decomposed`` when reading processor* dirs via vtkPOpenFOAMReader)."""
        c = self.case
        mode = " · decomposed" if getattr(c, "decomposed", False) else ""
        return (
            f"{c.n_cells():,} cells · {len(c.times)} time steps · "
            f"{len(c.patches)} patches{mode}"
        )

    def _push_field_lists(self):
        """Refresh the field/vector selectors from the loaded step's actual
        arrays. OpenFOAM fields can appear in later time steps (e.g. a species
        added mid-run), so the dropdowns must be rebuilt after every load — not
        only when the case is first opened."""
        self.state.field_items = sorted(self.case.fields)
        self.state.vector_items = self.case.vector_fields

    # --------------------------------------------------------------- scene

    def update_scene(self, reset_camera=False):
        """Push all state into the pipeline and redraw."""
        if self._loading or self.case is None:
            return
        s = self.state
        p = self.pipeline

        p.color_field = s.color_field
        p.color_component = s.color_component
        p.vector_field = s.vector_field
        p.use_cell_data = bool(s.use_cell_data)
        p.apply_color_array()
        if self.case.vector_field_available(s.vector_field):
            self.case.internal.GetPointData().SetActiveVectors(s.vector_field)

        p.n_colors = int(s.n_colors or 0)
        p.set_preset(s.preset)
        p.set_color_range(float(s.range_min), float(s.range_max))

        p.update_plane(s.plane_axis, float(s.plane_position))
        p.update_plane_outline(s.plane_axis, float(s.plane_position))
        p.update_surface(
            s.surface_visible,
            s.surface_colored,
            float(s.surface_opacity),
            s.surface_edges,
            s.surface_clip,
            s.surface_cull,
        )
        p.update_slice(s.slice_visible, s.slice_edges)
        p.update_contour(s.contour_visible, int(s.contour_count), float(s.contour_opacity))
        p.update_streamlines(
            s.stream_visible,
            int(s.stream_seeds),
            float(s.stream_radius),
            float(s.stream_length),
        )
        p.update_glyphs(
            s.glyph_visible,
            s.glyph_source,
            int(s.glyph_count),
            float(s.glyph_scale),
            s.glyph_scale_by,
        )

        self._update_legend()

        if reset_camera:
            p.set_view("iso")
            self.ctrl.view_reset_camera()
        self.ctrl.view_update()

    def _sync_drafts(self):
        """Mirror each debounced slider's real var into its `<name>_draft`, so a
        thumb bound to the draft follows programmatic changes (case load, and any
        future reset) instead of snapping back to a stale drag value."""
        for n in _DEBOUNCED:
            setattr(self.state, f"{n}_draft", getattr(self.state, n))

    # -- cut plane: fraction (0..1) <-> world coordinate --------------------

    def _axis_range(self, axis):
        b = self.case.bounds()  # xmin, xmax, ymin, ymax, zmin, zmax
        i = "xyz".index(axis)
        return b[2 * i], b[2 * i + 1]

    def _coord_from_fraction(self, axis, frac):
        lo, hi = self._axis_range(axis)
        return lo + (hi - lo) * min(max(frac, 0.0), 1.0)

    def _fraction_from_coord(self, axis, coord):
        lo, hi = self._axis_range(axis)
        return 0.5 if hi <= lo else min(max((coord - lo) / (hi - lo), 0.0), 1.0)

    def _sync_plane_coord(self):
        """Refresh the world-coordinate field (and the slider draft) from the
        committed fraction/axis/bounds — after a coord edit, an axis switch or a
        case load. The coord field and the fraction are two views of one plane."""
        if self.case is None:
            return
        if self.state.plane_position_draft != self.state.plane_position:
            self.state.plane_position_draft = self.state.plane_position
        coord = round(
            self._coord_from_fraction(self.state.plane_axis, float(self.state.plane_position)), 4
        )
        if self.state.plane_coord != coord:
            self.state.plane_coord = coord

    def _rescale(self):
        """Recompute the colour range from the data, honouring the range mode."""
        if self.case is None:
            return
        lo, hi = self.case.field_range(
            self.state.color_field, self.state.color_component, self.state.robust_range
        )
        if hi - lo < 1e-12:  # a uniform field still needs a drawable range
            lo, hi = lo - 0.5, hi + 0.5
        with self.state:
            self.state.range_min = round(lo, 6)
            self.state.range_max = round(hi, 6)

    def _update_legend(self):
        lo, hi = self.pipeline.color_range
        unit = _FIELD_UNITS.get(self.state.color_field, "")
        label = self.state.color_field or ""
        if self.state.component_enabled:
            label += f" ({self.state.color_component})"
        self.state.legend_gradient = colors.css_gradient(
            self.state.preset, n_colors=int(self.state.n_colors or 0)
        )
        # Top-to-bottom, matching the vertical gradient.
        values = [hi - (hi - lo) * i / 4 for i in range(5)]
        self.state.legend_ticks = _format_ticks(values)
        self.state.legend_title = f"{label} {unit}".strip()

    # ------------------------------------------------------- busy overlay

    def _busy_call(self, work):
        """Raise the busy overlay, then run `work` (a blocking scene rebuild) on
        the NEXT event-loop tick. The tick matters: trame is single-threaded, so
        a heavy VTK update freezes the loop; scheduling it after the busy flush
        lets the browser receive busy=True and put up the overlay — which
        captures further widget clicks — *before* the freeze, so edits don't
        queue up behind a long operation. Cheap, live edits (opacity, colour map,
        tube width) skip this and stay synchronous."""
        if self._loading or self.case is None:
            return
        self._busy_count += 1
        with self.state:
            self.state.busy = True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._run(work)  # no loop (e.g. at construction) — just run it
            return
        loop.call_soon(self._run, work)

    def _run(self, work):
        try:
            work()
        except Exception:
            log.exception("scene update failed")
        finally:
            self._busy_count = max(0, self._busy_count - 1)
            if self._busy_count == 0:
                with self.state:
                    self.state.busy = False

    # ------------------------------------------------------- state handlers

    @change("case_name")
    def _on_case(self, case_name, **_):
        # Ignore the echo of our own state write while a case is being loaded.
        if self._loading or not case_name:
            return
        if self.case is None or case_name != self.case.name:
            self.load_case(case_name)

    def _rescan_cases(self):
        """Re-read the case root — cases can appear after startup — and refresh
        the drawer list. Returns the updated case_paths."""
        self.case_paths = {p.name: str(p) for p in find_cases(self.case_root)}
        with self.state:
            self.state.case_items = sorted(self.case_paths)
        return self.case_paths

    def _preselect(self, name):
        """Load a case by name — the hook for the ``/viz/?case=<name>`` deep link
        (see :meth:`_add_http_routes`). Re-scans the case root first if the name
        is unknown, so a case created after startup still resolves. Runs in the
        event loop via call_soon, so it swallows/logs its own errors rather than
        taking the server down."""
        try:
            if not name:
                return
            if name not in self.case_paths:
                self._rescan_cases()
            if name not in self.case_paths:
                log.warning("preselect: unknown case %r (have %s)",
                            name, sorted(self.case_paths))
                return
            if self.case is None or name != self.case.name:
                log.info("preselect: loading case %s", name)
                self.load_case(name)
            else:
                # Same case reopened — refresh its time list so steps written
                # since the last open (e.g. more solve iterations) show up.
                log.info("preselect: refreshing times for %s", name)
                self._refresh_times()
        except Exception:
            log.exception("preselect(%r) failed", name)

    @change("time_index")
    def _on_time(self, time_index, **_):
        if self._loading or self.case is None:
            return
        idx = max(0, min(int(time_index), len(self.case.times) - 1))
        self.state.time_label = _fmt_time(self.case.times[idx])
        self._busy_call(lambda: self._do_time(idx))  # loading a step is heavy

    def _do_time(self, idx):
        time = self.case.times[idx]
        if self.case.load(time, self.state.selected_patches):
            self.pipeline.update_data()
            self._push_field_lists()  # fields can differ between time steps
            if self.state.auto_range:
                self._rescale()
        self.update_scene()

    @change("selected_patches")
    def _on_patches(self, **_):
        self._busy_call(self._do_patches)

    def _do_patches(self):
        self.case.load(self.case.times[int(self.state.time_index)], self.state.selected_patches)
        self.pipeline.update_data()
        self.update_scene()

    @change("plane_position_draft")
    def _on_plane_preview(self, **_):
        """Live, cheap preview while the position slider is dragged: move the
        amber plane frame to the draft position without recutting. The real
        slice/streamlines/glyphs recompute once, on release, via _on_heavy."""
        if self._loading or self.case is None:
            return
        self.pipeline.update_plane_outline(
            self.state.plane_axis, float(self.state.plane_position_draft)
        )
        self.ctrl.view_update()

    @change("plane_position", "plane_axis")
    def _on_plane_sync(self, **_):
        # Keep the world-coord field and the slider draft in step with the
        # committed position (needed after a coord edit or an axis switch).
        if self._loading or self.case is None:
            return
        self._sync_plane_coord()

    @change("plane_coord")
    def _on_plane_coord(self, plane_coord, **_):
        """Place the plane from a typed world coordinate. Setting plane_position
        commits it (heavy, via _on_heavy) and echoes back through _on_plane_sync;
        the fraction comparison absorbs that echo so it does not loop."""
        if self._loading or self.case is None:
            return
        frac = self._fraction_from_coord(self.state.plane_axis, float(plane_coord or 0.0))
        if abs(frac - float(self.state.plane_position)) > 1e-4:
            self.state.plane_position = round(frac, 6)

    @change("color_field", "color_component", "robust_range")
    def _on_field(self, **_):
        if self._loading or self.case is None:
            return
        self.state.component_enabled = self.case.fields.get(self.state.color_field) == 3
        if not self.state.component_enabled:
            self.state.color_component = "magnitude"
        self._busy_call(self._do_field)

    def _do_field(self):
        if self.state.auto_range:
            self._rescale()
        self.update_scene()

    # Heavy: recompute geometry (recut/clip, contour/streamline/glyph filters).
    @change(
        "plane_axis",
        "plane_position",
        "surface_clip",
        "contour_visible",
        "contour_count",
        "stream_visible",
        "stream_seeds",
        "stream_length",
        "vector_field",
        "glyph_visible",
        "glyph_source",
        "glyph_count",
        "glyph_scale_by",
        # showing the mesh switches the slice to a crinkle extraction (whole
        # cells) — real geometry work, so it belongs behind the busy overlay
        "slice_edges",
    )
    def _on_heavy(self, **_):
        self._busy_call(self.update_scene)

    # Cheap: render-only tweaks of already-computed geometry — stay live, no
    # overlay (they finish in a frame).
    @change(
        "preset",
        "range_min",
        "range_max",
        "n_colors",
        "use_cell_data",
        "surface_visible",
        "surface_colored",
        "surface_opacity",
        "surface_edges",
        "surface_cull",
        "slice_visible",
        "contour_opacity",
        "stream_radius",
        "glyph_scale",
    )
    def _on_cheap(self, **_):
        self.update_scene()

    # ------------------------------------------------------------ controller

    @controller.set("rescale")
    def rescale(self):
        self._busy_call(self._do_field)

    @controller.set("set_view")
    def set_view(self, direction):
        self.pipeline.set_view(direction)
        self.ctrl.view_reset_camera()
        self.ctrl.view_update()

    @controller.set("toggle_play")
    def toggle_play(self):
        self.state.playing = not self.state.playing
        if self.state.playing:
            self._player_task = asyncio.create_task(self._play())

    async def _play(self):
        """Step through the time steps until stopped or the last step is shown."""
        while self.state.playing:
            with self.state:
                nxt = int(self.state.time_index) + 1
                if nxt >= len(self.case.times):
                    self.state.playing = False
                    break
                self.state.time_index = nxt
            await asyncio.sleep(0.35)

    def _refresh_times(self, force=False):
        """Re-scan for time steps written during a running solve. Cheap when
        nothing changed: it re-reads only the time list and updates the slider
        bounds. The blocking mesh re-read (and jump to the newest step) happens
        only when a newer step appeared, or when ``force`` (the refresh button).

        Reopening the viewer uses ``force=False``, so several tabs opening the
        shared session at once don't each trigger a full reload that freezes the
        single event loop (a likely cause of intermittent 502s under nginx)."""
        if self.case is None:
            return
        prev_latest = self.case.times[-1]
        times = self.case.refresh_times()
        idx = len(times) - 1
        with self.state:
            self.state.time_values = times
            self.state.n_times = len(times)
        if not (force or times[-1] != prev_latest):
            return  # nothing new — no blocking mesh read

        self._loading = True
        self.case.load(times[idx], self.state.selected_patches, force=force)
        self.pipeline.update_data()
        self._push_field_lists()  # a continued run may have added fields
        self.state.update(
            {
                "time_index": idx,
                "time_label": _fmt_time(times[idx]),
                "case_info": self._case_info_text(),  # after load: fresh n_cells
            }
        )
        self._loading = False
        if self.state.auto_range:
            self._rescale()
        self.update_scene()

    @controller.set("refresh_times")
    def refresh_times(self):
        self._refresh_times(force=True)

    def _add_http_routes(self, wslink_server):
        """Serve a freshly rendered PNG at :data:`SHOT_ROUTE`.

        A real HTTP route rather than any client-side trickery: the camera
        control is then an ordinary download link, and one user click renders
        one image. The alternatives all fight the browser -- Chromium refuses a
        scripted click on a multi-megabyte ``data:`` URL, and the Vue template
        expressions trame exposes cannot reach ``document`` or ``$refs`` on the
        surrounding component.
        """
        from aiohttp import web

        async def handler(_request):
            with tempfile.TemporaryDirectory() as tmp:
                png = Path(tmp) / "shot.png"
                self.pipeline.screenshot(png, magnification=2)
                body = png.read_bytes()
            name = f"foamviz-{self.state.case_name}-t{self.state.time_label}.png"
            name = name.replace(" ", "").replace("/", "-")
            return web.Response(
                body=body,
                content_type="image/png",
                headers={"Content-Disposition": f'attachment; filename="{name}"'},
            )

        wslink_server.app.router.add_get(SHOT_ROUTE, handler)

        # Deep link: /viz/?case=<name> preselects a case, so the CFD backend can
        # open a specific case directly. Done server-side (a request middleware)
        # rather than client JS, because Vue template expressions cannot read
        # window.location (see CLAUDE.md). This suits the shared-session model:
        # one HTTP signal sets the one shared scene. Scheduled after the response
        # so the page returns before the (blocking) case load runs.
        # NB: the second parameter must be named `handler` — aiohttp invokes
        # middlewares as partial(mw, handler=next_handler), i.e. by keyword.
        @web.middleware
        async def preselect_case(request, handler):
            response = await handler(request)
            name = request.query.get("case")
            if name:
                asyncio.get_running_loop().call_soon(self._preselect, name)
            return response

        wslink_server.app.middlewares.append(preselect_case)

    # ------------------------------------------------------------------- UI

    def _build_ui(self):
        # Dark throughout: the 3D view is dark by necessity, and a light chrome
        # around it makes every colour-mapped result look washed out.
        with SinglePageWithDrawerLayout(self.server, width=340, theme="dark") as layout:
            self.ui = layout
            layout.title.set_text("FoamViz")
            client.Style(_CSS)

            with layout.icon:
                v3.VIcon("mdi-air-filter")

            self._toolbar()
            self._drawer()
            self._content()
            self._busy_overlay()

    def _toolbar(self):
        with self.ui.toolbar:
            v3.VSpacer()

            # Widget-tool selector: picks which tool's controls the drawer shows
            # (see TOOLS / _drawer). It only drives the side panel, not what's
            # rendered -- each representation keeps its own "Show ..." switch, so
            # several can be visible at once regardless of the selected tool.
            with v3.VBtnToggle(
                v_model=("active_tool", "cutplane"),
                mandatory=True,
                density="comfortable",
                variant="outlined",
                divided=True,
            ):
                for key, title, icon in TOOLS:
                    v3.VBtn(title, value=key, prepend_icon=icon, size="small")

            v3.VSpacer()

            # A download link, not a button with a callback: the route renders
            # on demand, so the click and the image cannot get out of step.
            with html.A(
                href=SHOT_ROUTE,
                download=True,
                classes="js-screenshot",
                style="text-decoration: none",
            ):
                v3.VBtn(
                    icon="mdi-camera",
                    variant="text",
                    density="comfortable",
                )

    def _drawer(self):
        with self.ui.drawer:
            v3.VSelect(
                v_model=("case_name", None),
                items=("case_items",),
                label="Case",
                density="compact",
                variant="outlined",
                hide_details=True,
                prepend_inner_icon="mdi-folder-open",
                classes="mt-3 mb-1",
            )
            html.Div(
                "{{ case_info }}",
                classes="text-caption text-medium-emphasis mb-2 px-1",
            )

            # Colouring applies to everything, so it stays permanently visible.
            self._section_colour()

            # One tool's controls at a time, chosen by the toolbar selector. The
            # panels are only hidden (v-show), not unmounted, so their state and
            # their representations survive a tool switch.
            builders = {
                "cutplane": self._tool_cutplane,
                "boundary": self._tool_boundary,
                "contour": self._tool_contour,
                "stream": self._tool_stream,
                "glyph": self._tool_glyph,
            }
            for key, title, icon in TOOLS:
                with html.Div(v_show=(f"active_tool === '{key}'",)):
                    builders[key](title, icon)

    # -- drawer sections --------------------------------------------------

    def _section_colour(self):
        with _section("Colour", "mdi-palette"):
            # The `js-*` classes are stable hooks for tests/browser_check.py;
            # Vuetify's own markup offers nothing reliable to select on.
            v3.VSelect(
                v_model=("color_field", None),
                items=("field_items",),
                label="Field",
                classes="mb-3 js-color-field",
                **_SELECT_BASE,
            )
            v3.VSelect(
                v_model=("color_component", "magnitude"),
                items=("components", COMPONENTS),
                item_title="title",
                item_value="value",
                label="Component",
                disabled=("!component_enabled",),
                classes="mb-3 js-color-component",
                **_SELECT_BASE,
            )
            v3.VSelect(
                v_model=("preset", "coolwarm"),
                items=("preset_items",),
                item_title="title",
                item_value="value",
                label="Colour map",
                classes="mb-3 js-preset",
                **_SELECT_BASE,
            )
            with v3.VRow(classes="mt-1 mx-0 align-center"):
                v3.VSwitch(
                    v_model=("auto_range", True),
                    label="Auto range",
                    density="compact",
                    hide_details=True,
                    color="primary",
                    classes="mr-3",
                )
                v3.VSwitch(
                    v_model=("robust_range", False),
                    label="1-99%",
                    density="compact",
                    hide_details=True,
                    color="primary",
                )
            _switch("use_cell_data", "True cell values")
            with html.Div(classes="d-flex mt-3", style="gap: 8px"):
                v3.VTextField(
                    v_model_number=("range_min", 0.0),
                    label="Min",
                    type="number",
                    **_FIELD,
                )
                v3.VTextField(
                    v_model_number=("range_max", 1.0),
                    label="Max",
                    type="number",
                    **_FIELD,
                )
            # 0 (or blank) = smooth; >0 bands the map into that many colours.
            v3.VTextField(
                v_model_number=("n_colors", 0),
                label="Bands (0 = smooth)",
                type="number",
                min=0,
                max=32,
                classes="mt-3 js-bands",
                **_FIELD,
            )
            v3.VBtn(
                "Rescale to data",
                block=True,
                variant="tonal",
                size="small",
                classes="mt-2",
                prepend_icon="mdi-arrow-expand-horizontal",
                click=self.ctrl.rescale,
            )

    def _tool_cutplane(self, title, icon):
        """Cut plane + slice: the plane is the hub the slice, stream seeds and
        arrows all sit on, and the slice is its most direct visualisation."""
        with _section(title, icon):
            html.Div(
                "Slice, stream seeds and arrows all sit on this plane.",
                classes="text-caption text-medium-emphasis mb-2",
            )
            # Build the buttons inside the toggle's own context: constructing a
            # VBtn while another element is the active parent would attach it
            # there as well, and it would render twice.
            with v3.VBtnToggle(
                v_model=("plane_axis", "z"),
                mandatory=True,
                density="compact",
                variant="outlined",
                divided=True,
                classes="mb-3",
            ):
                v3.VBtn("X", value="x", size="small")
                v3.VBtn("Y", value="y", size="small")
                v3.VBtn("Z", value="z", size="small")
            _slider("plane_position", "Position", 0.0, 1.0, 0.005, debounce=True)
            # Exact placement by world coordinate along the active axis (metres).
            v3.VTextField(
                v_model_number=("plane_coord", 0.0),
                label=("plane_axis.toUpperCase() + ' coordinate [m]'",),
                type="number",
                step=0.05,
                classes="mt-1 js-plane-coord",
                **_FIELD,
            )
            v3.VDivider(classes="my-3")
            _switch("slice_visible", "Show slice")
            # With the mesh on, the slice becomes a crinkle slice: whole cells
            # the plane passes through, i.e. the true mesh, not a flat cut.
            _switch("slice_edges", "Mesh (crinkle)")

    def _tool_boundary(self, title, icon):
        """Room shell (the boundary surface) + which patches are read."""
        with _section(title, icon):
            _switch("surface_visible", "Show boundary patches")
            _switch("surface_colored", "Colour by field")
            _switch("surface_cull", "Cull near walls")
            _switch("surface_clip", "Cut away at plane")
            _switch("surface_edges", "Mesh edges")
            _slider("surface_opacity", "Opacity", 0.0, 1.0, 0.01)
            v3.VDivider(classes="my-3")
            v3.VSelect(
                v_model=("selected_patches", []),
                items=("patch_items",),
                label="Patches to read",
                multiple=True,
                chips=True,
                closable_chips=True,
                **_SELECT,
            )

    def _tool_contour(self, title, icon):
        with _section(title, icon):
            _switch("contour_visible", "Show isosurfaces")
            _slider("contour_count", "Count", 1, 12, 1, debounce=True)
            _slider("contour_opacity", "Opacity", 0.05, 1.0, 0.05)

    def _tool_stream(self, title, icon):
        with _section(title, icon):
            _switch("stream_visible", "Show streamlines")
            v3.VSelect(
                v_model=("vector_field", None),
                items=("vector_items",),
                label="Vector field",
                **_SELECT,
            )
            _slider("stream_seeds", "Seeds", 5, 400, 5, debounce=True)
            _slider("stream_radius", "Tube width", 0.2, 5.0, 0.1)
            _slider("stream_length", "Max length (x domain)", 0.5, 15.0, 0.5, debounce=True)

    def _tool_glyph(self, title, icon):
        with _section(title, icon):
            _switch("glyph_visible", "Show arrows")
            with v3.VBtnToggle(
                v_model=("glyph_source", "slice"),
                mandatory=True,
                density="compact",
                variant="outlined",
                divided=True,
                classes="mb-3",
            ):
                v3.VBtn("On plane", value="slice", size="small")
                v3.VBtn("In volume", value="volume", size="small")
            _switch("glyph_scale_by", "Length follows magnitude")
            _slider("glyph_count", "Count", 20, 3000, 20, debounce=True)
            _slider("glyph_scale", "Size", 0.1, 5.0, 0.1)

    # -- main content -----------------------------------------------------

    def _content(self):
        with self.ui.content:
            with html.Div(classes="foamviz-stage"):
                view = vtk_widgets.VtkRemoteLocalView(
                    self.pipeline.render_window,
                    namespace="view",
                    mode="local",
                    interactive_ratio=1,
                )
                self.ctrl.view_update = view.update
                self.ctrl.view_reset_camera = view.reset_camera

                self._legend()
                self._bottom_bar()
                self._mode_switch()

    def _bottom_bar(self):
        """Floating strip over the 3D view: camera presets on the left, the time
        controls on the right. Camera and time both act on the whole scene, so
        they live outside the per-tool drawer -- and floating over the canvas
        keeps them one glance from the result they change."""
        with html.Div(classes="foamviz-bottombar"):
            for label, direction in VIEW_BUTTONS:
                v3.VBtn(
                    label,
                    size="small",
                    variant="tonal",
                    classes="mx-0 px-2",
                    min_width="0",
                    click=(self.ctrl.set_view, f"['{direction}']"),
                )
            v3.VDivider(vertical=True, classes="mx-2")
            v3.VBtn(
                icon=("playing ? 'mdi-pause' : 'mdi-play'",),
                variant="text",
                density="comfortable",
                click=self.ctrl.toggle_play,
                disabled=("n_times < 2",),
            )
            v3.VSlider(
                v_model=("time_index", 0),
                min=0,
                max=("n_times - 1",),
                step=1,
                hide_details=True,
                density="compact",
                style="width: 170px",
                classes="js-time-slider",
                disabled=("n_times < 2",),
            )
            html.Div(
                "t = {{ time_label }}",
                classes="text-caption text-medium-emphasis mx-2 js-time-label",
                style="min-width: 84px",
            )
            # Re-scan for time steps written since the case was opened (a solve
            # keeps writing them) and jump to the newest.
            v3.VBtn(
                icon="mdi-refresh",
                variant="text",
                density="comfortable",
                click=self.ctrl.refresh_times,
                classes="js-refresh-times",
            )

    def _legend(self):
        with html.Div(classes="foamviz-legend"):
            html.Div("{{ legend_title }}", classes="foamviz-legend-title")
            with html.Div(classes="foamviz-legend-body"):
                html.Div(
                    classes="foamviz-legend-bar",
                    style=("`background: ${legend_gradient}`",),
                )
                with html.Div(classes="foamviz-legend-ticks"):
                    html.Div("{{ t }}", v_for="t in legend_ticks", key="t")
            # Decodes the in-scene triad: the arrow colours are the only thing
            # naming the axes, so say which is which.
            with html.Div(classes="foamviz-axis-key"):
                html.Span("X", classes="ax-x")
                html.Span("Y", classes="ax-y")
                html.Span("Z", classes="ax-z")

    def _mode_switch(self):
        with html.Div(classes="foamviz-mode"):
            with v3.VBtnToggle(
                v_model=("viewMode", "local"),
                mandatory=True,
                density="compact",
                variant="outlined",
                divided=True,
                bg_color="rgba(16,18,24,.72)",
            ):
                v3.VBtn("Client", value="local", size="x-small")
                v3.VBtn("Server", value="remote", size="x-small")

    def _busy_overlay(self):
        # Full-screen overlay shown while a heavy update runs; its scrim captures
        # clicks (persistent), so the drawer/toolbar can't be operated until the
        # operation finishes — no queued edits. There is no reliable way to abort
        # a running VTK filter (even ParaView can't), so this prevents piling
        # more work on rather than trying to interrupt it.
        with v3.VOverlay(
            v_model=("busy",),
            persistent=True,
            classes="align-center justify-center",
            style="backdrop-filter: blur(2px)",
        ):
            with html.Div(classes="d-flex flex-column align-center"):
                v3.VProgressCircular(indeterminate=True, size=64, width=5, color="primary")
                html.Div("Working…", classes="mt-4 text-subtitle-1")

    def start(self, **kwargs):
        self.server.start(**kwargs)


# --------------------------------------------------------------------- helpers

_SELECT_BASE = dict(
    density="compact",
    variant="outlined",
    hide_details=True,
)

_SELECT = dict(_SELECT_BASE, classes="mb-3")

_FIELD = dict(
    density="compact",
    variant="outlined",
    hide_details=True,
)

_FIELD_UNITS = {
    "T": "[K]",
    "U": "[m/s]",
    "p": "[m²/s²]",
    "p_rgh": "[m²/s²]",
    "k": "[m²/s²]",
    "epsilon": "[m²/s³]",
    "nut": "[m²/s]",
    "alphat": "[kg/m·s]",
    "rho": "[kg/m³]",
}


def _fmt_time(value):
    return f"{value:g} s"


def _format_ticks(values):
    """Label colour-bar ticks so neighbouring ticks are always distinguishable.

    Fixed significant figures are not enough: a temperature range of
    300.0-300.45 K renders as five identical "300" labels at 3 s.f. The
    precision has to come from the *span*, not from the magnitude.
    """
    span = abs(values[0] - values[-1])
    if span == 0:
        return [f"{values[0]:.4g}"] * len(values)

    largest = max(abs(v) for v in values)
    if largest >= 1e5 or (largest > 0 and largest < 1e-3):
        return [f"{v:.2e}" for v in values]

    # One digit finer than the tick spacing, so adjacent labels differ.
    decimals = max(0, int(math.ceil(-math.log10(span / len(values)))) + 1)
    return [f"{v:.{min(decimals, 8)}f}" for v in values]


# Heavy sliders whose change handler rebuilds geometry — debounced so the work
# runs once on release, not on every tick of a drag. Each gets a `<name>_draft`
# mirror in the state; _sync_drafts() keeps it aligned on programmatic changes.
_DEBOUNCED = (
    "plane_position",
    "contour_count",
    "stream_seeds",
    "stream_length",
    "glyph_count",
)


def _slider(name, label, vmin, vmax, step, debounce=False):
    """A labelled slider that shows its current value.

    ``debounce=True`` binds the thumb (and its live label) to a ``<name>_draft``
    mirror and only writes the real state var on release (VSlider ``@end``), so
    an expensive change handler fires once per drag rather than every tick. The
    real var must be in :data:`_DEBOUNCED` so its draft is initialised and
    re-synced. Cheap sliders leave ``debounce`` off and stay live."""
    model = f"{name}_draft" if debounce else name
    with html.Div(classes="mb-2"):
        with html.Div(classes="d-flex justify-space-between"):
            html.Span(label, classes="text-caption text-medium-emphasis")
            html.Span(f"{{{{ {model} }}}}", classes="text-caption")
        kwargs = dict(
            v_model=(model,),
            min=vmin,
            max=vmax,
            step=step,
            hide_details=True,
            density="compact",
            color="primary",
            thumb_size=12,
        )
        if debounce:
            # JS run on the client at release: commit the draft to the real var,
            # which flushes to the server and triggers the heavy handler once.
            kwargs["end"] = f"{name} = {model}"
        v3.VSlider(**kwargs)


def _switch(name, label):
    v3.VSwitch(
        v_model=(name,),
        label=label,
        density="compact",
        hide_details=True,
        color="primary",
        classes="mb-1",
    )


def _section(title, icon):
    """A titled drawer section. Returns the body container to fill with `with`,
    mirroring how the old expansion-panel helper was used."""
    with html.Div(classes="foamviz-section"):
        with html.Div(classes="foamviz-section-head"):
            v3.VIcon(icon, size="small", classes="mr-2")
            html.Span(title)
        body = html.Div(classes="foamviz-section-body")
    return body


_CSS = """
.foamviz-stage { position: relative; width: 100%; height: 100%; }
.foamviz-legend {
  position: absolute; left: 18px; bottom: 18px; z-index: 5;
  background: rgba(16,18,24,.72); border: 1px solid rgba(255,255,255,.10);
  border-radius: 8px; padding: 10px 12px; backdrop-filter: blur(6px);
  color: #e8eaf0; font-size: 11px; line-height: 1.5; pointer-events: none;
}
.foamviz-legend-title { font-weight: 600; letter-spacing: .03em; margin-bottom: 6px; }
.foamviz-legend-body { display: flex; gap: 8px; }
.foamviz-legend-bar {
  width: 14px; height: 150px; border-radius: 3px;
  border: 1px solid rgba(255,255,255,.18);
}
.foamviz-legend-ticks {
  display: flex; flex-direction: column; justify-content: space-between;
  height: 150px; font-variant-numeric: tabular-nums;
}
.foamviz-axis-key { margin-top: 8px; font-weight: 700; letter-spacing: .05em; }
.foamviz-axis-key span { margin-right: 7px; }
.foamviz-axis-key .ax-x { color: #e64d4d; }
.foamviz-axis-key .ax-y { color: #66d966; }
.foamviz-axis-key .ax-z { color: #598cf2; }
.foamviz-mode { position: absolute; right: 18px; bottom: 18px; z-index: 5; }
.foamviz-bottombar {
  position: absolute; left: 50%; bottom: 18px; transform: translateX(-50%);
  z-index: 5; display: flex; align-items: center; gap: 4px;
  background: rgba(16,18,24,.72); border: 1px solid rgba(255,255,255,.10);
  border-radius: 10px; padding: 6px 10px; backdrop-filter: blur(6px);
}
.foamviz-section { margin: 2px 0; }
.foamviz-section-head {
  display: flex; align-items: center; padding: 10px 4px 6px;
  font-size: 12px; font-weight: 600; letter-spacing: .02em; color: #c8ccd6;
  border-top: 1px solid rgba(255,255,255,.07);
}
.foamviz-section-body { padding: 2px 4px 6px; }
"""
