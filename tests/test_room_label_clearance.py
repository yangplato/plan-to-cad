import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import ezdxf
from shapely.geometry import MultiLineString

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from room_label_clearance import audit


class RoomLabelClearanceTests(unittest.TestCase):
    def setUp(self):
        self.doc = ezdxf.new()
        self.doc.units = 4
        self.doc.layers.new("C-FLOR-IDEN")
        self.doc.layers.new("A-FURN-FIXD")
        self.doc.layers.new("A-FURN-MOVE")
        self.doc.layers.new("DO-NOT-DRAW")
        self.model = self.doc.modelspace()
        self.label = self.model.add_text("房", dxfattribs={"layer": "C-FLOR-IDEN"})
        # Two disjoint strokes leave space within the overall label bounds.
        self.glyph = MultiLineString([[(0, 0), (0, 5)], [(4, 0), (4, 5)]])

    def block(self, name, points, at=(0, 0), layer="A-FURN-FIXD"):
        definition = self.doc.blocks.new(name)
        definition.add_line(*points)
        return self.model.add_blockref(name, at, dxfattribs={"layer": layer})

    def check(self):
        with patch("room_label_clearance.label_outline", return_value=(self.glyph, "test.ttf")):
            return audit(self.doc, {})

    def test_distant_furniture_and_penetration_of_glyph_box_only_are_clear(self):
        self.block("far", [(8, 3), (9, 3)])
        self.block("in_whitespace", [(1, 3), (3, 3)])
        q = self.check()
        self.assertEqual(q["status"], "pass")
        self.assertEqual(q["candidates"], [])
        self.assertEqual(q["counts"]["checked_furniture"], 2)

    def test_actual_nested_polyline_crossings_are_world_points(self):
        inner = self.doc.blocks.new("stroke")
        inner.add_lwpolyline([(-3, 0), (3, 0)])
        outer = self.doc.blocks.new("double")
        outer.add_blockref("stroke", (0, 0), dxfattribs={"xscale": 2})
        ref = self.model.add_blockref("double", (2, 3), dxfattribs={"layer": "A-FURN-MOVE"})
        q = self.check()
        self.assertEqual(q["status"], "review")
        hit, = q["candidates"]
        self.assertEqual(hit["id"], f"room-label/{self.label.dxf.handle}/{ref.dxf.handle}")
        self.assertEqual(hit["intersections_mm"], [[0.0, 3.0], [4.0, 3.0]])
        self.assertIn("/stroke[", hit["member_paths"][0])
        self.assertEqual(q["coverage_issues"], [])

    def test_coincident_stroke_reports_length_and_endpoints(self):
        self.block("same", [(0, 1), (0, 3)])
        hit, = self.check()["candidates"]
        span, = hit["overlaps"]
        self.assertEqual(span["length_mm"], 2)
        self.assertEqual(span["ends_mm"], [[0, 1], [0, 3]])

    def test_frozen_layer_and_invisible_nested_members_are_skipped(self):
        self.doc.layers.get("DO-NOT-DRAW").off()
        self.doc.layers.get("A-FURN-MOVE").freeze()
        self.block("hidden_top", [(-1, 3), (1, 3)], layer="A-FURN-MOVE")
        detail = self.doc.blocks.new("detail")
        detail.add_line((-1, 3), (1, 3), dxfattribs={"layer": "DO-NOT-DRAW"})
        detail.add_line((-1, 2), (1, 2), dxfattribs={"invisible": 1})
        detail.add_line((1, 3), (3, 3))
        self.model.add_blockref("detail", (0, 0), dxfattribs={"layer": "A-FURN-FIXD"})
        q = self.check()
        self.assertEqual(q["counts"]["furniture"], 1)
        self.assertEqual(q["candidates"], [])
        self.assertEqual(q["coverage_issues"], [])

    def test_unsupported_filled_block_produces_coverage_issue_only(self):
        detail = self.doc.blocks.new("fill")
        detail.add_solid([(-1, -1), (-1, 9), (8, -1), (8, 9)])
        ref = self.model.add_blockref("fill", (0, 0), dxfattribs={"layer": "A-FURN-FIXD"})
        q = self.check()
        self.assertEqual(q["candidates"], [])
        self.assertTrue(any(ref.dxf.handle in v["id"] and "SOLID" in v["reasons"][0]
                            for v in q["coverage_issues"]))

    def test_missing_font_never_measures_substitute(self):
        self.doc.styles.new("ABSENT", dxfattribs={"font": "room-audit-no-such-font.ttf"})
        self.label.dxf.style = "ABSENT"
        self.block("might_cross_substitute", [(-1, 3), (1, 3)])
        with patch("room_label_clearance.text2path.make_paths_from_entity") as make_paths:
            q = audit(self.doc, {})
        make_paths.assert_not_called()
        issue, = q["coverage_issues"]
        self.assertEqual(issue["id"], f"room-label/{self.label.dxf.handle}")
        self.assertEqual(q["candidates"], [])
        self.assertEqual(q["counts"]["checked_labels"], 0)

    def test_font_manager_substitution_is_reported(self):
        self.doc.styles.new("DESIGN", dxfattribs={"font": "RoomDesign.ttf"})
        self.label.dxf.style = "DESIGN"
        self.block("line", [(-1, 3), (1, 3)])
        with (
            patch("room_label_clearance.fonts.font_manager.has_font", return_value=True),
            patch("room_label_clearance.fonts.font_manager.find_font_name", return_value="Replacement.ttf"),
            patch("room_label_clearance.text2path.make_paths_from_entity") as make_paths,
        ):
            q = audit(self.doc, {})
        make_paths.assert_not_called()
        issue, = q["coverage_issues"]
        self.assertIn("字体替代", issue["reasons"][0])
        self.assertEqual(q["candidates"], [])

    def test_empty_scope_is_not_evidence_of_no_contact(self):
        q = audit(self.doc, {})
        self.assertEqual(q["status"], "review")
        self.assertEqual(q["coverage_issues"][0]["id"], "room-furniture/inventory")


if __name__ == "__main__":
    unittest.main()
