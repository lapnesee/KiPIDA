
import unittest
import sys
import os
import math

try:
    import numpy as np
    from shapely.geometry import Polygon
except ImportError:
    pass

plugin_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if plugin_dir not in sys.path:
    sys.path.insert(0, plugin_dir)

# 1. Pcbnew Mock (Base for others)
class MockPcbnew:
    PAD_ATTRIB_PTH = 0
    class ActionPlugin: pass
    class PCB_VIA: pass
    class PCB_TRACK: pass
    @staticmethod
    def ToMM(v): return v / 1e6

if 'pcbnew' not in sys.modules:
    sys.modules['pcbnew'] = MockPcbnew

import pcbnew # Now we can import it

# 2. Other Mocks
class MockBoard:
    def __init__(self):
        self.tracks = []
        self.footprints = []
        self.layers_copper = {0: "F_Cu", 31: "B_Cu"}
    def FindNet(self, name): return MockNet(1)
    def GetNetCode(self): return 1
    def GetTracks(self): return self.tracks
    def GetFootprints(self): return self.footprints
    def GetEnabledLayers(self): return MockSeq([0, 31])
    def IsLayerCopper(self, id): return id in self.layers_copper

class MockNet:
    def __init__(self, c): self.c = c
    def GetNetCode(self): return self.c

class MockSeq:
    def __init__(self, s): self.s = s
    def Seq(self): return self.s
    
class MockTrack(MockPcbnew.PCB_VIA):
    def __init__(self, net, layer, pos, w, cls):
        self.net = net
        self.pos = pos
        self.w = w
        self.cls = cls
        self.top = 0
        self.bot = 31
    def GetNetCode(self): return self.net
    def GetClass(self): return self.cls
    def GetPosition(self): return self.pos
    def GetWidth(self): return self.w
    def GetDrillValue(self): return self.w
    def TopLayer(self): return self.top
    def BottomLayer(self): return self.bot
    def GetLayerSet(self):
        class MockLayerSet:
            def Seq(self): return [0, 31]
        return MockLayerSet()

class MockPad:
    def __init__(self, net, pos, size, attrib):
        self.net = net
        self.pos = pos
        self.size = size
        self.attrib = attrib
    def GetNetCode(self): return self.net
    def GetPosition(self): return self.pos
    def GetSize(self): return self.size
    def GetAttribute(self): return self.attrib

class MockFootprint:
    def __init__(self, pads): self.pads = pads
    def Pads(self): return self.pads

class MockPoint:
    def __init__(self, x, y): self.x, self.y = x, y

from mesh import Mesher, Mesh
from runtime_config import RuntimeComputeSettings

class TestMesher(unittest.TestCase):
    def setUp(self):
        self.board = MockBoard()
        self.mesher = Mesher(self.board)
        
    def test_simple_rect_mesh(self):
        # 20x10 mm rect on layer 0
        poly = Polygon([(0,0), (20,0), (20,10), (0,10)])
        geo = {0: poly}
        stackup = {
            'copper': {0: {'thickness_mm': 0.035}},
            'resistivity': 1.7e-5
        }
        
        mesh = self.mesher.generate_mesh("Test", geo, stackup, grid_size_mm=5.0)
        
        self.assertEqual(len(mesh.nodes), 15)
        # Check that we have generated matrix entries
        self.assertTrue(len(mesh.G_coo_data) > 0)
        
    def test_vertical_link_via(self):
        # 2 layers, node at 0,0 on both
        poly = Polygon([(-1,-1), (1,-1), (1,1), (-1,1)]) # Small square at origin
        geo = {0: poly, 31: poly}
        stackup = {
            'copper': {0: {}, 31: {}},
            'resistivity': 1.7e-5,
            'substrate': []
        }
        
        # Add Via at 0,0
        via = MockTrack(1, 0, MockPoint(0,0), 300000, "PCB_VIA")
        self.board.tracks.append(via)
        
        mesh = self.mesher.generate_mesh("Test", geo, stackup, grid_size_mm=0.5)
        
        # Check that we added vertical connections to the sparse matrix
        # Vertical conductance is usually large (short) or specific value
        # We can just check that G_coo_data grew
        self.assertTrue(len(mesh.G_coo_data) > 0)

    def test_add_vertical_stack_connects_a_pth_pads_layers(self):
        # docs/audit-2026-09-23.md "old items": _add_vertical_stack -- the
        # PTH pad barrel path taken by a through-hole footprint pad, as
        # opposed to _add_vertical_link's board via -- had no direct test.
        mesh = Mesh()
        mesh.grid_origin = (0.0, 0.0)
        mesh.grid_step = 0.5
        mesh.node_map[(0, 0, 0)] = 10
        mesh.node_map[(0, 0, 31)] = 20
        mesh.node_coords[10] = (0.0, 0.0, 0)
        mesh.node_coords[20] = (0.0, 0.0, 31)
        stackup = {
            'copper': {0: {}, 31: {}},
            'resistivity': 1.7e-5,
            'substrate': [{'between': [0, 31], 'thickness_mm': 1.51}],
        }

        self.mesher._add_vertical_stack(
            mesh, MockPoint(0, 0), layers=[0, 31], diameter=0.3, stackup=stackup,
        )

        self.assertEqual(len(mesh.branches), 1)
        branch = mesh.branches[0]
        self.assertEqual({branch.node_a, branch.node_b}, {10, 20})
        self.assertEqual(branch.kind, "pth")
        self.assertGreater(branch.resistance_ohm, 0.0)
        self.assertGreater(branch.inductance_h, 0.0)
        self.assertGreater(branch.cross_section_mm2, 0.0)

    def test_large_electrical_mesh_adapts_grid_instead_of_exhausting_memory(self):
        poly = Polygon([(0, 0), (20, 0), (20, 10), (0, 10)])
        stackup = {'copper': {0: {'thickness_mm': 0.035}}, 'resistivity': 1.7e-5}
        self.mesher.MAX_ELECTRICAL_NODES = 100
        mesh = self.mesher.generate_mesh("Plane", {0: poly}, stackup, grid_size_mm=1.0)
        self.assertTrue(mesh.adaptive_grid)
        self.assertEqual(mesh.requested_grid_step, 1.0)
        self.assertGreater(mesh.grid_step, mesh.requested_grid_step)
        self.assertLessEqual(len(mesh.nodes), self.mesher.MAX_ELECTRICAL_NODES)

    def test_preflight_selects_safe_grid_before_full_raster(self):
        messages = []
        self.mesher.log_callback = messages.append
        self.mesher.MAX_ELECTRICAL_NODES = 100
        geometry = {0: Polygon([(0, 0), (20, 0), (20, 10), (0, 10)])}

        grid, estimate = self.mesher._preflight_grid_size(geometry, 1.0)

        self.assertGreater(estimate, self.mesher.MAX_ELECTRICAL_NODES)
        self.assertGreater(grid, 1.0)
        mesh = self.mesher.generate_mesh("Plane", geometry, {
            'copper': {0: {'thickness_mm': 0.035}}, 'resistivity': 1.7e-5,
        }, grid_size_mm=1.0)
        self.assertTrue(mesh.adaptive_grid)
        self.assertTrue(any("preflight estimates" in message for message in messages))

    def test_large_layer_is_split_into_multiple_parallel_raster_chunks(self):
        settings = RuntimeComputeSettings(cpu_multithread=True, cpu_threads=4)
        mesher = Mesher(self.board, compute_settings=settings)
        poly = Polygon([(0, 0), (500, 0), (500, 700), (0, 700)])
        x_coords = np.linspace(0.0, 500.0, 501)
        y_coords = np.linspace(0.0, 700.0, 701)
        shape = (len(y_coords), len(x_coords))

        sequential = mesher._rasterize_polygon(poly, x_coords, y_coords, shape)
        parallel, chunk_count = mesher._rasterize_layers(
            {0: poly}, [0], x_coords, y_coords, shape, workers=4,
        )

        self.assertGreater(chunk_count, 1)
        np.testing.assert_array_equal(parallel[0], sequential)

    def test_vector_rasterizer_preserves_polygon_holes(self):
        poly = Polygon(
            [(0, 0), (10, 0), (10, 10), (0, 10)],
            holes=[[(4, 4), (6, 4), (6, 6), (4, 6)]],
        )
        x_coords = np.linspace(0.0, 10.0, 11)
        y_coords = np.linspace(0.0, 10.0, 11)
        mask = self.mesher._rasterize_polygon(
            poly, x_coords, y_coords, (len(y_coords), len(x_coords)),
        )

        self.assertTrue(mask[2, 2])
        self.assertFalse(mask[5, 5])

    def test_plane_pair_loop_inductance_is_not_doubled(self):
        # AC-3 (docs/audit-2026-09-23.md, scratchpad/audit/ac/ac_checks.py (A)):
        # mu0*d used to be given to EVERY branch of BOTH the rail and the
        # ground mesh, doubling the plane-pair loop inductance: 0.999 nH
        # modelled vs 0.500 nH analytic (mu0*d*l/w, d=0.0994 mm, l/w=4).
        # Each mesh now only owns half of mu0*h, h measured to the nearest
        # layer carrying the OTHER net, so the two halves sum back to the
        # analytic mu0*d for a simple two-layer plane pair.
        stackup = {'substrate': [{'between': [0, 1], 'thickness_mm': 0.0994}], 'copper': {}}
        rail_l = self.mesher._estimate_lateral_l(0, stackup, return_layers={1})
        ground_l = self.mesher._estimate_lateral_l(1, stackup, return_layers={0})
        mu0 = 4e-7 * math.pi
        self.assertAlmostEqual(rail_l, ground_l)
        self.assertAlmostEqual(rail_l + ground_l, mu0 * 0.0994e-3, delta=1e-15)

    def test_lateral_l_falls_back_without_return_layers(self):
        # DC callers (application/dc_controller.py) never pass return_layers
        # since DC ignores inductance entirely; the legacy nearest-dielectric
        # estimate must still be available for them.
        stackup = {'substrate': [{'between': [0, 1], 'thickness_mm': 0.0994}], 'copper': {}}
        self.assertGreater(self.mesher._estimate_lateral_l(0, stackup), 0.0)

if __name__ == '__main__':
    unittest.main()
