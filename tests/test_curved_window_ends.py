import math
import sys
import unittest
from pathlib import Path

import ezdxf
from shapely.geometry import LineString, box

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"scripts"))
from curved_window_ends import curve_path, inspect_ends
from wall_geometry_v16 import Results
from check_room_material import check
from test_wall_geometry_v16 import CROP, CURRENT, SOURCE, make
from wall_geometry_v16 import audit


class CornerWindow(unittest.TestCase):
    def setUp(self):
        model = ezdxf.new().modelspace()
        self.rails = [curve_path(model.add_ellipse((0,0), (100+d,0), ratio=(80+d)/(100+d),
                     start_param=0, end_param=math.pi/2), .1) for d in (0,5,10,15)]
        self.op = {"id":"W", "a":[107.5,0], "b":[0,87.5], "jamb_support":["wall","frame"],
                   "frame_neighbors":{"b":"next"}}
        self.cap = LineString([(100,0),(115,0)])
        self.shared = LineString([(0,80),(0,95)])

    def check(self, cap, shared, wall):
        out = Results()
        inspect_ends(out, self.op, self.rails, [cap], {"next":[shared]}, wall, .1, lambda _: False)
        return out.report()

    def test_curved_rails_have_different_normals_and_can_share_cap(self):
        report = self.check(self.cap, self.shared, box(95,-10,120,0))
        self.assertEqual(report["status"], "pass", report)

    def test_cap_gap_and_unrelated_adjacent_frame_fail(self):
        report = self.check(self.cap, LineString([(-2,80),(-2,95)]), box(95,-10,120,-1))
        self.assertIn("end/W/a", report["failures"])
        self.assertIn("end/W/b", report["failures"])

    def test_shortened_cap_is_not_evidence_of_full_support(self):
        report = self.check(LineString([(102,0),(115,0)]), self.shared, box(95,-10,120,0))
        self.assertIn("end/W/a", report["failures"])

    def test_two_independently_drawn_coincident_caps_are_an_editing_defect(self):
        for other, duplicate in [({"4"},True),({"2"},False)]:
            out=Results()
            inspect_ends(out,self.op,self.rails,[self.cap,self.shared],{"next":[self.shared]},
                         box(95,-10,120,0),.1,lambda _:False,{"W":{"1","2"},"next":other})
            self.assertEqual("duplicate-cap/W/b" in out.report()["failures"],duplicate)

    def build_corner(self):
        doc, ledger = make()
        ledger.update(drafting_version="1.6.1",rooms=[dict(id="room",free_points=[[400,500]])])
        op = dict(self.op,kind="window",hosts=[],wall_thickness_mm=15,clear_margin_mm=2,
                  geometry_exception={**CROP,"kind":"corner_frame"}, symbol_handles=[])
        for d in (0,5,10,15):
            ent = doc.modelspace().add_ellipse((0,0), (100+d,0), ratio=(80+d)/(100+d),
                       start_param=0, end_param=math.pi/2, dxfattribs={"layer":CURRENT["window"]})
            op["symbol_handles"].append(ent.dxf.handle)
        ledger["openings"].append(op)
        return doc,ledger

    def test_diagnoses_wall_in_curved_aperture_and_ignores_chord(self):
        doc, ledger = self.build_corner()
        msp=doc.modelspace()
        def solid(x,y):
            msp.add_solid([(x,y),(x+4,y),(x,y+4),(x+4,y+4)],dxfattribs={"layer":CURRENT["bearing"]})
        def actual():
            return next(r for r in check(doc,ledger)["checks"] if r["id"]=="W")
        solid(51,42)
        self.assertEqual(actual()["status"],"通过")
        solid(76,59)
        self.assertEqual(actual()["status"],"失败")
        ledger["openings"][-1]["symbol_handles"].pop()
        self.assertEqual(actual()["status"],"待核")

    def test_native_corner_does_not_skip_adjacent_furniture_or_straight_frames(self):
        doc,ledger=self.build_corner()
        block=doc.blocks.new("TV");block.add_lwpolyline([(0,0),(12,0),(12,12),(0,12)],close=True)
        ref=doc.modelspace().add_blockref("TV",(71,53),dxfattribs={"layer":CURRENT["fixed"]})
        obj={"handle":ref.dxf.handle,"block":"TV"}
        ledger["geometry_objects_v16"]={"TV":obj}
        job=dict(items={"TV":dict(**obj,installed=True,free_sides={},freestanding_basis=None)},
                 relations=[],review_reach_mm=75)
        r=audit(doc,ledger,job,source_sha256=SOURCE)
        self.assertFalse(any("measurement-incomplete" in i for i in r["pending"]),r)
        self.assertIn("frame-in-body/W/TV",r["failures"],r)
        self.assertIn("four-rails/north",[row["id"] for row in r["checks"]],r)
