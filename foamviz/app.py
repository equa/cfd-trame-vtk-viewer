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
import json
import logging
import math
import os
import shutil
import tempfile
import zipfile
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

# Interactive vtk.js scene export is mothballed for now: the report uses the PNG
# poster only, and unused <case>/report/*.vtkjs files would just accumulate. The
# export code is kept intact -- flip this to True to write the scenes again once
# an interactive report viewer is built.
EXPORT_VTKJS = False

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
    ("geometry", "Geometry", "mdi-home-outline"),
]

# Keyboard shortcuts, extensibly: a pressed key (event.key) -> a CSS selector to
# click. Reusable for any button-backed action -- add a row here and give the
# target element that class; the shortcut then rides the element's own click
# handler, so there's no separate JS<->Python wiring. Shift yields an uppercase
# key (so shift+x -> -x). (vtk.js already binds "r" to reset the camera.)
KEY_SHORTCUTS = {
    "x": ".js-view-px", "X": ".js-view-mx",
    "y": ".js-view-py", "Y": ".js-view-my",
    "z": ".js-view-pz", "Z": ".js-view-mz",
}

# One window-level keydown listener that dispatches KEY_SHORTCUTS by clicking the
# mapped element. Ignores typing in fields and OS/browser modifier combos.
_KEY_JS_TEMPLATE = """
(function () {
  if (window.__foamvizKeys) return;
  window.__foamvizKeys = true;
  const MAP = __MAP__;
  window.addEventListener('keydown', function (e) {
    if (e.ctrlKey || e.altKey || e.metaKey) return;
    const t = e.target;
    if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
    const sel = MAP[e.key];
    if (!sel) return;
    const el = document.querySelector(sel);
    if (el) { el.click(); e.preventDefault(); }
  });
})();
"""


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
                # UI theme, driven by the embedding app via ?theme=light|dark.
                # Bound to the layout's <VApp :theme>, so it switches at runtime.
                "ui_theme": "dark",
                # "Add to case report" — caption for the next figure + a snackbar
                "report_caption": "",
                "report_msg": "",
                "report_snack": False,
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
                # Scene lighting: the light kit (multi-light rig) on/off, plus an
                # ambient floor (no face goes fully black) and diffuse
                # (directional shading) on the lit actors. Persisted globally.
                "light_kit": True,
                "light_ambient": 0.3,
                "light_diffuse": 0.7,
                "auto_range": True,
                # Off by default. It is the right tool when a tiny extreme
                # region flattens the map, but it saturates everything above the
                # 99th percentile -- misleading unless you asked for it.
                "robust_range": False,
                "range_min": 0.0,
                "range_max": 1.0,
                # cut plane. Source of truth: the world point (plane_x/y/z) plus
                # the normal axis; only the active-axis coordinate positions the
                # cut, the other two are remembered across a normal switch. The
                # slider (ranged by axis_min/axis_max) previews the active
                # coordinate live and commits it on release; the fields are the
                # truth and take effect on Apply.
                "plane_axis": "z",
                "plane_x": 0.0,
                "plane_y": 0.0,
                "plane_z": 0.0,
                "plane_slider": 0.0,
                "axis_min": 0.0,
                "axis_max": 1.0,
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
                # building geometry (OBJ). has_geometry is set per case.
                "has_geometry": False,
                "geometry_visible": False,
                "geometry_mode": "features",
                "geometry_opacity": 1.0,
                "geometry_line_width": 2.0,
            }
        )
        # Debounced sliders (see _slider(debounce=True)) bind their thumb to a
        # `<name>_draft` mirror during the drag and only commit the real var on
        # release, so their heavy change handler runs once, not every tick.
        self.state.update({f"{n}_draft": getattr(self.state, n) for n in _DEBOUNCED})

        # Restore globally-persisted preferences (lighting) over the defaults.
        saved = self._load_settings()
        for key, lo, hi in (("light_ambient", 0.0, 1.0), ("light_diffuse", 0.0, 1.0)):
            if key in saved:
                try:
                    setattr(self.state, key, min(hi, max(lo, float(saved[key]))))
                except (TypeError, ValueError):
                    pass
        if "light_kit" in saved:
            setattr(self.state, "light_kit", bool(saved["light_kit"]))

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
                # Start every case with the heavy representations off. Otherwise a
                # big case inherits streamlines/isosurfaces/arrows left on from the
                # previous case and rebuilds them all on load -- slow, and enough
                # to hang or crash the viewer. The cheap slice stays on.
                "contour_visible": False,
                "stream_visible": False,
                "glyph_visible": False,
                # does this case ship constant/triSurface/building.obj?
                "has_geometry": self.pipeline.has_geometry,
            }
        )

        self._sync_drafts()  # keep debounced sliders' *_draft in step with the case
        self._reset_plane()  # bounds changed -> recentre the plane, re-range slider
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

    # ---------------------------------------------------- persisted settings

    def _settings_path(self):
        """Where global preferences live. Default: a dotfile at the data root
        (persistent in the deployment — the CFD_HOME volume); override with
        ``FOAMVIZ_SETTINGS``."""
        env = os.environ.get("FOAMVIZ_SETTINGS")
        if env:
            return Path(env)
        root = Path(self.case_root) if self.case_root else Path.home()
        return root / ".foamviz-settings.json"

    def _load_settings(self):
        try:
            return json.loads(self._settings_path().read_text())
        except (OSError, ValueError):
            return {}

    def _save_settings(self):
        data = {k: getattr(self.state, k) for k in ("light_kit", "light_ambient", "light_diffuse")}
        try:
            path = self._settings_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, indent=2))
        except OSError as exc:
            log.warning("could not persist settings to %s: %s", self._settings_path(), exc)

    def _apply_lighting(self):
        self.pipeline.set_light_kit(bool(self.state.light_kit))
        self.pipeline.set_lighting(float(self.state.light_ambient), float(self.state.light_diffuse))

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
        self._apply_lighting()

        coord = self._active_coord()
        p.update_plane(s.plane_axis, coord)
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
        p.update_geometry(
            s.geometry_visible,
            s.geometry_mode,
            float(s.geometry_opacity),
            float(s.geometry_line_width),
        )

        self._update_legend()

        if reset_camera:
            p.set_view("iso")
            self.ctrl.view_push_camera()  # apply the default orientation on the client
        self.ctrl.view_update()

    def _sync_drafts(self):
        """Mirror each debounced slider's real var into its `<name>_draft`, so a
        thumb bound to the draft follows programmatic changes (case load, and any
        future reset) instead of snapping back to a stale drag value."""
        for n in _DEBOUNCED:
            setattr(self.state, f"{n}_draft", getattr(self.state, n))

    # -- cut plane: a world-coordinate point + a live position slider --------
    #
    # plane_x/y/z hold the plane point; plane_axis is the normal. Only the
    # active-axis coordinate cuts, so everything funnels through update_scene()
    # reading _active_coord(). The slider is a view: it previews live and, on
    # release, writes the active coordinate and auto-applies. The fields are
    # inert until Apply. No fraction anywhere -- world coordinates throughout.

    def _axis_range(self, axis):
        b = self.case.bounds()  # xmin, xmax, ymin, ymax, zmin, zmax
        i = "xyz".index(axis)
        return b[2 * i], b[2 * i + 1]

    def _active_coord(self):
        """The plane point's coordinate along the active normal axis."""
        return float(getattr(self.state, f"plane_{self.state.plane_axis}"))

    def _reset_plane(self):
        """Centre the plane point in the domain. On case load the bounds -- hence
        the sensible default and the valid range -- change."""
        if self.case is None:
            return
        b = self.case.bounds()
        self.state.plane_x = round((b[0] + b[1]) / 2, 4)
        self.state.plane_y = round((b[2] + b[3]) / 2, 4)
        self.state.plane_z = round((b[4] + b[5]) / 2, 4)
        self._sync_plane_ui()

    def _sync_plane_ui(self):
        """Point the slider at the active axis: range from the bounds, value from
        the active coordinate. After a case load, an axis switch or an Apply."""
        if self.case is None:
            return
        lo, hi = self._axis_range(self.state.plane_axis)
        self.state.axis_min = round(lo, 4)
        self.state.axis_max = round(hi, 4)
        self.state.plane_slider = round(self._active_coord(), 4)

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

    def _set_theme(self, theme):
        """Apply a light/dark UI theme (the ``?theme=`` hook). The Vuetify chrome
        follows the ``ui_theme`` state (bound to ``<VApp :theme>``) and the
        overlays follow Vuetify's theme CSS vars; here we also flip the 3D
        viewport — background and the neutral geometry line colour. Runs via
        call_soon, so it logs its own errors."""
        try:
            light = theme == "light"
            with self.state:
                self.state.ui_theme = "light" if light else "dark"
            self.pipeline.set_theme(light)
            self.ctrl.view_update()
        except Exception:
            log.exception("set_theme(%r) failed", theme)

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

    @change("plane_slider")
    def _on_plane_slide(self, **_):
        """Live, cheap preview while the position slider is dragged: move the red
        plane frame to the dragged coordinate without recutting. The slice and
        everything seeded on the plane recompute once, on release."""
        if self._loading or self.case is None:
            return
        self.pipeline.update_plane_outline(self.state.plane_axis, float(self.state.plane_slider))
        self.ctrl.view_update()

    @change("plane_axis")
    def _on_plane_axis(self, **_):
        # Switching the normal keeps x/y/z; re-point the slider at the new axis
        # and redraw the cut there.
        if self._loading or self.case is None:
            return
        self.plane_apply()

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
    # The cut plane is not here -- it commits through plane_apply / the slider
    # release / an axis switch, all of which route to update_scene themselves.
    @change(
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
        "geometry_visible",
        "geometry_mode",
        "geometry_opacity",
        "geometry_line_width",
    )
    def _on_cheap(self, **_):
        self.update_scene()

    @change("n_colors")
    def _on_n_colors(self, n_colors, **_):
        # Clamp the colour-band count to [0, 256] — the number field lets you
        # type out of range (negative, huge). Snap it back, then render.
        clamped = max(0, min(256, int(n_colors or 0)))
        if clamped != n_colors:
            self.state.n_colors = clamped  # re-fires this handler with the clamped value
            return
        self.update_scene()

    @change("light_kit", "light_ambient", "light_diffuse")
    def _on_lighting(self, **_):
        # Apply directly (works even before a case is loaded — lighting is global
        # and the pipeline always exists) and persist the preference.
        self._apply_lighting()
        self.ctrl.view_update()
        self._save_settings()

    # ------------------------------------------------------------ controller

    @controller.set("rescale")
    def rescale(self):
        self._busy_call(self._do_field)

    @controller.set("plane_apply")
    def plane_apply(self):
        """Redraw the slice at the current plane point (the X/Y/Z fields). Heavy,
        so it goes behind the busy overlay. Also the auto-apply after a slider
        release and an axis switch."""
        self._busy_call(self._do_plane_apply)

    @controller.set("plane_drag_start")
    def plane_drag_start(self):
        """Slider grabbed: show the red plane frame at the current position so it
        previews the drag. Hidden again on release (plane_slider_release)."""
        self.pipeline.update_plane_outline(self.state.plane_axis, float(self.state.plane_slider))
        self.pipeline.set_plane_outline_visible(True)
        self.ctrl.view_update()

    @controller.set("plane_slider_release")
    def plane_slider_release(self):
        """Slider let go: hide the frame, the dragged value becomes the active
        coordinate (the source of truth), then auto-apply."""
        self.pipeline.set_plane_outline_visible(False)
        axis = self.state.plane_axis
        setattr(self.state, f"plane_{axis}", round(float(self.state.plane_slider), 4))
        self.plane_apply()

    def _do_plane_apply(self):
        axis = self.state.plane_axis
        lo, hi = self._axis_range(axis)
        coord = min(max(self._active_coord(), lo), hi)  # clamp typed values into range
        setattr(self.state, f"plane_{axis}", round(coord, 4))
        self._sync_plane_ui()  # reflect the applied coordinate on the slider
        self.update_scene()

    # -- add to case report -------------------------------------------------

    @controller.set("add_to_report")
    def add_to_report(self):
        """Snapshot the current scene into <case>/report/ as a self-contained
        figure: an interactive .vtkjs, a .png poster (for print and while the
        viewer loads), and a .json of the colouring so the report can redraw the
        colour bar. Behind the busy overlay -- exporting a big scene takes a moment."""
        if self.case is None:
            return
        self._busy_call(self._do_add_to_report)

    def _do_add_to_report(self):
        report_dir = self.case.case_dir / "report"
        report_dir.mkdir(exist_ok=True)
        stem = f"figure_{self._next_report_index(report_dir):02d}"

        # Poster first: the scene exporter perturbs the render window, so grab the
        # PNG before it runs (screenshot() re-renders to be safe regardless).
        self.pipeline.screenshot(str(report_dir / f"{stem}.png"))

        # .vtkjs -- the exporter writes a directory; zip it into one file.
        # Disabled for now (see EXPORT_VTKJS); kept so an interactive report
        # viewer can re-enable it with a one-line flip.
        if EXPORT_VTKJS:
            tmp = Path(tempfile.mkdtemp(prefix="foamviz_scene_"))
            try:
                self.pipeline.write_vtkjs(tmp)
                with zipfile.ZipFile(report_dir / f"{stem}.vtkjs", "w", zipfile.ZIP_DEFLATED) as z:
                    for f in tmp.rglob("*"):
                        if f.is_file():
                            z.write(f, f.relative_to(tmp))
            finally:
                shutil.rmtree(tmp, ignore_errors=True)

        meta = {
            "index": int(stem.split("_")[1]),
            "caption": (self.state.report_caption or "").strip() or self._auto_caption(),
            "case": self.case.name,
            "field": self.state.color_field,
            "component": self.state.color_component if self.state.component_enabled else None,
            "range": [float(self.state.range_min), float(self.state.range_max)],
            "preset": self.state.preset,
            "n_colors": int(self.state.n_colors or 0),
            "unit": _FIELD_UNITS.get(self.state.color_field, ""),
            # The report has no access to the LUT, and the poster PNG shows only
            # the 3D view (not the legend), so ship the exact colour bar with the
            # figure: the same gradient + ticks the on-screen legend uses.
            "gradient": colors.css_gradient(
                self.state.preset, n_colors=int(self.state.n_colors or 0)
            ),
            "ticks": list(self.state.legend_ticks),
        }
        (report_dir / f"{stem}.json").write_text(json.dumps(meta, indent=2))
        log.info("added %s to case report at %s", stem, report_dir)

        with self.state:
            self.state.report_caption = ""
            self.state.report_msg = f"Added {stem} to the case report"
            self.state.report_snack = True

    def _next_report_index(self, report_dir):
        used = [int(p.stem.split("_")[1]) for p in report_dir.glob("figure_*.json")
                if p.stem.split("_")[1].isdigit()]
        return max(used, default=0) + 1

    def _auto_caption(self):
        """A caption from what is on screen, when the user did not type one."""
        shown = [name for flag, name in (
            (self.state.slice_visible, "slice"),
            (self.state.contour_visible, "isosurfaces"),
            (self.state.stream_visible, "streamlines"),
            (self.state.glyph_visible, "arrows"),
        ) if flag] or ["boundary"]
        return f"{self.state.color_field or ''} — {', '.join(shown)}".strip(" —")

    @controller.set("set_view")
    def set_view(self, direction):
        self.pipeline.set_view(direction)
        self.ctrl.view_push_camera()  # push orientation to the client (local mode)
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
                self.pipeline.screenshot(png)
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
            theme = request.query.get("theme")
            if theme in ("light", "dark"):
                asyncio.get_running_loop().call_soon(self._set_theme, theme)
            return response

        wslink_server.app.middlewares.append(preselect_case)

    # ------------------------------------------------------------------- UI

    def _build_ui(self):
        # Theme follows ui_theme (dark by default), switched at runtime via the
        # ?theme= hook; see "Light/dark theme" in CLAUDE.md.
        with SinglePageWithDrawerLayout(self.server, width=340, theme=("ui_theme",)) as layout:
            self.ui = layout
            layout.title.set_text("FoamViz")
            client.Style(_CSS)
            client.Script(_KEY_JS_TEMPLATE.replace("__MAP__", json.dumps(KEY_SHORTCUTS)))

            with layout.icon:
                v3.VIcon("mdi-air-filter")

            self._toolbar()
            self._drawer()
            self._content()
            self._busy_overlay()
            with v3.VSnackbar(v_model=("report_snack", False), timeout=2500, color="success"):
                html.Span("{{ report_msg }}")

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
            # RELATIVE href (no leading slash): behind nginx the viewer is served
            # under /viz/, so an absolute /foamviz/... would hit the origin root
            # (the SPA) instead of this service. Relative resolves to
            # /viz/foamviz/... which nginx strips back to the registered route;
            # standalone (page at /) it resolves to /foamviz/... — works both.
            with html.A(
                href=SHOT_ROUTE.lstrip("/"),
                download=True,
                classes="js-screenshot",
                style="text-decoration: none",
            ):
                v3.VBtn(
                    icon="mdi-camera",
                    variant="text",
                    density="comfortable",
                )

            # Snapshot the current scene into the case report (see add_to_report).
            v3.VBtn(
                "Report",
                prepend_icon="mdi-image-plus",
                size="small",
                variant="tonal",
                classes="ml-1 js-add-report",
                click=self.ctrl.add_to_report,
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
                "geometry": self._tool_geometry,
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
                max=256,
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
            # Scene lighting, in a collapsible panel (hidden by default — mostly
            # fine-tuning / dev). The base rig does the heavy lifting: the light
            # kit (toggle), an ambient floor that lifts faces angled away from
            # the lights, and diffuse for directional shading. Settings persist
            # globally (see _save_settings).
            with v3.VExpansionPanels(variant="accordion", flat=True, classes="mt-3"):
                with v3.VExpansionPanel():
                    with v3.VExpansionPanelTitle(classes="text-caption pa-2"):
                        v3.VIcon("mdi-lightbulb-on-outline", size="small", classes="mr-2")
                        html.Span("Lighting")
                    with v3.VExpansionPanelText():
                        _switch("light_kit", "Light kit")
                        _slider("light_ambient", "Ambient", 0.0, 1.0, 0.05)
                        _slider("light_diffuse", "Diffuse", 0.0, 1.0, 0.05)

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
            # Position along the active axis. The slider previews live (the red
            # frame follows) and commits on release; the X/Y/Z fields below are
            # the source of truth and take effect on Apply.
            with html.Div(classes="d-flex justify-space-between mt-1"):
                html.Span("Position", classes="text-caption text-medium-emphasis")
                html.Span(
                    "{{ Number(plane_slider).toFixed(2) }} m", classes="text-caption"
                )
            v3.VSlider(
                v_model=("plane_slider",),
                min=("axis_min",),
                max=("axis_max",),
                step=("Math.max((axis_max - axis_min) / 500, 0.001)",),
                start=(self.ctrl.plane_drag_start,),
                end=(self.ctrl.plane_slider_release,),
                hide_details=True,
                density="compact",
                color="primary",
                thumb_size=12,
                classes="js-plane-slider",
            )
            with html.Div(classes="d-flex mt-2", style="gap: 6px"):
                v3.VTextField(v_model_number=("plane_x", 0.0), label="X", type="number",
                              classes="js-plane-x", **_FIELD)
                v3.VTextField(v_model_number=("plane_y", 0.0), label="Y", type="number", **_FIELD)
                v3.VTextField(v_model_number=("plane_z", 0.0), label="Z", type="number", **_FIELD)
            v3.VBtn(
                "Apply",
                block=True,
                variant="tonal",
                size="small",
                classes="mt-2 js-plane-apply",
                click=self.ctrl.plane_apply,
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

    def _tool_geometry(self, title, icon):
        """Building geometry from constant/triSurface/building.obj."""
        with _section(title, icon):
            html.Div(
                "No building.obj (or building<N>.obj) in constant/triSurface.",
                v_if="!has_geometry",
                classes="text-caption text-medium-emphasis mb-2",
            )
            _switch("geometry_visible", "Show geometry")
            with v3.VBtnToggle(
                v_model=("geometry_mode", "features"),
                mandatory=True,
                density="compact",
                variant="outlined",
                divided=True,
                classes="mb-3",
            ):
                v3.VBtn("Feature edges", value="features", size="small")
                v3.VBtn("Wireframe", value="wireframe", size="small")
            _slider("geometry_opacity", "Opacity", 0.0, 1.0, 0.05)
            _slider("geometry_line_width", "Line width", 0.5, 6.0, 0.5)

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
                # Push the server camera to the client: in local (vtk.js) mode the
                # client owns its camera, so a preset orientation set server-side
                # is invisible until pushed (this is why the view buttons never
                # worked -- reset_camera only refit the client's own orientation).
                self.ctrl.view_push_camera = view.push_camera
                # NB: do NOT push_remote_camera_on_end_interaction() here. In local
                # mode that observer fires on every EndInteraction (mouse up / leave
                # canvas) and setCamera()s the SERVER camera onto the client, which
                # re-applies the focal point and resets the client's center of
                # rotation -- so orbiting felt broken and needed constant R. The
                # server camera already tracks the client in local mode (that's how
                # the Client→Server switch and the report screenshot work), so it
                # bought nothing anyway.

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
                # Stable class (js-view-px, js-view-mx, ...) so a keyboard
                # shortcut can trigger the button (see KEY_SHORTCUTS / _KEY_JS).
                token = direction.replace("+", "p").replace("-", "m")
                v3.VBtn(
                    label,
                    size="small",
                    variant="tonal",
                    classes=f"mx-0 px-2 js-view-{token}",
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
/* Overlay chrome uses Vuetify's theme CSS vars (surface / on-surface), so the
   floating boxes follow the <VApp :theme> light/dark switch automatically. */
.foamviz-stage { position: relative; width: 100%; height: 100%; }
.foamviz-legend {
  position: absolute; left: 18px; bottom: 18px; z-index: 5;
  background: rgba(var(--v-theme-surface), .82);
  border: 1px solid rgba(var(--v-theme-on-surface), .12);
  border-radius: 8px; padding: 10px 12px; backdrop-filter: blur(6px);
  color: rgb(var(--v-theme-on-surface)); font-size: 11px; line-height: 1.5;
  pointer-events: none;
}
.foamviz-legend-title { font-weight: 600; letter-spacing: .03em; margin-bottom: 6px; }
.foamviz-legend-body { display: flex; gap: 8px; }
.foamviz-legend-bar {
  width: 14px; height: 150px; border-radius: 3px;
  border: 1px solid rgba(var(--v-theme-on-surface), .3);
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
.foamviz-mode {
  position: absolute; right: 18px; bottom: 18px; z-index: 5;
  background: rgba(var(--v-theme-surface), .82);
  border: 1px solid rgba(var(--v-theme-on-surface), .12);
  border-radius: 10px; padding: 4px; backdrop-filter: blur(6px);
}
.foamviz-bottombar {
  position: absolute; left: 50%; bottom: 18px; transform: translateX(-50%);
  z-index: 5; display: flex; align-items: center; gap: 4px;
  background: rgba(var(--v-theme-surface), .82);
  border: 1px solid rgba(var(--v-theme-on-surface), .12);
  border-radius: 10px; padding: 6px 10px; backdrop-filter: blur(6px);
}
.foamviz-section { margin: 10px 6px; }
.foamviz-section-head {
  display: flex; align-items: center; padding: 10px 4px 6px;
  font-size: 12px; font-weight: 600; letter-spacing: .02em;
  color: rgb(var(--v-theme-on-surface));
  border-top: 1px solid rgba(var(--v-theme-on-surface), .09);
}
.foamviz-section-body { padding: 2px 4px 6px; }
"""
