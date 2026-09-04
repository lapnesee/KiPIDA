"""Surface heat-transfer coefficients for a board exchanging with still or moving air.

The thermal mesh previously used a single hardcoded 5.0 W/m^2K for natural
convection on every exposed face, and linearised radiation about the *ambient*
temperature.  Both are wrong in the same direction -- they under-predict the
heat leaving the board, so the solver over-predicts its temperature.

Everything here is a closed-form correlation on the board's own geometry and
solved temperature.  Nothing is fitted to a particular board, and every
constant carries its source in the docstring, so a reader can check the
physics rather than trust a number.

Validity is stated per function and enforced where it matters: a correlation
applied outside its Rayleigh or Reynolds range returns a value flagged by
:func:`natural_convection_h`'s ``in_range`` companion rather than silently
extrapolating.
"""

import math

# Stefan-Boltzmann constant, W/m^2K^4 (CODATA).
SIGMA = 5.670374419e-8

# Standard gravity, m/s^2.
GRAVITY = 9.80665

# Prandtl number of air.  Varies only between 0.71 and 0.70 across 250-400 K,
# so a constant is accurate to better than 1% over the whole range of interest.
PRANDTL_AIR = 0.707

# Rayleigh range over which the laminar horizontal-plate correlations below
# hold.  Outside it the coefficient is still returned -- refusing to answer is
# worse than answering with a stated caveat -- but ``natural_convection_in_range``
# reports false so the caller can qualify the result.
RAYLEIGH_LAMINAR_MIN = 1.0e4
RAYLEIGH_LAMINAR_MAX = 1.0e7

# Flat-plate transition Reynolds number.  The enclosure CFD is laminar, so a
# forced-convection coefficient derived from it is only meaningful below this.
REYNOLDS_LAMINAR_MAX = 5.0e5


def air_thermal_conductivity(temperature_c):
    """Thermal conductivity of air, W/m.K, at *temperature_c*.

    Power-law fit to standard tables, k = 0.02624 * (T/300)^0.8646, accurate to
    about 1% over 250-400 K.
    """
    kelvin = max(1.0, float(temperature_c) + 273.15)
    return 0.02624 * (kelvin / 300.0) ** 0.8646


def air_kinematic_viscosity(temperature_c):
    """Kinematic viscosity of air, m^2/s, at *temperature_c*.

    Power-law fit, nu = 1.5689e-5 * (T/300)^1.7, accurate to about 2% over
    250-400 K.  The exponent absorbs both the Sutherland viscosity rise and
    the ideal-gas density fall.
    """
    kelvin = max(1.0, float(temperature_c) + 273.15)
    return 1.5689e-5 * (kelvin / 300.0) ** 1.7


def characteristic_length_m(area_mm2, perimeter_mm):
    """Horizontal-plate characteristic length L = A / P, in metres.

    This is the length scale the plate correlations are defined against
    (Incropera), not the board's longest side.  An 80 x 35 mm board gives
    12.2 mm, not 80 mm -- using the side length instead would inflate Rayleigh
    by a factor of 280 and the coefficient by about 4.
    """
    area = max(0.0, float(area_mm2))
    perimeter = max(1.0e-9, float(perimeter_mm))
    return (area / perimeter) * 1.0e-3


def rayleigh_number(delta_t_k, length_m, film_temperature_c):
    """Ra = g.beta.dT.L^3 / (nu.alpha), with air properties at the film temperature.

    Returns 0.0 for a non-positive temperature difference or length, which
    makes the caller fall back to a floor rather than divide by zero.
    """
    delta_t = float(delta_t_k)
    length = float(length_m)
    if delta_t <= 0.0 or length <= 0.0:
        return 0.0
    nu = air_kinematic_viscosity(film_temperature_c)
    alpha = nu / PRANDTL_AIR
    beta = 1.0 / (float(film_temperature_c) + 273.15)
    return GRAVITY * beta * delta_t * length ** 3 / (nu * alpha)


def natural_convection_in_range(rayleigh):
    """True when *rayleigh* lies inside the laminar correlation's stated range."""
    return RAYLEIGH_LAMINAR_MIN <= float(rayleigh) <= RAYLEIGH_LAMINAR_MAX


def natural_convection_h(delta_t_k, length_m, facing="up", ambient_c=25.0,
                         minimum_h=2.0):
    """Natural-convection coefficient for a heated horizontal plate, W/m^2K.

    ``facing="up"`` uses Nu = 0.54.Ra^(1/4); ``facing="down"`` uses
    Nu = 0.27.Ra^(1/4).  Both are the standard laminar horizontal-plate
    correlations (Incropera, Fundamentals of Heat and Mass Transfer).  The
    factor of two between them is real: a hot surface facing up drives a plume,
    one facing down traps the boundary layer against itself.

    Edges are treated as facing down.  A board edge is a narrow vertical strip
    whose contribution is small; taking the pessimistic branch keeps the
    result conservative rather than inventing a vertical-plate correlation for
    a 1.6 mm strip.

    *minimum_h* floors the result so a near-isothermal board does not report a
    coefficient of zero, which would make the surface adiabatic.
    """
    delta_t = max(0.0, float(delta_t_k))
    film_c = float(ambient_c) + delta_t / 2.0
    rayleigh = rayleigh_number(delta_t, length_m, film_c)
    if rayleigh <= 0.0:
        return float(minimum_h)
    coefficient = 0.54 if str(facing).lower() == "up" else 0.27
    nusselt = coefficient * rayleigh ** 0.25
    conductivity = air_thermal_conductivity(film_c)
    h = nusselt * conductivity / max(float(length_m), 1.0e-9)
    return max(float(minimum_h), float(h))


def radiation_h(emissivity, surface_c, ambient_c):
    """Radiative coefficient linearised about the *surface* temperature, W/m^2K.

    h_r = eps.sigma.(Ts^2 + Ta^2).(Ts + Ta)

    This is exact, not an approximation: it is the algebraic factorisation of
    sigma.(Ts^4 - Ta^4) = h_r.(Ts - Ta).  The thermal mesh previously used
    4.eps.sigma.Ta^3, linearising about ambient, which under-predicts whenever
    the surface is hotter than ambient -- 5.4 against 7.0 W/m^2K for a 78 C
    surface at 25 C ambient.

    Note that the tangent form 4.eps.sigma.Ts^3 over-predicts by the same
    argument (8.8 for that case); the secant form here is the one that
    reproduces the fourth-power law exactly at both endpoints.
    """
    eps = max(0.0, min(1.0, float(emissivity)))
    if eps <= 0.0:
        return 0.0
    surface_k = float(surface_c) + 273.15
    ambient_k = float(ambient_c) + 273.15
    return eps * SIGMA * (surface_k ** 2 + ambient_k ** 2) * (surface_k + ambient_k)


def forced_convection_in_range(reynolds):
    """True while the flat-plate laminar forced correlation still applies."""
    return 0.0 < float(reynolds) <= REYNOLDS_LAMINAR_MAX


def reynolds_number(velocity_m_s, length_m, film_temperature_c):
    """Re = u.L / nu, with air properties at the film temperature."""
    velocity = max(0.0, float(velocity_m_s))
    length = max(0.0, float(length_m))
    if velocity <= 0.0 or length <= 0.0:
        return 0.0
    return velocity * length / air_kinematic_viscosity(film_temperature_c)


def forced_convection_h(velocity_m_s, length_m, ambient_c=25.0, delta_t_k=0.0,
                        minimum_h=0.0):
    """Average forced-convection coefficient over a flat plate, W/m^2K.

    Nu = 0.664.Re^(1/2).Pr^(1/3), the laminar flat-plate average (Incropera).
    Above Re = 5e5 the boundary layer transitions and this under-predicts; the
    caller should consult :func:`forced_convection_in_range` and say so rather
    than let the number pass unqualified.

    Returns 0.0 for still air, so that :func:`combined_h` reduces exactly to
    the natural-convection branch.
    """
    film_c = float(ambient_c) + max(0.0, float(delta_t_k)) / 2.0
    reynolds = reynolds_number(velocity_m_s, length_m, film_c)
    if reynolds <= 0.0:
        return float(minimum_h)
    nusselt = 0.664 * math.sqrt(reynolds) * PRANDTL_AIR ** (1.0 / 3.0)
    conductivity = air_thermal_conductivity(film_c)
    h = nusselt * conductivity / max(float(length_m), 1.0e-9)
    return max(float(minimum_h), float(h))


def combined_h(natural_h, forced_h, exponent=3.0):
    """Blend natural and forced convection, h = (h_nat^n + h_for^n)^(1/n).

    Churchill's exponent n = 3 for assisting mixed convection.  Choosing the
    larger of the two would discard the other entirely; this reduces to either
    branch when the other vanishes, which is what makes it safe to apply
    unconditionally.
    """
    natural = max(0.0, float(natural_h))
    forced = max(0.0, float(forced_h))
    if forced <= 0.0:
        return natural
    if natural <= 0.0:
        return forced
    n = max(1.0, float(exponent))
    return (natural ** n + forced ** n) ** (1.0 / n)
