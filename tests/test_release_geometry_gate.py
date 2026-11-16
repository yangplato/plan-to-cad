"""Saved-DXF release regressions; DWG/PDF bytes are synthetic hash fixtures only."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

import ezdxf

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import review_working_plan
from source_geometry_review import VERSION, audit as geometry_audit


def fixture(root, east=900, source_spec=True, paper=True):
    native=root/'review/native';native.mkdir(parents=True)
    def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
    def write(p,v):p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(v))
    doc=ezdxf.new('R2018');doc.units=4;doc.layers.new('WALL')
    def wall(box):
        x0,y0,x1,y1=box
        points=[(x0,y0),(x1,y0),(x1,y1),(x0,y1)]
        poly=doc.modelspace().add_lwpolyline(points,close=True,dxfattribs={'layer':'WALL'})
        hatch=doc.modelspace().add_hatch(dxfattribs={'layer':'WALL'})
        hatch.paths.add_polyline_path(points,is_closed=True)
        return poly.dxf.handle
    left=wall([0,0,100,1000]);right=wall([east,0,east+100,1000])
    bottom=wall([100,0,east,100]);top=wall([100,900,east,1000])
    doc.saveas(native/'readback.dxf')
    (native/'plan.dwg').write_bytes(b'SYNTHETIC hash fixture, NOT native AutoCAD evidence')
    for name in ['both_on','movable_off','fixed_off','both_off']:
        (native/(name+'.dxf')).write_bytes((native/'readback.dxf').read_bytes())
    files={p.name:sha(p) for p in native.iterdir()};write(native/'capture.json',{'files':files})
    source=root/'source.bin';source.write_bytes(b'Synthetic source east wall faces at 900..1000')
    spec=dict(review_version=VERSION,source_sha256=sha(source),dxf_sha256=files['readback.dxf'],
              dwg_sha256=files['plan.dwg'],wall_layers=['WALL'],required_ids=['east_wall'],
              checks=[dict(id='east_wall',kind='wall_section',axis='x',at_mm=500,search_mm=[800,1300],
                           expected_faces_mm=[900,1000],image_uncertainty_mm=0,source_evidence='Synthetic independent source')])
    ledger=dict(drafting_version='1.1.0',source_sha256=sha(source),wall_layers={'WALL':'fills'},
                nonwall_fill_layers=[],rooms=[{'id':'room'}],openings=[],source_geometry_review_required=True)
    if source_spec:ledger['source_geometry_review']=spec
    write(root/'ledger.json',ledger)
    assessment=dict(dwg_sha256=files['plan.dwg'],reviewer='synthetic regression',paper_observation='Synthetic fixture, not a print observation',issues=[],
      overlay_review=dict(source_sha256=sha(source),dwg_sha256=files['plan.dwg'],observations=dict(junctions='synthetic',furniture='none',uncertainties='none')),
      professional_review=dict(source_rooms=['room'],source_openings=[],opening_reviews={},steps={},rooms=[dict(id='room',faces='synthetic closed room',use='unfurnished',spans=[
        dict(between='west/east inside faces',points_mm=[[100,500],[east,500]],handles=[left,right],mm=east-100),
        dict(between='south/north inside faces',points_mm=[[500,100],[500,900]],handles=[bottom,top],mm=800)])]))
    write(root/'registration.json',dict(grid_registration={'mode':'unmarked'},source_sha256=sha(source)))
    geometry={'DWG_SHA256':files['plan.dwg'],'墙材':'通过','原生改点':'不适用','符号全部存在':True,'逐件尺寸和分层退出码':{'家具块和层':0,'完整尺寸候选':0}}
    write(root/'author-summary.json',dict(dwg_sha256=files['plan.dwg'],geometry=geometry,furniture={'unmeasured':[]}))
    write(root/'review/object-edge-relations.json',dict(relations=[],collisions=[],coverage_issues=[]))
    write(root/'review/furniture.json',dict(door_body_intersections=[],host_pairs_without_overlap=[]))
    write(root/'review/seat-fronts.json',dict(seats=[],coverage_issues=[]))
    proof=root/'review/synthetic.pdf';proof.write_bytes(b'SYNTHETIC hash fixture, NOT a print proof')
    write(root/'review/print-proof.json',dict(file=proof.name,dwg_sha256=files['plan.dwg'],pdf_sha256=sha(proof),paper_audit={'passed':paper}))
    return assessment


class ReleaseGeometryGate(unittest.TestCase):
    def author_geometry(self, root, spec):
        actual=geometry_audit(ezdxf.readfile(root/'review/native/readback.dxf'),spec,
            dxf_sha256=spec['dxf_sha256'],source_sha256=spec['source_sha256'],dwg_sha256=spec['dwg_sha256'])
        summary=json.loads((root/'author-summary.json').read_text())
        summary['source_geometry_review']=actual
        (root/'author-summary.json').write_text(json.dumps(summary))
        return actual

    def run_case(self,**kwargs):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);assessment=fixture(root,**kwargs)
            return review_working_plan.review(root,assessment)

    def test_current_independent_expectation_can_complete_new_job(self):
        self.assertEqual(self.run_case()['status'],'可交设计师接手')

    def test_real_shifted_wall_blocks_whole_plan_release(self):
        result=self.run_case(east=1100)
        self.assertEqual(result['professional_review']['source_geometry_review']['failed'],['east_wall'])
        self.assertNotEqual(result['status'],'可交设计师接手')

    def test_required_source_inventory_cannot_silently_use_legacy(self):
        result=self.run_case(source_spec=False)
        self.assertNotEqual(result['status'],'可交设计师接手')
        self.assertTrue(result['pending_checks'])

    def test_paper_completeness_blocks_even_with_generic_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);assessment=fixture(root,paper=False)
            assessment['measured_exceptions']=[dict(id='纸图/图框图签房名实查未通过',evidence='A generic note must not waive the actual paper gate')]
            result=review_working_plan.review(root,assessment)
            self.assertIn('纸图/图框图签房名实查未通过',result['pending_checks'])
            self.assertNotEqual(result['status'],'可交设计师接手')

    def test_smaller_fresh_scope_cannot_erase_confirmed_east_wall_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);assessment=fixture(root,east=1100)
            ledger=json.loads((root/'ledger.json').read_text())
            spec=ledger['source_geometry_review']
            actual=geometry_audit(ezdxf.readfile(root/'review/native/readback.dxf'),spec,
                dxf_sha256=spec['dxf_sha256'],source_sha256=spec['source_sha256'],dwg_sha256=spec['dwg_sha256'])
            self.assertEqual(actual['failed'],['east_wall'])
            summary=json.loads((root/'author-summary.json').read_text())
            summary['source_geometry_review']=actual
            (root/'author-summary.json').write_text(json.dumps(summary))
            west=dict(spec,required_ids=['west_wall'],checks=[dict(id='west_wall',kind='wall_section',axis='x',at_mm=500,
                search_mm=[-100,200],expected_faces_mm=[0,100],image_uncertainty_mm=0,source_evidence='Independent west wall only')])
            assessment['source_geometry_review']=west
            result=review_working_plan.review(root,assessment)
            self.assertNotEqual(result['status'],'可交设计师接手')
            self.assertIn('源图实体几何/east_wall',result['open_repairs'])
            self.assertTrue(any('范围' in x for x in result['pending_checks']))

    def test_smaller_fresh_scope_cannot_erase_old_pending_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);assessment=fixture(root)
            spec=json.loads((root/'ledger.json').read_text())['source_geometry_review']
            unresolved=dict(spec,checks=[dict(spec['checks'][0],search_mm=[1050,1250],expected_faces_mm=[1100,1200])])
            old=self.author_geometry(root,unresolved)
            self.assertEqual(old['pending'],['east_wall'])
            assessment['source_geometry_review']=dict(spec,required_ids=['west_wall'],checks=[dict(
                id='west_wall',kind='wall_section',axis='x',at_mm=500,search_mm=[-100,200],
                expected_faces_mm=[0,100],image_uncertainty_mm=0,source_evidence='Independent west wall only')])
            result=review_working_plan.review(root,assessment)
            self.assertIn('源图实体几何/east_wall',result['pending_checks'])
            self.assertNotEqual(result['status'],'可交设计师接手')

    def test_current_same_id_explicit_pass_can_replace_old_same_hash_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);assessment=fixture(root)
            spec=json.loads((root/'ledger.json').read_text())['source_geometry_review']
            mistaken=dict(spec,checks=[dict(spec['checks'][0],search_mm=[600,1200],expected_faces_mm=[700,800])])
            old=self.author_geometry(root,mistaken)
            self.assertEqual(old['failed'],['east_wall'])
            result=review_working_plan.review(root,assessment)
            self.assertEqual(result['professional_review']['source_geometry_review']['results']['east_wall']['status'],'通过')
            self.assertEqual(result['status'],'可交设计师接手')

    def test_new_pending_result_does_not_downgrade_a_confirmed_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);assessment=fixture(root,east=1100)
            spec=json.loads((root/'ledger.json').read_text())['source_geometry_review']
            self.assertEqual(self.author_geometry(root,spec)['failed'],['east_wall'])
            assessment['source_geometry_review']=dict(spec,checks=[dict(spec['checks'][0],search_mm=[800,1000])])
            result=review_working_plan.review(root,assessment)
            self.assertIn('源图实体几何/east_wall',result['open_repairs'])
            self.assertIn('源图实体几何/east_wall',result['pending_checks'])

    def test_same_id_with_stale_expected_hash_cannot_replace_known_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);assessment=fixture(root,east=1100)
            spec=json.loads((root/'ledger.json').read_text())['source_geometry_review']
            self.assertEqual(self.author_geometry(root,spec)['failed'],['east_wall'])
            assessment['source_geometry_review']=dict(spec,dxf_sha256='0'*64,
                checks=[dict(spec['checks'][0],expected_faces_mm=[1100,1200])])
            result=review_working_plan.review(root,assessment)
            self.assertIn('源图实体几何/east_wall',result['open_repairs'])
            self.assertIn('源图实体几何/新审查证据版本不符',result['pending_checks'])

    def test_fresh_pass_does_not_waive_an_explicit_unclosed_repair_issue(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);assessment=fixture(root)
            assessment['issues']=[dict(id='源图实体几何/east_wall',kind='repair',location='east wall',
                evidence='Explicit review issue requires the repair/recheck workflow',finding='Unclosed issue')]
            result=review_working_plan.review(root,assessment)
            self.assertIn('源图实体几何/east_wall',result['open_repairs'])
            self.assertNotEqual(result['status'],'可交设计师接手')


if __name__=='__main__':unittest.main()
