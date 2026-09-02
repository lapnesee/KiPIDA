"""wx-independent PNG generation for DC rail results."""

def layer_name(stackup, layer_id):
    if stackup and layer_id in stackup.get("copper", {}):
        return str(stackup["copper"][layer_id].get("name", layer_id))
    return str(layer_id)


def render_dc_plots(
    system_results, *, stackup=None, board_bounds=None, drop_pct=5.0,
    debug=False, plotter_factory=None,
):
    """Return ordered ``(rail, [(view, png), ...])`` groups."""
    if plotter_factory is None:
        from plotter import Plotter
        plotter_factory = Plotter
    plotter = plotter_factory(debug=debug)
    groups = []
    drop_pct = min(100.0, max(0.0, float(drop_pct)))
    for rail_name, data in system_results.items():
        mesh = data["mesh"]
        mesh.results = data["results"]
        _vmin, vmax, _drop = data["stats"]
        plot_vmin = vmax * (1.0 - drop_pct / 100.0)
        views = []
        rendered = plotter.plot_3d_mesh(
            mesh, stackup, vmin=plot_vmin, vmax=vmax,
            board_bounds=board_bounds, as_png=True,
        )
        if rendered:
            views.append(("3D View", rendered))
        layer_ids = sorted({coords[2] for coords in mesh.node_coords.values()})
        for layer_id in layer_ids:
            name = layer_name(stackup, layer_id)
            rendered = plotter.plot_layer_2d(
                mesh, layer_id, stackup, vmin=plot_vmin, vmax=vmax,
                layer_name=name, board_bounds=board_bounds, as_png=True,
            )
            if rendered:
                views.append((name, rendered))
            density = data.get("current_density")
            if density is not None:
                rendered = plotter.plot_current_density_layer(
                    mesh, layer_id, density, layer_name=name,
                    board_bounds=board_bounds, as_png=True,
                )
                if rendered:
                    views.append((f"{name} — Current density", rendered))
        density = data.get("current_density")
        if density is not None and density.vertical_samples:
            rendered = plotter.plot_vertical_current_density(
                density, board_bounds=board_bounds, as_png=True,
            )
            if rendered:
                views.append(("Vias/PTH — Current density", rendered))
        groups.append((rail_name, views))
    return groups


def flatten_dc_plot_groups(groups):
    """Give each DC view a globally unambiguous title for results and history."""
    return [
        (f"{rail_name} — {view_title}", rendered)
        for rail_name, views in groups
        for view_title, rendered in views
        if rendered
    ]
