#!/usr/bin/env python3
"""Server-side checks for the reader and the VTK pipeline.

Run directly (no pytest needed):

    python tests/test_pipeline.py

Every representation is checked by *output size*, not by exit status. An empty
filter raises nothing and renders as a perfectly plausible blank image, so the
only useful question is whether geometry actually came out the other end.
"""

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from foamviz.case import FoamCase, derive_scalars, find_cases  # noqa: E402
from foamviz.pipeline import COLOR_ARRAY, FoamPipeline  # noqa: E402

CASE = ROOT / "data" / "hotRoom"

checks, failures = 0, []


def check(label, condition, detail=""):
    global checks
    checks += 1
    if condition:
        print(f"  ok   {label}" + (f"  ({detail})" if detail else ""))
    else:
        print(f"  FAIL {label}  {detail}")
        failures.append(label)


def main():
    if not CASE.is_dir():
        print(f"missing demo case at {CASE}; run data/hotRoom/Allrun first")
        return 1

    print("case discovery")
    found = find_cases(ROOT / "data")
    check("finds the demo case", CASE in found, f"{len(found)} case(s)")

    print("\nreader")
    case = FoamCase(CASE)
    check("reports time steps", len(case.times) > 1, f"{len(case.times)} steps")
    check("reports patches", len(case.patches) == 3, str(case.patches))

    case.load(case.times[-1])
    check("reads internal mesh", case.n_cells() == 32000, f"{case.n_cells()} cells")
    check("reads boundary patches", len(case.boundary) == 3, str(list(case.boundary)))
    check("finds T and U", {"T", "U"} <= set(case.fields), str(sorted(case.fields)))
    check("classifies U as a vector", case.fields.get("U") == 3)
    check("excludes T.orig", "T.orig" not in case.fields)

    print("\nfield ranges")
    tlo, thi = case.field_range("T", "magnitude")
    check("T spans the hot patch", tlo == 300.0 and thi > 550, f"{tlo:.1f}..{thi:.1f}")
    rlo, rhi = case.field_range("T", "magnitude", robust=True)
    check("percentile range is tighter", rhi < thi, f"{rlo:.2f}..{rhi:.2f}")
    ulo, uhi = case.field_range("U", "magnitude")
    check("speed is non-negative", ulo >= 0 and uhi > 0, f"{ulo:.4f}..{uhi:.4f}")

    print("\nderived scalars")
    vectors = np.array([[3.0, 4.0, 0.0], [0.0, 0.0, 2.0]])
    check("magnitude", np.allclose(derive_scalars(vectors, "magnitude"), [5.0, 2.0]))
    check("y component", np.allclose(derive_scalars(vectors, "y"), [4.0, 0.0]))
    check("scalars ignore component",
          np.allclose(derive_scalars(np.array([[7.0]]), "z"), [7.0]))

    print("\npipeline")
    pipe = FoamPipeline()
    pipe.set_case(case)
    pipe.update_data()
    check("defaults to a vector field", pipe.color_field == "U", pipe.color_field)
    check("bakes the colour array",
          case.internal.GetPointData().GetArray(COLOR_ARRAY) is not None)
    check("bakes it on patches too",
          all(p.GetPointData().GetArray(COLOR_ARRAY) is not None
              for p in case.boundary.values()))

    lo, hi = pipe.autoscale()
    check("autoscale matches the reader", (lo, hi) == (ulo, uhi), f"{lo:.4f}..{hi:.4f}")

    zmid = sum(case.bounds()[4:6]) / 2  # world coordinate of the mid-height cut
    pipe.update_plane("z", zmid)
    pipe.cutter.Update()
    check("slice has cells", pipe.cutter.GetOutput().GetNumberOfCells() > 1000,
          f"{pipe.cutter.GetOutput().GetNumberOfCells()} cells")

    # crinkle slice: whole cells the plane passes through (the true mesh layer)
    pipe.crinkle_surface.Update()
    crink = pipe.crinkle_surface.GetOutput()
    check("crinkle slice extracts a cell layer", crink.GetNumberOfCells() > 1000,
          f"{crink.GetNumberOfCells()} faces")
    check("crinkle carries the colour array",
          crink.GetPointData().GetArray(COLOR_ARRAY) is not None)

    # plane outline: four coplanar corners at the cut height (cheap drag preview)
    pipe.update_plane_outline("z", zmid)
    op = pipe.plane_outline.GetPoints()
    zs = {round(op.GetPoint(i)[2], 6) for i in range(4)}
    check("plane outline is planar", len(zs) == 1, str(zs))
    check("plane outline sits at the cut height", abs(next(iter(zs)) - zmid) < 1e-6)

    # building geometry (OBJ): the demo case ships a room-box building.obj.
    # One fixed actor; the mode toggles manifold edges on a single
    # vtkFeatureEdges (feature edges vs every edge) -- a filter-parameter change,
    # not scene/mapper mutation. OBJ is read lazily.
    check("case ships building.obj", pipe.has_geometry)
    check("geometry off -> OBJ not read yet",
          pipe.geometry_reader.GetOutput().GetNumberOfPoints() == 0)

    pipe.update_geometry(True, "features", 1.0, 2.0)
    pipe.geometry_edges.Update()
    n_features = pipe.geometry_edges.GetOutput().GetNumberOfCells()
    check("feature edges extracted", n_features > 0, f"{n_features} edges")
    check("geometry actor shown", pipe.geometry_actor.GetVisibility() == 1)

    pipe.update_geometry(True, "wireframe", 1.0, 2.0)
    pipe.geometry_edges.Update()
    n_all = pipe.geometry_edges.GetOutput().GetNumberOfCells()
    check("wireframe adds interior edges", n_all > n_features, f"{n_all} vs {n_features}")

    pipe.update_geometry(True, "features", 1.0, 2.0)
    pipe.geometry_edges.Update()
    check("round-trip restores feature edges", pipe.geometry_edges.GetOutput().GetNumberOfCells() == n_features,
          f"{pipe.geometry_edges.GetOutput().GetNumberOfCells()} vs {n_features}")

    pipe.update_geometry(False, "features", 1.0, 2.0)
    check("geometry off -> actor hidden", pipe.geometry_actor.GetVisibility() == 0)

    pipe.update_surface(True, True, 0.3, False, True, False)
    pipe.surface_clip.Update()
    clipped = pipe.surface_clip.GetOutput().GetNumberOfCells()
    pipe.surface_input.Update()
    whole = pipe.surface_input.GetOutput().GetNumberOfCells()
    check("clip removes part of the shell", 0 < clipped < whole, f"{clipped}/{whole}")

    # cull front faces — a render-only actor property
    pipe.update_surface(True, True, 0.3, False, False, True)
    check("cull enables front-face culling",
          pipe.surface_actor.GetProperty().GetFrontfaceCulling() == 1)
    pipe.update_surface(True, True, 0.3, False, False, False)
    check("no cull leaves front faces",
          pipe.surface_actor.GetProperty().GetFrontfaceCulling() == 0)

    # true cell values — bake a cell COLOR array and colour by it
    pipe.use_cell_data = True
    pipe.apply_color_array()
    pipe.update_surface(True, True, 0.3, False, False, False)
    check("cell mode bakes a cell colour array",
          case.internal.GetCellData().GetArray(COLOR_ARRAY) is not None)
    check("surface colours by cell data",
          pipe.surface_mapper.GetScalarModeAsString() == "UseCellFieldData")
    pipe.use_cell_data = False
    pipe.apply_color_array()
    pipe.update_surface(True, True, 0.3, False, False, False)
    check("point mode colours by point data",
          pipe.surface_mapper.GetScalarModeAsString() == "UsePointFieldData")

    # banded colour map — flat plateaus baked into the transfer-function nodes
    pipe.n_colors = 6
    pipe.set_color_range(0.0, 1.0)
    check("6 bands make 12 transfer-function nodes", pipe.lut.GetSize() == 12,
          f"{pipe.lut.GetSize()} nodes")
    mid = tuple(round(v, 3) for v in pipe.lut.GetColor(0.10))
    edge = tuple(round(v, 3) for v in pipe.lut.GetColor(0.15))
    check("colour is flat within a band", mid == edge, f"{mid} vs {edge}")
    pipe.n_colors = 0
    pipe.set_color_range(0.0, 1.0)
    check("0 bands is the smooth 256-node map", pipe.lut.GetSize() == 256,
          f"{pipe.lut.GetSize()} nodes")

    pipe.update_contour(True, 4, 0.4)
    pipe.contour.Update()
    check("isosurfaces have polygons", pipe.contour.GetOutput().GetNumberOfCells() > 0,
          f"{pipe.contour.GetOutput().GetNumberOfCells()} cells")

    pipe.update_streamlines(True, 80, 1.4, 4.0)
    pipe.tracer.Update()
    lines = pipe.tracer.GetOutput().GetNumberOfLines()
    check("streamlines integrate", lines > 20, f"{lines} lines")
    pipe.stream_tube.Update()
    check("tubes are generated", pipe.stream_tube.GetOutput().GetNumberOfPoints() > 0)

    for scale_by in (False, True):
        pipe.update_glyphs(True, "slice", 200, 1.0, scale_by)
        pipe.glyph.Update()
        check(f"glyphs build (scale_by_magnitude={scale_by})",
              pipe.glyph.GetOutput().GetNumberOfPoints() > 0,
              f"{pipe.glyph.GetOutput().GetNumberOfPoints()} pts")

    print("\ncentre-of-rotation pick (F-key focus)")
    pipe.set_view("iso")
    cam = pipe.renderer.GetActiveCamera()
    d0 = np.array(cam.GetFocalPoint()) - np.array(cam.GetPosition())
    dist0 = np.linalg.norm(d0)
    hit = pipe.pick_cor(400, 300, 800, 600)  # canvas centre -> onto the model
    check("centre pick hits geometry", hit)
    d1 = np.array(cam.GetFocalPoint()) - np.array(cam.GetPosition())
    check("focus keeps view direction", np.allclose(d0 / dist0, d1 / np.linalg.norm(d1), atol=1e-6))
    check("focus keeps zoom (distance)", np.isclose(dist0, np.linalg.norm(d1), rtol=1e-6))
    fp_before = np.array(cam.GetFocalPoint())
    miss = pipe.pick_cor(2, 2, 800, 600)  # corner -> empty space
    check("miss is a no-op", (not miss) and np.allclose(fp_before, cam.GetFocalPoint()))
    check("bad canvas size guarded", pipe.pick_cor(10, 10, 0, 0) is False)

    print("\nrendering (offscreen / OSMesa)")
    pipe.set_view("iso")
    pipe.render_window.Render()
    check("uses an offscreen GL context",
          "OSOpenGL" in pipe.render_window.GetClassName()
          or pipe.render_window.GetOffScreenRendering() == 1,
          pipe.render_window.GetClassName())

    import tempfile
    from PIL import Image

    with tempfile.TemporaryDirectory() as tmp:
        png = Path(tmp) / "shot.png"
        pipe.screenshot(png, magnification=1)
        image = Image.open(png).convert("RGB")
        distinct = len(set(image.getdata()))
    check("render is not blank", distinct > 200, f"{distinct} distinct colours")

    print("\ntime stepping")
    reloaded = case.load(case.times[0])
    check("earlier step re-reads", reloaded)
    pipe.update_data()
    zero_lo, zero_hi = case.field_range("U", "magnitude")
    check("t=0 is the still initial field", zero_hi < uhi,
          f"t0 max {zero_hi:.5f} vs final {uhi:.5f}")

    print(f"\n{checks - len(failures)}/{checks} checks passed")
    if failures:
        print("failed:", ", ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
