"""Physical constants shared by every analysis engine, each defined once.

Before this module the DC analysis modelled copper at 1.68e-8 ohm-m and the DC
advisor at 1.72e-8, so a voltage-drop finding and the fix sized for it
disagreed by 2.4 % before any geometry was considered; a plated hole with no
parsed plating was 25 um in one engine and 20 um in another; the board and
copper thickness fallbacks were written out in a dozen places. A value that has
to change must change for every engine at once, and ``tests/
test_physical_constants_single_source.py`` fails if a second copy appears.

Every value here is a fallback or a material property. Geometry read from the
board -- stackup thicknesses, drill sizes, annular rings -- always wins.
"""

# Resistivity of annealed copper at 20 C. IEC 60028 defines the standard as
# 1/58 ohm-mm2/m = 1.7241e-8 ohm-m; PCB copper, electro-deposited or rolled,
# sits within a few percent of it. Rounded to the value the validated hybrid
# path has always used.
COPPER_RESISTIVITY_OHM_M = 1.72e-8
COPPER_RESISTIVITY_OHM_MM = COPPER_RESISTIVITY_OHM_M * 1e3
COPPER_RESISTIVITY_REFERENCE_C = 20.0

# IEC 60028 temperature coefficient of resistance of annealed copper at 20 C.
COPPER_TEMPERATURE_COEFFICIENT_PER_C = 0.00393

# Thermal conductivity of copper near room temperature.
COPPER_THERMAL_CONDUCTIVITY_W_MK = 385.0

# Copper thickness when the stackup does not state one: 1 oz/ft2 as a finished
# nominal. One ounce is 34.8 um exactly; the advisor needs the exact figure to
# turn a thickness into a weight a fabricator quotes.
DEFAULT_COPPER_THICKNESS_MM = 0.035
COPPER_THICKNESS_PER_OUNCE_MM = 0.0348

# Barrel plating of a via or plated pad when the geometry does not give it.
# IPC-6012 Class 2 minimum average for through holes. A fallback must not
# flatter the model, so the floor rather than a typical value.
PLATING_THICKNESS_FALLBACK_MM = 0.020

# Finished board thickness when the stackup does not state one.
DEFAULT_BOARD_THICKNESS_MM = 1.6
