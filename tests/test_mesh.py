
import unittest
import sys
import os

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

    def test_large_electrical_mesh_adapts_grid_instead_of_exhausting_memory(self):
        poly = Polygon([(0, 0), (20, 0), (20, 10), (0, 10)])
        stackup = {'copper': {0: {'thickness_mm': 0.035}}, 'resistivity': 1.7e-5}
        self.mesher.MAX_ELECTRICAL_NODES = 100
        mesh = self.mesher.generate_mesh("Plane", {0: poly}, stackup, grid_size_mm=1.0)
        self.assertTrue(mesh.adaptive_grid)
        self.assertEqual(mesh.requested_grid_step, 1.0)
        self.assertGreater(mesh.grid_step, mesh.requested_grid_step)
        self.assertLessEqual(len(mesh.nodes), self.mesher.MAX_ELECTRICAL_NODES)

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

if __name__ == '__main__':
    unittest.main()
