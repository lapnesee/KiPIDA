"""Build a targeted Palace FEM project from a Ki-PIDA Phase 10 region.

This worker runs outside KiCad's Python process because the Gmsh Python API is
provided by the isolated Phase 10 runtime.  The generated model is deliberately
bounded: real stackup, routed copper, zones and vias are retained, while the
source is an explicitly disclosed current-dipole approximation.
"""

import json
import math
from pathlib import Path
import sys
import time

import gmsh


def _layer_elevations(layers):
    elevations, z = {}, 0.0
    for layer in layers:
        thickness = max(float(layer.get("thickness_mm", 0.0)), 0.0)
        if str(layer.get("kind", "")).upper() == "COPPER":
            layer_id = layer.get("layer_id")
            if layer_id is not None:
                elevations[int(layer_id)] = z + 0.5 * thickness
        z += thickness
    return elevations, max(z, 0.1)


def _track_polygon(track):
    x1, y1 = map(float, track["start"])
    x2, y2 = map(float, track["end"])
    half = max(float(track.get("width_mm", 0.1)) / 2.0, 0.005)
    dx, dy = x2 - x1, y2 - y1
    length = math.hypot(dx, dy)
    if length <= 1.0e-12:
        return [
            [x1 - half, y1 - half], [x1 + half, y1 - half],
            [x1 + half, y1 + half], [x1 - half, y1 + half],
        ]
    nx, ny = -dy / length * half, dx / length * half
    return [
        [x1 + nx, y1 + ny], [x2 + nx, y2 + ny],
        [x2 - nx, y2 - ny], [x1 - nx, y1 - ny],
    ]


def _simplify_polygon(points, tolerance):
    """Dependency-free Ramer-Douglas-Peucker simplification for zone outlines."""
    cleaned = [[float(point[0]), float(point[1])] for point in points]
    if len(cleaned) > 2 and cleaned[0] == cleaned[-1]:
        cleaned.pop()
    if len(cleaned) < 4:
        return cleaned

    def distance(point, start, end):
        dx, dy = end[0] - start[0], end[1] - start[1]
        if dx * dx + dy * dy <= 1.0e-24:
            return math.hypot(point[0] - start[0], point[1] - start[1])
        value = abs(dy * point[0] - dx * point[1] + end[0] * start[1] - end[1] * start[0])
        return value / math.hypot(dx, dy)

    def rdp(sequence):
        if len(sequence) <= 2:
            return sequence
        distances = [distance(point, sequence[0], sequence[-1]) for point in sequence[1:-1]]
        maximum = max(distances, default=0.0)
        if maximum <= tolerance:
            return [sequence[0], sequence[-1]]
        index = distances.index(maximum) + 1
        return rdp(sequence[:index + 1])[:-1] + rdp(sequence[index:])

    # Rotate the ring so a point far from the first point becomes the other
    # RDP endpoint; this avoids collapsing a closed polygon to one segment.
    pivot = max(
        range(1, len(cleaned)),
        key=lambda index: math.hypot(
            cleaned[index][0] - cleaned[0][0], cleaned[index][1] - cleaned[0][1],
        ),
    )
    first = rdp(cleaned[:pivot + 1])
    second = rdp(cleaned[pivot:] + [cleaned[0]])
    return first[:-1] + second[:-1]


def _inset_points(points, bounds, inset):
    xmin, ymin, xmax, ymax = bounds
    return [
        [
            min(max(float(point[0]), xmin + inset), xmax - inset),
            min(max(float(point[1]), ymin + inset), ymax - inset),
        ]
        for point in points
    ]


def _plane_surface(points, z):
    cleaned = []
    for point in points:
        xy = (float(point[0]), float(point[1]))
        if not cleaned or math.hypot(xy[0] - cleaned[-1][0], xy[1] - cleaned[-1][1]) > 1.0e-9:
            cleaned.append(xy)
    if len(cleaned) > 2 and cleaned[0] == cleaned[-1]:
        cleaned.pop()
    if len(cleaned) < 3:
        return None
    point_tags = [gmsh.model.occ.addPoint(x, y, z) for x, y in cleaned]
    lines = [
        gmsh.model.occ.addLine(point_tags[index], point_tags[(index + 1) % len(point_tags)])
        for index in range(len(point_tags))
    ]
    return gmsh.model.occ.addPlaneSurface([gmsh.model.occ.addCurveLoop(lines)])


def _representative_frequency(source, f_start, f_stop):
    fundamental = max(float(source.get("frequency_hz", 0.0)), 1.0)
    if fundamental < f_start:
        fundamental *= max(1, int(math.ceil(f_start / fundamental)))
    return min(max(fundamental, f_start), f_stop)


def _source_tracks(payload, source, net_name):
    candidates = [track for track in payload.get("tracks", []) if track.get("net_name") == net_name]
    ports = [
        port for port in payload.get("source_ports", [])
        if port.get("source_name") == source.get("name") and port.get("net_name") == net_name
    ]
    if ports:
        px, py = float(ports[0]["x_mm"]), float(ports[0]["y_mm"])
        candidates.sort(key=lambda track: min(
            math.hypot(float(track["start"][0]) - px, float(track["start"][1]) - py),
            math.hypot(float(track["end"][0]) - px, float(track["end"][1]) - py),
        ))
    else:
        candidates.sort(key=lambda track: -float(track.get("length_mm", 0.0)))
    return candidates


def _dipole(source, payload, elevations, net_name, index, sign=1.0):
    tracks = _source_tracks(payload, source, net_name)
    if not tracks:
        return None
    track = tracks[0]
    x1, y1 = map(float, track["start"])
    x2, y2 = map(float, track["end"])
    dx, dy = x2 - x1, y2 - y1
    # A routed segment's length depends on how KiCad happens to split the
    # polyline.  Using it as the dipole length made source strength change
    # after innocuous editing.  A local injection length equal to the routed
    # conductor width is deterministic and is disclosed in provenance.
    injection_length_mm = max(float(track.get("width_mm", 0.1)), 0.05)
    if math.hypot(dx, dy) <= 1.0e-12:
        dx, dy = 1.0, 0.0
    norm = math.hypot(dx, dy)
    z = float(elevations.get(int(track["layer_id"]), 0.0)) + 0.05
    current = max(abs(float(source.get("current_a", 0.1))), 1.0e-6)
    return {
        "Index": index,
        "Moment": sign * current * injection_length_mm * 1.0e-3,
        "Center": [(x1 + x2) / 2.0, (y1 + y2) / 2.0, z],
        "Direction": [dx / norm, dy / norm, 0.0],
        "_KiPIDAInjectionLengthMm": injection_length_mm,
    }


def _outer_surfaces(bounds, tolerance=1.0e-6):
    xmin, ymin, zmin, xmax, ymax, zmax = bounds
    selected = []
    for _, tag in gmsh.model.getEntities(2):
        sxmin, symin, szmin, sxmax, symax, szmax = gmsh.model.getBoundingBox(2, tag)
        if (
            abs(sxmin - xmin) <= tolerance and abs(sxmax - xmin) <= tolerance
            or abs(sxmin - xmax) <= tolerance and abs(sxmax - xmax) <= tolerance
            or abs(symin - ymin) <= tolerance and abs(symax - ymin) <= tolerance
            or abs(symin - ymax) <= tolerance and abs(symax - ymax) <= tolerance
            or abs(szmin - zmin) <= tolerance and abs(szmax - zmin) <= tolerance
            or abs(szmin - zmax) <= tolerance and abs(szmax - zmax) <= tolerance
        ):
            selected.append(tag)
    return sorted(set(selected))


def _host_volume(point, volume_tags):
    for tag in volume_tags:
        try:
            if gmsh.model.isInside(3, tag, list(point)):
                return tag
        except Exception:
            xmin, ymin, zmin, xmax, ymax, zmax = gmsh.model.getBoundingBox(3, tag)
            if xmin <= point[0] <= xmax and ymin <= point[1] <= ymax and zmin <= point[2] <= zmax:
                return tag
    return None


def _build_once(payload, project_directory, via_model="CYLINDRICAL", inherited_warnings=None):
    started = time.perf_counter()
    warnings = list(inherited_warnings or ())
    project_directory = Path(project_directory)
    mesh_directory = project_directory / "mesh"
    mesh_directory.mkdir(parents=True, exist_ok=True)
    (project_directory / "input.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8",
    )
    xmin, ymin, xmax, ymax = map(float, payload["region"]["bounds_mm"])
    elevations, board_thickness = _layer_elevations(payload.get("stackup", []))
    resolution = max(float(payload.get("mesh_resolution_mm", 0.25)), 0.05)
    air = max(3.0, 0.5 * max(xmax - xmin, ymax - ymin))
    outer_bounds = (
        xmin - air, ymin - air, -air,
        xmax + air, ymax + air, board_thickness + air,
    )

    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 1 if payload.get("gmsh_terminal") else 0)
        gmsh.model.add("kipida_phase10_region")
        outer = gmsh.model.occ.addBox(
            outer_bounds[0], outer_bounds[1], outer_bounds[2],
            outer_bounds[3] - outer_bounds[0],
            outer_bounds[4] - outer_bounds[1],
            outer_bounds[5] - outer_bounds[2],
        )
        base_inputs = [(3, outer)]
        dielectric_inputs = []
        dielectric_properties = []
        dielectric_layers = [
            layer for layer in payload.get("stackup", [])
            if str(layer.get("kind", "")).upper() == "DIELECTRIC"
            and float(layer.get("thickness_mm", 0.0)) > 0.0
        ]
        if dielectric_layers:
            dielectric_thickness = sum(float(layer["thickness_mm"]) for layer in dielectric_layers)
            epsilon_r = sum(
                float(layer["thickness_mm"]) * max(float(layer.get("epsilon_r", 1.0)), 1.0)
                for layer in dielectric_layers
            ) / dielectric_thickness
            loss_tangent = sum(
                float(layer["thickness_mm"]) * max(float(layer.get("loss_tangent", 0.0)), 0.0)
                for layer in dielectric_layers
            ) / dielectric_thickness
            tag = gmsh.model.occ.addBox(
                xmin, ymin, 0.0, xmax - xmin, ymax - ymin, board_thickness,
            )
            base_inputs.append((3, tag))
            dielectric_inputs.append((3, tag))
            dielectric_properties.append((epsilon_r, loss_tangent))
            warnings.append(
                "Dielectric plies were homogenized into one volume; copper elevations still "
                "follow the configured physical stackup."
            )

        via_model = str(via_model).upper()
        if via_model not in {"CYLINDRICAL", "CONDUCTIVE_SOLID"}:
            raise ValueError(f"Unsupported Palace via model: {via_model}")
        via_inputs = []
        modeled_vias = []
        omitted_vias = []
        requested_vias = list(payload.get("vias", []))
        for via in requested_vias:
            drill = float(via.get("drill_mm", 0.0) or 0.0)
            diameter = float(via.get("diameter_mm", 0.0) or 0.0)
            x, y = map(float, via.get("position", (0.0, 0.0)))
            layer_z = [
                elevations[int(layer)] for layer in via.get("layer_ids", [])
                if int(layer) in elevations
            ]
            if drill <= 0.0 or diameter <= drill or not layer_z:
                omitted_vias.append(via)
                continue
            z0, z1 = min(layer_z), max(layer_z)
            if z1 - z0 <= 1.0e-6:
                omitted_vias.append(via)
                continue
            modeled = {
                **via, "_z0": z0, "_z1": z1,
                "_pad_radius": diameter / 2.0, "_hole_radius": drill / 2.0,
            }
            modeled_vias.append(modeled)
            if via_model in {"CYLINDRICAL", "CONDUCTIVE_SOLID"}:
                cap_extension = (
                    max(0.005, resolution * 0.05)
                    if via_model == "CONDUCTIVE_SOLID" else 0.0
                )
                geometry_z0 = z0 - cap_extension
                geometry_z1 = z1 + cap_extension
                split_levels = [geometry_z0, geometry_z1]
                if via_model == "CONDUCTIVE_SOLID":
                    split_levels.extend(
                        z for z in elevations.values()
                        if geometry_z0 + 1.0e-9 < z < geometry_z1 - 1.0e-9
                    )
                split_levels = sorted(set(split_levels))
                for segment_z0, segment_z1 in zip(split_levels, split_levels[1:]):
                    if via_model == "CONDUCTIVE_SOLID":
                        tag = gmsh.model.occ.addBox(
                            x - drill / 2.0, y - drill / 2.0, segment_z0,
                            drill, drill, segment_z1 - segment_z0,
                        )
                    else:
                        tag = gmsh.model.occ.addCylinder(
                            x, y, segment_z0, 0.0, 0.0,
                            segment_z1 - segment_z0, drill / 2.0,
                        )
                    via_inputs.append(((3, tag), modeled))
        if omitted_vias:
            warnings.append(
                f"{len(omitted_vias)} via(s) were not meshed because their saved drill, pad "
                "diameter, or layer span was unavailable."
            )
        # Retain only the zero-thickness barrel walls.  Making the finished
        # hole a separate 3-D volume creates a non-conforming interface where
        # embedded copper lands meet that volume; a closed PEC wall already
        # excludes fields from its interior and is substantially more robust.
        via_barrel_surfaces_by_net = {}
        if via_inputs and via_model == "CYLINDRICAL":
            gmsh.model.occ.synchronize()
            for volume, via in via_inputs:
                for dim, tag in gmsh.model.getBoundary([volume], oriented=False):
                    if dim != 2:
                        continue
                    _, _, szmin, _, _, szmax = gmsh.model.getBoundingBox(2, tag)
                    if szmax - szmin > 1.0e-6:
                        via_barrel_surfaces_by_net.setdefault(
                            str(via.get("net_name", "")), set(),
                        ).add(tag)
            gmsh.model.occ.remove([volume for volume, _ in via_inputs], recursive=False)
            gmsh.model.occ.synchronize()

        conductive_volume_inputs = []
        if via_model == "CONDUCTIVE_SOLID":
            # Each via was created directly as a stack of cylinders bounded by
            # copper elevations, so no 2-D splitter surfaces or long unsplit
            # cylinder seams remain in the CAD model.
            conductive_volume_inputs = [volume for volume, _ in via_inputs]
        fragment_inputs = base_inputs + conductive_volume_inputs
        _, mapping = gmsh.model.occ.fragment([fragment_inputs[0]], fragment_inputs[1:])
        gmsh.model.occ.synchronize()
        mapped = [set(tag for dim, tag in items if dim == 3) for items in mapping]
        outer_volumes = mapped[0]
        dielectric_volumes = mapped[1:1 + len(dielectric_inputs)]
        mapped_via_volumes = mapped[
            len(base_inputs):len(base_inputs) + len(conductive_volume_inputs)
        ] if via_model == "CONDUCTIVE_SOLID" else []
        via_volumes = set().union(*mapped_via_volumes) if mapped_via_volumes else set()
        dielectric_union = set().union(*dielectric_volumes) if dielectric_volumes else set()
        air_volumes = outer_volumes - dielectric_union - via_volumes
        dielectric_volumes = [items - via_volumes for items in dielectric_volumes]

        material_entries = []
        if not air_volumes:
            raise RuntimeError("Gmsh did not retain an air volume around the PCB region.")
        gmsh.model.addPhysicalGroup(3, sorted(air_volumes), 1)
        gmsh.model.setPhysicalName(3, 1, "air")
        material_entries.append({
            "Attributes": [1], "Permittivity": 1.0, "Permeability": 1.0,
        })
        for index, (volumes, properties) in enumerate(
            zip(dielectric_volumes, dielectric_properties), start=10,
        ):
            if not volumes:
                continue
            gmsh.model.addPhysicalGroup(3, sorted(volumes), index)
            gmsh.model.setPhysicalName(3, index, f"dielectric_{index - 9}")
            epsilon_r, loss_tangent = properties
            material_entries.append({
                "Attributes": [index], "Permittivity": epsilon_r,
                "Permeability": 1.0, "LossTan": loss_tangent,
            })
        if via_volumes:
            gmsh.model.addPhysicalGroup(3, sorted(via_volumes), 50)
            gmsh.model.setPhysicalName(3, 50, "via_copper")
            material_entries.append({
                "Attributes": [50], "Permittivity": 1.0,
                "Permeability": 1.0, "Conductivity": 5.8e7,
            })

        pec_surfaces = set().union(*via_barrel_surfaces_by_net.values()) \
            if via_barrel_surfaces_by_net else set()

        all_volumes = sorted(air_volumes | (dielectric_union - via_volumes))
        surfaces_by_conductor = {}
        copper_inset = max(0.01, resolution * 0.05)
        copper_clearance = max(
            float(payload.get("copper_clearance_mm", 0.2)), copper_inset,
        )
        minimum_track_length = max(0.005, resolution * 0.1)
        modeled_tracks = []
        omitted_short_tracks = 0
        for track in payload.get("tracks", []):
            track_length = math.hypot(
                float(track["end"][0]) - float(track["start"][0]),
                float(track["end"][1]) - float(track["start"][1]),
            )
            if track_length < minimum_track_length:
                omitted_short_tracks += 1
                continue
            modeled_tracks.append(track)
            layer_id = int(track["layer_id"])
            polygon = _inset_points(
                _track_polygon(track), (xmin, ymin, xmax, ymax), copper_inset,
            )
            surface = _plane_surface(polygon, elevations.get(layer_id, 0.0))
            if surface is not None:
                conductor = (layer_id, str(track.get("net_name", "")))
                surfaces_by_conductor.setdefault(conductor, []).append(surface)
        if omitted_short_tracks:
            warnings.append(
                f"{omitted_short_tracks} sub-resolution track segment(s) shorter than "
                f"{minimum_track_length:.6g} mm were removed before OCC union to avoid "
                "sliver tetrahedra; overlapping conductor widths preserve local continuity."
            )
        for zone in payload.get("zones", []):
            layer_id = int(zone["layer_id"])
            polygon = _simplify_polygon(zone.get("polygon", []), max(0.02, resolution * 0.2))
            polygon = _inset_points(
                polygon, (xmin, ymin, xmax, ymax), copper_inset,
            )
            surface = _plane_surface(polygon, elevations.get(layer_id, 0.0))
            if surface is not None:
                conductor = (layer_id, str(zone.get("net_name", "")))
                surfaces_by_conductor.setdefault(conductor, []).append(surface)
        # Add the saved annular via lands on every copper layer crossed by the
        # barrel.  The finished hole is cut after the coplanar copper union so
        # a routed track cannot accidentally cap the aperture.
        via_holes_by_conductor = {}
        captured_conductors = set(surfaces_by_conductor)
        for via in modeled_vias:
            x, y = map(float, via["position"])
            for layer_id, z in elevations.items():
                if via["_z0"] - 1.0e-9 <= z <= via["_z1"] + 1.0e-9:
                    conductor = (layer_id, str(via.get("net_name", "")))
                    if (
                        via_model == "CYLINDRICAL"
                        or conductor in captured_conductors
                    ):
                        if via_model == "CONDUCTIVE_SOLID":
                            pad = gmsh.model.occ.addRectangle(
                                x - via["_pad_radius"], y - via["_pad_radius"], z,
                                2.0 * via["_pad_radius"],
                                2.0 * via["_pad_radius"],
                            )
                        else:
                            pad = gmsh.model.occ.addDisk(
                                x, y, z, via["_pad_radius"], via["_pad_radius"],
                            )
                        via_holes_by_conductor.setdefault(conductor, []).append(
                            (x, y, z, via["_hole_radius"])
                        )
                        surfaces_by_conductor.setdefault(conductor, []).append(pad)
        # Zone polygons in the worker snapshot are conservative outlines and
        # do not carry KiCad's filled-polygon clearance cutouts.  Reconstruct
        # every via antipad explicitly on foreign nets.  Without these cuts a
        # through via intersects internal reference planes and Gmsh reports a
        # segment/facet PLC error.
        for conductor in surfaces_by_conductor:
            layer_id, net_name = conductor
            z = elevations.get(layer_id)
            if z is None:
                continue
            for via in modeled_vias:
                if not (via["_z0"] - 1.0e-9 <= z <= via["_z1"] + 1.0e-9):
                    continue
                x, y = map(float, via["position"])
                if str(via.get("net_name", "")) == net_name:
                    if (
                        via_model == "CONDUCTIVE_SOLID"
                        and conductor not in via_holes_by_conductor
                    ):
                        via_holes_by_conductor.setdefault(conductor, []).append(
                            (x, y, z, via["_hole_radius"])
                        )
                else:
                    via_holes_by_conductor.setdefault(conductor, []).append(
                        (x, y, z, via["_pad_radius"] + copper_clearance)
                    )
        gmsh.model.occ.synchronize()
        prepared_copper = []
        for (layer_id, net_name), surfaces in surfaces_by_conductor.items():
            fused = [(2, surfaces[0])]
            if len(surfaces) > 1:
                try:
                    fused, _ = gmsh.model.occ.fuse(fused, [(2, tag) for tag in surfaces[1:]])
                    gmsh.model.occ.synchronize()
                except Exception as exc:
                    warnings.append(
                        f"Copper union on layer {layer_id}, net {net_name!r}, fell back "
                        f"to separate surfaces: {exc}"
                    )
                    fused = [(2, tag) for tag in surfaces]
            conductor = (layer_id, net_name)
            if via_holes_by_conductor.get(conductor):
                cut_surfaces = []
                for _, surface in fused:
                    holes = [
                        (
                            2,
                            gmsh.model.occ.addRectangle(
                                x - radius, y - radius, z,
                                2.0 * radius, 2.0 * radius,
                            ) if via_model == "CONDUCTIVE_SOLID" else
                            gmsh.model.occ.addDisk(x, y, z, radius, radius),
                        )
                        for x, y, z, radius in via_holes_by_conductor[conductor]
                    ]
                    try:
                        cut, _ = gmsh.model.occ.cut(
                            [(2, surface)], holes, removeObject=True, removeTool=True,
                        )
                        cut_surfaces.extend(cut)
                    except Exception as exc:
                        warnings.append(
                            f"Finished-hole cut on copper layer {layer_id}, net "
                            f"{net_name!r}, failed: {exc}"
                        )
                        cut_surfaces.append((2, surface))
                fused = cut_surfaces
                gmsh.model.occ.synchronize()
            prepared_copper.extend(
                (layer_id, net_name, surface) for _, surface in fused
            )

        if via_model == "CONDUCTIVE_SOLID" and prepared_copper:
            # Re-fragment every material volume together with the finished
            # copper surfaces.  This turns via/copper contact curves into true
            # volume-boundary topology; mesh.embed alone cannot repair a PLC
            # where an internal conductor meets a material interface.
            volume_records = []
            for tag in sorted(air_volumes):
                volume_records.append(("air", 0, tag))
            for dielectric_index, volumes in enumerate(dielectric_volumes):
                for tag in sorted(volumes):
                    volume_records.append(("dielectric", dielectric_index, tag))
            for tag in sorted(via_volumes):
                volume_records.append(("via", 0, tag))
            gmsh.model.removePhysicalGroups()
            _, conformal_mapping = gmsh.model.occ.fragment(
                [(3, tag) for _, _, tag in volume_records],
                [(2, surface) for _, _, surface in prepared_copper],
            )
            gmsh.model.occ.synchronize()
            remapped_air = set()
            remapped_dielectrics = [set() for _ in dielectric_volumes]
            remapped_vias = set()
            for record, mapped_items in zip(volume_records, conformal_mapping):
                tags = {tag for dim, tag in mapped_items if dim == 3}
                if record[0] == "air":
                    remapped_air.update(tags)
                elif record[0] == "dielectric":
                    remapped_dielectrics[record[1]].update(tags)
                else:
                    remapped_vias.update(tags)
            copper_maps = conformal_mapping[len(volume_records):]
            prepared_copper = [
                (layer_id, net_name, tag)
                for (layer_id, net_name, _), mapped_items in zip(
                    prepared_copper, copper_maps,
                )
                for dim, tag in mapped_items if dim == 2
            ]
            air_volumes = remapped_air
            dielectric_volumes = remapped_dielectrics
            via_volumes = remapped_vias
            dielectric_union = set().union(*dielectric_volumes) \
                if dielectric_volumes else set()
            all_volumes = sorted(
                air_volumes | dielectric_union | via_volumes
            )
            material_entries = []
            gmsh.model.addPhysicalGroup(3, sorted(air_volumes), 1)
            gmsh.model.setPhysicalName(3, 1, "air")
            material_entries.append({
                "Attributes": [1], "Permittivity": 1.0, "Permeability": 1.0,
            })
            for index, (volumes, properties) in enumerate(
                zip(dielectric_volumes, dielectric_properties), start=10,
            ):
                if not volumes:
                    continue
                gmsh.model.addPhysicalGroup(3, sorted(volumes), index)
                gmsh.model.setPhysicalName(3, index, f"dielectric_{index - 9}")
                epsilon_r, loss_tangent = properties
                material_entries.append({
                    "Attributes": [index], "Permittivity": epsilon_r,
                    "Permeability": 1.0, "LossTan": loss_tangent,
                })
            if via_volumes:
                gmsh.model.addPhysicalGroup(3, sorted(via_volumes), 50)
                gmsh.model.setPhysicalName(3, 50, "via_copper")
                material_entries.append({
                    "Attributes": [50], "Permittivity": 1.0,
                    "Permeability": 1.0, "Conductivity": 5.8e7,
                })

        # Make the circular copper-hole edges and barrel-wall edges topologically
        # identical before meshing.  Geometric coincidence alone produces a
        # non-conforming PLC when an embedded trace meets a via barrel.
        mapped_copper = []
        mapped_vias = set()
        conductor_nets = {
            net_name for _, net_name, _ in prepared_copper
        } | set(via_barrel_surfaces_by_net)
        for net_name in conductor_nets:
            net_copper = [
                item for item in prepared_copper if item[1] == net_name
            ]
            net_vias = sorted(via_barrel_surfaces_by_net.get(net_name, set()))
            if net_copper and net_vias:
                _, conformal_map = gmsh.model.occ.fragment(
                    [(2, surface) for _, _, surface in net_copper],
                    [(2, surface) for surface in net_vias],
                )
                for (layer_id, mapped_net, _), mapped in zip(
                    net_copper, conformal_map,
                ):
                    mapped_copper.extend(
                        (layer_id, mapped_net, tag)
                        for dim, tag in mapped if dim == 2
                    )
                for mapped in conformal_map[len(net_copper):]:
                    mapped_vias.update(tag for dim, tag in mapped if dim == 2)
            else:
                mapped_copper.extend(net_copper)
                mapped_vias.update(net_vias)
        prepared_copper = mapped_copper
        pec_surfaces = mapped_vias
        gmsh.model.occ.synchronize()

        # Barrel surfaces and planar copper are embedded into the same material
        # volumes after their shared circular edges have been made conformal.
        if via_model == "CYLINDRICAL":
            for surface in sorted(pec_surfaces):
                cx, cy, cz = gmsh.model.occ.getCenterOfMass(2, surface)
                host = _host_volume((cx, cy, cz), all_volumes)
                if host is not None:
                    gmsh.model.mesh.embed(2, [surface], 3, host)

        for layer_id, net_name, surface in prepared_copper:
            cx, cy, cz = gmsh.model.occ.getCenterOfMass(2, surface)
            host = _host_volume((cx, cy, cz), all_volumes)
            if host is None and via_model != "CONDUCTIVE_SOLID":
                warnings.append(
                    f"Copper surface {surface} on layer {layer_id}, net {net_name!r}, "
                    "was outside the FEM domain."
                )
                continue
            if via_model != "CONDUCTIVE_SOLID":
                gmsh.model.mesh.embed(2, [surface], 3, host)
            copper_points = [
                (dim, tag) for dim, tag in gmsh.model.getBoundary(
                    [(2, surface)], oriented=False, recursive=True,
                ) if dim == 0
            ]
            if copper_points:
                gmsh.model.mesh.setSize(copper_points, resolution)
            pec_surfaces.add(surface)

        outer_surfaces = _outer_surfaces(outer_bounds)
        pec_surfaces.difference_update(outer_surfaces)
        if not pec_surfaces:
            raise RuntimeError("No routed copper surface was available for the Palace region.")
        gmsh.model.addPhysicalGroup(2, sorted(pec_surfaces), 100)
        gmsh.model.setPhysicalName(2, 100, "pcb_copper_pec")
        gmsh.model.addPhysicalGroup(2, outer_surfaces, 101)
        gmsh.model.setPhysicalName(2, 101, "absorbing_boundary")

        characteristic_min = max(0.02, resolution * 0.5)
        characteristic_max = max(0.15, resolution * 3.0)
        gmsh.option.setNumber("Mesh.CharacteristicLengthMin", characteristic_min)
        gmsh.option.setNumber("Mesh.CharacteristicLengthMax", characteristic_max)
        mesh_algorithm_3d = int(payload.get("gmsh_algorithm_3d", 1))
        gmsh.option.setNumber("Mesh.Algorithm3D", mesh_algorithm_3d)
        gmsh.option.setNumber("Mesh.MshFileVersion", 2.2)
        gmsh.option.setNumber("Mesh.Binary", 0)
        gmsh.model.mesh.generate(3)
        mesh_path = mesh_directory / "region.msh"
        gmsh.write(str(mesh_path))
        element_count = sum(len(tags) for tags in gmsh.model.mesh.getElements()[1])
        node_count = len(gmsh.model.mesh.getNodes()[0])

        sources = [source for source in payload.get("sources", []) if source.get("enabled")]
        if not sources:
            raise RuntimeError("The selected region has no enabled excitation source.")
        source = sources[0]
        dipoles = []
        source_payload = dict(payload)
        source_payload["tracks"] = modeled_tracks
        positive = _dipole(
            source, source_payload, elevations, source.get("net_name", ""), 1, 1.0,
        )
        if positive:
            dipoles.append(positive)
        if str(source.get("kind", "")).upper() == "DIFFERENTIAL":
            negative = _dipole(
                source, source_payload, elevations, source.get("negative_net_name", ""), 2, -1.0,
            )
            if negative:
                dipoles.append(negative)
        if not dipoles:
            raise RuntimeError("No routed conductor could anchor the Palace current excitation.")
        if str(source.get("kind", "")).upper() == "DIFFERENTIAL" and len(dipoles) != 2:
            raise RuntimeError("Differential Palace excitation requires both routed conductors.")

        source_moment = sum(abs(float(item["Moment"])) for item in dipoles)
        injection_lengths = [float(item.pop("_KiPIDAInjectionLengthMm")) for item in dipoles]

        f_start = max(float(payload.get("frequency_start_hz", 30.0e6)), 1.0)
        f_stop = max(float(payload.get("frequency_stop_hz", 1.0e9)), f_start)
        frequency = _representative_frequency(source, f_start, f_stop)
        source_fundamental = max(float(source.get("frequency_hz", 0.0)), 1.0)
        harmonic_order = max(1, int(round(frequency / source_fundamental)))
        config = {
            "Problem": {
                "Type": "Driven", "Verbose": 2, "Output": "postpro",
                "OutputFormats": {"Paraview": True, "GridFunction": False},
            },
            "Model": {"Mesh": "mesh/region.msh", "L0": 1.0e-3},
            "Domains": {
                "Materials": material_entries,
                "CurrentDipole": dipoles,
                "Postprocessing": {
                    "Energy": [{"Index": 1, "Attributes": sorted(
                        attribute for item in material_entries for attribute in item["Attributes"]
                    )}],
                },
            },
            "Boundaries": {
                "PEC": {"Attributes": [100]},
                "Absorbing": {"Attributes": [101], "Order": 1},
            },
            "Solver": {
                "Order": 1,
                "Device": "CPU",
                "Driven": {"Samples": [{
                    "Type": "Point", "Freq": [frequency / 1.0e9], "SaveStep": 1,
                }]},
                "Linear": {"Type": "Default", "KSPType": "GMRES", "Tol": 1.0e-8, "MaxIts": 300},
            },
        }
        config_path = project_directory / "palace-region.json"
        config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        estimated_palace_peak_memory_gib = element_count * 2000.0 / (1024.0 ** 3)
        if estimated_palace_peak_memory_gib > 2.0:
            warnings.append(
                f"Conservative Palace peak-memory estimate is "
                f"{estimated_palace_peak_memory_gib:.2f} GiB; the remote solve may be "
                "terminated on a memory-limited host."
            )
        provenance = {
            "schema": "KIPIDA_PHASE10_PALACE_REGION_1",
            "source_region_input": "input.json",
            "frequency_hz": frequency,
            "mesh_elements": element_count,
            "mesh_nodes": node_count,
            "mesh_resolution_mm": resolution,
            "mesh_characteristic_min_mm": characteristic_min,
            "mesh_characteristic_max_mm": characteristic_max,
            "gmsh_algorithm_3d": mesh_algorithm_3d,
            "estimated_palace_peak_memory_gib": estimated_palace_peak_memory_gib,
            "minimum_retained_track_length_mm": minimum_track_length,
            "copper_clearance_mm": copper_clearance,
            "omitted_short_track_count": omitted_short_tracks,
            "air_margin_mm": air,
            "copper_model": (
                "ZERO_THICKNESS_PEC_SURFACES_AND_SAVED_FINISHED_HOLE_VIA_BARRELS"
                if via_model == "CYLINDRICAL" else
                "ZERO_THICKNESS_PEC_SURFACES_WITH_FINITE_CONDUCTIVITY_SQUARE_SOLID_VIAS"
            ),
            "via_model": via_model,
            "modeled_via_count": len(modeled_vias),
            "omitted_via_count": len(omitted_vias),
            "requested_via_count": len(requested_vias),
            "via_geometry_fallback": via_model != "CYLINDRICAL",
            "excitation_model": (
                "OPPOSED_ROUTED_CURRENT_DIPOLES" if len(dipoles) == 2
                else "ROUTED_CURRENT_DIPOLE"
            ),
            "source_moment_a_m": source_moment,
            "source_injection_lengths_mm": injection_lengths,
            "source_harmonic_order": harmonic_order,
            "calibration": "UNCALIBRATED_ENGINEERING_MODEL",
            "warnings": warnings,
        }
        (project_directory / "palace-region-provenance.json").write_text(
            json.dumps(provenance, indent=2), encoding="utf-8",
        )
        return {
            "status": "PROJECT_GENERATED", "config_path": str(config_path),
            "mesh_path": str(mesh_path), "mesh_elements": element_count,
            "frequency_hz": frequency, "harmonic_order": harmonic_order,
            "source_moment_a_m": source_moment, "dipole_count": len(dipoles),
            "modeled_via_count": len(modeled_vias), "mesh_nodes": node_count,
            "requested_via_count": len(requested_vias),
            "via_model": via_model,
            "via_geometry_fallback": via_model != "CYLINDRICAL",
            "mesh_resolution_mm": resolution,
            "mesh_characteristic_min_mm": characteristic_min,
            "mesh_characteristic_max_mm": characteristic_max,
            "gmsh_algorithm_3d": mesh_algorithm_3d,
            "estimated_palace_peak_memory_gib": estimated_palace_peak_memory_gib,
            "omitted_short_track_count": omitted_short_tracks,
            "warnings": warnings, "elapsed_seconds": time.perf_counter() - started,
        }
    finally:
        gmsh.finalize()


def build(payload, project_directory):
    """Build a Palace project without ever dropping valid saved vias."""
    try:
        return _build_once(payload, project_directory, via_model="CYLINDRICAL")
    except Exception as exc:
        message = str(exc)
        geometry_failure = any(token in message.lower() for token in (
            "plc error", "invalid boundary mesh", "overlapping facets",
            "segment and a facet intersect", "self intersect",
        ))
        if payload.get("vias") and geometry_failure:
            return _build_once(
                payload, project_directory, via_model="CONDUCTIVE_SOLID",
                inherited_warnings=[
                    "The via-inclusive Gmsh model was rejected as a non-conforming PLC "
                    f"({message}); all saved vias were retained as conformal square "
                    "finite-conductivity copper volumes with captured lands and "
                    "full saved layer span."
                ],
            )
        raise


def main():
    if len(sys.argv) != 4:
        raise SystemExit("usage: phase10_palace_worker.py INPUT_JSON PROJECT_DIR RESULT_JSON")
    input_path = Path(sys.argv[1]).resolve()
    project_directory = Path(sys.argv[2]).resolve()
    result_path = Path(sys.argv[3]).resolve()
    project_directory.mkdir(parents=True, exist_ok=True)
    try:
        payload = json.loads(input_path.read_text(encoding="utf-8"))
        result = build(payload, project_directory)
    except Exception as exc:
        result = {"status": "FAILED", "warnings": [str(exc)]}
    result_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0 if result.get("status") == "PROJECT_GENERATED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
