"""Reading an OpenFOAM case with VTK's native reader.

The reader is kept behind this class for one reason: everything downstream
wants a *snapshot* (a concrete dataset for one instant in time), not a live
pipeline connection. Requesting a time step through a connected pipeline means
every downstream ``Update()`` re-negotiates the time request, and it is easy to
silently fall back to t=0. So we pull the time step here, shallow-copy the
result, and hand the rest of the app plain data objects.
"""

from pathlib import Path

import numpy as np
import vtk
from vtkmodules.util.numpy_support import vtk_to_numpy

# The reader reports patches with a "patch/" or "group/" prefix. Groups overlap
# with the individual patches they contain, so we only ever expose real patches.
PATCH_PREFIX = "patch/"
INTERNAL_MESH = "internalMesh"


def find_cases(root):
    """Directories under *root* that look like OpenFOAM cases.

    A case is recognised by having ``system/controlDict``; this is the same
    test the OpenFOAM utilities themselves effectively use.
    """
    root = Path(root)
    if not root.is_dir():
        return []

    found = []
    if (root / "system" / "controlDict").is_file():
        found.append(root)
    for entry in sorted(root.iterdir()):
        if entry.is_dir() and (entry / "system" / "controlDict").is_file():
            found.append(entry)
    return found


def ensure_foam_stub(case_dir):
    """VTK's reader is driven by a ``*.foam`` marker file; create one if absent.

    The file is empty by design -- it only tells the reader which directory is
    the case root, exactly as ParaView's ``paraFoam -builtin`` does.
    """
    case_dir = Path(case_dir)
    existing = sorted(case_dir.glob("*.foam"))
    if existing:
        return existing[0]
    stub = case_dir / "case.foam"
    stub.touch()
    return stub


class FoamCase:
    """One OpenFOAM case: time values, fields, patches, and time snapshots."""

    def __init__(self, case_dir):
        self.case_dir = Path(case_dir).resolve()
        self.name = self.case_dir.name
        self.foam_file = ensure_foam_stub(self.case_dir)

        self.decomposed = False  # set by _make_reader() from the case layout
        self.reader = self._open_reader()
        self.times = self._read_times()
        self.patches = self._read_patches()

        # Populated on the first snapshot, once we can see the real arrays
        # rather than the reader's advertised list (which includes fields such
        # as ``T.orig`` that never make it into the output).
        self.fields = {}
        self._loaded_time = None
        self._loaded_patches = None

        self.internal = None  # vtkUnstructuredGrid
        self.boundary = {}  # patch name -> vtkPolyData

    # -- reader / metadata ------------------------------------------------

    def _open_reader(self):
        reader = self._make_reader()
        reader.SetFileName(str(self.foam_file))
        # Interpolate cell values to points: contouring and stream tracing both
        # need point data, and it makes the surface colouring smooth.
        reader.SetCreateCellToPoint(1)
        reader.SetSkipZeroTime(0)
        reader.UpdateInformation()    # discover the time steps and available arrays
        reader.EnableAllCellArrays()  # then enable everything that was discovered
        return reader

    def _make_reader(self):
        """Pick the reader for the case's current state: the serial
        vtkOpenFOAMReader normally, or the parallel vtkPOpenFOAMReader in
        decomposed mode when the newest time only exists in processor* dirs —
        i.e. what the backend reports as case_info.time_in == 'parallel'.
        vtkPOpenFOAMReader is a vtkOpenFOAMReader subclass, so the rest of this
        class is unchanged; it reads all processor* dirs on a single process."""
        self.decomposed = self._is_decomposed()
        if self.decomposed:
            reader = vtk.vtkPOpenFOAMReader()
            reader.SetCaseType(vtk.vtkPOpenFOAMReader.DECOMPOSED_CASE)
            return reader
        return vtk.vtkOpenFOAMReader()

    def _is_decomposed(self):
        """True when the newest available time lives only in (or later in) the
        processor* directories rather than reconstructed in the case root."""

        def latest(dirs):
            times = []
            for d in dirs:
                try:
                    times.append(float(d.name))
                except ValueError:
                    pass  # constant, system, processor*, geometry, ...
            return max(times) if times else None

        proc0 = self.case_dir / "processor0"
        if not proc0.is_dir():
            return False
        proc_latest = latest(p for p in proc0.iterdir() if p.is_dir())
        if proc_latest is None:
            return False
        root_latest = latest(p for p in self.case_dir.iterdir() if p.is_dir())
        return root_latest is None or proc_latest > root_latest

    def refresh_times(self):
        """Re-scan the case for time directories written since it was opened.

        vtkOpenFOAMReader reads the time list once (at UpdateInformation) and
        never notices new steps, so the surest refresh is a fresh reader. Cheap:
        only the time list is re-read here, not the mesh. The load cache is kept,
        so re-opening an unchanged case costs nothing — the caller decides whether
        a mesh reload is actually needed."""
        self.reader = self._open_reader()
        self.times = self._read_times()
        self.patches = self._read_patches()
        return self.times

    def _read_times(self):
        tv = self.reader.GetTimeValues()
        if tv is None or tv.GetNumberOfTuples() == 0:
            return [0.0]
        return [tv.GetValue(i) for i in range(tv.GetNumberOfTuples())]

    def _read_patches(self):
        names = []
        for i in range(self.reader.GetNumberOfPatchArrays()):
            raw = self.reader.GetPatchArrayName(i)
            if raw.startswith(PATCH_PREFIX):
                names.append(raw[len(PATCH_PREFIX):])
        return names

    # -- loading ----------------------------------------------------------

    def load(self, time, patches=None, force=False):
        """Read one time step. Returns True if anything was actually re-read.
        ``force`` re-reads even if the same time/patches are already loaded (the
        refresh button, in case the step was overwritten in place)."""
        patches = list(patches if patches is not None else self.patches)
        if not force and self._loaded_time == time and self._loaded_patches == patches:
            return False

        self.reader.SetPatchArrayStatus(INTERNAL_MESH, 1)
        for name in self.patches:
            self.reader.SetPatchArrayStatus(
                PATCH_PREFIX + name, 1 if name in patches else 0
            )
        # Groups would duplicate the geometry of their member patches.
        for i in range(self.reader.GetNumberOfPatchArrays()):
            raw = self.reader.GetPatchArrayName(i)
            if raw.startswith("group/"):
                self.reader.SetPatchArrayStatus(raw, 0)

        self.reader.UpdateTimeStep(float(time))
        self._split_blocks(self.reader.GetOutput())

        self._loaded_time = time
        self._loaded_patches = patches
        return True

    def _split_blocks(self, multiblock):
        """Flatten the reader's multiblock output into internal mesh + patches."""
        self.internal = None
        self.boundary = {}

        it = multiblock.NewTreeIterator()
        it.VisitOnlyLeavesOn()
        it.InitTraversal()
        while not it.IsDoneWithTraversal():
            data = it.GetCurrentDataObject()
            name = None
            if it.HasCurrentMetaData():
                name = it.GetCurrentMetaData().Get(vtk.vtkCompositeDataSet.NAME())

            if name == INTERNAL_MESH:
                grid = vtk.vtkUnstructuredGrid()
                grid.ShallowCopy(data)
                self.internal = grid
            elif name is not None and data.IsA("vtkPolyData"):
                poly = vtk.vtkPolyData()
                poly.ShallowCopy(data)
                self.boundary[name] = poly
            it.GoToNextItem()

        if self.internal is not None:
            self.fields = self._describe_fields(self.internal)

    @staticmethod
    def _describe_fields(dataset):
        """Map field name -> number of components, from the point data itself."""
        pd = dataset.GetPointData()
        info = {}
        for i in range(pd.GetNumberOfArrays()):
            arr = pd.GetArray(i)
            if arr is None or arr.GetName() is None:
                continue
            info[arr.GetName()] = arr.GetNumberOfComponents()
        return info

    # -- derived quantities ----------------------------------------------

    @property
    def scalar_fields(self):
        return sorted(n for n, c in self.fields.items() if c == 1)

    @property
    def vector_fields(self):
        return sorted(n for n, c in self.fields.items() if c == 3)

    def vector_field_available(self, name):
        return bool(name) and self.fields.get(name) == 3

    def bounds(self):
        if self.internal is None:
            return (0, 1, 0, 1, 0, 1)
        return self.internal.GetBounds()

    def n_cells(self):
        return self.internal.GetNumberOfCells() if self.internal else 0

    def n_points(self):
        return self.internal.GetNumberOfPoints() if self.internal else 0

    def datasets(self):
        """Every loaded dataset: internal mesh first, then boundary patches."""
        out = []
        if self.internal is not None:
            out.append(self.internal)
        out.extend(self.boundary.values())
        return out

    def field_range(self, field, component, robust=False):
        """Range of *field* (see :func:`derive_scalars`) over all loaded blocks.

        With *robust*, clip to the 1st-99th percentile. CFD results routinely
        contain a small, very extreme region -- a heater surface, a stagnation
        point -- that flattens the colour map everywhere else; the percentile
        range keeps the bulk of the field readable.
        """
        chunks = []
        for ds in self.datasets():
            arr = ds.GetPointData().GetArray(field)
            if arr is None:
                continue
            values = derive_scalars(vtk_to_numpy(arr), component)
            if values.size:
                chunks.append(values)
        if not chunks:
            return (0.0, 1.0)

        allvals = np.concatenate(chunks)
        allvals = allvals[np.isfinite(allvals)]
        if allvals.size == 0:
            return (0.0, 1.0)

        if robust:
            lo, hi = np.percentile(allvals, [1.0, 99.0])
        else:
            lo, hi = allvals.min(), allvals.max()
        return (float(lo), float(hi))


def derive_scalars(values, component):
    """Reduce an Nx1 or Nx3 array to the scalars selected by *component*.

    *component* is ``"magnitude"`` or ``"x"``/``"y"``/``"z"``. Scalar fields
    ignore it entirely.
    """
    if values.ndim == 1 or values.shape[1] == 1:
        return values.reshape(-1)
    if component == "magnitude":
        return np.linalg.norm(values, axis=1)
    index = {"x": 0, "y": 1, "z": 2}.get(component, 0)
    return values[:, index]
