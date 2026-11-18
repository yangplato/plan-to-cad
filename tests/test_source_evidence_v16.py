"""Regression fixtures use synthetic pixels and DXF, with no claimed production review."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import ezdxf
from PIL import Image, ImageDraw
from shapely.geometry import box

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"scripts"))
import source_evidence as evidence
from draw_plan_parts import add_wall_material
from presentation import CURRENT, PAPER_LAYERS


def row(ident, kind, points, *, panel="lower", subtype=None, **rest):
    return dict(id=ident, panel=panel, kind=kind, subtype=subtype or kind,
                points_px=points, box_px=[0,40,140,100] if panel=="lower" else [0,0,140,40],
                uncertainty_px=1, evidence="Synthetic source witness", **rest)


def panel_spec(sha):
    return dict(schema=evidence.PANEL_SCHEMA, source_sha256=sha,
        panels=[dict(id="lower",role="target",box_px=[0,40,140,100],label="装修图"),
                dict(id="upper",role="reference",box_px=[0,0,140,40],label="结构图")])


def reader(sha, session, rows, **rest):
    return dict(schema=evidence.INVENTORY_SCHEMA,source_sha256=sha,source_only=True,
                reader="同型号的两个独立会话",session_id=session,items=rows,**rest)


class SourceEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.image = self.root/"source.png"
        image = Image.new("RGB", (140,100),"white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((10,65,39,71),fill=(132,128,121))
        draw.rectangle((40,65,70,71),fill=(61,59,57))
        image.save(self.image)
        self.sha = evidence.digest(self.image)
        self.panels_file = self.root/"panels.json"
        self.spec = panel_spec(self.sha)
        evidence.write(self.panels_file,self.spec)
        self.panels, self.target, self.legend = evidence.manifest(self.panels_file,self.sha,image.size)
        self.reg = dict(source_sha256=self.sha, source_panel=self.target,
                    source_to_mm=[10,0,0,-10,0,1000], layers=CURRENT,
                    holdout_limit_mm=0,holdouts=[dict(source_xy=[5,43],cad_mm=[50,570])])
        self.logs = []

    def add(self,state,ident,message,details=None):
        self.logs.append((state,ident,message,details))

    def source_file(self, name, stamp, rows, **rest):
        target = self.root/name
        evidence.write(target,reader(self.sha,stamp,rows,**rest))
        return target

    def inventory(self, name, stamp, rows, **rest):
        return evidence.inventory(self.source_file(name,stamp,rows,**rest),self.sha,self.panels,self.target)

    def make_wall(self, doc, bounds, tone="bearing"):
        layer = CURRENT[tone]
        add_wall_material(doc.modelspace(), {"segment":(layer,box(*bounds))},{},{layer:66})

    def native_doc(self):
        doc = ezdxf.new("R2018")
        doc.units = 4
        for name in {*CURRENT.values(), PAPER_LAYERS["rooms"]}:
            doc.layers.new(name)
        return doc

    def test_visual_crops_preserve_original_and_keep_panel_roles(self):
        dst = self.root/"prepare"
        meta = evidence.prepare(self.image,self.panels_file,dst)
        self.assertEqual(meta["target_panel"],"lower")
        self.assertEqual({r["role"] for r in meta["views"]},{"target","reference"})
        with Image.open(dst/"panel-001.png") as plate:
            self.assertEqual(plate.getpixel((4,29)),(255,255,255))
        self.assertEqual(evidence.digest(self.image),self.sha)
        self.assertEqual(evidence.prepare(self.image,self.panels_file,dst)["views"],meta["views"])

    def test_report_can_retain_dwg_flow_with_missing_readers(self):
        report = evidence.run(self.image,None,None,None,None,None,self.root/"review")
        self.assertEqual(report["status"],"review")
        self.assertEqual(report["failures"], [])
        for missing in ("panels","author","independent"):
            self.assertIn("input/"+missing,report["pending"])
        self.assertEqual(report["hashes"]["source_sha256"],self.sha)
        self.assertIsNone(report["hashes"]["independent_sha256"])

    def test_reference_rail_not_mixed_into_target_inventory(self):
        first = self.inventory("a.json","a",[
            row("window-a","window",[[14,61],[49,61]]),
            row("same-id-unneeded","rail",[[12,20],[49,20]],panel="upper")],
            absent={"rail":"装修图只有落地窗"})
        second = self.inventory("b.json","b",[
            row("window-b","window",[[49,61],[14,61]])],
            absent={"rail":"装修图只有落地窗"})
        result = evidence.compare(first,second,12,self.add)
        self.assertEqual([(p["author"],p["independent"]) for p in result["pairs"]],
                         [("window-a","window-b")])
        self.assertEqual(result["unmatched"],[])
        self.assertEqual(self.logs,[])

    def test_wrong_panel_pixels_are_rejected(self):
        with self.assertRaisesRegex(ValueError,"超出所属图版"):
            self.inventory("a.json","a",[row("window-from-upper","window",[[14,21],[49,21]])])

    def test_same_position_conflicting_frame_and_displaced_furniture_are_reported(self):
        first = self.inventory("a.json","a",[
            row("w","window",[[14,61],[49,61]]),row("a-chair","furniture",[[93,70]],role="movable",anchor_kind="footprint_center"),
            row("only-a","room",[[72,74]],label="主卧")])
        second = self.inventory("b.json","b",[
            row("r","rail",[[14,61],[49,61]]),row("b-chair","furniture",[[99,70]],role="movable",anchor_kind="footprint_center"),
            row("only-b","wall",[[41,66],[61,66]])])
        result = evidence.compare(first,second,12,self.add)
        self.assertEqual({flag for p in result["pairs"] for flag in p["issues"]},
                         {"kind_or_subtype","position"})
        self.assertEqual({p["side"] for p in result["unmatched"]},{"only_author","only_independent"})

    def test_two_arbitrary_interior_points_do_not_prove_room_or_furniture_displacement(self):
        for kind in ("room","furniture"):
            fields={"label":"卧室"} if kind=="room" else {"role":"fixed"}
            first=self.inventory("a.json","a",[row("a",kind,[[93,70]],**fields)])
            second=self.inventory("b.json","b",[row("b",kind,[[99,70]],**fields)])
            result=evidence.compare(first,second,12,self.add)
            self.assertEqual(result["pairs"][0]["issues"],[])
            self.assertTrue(any("锚点" in str(item) for item in self.logs))

    def test_tied_chairs_do_not_turn_into_an_arbitrary_pair(self):
        a = row("a-chair","furniture",[[93,70]],role="movable")
        b = row("b-chair","furniture",[[92,70]],role="movable")
        c = row("c-chair","furniture",[[94,70]],role="movable")
        pairs, _, _, ambiguous_left, ambiguous_right = evidence.best_pairs([a],[b,c],12)
        self.assertEqual(pairs,[])
        self.assertEqual(ambiguous_left,{0})
        self.assertEqual(ambiguous_right,{0,1})

    def test_same_session_or_copied_rows_cannot_close_independence(self):
        first = self.source_file("a.json","same",[row("same-id","room",[[72,74]],label="主卧")])
        second = self.source_file("b.json","same",[row("other-id","room",[[71,74]],label="主卧")])
        out = self.root/"review"
        report = evidence.run(self.image,self.panels_file,first,second,None,None,out)
        self.assertIn("input/independence", report["pending"])
        self.assertNotEqual(report["hashes"]["author_sha256"],report["hashes"]["independent_sha256"])
        copy = self.source_file("c.json","different",[row("same-id","room",[[72,74]],label="主卧")])
        report = evidence.run(self.image,self.panels_file,first,copy,None,None,out)
        self.assertIn("input/independence", report["pending"])

    def test_dark_clip_and_single_geometric_cue_keep_wall_nature_pending(self):
        wall = row("north","wall",[[42,68],[66,68]],
                   wall_nature={"basis":"tone_only","classification":"bearing"})
        out = evidence.wall_nature(wall,self.panels,{})
        self.assertEqual(out["status"],"review")
        wall["wall_nature"].update(basis="inference",cues=[
            dict(kind="continuity",panel="lower",box_px=[40,64,67,73],evidence="连续墙芯"),
            dict(kind="grey_value",panel="lower",box_px=[40,64,67,73],evidence="墙芯较深")])
        self.assertEqual(evidence.wall_nature(wall,self.panels,{})["status"],"review")
        wall["wall_nature"]["cues"].append(
            dict(kind="structural_panel_match",panel="upper",box_px=[40,9,67,17],
                 evidence="同一段的两侧连续边、短垛和相接柱头在结构图可定位"))
        result = evidence.wall_nature(wall,self.panels,{})
        self.assertEqual((result["status"],result["basis"]),("pass","source_inference"))

    def test_legend_has_priority_and_conflict_is_visible(self):
        wall = row("north","wall",[[42,68],[66,68]],
                   wall_nature={"basis":"legend","classification":"partition","legend_id":"dark"})
        legend = {"dark":{"classification":"bearing","text":"黑色为剪力墙"}}
        self.assertEqual(evidence.wall_nature(wall,self.panels,legend)["status"],"different")
        wall["wall_nature"]["classification"] = "bearing"
        self.assertEqual(evidence.wall_nature(wall,self.panels,legend)["basis"],"printed_legend")

    def test_expected_wall_uses_saved_fill_and_actual_faces(self):
        doc = self.native_doc()
        self.make_wall(doc,(100,280,700,350))
        source = row("wall","wall",[[12,68],[68,68]],section_px=[[25,65],[25,72]])
        trace = evidence.inspect_native(doc,self.reg,[source],self.add)
        self.assertEqual(self.logs,[])
        self.assertEqual(trace["walls_and_rooms"][0]["section"]["actual_faces_mm"],[280,350])
        for entity in doc.modelspace():
            if entity.dxf.layer == CURRENT["bearing"]:
                entity.translate(0,45,0)
        evidence.inspect_native(doc,self.reg,[source],self.add)
        self.assertTrue(any(kind=="different" and path=="native/wall/wall/截面"
                            for kind,path,_,_ in self.logs))
        for entity in doc.modelspace():
            if entity.dxf.layer == CURRENT["bearing"]:
                entity.translate(0,70,0)
        evidence.inspect_native(doc,self.reg,[source],self.add)
        self.assertTrue(any(kind=="different" and path=="native/wall/wall" for kind,path,_,_ in self.logs))

    def test_saved_rail_layer_does_not_satisfy_window(self):
        doc = self.native_doc()
        self.make_wall(doc,(20,300,100,340))
        self.make_wall(doc,(410,300,490,340))
        aux = CURRENT["ancillary"]
        msp = doc.modelspace()
        for y in (312,328):
            msp.add_line((100,y),(410,y),dxfattribs={"layer":aux})
        for x in (100,410):
            msp.add_line((x,312),(x,328),dxfattribs={"layer":aux})
        src = row("窗扇候选","window",[[10,68],[41,68]], end_support=["wall","wall"],wall_thickness_mm=40)
        report = evidence.inspect_native(doc,self.reg,[src],self.add)
        self.assertEqual(report["frames"]["status"],"失败")
        self.assertTrue(any("其他图层" in v for v in report["frames"]["portals"][0]["failures"]))

    def test_wall_tone_report_is_reused_only_when_hashes_and_scope_match(self):
        guides = self.root/"guides.json"
        evidence.write(guides,dict(plan_box_px=[0,40,139,99],
            samples=[dict(points_px=[[15,68]],radius_px=1)],
            axes=[dict(a_px=[10,68],b_px=[70,68])]))
        dxf, reg = self.root/"readback.dxf",self.root/"registration.json"
        dxf.write_bytes(b"Test DXF identity, not a production DWG")
        evidence.write(reg,self.reg)
        hashes = dict(source_sha256=self.sha,readback_dxf_sha256=evidence.digest(dxf),
                      registration_sha256=evidence.digest(reg))
        report = self.root/"wall-tone-stations.json"
        spec = dict(status="different",sha256=dict(image=self.sha,reading=evidence.digest(guides),
            saved_dxf=hashes["readback_dxf_sha256"],registration=hashes["registration_sha256"]),
            source_read_band_px=1,normal_read_band_px=1,
            axes=[dict(orientation="H",normal_px=68,runs=[dict(stations_px=[10,70])])])
        evidence.write(report,spec)
        with patch.object(evidence,"check_tones",side_effect=AssertionError("unexpected recalculation")):
            result,_ = evidence.tone_report(self.image,guides,report,dxf,reg,hashes,[0,40,140,100],self.root,1,None)
        self.assertEqual(result["status"],"different")
        spec["axes"][0]["normal_px"] = 30
        evidence.write(report,spec)
        with self.assertRaisesRegex(ValueError,"超出所属图版"):
            evidence.tone_report(self.image,guides,report,dxf,reg,hashes,[0,40,140,100],self.root,1,None)
        spec["sha256"]["saved_dxf"] = "0"*64
        evidence.write(report,spec)
        with self.assertRaisesRegex(ValueError,"非同版"):
            evidence.tone_report(self.image,guides,report,dxf,reg,hashes,[0,40,140,100],self.root,1,None)

    def test_one_clean_wall_guide_cannot_clear_all_walls(self):
        axes = [dict(orientation="H",normal_px=68,runs=[
            dict(span_px=[10,30],label="bearing",sufficient_tone_evidence=True)])]
        rows = [row("known","wall",[[11,68],[29,68]]),row("missing","wall",[[40,68],[65,68]])]
        evidence.check_tone_coverage(rows,axes,self.add)
        self.assertEqual([(kind,key) for kind,key,_,_ in self.logs],
                         [("review","native/tone-scope/missing")])

    def test_saved_tone_station_reports_a_shift_without_rebuilding_walls(self):
        guides = self.root/"guides.json"
        evidence.write(guides,dict(plan_box_px=[0,40,139,99],samples=[
            dict(material="partition",points_px=[[22,68]],radius_px=1),
            dict(material="bearing",points_px=[[54,68]],radius_px=1)],
            axes=[dict(id="材料换色墙芯",a_px=[10,68],b_px=[70,68],
                       core_width_px=[6,8],search_px=2)]))
        dxf, reg = self.root/"readback.dxf",self.root/"registration.json"
        evidence.write(reg,self.reg)
        for contact, status in [(395,"within_band"),(465,"different")]:
            doc = self.native_doc()
            self.make_wall(doc,(95,285,contact,355),"partition")
            self.make_wall(doc,(contact,285,705,355),"bearing")
            doc.saveas(dxf)
            hashes = dict(source_sha256=self.sha,readback_dxf_sha256=evidence.digest(dxf),
                          registration_sha256=evidence.digest(reg))
            result,axes = evidence.tone_report(self.image,guides,None,dxf,reg,hashes,
                                              [0,40,140,100],self.root,1,None)
            self.assertEqual(result["status"],status)
            self.assertEqual(axes[0]["changes"][0]["status"],
                             "within_band" if status=="within_band" else "shifted_change")


if __name__ == "__main__":
    unittest.main()
