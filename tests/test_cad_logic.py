import sys
import unittest
import tempfile
from hashlib import sha256
from pathlib import Path

import ezdxf
from shapely.geometry import box

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"scripts"))
from check_cad_logic import freeze_review, inspect, review_gaps
from check_registered_frames import Stroke, measure_joined_rail
from drawing_junctions import bind_hinges, draw_frame_path, draw_stair_break, resolve_wall_nodes
from presentation import CURRENT


class CadLogicTests(unittest.TestCase):
    def setUp(self):
        self.doc = ezdxf.new(units=4)
        self.m = self.doc.modelspace()
        for name in (CURRENT["bearing"],CURRENT["ancillary"],CURRENT["door"]):
            self.doc.layers.new(name)
        self.m.add_lwpolyline([(-500,-100),(0,-100),(0,100),(-500,100)],close=True,
                              dxfattribs={"layer":CURRENT["bearing"]})

    def test_three_rail_corner_preserves_all_six_edges(self):
        paths = draw_frame_path(self.m,[(0,500),(1000,500),(1000,0)],[-40,0,40],CURRENT["ancillary"])
        self.assertEqual(len(paths),5)
        for e,d in zip(paths, [-40,0,40]):
            self.assertEqual(e.get_points("xy")[1],(1000+d,500+d))
        self.assertFalse([r for r in inspect(self.doc)["findings"] if "栏杆" in r["check"]])
        projected = []
        for i,e in enumerate(paths):
            pts = e.get_points("xy") if e.dxftype()=="LWPOLYLINE" else [e.dxf.start,e.dxf.end]
            for a,b in zip(pts,pts[1:]):
                projected.append(Stroke(complex(a[0],a[1]-500),complex(b[0],b[1]-500),"rail",str(i)))
        metric = {}
        measure_joined_rail(metric,projected,1000,2,-50,50)
        self.assertEqual(metric["end_turns"],[False,True])
        metric = {}
        # With both transverse tracks missing a long plain stripe is not a corner.
        measure_joined_rail(metric, [s for s in projected if abs(s.end.imag-s.start.imag) < 200],
                            1000,2,-50,50)
        self.assertFalse(metric.get("joined_rail"))

    def test_break_band_cuts_tread_and_preserves_other_flight(self):
        items=draw_stair_break(self.m,[((0,100),(500,100)),((650,100),(950,100))],
                               [(100,0),(300,200)],[(150,0),(350,200)],CURRENT["ancillary"])
        spans=[(e.dxf.start.x,e.dxf.end.x) for e in items if e.dxftype()=="LINE"]
        self.assertEqual(spans,[(0,200),(250,500),(650,950)])

    def test_separately_capped_rails_are_flagged(self):
        for pts in [[(0,500),(1000,500)],[(1000,500),(1000,0)]]:
            draw_frame_path(self.m,pts,[-40,0,40],CURRENT["ancillary"])
        flagged = [r for r in inspect(self.doc)["findings"] if "栏杆" in r["check"]]
        self.assertEqual(len(flagged),1)

    def test_free_hinge_and_real_casing(self):
        attrs = {"layer":CURRENT["door"]}
        self.m.add_arc((20,100),800,0,90,dxfattribs=attrs)
        self.m.add_lwpolyline([(20,100),(20,900),(52,900),(52,100)],close=True,dxfattribs=attrs)
        def gaps():return [r["measured_mm"] for r in inspect(self.doc)["findings"] if "缺支承" in r["check"]]
        self.assertEqual(gaps(),[20])
        self.m.add_lwpolyline([(0,72),(20,72),(20,100),(0,100)],close=True,dxfattribs=attrs)
        self.assertEqual(gaps(),[])

    def test_compute_actual_local_face_at_one_sided_pier(self):
        op = dict(id="d",a=(0,0),b=(900,0),wall_thickness_mm=240,toward=(0,1000),
                  leaves=[dict(hinge="a", share=1, hinge_face="approach")])
        bind_hinges(op,box(-500,-60,0,60))
        self.assertEqual(op["leaves"][0]["actual_pivot_mm"],[0,-60])

    def test_wall_corner_moves_only_declared_ends(self):
        walls=[dict(id="a",a=(0,0),b=(760,0)),dict(id="b",a=(800,50),b=(800,400))]
        resolve_wall_nodes(walls,[dict(members=[dict(wall="a",end="b"),dict(wall="b",end="a")])])
        self.assertEqual(walls[0]["b"],[800,0])
        self.assertEqual(walls[1]["a"],[800,0])
        self.assertEqual(walls[1]["b"],(800,400))

    def test_only_standalone_tracks_with_common_end_match(self):
        attrs = {"layer":CURRENT["door"]}
        for y in (1500,1540):
            self.m.add_line((0,y),(1000,y),dxfattribs=attrs)
        self.m.add_line((1000,1500),(1000,1540),dxfattribs=attrs)
        self.m.add_lwpolyline([(2000,1500),(2900,1500),(2900,1540),(2000,1540)],
                              close=True,dxfattribs=attrs)
        hits=[r for r in inspect(self.doc)["findings"] if r["check"]=="移门轨端缺支承"]
        self.assertEqual(len(hits),1)

    def test_contained_furniture_is_seen_without_crossing_lines(self):
        for role in ("fixed","movable"):
            self.doc.layers.new(CURRENT[role])
        for name,size,position,role in [("cabinet",600,(0,0),"fixed"),("table",200,(100,100),"movable")]:
            block=self.doc.blocks.new(name)
            block.add_lwpolyline([(0,0),(size,0),(size,size),(0,size)],close=True)
            self.m.add_blockref(name,position,dxfattribs={"layer":CURRENT[role]})
        hits=[r for r in inspect(self.doc)["findings"] if r["check"]=="家具包含"]
        self.assertEqual(len(hits),1)
        self.assertEqual(hits[0]["status"],"待判读")

    def test_freeze_is_bound_to_readback_and_cannot_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            native=Path(directory)/"drawing.dxf"
            self.doc.saveas(native)
            obs=dict(reviewer="peer",dxf_sha256=sha256(native.read_bytes()).hexdigest(),
                     junctions="outer corner",access="one entry",symbols="swing",sheet="visible")
            frozen=Path(directory)/"first.json"
            freeze_review(frozen,native,inspect(self.doc),obs)
            with self.assertRaises(FileExistsError):
                freeze_review(frozen,native,inspect(self.doc),obs)
            with self.assertRaises(ValueError):
                freeze_review(Path(directory)/"next.json",native,inspect(self.doc),dict(obs,dxf_sha256="other"))

    def test_inner_window_contacts_both_shoulders_or_is_really_short(self):
        self.doc.layers.new(CURRENT["window"])
        attrs={"layer":CURRENT["window"]}
        # Existing wall ends at x=0, thickness 200 from y=-100 to +100.
        self.m.add_line((65,-900),(65,900),dxfattribs=attrs)
        def hits():
            return [r["measured_mm"] for r in inspect(self.doc)["findings"]
                    if r["check"]=="短墙端到连续窗"]
        self.assertEqual(hits(),[65])
        self.m.add_line((0,-900),(0,-100),dxfattribs=attrs)
        self.m.add_line((0,100),(0,900),dxfattribs=attrs)
        self.assertEqual(hits(),[])

    def test_review_follows_same_readback_and_unresolved_issues(self):
        report={"findings":[{"id":"suspect"}],"coverage_issues":[]}
        rec=dict(reviewer="reviewer",dxf_sha256="a",junctions="corner",access="entry",
                 symbols="wardrobe",sheet="proof",dispositions={})
        self.assertEqual(review_gaps(report,rec,"a"),["suspect"])
        rec["dispositions"]["suspect"]={"status":"可接受","evidence":"独立框同时接墙和门轴"}
        self.assertEqual(review_gaps(report,rec,"a"),[])
        self.assertTrue(review_gaps(report,rec,"b"))
        rec["dispositions"]["suspect"]={"status":"待对图","evidence":"检修侧的折线用途待定"}
        follow={"source_sha256":"s","dxf_sha256":"a",
                "dispositions":{"suspect":{"status":"可接受","evidence":"源图电梯西北侧见同一折线"}}}
        self.assertEqual(review_gaps(report,rec,"a",follow,"s"),[])
        self.assertEqual(review_gaps(report,rec,"a",follow,"wrong"),["suspect"])


if __name__ == "__main__":
    unittest.main()
