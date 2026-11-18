import unittest
from io import StringIO

import ezdxf
from test_wall_geometry_v16 import CROP, CURRENT, E, SOURCE, make, wall, window
from curved_wall_returns import draw_returns, footprint
from wall_geometry_v16 import audit


def example():
    walls=[wall("west",[100,0],[100,400]),wall("north-wall",[500,800],[1500,800])]
    op=window()
    op.update(a=[500,800],b=[1200,800],jamb_support=["frame","wall"])
    doc,ledger=make(walls,[op])
    op["jamb_support"]=["wall","wall"]
    spec=dict(CROP,id="corner",wall_id="west",wall_end="b",window_id="north",window_end="a")
    return doc,ledger,spec


class CurvedReturn(unittest.TestCase):
    def test_normal_job_builds_real_jamb_before_binding_the_straight_window(self):
        from test_wall_thickness import grid_scene
        from build_plan_objects import compile_plan
        from drafting_version import RELEASE
        scene=grid_scene()
        scope=dict(CROP,bbox_px=[0,0,1700,2550])
        scene.update(drafting_version=RELEASE,wall_geometry_v16={"exterior_width_mm":200})
        west=scene["walls"][0]
        west.update(boundary_role="exterior",boundary_evidence=scope,
                    thickness_evidence=dict(kind="printed_dimension",source_sha256=SOURCE,locator="west",value_mm=200))
        scene["walls"].append(dict(west,id="south",a=[500,2400],b=[1500,2400]))
        scene["openings"].append(dict(id="window",kind="window",a=[500,2400],b=[1200,2400],hosts=["south"]))
        scene["curved_wall_returns"]=[dict(scope,id="bend",wall_id="west",wall_end="b",window_id="window",window_end="a")]
        doc, products=compile_plan(scene,[])
        ledger=products["ledger"]
        self.assertAlmostEqual(ledger["openings"][-1]["frame_face_offsets_mm"][0],-100,places=3)
        result=audit(doc,ledger,products["edge-relations"],source_sha256=SOURCE)
        for key in ("solid-return/bend","flush/window/a","wall-envelope/extra","wall-envelope/missing"):
            self.assertIn((key,"pass"),[(r["id"],r["status"]) for r in result["checks"]],result)

    def test_native_associative_contour_and_hatch_respond_to_edits(self):
        doc,ledger,row=example()
        draw_returns(doc,ledger,[row])
        stream=StringIO();doc.write(stream);stream.seek(0);doc=ezdxf.read(stream)
        def outcome():
            r=audit(doc,ledger,E,source_sha256=SOURCE)
            self.assertNotIn("wall-envelope/extra",r["failures"],r)
            return next(c for c in r["checks"] if c["id"]=="solid-return/corner")
        self.assertEqual(outcome()["status"],"pass")
        shape,layer=footprint(ledger,row)
        self.assertEqual(layer,CURRENT["bearing"])
        self.assertAlmostEqual(shape.area,400*200*3.14159265/2,delta=4)
        boundary=doc.entitydb[ledger["curved_wall_returns"][0]["native_handles"][1]]
        boundary.translate(0,2,0)
        self.assertEqual(outcome()["status"],"fail")

    def test_invalid_direction_crop_or_support_stays_out_of_the_model(self):
        for change in [dict(wall_end="a"),dict(window_end="b"),dict(bbox_px=[10,10,20,20]),
                       dict(source_sha256="b"*64)]:
            doc,ledger,row=example()
            with self.assertRaises(ValueError):
                footprint(ledger,{**row,**change})
        doc,ledger,row=example()
        with self.assertRaisesRegex(ValueError,"Duplicate"):
            draw_returns(doc,ledger,[row,row])
