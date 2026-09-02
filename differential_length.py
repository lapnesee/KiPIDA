"""Interface-aware length-symmetry checks for differential pairs."""

from dataclasses import dataclass
import math


# Shared with the EMC analyser. Values preserve Ki-PIDA's existing protocol
# limits while fixing exact/specific interface selection (USB_SS before USB).
PROTOCOL_SKEW_LIMIT_PS = {
    "USB_SS": 5.0,
    "PCIE": 5.0,
    "HDMI": 20.0,
    "USB_HS": 25.0,
    "USB": 25.0,
    "ETHERNET": 50.0,
    "SATA": 20.0,
    "MIPI": 5.0,
    "LVDS": 20.0,
    "CAN": 100.0,
    "RS485": 100.0,
    "DDR": 10.0,
    "GENERIC": 50.0,
}


def protocol_skew_limit_ps(interface):
    token = str(interface or "GENERIC").strip().upper()
    if token in PROTOCOL_SKEW_LIMIT_PS:
        return PROTOCOL_SKEW_LIMIT_PS[token]
    for name in sorted(PROTOCOL_SKEW_LIMIT_PS, key=len, reverse=True):
        if name != "GENERIC" and name in token:
            return PROTOCOL_SKEW_LIMIT_PS[name]
    return PROTOCOL_SKEW_LIMIT_PS["GENERIC"]


@dataclass(frozen=True)
class LengthSymmetryAssessment:
    positive_length_mm: float
    negative_length_mm: float
    mismatch_mm: float
    mismatch_pct: float
    estimated_skew_ps: float
    skew_limit_ps: float
    maximum_mismatch_mm: float
    margin_ps: float
    status: str
    shorter_polarity: str


def assess_length_symmetry(positive_tracks, negative_tracks, interface, epsilon_effective):
    positive = sum(max(0.0, float(item.get("length_mm", 0.0))) for item in positive_tracks)
    negative = sum(max(0.0, float(item.get("length_mm", 0.0))) for item in negative_tracks)
    mismatch = abs(positive - negative)
    average = 0.5 * (positive + negative)
    mismatch_pct = 100.0 * mismatch / max(average, 1e-12) if average > 0.0 else 0.0
    epsilon = max(1.0, float(epsilon_effective or 1.0))
    delay_ps_per_mm = math.sqrt(epsilon) / 0.299792458
    skew = mismatch * delay_ps_per_mm
    limit = protocol_skew_limit_ps(interface)
    maximum_mismatch = limit / delay_ps_per_mm
    if positive <= 0.0 or negative <= 0.0:
        status = "NO_DATA"
    elif skew > limit + 1e-12:
        status = "FAIL"
    elif skew > 0.5 * limit + 1e-12:
        status = "MARGINAL"
    else:
        status = "PASS"
    shorter = "NONE"
    if mismatch > 1e-12:
        shorter = "POSITIVE" if positive < negative else "NEGATIVE"
    return LengthSymmetryAssessment(
        positive, negative, mismatch, mismatch_pct, skew, limit,
        maximum_mismatch, limit - skew, status, shorter,
    )
