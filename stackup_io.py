"""Import and validate user-owned stackup profiles without editing KiCad files."""

import json
from pathlib import Path

try:
    from .models import StackupLayerModel, StackupProfile
except (ImportError, ValueError):
    from models import StackupLayerModel, StackupProfile


def stackup_profile_from_dict(data, source="IMPORTED"):
    if "stackup_override" in data:
        data = data["stackup_override"] or {}
    if "differential_profile" in data:
        data = (data["differential_profile"] or {}).get("stackup_override") or {}
    layers = []
    for entry in data.get("layers", []):
        kind = str(entry.get("kind", entry.get("type", "DIELECTRIC"))).upper()
        layer_id = entry.get("layer_id")
        layers.append(StackupLayerModel(
            name=str(entry.get("name", "")),
            kind=kind,
            thickness_mm=float(entry.get("thickness_mm", entry.get("thickness", 0.0))),
            layer_id=int(layer_id) if layer_id is not None else None,
            material=str(entry.get("material", "")),
            epsilon_r=float(entry.get("epsilon_r", 1.0 if kind == "COPPER" else 4.4)),
            loss_tangent=float(entry.get("loss_tangent", 0.0)),
        ))
    profile = StackupProfile(
        layers=layers,
        source=source,
        trustworthy=bool(data.get("trustworthy", True)),
        warnings=list(data.get("warnings", [])),
    )
    validate_stackup_profile(profile)
    return profile


def validate_stackup_profile(profile):
    copper = [layer for layer in profile.layers if layer.kind == "COPPER"]
    if len(copper) < 2:
        raise ValueError("A stackup requires at least two copper layers.")
    ids = [layer.layer_id for layer in copper]
    if any(layer_id is None for layer_id in ids):
        raise ValueError("Every copper layer requires its KiCad layer_id.")
    if len(set(ids)) != len(ids):
        raise ValueError("Copper layer_id values must be unique.")
    for index, layer in enumerate(profile.layers):
        if layer.thickness_mm <= 0:
            raise ValueError(f"Layer {index + 1} ({layer.name}) has no positive thickness.")
        if layer.kind != "COPPER" and layer.epsilon_r <= 1.0:
            raise ValueError(f"Dielectric {layer.name} requires epsilon_r > 1.")
    for first, second in zip(profile.layers, profile.layers[1:]):
        if first.kind == "COPPER" and second.kind == "COPPER":
            raise ValueError("Adjacent copper layers require a dielectric between them.")
    return profile


def load_stackup_profile(filepath):
    path = Path(filepath)
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    return stackup_profile_from_dict(data, source="IMPORTED")


def stackup_profile_to_dict(profile):
    return {
        "source": profile.source,
        "trustworthy": profile.trustworthy,
        "warnings": list(profile.warnings),
        "layers": [{
            "name": layer.name,
            "kind": layer.kind,
            "layer_id": layer.layer_id,
            "thickness_mm": layer.thickness_mm,
            "material": layer.material,
            "epsilon_r": layer.epsilon_r,
            "loss_tangent": layer.loss_tangent,
        } for layer in profile.layers],
    }
