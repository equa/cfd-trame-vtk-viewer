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


@TrameApp()
class FoamViz:
    def __init__(self, case_root, server=None):
        self.server = get_server(server, client_type="vue3")
        self.pipeline = FoamPipeline()
        self.case = None
        self.case_root = case_root  # kept so cases can be re-scanned on demand
        self._player_task = None
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
                # colouring
                "field_items": [],
                "color_field": None,
                "color_component": "magnitude",
                "component_enabled": False,
                "preset": "coolwarm",
                "preset_items": colors.preset_items(),
                "legend_gradient": colors.css_gradient("coolwarm"),
                "legend_ticks": [],
                "legend_title": "",
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
                # representations
                "surface_visible": True,
                "surface_colored": False,
                "surface_opacity": 0.12,
                "surface_edges": False,
                "surface_clip": False,
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
        p.apply_color_array()
        if self.case.vector_field_available(s.vector_field):
            self.case.internal.GetPointData().SetActiveVectors(s.vector_field)

        p.set_preset(s.preset)
        p.set_color_range(float(s.range_min), float(s.range_max))

        p.update_plane(s.plane_axis, float(s.plane_position))
        p.update_surface(
            s.surface_visible,
            s.surface_colored,
            float(s.surface_opacity),
            s.surface_edges,
            s.surface_clip,
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
        self.state.legend_gradient = colors.css_gradient(self.state.preset)
        # Top-to-bottom, matching the vertical gradient.
        values = [hi - (hi - lo) * i / 4 for i in range(5)]
        self.state.legend_ticks = _format_ticks(values)
        self.state.legend_title = f"{label} {unit}".strip()

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
        time = self.case.times[idx]
        self.state.time_label = _fmt_time(time)
        if self.case.load(time, self.state.selected_patches):
            self.pipeline.update_data()
            self._push_field_lists()  # fields can differ between time steps
            if self.state.auto_range:
                self._rescale()
        self.update_scene()

    @change("selected_patches")
    def _on_patches(self, **_):
        if self._loading or self.case is None:
            return
        self.case.load(self.case.times[int(self.state.time_index)], self.state.selected_patches)
        self.pipeline.update_data()
        self.update_scene()

    @change("color_field", "color_component", "robust_range")
    def _on_field(self, **_):
        if self._loading or self.case is None:
            return
        self.state.component_enabled = self.case.fields.get(self.state.color_field) == 3
        if not self.state.component_enabled:
            self.state.color_component = "magnitude"
        if self.state.auto_range:
            self._rescale()
        self.update_scene()

    @change(
        "preset",
        "range_min",
        "range_max",
        "plane_axis",
        "plane_position",
        "surface_visible",
        "surface_colored",
        "surface_opacity",
        "surface_edges",
        "surface_clip",
        "slice_visible",
        "slice_edges",
        "contour_visible",
        "contour_count",
        "contour_opacity",
        "stream_visible",
        "stream_seeds",
        "stream_radius",
        "stream_length",
        "vector_field",
        "glyph_visible",
        "glyph_source",
        "glyph_count",
        "glyph_scale",
        "glyph_scale_by",
    )
    def _on_appearance(self, **_):
        self.update_scene()

    # ------------------------------------------------------------ controller

    @controller.set("rescale")
    def rescale(self):
        self._rescale()
        self.update_scene()

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

    def _toolbar(self):
        with self.ui.toolbar:
            v3.VSpacer()

            # --- time -------------------------------------------------------
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
                style="max-width: 220px",
                classes="js-time-slider",
                disabled=("n_times < 2",),
            )
            html.Div(
                "t = {{ time_label }}",
                classes="text-caption text-medium-emphasis mx-2 js-time-label",
                style="min-width: 92px",
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

            v3.VDivider(vertical=True, classes="mx-2")

            # --- camera -----------------------------------------------------
            for label, direction in VIEW_BUTTONS:
                v3.VBtn(
                    label,
                    size="small",
                    variant="tonal",
                    classes="mx-1 px-2",
                    min_width="0",
                    click=(self.ctrl.set_view, f"['{direction}']"),
                )

            v3.VDivider(vertical=True, classes="mx-2")

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

            with v3.VExpansionPanels(
                model_value=([0, 1],), multiple=True, variant="accordion", flat=True
            ):
                self._panel_colour()
                self._panel_plane()
                self._panel_surface()
                self._panel_slice()
                self._panel_contour()
                self._panel_streamlines()
                self._panel_glyphs()
                self._panel_patches()

    # -- drawer panels ----------------------------------------------------

    def _panel_colour(self):
        with _panel("Colour", "mdi-palette"):
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
            v3.VBtn(
                "Rescale to data",
                block=True,
                variant="tonal",
                size="small",
                classes="mt-2",
                prepend_icon="mdi-arrow-expand-horizontal",
                click=self.ctrl.rescale,
            )

    def _panel_plane(self):
        with _panel("Cut plane", "mdi-square-outline"):
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
            _slider("plane_position", "Position", 0.0, 1.0, 0.005)

    def _panel_surface(self):
        with _panel("Room shell", "mdi-cube-outline"):
            _switch("surface_visible", "Show boundary patches")
            _switch("surface_colored", "Colour by field")
            _switch("surface_clip", "Cut away at plane")
            _switch("surface_edges", "Mesh edges")
            _slider("surface_opacity", "Opacity", 0.0, 1.0, 0.01)

    def _panel_slice(self):
        with _panel("Slice", "mdi-layers-outline"):
            _switch("slice_visible", "Show slice")
            _switch("slice_edges", "Mesh edges")

    def _panel_contour(self):
        with _panel("Isosurfaces", "mdi-blur"):
            _switch("contour_visible", "Show isosurfaces")
            _slider("contour_count", "Count", 1, 12, 1)
            _slider("contour_opacity", "Opacity", 0.05, 1.0, 0.05)

    def _panel_streamlines(self):
        with _panel("Streamlines", "mdi-vector-polyline"):
            _switch("stream_visible", "Show streamlines")
            v3.VSelect(
                v_model=("vector_field", None),
                items=("vector_items",),
                label="Vector field",
                **_SELECT,
            )
            _slider("stream_seeds", "Seeds", 5, 400, 5)
            _slider("stream_radius", "Tube width", 0.2, 5.0, 0.1)
            _slider("stream_length", "Max length (x domain)", 0.5, 15.0, 0.5)

    def _panel_glyphs(self):
        with _panel("Vector arrows", "mdi-arrow-top-right"):
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
            _slider("glyph_count", "Count", 20, 3000, 20)
            _slider("glyph_scale", "Size", 0.1, 5.0, 0.1)

    def _panel_patches(self):
        with _panel("Boundary patches", "mdi-select-group"):
            v3.VSelect(
                v_model=("selected_patches", []),
                items=("patch_items",),
                label="Patches to read",
                multiple=True,
                chips=True,
                closable_chips=True,
                **_SELECT,
            )

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
                self._mode_switch()

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


def _slider(name, label, vmin, vmax, step):
    """A labelled slider that shows its current value."""
    with html.Div(classes="mb-2"):
        with html.Div(classes="d-flex justify-space-between"):
            html.Span(label, classes="text-caption text-medium-emphasis")
            html.Span(f"{{{{ {name} }}}}", classes="text-caption")
        v3.VSlider(
            v_model=(name,),
            min=vmin,
            max=vmax,
            step=step,
            hide_details=True,
            density="compact",
            color="primary",
            thumb_size=12,
        )


def _switch(name, label):
    v3.VSwitch(
        v_model=(name,),
        label=label,
        density="compact",
        hide_details=True,
        color="primary",
        classes="mb-1",
    )


def _panel(title, icon):
    panel = v3.VExpansionPanel()
    with panel:
        with v3.VExpansionPanelTitle(classes="text-body-2"):
            v3.VIcon(icon, size="small", classes="mr-2")
            html.Span(title)
        text = v3.VExpansionPanelText()
    return text


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
"""
