"""Small saved-model geometry regressions. No AutoCAD or paid services required."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import ezdxf

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import source_geometry_review as review


SOURCE_HASH = "a" * 64
DXF_HASH = "b" * 64


def wall(doc, bounds):
    x0, y0, x1, y1 = bounds
    return doc.modelspace().add_lwpolyline([(x0, y0), (x1, y0), (x1, y1), (x0, y1)],
                                         close=True, dxfattribs={"layer": "WALL"})


def model():
    doc = ezdxf.new("R2010")
    doc.units = 4
    doc.layers.new("WALL")
    doc.layers.new("FRAME")
    left = wall(doc, [0, 0, 100, 1000])
    right = wall(doc, [900, 0, 1000, 1000])
    for y in (1100, 1120):
        doc.modelspace().add_line((0, y), (1000, y), dxfattribs={"layer": "FRAME"})
    dim = doc.modelspace().add_linear_dim(base=(500, -100), p1=(50, 500), p2=(950, 500), angle=0)
    dim.render()
    return doc, left, right, dim.dimension


def section(ident, expected, search, **extra):
    return dict(id=ident, kind="wall_section", axis="x", at_mm=500,
                search_mm=search, expected_faces_mm=expected, image_uncertainty_mm=0,
                section_role="body", source_evidence="independent source wall faces", **extra)


def spec_for(checks):
    return {"review_version": review.VERSION, "source_sha256": SOURCE_HASH, "dxf_sha256": DXF_HASH,
            "wall_layers": ["WALL"], "required_ids": [r["id"] for r in checks], "checks": checks}


def full_spec(dim):
    checks = [section("left", [0, 100], [-100, 200]), section("right", [900, 1000], [800, 1100]),
              {"id": "net", "kind": "span", "from": {"section": "left", "position": "max"},
               "to": {"section": "right", "position": "min"}, "expected_mm": 800,
               "image_uncertainty_mm": 0, "source_evidence": "independent clear width"},
              {"id": "control", "kind": "control", "axis": "x",
               "from": {"section": "left", "position": "center"},
               "to": {"section": "right", "position": "center"}, "expected_mm": 900,
               "dimension_handle": dim.dxf.handle, "image_uncertainty_mm": 0,
               "source_evidence": "legible printed control dimension"},
              {"id": "frame", "kind": "line_section", "axis": "y", "at_mm": 500,
               "layers": ["FRAME"], "search_mm": [1050, 1150], "expected_faces_mm": [1100, 1120],
               "image_uncertainty_mm": 0, "source_evidence": "independent frame faces"},
              {"id": "opening", "kind": "clear_region", "polygon_mm": [[200, 200], [800, 200], [800, 800], [200, 800]],
               "source_evidence": "source open passage"}]
    return spec_for(checks)


class SourceGeometryReviewTests(unittest.TestCase):
    def audit(self, doc, spec, **kwargs):
        return review.audit(doc, spec, dxf_sha256=DXF_HASH, source_sha256=SOURCE_HASH, **kwargs)

    def test_positive_actual_faces_dimensions_net_and_open_region(self):
        doc, _, _, dim = model()
        result = self.audit(doc, full_spec(dim))
        self.assertEqual(result["status"], "通过", result)
        self.assertEqual(result["results"]["net"]["actual_span_mm"], 800)
        self.assertEqual(result["coverage"]["processed"], 6)

    def test_right_numbers_do_not_hide_shifted_actual_wall(self):
        doc, _, right, dim = model()
        right.translate(200, 0, 0)
        spec = full_spec(dim)
        spec["checks"][1]["search_mm"] = [800, 1300]
        result = self.audit(doc, spec)
        self.assertEqual(result["status"], "需返修")
        self.assertIn("control", result["failed"])
        control = result["results"]["control"]
        self.assertEqual(control["dimension_object_value_mm"], 900)
        self.assertEqual(control["actual_span_mm"], 1100)
        self.assertIn("dimension_definitions_do_not_land_on_selected_actual_controls", control["failures"])

    def test_union_thickness_and_collinear_overlap_are_measured(self):
        doc = ezdxf.new(); doc.units = 4; doc.layers.new("WALL")
        wall(doc, [-120, 0, 120, 1000]); wall(doc, [70, 0, 310, 1000])
        result = self.audit(doc, spec_for([section("common", [-120, 120], [-200, 400])]))
        row = result["results"]["common"]
        self.assertEqual(row["union_thickness_mm"], 430)
        self.assertEqual(row["overlap_mm"], 50)
        self.assertEqual(result["status"], "需返修")

    def test_matching_outline_and_fill_are_not_double_counted(self):
        doc, _, _, dim = model()
        for pts in [[(0, 0), (100, 0), (100, 1000), (0, 1000)],
                    [(900, 0), (1000, 0), (1000, 1000), (900, 1000)]]:
            hatch = doc.modelspace().add_hatch(dxfattribs={"layer": "WALL"})
            hatch.paths.add_polyline_path(pts, is_closed=True)
        result = self.audit(doc, full_spec(dim))
        self.assertEqual(result["status"], "通过", result)
        self.assertEqual(result["results"]["left"]["overlap_mm"], 0)
        # A stale fill cannot validate a moved editable wall outline.
        next(iter(doc.modelspace().query('LWPOLYLINE[layer=="WALL"]'))).translate(-20, 0, 0)
        result = self.audit(doc, full_spec(dim))
        self.assertEqual(result["status"], "待核")

    def test_nested_unfilled_contours_need_wall_material_interpretation(self):
        doc = ezdxf.new(); doc.units = 4; doc.layers.new("WALL")
        wall(doc, [0, 0, 1000, 1000]); wall(doc, [100, 100, 900, 900])
        result = self.audit(doc, spec_for([section("outline", [0, 1000], [-10, 1010])]))
        self.assertNotEqual(result["status"], "通过")
        self.assertTrue(any("nested" in p for p in result["pending"]))

    def test_single_closed_hatch_style_one_and_two_are_equivalent_without_mutation(self):
        for style in (1, 2):
            with self.subTest(style=style):
                doc = ezdxf.new(); doc.units = 4; doc.layers.new("WALL")
                wall(doc, [0, 0, 100, 1000])
                hatch = doc.modelspace().add_hatch(dxfattribs={"layer": "WALL", "hatch_style": style})
                hatch.paths.add_polyline_path([(0, 0), (100, 0), (100, 1000), (0, 1000)], is_closed=True)
                result = self.audit(doc, spec_for([section("wall", [0, 100], [-10, 110])]))
                self.assertEqual(result["status"], "通过", result)
                self.assertEqual(result["results"]["wall"]["union_thickness_mm"], 100)
                self.assertEqual(result["results"]["wall"]["overlap_mm"], 0)
                self.assertEqual(hatch.dxf.hatch_style, style)
                self.assertEqual(result["hatch_style_equivalences"][0]["original_style"], style)

    def test_non_normal_hatch_with_multiple_boundaries_is_not_blindly_normalized(self):
        for style in (1, 2):
            with self.subTest(style=style):
                doc = ezdxf.new(); doc.units = 4; doc.layers.new("WALL")
                hatch = doc.modelspace().add_hatch(dxfattribs={"layer": "WALL", "hatch_style": style})
                hatch.paths.add_polyline_path([(0, 0), (1000, 0), (1000, 1000), (0, 1000)], is_closed=True)
                hatch.paths.add_polyline_path([(100, 100), (900, 100), (900, 900), (100, 900)], is_closed=True)
                result = self.audit(doc, spec_for([section("wall", [0, 1000], [-10, 1010])]))
                self.assertEqual(result["status"], "待核", result)
                self.assertEqual(result["hatch_style_equivalences"], [])
                self.assertEqual(hatch.dxf.hatch_style, style)

    def test_non_normal_hatch_open_boundary_is_pending(self):
        doc = ezdxf.new(); doc.units = 4; doc.layers.new("WALL")
        hatch = doc.modelspace().add_hatch(dxfattribs={"layer": "WALL", "hatch_style": 1})
        hatch.paths.add_polyline_path([(0, 0), (100, 0), (100, 1000), (0, 1000)], is_closed=False)
        result = self.audit(doc, spec_for([section("wall", [0, 100], [-10, 110])]))
        self.assertEqual(result["status"], "待核")

    def test_documented_normal_junction_is_not_deleted_or_failed_by_area(self):
        doc, _, _, _ = model()
        wall(doc, [0, 450, 1000, 550])
        check = section("joint", [0, 1000], [-10, 1010])
        check.update(section_role="junction", junction_evidence="source T intersection")
        count = len(doc.modelspace())
        result = self.audit(doc, spec_for([check]))
        self.assertEqual(result["status"], "通过", result)
        self.assertGreater(result["results"]["joint"]["overlap_mm"], 0)
        self.assertEqual(len(doc.modelspace()), count)

    def test_solid_wall_in_open_region_fails_before_missing_evidence(self):
        doc, _, _, dim = model(); wall(doc, [300, 300, 700, 400])
        spec = full_spec(dim); spec["required_ids"].append("missing-source-wall")
        result = self.audit(doc, spec)
        self.assertEqual(result["status"], "需返修")
        self.assertIn("opening", result["failed"])
        self.assertIn("missing-source-wall", result["pending"])

    def test_net_span_cannot_skip_an_intermediate_wall_or_use_outer_face(self):
        doc, _, _, dim = model(); wall(doc, [400, 400, 500, 600])
        result = self.audit(doc, full_spec(dim))
        self.assertIn("wall_material_blocks_claimed_net_span", result["results"]["net"]["failures"])
        doc, _, _, dim = model(); spec = full_spec(dim)
        spec["checks"][2]["from"]["position"] = "min"
        spec["checks"][2]["expected_mm"] = 900
        result = self.audit(doc, spec)
        self.assertIn("wall_material_blocks_claimed_net_span", result["results"]["net"]["failures"])

    def test_missing_boundary_is_pending_not_false_clearance(self):
        doc, _, right, dim = model(); doc.modelspace().delete_entity(right)
        result = self.audit(doc, full_spec(dim))
        self.assertEqual(result["status"], "待核")
        self.assertIn("right", result["missing_boundaries"])
        self.assertNotIn("actual_span_mm", result["results"]["net"])

    def test_source_attachment_candidates_are_preserved_not_chosen(self):
        doc, _, _, dim = model(); spec = full_spec(dim)
        control = spec["checks"][3]
        control["from"] = {"section": "left", "position_candidates": ["center", "max"]}
        control["inference_reason"] = "marketing tick does not uniquely identify a wall face"
        result = self.audit(doc, spec)
        self.assertEqual(result["status"], "待核")
        self.assertEqual(result["results"]["control"]["from_candidates_mm"], [50, 100])
        self.assertEqual(result["retained_inferences"][0]["id"], "control")

    def test_non_same_section_requires_explicit_reference_span(self):
        doc, _, _, dim = model(); spec = full_spec(dim)
        spec["checks"][1]["at_mm"] = 600
        result = self.audit(doc, spec)
        self.assertIn("net", result["pending"])
        spec["checks"][2].update(measurement_kind="reference_span", reference_reason="two source control planes; not a room net")
        result = self.audit(doc, spec)
        self.assertEqual(result["status"], "通过", result)
        self.assertEqual(result["results"]["net"]["measurement_kind"], "reference_span_not_room_net")

    def test_numeric_definition_and_image_uncertainty_are_separate(self):
        doc, _, _, dim = model(); spec = full_spec(dim)
        spec["checks"][3]["expected_mm"] = 899
        spec["checks"][3]["image_uncertainty_mm"] = 100
        result = self.audit(doc, spec)
        self.assertEqual(result["status"], "需返修")
        self.assertIn("dimension_definition_or_direction_disagrees_with_source_value", result["results"]["control"]["failures"])

    def test_wrong_material_cannot_pass_as_rail(self):
        doc, _, _, _ = model()
        check = {"id": "rail", "kind": "line_section", "axis": "x", "at_mm": 500,
                 "layers": ["FRAME"], "search_mm": [-10, 150], "expected_faces_mm": [0, 100],
                 "image_uncertainty_mm": 1, "source_evidence": "source rail, not a solid wall"}
        result = self.audit(doc, spec_for([check]))
        self.assertEqual(result["status"], "需返修")

    def test_empty_inventory_hash_mismatch_and_nonfinite_are_pending(self):
        doc, _, _, dim = model()
        empty = spec_for([])
        self.assertEqual(self.audit(doc, empty)["status"], "待核")
        spec = full_spec(dim); spec["source_sha256"] = "c"*64
        self.assertIn("identity/source_sha256", self.audit(doc, spec)["pending"])
        for value in (float("nan"), float("inf"), True, "500"):
            spec = full_spec(dim); spec["checks"][0]["at_mm"] = value
            self.assertEqual(self.audit(doc, spec)["results"]["left"]["status"], "待核")

    def test_legacy_opt_in_is_explicit(self):
        self.assertIsNone(review.declared_spec({"rooms": []}))
        self.assertEqual(review.declared_spec({"review_version": review.VERSION}), {})
        self.assertEqual(review.declared_spec({"source_geometry_review": {"x": 1}}), {"x": 1})
        self.assertEqual(review.declared_spec({"source_geometry_review_required": True}), {})
        self.assertIsNone(review.declared_spec({"source_geometry_review_required": False}))
        self.assertEqual(review.declared_spec({"source_geometry_review": None}), {})

    def test_cli_binds_actual_files_and_writes_report(self):
        doc, _, _, dim = model()
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder); drawing = folder/"drawing.dxf"; doc.saveas(drawing)
            source = folder/"source-reference"; source.write_bytes(b"independent source bytes")
            spec = full_spec(dim); spec["dxf_sha256"] = review.digest(drawing); spec["source_sha256"] = review.digest(source)
            path = folder/"spec.json"; path.write_text(json.dumps(spec)); output = folder/"report.json"
            run = subprocess.run([sys.executable, review.__file__, str(drawing), str(path), "--source", str(source), "--report", str(output)], capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stdout+run.stderr)
            self.assertEqual(json.loads(output.read_text())["status"], "通过")
            spec["dxf_sha256"] = "d"*64; path.write_text(json.dumps(spec))
            run = subprocess.run([sys.executable, review.__file__, str(drawing), str(path), "--source", str(source), "--report", str(output)], capture_output=True, text=True)
            self.assertEqual(run.returncode, 2, run.stdout+run.stderr)
            result = json.loads(output.read_text())
            self.assertEqual(result["status"], "待核")
            self.assertIn("identity/dxf_sha256", result["pending"])


if __name__ == "__main__":
    unittest.main()
