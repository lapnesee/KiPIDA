"""How far the thermal hotspot moves when the grid step doubles (plan lot 8).

    python validation/thermal_mesh_sensitivity.py [--steps 0.5 1.0 2.0]

A synthetic 60 x 40 mm two-layer board with full copper on both faces and three
components -- 2 W on 5 x 5 mm, 0.8 W on 3 x 3 mm, 0.25 W on 2 x 2 mm -- in
natural convection with radiation, solved with surface refinement at each
step. The thermal analysis now repeats its solve at twice the step and raises
TH-004 above a 2 C hotspot change; this measures what that check reports at
the "Normal (1.0 mm)" preset on a board that is not the reference one, which
needs KiCad or Zeo running and is left to a launch from there.

Nothing is written.
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shapely.geometry import box  # noqa: E402

from models import AirflowSettings, ThermalAnalysisSettings, ThermalComponentModel  # noqa: E402
from thermal_mesh import ThermalMesher  # noqa: E402
from thermal_model import ThermalBoardModel, ThermalPlacement  # noqa: E402
from thermal_solver import ThermalSolver  # noqa: E402

COMPONENTS = (
    ("U1", 2.0, 5.0, 15.0, 20.0),
    ("U2", 0.8, 3.0, 40.0, 12.0),
    ("U3", 0.25, 2.0, 48.0, 30.0),
)


def board_model():
    outline = box(0.0, 0.0, 60.0, 40.0)
    return ThermalBoardModel(
        bounds_mm=(0.0, 0.0, 60.0, 40.0),
        outline=outline,
        stackup={
            "copper": {0: {"name": "F.Cu", "thickness_mm": 0.035}, 31: {"name": "B.Cu", "thickness_mm": 0.035}},
            "layer_order": [0, 31],
            "substrate": [{"between": [0, 31], "material": "FR4", "thickness_mm": 1.53}],
        },
        copper_by_layer={0: outline, 31: outline},
        vias=[],
        placements={
            ref: ThermalPlacement(ref, x, y, size, size, "TOP")
            for ref, _power, size, x, y in COMPONENTS
        },
        components=[
            ThermalComponentModel(
                ref_des=ref, power_w=power, width_mm=size, depth_mm=size,
                theta_jb_c_per_w=10.0, max_junction_c=125.0,
            )
            for ref, power, size, _x, _y in COMPONENTS
        ],
    )


def hotspot(step_mm):
    settings = ThermalAnalysisSettings(
        ambient_c=25.0, grid_size_mm=step_mm, include_radiation=True,
        airflow=AirflowSettings(mode="NATURAL", expose_top=True, expose_bottom=True, expose_edges=True),
    )
    started = time.perf_counter()
    mesh = ThermalMesher().generate_mesh(board_model(), settings)
    result = ThermalSolver().solve_with_surface_refinement(mesh, settings)
    return result.hotspot.temperature_c, float(result.effective_grid_size_mm or step_mm), len(mesh.nodes), time.perf_counter() - started


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--steps", type=float, nargs="+", default=[0.5, 1.0, 2.0])
    args = parser.parse_args()
    solved = {}
    print(f"{'step mm':>8} {'hotspot C':>10} {'nodes':>9} {'seconds':>8}")
    for step in sorted(set(args.steps) | {step * 2.0 for step in args.steps if step * 2.0 not in args.steps}):
        temperature, effective, nodes, seconds = hotspot(step)
        solved[step] = temperature
        print(f"{effective:8.3g} {temperature:10.3f} {nodes:9d} {seconds:8.2f}")
    print("\nWhat the thermal analysis reports (hotspot change at twice the step):")
    for step in sorted(args.steps):
        if step * 2.0 in solved:
            delta = abs(solved[step] - solved[step * 2.0])
            print(f"  {step:g} mm -> {step * 2.0:g} mm: {delta:.3f} C" + ("  TH-004" if delta > 2.0 else ""))


if __name__ == "__main__":
    main()
