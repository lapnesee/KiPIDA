"""Fast, traceable magnetic-emission models for switching inductors.

The analytical estimate intentionally does not infer a shielding attenuation
from the word "shielded".  A numerical attenuation is applied only when a
manufacturer curve or a user measurement supplies it.
"""

from dataclasses import replace
import math
import re

try:
    from .models import EMCInductorModel
except (ImportError, ValueError):
    from models import EMCInductorModel


TDK_SPM6530_CATALOG = {
    "SPM6530T-2R2M": dict(
        inductance_h=2.2e-6, width_mm=7.1, depth_mm=6.5, height_mm=3.0,
        isat_a=8.4, itemp_a=8.2, shield_state="SHIELDED",
        parameter_reference=(
            "TDK SPM6530 commercial catalog, characteristics table and dimensions, "
            "SPM6530T-2R2M"
        ),
    ),
    "SPM6530T-3R3M": dict(
        inductance_h=3.3e-6, width_mm=7.1, depth_mm=6.5, height_mm=3.0,
        isat_a=7.3, itemp_a=6.8, shield_state="SHIELDED",
        parameter_reference=(
            "TDK SPM6530 commercial catalog, characteristics table and dimensions, "
            "SPM6530T-3R3M"
        ),
    ),
}


def scalar(value, default=0.0):
    if isinstance(value, dict):
        value = value.get("value", default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def parse_inductance_h(text):
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(uH|µH|nH|mH|H)\b", str(text), re.I)
    if not match:
        return 0.0
    scale = {"nh": 1e-9, "uh": 1e-6, "µh": 1e-6, "mh": 1e-3, "h": 1.0}
    return float(match.group(1)) * scale[match.group(2).lower()]


def buck_ripple_current_pp(vin_v, vout_v, inductance_h, frequency_hz):
    """CCM peak-to-peak ripple for an ideal buck at nominal duty ratio."""
    if vin_v <= 0.0 or vout_v <= 0.0 or vout_v >= vin_v:
        return 0.0
    if inductance_h <= 0.0 or frequency_hz <= 0.0:
        return 0.0
    duty = vout_v / vin_v
    return (vin_v - vout_v) * duty / (inductance_h * frequency_hz)


def triangular_harmonic_peak(ripple_pp_a, harmonic_number, duty=0.5, samples=2048):
    """Peak Fourier amplitude of a zero-mean, duty-asymmetric triangular ripple."""
    n = max(1, int(harmonic_number))
    duty = min(1.0 - 1e-6, max(1e-6, float(duty)))
    ripple_pp_a = max(0.0, float(ripple_pp_a))
    if ripple_pp_a <= 0.0:
        return 0.0
    real = imag = 0.0
    for index in range(samples):
        phase = (index + 0.5) / samples
        if phase < duty:
            current = -0.5 * ripple_pp_a + ripple_pp_a * phase / duty
        else:
            current = 0.5 * ripple_pp_a - ripple_pp_a * (phase - duty) / (1.0 - duty)
        angle = -2.0 * math.pi * n * phase
        real += current * math.cos(angle)
        imag += current * math.sin(angle)
    return 2.0 * math.hypot(real, imag) / samples


def apply_catalog(model):
    data = TDK_SPM6530_CATALOG.get(str(model.mpn).upper())
    if not data:
        return model
    updates = {}
    for key, value in data.items():
        current = getattr(model, key)
        if not current or (key == "shield_state" and str(current).upper() == "UNKNOWN"):
            updates[key] = value
    updates.update(parameter_source="datasheet", parameter_confidence="HIGH")
    return replace(model, **updates)


def resolve_inductor_models(configured, footprints, rails, sources):
    """Merge user configuration, live placement and power-tree scenario data."""
    models = {item.ref_des.upper(): apply_catalog(item) for item in configured if item.enabled}
    footprints_by_ref = {item.reference.upper(): item for item in footprints}
    rails_by_name = {rail.net_name: rail for rail in (rails or [])}
    sources = [item for item in sources if item.enabled and str(item.kind).upper() == "SWITCHING"]
    for rail in rails or []:
        for regulator in getattr(rail, "child_regulators", []) or []:
            if str(getattr(regulator, "reg_type", "")).upper() != "SWITCHING":
                continue
            ref_des = str(getattr(regulator, "output_ref_des", "") or "").upper()
            if not ref_des.startswith("L"):
                continue
            footprint = footprints_by_ref.get(ref_des)
            source = next((item for item in sources if footprint and item.net_name in footprint.nets), None)
            if source is None:
                controller = str((getattr(regulator, "loss_model", {}) or {}).get(
                    "controller_ref_des", getattr(regulator, "input_ref_des", "")
                ) or "").upper()
                source = next((item for item in sources if controller and controller in item.name.upper()), None)
            current = float(getattr(source, "current_a", 0.0) or 0.0) if source else 0.0
            model = models.get(ref_des, EMCInductorModel(ref_des=ref_des))
            loss_model = getattr(regulator, "loss_model", {}) or {}
            frequency = scalar(loss_model.get("switching_frequency_hz"))
            vin = float(getattr(rail, "nominal_voltage", 0.0) or 0.0)
            output_rail = rails_by_name.get(getattr(regulator, "output_rail_name", ""))
            vout = float(getattr(output_rail, "nominal_voltage", 0.0) or 0.0)
            inductance = model.inductance_h or parse_inductance_h(footprint.value if footprint else "")
            ripple = model.ripple_current_pp_a or buck_ripple_current_pp(vin, vout, inductance, frequency)
            model = replace(
                model,
                source_name=model.source_name or (source.name if source else ""),
                switching_net=model.switching_net or (source.net_name if source else ""),
                inductance_h=inductance,
                vin_v=model.vin_v or vin,
                vout_v=model.vout_v or vout,
                switching_frequency_hz=model.switching_frequency_hz or frequency,
                output_current_a=model.output_current_a or current,
                ripple_current_pp_a=ripple,
            )
            models[ref_des] = apply_catalog(model)
    return [models[key] for key in sorted(models)]


class TargetedInductorRefiner:
    """Gate for future local material-aware 3-D refinement.

    The current implementation refuses to claim a permeability-based solve
    when complex material data are unavailable, while making that state
    explicit and deterministic in the report.
    """

    @staticmethod
    def status(model):
        if model.shielding_attenuation_db is not None:
            return "CALIBRATED_ATTENUATION_AVAILABLE"
        if str(model.shield_state).upper() == "SHIELDED":
            return "BLOCKED_MISSING_COMPLEX_MATERIAL_OR_FIELD_CURVE"
        return "NOT_REQUIRED_FOR_UNSHIELDED_ANALYTIC_MODEL"
