"""Reusable, traceable loss models for power-conversion stages.

The estimator intentionally keeps an efficiency fallback separate from the
physical mechanisms.  This prevents a legacy ``Pin - Pout`` figure being
silently deposited into one IC while still allowing an incomplete datasheet to
close the power balance and make its limitation visible to the user.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Tuple

try:
    from .models import LossContribution, PowerStageResult, VoltageRegulator
except (ImportError, ValueError):
    from models import LossContribution, PowerStageResult, VoltageRegulator


def _number(value: Any, default: float = 0.0) -> float:
    if isinstance(value, dict):
        value = value.get("value", default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _provenance(value: Any, default_source: str = "configuration") -> Dict[str, Any]:
    if isinstance(value, dict):
        return {
            "source": value.get("source", default_source),
            "confidence": value.get("confidence", "medium"),
            "reference": value.get("reference", ""),
            "condition": value.get("condition", ""),
            "typical_or_max": value.get("typical_or_max", ""),
        }
    return {"source": default_source, "confidence": "medium", "reference": ""}


def interpolate_efficiency(table: Iterable[Dict[str, Any]], vin_v: float, vout_v: float,
                           iout_a: float) -> float | None:
    """Interpolate Iout curves, then Vout and Vin, with endpoint clamping."""
    rows = [row for row in table if _number(row.get("efficiency"), -1.0) > 0.0]
    if not rows:
        return None

    def interpolate(points, target):
        ordered = sorted(points)
        if target <= ordered[0][0] or len(ordered) == 1:
            return ordered[0][1]
        if target >= ordered[-1][0]:
            return ordered[-1][1]
        for (x0, y0), (x1, y1) in zip(ordered, ordered[1:]):
            if x0 <= target <= x1:
                return y0 + (target - x0) * (y1 - y0) / (x1 - x0)
        return ordered[-1][1]

    curves: Dict[Tuple[float, float], List[Tuple[float, float]]] = {}
    for row in rows:
        key = (_number(row.get("vin_v"), vin_v), _number(row.get("vout_v"), vout_v))
        curves.setdefault(key, []).append((
            _number(row.get("iout_a")), _number(row.get("efficiency"))
        ))
    by_vin: Dict[float, List[Tuple[float, float]]] = {}
    for (curve_vin, curve_vout), points in curves.items():
        by_vin.setdefault(curve_vin, []).append((
            curve_vout, interpolate(points, iout_a)
        ))
    vin_points = [
        (curve_vin, interpolate(vout_points, vout_v))
        for curve_vin, vout_points in by_vin.items()
    ]
    return interpolate(vin_points, vin_v)


def _efficiency_table_applicable(model: Dict[str, Any], vin_v: float, vout_v: float) -> bool:
    validity = dict(model.get("efficiency_table_validity", {}) or {})
    if not validity:
        return True
    return (
        _number(validity.get("vin_min_v"), -math.inf) <= vin_v <=
        _number(validity.get("vin_max_v"), math.inf) and
        _number(validity.get("vout_min_v"), -math.inf) <= vout_v <=
        _number(validity.get("vout_max_v"), math.inf)
    )


def _temperature_multiplier(model: Dict[str, Any], temperature_c: float) -> float:
    table = list(model.get("temperature_multiplier_table", []) or [])
    if table:
        ordered = sorted(table, key=lambda row: _number(row.get("temperature_c")))
        if temperature_c <= _number(ordered[0].get("temperature_c")):
            return _number(ordered[0].get("multiplier"), 1.0)
        if temperature_c >= _number(ordered[-1].get("temperature_c")):
            return _number(ordered[-1].get("multiplier"), 1.0)
        for left, right in zip(ordered, ordered[1:]):
            x0, x1 = _number(left.get("temperature_c")), _number(right.get("temperature_c"))
            if x0 <= temperature_c <= x1 and x1 > x0:
                y0, y1 = _number(left.get("multiplier"), 1.0), _number(right.get("multiplier"), 1.0)
                return y0 + (temperature_c - x0) * (y1 - y0) / (x1 - x0)
    reference = _number(model.get("reference_temperature_c"), 25.0)
    return max(0.0, 1.0 + _number(model.get("tempco_per_c"), 0.0) * (temperature_c - reference))


def _prefixed_temperature_multiplier(model: Dict[str, Any], prefix: str,
                                     temperature_c: float) -> float:
    """Use a mechanism-specific curve, falling back to the legacy common model."""
    table_key = f"{prefix}_temperature_multiplier_table"
    tempco_key = f"{prefix}_tempco_per_c"
    if model.get(table_key) or model.get(tempco_key) is not None:
        specific = {
            "temperature_multiplier_table": model.get(table_key, []),
            "tempco_per_c": model.get(tempco_key),
            "reference_temperature_c": model.get("reference_temperature_c", 25.0),
        }
        return _temperature_multiplier(specific, temperature_c)
    return _temperature_multiplier(model, temperature_c)


def _interpolate_temperature_value(table: Iterable[Dict[str, Any]], temperature_c: float,
                                   value_key: str, default: float) -> float:
    points = sorted(
        ((_number(row.get("temperature_c")), _number(row.get(value_key), default))
         for row in (table or [])),
        key=lambda item: item[0],
    )
    if not points:
        return default
    if temperature_c <= points[0][0]:
        return points[0][1]
    if temperature_c >= points[-1][0]:
        return points[-1][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x0 <= temperature_c <= x1:
            return y0 + (temperature_c - x0) * (y1 - y0) / (x1 - x0)
    return default


def _model_temperature(model: Dict[str, Any], component_temperatures_c: Dict[str, float] | None,
                       ref_des: str) -> float:
    """Resolve temperature without hiding whether it came from the coupled solve."""
    if component_temperatures_c and ref_des in component_temperatures_c:
        return float(component_temperatures_c[ref_des])
    return _number(
        model.get("temperature_c"),
        _number(model.get("reference_temperature_c"), 25.0),
    )


def _mosfet_losses(models: Iterable[Dict[str, Any]], current_rms_a: float,
                    component_temperatures_c: Dict[str, float] | None = None
                    ) -> List[LossContribution]:
    losses = []
    for model in models:
        ref_des = str(model.get("ref_des", "")).strip()
        if not ref_des:
            continue
        rds = model.get("rds_on_ohm", 0.0)
        temperature_c = _model_temperature(model, component_temperatures_c, ref_des)
        paths = max(1, int(_number(model.get("parallel_paths"), 1)))
        effective_rds = _number(rds) * _temperature_multiplier(model, temperature_c) / paths
        losses.append(LossContribution(
            ref_des=ref_des,
            mechanism="mosfet-conduction-i2r",
            power_w=current_rms_a * current_rms_a * effective_rds,
            provenance={**_provenance(rds), "temperature_c": temperature_c,
                        "parallel_paths": paths, "effective_rds_on_ohm": effective_rds,
                        "temperature_model": _provenance(
                            model.get("temperature_model_provenance", {}), "estimate"
                        )},
        ))
    return losses


def _inductor_losses(models: Iterable[Dict[str, Any]], current_rms_a: float,
                      component_temperatures_c: Dict[str, float] | None = None
                      ) -> List[LossContribution]:
    losses = []
    for model in models:
        ref_des = str(model.get("ref_des", "")).strip()
        if not ref_des:
            continue
        dcr = model.get("dcr_ohm", 0.0)
        temperature_c = _model_temperature(model, component_temperatures_c, ref_des)
        effective_dcr = _number(dcr) * _temperature_multiplier(model, temperature_c)
        losses.append(LossContribution(
            ref_des=ref_des,
            mechanism="inductor-copper-i2r",
            power_w=current_rms_a * current_rms_a * effective_dcr,
            provenance={**_provenance(dcr), "temperature_c": temperature_c,
                        "effective_dcr_ohm": effective_dcr},
        ))
        # Core loss is only included when the user/datasheet supplied a value.
        core_loss = model.get("core_loss_w")
        if core_loss is not None:
            losses.append(LossContribution(
                ref_des=ref_des, mechanism="inductor-core", power_w=max(0.0, _number(core_loss)),
                provenance=_provenance(core_loss),
            ))
    return losses


def estimate_stage(regulator: VoltageRegulator, vin_v: float, vout_v: float,
                   iout_a: float,
                   component_temperatures_c: Dict[str, float] | None = None
                   ) -> PowerStageResult:
    """Return one stage's component-resolved loss and power balance."""
    model = dict(getattr(regulator, "loss_model", {}) or {})
    kind = str(model.get("kind", "")).lower()
    iout = max(0.0, float(iout_a))
    vin, vout = max(0.0, float(vin_v)), max(0.0, float(vout_v))
    output_power = vout * iout
    losses: List[LossContribution] = []
    warnings: List[str] = []
    warnings.extend(str(item) for item in (model.get("warnings", []) or []))
    efficiency_provenance = "analytic physical-loss model"

    if kind == "mosfet":
        losses.extend(_mosfet_losses(
            model.get("mosfets", []), iout, component_temperatures_c
        ))
    elif kind == "buck":
        ripple = max(0.0, _number(model.get("inductor_ripple_a")))
        irms = math.sqrt(iout * iout + ripple * ripple / 12.0)
        duty = min(1.0, max(0.0, vout / vin)) if vin else 0.0
        controller_ref = str(model.get("controller_ref_des") or regulator.thermal_ref_des or regulator.input_ref_des)
        controller_temperature_c = _model_temperature(
            model, component_temperatures_c, controller_ref
        )
        high_side_multiplier = _prefixed_temperature_multiplier(
            model, "high_side", controller_temperature_c
        )
        low_side_multiplier = _prefixed_temperature_multiplier(
            model, "low_side", controller_temperature_c
        )
        if (component_temperatures_c and controller_ref in component_temperatures_c and
                not model.get("temperature_multiplier_table") and
                not model.get("high_side_temperature_multiplier_table") and
                not model.get("low_side_temperature_multiplier_table") and
                model.get("tempco_per_c") is None):
            warnings.append(
                f"{controller_ref} RDS(on) temperature correction unavailable; "
                "25 C datasheet resistance retained."
            )
        hs = model.get("high_side_rds_on_ohm")
        ls = model.get("low_side_rds_on_ohm")
        if hs is not None:
            losses.append(LossContribution(controller_ref, "buck-high-side-conduction",
                irms * irms * duty * _number(hs) * high_side_multiplier,
                {**_provenance(hs), "temperature_c": controller_temperature_c,
                 "resistance_multiplier": high_side_multiplier,
                 "effective_rds_on_ohm": _number(hs) * high_side_multiplier,
                 "temperature_model": _provenance(
                     model.get("high_side_temperature_model_provenance", {}), "estimate"
                 )}))
        if ls is not None:
            losses.append(LossContribution(controller_ref, "buck-low-side-conduction",
                irms * irms * (1.0 - duty) * _number(ls) * low_side_multiplier,
                {**_provenance(ls), "temperature_c": controller_temperature_c,
                 "resistance_multiplier": low_side_multiplier,
                 "effective_rds_on_ohm": _number(ls) * low_side_multiplier,
                 "temperature_model": _provenance(
                     model.get("low_side_temperature_model_provenance", {}), "estimate"
                 )}))
        fsw = _number(model.get("switching_frequency_hz"))
        transition = model.get("switching_transition_s")
        if fsw > 0.0 and transition is not None:
            losses.append(LossContribution(controller_ref, "buck-switching",
                0.5 * vin * iout * fsw * max(0.0, _number(transition)), _provenance(transition)))
        qg = model.get("gate_charge_c")
        gate_v = _number(model.get("gate_drive_v"))
        if fsw > 0.0 and qg is not None and gate_v > 0.0:
            losses.append(LossContribution(controller_ref, "buck-gate-drive",
                _number(qg) * gate_v * fsw, _provenance(qg)))
        iq = model.get("quiescent_current_a")
        if iq is not None:
            iq_value = _interpolate_temperature_value(
                model.get("quiescent_current_temperature_table", []),
                controller_temperature_c, "current_a", max(0.0, _number(iq)),
            )
            losses.append(LossContribution(controller_ref, "buck-quiescent-input",
                vin * max(0.0, iq_value),
                {**_provenance(iq), "temperature_c": controller_temperature_c,
                 "effective_current_a": iq_value}))
        losses.extend(_inductor_losses(
            model.get("inductors", []), irms, component_temperatures_c
        ))
        losses.extend(_mosfet_losses(
            model.get("mosfets", []), irms, component_temperatures_c
        ))
    elif str(regulator.reg_type).upper() == "LINEAR":
        # Legacy LDO/pass-device behaviour stays intact when no physical model exists.
        losses.append(LossContribution(
            regulator.thermal_ref_des or regulator.input_ref_des or regulator.output_ref_des,
            "linear-voltage-drop", max(0.0, vin - vout) * iout,
            {"source": "legacy linear model", "confidence": "medium"},
        ))
        efficiency_provenance = "linear voltage-drop model"

    known_loss = sum(item.power_w for item in losses)
    table_efficiency = (
        interpolate_efficiency(model.get("efficiency_table", []), vin, vout, iout)
        if _efficiency_table_applicable(model, vin, vout) else None
    )
    fallback_efficiency = _number(model.get("fallback_efficiency"), _number(regulator.efficiency, 0.0))
    selected_efficiency = table_efficiency
    if selected_efficiency is not None:
        table_source = _provenance(model.get("efficiency_table_source", {}), "datasheet")
        efficiency_provenance = (
            "interpolated efficiency table; "
            f"source={table_source['source']}; confidence={table_source['confidence']}"
            + (f"; {table_source['reference']}" if table_source.get("reference") else "")
        )
    elif (kind == "buck" or str(regulator.reg_type).upper() == "SWITCHING") and 0.0 < fallback_efficiency <= 1.0:
        selected_efficiency = fallback_efficiency
        fallback_source = _provenance(model.get("fallback_efficiency"))
        efficiency_provenance = (
            "configured efficiency fallback (incomplete physical data); "
            f"source={fallback_source['source']}; confidence={fallback_source['confidence']}"
            + (f"; {fallback_source['reference']}" if fallback_source.get("reference") else "")
        )

    if selected_efficiency is not None and output_power > 0.0:
        target_loss = output_power * (1.0 / selected_efficiency - 1.0)
        residual = target_loss - known_loss
        if residual > 1e-15:
            losses.append(LossContribution(
                str(model.get("controller_ref_des") or regulator.thermal_ref_des or regulator.input_ref_des),
                str(model.get("residual_mechanism") or "unmodelled-conversion-residual"), residual,
                {"source": "efficiency fallback" if table_efficiency is None else "efficiency table residual",
                 "confidence": "low" if table_efficiency is None else "medium",
                 "reference": str(model.get("residual_reference", ""))},
            ))
            target_source = "efficiency-table" if table_efficiency is not None else "efficiency-fallback"
            warnings.append(
                f"Switching/core losses are incomplete; the {target_source} residual closes "
                "the stage power target."
            )
        elif residual < -max(1e-12, target_loss * 0.01):
            efficiency_provenance = (
                "analytic physical-loss model; configured efficiency target rejected because "
                "resolved losses are higher"
            )
            warnings.append(
                "Physical losses exceed the configured efficiency target; no negative residual was applied. "
                "Switching/core losses remain incomplete, so this is not an upper bound."
            )

    total_loss = sum(item.power_w for item in losses)
    input_power = output_power + total_loss
    iin = input_power / vin if vin > 0.0 else 0.0
    actual_efficiency = output_power / input_power if input_power > 0.0 else 1.0
    balance_error = abs(input_power - output_power - total_loss) / max(input_power, 1e-12) * 100.0
    if balance_error > 1.0:
        warnings.append(f"Power balance error {balance_error:.3g}% exceeds 1%.")
    return PowerStageResult(
        name=regulator.name, input_ref_des=regulator.input_ref_des, output_ref_des=regulator.output_ref_des,
        vin_v=vin, vout_v=vout, iin_a=iin, iout_a=iout, efficiency=actual_efficiency,
        efficiency_provenance=efficiency_provenance, losses=losses, warnings=warnings,
        balance_relative_error_pct=balance_error,
    )


def format_power_stage_report(stage: PowerStageResult) -> List[str]:
    """Human-readable loss/provenance block shared by the thermal report."""
    status = "WARNING" if stage.balance_relative_error_pct > 1.0 else "OK"
    lines = [
        f"  - {stage.name}: Vin={stage.vin_v:.4g} V, Vout={stage.vout_v:.4g} V, "
        f"Iin={stage.iin_a:.4g} A, Iout={stage.iout_a:.4g} A, "
        f"eta={stage.efficiency * 100:.3g}% ({stage.efficiency_provenance}), "
        f"Ploss={stage.total_loss_w:.5g} W, balance error="
        f"{stage.balance_relative_error_pct:.4g}% [{status}]",
    ]
    for loss in stage.losses:
        source = loss.provenance.get("source", "unknown")
        confidence = loss.provenance.get("confidence", "unknown")
        reference = loss.provenance.get("reference", "")
        details = []
        if reference:
            details.append(reference)
        if "temperature_c" in loss.provenance:
            details.append(f"T={float(loss.provenance['temperature_c']):.3g} C")
        if "effective_dcr_ohm" in loss.provenance:
            details.append(f"DCR(T)={float(loss.provenance['effective_dcr_ohm']):.5g} ohm")
        if "effective_rds_on_ohm" in loss.provenance:
            details.append(f"RDS(on,T)={float(loss.provenance['effective_rds_on_ohm']):.5g} ohm")
        if "parallel_paths" in loss.provenance:
            details.append(f"parallel paths={int(loss.provenance['parallel_paths'])}")
        temperature_model = loss.provenance.get("temperature_model")
        if isinstance(temperature_model, dict) and temperature_model.get("reference"):
            details.append(
                "temperature model=" + str(temperature_model["reference"]) +
                f" ({temperature_model.get('confidence', 'unknown')} confidence)"
            )
        suffix = "; " + "; ".join(details) if details else ""
        lines.append(
            f"      {loss.ref_des}: {loss.mechanism} = {loss.power_w:.5g} W "
            f"[{source}; confidence={confidence}{suffix}]"
        )
    lines.extend(f"      warning: {warning}" for warning in stage.warnings)
    return lines
