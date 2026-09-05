"""Tests for ingest.design_model — DesignModel builder and serialisation."""

import sys
import os
import json
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ingest.board_reader import ParsedBoard, Stackup, BoardBounds, CopperLayer
from ingest.netlist_builder import BoardNetlist
from ingest.project_resolver import BoardProject, MultiboardProject
from ingest.multiboard import CrossBoardNet
from ingest.design_model import DesignModel, build_design_model, _from_single_board, _netlist_to_dict


def _empty_stackup():
    return Stackup(layers=[], total_thickness_mm=1.6, copper_layer_count=0)


def _empty_bounds():
    return BoardBounds(0, 0, 50, 30)


def _empty_netlist(uuid="uuid-test", pcb_path=None):
    return BoardNetlist(
        board_uuid=uuid,
        pcb_path=pcb_path or Path("/fake/board.kicad_pcb"),
        components=[],
        nets=[],
        stackup=_empty_stackup(),
        bounds=_empty_bounds(),
    )


class TestDesignModelToDict(unittest.TestCase):
    def test_round_trip_json(self):
        nl_dict = _netlist_to_dict(_empty_netlist())
        dm = DesignModel(
            project_name="test_project",
            is_multiboard=False,
            board_netlists=(nl_dict,),
            cross_board_nets=(),
            source_hash="abc123",
        )
        d = dm.to_dict()
        self.assertEqual(d["project_name"], "test_project")
        self.assertFalse(d["is_multiboard"])
        self.assertEqual(d["source_hash"], "abc123")
        self.assertEqual(len(d["board_netlists"]), 1)

    def test_to_json_valid(self):
        nl_dict = _netlist_to_dict(_empty_netlist())
        dm = DesignModel(
            project_name="proj",
            is_multiboard=False,
            board_netlists=(nl_dict,),
            cross_board_nets=(),
            source_hash="deadbeef",
        )
        j = dm.to_json()
        parsed = json.loads(j)
        self.assertEqual(parsed["project_name"], "proj")

    def test_multiboard_cross_nets_in_dict(self):
        nl_dict = _netlist_to_dict(_empty_netlist())
        cross = {"net_name": "GND", "boards": ["uuid-a", "uuid-b"]}
        dm = DesignModel(
            project_name="multi",
            is_multiboard=True,
            board_netlists=(nl_dict,),
            cross_board_nets=(cross,),
            source_hash="xyz",
        )
        d = dm.to_dict()
        self.assertTrue(d["is_multiboard"])
        self.assertEqual(len(d["cross_board_nets"]), 1)
        self.assertEqual(d["cross_board_nets"][0]["net_name"], "GND")


class TestBuildDesignModelFromSingleBoard(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.pro_path = Path(self.tmpdir) / "myboard.kicad_pro"
        self.pcb_path = Path(self.tmpdir) / "myboard.kicad_pcb"
        self.sch_path = Path(self.tmpdir) / "myboard.kicad_sch"

        pro_data = {
            "meta": {"filename": "myboard.kicad_pro", "version": 3},
            "sheets": [["root-uuid", "Root"]],
        }
        self.pro_path.write_text(json.dumps(pro_data), encoding="utf-8")
        self.pcb_path.write_text("(kicad_pcb (version 20260206) (general (thickness 1.6)))",
                                  encoding="utf-8")
        self.sch_path.write_text("(kicad_sch (version 20260306) (uuid \"root\"))",
                                  encoding="utf-8")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_bp(self):
        return BoardProject(
            pro_path=self.pro_path,
            pcb_path=self.pcb_path,
            sch_root_path=self.sch_path,
            sch_sheets=[("root-uuid", "Root")],
            is_part_of_multiboard=False,
            sub_project_uuid="uuid-single",
        )

    def test_build_returns_design_model(self):
        dm = build_design_model(self._make_bp())
        self.assertIsInstance(dm, DesignModel)

    def test_not_multiboard(self):
        dm = build_design_model(self._make_bp())
        self.assertFalse(dm.is_multiboard)

    def test_project_name(self):
        dm = build_design_model(self._make_bp())
        self.assertEqual(dm.project_name, "myboard")

    def test_source_hash_is_hex(self):
        dm = build_design_model(self._make_bp())
        self.assertTrue(all(c in "0123456789abcdef" for c in dm.source_hash))
        self.assertEqual(len(dm.source_hash), 64)

    def test_one_board_netlist(self):
        dm = build_design_model(self._make_bp())
        self.assertEqual(len(dm.board_netlists), 1)


class TestNetlistToDict(unittest.TestCase):
    def test_structure(self):
        nl = _empty_netlist("uuid-abc")
        d = _netlist_to_dict(nl)
        self.assertEqual(d["board_uuid"], "uuid-abc")
        self.assertIn("stackup", d)
        self.assertIn("bounds", d)
        self.assertIn("components", d)
        self.assertIn("nets", d)

    def test_bounds_fields(self):
        nl = _empty_netlist()
        d = _netlist_to_dict(nl)
        self.assertAlmostEqual(d["bounds"]["width_mm"], 50.0)
        self.assertAlmostEqual(d["bounds"]["height_mm"], 30.0)
        self.assertAlmostEqual(d["bounds"]["area_mm2"], 1500.0)

    def test_stackup_fields(self):
        nl = _empty_netlist()
        d = _netlist_to_dict(nl)
        self.assertAlmostEqual(d["stackup"]["total_thickness_mm"], 1.6)
        self.assertEqual(d["stackup"]["copper_layer_count"], 0)


if __name__ == "__main__":
    unittest.main()
