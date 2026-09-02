"""Isolated Python 3.13 openEMS worker for Ki-PIDA Phase 10."""

import json
import math
import os
from pathlib import Path
import sys
import time

import numpy as np
from CSXCAD import ContinuousStructure
from openEMS import openEMS


def _layer_elevations(layers):
    elevations, z = {}, 0.0
    for layer in layers:
        thickness = max(float(layer.get("thickness_mm", 0.0)), 0.0)
        if str(layer.get("kind", "")).upper() == "COPPER":
            layer_id = layer.get("layer_id")
            if layer_id is not None:
                elevations[int(layer_id)] = z
        z += thickness
    return elevations, max(z, 0.1)


def _track_polygon(track):
    x1, y1 = track["start"]
    x2, y2 = track["end"]
    half = max(float(track["width_mm"]) / 2.0, 0.001)
    dx, dy = x2 - x1, y2 - y1
    length = math.hypot(dx, dy)
    if length <= 1e-12:
        return [[x1 - half, y1 - half], [x1 + half, y1 - half],
                [x1 + half, y1 + half], [x1 - half, y1 + half]]
    nx, ny = -dy / length * half, dx / length * half
    return [[x1 + nx, y1 + ny], [x2 + nx, y2 + ny],
            [x2 - nx, y2 - ny], [x1 - nx, y1 - ny]]


def _point_in_polygon(x, y, polygon):
    """Boundary-tolerant ray casting, kept dependency-free in the solver venv."""
    inside = False
    if len(polygon) < 3:
        return False
    x1, y1 = polygon[-1]
    for x2, y2 in polygon:
        dx, dy = x2 - x1, y2 - y1
        cross = (x - x1) * dy - (y - y1) * dx
        if abs(cross) <= 1e-9 and min(x1, x2) - 1e-9 <= x <= max(x1, x2) + 1e-9 \
                and min(y1, y2) - 1e-9 <= y <= max(y1, y2) + 1e-9:
            return True
        if (y1 > y) != (y2 > y):
            intersection_x = x1 + (y - y1) * dx / dy
            if intersection_x >= x:
                inside = not inside
        x1, y1 = x2, y2
    return inside


def _port_reference(candidate, payload, elevations):
    x, y = float(candidate["x_mm"]), float(candidate["y_mm"])
    signal_layer = int(candidate["layer_id"])
    signal_z = elevations.get(signal_layer)
    if signal_z is None:
        return None
    reference_nets = set(payload.get("reference_nets", []))
    matches = []
    for zone in payload.get("zones", []):
        layer_id = int(zone["layer_id"])
        if zone.get("net_name") not in reference_nets or layer_id == signal_layer:
            continue
        if layer_id in elevations and _point_in_polygon(x, y, zone.get("polygon", [])):
            matches.append((abs(elevations[layer_id] - signal_z), layer_id))
    if not matches:
        return None
    return min(matches)[1]


def _latest_field_maximum(path):
    """Read the latest time-domain vector snapshot from an openEMS HDF5 dump."""
    try:
        import h5py
        with h5py.File(path, "r") as handle:
            group = handle["FieldData/TD"]
            keys = sorted(group.keys(), key=lambda item: int(item))
            if not keys:
                return None, None
            dataset = group[keys[-1]]
            values = np.asarray(dataset[...])
            if values.ndim < 1 or values.shape[0] != 3:
                return None, None
            magnitude = np.sqrt(np.sum(np.abs(values) ** 2, axis=0))
            finite = magnitude[np.isfinite(magnitude)]
            maximum = float(np.max(finite)) if finite.size else None
            time_values = np.asarray(dataset.attrs.get("time", 0.0)).reshape(-1)
            snapshot_time = float(time_values[0]) if time_values.size else 0.0
            return maximum, snapshot_time
    except Exception:
        return None, None


def _candidate_rank(candidate, payload, elevations):
    geometry_rank = {"ROUTED_TRACK": 0, "ZONE_PAD_ANCHORED": 1, "ZONE_REPRESENTATIVE": 2}
    return (
        0 if _port_reference(candidate, payload, elevations) is not None else 1,
        geometry_rank.get(candidate.get("geometry_source", ""), 9),
        candidate.get("x_mm", 0.0), candidate.get("y_mm", 0.0),
    )


def _select_modal_pair(candidates, payload, elevations):
    positive = [item for item in candidates if item.get("conductor_role") == "POSITIVE"]
    negative = [item for item in candidates if item.get("conductor_role") == "NEGATIVE"]
    ranked = []
    for pos in positive:
        pos_ref = _port_reference(pos, payload, elevations)
        for neg in negative:
            neg_ref = _port_reference(neg, payload, elevations)
            distance = math.hypot(
                float(pos["x_mm"]) - float(neg["x_mm"]),
                float(pos["y_mm"]) - float(neg["y_mm"]),
            )
            ranked.append((
                0 if pos_ref is not None and pos_ref == neg_ref else 1,
                0 if int(pos["layer_id"]) == int(neg["layer_id"]) else 1,
                distance, _candidate_rank(pos, payload, elevations),
                _candidate_rank(neg, payload, elevations), pos, neg, pos_ref, neg_ref,
            ))
    if not ranked:
        return None
    selected = min(ranked, key=lambda item: item[:-4])
    return selected[-4], selected[-3], selected[-2], selected[-1]


def _add_vertical_port(fdtd, candidate, reference_layer, elevations, total_thickness,
                       resolution, number, impedance, excitation):
    x, y = float(candidate["x_mm"]), float(candidate["y_mm"])
    signal_z = elevations.get(int(candidate["layer_id"]), total_thickness)
    reference_z = elevations.get(reference_layer) if reference_layer is not None else None
    if reference_z is None:
        reference_z = 0.0 if abs(signal_z) > 1e-9 else total_thickness
    lo, hi = sorted((signal_z, reference_z))
    if hi - lo < 0.01:
        hi = lo + 0.01
    fdtd.AddLumpedPort(
        number, impedance,
        [x - resolution / 2, y - resolution / 2, lo],
        [x + resolution / 2, y + resolution / 2, hi],
        "z", excitation, priority=100, edges2grid="xy",
    )


def build(payload, output_directory):
    started = time.perf_counter()
    warnings = []
    region = payload["region"]
    xmin, ymin, xmax, ymax = region["bounds_mm"]
    resolution = max(float(payload["mesh_resolution_mm"]), 0.01)
    elevations, total_thickness = _layer_elevations(payload["stackup"])
    air = max(3.0, 0.5 * max(xmax - xmin, ymax - ymin))
    zmin, zmax = -air, total_thickness + air
    nx = max(2, int(math.ceil((xmax - xmin + 2 * air) / resolution)))
    ny = max(2, int(math.ceil((ymax - ymin + 2 * air) / resolution)))
    nz = max(2, int(math.ceil((zmax - zmin) / resolution)))
    cells = nx * ny * nz
    if cells > int(payload["maximum_cells"]):
        return {"status": "SKIPPED_CELL_LIMIT", "estimated_cells": cells,
                "warnings": [f"Mesh would contain {cells:,} cells."]}

    maximum_timesteps = max(100, int(payload.get("openems_max_timesteps", 8000)))
    end_criteria = max(1.0e-8, min(0.1, float(payload.get("openems_end_criteria", 1.0e-3))))
    fdtd = openEMS(NrTS=maximum_timesteps, EndCriteria=end_criteria)
    f_start = max(float(payload["frequency_start_hz"]), 1.0)
    f_stop = max(float(payload["frequency_stop_hz"]), f_start)
    fdtd.SetGaussExcite((f_start + f_stop) / 2.0, (f_stop - f_start) / 2.0)
    fdtd.SetBoundaryCond(["PML_8"] * 6)
    csx = ContinuousStructure()
    fdtd.SetCSX(csx)
    grid = csx.GetGrid()
    grid.SetDeltaUnit(1e-3)
    grid.AddLine("x", np.linspace(xmin - air, xmax + air, nx + 1))
    grid.AddLine("y", np.linspace(ymin - air, ymax + air, ny + 1))
    grid.AddLine("z", np.linspace(zmin, zmax, nz + 1))
    stack_boundaries, stack_z = [0.0], 0.0
    for layer in payload["stackup"]:
        stack_z += max(float(layer.get("thickness_mm", 0.0)), 0.0)
        stack_boundaries.append(stack_z)
    grid.AddLine("z", sorted(set(stack_boundaries)))

    z = 0.0
    for index, layer in enumerate(payload["stackup"]):
        thickness = max(float(layer.get("thickness_mm", 0.0)), 0.0)
        if str(layer.get("kind", "")).upper() == "DIELECTRIC" and thickness > 0.0:
            material = csx.AddMaterial(
                f"dielectric_{index}", epsilon=max(float(layer.get("epsilon_r", 1.0)), 1.0),
                kappa=0.0,
            )
            material.AddBox([xmin, ymin, z], [xmax, ymax, z + thickness])
        z += thickness

    metals = {}
    def metal(layer_id):
        if layer_id not in metals:
            metals[layer_id] = csx.AddMetal(f"copper_{layer_id}")
        return metals[layer_id]

    for track in payload["tracks"]:
        layer_id = int(track["layer_id"])
        points = _track_polygon(track)
        metal(layer_id).AddPolygon(
            [[point[0] for point in points], [point[1] for point in points]],
            norm_dir=2, elevation=elevations.get(layer_id, 0.0), priority=20,
        )
    for zone in payload["zones"]:
        layer_id = int(zone["layer_id"])
        points = zone["polygon"]
        if len(points) >= 3:
            metal(layer_id).AddPolygon(
                [[point[0] for point in points], [point[1] for point in points]],
                norm_dir=2, elevation=elevations.get(layer_id, 0.0), priority=10,
            )
    via_metal = csx.AddMetal("vias")
    for via in payload["vias"]:
        x, y = via["position"]
        via_metal.AddCylinder([x, y, 0.0], [x, y, total_thickness], 0.15, priority=30)

    enabled_sources = [source for source in payload["sources"] if source.get("enabled")]
    source_nets = {
        net for source in enabled_sources
        for net in (source.get("net_name", ""), source.get("negative_net_name", "")) if net
    }
    port_candidates = list(payload.get("source_ports", []))
    if not port_candidates:  # Backward compatibility with schema 1 manifests.
        port_candidates = [{
            "source_name": "", "net_name": track["net_name"],
            "x_mm": track["start"][0], "y_mm": track["start"][1],
            "layer_id": track["layer_id"], "geometry_source": "ROUTED_TRACK",
            "confidence": "HIGH", "conductor_role": "SINGLE",
        } for track in payload["tracks"] if track["net_name"] in source_nets]
    port_candidates = [item for item in port_candidates if item.get("net_name") in source_nets]
    selected_ports = []
    port_mode = ""
    port_references = []
    port_excitations = []
    port_leg_impedance = 0.0
    selected_source = enabled_sources[0] if enabled_sources else None
    if selected_source and str(selected_source.get("kind", "")).upper() == "DIFFERENTIAL":
        source_candidates = [
            item for item in port_candidates
            if item.get("source_name") == selected_source.get("name")
        ]
        pair = _select_modal_pair(source_candidates, payload, elevations)
        excitation_mode = str(payload.get("differential_excitation_mode", "DIFFERENTIAL")).upper()
        if excitation_mode not in {"DIFFERENTIAL", "COMMON_MODE"}:
            excitation_mode = "DIFFERENTIAL"
        if pair is not None:
            positive, negative, positive_ref, negative_ref = pair
            if positive_ref is not None and positive_ref == negative_ref:
                amplitudes = (0.5, -0.5) if excitation_mode == "DIFFERENTIAL" else (0.5, 0.5)
                impedance = max(float(payload.get("differential_leg_impedance_ohm", 45.0)), 1.0)
                for number, (candidate, amplitude) in enumerate(
                    ((positive, amplitudes[0]), (negative, amplitudes[1])), start=1,
                ):
                    _add_vertical_port(
                        fdtd, candidate, positive_ref, elevations, total_thickness,
                        resolution, number, impedance, amplitude,
                    )
                    selected_ports.append(candidate)
                    port_references.append(positive_ref)
                    port_excitations.append(amplitude)
                port_mode = f"{excitation_mode}_MODAL"
                port_leg_impedance = impedance
                warnings.append(
                    f"{port_mode} excitation uses two {impedance:g}-ohm lumped legs "
                    "referenced to the same plane; this is not a de-embedded wave port."
                )
            else:
                warnings.append(
                    "Differential/common-mode port rejected because both conductors do not "
                    "share a continuous reference plane at the selected cross-section."
                )
        else:
            warnings.append("Differential/common-mode port rejected because one conductor is missing.")
    elif port_candidates:
        candidate = min(port_candidates, key=lambda item: _candidate_rank(item, payload, elevations))
        reference = _port_reference(candidate, payload, elevations)
        if reference is None:
            warnings.append(
                "No reference-net zone covers the selected source point; the port uses the "
                "opposite board surface as a low-confidence return approximation."
            )
        _add_vertical_port(
            fdtd, candidate, reference, elevations, total_thickness,
            resolution, 1, 50.0, 1.0,
        )
        selected_ports.append(candidate)
        port_excitations.append(1.0)
        if reference is not None:
            port_references.append(reference)
        port_mode = "SINGLE_ENDED"
        port_leg_impedance = 50.0
    else:
        warnings.append("No routed-track or copper-zone source intersects this region; geometry exported without a port.")

    e_dump = csx.AddDump("Et", dump_type=0, file_type=1)
    h_dump = csx.AddDump("Ht", dump_type=1, file_type=1)
    # A near-field scan is a plane measurement, not a volume capture.  The
    # previous full-air-volume dump multiplied HDF5 I/O by every z cell and
    # made small targeted solves appear frozen.
    probe_z = total_thickness + min(3.0, air * 0.75)
    dump_start = [xmin, ymin, probe_z]
    dump_stop = [xmax, ymax, probe_z]
    e_dump.AddBox(dump_start, dump_stop)
    h_dump.AddBox(dump_start, dump_stop)

    output_directory = Path(output_directory)
    output_directory.mkdir(parents=True, exist_ok=True)
    xml_path = output_directory / "phase10-openems.xml"
    csx.Write2XML(str(xml_path))
    status = "GEOMETRY_EXPORTED"
    maximum_e = maximum_h = None
    field_snapshot_time_s = None
    fields_extracted = False
    if payload.get("run_solver") and selected_ports:
        fdtd.Run(str(output_directory), cleanup=False)
        status = "SOLVED_FIELDS_UNCALIBRATED"
        maximum_e, e_time = _latest_field_maximum(output_directory / "Et.h5")
        maximum_h, h_time = _latest_field_maximum(output_directory / "Ht.h5")
        fields_extracted = maximum_e is not None and maximum_h is not None
        field_snapshot_time_s = max(e_time or 0.0, h_time or 0.0) if fields_extracted else None
        if fields_extracted:
            warnings.append(
                "E/H maxima were extracted from the latest time-domain dump; they remain "
                "uncalibrated and are invalid for regulatory limit comparison."
            )
        else:
            warnings.append("openEMS ran, but E/H field maxima could not be extracted from its dumps.")
    return {
        "status": status, "estimated_cells": cells,
        "maximum_timesteps": maximum_timesteps, "end_criteria": end_criteria,
        "observation_plane_z_mm": probe_z,
        "solver_output_path": str(output_directory),
        "maximum_e_v_m": maximum_e, "maximum_h_a_m": maximum_h,
        "fields_extracted": fields_extracted,
        "field_snapshot_time_s": field_snapshot_time_s,
        "port_net_name": selected_ports[0].get("net_name", "") if selected_ports else "",
        "port_net_names": [item.get("net_name", "") for item in selected_ports],
        "port_count": len(selected_ports), "port_mode": port_mode,
        "port_leg_impedance_ohm": port_leg_impedance,
        "port_excitations": port_excitations,
        "port_geometry_source": "+".join(sorted({
            item.get("geometry_source", "") for item in selected_ports
        })) if selected_ports else "",
        "port_confidence": (
            "HIGH" if selected_ports and all(item.get("confidence") == "HIGH" for item in selected_ports)
            else "MEDIUM" if selected_ports else ""
        ),
        "port_reference_layer_id": port_references[0] if port_references else None,
        "port_reference_layer_ids": port_references,
        "elapsed_seconds": time.perf_counter() - started, "warnings": warnings,
    }


def main():
    input_path, result_path = map(Path, sys.argv[1:3])
    try:
        payload = json.loads(input_path.read_text(encoding="utf-8"))
        result = build(payload, result_path.parent / "openems")
    except Exception as exc:
        result = {"status": "FAILED", "warnings": [f"{type(exc).__name__}: {exc}"]}
    result_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0 if result["status"] != "FAILED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
