"""Colour map presets shared by the VTK pipeline and the HTML legend.

Both the 3D rendering and the on-screen legend are derived from the same
sampled RGB table, so what the legend promises is what the geometry shows.
"""

import matplotlib
import vtk

# Ordered so the perceptually-uniform and diverging maps come first; those are
# the ones that are actually defensible for CFD post-processing.
PRESETS = [
    ("coolwarm", "Cool to Warm"),
    ("viridis", "Viridis"),
    ("plasma", "Plasma"),
    ("inferno", "Inferno"),
    ("turbo", "Turbo"),
    ("jet", "Jet"),
    ("RdBu_r", "Blue-Red"),
    ("Spectral_r", "Spectral"),
    ("gray", "Greyscale"),
]

PRESET_NAMES = [name for name, _ in PRESETS]

_N_SAMPLES = 256


def _samples(name, n=_N_SAMPLES):
    cmap = matplotlib.colormaps[name]
    return [cmap(i / (n - 1))[:3] for i in range(n)]


def color_transfer_function(name, vmin, vmax):
    """A vtkColorTransferFunction spanning [vmin, vmax] for the given preset."""
    if vmax <= vmin:
        vmax = vmin + 1e-9

    ctf = vtk.vtkColorTransferFunction()
    ctf.SetColorSpaceToRGB()
    rgbs = _samples(name)
    for i, (r, g, b) in enumerate(rgbs):
        x = vmin + (vmax - vmin) * i / (len(rgbs) - 1)
        ctf.AddRGBPoint(x, r, g, b)
    ctf.Build()
    return ctf


def css_gradient(name, stops=32):
    """`linear-gradient(...)` string matching the preset, for the HTML legend."""
    cmap = matplotlib.colormaps[name]
    parts = []
    for i in range(stops):
        f = i / (stops - 1)
        r, g, b = cmap(f)[:3]
        parts.append(
            f"rgb({int(r * 255)},{int(g * 255)},{int(b * 255)}) {f * 100:.1f}%"
        )
    return "linear-gradient(to top, " + ", ".join(parts) + ")"


def preset_items():
    """Select-box items for the UI, each carrying its own gradient preview."""
    return [
        {"title": label, "value": name, "gradient": css_gradient(name)}
        for name, label in PRESETS
    ]
