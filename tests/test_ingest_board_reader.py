"""Tests for ingest.board_reader — .kicad_pcb parser."""

import sys
import os
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ingest.board_reader import read_board

MINI_PCB = """\
(kicad_pcb
    (version 20260206)
    (generator "pcbnew")
    (generator_version "10.0")
    (general
        (thickness 1.6)
    )
    (layers
        (0 "F.Cu" signal)
        (31 "B.Cu" signal)
        (25 "Edge.Cuts" user)
    )
    (setup
        (stackup
            (layer "F.Cu"
                (type "copper")
                (thickness 0.035)
            )
            (layer "dielectric 1"
                (type "core")
                (thickness 1.51)
                (material "FR4")
                (epsilon_r 4.5)
                (loss_tangent 0.02)
            )
            (layer "B.Cu"
                (type "copper")
                (thickness 0.035)
            )
        )
    )
    (footprint "Resistor_SMD:R_0603_1608Metric"
        (layer "F.Cu")
        (uuid "fp-uuid-1")
        (at 10.0 10.0 0)
        (property "Reference" "R1"
            (at 0 0 0)
            (layer "F.SilkS")
            (uuid "prop-r1-ref")
        )
        (property "Value" "10k"
            (at 0 1 0)
            (layer "F.Fab")
            (uuid "prop-r1-val")
        )
        (path "/sheet-uuid/sym-uuid-1")
        (sheetfile "sheet_main.kicad_sch")
        (pad "1" smd roundrect
            (at -0.825 0 0)
            (size 0.8 0.95)
            (layers "F.Cu" "F.Mask" "F.Paste")
            (net "+3V3_MAIN")
            (pintype "passive")
            (uuid "pad-uuid-1")
        )
        (pad "2" smd roundrect
            (at 0.825 0 0)
            (size 0.8 0.95)
            (layers "F.Cu" "F.Mask" "F.Paste")
            (net "GND")
            (pintype "passive")
            (uuid "pad-uuid-2")
        )
    )
    (footprint "Capacitor_SMD:C_0402_1005Metric"
        (layer "F.Cu")
        (uuid "fp-uuid-2")
        (at 20.0 10.0 90)
        (property "Reference" "C1"
            (at 0 0 0)
            (layer "F.SilkS")
            (uuid "prop-c1-ref")
        )
        (property "Value" "100nF"
            (at 0 1 0)
            (layer "F.Fab")
            (uuid "prop-c1-val")
        )
        (path "/sheet-uuid/sym-uuid-2")
        (sheetfile "sheet_main.kicad_sch")
        (pad "1" smd roundrect
            (at -0.5 0 0)
            (size 0.6 0.6)
            (layers "F.Cu" "F.Mask" "F.Paste")
            (net "+3V3_MAIN")
            (pintype "passive")
            (uuid "pad-uuid-3")
        )
        (pad "2" smd roundrect
            (at 0.5 0 0)
            (size 0.6 0.6)
            (layers "F.Cu" "F.Mask" "F.Paste")
            (net "GND")
            (pintype "passive")
            (uuid "pad-uuid-4")
        )
    )
    (segment
        (start 9.175 10.0)
        (end 19.5 10.0)
        (width 0.25)
        (layer "F.Cu")
        (net "+3V3_MAIN")
        (uuid "seg-uuid-1")
    )
    (segment
        (start 10.825 10.0)
        (end 20.5 10.0)
        (width 0.25)
        (layer "F.Cu")
        (net "GND")
        (uuid "seg-uuid-2")
    )
    (segment
        (start 9.0 9.0)
        (end 21.0 9.0)
        (width 1.0)
        (layer "B.Cu")
        (net "+3V3_MAIN")
        (uuid "seg-uuid-3")
    )
    (via
        (at 15.0 10.0)
        (size 0.8)
        (drill 0.4)
        (layers "F.Cu" "B.Cu")
        (net "+3V3_MAIN")
        (uuid "via-uuid-1")
    )
    (zone
        (net "GND")
        (net_name "GND")
        (layer "B.Cu")
        (uuid "zone-uuid-1")
        (connect_pads (clearance 0.5))
        (filled_polygon
            (layer "B.Cu")
            (pts (xy 5 5) (xy 25 5) (xy 25 20) (xy 5 20))
        )
    )
    (gr_rect
        (start 0 0)
        (end 30 25)
        (stroke (width 0.05) (type solid))
        (fill no)
        (layer "Edge.Cuts")
        (uuid "edge-uuid-1")
    )
)
"""


class TestReadBoard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(
            suffix=".kicad_pcb", mode="w", encoding="utf-8", delete=False
        )
        self.tmp.write(MINI_PCB)
        self.tmp.close()
        self.pcb_path = Path(self.tmp.name)
        self.board = read_board(self.pcb_path)

    def tearDown(self):
        self.pcb_path.unlink(missing_ok=True)

    # --- Stackup ---
    def test_stackup_copper_count(self):
        self.assertEqual(self.board.stackup.copper_layer_count, 2)

    def test_stackup_layer_names(self):
        names = [l.name for l in self.board.stackup.layers]
        self.assertIn("F.Cu", names)
        self.assertIn("B.Cu", names)

    def test_stackup_thickness(self):
        # F.Cu 0.035 + core 1.51 + B.Cu 0.035 = 1.58
        self.assertAlmostEqual(self.board.stackup.total_thickness_mm, 1.58, places=3)

    # --- Bounds ---
    def test_bounds_from_edge_cuts(self):
        self.assertAlmostEqual(self.board.bounds.x_min, 0.0)
        self.assertAlmostEqual(self.board.bounds.x_max, 30.0)
        self.assertAlmostEqual(self.board.bounds.y_min, 0.0)
        self.assertAlmostEqual(self.board.bounds.y_max, 25.0)

    def test_bounds_width_height(self):
        self.assertAlmostEqual(self.board.bounds.width_mm, 30.0)
        self.assertAlmostEqual(self.board.bounds.height_mm, 25.0)

    def test_bounds_area(self):
        self.assertAlmostEqual(self.board.bounds.area_mm2, 750.0)

    # --- Footprints ---
    def test_footprint_count(self):
        self.assertEqual(len(self.board.footprints), 2)

    def test_footprint_refs(self):
        refs = {fp.reference for fp in self.board.footprints}
        self.assertIn("R1", refs)
        self.assertIn("C1", refs)

    def test_footprint_values(self):
        r1 = next(fp for fp in self.board.footprints if fp.reference == "R1")
        self.assertEqual(r1.value, "10k")

    def test_footprint_sch_path(self):
        r1 = next(fp for fp in self.board.footprints if fp.reference == "R1")
        self.assertIn("sheet-uuid", r1.sch_path)

    def test_footprint_pads(self):
        r1 = next(fp for fp in self.board.footprints if fp.reference == "R1")
        self.assertEqual(len(r1.pads), 2)
        pad_nets = {p.net_name for p in r1.pads}
        self.assertIn("+3V3_MAIN", pad_nets)
        self.assertIn("GND", pad_nets)

    # --- Segments ---
    def test_segment_count(self):
        self.assertEqual(len(self.board.segments), 3)

    def test_segment_net_names(self):
        nets = {s.net_name for s in self.board.segments}
        self.assertIn("+3V3_MAIN", nets)
        self.assertIn("GND", nets)

    def test_segment_widths(self):
        widths = {s.width_mm for s in self.board.segments}
        self.assertIn(0.25, widths)
        self.assertIn(1.0, widths)

    # --- Vias ---
    def test_via_count(self):
        self.assertEqual(len(self.board.vias), 1)

    def test_via_net(self):
        self.assertEqual(self.board.vias[0].net_name, "+3V3_MAIN")

    def test_via_size(self):
        self.assertAlmostEqual(self.board.vias[0].size_mm, 0.8)

    # --- Zones ---
    def test_zone_count(self):
        self.assertEqual(len(self.board.zones), 1)

    def test_zone_net(self):
        self.assertEqual(self.board.zones[0].net_name, "GND")

    # --- all_net_names ---
    def test_all_net_names(self):
        nets = self.board.all_net_names
        self.assertIn("+3V3_MAIN", nets)
        self.assertIn("GND", nets)


class TestZoneIslandsAndLayers(unittest.TestCase):
    """Every filled island of a zone is copper, on every layer it fills.

    The reader kept the largest island of a zone's single layer. A zone declared
    with (layers ...) had no layer, so all its fills were dropped -- 8 of 24 zones
    on the reference board, and zones of that form on 5 of the KiCad 10 demo
    projects; a pour split into islands lost all but one.
    """

    ZONES = MINI_PCB.replace(
        """    (zone
        (net "GND")
        (net_name "GND")
        (layer "B.Cu")
        (uuid "zone-uuid-1")
        (connect_pads (clearance 0.5))
        (filled_polygon
            (layer "B.Cu")
            (pts (xy 5 5) (xy 25 5) (xy 25 20) (xy 5 20))
        )
    )""",
        """    (zone
        (net "GND")
        (net_name "GND")
        (layers "F.Cu" "B.Cu")
        (uuid "zone-uuid-1")
        (polygon (pts (xy 0 0) (xy 30 0) (xy 30 25) (xy 0 25)))
        (filled_polygon (layer "F.Cu") (pts (xy 1 1) (xy 9 1) (xy 9 9) (xy 1 9)))
        (filled_polygon (layer "F.Cu") (pts (xy 20 1) (xy 29 1) (xy 29 9) (xy 20 9)))
        (filled_polygon (layer "B.Cu") (pts (xy 1 1) (xy 29 1) (xy 29 24) (xy 1 24)))
    )
    (zone
        (net "GND")
        (net_name "GND")
        (layers "F.Cu" "B.Cu")
        (uuid "zone-uuid-2")
        (polygon (pts (xy 0 0) (xy 5 0) (xy 5 5) (xy 0 5)))
    )""",
    )

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(
            suffix=".kicad_pcb", mode="w", encoding="utf-8", delete=False
        )
        self.tmp.write(self.ZONES)
        self.tmp.close()
        self.pcb_path = Path(self.tmp.name)
        self.board = read_board(self.pcb_path)

    def tearDown(self):
        self.pcb_path.unlink(missing_ok=True)

    def test_every_island_on_every_layer_is_kept(self):
        filled = sorted((zone.layer, zone.filled_polygon[0]) for zone in self.board.zones if zone.filled_polygon)
        self.assertEqual(filled, [
            ("B.Cu", (1.0, 1.0)), ("F.Cu", (1.0, 1.0)), ("F.Cu", (20.0, 1.0)),
        ])

    def test_an_unfilled_multi_layer_zone_keeps_its_outline_per_layer(self):
        unfilled = sorted(zone.layer for zone in self.board.zones if not zone.filled_polygon)
        self.assertEqual(unfilled, ["B.Cu", "F.Cu"])


class TestRotatedPadPositions(unittest.TestCase):
    """A rotated footprint's pads sit where KiCad draws them.

    KiCad's Y axis points down, so turning a footprint by +90 degrees moves a
    pad at local +X to -Y. The reader applied the Y-up matrix and mirrored the
    pads of every footprint turned by anything but 0 or 180 degrees; tracks,
    which KiCad lands on pad centres, then missed them -- 3 of 69 pads at 90
    degrees on the reference board matched a track end, 42 with the right sign.
    """

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(
            suffix=".kicad_pcb", mode="w", encoding="utf-8", delete=False
        )
        self.tmp.write(MINI_PCB)
        self.tmp.close()
        self.pcb_path = Path(self.tmp.name)
        self.board = read_board(self.pcb_path)

    def tearDown(self):
        self.pcb_path.unlink(missing_ok=True)

    def test_a_quarter_turn_moves_local_minus_x_to_plus_y(self):
        # C1 sits at (20, 10) turned 90 degrees; its pad 1 is at local (-0.5, 0).
        c1 = next(fp for fp in self.board.footprints if fp.reference == "C1")
        pad = next(p for p in c1.pads if p.number == "1")
        self.assertAlmostEqual(pad.position[0], 20.0, places=6)
        self.assertAlmostEqual(pad.position[1], 10.5, places=6)

    def test_an_unrotated_footprint_is_unchanged(self):
        r1 = next(fp for fp in self.board.footprints if fp.reference == "R1")
        pad = next(p for p in r1.pads if p.number == "1")
        self.assertAlmostEqual(pad.position[0], 9.175, places=6)
        self.assertAlmostEqual(pad.position[1], 10.0, places=6)


class TestBoardMissingFile(unittest.TestCase):
    def test_missing_file_raises(self):
        with self.assertRaises((OSError, ValueError)):
            read_board(Path("/nonexistent/board.kicad_pcb"))


if __name__ == "__main__":
    unittest.main()
