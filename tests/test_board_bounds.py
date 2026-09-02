import tempfile
import unittest
from pathlib import Path

from extractor import GeometryExtractor, Point


class BoardBoundsTests(unittest.TestCase):
    def test_reads_edge_cuts_rectangle_from_board_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "board.kicad_pcb"
            path.write_text('(gr_rect (start 59.8 49.89) (end 134.8 79.89) (layer "Edge.Cuts"))', encoding="utf-8")
            self.assertEqual(
                GeometryExtractor._edge_cuts_bounds_from_file(path),
                (59.8, 49.89, 134.8, 79.89),
            )

    def test_expands_file_bounds_by_the_requested_margin(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "board.kicad_pcb"
            path.write_text(
                '(gr_rect (start 0 0) (end 75 30) (layer "Edge.Cuts"))',
                encoding="utf-8",
            )
            extractor = GeometryExtractor(board=None)
            self.assertEqual(
                extractor.get_board_bounds(margin_mm=2.0, board_file_path=path),
                (-2.0, -2.0, 77.0, 32.0),
            )

    def test_reads_line_and_polygon_edge_cuts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "board.kicad_pcb"
            path.write_text(
                '''
                (gr_line (start 0 0) (end 75 0) (layer "Edge.Cuts"))
                (gr_poly (pts (xy 0 30) (xy 75 30)) (layer "Edge.Cuts"))
                ''',
                encoding="utf-8",
            )
            self.assertEqual(
                GeometryExtractor._edge_cuts_bounds_from_file(path),
                (0.0, 0.0, 75.0, 30.0),
            )

    @unittest.skipIf(Point is None, "Shapely is required for thermal outlines")
    def test_reads_rounded_edge_cuts_rectangle_as_physical_outline(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "board.kicad_pcb"
            path.write_text(
                '(gr_rect (start 0 0) (end 75 30) (radius 2) (layer "Edge.Cuts"))',
                encoding="utf-8",
            )

            outline = GeometryExtractor._edge_cuts_outline_from_file(path)

            self.assertEqual(tuple(outline.bounds), (0.0, 0.0, 75.0, 30.0))
            self.assertTrue(outline.covers(Point(2.0, 2.0)))
            self.assertFalse(outline.covers(Point(0.25, 0.25)))
