"""Measure how far the raster DC mesher's track resistance is from the copper.

The DC analysis, the AC sweep and the DC copper losses feeding thermal and CFD
all mesh copper with ``mesh.Mesher``: one node per grid point inside the
polygon, and every 4-neighbour edge given ``g = t / rho``. A track is therefore
modelled as ``n * step`` wide, where ``n`` is the number of grid rows its
outline happens to contain -- a number set by the grid alignment, not by the
track. ``docs/refonte-analyses.md`` stated the consequence (+50 % / -40 %) from
geometry alone; nothing in the repository had measured it.

    python validation/dc_raster_fidelity.py [--steps 0.1 0.25 0.5]
                                            [--widths 0.15 0.2 0.25 0.5 1 2]
    python validation/dc_raster_fidelity.py --staircase [--step 0.1]

Each case is a straight 20 mm track at 0 deg or 45 deg. Nodes with an axial
coordinate below 2 mm are held at 1 V, above 18 mm at 0 V, and the current
through the mesh gives R_mesh. The reference is rho * gap / (w * t) over the
gap between the two held regions, with the same rho, so only discretisation is
measured. A far-away anchor sets the grid origin and is shifted by fractions
of a step, because on a real board the origin comes from the whole net's
bounds and a track's alignment to the grid is arbitrary.

``--staircase`` holds the alignment fixed and widens the track in 0.01 mm
steps, which shows what a before/after comparison on the DC analysis can see.

No board is needed and nothing is written.
"""

import argparse
import math
import os
import statistics
import sys
import warnings
from types import SimpleNamespace

import numpy as np
import scipy.sparse as sp
import scipy.sparse.linalg as spla
from scipy.sparse.csgraph import connected_components
from shapely.affinity import rotate
from shapely.geometry import LineString

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mesh import Mesher  # noqa: E402

RHO_OHM_MM = 1.72e-5
THICKNESS_MM = 0.035
LENGTH_MM = 20.0
HELD_BELOW_MM, HELD_ABOVE_MM = 2.0, 18.0
ALIGNMENTS = (0.0, 0.125, 0.25, 0.375, 0.5, 0.625, 0.75, 0.875)


def mesh_resistance(width_mm, step_mm, alignment, angle_deg):
    """(R_mesh, gap_mm), or None when the two held regions are not connected."""
    track = LineString([(0.0, 0.0), (LENGTH_MM, 0.0)]).buffer(width_mm / 2.0, cap_style=2)
    track = rotate(track, angle_deg, origin=(0.0, 0.0))
    shift_y = alignment * step_mm
    shift_x = ((alignment * 0.61 + 0.1) % 1.0) * step_mm
    anchor = LineString(
        [(-30.0 + shift_x, -30.0 + shift_y), (-29.0 + shift_x, -30.0 + shift_y)]
    ).buffer(0.5, cap_style=2)

    mesher = Mesher(SimpleNamespace(vias=[], footprints=[]))
    mesher.MAX_ELECTRICAL_NODES = 10_000_000  # measure the requested step, never a coarsened one
    stackup = {"copper": {0: {"thickness_mm": THICKNESS_MM}}, "resistivity": RHO_OHM_MM}
    mesh = mesher.generate_mesh("T", {0: track.union(anchor)}, stackup, grid_size_mm=step_mm)
    count = len(mesh.nodes)
    if count == 0:
        return None
    conductance = sp.coo_matrix(
        (mesh.G_coo_data, (mesh.G_coo_row, mesh.G_coo_col)), shape=(count, count),
    ).tocsr()

    cos_a, sin_a = math.cos(math.radians(angle_deg)), math.sin(math.radians(angle_deg))
    coords = np.array([mesh.node_coords[i][:2] for i in range(count)])
    axial = coords[:, 0] * cos_a + coords[:, 1] * sin_a
    on_track = axial > -1.0
    _, labels = connected_components(conductance, directed=False)
    shared = set(labels[on_track & (axial < HELD_BELOW_MM)]) & set(labels[on_track & (axial > HELD_ABOVE_MM)])
    if not shared:
        return None
    keep = np.nonzero(np.isin(labels, list(shared)) & on_track)[0]
    conductance = conductance[keep][:, keep]
    axial = axial[keep]

    source, sink = axial < HELD_BELOW_MM, axial > HELD_ABOVE_MM
    free = np.nonzero(~(source | sink))[0]
    voltage = np.zeros(len(keep))
    voltage[source] = 1.0
    if len(free):
        rhs = -conductance[free][:, np.nonzero(source)[0]] @ np.ones(int(source.sum()))
        voltage[free] = spla.spsolve(conductance[free][:, free].tocsc(), rhs)
    current = float((conductance @ voltage)[source].sum())
    if current <= 0.0:
        return None
    return 1.0 / current, float(axial[sink].min() - axial[source].max())


def sweep(steps, widths):
    print("width  step  angle   R_mesh / R_exact over grid alignments (min / median / max)")
    for step in steps:
        for width in widths:
            for angle in (0.0, 45.0):
                ratios = []
                for alignment in ALIGNMENTS:
                    measured = mesh_resistance(width, step, alignment, angle)
                    if measured is None:
                        continue
                    resistance, gap = measured
                    ratios.append(resistance / (RHO_OHM_MM * gap / (width * THICKNESS_MM)))
                label = f"{width:5.2f} {step:5.2f} {angle:5.0f}  "
                if not ratios:
                    print(label + " no conducting path at any alignment")
                    continue
                opened = len(ALIGNMENTS) - len(ratios)
                print(
                    label + f"{min(ratios):6.3f} / {statistics.median(ratios):6.3f} / {max(ratios):6.3f}"
                    + (f"   open at {opened} of {len(ALIGNMENTS)} alignments" if opened else "")
                )


def staircase(step):
    for alignment in (0.0, 0.3, 0.6):
        cells = []
        for hundredths in range(15, 41):
            width = hundredths / 100.0
            measured = mesh_resistance(width, step, alignment, 0.0)
            if measured is None:
                cells.append(f"{width:.2f}:open")
                continue
            resistance, gap = measured
            cells.append(f"{width:.2f}:{resistance / gap * 1000.0:.2f}")
        print(f"alignment {alignment}: width mm : modelled mOhm/mm")
        print("  " + " ".join(cells))


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--steps", type=float, nargs="+", default=[0.1, 0.25, 0.5])
    parser.add_argument("--widths", type=float, nargs="+", default=[0.15, 0.2, 0.25, 0.5, 1.0, 2.0])
    parser.add_argument("--staircase", action="store_true")
    parser.add_argument("--step", type=float, default=0.1)
    args = parser.parse_args(argv[1:])
    warnings.filterwarnings("ignore", category=sp.SparseEfficiencyWarning)
    if args.staircase:
        staircase(args.step)
    else:
        sweep(args.steps, args.widths)


if __name__ == "__main__":
    main(sys.argv)
