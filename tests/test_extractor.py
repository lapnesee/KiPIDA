
import unittest
import sys
import os
import math
import tempfile
from pathlib import Path

try:
    from shapely.geometry import LineString, Polygon, Point, box
    import shapely.affinity
except ImportError:
    pass

# Include plugin dir
plugin_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if plugin_dir not in sys.path:
    sys.path.insert(0, plugin_dir)

# 1. Pcbnew Mock
class MockPcbnew:
    PAD_SHAPE_CIRCLE = 1
    PAD_SHAPE_RECT = 2
    PAD_SHAPE_ROUNDRECT = 3
    PAD_SHAPE_OVAL = 4
    
    class ActionPlugin: pass
    class PCB_VIA: pass
    class PCB_TRACK: pass
    
    @staticmethod
    def ToMM(val): return val / 1000000.0
    
    @staticmethod
    def IsCopperLayer(lid): return lid in [0, 31]

if 'pcbnew' not in sys.modules:
    sys.modules['pcbnew'] = MockPcbnew

import pcbnew

# 2. Other Mocks
class MockBoard:
    def __init__(self):
        self.tracks = []
        self.footprints = []
        self.zones = []
        self.design_settings = MockDesignSettings()
        self.layers_copper = {0: "F_Cu", 31: "B_Cu"} # Example IDs

    def GetDesignSettings(self): return self.design_settings
    def GetEnabledLayers(self): return MockSeq(list(self.layers_copper.keys()))
    def IsLayerCopper(self, layer_id): return layer_id in self.layers_copper
    def GetLayerName(self, layer_id): return self.layers_copper.get(layer_id, "Unknown")
    def FindNet(self, name): return MockNet(1, name)
    def GetTracks(self): return self.tracks
    def GetFootprints(self): return self.footprints
    def GetZones(self): return self.zones
    def Zones(self): return self.zones

class MockSeq:
    def __init__(self, items): self.items = items
    def Seq(self): return self.items

class MockDesignSettings:
    def GetStackupDescriptor(self): return None # Not implemented in mock yet

class MockNet:
    def __init__(self, code, name):
        self.code = code
        self.name = name
    def GetNetCode(self): return self.code

class MockTrack(MockPcbnew.PCB_TRACK):
    def __init__(self, net_code, layer, start, end, width):
        self.net_code = net_code
        self.layer = layer
        self.start = MockPoint(start[0], start[1])
        self.end = MockPoint(end[0], end[1])
        self.width = width
        self.classname = "PCB_TRACK"
    def GetNetCode(self): return self.net_code
    def GetLayer(self): return self.layer
    def GetWidth(self): return self.width
    def GetStart(self): return self.start
    def GetEnd(self): return self.end
    def GetClass(self): return self.classname

class MockPoint:
    def __init__(self, x, y): self.x, self.y = x, y

from extractor import GeometryExtractor

class TestGeometryExtractor(unittest.TestCase):
    def setUp(self):
        self.board = MockBoard()
        self.extractor = GeometryExtractor(self.board, debug=False, log_callback=None)
        
    def test_stackup_defaults(self):
        stackup = self.extractor.get_board_stackup()
        self.assertIn('copper', stackup)
        self.assertIn(0, stackup['copper']) # F_Cu
        self.assertEqual(stackup['copper'][0]['thickness_mm'], 0.035)

    def test_track_extraction(self):
        # 0.5mm width track from 0,0 to 10mm,0
        w = 500000 # 0.5mm in nm
        start = (0,0)
        end = (10000000, 0) # 10mm
        
        t = MockTrack(1, 0, start, end, w)
        self.board.tracks.append(t)
        
        geo = self.extractor.get_net_geometry("TestNet")
        
        self.assertIn(0, geo)
        poly = geo[0]
        
        # safety_buffer was removed in Phase 1 (HybridMesher / exact geometry
        # made the rasterization inflation unnecessary and non-conservative).
        radius = 0.25
        self.assertAlmostEqual(
            poly.area, 10.0 * (2.0 * radius) + math.pi * radius**2,
            delta=0.01,
        )

    def test_unmerged_geometry_collection_for_dc_rasterization(self):
        self.board.tracks.extend([
            MockTrack(1, 0, (0, 0), (10000000, 0), 500000),
            MockTrack(1, 0, (0, 1000000), (10000000, 1000000), 500000),
        ])
        geometry = self.extractor.get_net_geometry("TestNet", merge=False)
        self.assertEqual(geometry[0].geom_type, "GeometryCollection")
        self.assertEqual(len(geometry[0].geoms), 2)

    def test_track_classification_geometry_uses_physical_width(self):
        self.board.tracks.append(
            MockTrack(1, 0, (0, 0), (10_000_000, 0), 500_000),
        )
        geometry = self.extractor.get_track_geometry("TestNet")
        expected_area = 10.0 * 0.5 + math.pi * 0.25 ** 2
        self.assertAlmostEqual(geometry[0].area, expected_area, delta=0.01)

    def test_an_arc_track_is_classified_along_the_arc_itself(self):
        # Start, mid and end alone cut a quarter turn's corner by 7.6 % of its
        # radius; this copper is compared with the saved file's, which reads
        # the arc as 10-degree chords.
        radius, width = 5_000_000, 300_000
        arc = MockTrack(1, 0, (radius, 0), (0, radius), width)
        arc.mid = MockPoint(radius * math.cos(math.pi / 4), radius * math.sin(math.pi / 4))
        self.board.tracks.append(arc)
        geometry = self.extractor.get_track_geometry("TestNet")
        exact = math.pi / 2 * 5.0 * 0.3 + math.pi * 0.15 ** 2
        self.assertAlmostEqual(geometry[0].area, exact, delta=0.005 * exact)
        self.assertTrue(geometry[0].contains(Point(5.0 * math.cos(math.pi / 8), 5.0 * math.sin(math.pi / 8))))

    def test_geometry_index_logging_does_not_depend_on_merge_scope(self):
        messages = []
        extractor = GeometryExtractor(self.board, log_callback=messages.append)
        self.board.tracks.append(MockTrack(1, 0, (0, 0), (1000000, 0), 500000))
        geometry = extractor.get_net_geometry("TestNet", merge=False)
        self.assertIn(0, geometry)
        self.assertTrue(any("Indexed" in message for message in messages))
        self.assertFalse(any("Collecting" in message for message in messages))

    def test_saved_multilayer_zone_fills_missing_ipc_layer(self):
        board_text = """
(kicad_pcb
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (zone
    (net "GND")
    (layers "F.Cu" "B.Cu")
    (filled_polygon
      (layer "B.Cu")
      (pts (xy 0 0) (xy 10 0) (xy 10 5) (xy 0 5))
    )
  )
)
"""
        with tempfile.TemporaryDirectory() as directory:
            board_path = Path(directory) / "multilayer.kicad_pcb"
            board_path.write_text(board_text, encoding="utf-8")
            extractor = GeometryExtractor(self.board, board_path=board_path)
            geometry = extractor.get_zone_geometry("GND")
        self.assertIn(31, geometry)
        self.assertAlmostEqual(geometry[31].area, 50.0)

    def test_saved_zone_layer_names_remap_to_live_kicad10_ids(self):
        board_text = """
(kicad_pcb
  (layers
    (10 "In4.Cu" power)
    (2 "B.Cu" signal)
  )
  (zone
    (net "GND")
    (layers "In4.Cu" "B.Cu")
    (filled_polygon
      (layer "In4.Cu")
      (pts (xy 0 0) (xy 10 0) (xy 10 5) (xy 0 5))
    )
  )
)
"""
        with tempfile.TemporaryDirectory() as directory:
            board_path = Path(directory) / "kicad10-layers.kicad_pcb"
            board_path.write_text(board_text, encoding="utf-8")
            extractor = GeometryExtractor(self.board, board_path=board_path)
            extractor._stackup_cache = {
                'copper': {
                    7: {'name': 'In4.Cu', 'thickness_mm': 0.0152},
                    8: {'name': 'B.Cu', 'thickness_mm': 0.035},
                },
                'layer_order': [7, 8], 'substrate': [],
                'source': 'KICAD_IPC', 'trustworthy': True, 'warnings': [],
            }
            geometry = extractor.get_zone_geometry("GND")
        self.assertIn(7, geometry)
        self.assertNotIn(10, geometry)
        self.assertAlmostEqual(geometry[7].area, 50.0)

    def test_saved_zone_layers_pair_with_renamed_live_layers(self):
        # P02 Rev E names In2.Cu "PWR.L3", and the live stackup knows it only
        # by that name. Kept under its file ordinal 6 -- the IPC id of In3.Cu --
        # the In2 VBUS_SYS pour was read as In3 copper, and DC-004 reported
        # every pour of the board edited since the last save.
        board_text = """
(kicad_pcb
  (layers
    (0 "F.Cu" signal "F.Signal")
    (4 "In1.Cu" power "GND.L2")
    (6 "In2.Cu" power "PWR.L3")
    (8 "In3.Cu" power "PWR.L4")
    (2 "B.Cu" signal "B.Signal")
  )
  (zone
    (net "VBUS_SYS")
    (layer "In2.Cu")
    (filled_polygon
      (layer "In2.Cu")
      (pts (xy 0 0) (xy 10 0) (xy 10 5) (xy 0 5))
    )
  )
)
"""
        with tempfile.TemporaryDirectory() as directory:
            board_path = Path(directory) / "renamed-layers.kicad_pcb"
            board_path.write_text(board_text, encoding="utf-8")
            extractor = GeometryExtractor(self.board, board_path=board_path)
            names = ("F.Signal", "GND.L2", "PWR.L3", "PWR.L4", "B.Signal")
            extractor._stackup_cache = {
                'copper': {live_id: {'name': name, 'thickness_mm': 0.035}
                           for live_id, name in zip((3, 4, 5, 6, 34), names)},
                'layer_order': [3, 4, 5, 6, 34], 'substrate': [],
                'source': 'KICAD_IPC', 'trustworthy': True, 'warnings': [],
            }
            geometry = extractor.get_zone_geometry("VBUS_SYS")
        self.assertEqual(set(geometry), {5})


class FakeIpcDielectric:
    """Reproduces the KiCad IPC bug: BoardStackupDielectricLayer.layers is
    empty, so GeometryExtractor._get_stackup_protobuf's get_epsilon_r/
    get_material helpers fall back to 4.4/"FR4" (SI-1)."""
    def __init__(self):
        self.layers = []


class FakeIpcStackupLayer:
    def __init__(self, type_val, layer=None, thickness_mm=0.0, user_name=None, dielectric=None):
        self.type = type_val
        self.layer = layer
        self.thickness = thickness_mm * 1e6  # nm, per to_mm()
        self.user_name = user_name
        self.dielectric = dielectric


class FakeIpcStackup:
    def __init__(self, layers):
        self.layers = layers


class FakeIpcBoard:
    def __init__(self, stackup):
        self._stackup = stackup

    def get_stackup(self):
        return self._stackup


def _p02_ipc_layers():
    """P02 Rev E stackup with the IPC dielectric bug: all 5 dielectrics
    report empty .layers (see FakeIpcDielectric)."""
    coppers = [(0, 'F.Cu', 0.035), (1, 'In1.Cu', 0.0152), (2, 'In2.Cu', 0.0152),
               (3, 'In3.Cu', 0.0152), (4, 'In4.Cu', 0.0152), (31, 'B.Cu', 0.035)]
    thicknesses_mm = [0.0994, 0.35, 0.1088, 0.35, 0.0994]
    layers = []
    for index, (lid, name, thickness) in enumerate(coppers):
        layers.append(FakeIpcStackupLayer(1, layer=lid, thickness_mm=thickness, user_name=name))
        if index < len(thicknesses_mm):
            layers.append(FakeIpcStackupLayer(
                2, thickness_mm=thicknesses_mm[index], dielectric=FakeIpcDielectric(),
            ))
    return layers


# P02 Rev E .kicad_pcb stackup block, run 20260922-2156 (audit SI-1): the
# saved file carries the real er/material/loss tangent that IPC dropped.
P02_STACKUP_BLOCK = """
	(stackup
		(layer "F.Mask"
			(type "Top Solder Mask")
			(thickness 0.01)
		)
		(layer "F.Cu"
			(type "copper")
			(thickness 0.035)
		)
		(layer "dielectric 1"
			(type "prepreg")
			(thickness 0.0994)
			(material "3313")
			(epsilon_r 4.1)
			(loss_tangent 0.02)
		)
		(layer "In1.Cu"
			(type "copper")
			(thickness 0.0152)
		)
		(layer "dielectric 2"
			(type "core")
			(thickness 0.35)
			(material "FR4")
			(epsilon_r 4.6)
			(loss_tangent 0.02)
		)
		(layer "In2.Cu"
			(type "copper")
			(thickness 0.0152)
		)
		(layer "dielectric 3"
			(type "prepreg")
			(thickness 0.1088)
			(material "2116")
			(epsilon_r 4.16)
			(loss_tangent 0.02)
		)
		(layer "In3.Cu"
			(type "copper")
			(thickness 0.0152)
		)
		(layer "dielectric 4"
			(type "core")
			(thickness 0.35)
			(material "FR4")
			(epsilon_r 4.6)
			(loss_tangent 0.02)
		)
		(layer "In4.Cu"
			(type "copper")
			(thickness 0.0152)
		)
		(layer "dielectric 5"
			(type "prepreg")
			(thickness 0.0994)
			(material "3313")
			(epsilon_r 4.1)
			(loss_tangent 0.02)
		)
		(layer "B.Cu"
			(type "copper")
			(thickness 0.035)
		)
		(layer "B.Mask"
			(type "Bottom Solder Mask")
			(thickness 0.01)
		)
		(copper_finish "ENIG")
		(dielectric_constraints yes)
	)
"""


class TestStackupDielectricFileFallback(unittest.TestCase):
    """SI-1: IPC's dielectric.layers comes back empty on P02 Rev E (run
    20260922-2156) and the 2-D solver silently used er 4.4 "FR4" for all 5
    dielectrics, worth 76.86 ohm on the USB pairs. The board file has
    4.1 / 4.6 / 4.16 / 4.6 / 4.1, materials 3313 / FR4 / 2116 / FR4 / 3313,
    which the reference solver measures at 85.4 ohm."""

    def test_falls_back_to_board_file_when_ipc_dielectric_is_empty(self):
        board = FakeIpcBoard(FakeIpcStackup(_p02_ipc_layers()))
        with tempfile.TemporaryDirectory() as directory:
            board_path = Path(directory) / "p02_revision_e.kicad_pcb"
            board_path.write_text(f"(kicad_pcb\n{P02_STACKUP_BLOCK}\n)\n", encoding="utf-8")
            extractor = GeometryExtractor(board, board_path=board_path)
            stackup = extractor._get_stackup_protobuf()

        self.assertTrue(stackup['trustworthy'])
        self.assertEqual(stackup['warnings'], [])
        eps = [sub['epsilon_r'] for sub in stackup['substrate']]
        self.assertEqual(eps, [4.1, 4.6, 4.16, 4.6, 4.1])
        materials = [sub['material'] for sub in stackup['substrate']]
        self.assertEqual(materials, ['3313', 'FR4', '2116', 'FR4', '3313'])
        self.assertTrue(all(sub['loss_tangent'] == 0.02 for sub in stackup['substrate']))

    def test_marks_untrustworthy_with_named_layers_when_no_file_available(self):
        board = FakeIpcBoard(FakeIpcStackup(_p02_ipc_layers()))
        extractor = GeometryExtractor(board, board_path=None)
        stackup = extractor._get_stackup_protobuf()

        self.assertFalse(stackup['trustworthy'])
        self.assertEqual(len(stackup['warnings']), 1)
        self.assertIn('F.Cu-In1.Cu', stackup['warnings'][0])
        self.assertTrue(all(sub['epsilon_r'] == 4.4 for sub in stackup['substrate']))


if __name__ == '__main__':
    unittest.main()
