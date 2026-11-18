"""V1.5 source scale, open boundaries, furniture and drawing acceptance."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"examples"))
from build_plan_objects import compile_plan
from dimension_grid import prepare_plan
from native_readback import digest
from professional_review import assess_capture
from review_working_plan import checked_run, model_findings, review
from smoke_job import make_job
from test_release_geometry_gate import fixture


def load(path):
    return json.loads(path.read_text())


class DeliveryPolicy(unittest.TestCase):
    def test_assumed_reference_only_sets_one_wall_and_records_the_scale(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/"input"
            make_job(root)
            scene=load(root/"scene.json")
            scene.pop("source_to_mm")
            scene["dimension_chains"]=[]
            controls={}
            for key,px,face in [("first",15.5,"min_face"),("second",25.5,"max_face")]:
                controls[key]=dict(kind="wall_face",axis="x",px=px,evidence="west face",
                                   bindings=[dict(wall="west",at=face)])
            scene["dimension_grid"]=dict(mode="unmarked",controls=controls,
                reference=dict(kind="assumed_wall_thickness",axis="x",controls=list(controls),evidence="shear wall candidate"))
            for row in scene["walls"]:
                if row["id"]=="west":
                    row.pop("thickness_evidence")
            prepared,_=prepare_plan(scene,[])
            walls={w["id"]:w for w in prepared["walls"]}
            self.assertEqual(walls["west"]["width_mm"],200)
            self.assertEqual(walls["west"]["wall_thickness_review"]["basis"],"assumed_wall_thickness")
            self.assertEqual(walls["divider"]["width_mm"],120)
            self.assertEqual(walls["divider"]["wall_thickness_review"]["basis"],"provisional_pixel_scale")
            self.assertEqual(prepared["grid_registration"]["scale_provenance"]["value_mm"],200)
            controls["second"]["bindings"][0]["wall"]="divider"
            with self.assertRaisesRegex(ValueError,"opposite faces"):
                prepare_plan(scene,[])

    def test_open_direction_requires_same_room_verified_geometry_and_usable_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            review=fixture(root)
            ledger=load(root/"ledger.json")
            ledger["drafting_version"]="1.5.0"
            geom=ledger["source_geometry_review"]
            geom["required_ids"].append("open-edge")
            geom["checks"].append(dict(id="open-edge",kind="clear_region",room_id="room",axis="y",
                 polygon_mm=[[300,400],[600,400],[600,600],[300,600]],source_evidence="synthetic open region"))
            row=review["professional_review"]["rooms"][0]
            side=dict(context="perimeter",boundary="north edge",adjacent="terrace",source_evidence="open edge on plan",
                      use_check="checked clear passage",clear_region_id="open-edge")
            row.update(room_use="平台",enclosure="open",open_sides={"y":side})
            row["spans"]=row["spans"][:1]
            def result():
                return assess_capture(root/"review/native",ledger,review,{})
            self.assertEqual(result()["missing"],[])
            row["room_use"]="主卧"
            self.assertTrue(any("围护" in s for s in result()["missing"]))
            row["room_use"]="卫生间"
            self.assertTrue(any("围护" in s for s in result()["missing"]))
            row["room_use"]="平台"
            geom["checks"][-1]["axis"]="x"
            self.assertTrue(any("同方向" in s for s in result()["missing"]))
            geom["checks"][-1].update(axis="y",polygon_mm=[[10,400],[60,400],[60,600],[10,600]])
            self.assertIn("open-edge",result()["source_geometry_review"]["failed"])
            self.assertTrue(any("净区" in s for s in result()["missing"]))

    def test_furniture_uses_catalogued_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/"input"
            make_job(root)
            scene=load(root/"scene.json")
            objects=load(root/"objects.json")
            doc,products=compile_plan(scene,objects)
            for record in products["objects"].values():
                self.assertEqual(doc.entitydb[record["handle"]].dxftype(),"INSERT")
                self.assertIn("block",record["source"])
            for role in ("fixed","movable"):
                with self.subTest(role=role),self.assertRaisesRegex(ValueError,"参考图库"):
                    compile_plan(scene,[dict(id="table",type="outline",role=role)])

    def test_print_option_preserves_identity_and_old_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            fixture(root,paper=False)
            dwg=digest(root/"review/native/plan.dwg")
            self.assertTrue(any("纸图" in k for k in model_findings(root,dwg)[1]))
            ledger=load(root/"ledger.json")
            ledger["drafting_version"]="1.5.0"
            (root/"ledger.json").write_text(json.dumps(ledger))
            self.assertFalse(any("纸图" in k for k in model_findings(root,dwg)[1]))
            note=root/"review/print-proof.json"
            note.write_text(json.dumps(dict(status="未要求输出PDF",dwg_sha256=dwg)))
            self.assertEqual(checked_run(root),dwg)
            note.write_text(json.dumps(dict(status="未要求输出PDF",dwg_sha256="bad")))
            with self.assertRaisesRegex(ValueError,"version"):
                checked_run(root)

    def test_blind_defect_must_enter_batch_and_remain_visible_until_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            assessment=fixture(root)
            ledger=load(root/"ledger.json")
            ledger["drafting_version"]="1.5.0"
            (root/"ledger.json").write_text(json.dumps(ledger))
            blind=dict(reviewer="independent example",dxf_sha256=digest(root/"review/native/readback.dxf"),
                       open_defects=["stair-crosses-wall"])
            (root/"review/cad-only-frozen.json").write_text(json.dumps(blind))
            self.assertIn("盲审/stair-crosses-wall",review(root,assessment)["open_repairs"])
            with self.assertRaisesRegex(ValueError,"原 ID"):
                review(root,assessment,root,{})

    def test_paper_metadata_does_not_block_native_format(self):
        from audit_presentation import audit
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/"input"
            make_job(root)
            doc, products=compile_plan(load(root/"scene.json"),load(root/"objects.json"))
            result=audit(doc,products["ledger"])
            self.assertTrue(result["passed"],result["issues"])
            self.assertFalse(result["paper"]["passed"])
