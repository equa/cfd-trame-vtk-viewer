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


def color_transfer_function(name, vmin, vmax, n_colors=0):
    """A vtkColorTransferFunction spanning [vmin, vmax] for the given preset.

    ``n_colors > 0`` bands the map into that many discrete colours instead of a
    smooth ramp. The banding is baked into the transfer-function *nodes* (flat
    plateaus, one colour per band) rather than via
    ``vtkDiscretizableColorTransferFunction`` — that keeps it a plain
    ``vtkColorTransferFunction`` whose ``DeepCopy`` (used when the range changes)
    carries the banding along, which the discretizable flavour's does not."""
    if vmax <= vmin:
        vmax = vmin + 1e-9

    ctf = vtk.vtkColorTransferFunction()
    ctf.SetColorSpaceToRGB()
    if n_colors and n_colors > 0:
        cmap = matplotlib.colormaps[name]
        eps = (vmax - vmin) * 1e-6  # keep band edges distinct, so plateaus stay flat
        for i in range(n_colors):
            r, g, b = cmap((i + 0.5) / n_colors)[:3]
            x0 = vmin + (vmax - vmin) * i / n_colors
            x1 = vmin + (vmax - vmin) * (i + 1) / n_colors
            ctf.AddRGBPoint(x0, r, g, b)
            ctf.AddRGBPoint(x1 - eps, r, g, b)
    else:
        for i, (r, g, b) in enumerate(_samples(name)):
            x = vmin + (vmax - vmin) * i / (_N_SAMPLES - 1)
            ctf.AddRGBPoint(x, r, g, b)
    ctf.Build()
    return ctf


def css_gradient(name, stops=32, n_colors=0):
    """`linear-gradient(...)` string matching the preset, for the HTML legend.

    ``n_colors > 0`` produces a matching banded gradient with hard stops, so the
    legend shows the same discrete steps the geometry does."""
    cmap = matplotlib.colormaps[name]
    if n_colors and n_colors > 0:
        parts = []
        for i in range(n_colors):
            r, g, b = cmap((i + 0.5) / n_colors)[:3]
            rgb = f"rgb({int(r * 255)},{int(g * 255)},{int(b * 255)})"
            parts.append(f"{rgb} {i / n_colors * 100:.2f}%")
            parts.append(f"{rgb} {(i + 1) / n_colors * 100:.2f}%")
        return "linear-gradient(to top, " + ", ".join(parts) + ")"
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
