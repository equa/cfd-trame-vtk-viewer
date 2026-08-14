"""The VTK scene: representations, colouring, and the render window.

Design notes
------------
*Colouring is baked into a derived array.* Rather than asking the mapper to
take the magnitude or the Y-component of a vector at render time, we compute a
plain scalar array (``COLOR_ARRAY``) and colour by that. Vector modes on a
lookup table are a rendering-side concept that does not survive the trip to
vtk.js, so baking the scalars keeps remote and local rendering pixel-identical
and makes the data range trivially correct.

*The slice plane is the hub.* Stream tracer seeds and vector glyphs are both
placed on the cut plane by default, so moving one slider moves everything that
depends on it. For room airflow that matches how people actually look at a
result: pick a plane, then ask what the air is doing on it.
"""

import numpy as np
import vtk
from vtkmodules.util.numpy_support import numpy_to_vtk, vtk_to_numpy

from . import colors
from .case import derive_scalars

COLOR_ARRAY = "FoamVizColor"

AXES = ["x", "y", "z"]
_AXIS_NORMAL = {"x": (1, 0, 0), "y": (0, 1, 0), "z": (0, 0, 1)}


class FoamPipeline:
    """Owns the renderer and every representation drawn into it."""

    def __init__(self):
        self.case = None
        self.color_field = None
        self.color_component = "magnitude"
        self.vector_field = None
        self.color_range = (0.0, 1.0)
        self.preset = "coolwarm"
        # Colour the surface/slice by true cell values (flat per cell) rather
        # than the reader's point-interpolated (smooth) values.
        self.use_cell_data = False
        # 0 = smooth colour map; >0 bands it into that many discrete colours.
        self.n_colors = 0

        self._build_scene()
        self._build_filters()

    # -- construction -----------------------------------------------------

    def _build_scene(self):
        self.renderer = vtk.vtkRenderer()
        self.renderer.SetBackground(0.09, 0.10, 0.13)
        self.renderer.SetBackground2(0.17, 0.19, 0.24)
        self.renderer.GradientBackgroundOn()

        self.render_window = vtk.vtkRenderWindow()
        self.render_window.SetOffScreenRendering(1)
        self.render_window.AddRenderer(self.renderer)
        self.render_window.SetSize(1200, 800)
        self.render_window.SetMultiSamples(0)

        self.interactor = vtk.vtkRenderWindowInteractor()
        self.interactor.SetRenderWindow(self.render_window)
        self.interactor.SetInteractorStyle(vtk.vtkInteractorStyleTrackballCamera())

        self.lut = colors.color_transfer_function(self.preset, 0.0, 1.0)

    def _build_filters(self):
        # --- boundary surface -------------------------------------------
        self.surface_input = vtk.vtkAppendPolyData()
        self.surface_clip = vtk.vtkClipPolyData()
        self.surface_clip.SetInputConnection(self.surface_input.GetOutputPort())
        self.surface_clip.SetClipFunction(vtk.vtkPlane())
        self.surface_clip.InsideOutOn()

        self.surface_actor, self.surface_mapper = self._make_actor()
        self.surface_actor.GetProperty().SetColor(0.72, 0.75, 0.80)

        # --- slice -------------------------------------------------------
        self.cutter = vtk.vtkCutter()
        self.cutter.SetCutFunction(vtk.vtkPlane())
        # Crinkle slice: the whole cells the plane passes through (the true mesh
        # layer) rather than a flat triangulated cut. vtk3DLinearGridCrinkleExtractor
        # is VTK's purpose-built, threaded crinkle filter -- fast enough for the
        # big case; it needs a 3D *linear* grid, which the reader gives us (it
        # decomposes polyhedra by default). It shares the cutter's plane, so the
        # position slider moves both. vtkGeometryFilter turns its unstructured
        # output into polydata for the shared slice mapper.
        self.crinkle = vtk.vtk3DLinearGridCrinkleExtractor()
        self.crinkle.SetImplicitFunction(self.cutter.GetCutFunction())
        self.crinkle.SetCopyCellData(True)
        self.crinkle.SetCopyPointData(True)
        self.crinkle_surface = vtk.vtkGeometryFilter()
        self.crinkle_surface.SetInputConnection(self.crinkle.GetOutputPort())

        self.slice_actor, self.slice_mapper = self._make_actor()
        self.slice_mapper.SetInputConnection(self.cutter.GetOutputPort())
        # A cut plane is read quantitatively against the colour bar, so shading
        # it only corrupts the reading -- and the cutter emits no normals, which
        # the two renderers then guess at differently.
        self.slice_actor.GetProperty().LightingOff()

        # Plane outline: an amber frame at the cut plane. It updates live while
        # the position slider is dragged (cheap -- four points, no recut), so it
        # previews where the debounced slice will land on release, and it marks
        # the seeding plane from any tool. Just four corner points + a line loop.
        self.plane_outline = vtk.vtkPolyData()
        pts = vtk.vtkPoints()
        pts.SetNumberOfPoints(4)
        self.plane_outline.SetPoints(pts)
        loop = vtk.vtkCellArray()
        ids = vtk.vtkIdList()
        for i in (0, 1, 2, 3, 0):
            ids.InsertNextId(i)
        loop.InsertNextCell(ids)
        self.plane_outline.SetLines(loop)
        self.plane_outline_actor, self.plane_outline_mapper = self._make_actor(
            scalar_visibility=False
        )
        self.plane_outline_mapper.SetInputData(self.plane_outline)
        pop = self.plane_outline_actor.GetProperty()
        pop.SetColor(0.95, 0.75, 0.20)
        pop.SetLineWidth(2)
        pop.LightingOff()
        # Always within the domain (a cross-section), so keep it out of
        # ResetCamera -- and out of the way before it is first positioned, when
        # its four points still sit at the origin.
        self.plane_outline_actor.SetUseBounds(False)

        # --- isosurface ---------------------------------------------------
        self.contour = vtk.vtkContourFilter()
        self.contour.SetInputArrayToProcess(
            0, 0, 0, vtk.vtkDataObject.FIELD_ASSOCIATION_POINTS, COLOR_ARRAY
        )
        self.contour_normals = vtk.vtkPolyDataNormals()
        self.contour_normals.SetInputConnection(self.contour.GetOutputPort())
        self.contour_normals.SetFeatureAngle(60)
        self.contour_actor, self.contour_mapper = self._make_actor()
        self.contour_mapper.SetInputConnection(self.contour_normals.GetOutputPort())

        # --- streamlines ---------------------------------------------------
        self.stream_seeds = vtk.vtkMaskPoints()
        self.stream_seeds.RandomModeOn()
        self.stream_seeds.SetRandomModeType(1)  # spatially even, not clumped
        self.stream_seeds.SetInputConnection(self.cutter.GetOutputPort())

        self.tracer = vtk.vtkStreamTracer()
        self.tracer.SetSourceConnection(self.stream_seeds.GetOutputPort())
        self.tracer.SetIntegratorTypeToRungeKutta45()
        self.tracer.SetIntegrationDirectionToBoth()
        self.tracer.SetInitialIntegrationStep(0.2)
        self.tracer.SetMaximumNumberOfSteps(2000)

        self.stream_tube = vtk.vtkTubeFilter()
        self.stream_tube.SetInputConnection(self.tracer.GetOutputPort())
        self.stream_tube.SetNumberOfSides(8)
        self.stream_tube.CappingOn()

        self.stream_actor, self.stream_mapper = self._make_actor()
        self.stream_mapper.SetInputConnection(self.stream_tube.GetOutputPort())

        # --- vector glyphs --------------------------------------------------
        self.glyph_seeds = vtk.vtkMaskPoints()
        self.glyph_seeds.RandomModeOn()
        self.glyph_seeds.SetRandomModeType(1)

        # Keep an explicit reference to the arrow: handing VTK a temporary
        # (`SetSourceConnection(vtkArrowSource().GetOutputPort())`) lets Python
        # collect it while the pipeline still points at it, and segfaults.
        self.glyph_source = vtk.vtkArrowSource()
        self.glyph_source.SetTipResolution(12)
        self.glyph_source.SetShaftResolution(12)

        self.glyph = vtk.vtkGlyph3D()
        self.glyph.SetSourceConnection(self.glyph_source.GetOutputPort())
        self.glyph.SetInputConnection(self.glyph_seeds.GetOutputPort())
        self.glyph.SetVectorModeToUseVector()
        self.glyph.SetScaleModeToScaleByVector()
        self.glyph.SetColorModeToColorByScalar()
        self.glyph.OrientOn()

        self.glyph_actor, self.glyph_mapper = self._make_actor()
        self.glyph_mapper.SetInputConnection(self.glyph.GetOutputPort())

        # --- static context: domain outline and an RGB orientation triad ---
        self.outline = vtk.vtkOutlineFilter()
        self.outline_actor, self.outline_mapper = self._make_actor(scalar_visibility=False)
        self.outline_mapper.SetInputConnection(self.outline.GetOutputPort())
        self.outline_actor.GetProperty().SetColor(0.55, 0.58, 0.64)
        self.outline_actor.GetProperty().SetLineWidth(1.5)

        self.triad_actors = self._build_triad()

        for actor in (
            self.surface_actor,
            self.slice_actor,
            self.plane_outline_actor,
            self.contour_actor,
            self.stream_actor,
            self.glyph_actor,
            self.outline_actor,
            *self.triad_actors,
        ):
            self.renderer.AddActor(actor)

    def _make_actor(self, scalar_visibility=True):
        mapper = vtk.vtkPolyDataMapper()
        mapper.SetScalarVisibility(1 if scalar_visibility else 0)
        if scalar_visibility:
            mapper.SetScalarModeToUsePointFieldData()
            mapper.SelectColorArray(COLOR_ARRAY)
            mapper.SetLookupTable(self.lut)
            mapper.SetColorModeToMapScalars()
            mapper.InterpolateScalarsBeforeMappingOn()
        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        return actor, mapper

    def _build_triad(self):
        """Three RGB arrows for X/Y/Z, as ordinary geometry.

        An orientation-marker widget would be the usual choice, but widgets do
        not survive serialisation to the browser-side renderer, so the triad is
        built from plain actors that render identically in both modes.
        """
        actors = []
        # Keep every stage referenced: see the note in _build_filters about
        # handing VTK a temporary.
        self._triad_sources = []
        self._triad_transforms = []
        self._triad_filters = []

        for rgb in [(0.90, 0.30, 0.30), (0.40, 0.85, 0.40), (0.35, 0.55, 0.95)]:
            arrow = vtk.vtkArrowSource()
            arrow.SetTipResolution(16)
            arrow.SetShaftResolution(16)
            # Chunky on purpose: at typical zoom a default-proportioned arrow is
            # a hairline that disappears against the domain outline.
            arrow.SetShaftRadius(0.05)
            arrow.SetTipRadius(0.15)
            arrow.SetTipLength(0.3)

            # Bake position/scale/rotation into the geometry instead of setting
            # them on the actor. Actor-level transforms are not reliably carried
            # across to the browser-side renderer, and a triad that only agrees
            # with itself in one render mode is worse than none.
            transform = vtk.vtkTransform()
            placed = vtk.vtkTransformPolyDataFilter()
            placed.SetInputConnection(arrow.GetOutputPort())
            placed.SetTransform(transform)

            mapper = vtk.vtkPolyDataMapper()
            mapper.SetInputConnection(placed.GetOutputPort())
            actor = vtk.vtkActor()
            actor.SetMapper(mapper)
            actor.GetProperty().SetColor(*rgb)
            actor.GetProperty().SetAmbient(0.45)
            actor.GetProperty().SetDiffuse(0.75)
            # Left inside ResetCamera's bounds deliberately: excluding it framed
            # the domain so tightly that the triad fell off the bottom of the
            # viewport. It is small enough that including it costs ~10% zoom.
            actor.SetUseBounds(True)

            self._triad_sources.append(arrow)
            self._triad_transforms.append(transform)
            self._triad_filters.append(placed)
            actors.append(actor)
        return actors

    def _place_triad(self):
        """Anchor the triad clear of the domain's lower corner, sized to it."""
        xmin, xmax, ymin, ymax, zmin, zmax = self.case.bounds()
        diagonal = self.diagonal()
        length = 0.10 * diagonal
        # Anchor on the corner nearest the default iso camera (+x, -y, +z). At
        # the opposite corner the triad sits behind the geometry, where an
        # opaque slice hides two of its three arrows.
        origin = (xmax, ymin, zmax)

        # vtkTransform pre-multiplies, so the calls read outermost-first:
        # rotate the +X arrow onto its axis, scale it, then move it into place.
        for axis, transform in zip(AXES, self._triad_transforms):
            transform.Identity()
            transform.Translate(*origin)
            transform.Scale(length, length, length)
            if axis == "y":
                transform.RotateZ(90)
            elif axis == "z":
                transform.RotateY(-90)

    # -- case handling ----------------------------------------------------

    def set_case(self, case):
        self.case = case
        self.vector_field = "U" if "U" in case.vector_fields else (
            case.vector_fields[0] if case.vector_fields else None
        )
        # Velocity magnitude is the useful opening view for room airflow: it is
        # well spread over the domain, whereas temperature in a heated room sits
        # in a narrow band with one extreme patch and reads as a flat wash.
        if self.vector_field:
            self.color_field = self.vector_field
        elif case.scalar_fields:
            self.color_field = case.scalar_fields[0]
        else:
            self.color_field = next(iter(case.fields), None)

    def diagonal(self):
        xmin, xmax, ymin, ymax, zmin, zmax = self.case.bounds()
        return float(np.linalg.norm([xmax - xmin, ymax - ymin, zmax - zmin])) or 1.0

    def update_data(self):
        """Re-attach the loaded datasets to every filter. Call after a reload."""
        case = self.case
        if case is None or case.internal is None:
            return

        self.apply_color_array()

        if case.vector_field_available(self.vector_field):
            case.internal.GetPointData().SetActiveVectors(self.vector_field)

        self.surface_input.RemoveAllInputs()
        for poly in case.boundary.values():
            self.surface_input.AddInputData(poly)
        # An empty append filter is an error, not an empty result.
        if not case.boundary:
            self.surface_input.AddInputData(vtk.vtkPolyData())

        self.cutter.SetInputData(case.internal)
        self.crinkle.SetInputData(case.internal)
        self.contour.SetInputData(case.internal)
        self.tracer.SetInputData(case.internal)
        self.outline.SetInputData(case.internal)
        self._place_triad()

    def apply_color_array(self):
        """Bake the selected field/component into ``COLOR_ARRAY``.

        Point data is always baked: isosurfaces, streamlines, glyphs and the
        smooth surface/slice colouring all read the point array. Cell data is
        baked too only when :attr:`use_cell_data` is set, so the surface and
        slice can show true, un-interpolated cell values (the cutter carries
        cell data through to the cut faces)."""
        if self.case is None or not self.color_field:
            return
        for dataset in self.case.datasets():
            self._bake_color(dataset.GetPointData())
            if self.use_cell_data:
                self._bake_color(dataset.GetCellData())

    def _bake_color(self, attr):
        """Bake ``COLOR_ARRAY`` into one attribute set (point or cell data)."""
        source = attr.GetArray(self.color_field)
        if source is None:
            return
        scalars = derive_scalars(vtk_to_numpy(source), self.color_component)
        baked = numpy_to_vtk(np.ascontiguousarray(scalars, dtype=np.float64), deep=1)
        baked.SetName(COLOR_ARRAY)
        attr.RemoveArray(COLOR_ARRAY)
        attr.AddArray(baked)
        attr.SetActiveScalars(COLOR_ARRAY)

    def _color_by_association(self, mapper):
        """Point the mapper at ``COLOR_ARRAY`` in point or cell data per the
        current :attr:`use_cell_data`. Only the surface and slice honour the
        toggle; the derived filters always need point data."""
        if self.use_cell_data:
            mapper.SetScalarModeToUseCellFieldData()
        else:
            mapper.SetScalarModeToUsePointFieldData()
        mapper.SelectColorArray(COLOR_ARRAY)

    # -- appearance -------------------------------------------------------

    def set_color_range(self, vmin, vmax):
        self.color_range = (vmin, vmax)
        new_lut = colors.color_transfer_function(self.preset, vmin, vmax, self.n_colors)
        self.lut.DeepCopy(new_lut)
        for mapper in (
            self.surface_mapper,
            self.slice_mapper,
            self.contour_mapper,
            self.stream_mapper,
            self.glyph_mapper,
        ):
            mapper.SetScalarRange(vmin, vmax)

    def set_preset(self, preset):
        self.preset = preset
        self.set_color_range(*self.color_range)

    def autoscale(self, robust=False):
        lo, hi = self.case.field_range(self.color_field, self.color_component, robust)
        self.set_color_range(lo, hi)
        return lo, hi

    # -- representations ---------------------------------------------------

    def _plane_position(self, axis, fraction):
        """World coordinate of the plane along *axis* at *fraction* of the bounds.
        Nudged off the exact boundary: a cut on the outer face is degenerate."""
        lo, hi = self._axis_range(axis)
        return lo + (hi - lo) * min(max(fraction, 0.001), 0.999)

    def _axis_range(self, axis):
        b = self.case.bounds()  # xmin, xmax, ymin, ymax, zmin, zmax
        i = "xyz".index(axis)
        return b[2 * i], b[2 * i + 1]

    def update_plane(self, axis, fraction):
        """Position the shared cut/clip plane along *axis* at *fraction* of the bounds."""
        xmin, xmax, ymin, ymax, zmin, zmax = self.case.bounds()
        pos = self._plane_position(axis, fraction)

        origin = [(xmin + xmax) / 2, (ymin + ymax) / 2, (zmin + zmax) / 2]
        origin["xyz".index(axis)] = pos
        normal = _AXIS_NORMAL[axis]

        for plane in (self.cutter.GetCutFunction(), self.surface_clip.GetClipFunction()):
            plane.SetOrigin(*origin)
            plane.SetNormal(*normal)

    def update_plane_outline(self, axis, fraction):
        """Move the amber plane frame to *fraction* along *axis* -- four points,
        no recut, so it is cheap enough to follow a slider drag live."""
        pos = self._plane_position(axis, fraction)
        ai = "xyz".index(axis)
        others = [i for i in range(3) if i != ai]
        b = self.case.bounds()
        u = (b[2 * others[0]], b[2 * others[0] + 1])
        v = (b[2 * others[1]], b[2 * others[1] + 1])

        pts = self.plane_outline.GetPoints()
        for k, (uu, vv) in enumerate([(u[0], v[0]), (u[1], v[0]), (u[1], v[1]), (u[0], v[1])]):
            corner = [0.0, 0.0, 0.0]
            corner[ai] = pos
            corner[others[0]] = uu
            corner[others[1]] = vv
            pts.SetPoint(k, *corner)
        pts.Modified()

    def update_surface(self, visible, colored, opacity, edges, clip, cull):
        self.surface_actor.SetVisibility(1 if visible else 0)
        self.surface_mapper.SetInputConnection(
            self.surface_clip.GetOutputPort() if clip else self.surface_input.GetOutputPort()
        )
        self.surface_mapper.SetScalarVisibility(1 if colored else 0)
        self._color_by_association(self.surface_mapper)
        prop = self.surface_actor.GetProperty()
        prop.SetOpacity(opacity)
        prop.SetEdgeVisibility(1 if edges else 0)
        prop.SetEdgeColor(0.25, 0.27, 0.32)
        # Cull the camera-facing walls so you can see into the room.
        prop.SetFrontfaceCulling(1 if cull else 0)

    def update_slice(self, visible, edges):
        self.slice_actor.SetVisibility(1 if visible else 0)
        self._color_by_association(self.slice_mapper)
        # Showing the mesh means showing the true cell layer (crinkle) rather
        # than the flat triangulated cut -- otherwise "mesh edges" would draw the
        # cutter's triangulation, which is not the real mesh.
        self.slice_mapper.SetInputConnection(
            self.crinkle_surface.GetOutputPort() if edges else self.cutter.GetOutputPort()
        )
        prop = self.slice_actor.GetProperty()
        prop.SetEdgeVisibility(1 if edges else 0)
        prop.SetEdgeColor(0.2, 0.2, 0.24)
        prop.SetLineWidth(1)

    def update_contour(self, visible, n_values, opacity):
        self.contour_actor.SetVisibility(1 if visible else 0)
        self.contour_actor.GetProperty().SetOpacity(opacity)
        if not visible:
            return
        lo, hi = self.color_range
        # Interior values only: an isosurface exactly at the data extreme is
        # either empty or coincident with the boundary.
        self.contour.SetNumberOfContours(n_values)
        for i in range(n_values):
            f = (i + 1) / (n_values + 1)
            self.contour.SetValue(i, lo + (hi - lo) * f)

    def update_streamlines(self, visible, n_seeds, radius_scale, max_length):
        self.stream_actor.SetVisibility(1 if visible else 0)
        if not visible:
            return
        self.stream_seeds.SetMaximumNumberOfPoints(n_seeds)
        diagonal = self.diagonal()
        self.tracer.SetMaximumPropagation(diagonal * max_length)
        self.tracer.SetIntegrationStepUnit(vtk.vtkStreamTracer.CELL_LENGTH_UNIT)
        self.stream_tube.SetRadius(diagonal * 0.0015 * radius_scale)

    def update_glyphs(self, visible, source, n_glyphs, scale, scale_by_magnitude):
        self.glyph_actor.SetVisibility(1 if visible else 0)
        if not visible:
            return
        if source == "slice":
            self.glyph_seeds.SetInputConnection(self.cutter.GetOutputPort())
        else:
            self.glyph_seeds.SetInputData(self.case.internal)
        self.glyph_seeds.SetMaximumNumberOfPoints(n_glyphs)

        # Room airflow spans orders of magnitude -- a plume core moving 100x
        # faster than the quiescent bulk. Scaling arrow length by speed makes
        # everything outside the plume vanish, so uniform-length arrows (which
        # still carry speed in their colour) are the more readable default.
        if scale_by_magnitude:
            self.glyph.SetScaleModeToScaleByVector()
            reference = max(abs(self.color_range[1]), 1e-12)
        else:
            self.glyph.SetScaleModeToDataScalingOff()
            reference = 1.0
        self.glyph.SetScaleFactor(self.diagonal() * 0.035 * scale / reference)

    # -- camera ------------------------------------------------------------

    def reset_camera(self):
        self.renderer.ResetCamera()

    def set_view(self, direction):
        """Look along a named axis: ``+x``, ``-x``, ``+y`` ... or ``iso``."""
        camera = self.renderer.GetActiveCamera()
        if direction == "iso":
            camera.SetPosition(1, 1, 1)
            camera.SetViewUp(0, 1, 0)
        else:
            sign = -1.0 if direction.startswith("-") else 1.0
            axis = direction[-1]
            vec = [0.0, 0.0, 0.0]
            vec["xyz".index(axis)] = sign
            camera.SetPosition(*vec)
            camera.SetViewUp((0, 1, 0) if axis != "y" else (0, 0, 1))
        camera.SetFocalPoint(0, 0, 0)
        self.renderer.ResetCamera()

    def screenshot(self, path, magnification=2):
        """Write a PNG of the current view at *magnification* times the size."""
        window_to_image = vtk.vtkWindowToImageFilter()
        window_to_image.SetInput(self.render_window)
        window_to_image.SetScale(magnification)
        window_to_image.ReadFrontBufferOff()
        window_to_image.Update()

        writer = vtk.vtkPNGWriter()
        writer.SetFileName(str(path))
        writer.SetInputConnection(window_to_image.GetOutputPort())
        writer.Write()
        return path
