"""wx-independent generation of the ordered thermal plot set."""

from plotter import Plotter


def internal_copper_slices(mesh):
    """Return internal copper slices in physical stackup order."""
    specs = list(getattr(mesh, "layer_specs", ()) or ())
    return [
        (index, spec) for index, spec in enumerate(specs)
        if getattr(spec, "material", "") == "copper-layer"
        and index not in (0, len(specs) - 1)
    ]


def render_thermal_plots(
    mesh, result, *, color_map="inferno", color_scale_minimum_c=None,
    color_scale_maximum_c=None, show_internal_copper_layers=True,
    plotter_factory=Plotter,
):
    """Render the canonical 3-D, surface, and optional copper-layer views."""
    plotter = plotter_factory(debug=False)
    bounds = getattr(mesh, "bounds_mm", None)
    common = {
        "as_png": True,
        "board_bounds": bounds,
        "color_map": color_map,
        "color_scale_minimum_c": color_scale_minimum_c,
        "color_scale_maximum_c": color_scale_maximum_c,
    }
    plots = [
        ("Thermal 3D", plotter.plot_thermal_3d(mesh, result, **common)),
        ("Top Surface", plotter.plot_thermal_surface(
            mesh, result, "TOP", with_hover_probe=True, **common,
        )),
    ]
    if show_internal_copper_layers:
        plots.extend(
            (str(spec.name), plotter.plot_thermal_layer(
                mesh, result, index, str(spec.name), with_hover_probe=True, **common,
            ))
            for index, spec in internal_copper_slices(mesh)
        )
    plots.append(("Bottom Surface", plotter.plot_thermal_surface(
        mesh, result, "BOTTOM", with_hover_probe=True, **common,
    )))
    return plots
