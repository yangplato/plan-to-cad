"""Native DXF geometries for the 117㎡ exterior, frame, small step, screen and TV cabinet cases."""
import copy
from pathlib import Path
import sys
import unittest

import ezdxf
from shapely.geometry import box
from shapely.ops import unary_union

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
from build_wall_runs import draw_wall_runs
from draw_opening import draw_opening
from presentation import CURRENT
from wall_geometry_v16 import audit, bind_controls, bind_material_joints, bind_window, jamb_faces, ordinary_preference
from wall_thickness import pixel_selection

SOURCE = "a"*64
CROP = dict(source_sha256=SOURCE,locator="原图北立面局部",bbox_px=[0,0,1300,1300])
E = {"items":{},"relations":[],"review_reach_mm":75}
REG = {"source_to_mm":[1,0,0,-1,0,1050]}


def wall(key, start, end, width=200, role="exterior", material="bearing"):
    return dict(id=key,a=start,b=end,width_mm=width,material=CURRENT[material],
                boundary_role=role,boundary_evidence=CROP,
                thickness_evidence=dict(source_sha256=SOURCE,kind="printed_dimension",locator="北外墙标数",value_mm=width))


def window(kind="window"):
    return dict(id="north",kind=kind,a=[200,1000],b=[800,1000],hosts=["north-wall"],
                wall_thickness_mm=200,lines_mm=[-100,-35,35,100],symbol_depth_mm=200)


def make(walls=None, ops=None):
    walls = [wall("north-wall",[0,1000],[1000,1000])] if walls is None else walls
    ops = [window()] if ops is None else ops
    doc = ezdxf.new("R2018");doc.units=4
    for name in CURRENT.values():
        doc.layers.new(name)
    msp = doc.modelspace()
    draw_wall_runs(msp,walls,ops,{CURRENT["bearing"]:66,CURRENT["partition"]:178})
    for op in ops:
        drawn = draw_opening(msp,op,CURRENT["door"],CURRENT["window"],CURRENT["ancillary"])
        op["symbol_handles"]=[en.dxf.handle for en in drawn]
    ledger=dict(walls=walls,openings=ops,source_sha256=SOURCE,wall_geometry_v16={"exterior_width_mm":200},
                geometry_layers_v16=CURRENT,geometry_objects_v16={},geometry_registration_v16=REG,
                wall_layers={w["material"]:"fills" for w in walls},nonwall_fill_layers=[])
    return doc,ledger


class Geometry(unittest.TestCase):
    def review(self, doc, ledger, job=E, source=SOURCE):
        return audit(doc,ledger,job,source_sha256=source)

    def test_normal_compile_writes_auditable_ledger_and_four_lines_without_authored_offsets(self):
        from test_wall_thickness import grid_scene
        from build_plan_objects import compile_plan
        from io import StringIO
        scene=grid_scene()
        scene.update(drafting_version="1.6.0",wall_geometry_v16={"exterior_width_mm":200})
        scene["walls"][0].update(boundary_role="exterior",boundary_evidence=CROP,
            thickness_evidence=dict(source_sha256=SOURCE,locator="west",kind="printed_dimension",value_mm=200))
        scene["openings"].append(dict(id="W",kind="window",a=[100,1510],b=[100,1910],hosts=["west"]))
        doc,products=compile_plan(scene,[])
        self.assertEqual(products["ledger"]["openings"][-1]["frame_face_offsets_mm"],[-100,100])
        stream=StringIO();doc.write(stream);stream.seek(0)
        report=self.review(ezdxf.read(stream),products["ledger"],products["edge-relations"])
        self.assertEqual(report["status"],"pass",report)

    def test_default_straight_two_cheeks_four_rails(self):
        doc,ledger=make()
        result=self.review(doc,ledger)
        self.assertEqual((result["failures"],result["pending"]),([],[]),result)
        self.assertEqual(result["status"],"pass")

    def test_same_depth_frame_translated_or_duplicated_fails(self):
        for duplicate in (False,True):
            doc,ledger=make()
            op=ledger["openings"][0]
            if duplicate:
                e=doc.entitydb[op["symbol_handles"][1]]
                e.translate(0,-65,0)
            else:
                for handle in op["symbol_handles"]:
                    doc.entitydb[handle].translate(0,12,0)
            result=self.review(doc,ledger)
            self.assertIn("four-rails/north" if duplicate else "flush/north/b",result["failures"])

    def test_end_cap_depth_and_span_read_from_entities(self):
        doc,ledger=make()
        handles=ledger["openings"][0]["symbol_handles"]
        doc.entitydb[handles[-1]].set_points([(800,900),(800,1020)])
        self.assertIn("flush/north/b",self.review(doc,ledger)["failures"])
        doc.entitydb[handles[1]].set_points([(205,965),(800,965)])
        self.assertIn("four-rails/north",self.review(doc,ledger)["failures"])

    def test_two_actual_cheeks_override_stale_depth_but_column_needs_resolution(self):
        op=window()
        op.update(lines_mm=[-30,30],symbol_depth_mm=60)
        material=unary_union([box(0,910,200,1110),box(800,910,1000,1110)])
        bind_window(op,material,SOURCE)
        self.assertEqual(op["frame_face_offsets_mm"],[-90,110])
        self.assertEqual(len(op["lines_mm"]),4)
        bind_window(op,material.union(box(800,800,1000,1110)),SOURCE)
        self.assertNotIn("frame_face_offsets_mm",op)
        self.assertIn("frame_binding_issue",op)

    def test_full_house_exterior_single_width_in_both_directions(self):
        walls=[wall("north-wall",[0,1000],[1000,1000]),wall("east",[1000,1000],[1000,0],240)]
        doc,ledger=make(walls,[])
        self.assertIn("exterior/east",self.review(doc,ledger)["failures"])
        walls[1]["geometry_exception"]={**CROP,"kind":"documented_thickness"}
        self.assertNotIn("exterior/east",self.review(doc,ledger)["failures"])
        walls[1]["geometry_exception"]["source_sha256"]="b"*64
        r=self.review(doc,ledger)
        self.assertIn("exterior/east",r["failures"])
        self.assertIn("exception/east",r["pending"])

    def test_clear_scaled_faces_allow_local_exception_without_printed_wall_number(self):
        walls = [wall("north", [0,1000], [1000,1000]), wall("east", [1000,1000], [1000,0], 240)]
        doc, ledger = make(walls, [])
        ledger["grid_registration"] = {"scale_provenance": "printed"}
        walls[1]["geometry_exception"] = {**CROP, "kind": "documented_thickness"}
        walls[1]["thickness_evidence"] = dict(kind="calibrated_pixels", source_sha256=SOURCE,
            face_points_px=[[880,500],[1120,500]], edge_uncertainty_px=1,
            calibration_basis="dimension chain", locator="east faces")
        self.assertNotIn("exterior/east", self.review(doc, ledger)["failures"])
        walls[1]["thickness_evidence"]["edge_uncertainty_px"] = 25
        self.assertIn("exception/east", self.review(doc, ledger)["pending"])
        ledger["grid_registration"]["scale_provenance"] = {"kind":"assumed_wall_thickness"}
        walls[1]["thickness_evidence"]["edge_uncertainty_px"] = 1
        self.assertIn("exception/east", self.review(doc, ledger)["pending"])

    def test_empty_inventory_and_unclaimed_window_never_pass(self):
        doc,ledger=make()
        ledger["openings"]=[]
        report=self.review(doc,ledger)
        self.assertTrue(any(x.startswith("unregistered-opening/") for x in report["pending"]))
        self.assertIn("wall-envelope/missing",report["failures"])
        ledger["walls"]=[]
        self.assertEqual(self.review(doc,ledger)["status"],"pending")

    def test_missing_hash_crop_or_object_inventory_is_pending(self):
        doc,ledger=make()
        ledger["walls"][0]["boundary_evidence"]={**CROP,"bbox_px":[4000,4000,4200,4200]}
        self.assertIn("classification/north-wall",self.review(doc,ledger)["pending"])
        self.assertIn("source-binding",self.review(doc,ledger,source="b"*64)["pending"])
        self.assertIn("neighbors/inventory",self.review(doc,ledger,None)["pending"])

    def test_material_joint_fixes_source_proven_seam_and_preserves_locked_end(self):
        walls=[wall("a",[0,400],[500,400]),wall("b",[506.7,400],[1000,400],material="partition")]
        row={**CROP,"id":"seam","members":[{"wall":"a","end":"b"},{"wall":"b","end":"a"}],
             "anchor_wall":"a","shared_face":"max_face"}
        scene={"source_sha256":SOURCE,"wall_geometry_v16":{"material_joints":[row]}}
        saved=copy.deepcopy(walls)
        bind_material_joints(walls,scene)
        self.assertEqual(walls[1]["a"],[500,400])
        doc,ledger=make(walls,[])
        ledger["wall_geometry_v16"]["material_joints"]=[row]
        self.assertEqual(self.review(doc,ledger)["status"],"pass")
        scene["grid_registration"]={"controls":{"printed-end":{"axis":"x","mm":506.7,
                                  "bindings":[{"wall":"b","at":"a"}]}}}
        with self.assertRaisesRegex(ValueError,"conflicts"):
            bind_material_joints(saved,scene)

    def test_real_t_node_on_through_member(self):
        walls=[wall("a",[0,500],[1000,500]),wall("b",[600,0],[600,492],width=120,role="interior",material="partition")]
        scene={"source_sha256":SOURCE,"wall_geometry_v16":{"material_joints":[
               {**CROP,"id":"T","members":[{"wall":"a","end":"through"},{"wall":"b","end":"b"}]}]}}
        bind_material_joints(walls,scene)
        self.assertEqual(walls[1]["b"],[600,500])
        doc,ledger=make(walls,[])
        self.assertNotIn("joint/a/b",self.review(doc,ledger)["failures"])

    def test_short_jog_and_true_column_have_different_treatment(self):
        walls=[wall("a",[0,500],[500,500]),wall("b",[500,506.7],[1000,506.7])]
        doc,ledger=make(walls,[])
        result=self.review(doc,ledger)
        self.assertTrue(any(k.startswith("错台/") for k in result["pending"]))
        self.assertIn("joint/a/b",result["pending"])
        walls[1].update(boundary_role="detail",element_kind="column",geometry_exception={**CROP,"kind":"column"})
        self.assertNotIn("exception/b",self.review(doc,ledger)["pending"])
        stale={**CROP,"kind":"column","ends_px":[[500,200],[500,214]],"edge_uncertainty_px":0}
        ledger["wall_geometry_v16"]["step_details"]=[stale]
        self.assertTrue(any(k.startswith("错台/") for k in self.review(doc,ledger)["pending"]))

    def test_sliding_end_and_cabinet_through_protruding_wall(self):
        op=window("sliding")
        op.update(tracks_mm=[-10,10],panels=[dict(start=0,end=.6,track=0),dict(start=.4,end=1,track=1)],symbol_depth_mm=40)
        doc,ledger=make(ops=[op])
        doc.entitydb[op["symbol_handles"][-1]].translate(-18,0,0)
        # TV cabinet crosses exposed wall/column end and the sliding line at the same time.
        block=doc.blocks.new("TV");block.add_lwpolyline([(0,0),(180,0),(180,60),(0,60)],close=True)
        ref=doc.modelspace().add_blockref("TV",(130,985),dxfattribs={"layer":CURRENT["fixed"]})
        obj={"handle":ref.dxf.handle,"block":"TV"}
        ledger["geometry_objects_v16"]={"TV":obj}
        job=dict(items={"TV":dict(**obj,installed=True,free_sides={},freestanding_basis=None)},
                 relations=[],review_reach_mm=75)
        r=self.review(doc,ledger,job)
        self.assertIn("end/north/b",r["failures"],r)
        self.assertTrue(any(k.startswith("collision/") for k in r["failures"]),r)
        self.assertIn("frame-in-body/north/TV",r["failures"])

    def test_adjacent_partition_meets_inner_window_at_both_flanks(self):
        for target,expected in [(900,"pass"),(893.3,"pending")]:
            walls=[wall("north-wall",[0,1000],[1000,1000]),
                   wall("divider",[500,200],[500,target],120,"interior","partition")]
            doc,ledger=make(walls)
            key="wall-to-window/divider/b/north"
            r=self.review(doc,ledger)
            row=next(r for r in r["checks"] if r["id"]==key)
            self.assertEqual(row["status"],expected)
            self.assertAlmostEqual(row["actual_gaps_mm"][0],900-target)
            if target!=900:
                ledger["wall_geometry_v16"]["frame_wall_ends"]=[{**CROP,"wall":"divider","end":"b","window":"north","gap_mm":0}]
                self.assertIn(key,self.review(doc,ledger)["failures"])

    def test_duplicate_native_wall_material_and_orphan_object_are_reported(self):
        doc,ledger=make()
        hatch=next(e for e in doc.modelspace() if e.dxftype()=="HATCH")
        doc.modelspace().add_entity(hatch.copy())
        ref=doc.modelspace().add_lwpolyline([(20,20),(30,20),(30,30)],close=True,dxfattribs={"layer":CURRENT["fixed"]})
        report=self.review(doc,ledger)
        self.assertTrue(any(key.startswith("material-overlap/") for key in report["failures"]))
        self.assertIn("unregistered-object/"+ref.dxf.handle,report["pending"])

    def test_source_confirmed_short_column_return_uses_both_end_pixels(self):
        walls=[wall("a",[0,500],[500,500]),wall("b",[500,506.7],[1000,506.7])]
        doc,ledger=make(walls,[])
        report=self.review(doc,ledger)
        found=[r for r in report["checks"] if r["id"].startswith("错台/")]
        self.assertEqual(len(found),2)
        ledger["wall_geometry_v16"]["step_details"]=[{**CROP,"kind":"column",
            "ends_px":[[x,1050-y] for x,y in r["ends_mm"]],"edge_uncertainty_px":0.2} for r in found]
        self.assertFalse(any(k.startswith("错台/") for k in self.review(doc,ledger)["pending"]))

    def test_registered_control_measures_native_faces_and_detects_displaced_face(self):
        doc,ledger=make()
        ledger["grid_registration"]={"controls":{"north-in":{"kind":"wall_face","axis":"y","mm":903,
                                     "bindings":[{"wall":"north-wall","at":"min_face"}]}}}
        self.assertIn("control/north-in/north-wall/min_face",self.review(doc,ledger)["failures"])

    def test_bind_real_control_only(self):
        scene=dict(source_sha256=SOURCE,dimension_grid={"controls":{"fixed":{"kind":"wall_face","bindings":[]}}},
                   wall_geometry_v16={"continuous_faces":[{**CROP,"id":"south","control":"fixed",
                   "members":[{"wall":"a","at":"max_face"},{"wall":"b","at":"max_face"}]}]})
        bind_controls(scene)
        self.assertEqual(len(scene["dimension_grid"]["controls"]["fixed"]["bindings"]),2)
        scene["dimension_grid"]["controls"]["fixed"]["kind"]="datum"
        with self.assertRaisesRegex(ValueError,"physical control"):
            bind_controls(scene)

    def test_global_choice_only_inside_local_pixel_range(self):
        w=wall("test",[0,100],[500,100],width=240)
        w["thickness_evidence"]={**CROP,"kind":"calibrated_pixels","face_points_px":[[50,0],[50,220]],
                                 "calibration_basis":"registered 1000 mm span","edge_uncertainty_px":12}
        scene=dict(source_sha256=SOURCE,wall_geometry_v16={"exterior_width_mm":200})
        preferred=ordinary_preference(scene,w)
        self.assertEqual(pixel_selection(w,SOURCE,lambda p:p,preferred)["selected_mm"],200)
        w["thickness_evidence"].update(face_points_px=[[50,0],[50,240]],edge_uncertainty_px=2)
        self.assertEqual(pixel_selection(w,SOURCE,lambda p:p,preferred)["selected_mm"],240)


if __name__=="__main__":
    unittest.main()
