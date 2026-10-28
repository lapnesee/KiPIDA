"""Board-file fallbacks must read S-expression lists, never the text of a string.

A title block, a text item or a material name may quote "(stackup ...)",
"(layer ...)" or "(gr_rect ...)". Those are strings, and the readers that
recover what IPC leaves out must skip them.
"""

import tempfile
import unittest
from pathlib import Path

from extractor import GeometryExtractor

# The title block comes before (setup ...) in every saved .kicad_pcb, so a
# comment that quotes a list keyword is the first occurrence in the file.
BOARD = r'''(kicad_pcb
	(version 20250302)
	(generator "pcbnew")
	(title_block
		(title "Test board")
		(comment 1 "2-core build, 1.6 mm (stackup unchanged)")
		(comment 2 "outline (gr_rect (start -50 -50) (end 500 500) (layer \"Edge.Cuts\"))")
		(comment 3 "say \"(stackup (layer \\\"x\\\"))\" here")
	)
	(layers
		(0 "F.Cu" signal)
		(4 "In1.Cu" signal)
		(2 "B.Cu" signal)
		(25 "Edge.Cuts" user)
	)
	(setup
		(stackup
			(layer "F.SilkS"
				(type "Top Silk Screen")
			)
			(layer "F.Mask"
				(type "Top Solder Mask")
				(thickness 0.015)
				(epsilon_r 3.6)
			)
			(layer "F.Cu"
				(type "copper")
				(thickness 0.035)
			)
			(layer "dielectric 1"
				(type "prepreg")
				(thickness 0.2)
				(material "PP (high Tg)")
				(epsilon_r 3.9)
				(loss_tangent 0.012)
			)
			(layer "In1.Cu"
				(type "copper")
				(thickness 0.035)
			)
			(layer "dielectric 2"
				(type "core")
				(thickness 1.2)
				(material "FR4")
				(epsilon_r 4.5)
				(loss_tangent 0.02)
			)
			(layer "B.Cu"
				(type "copper")
				(thickness 0.035)
			)
			(layer "B.Mask"
				(type "Bottom Solder Mask")
				(thickness 0.02)
			)
			(copper_finish "ENIG")
			(dielectric_constraints no)
		)
	)
	(gr_text "(gr_line (start 900 900) (end 901 901) (layer \"Edge.Cuts\"))"
		(at 10 10 0)
		(layer "F.SilkS")
	)
	(gr_rect
		(start 0 0)
		(end 75 30)
		(stroke (width 0.05) (type default))
		(fill no)
		(layer "Edge.Cuts")
	)
)
'''


class BoardFileSexprTests(unittest.TestCase):
    def _write(self, directory, text=BOARD):
        path = Path(directory) / "board.kicad_pcb"
        path.write_text(text, encoding="utf-8")
        return path

    def test_reads_dielectrics_past_a_comment_quoting_stackup(self):
        with tempfile.TemporaryDirectory() as directory:
            dielectrics = GeometryExtractor._stackup_from_file(self._write(directory))

        self.assertEqual(
            dielectrics,
            [
                {'thickness_mm': 0.2, 'material': 'PP (high Tg)',
                 'epsilon_r': 3.9, 'loss_tangent': 0.012},
                {'thickness_mm': 1.2, 'material': 'FR4',
                 'epsilon_r': 4.5, 'loss_tangent': 0.02},
            ],
        )

    def test_reads_solder_masks_past_a_comment_quoting_stackup(self):
        with tempfile.TemporaryDirectory() as directory:
            masks = GeometryExtractor._solder_masks_from_file(self._write(directory))

        self.assertEqual(masks, {
            'top': {'thickness_mm': 0.015, 'epsilon_r': 3.6},
            'bottom': {'thickness_mm': 0.02, 'epsilon_r': 0.0},
        })

    def test_a_file_whose_only_stackup_is_quoted_has_none(self):
        text = '(kicad_pcb (title_block (comment 1 "(stackup (layer \\"d\\" (epsilon_r 9)))")))'
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(directory, text)
            self.assertIsNone(GeometryExtractor._stackup_from_file(path))
            self.assertEqual(GeometryExtractor._solder_masks_from_file(path), {})

    def test_edge_cuts_quoted_in_strings_do_not_move_the_outline(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(directory)
            bounds = GeometryExtractor._edge_cuts_bounds_from_file(path)
            outline = GeometryExtractor._edge_cuts_outline_from_file(path)

        self.assertEqual(bounds, (0.0, 0.0, 75.0, 30.0))
        if outline is not None:  # Shapely is optional
            self.assertEqual(tuple(outline.bounds), (0.0, 0.0, 75.0, 30.0))

    def test_a_token_must_end_its_keyword(self):
        text = '(footprint (pad "1" (zone_connect 0)) (zone (net "GND") (filled_polygon (layer "F.Cu"))))'

        self.assertEqual(
            list(GeometryExtractor._balanced_blocks(text, "(zone")),
            ['(zone (net "GND") (filled_polygon (layer "F.Cu")))'],
        )

    def test_parentheses_inside_strings_do_not_close_a_list(self):
        text = '(zone (net "A)") (name "(b")) (zone (net "C"))'

        self.assertEqual(
            list(GeometryExtractor._balanced_blocks(text, "(zone")),
            ['(zone (net "A)") (name "(b"))', '(zone (net "C"))'],
        )


if __name__ == "__main__":
    unittest.main()
