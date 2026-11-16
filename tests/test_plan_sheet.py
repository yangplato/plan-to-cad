"""Paper-space contract tests. Synthetic readbacks do not claim native CAD validation."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

import ezdxf
import pymupdf

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from presentation import NUMBERED,PAPER_LAYERS,LEGACY_NUMBERED,LEGACY_PAPER_LAYERS,layer_spaces,roles
from plan_sheet import prepare_sheet,proof,audit_sheet,LAYOUT,SHEET


class PaperSheetTests(unittest.TestCase):
    def test_layer_schema_is_selected_by_saved_job_version(self):
        self.assertEqual(roles({'drafting_version':'1.2.0'}),NUMBERED)
        self.assertEqual(roles({'drafting_version':'1.1.0'}),LEGACY_NUMBERED)
        self.assertEqual(roles({'drafting_version':'1.0.0'}),LEGACY_NUMBERED)
        self.assertTrue(all(name.isascii() for name in [*NUMBERED.values(),*PAPER_LAYERS.values()]))

    def drawing(self,**settings):
        doc=ezdxf.new('R2018');doc.units=4
        for name in NUMBERED.values():doc.layers.add(name)
        wall=doc.modelspace().add_lwpolyline([(0,0),(10000,0),(10000,6000),(0,6000)],
            close=True,dxfattribs={'layer':NUMBERED['bearing']})
        metadata=dict(project='青云书院',unit_type='137㎡ B-1 听雨堂',drawing_title='家具布置平面图',
            revision='V1.1.0',date='2026-11-13',source='营销原图137.jpg',
            limitations='按营销图重建；未现场复核；未标尺寸为推定。')
        options=dict(scale=100,metadata=metadata,room_labels=[dict(name='客餐厅',point_mm=[5000,3000])])
        options.update(settings)
        before=list(wall.get_points())
        report=prepare_sheet(doc,NUMBERED,options)
        self.assertEqual(before,list(wall.get_points()))
        return doc,report

    def capture_fixture(self,root,doc):
        # A fixture for renderer behavior only. Manual native roundtrip is separate.
        (root/'plan.dwg').write_bytes(b'synthetic-native-identity-fixture')
        doc.saveas(root/'readback.dxf')
        files={name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in ('plan.dwg','readback.dxf')}
        (root/'capture.json').write_text(json.dumps({'files':files}))
        return files

    def test_real_paper_entities_and_model_geometry_unchanged(self):
        doc,report=self.drawing()
        paper=doc.layouts.get(LAYOUT)
        self.assertFalse(report['metadata_missing'])
        self.assertTrue(report['paper_audit']['passed'])
        self.assertEqual(report['paper_audit']['room_label_count'],1)
        self.assertGreater(len(paper),20)
        self.assertTrue(any(e.dxftype()=='LWPOLYLINE' and e.closed and e.dxf.layer==PAPER_LAYERS['frame'] for e in paper))
        self.assertEqual(report['room_label_space'],'model')
        self.assertEqual(len(doc.modelspace().query('TEXT')),1)
        self.assertFalse(any(e.dxf.layer in {v for k,v in PAPER_LAYERS.items() if k!='rooms'} for e in doc.modelspace()))
        self.assertFalse(any(e.dxf.layer==PAPER_LAYERS['rooms'] for e in paper))
        vp=next(e for e in paper.query('VIEWPORT') if e.dxf.flags&16384)
        self.assertAlmostEqual(vp.dxf.view_height/vp.dxf.height,100)

    def test_old_three_argument_call_and_wrapped_source(self):
        doc,_=self.drawing()
        old=prepare_sheet(doc,NUMBERED,{})
        self.assertTrue(old['metadata_missing'])
        self.assertFalse(old['paper_audit']['passed'])
        self.assertEqual(old['paper_audit']['status'],'incomplete')
        self.assertEqual(old['paper_audit']['expected_room_names'],[])
        long='按用户提供的原营销平面图重建；图像中的未标构造和遮挡处保留推定；'*2
        doc,report=self.drawing(metadata=dict(project='青云',unit_type='137㎡',drawing_title='平面图',
            revision='V1.1.0',date='2026-11-13',source=long,limitations='未现场复核。'))
        self.assertGreater(report['paper_audit']['tagged_entity_kinds']['source'],1)
        self.assertTrue(audit_sheet(doc)['passed'])

    def test_proof_uses_true_paperspace_at_fixed_scale_and_preserves_inputs(self):
        doc,_=self.drawing()
        vp=next(e for e in doc.layouts.get(LAYOUT).query('VIEWPORT') if e.dxf.flags&16384)
        vp.dxf.id=0;vp.dxf.status=0
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);before=self.capture_fixture(root,doc)
            result=proof(root,root/'proof.pdf')
            self.assertEqual(result['rendered_scope'],'actual readback paperspace and saved locked viewport')
            self.assertEqual(result['scale'],100)
            self.assertEqual(result['temporary_viewport_activation']['original_status'],0)
            with pymupdf.open(root/'proof.pdf') as pdf:
                self.assertEqual(len(pdf),1);page=pdf[0]
                self.assertEqual(page.get_images(),[])
                for got,want in zip((page.rect.width*25.4/72,page.rect.height*25.4/72),SHEET):
                    self.assertAlmostEqual(got,want,places=3)
                boxes=[(x['rect'].width*25.4/72,x['rect'].height*25.4/72) for x in page.get_drawings()]
                self.assertTrue(any(abs(w-100)<.06 and abs(h-60)<.06 for w,h in boxes),boxes[:10])
                self.assertTrue(any(abs(w-400)<.06 and abs(h-277)<.06 for w,h in boxes))
            self.assertEqual(before,{n:hashlib.sha256((root/n).read_bytes()).hexdigest() for n in before})

    def test_actual_room_text_and_metadata_control_completeness(self):
        doc,report=self.drawing(room_labels=[dict(name='客餐厅',point_mm=[5000,3000],anchor_mm=[4000,4000])])
        self.assertTrue(report['paper_audit']['passed'])
        self.assertEqual(report['paper_audit']['expected_room_names'],['客餐厅'])
        paper=doc.layouts.get(LAYOUT)
        self.assertEqual(report['paper_audit']['tagged_entity_kinds']['room_leader'],1)
        room=next(e for e in doc.modelspace().query('TEXT') if e.dxf.text=='客餐厅')
        room.dxf.text='改名后未登记'
        self.assertFalse(audit_sheet(doc)['passed'])
        self.assertIn('必要房名应有一个实际TEXT：客餐厅',audit_sheet(doc)['completeness_issues'])
        doc,_=self.drawing()
        paper=doc.layouts.get(LAYOUT)
        project=next(e for e in paper.query('TEXT') if e.dxf.text.startswith('项目：'))
        project.dxf.text='项目：项目未提供'
        self.assertFalse(audit_sheet(doc)['passed'])
        doc,_=self.drawing();prepare_sheet(doc,NUMBERED,{})
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);self.capture_fixture(root,doc)
            candidate=proof(root,root/'candidate.pdf')
            self.assertTrue((root/'candidate.pdf').is_file())
            self.assertFalse(candidate['paper_audit']['passed'])
            self.assertEqual(candidate['paper_audit']['status'],'incomplete')

    def test_legend_lists_only_wall_categories_present_in_model(self):
        doc,report=self.drawing()
        self.assertEqual([row['role'] for row in report['legend']],['bearing'])
        self.assertEqual(report['paper_audit']['actual_wall_roles'],['bearing'])
        self.assertNotIn('legend_partition',report['paper_audit']['tagged_entity_kinds'])
        self.assertTrue(audit_sheet(doc)['passed'])
        doc.modelspace().add_line((100,100),(100,300),dxfattribs={'layer':NUMBERED['partition']})
        self.assertIn('纸空间缺少实际采用墙类图例：partition',audit_sheet(doc)['structural_issues'])

    def test_room_width_factor_defaults_override_and_survives_save(self):
        doc,report=self.drawing()
        self.assertEqual(report['room_labels'][0]['width_factor'],1.)
        doc,report=self.drawing(room_label_width_factor=.7,room_labels=[
            dict(name='公卫',point_mm=[3000,3000]),
            dict(name='主卫',point_mm=[6500,3000],width_factor=.85)])
        self.assertEqual([r['width_factor'] for r in report['room_labels']],[.7,.85])
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'width.dxf';doc.saveas(path)
            readback=ezdxf.readfile(path)
            for row in report['room_labels']:
                entity=readback.entitydb[row['handle']]
                self.assertEqual(entity.dxftype(),'TEXT')
                self.assertEqual(entity.dxf.text,row['name'])
                self.assertAlmostEqual(entity.dxf.width,row['width_factor'])
                self.assertEqual(entity.dxf.height,350)
                self.assertEqual(entity.dxf.color,256)
                self.assertEqual(readback.layers.get(entity.dxf.layer).dxf.color,4)

    def test_room_width_factor_rejects_out_of_range_or_nonfinite(self):
        for value in (.59,1.01,float('nan'),float('inf'),True,None,'wide'):
            with self.subTest(scope='global',value=value),self.assertRaisesRegex(ValueError,'width_factor'):
                self.drawing(room_label_width_factor=value)
            with self.subTest(scope='item',value=value),self.assertRaisesRegex(ValueError,'width_factor'):
                self.drawing(room_labels=[dict(name='公卫',point_mm=[5000,3000],width_factor=value)])

    def test_missing_frame_and_model_layer_leak_fail(self):
        doc,_=self.drawing();paper=doc.layouts.get(LAYOUT)
        frame=next(e for e in paper if e.dxf.layer==PAPER_LAYERS['frame'])
        paper.delete_entity(frame)
        self.assertFalse(audit_sheet(doc)['passed'])
        doc,_=self.drawing();doc.modelspace().add_line((1,1),(2,2),dxfattribs={'layer':PAPER_LAYERS['title']})
        self.assertFalse(audit_sheet(doc)['passed'])

    def test_viewport_clipping_is_rejected(self):
        doc,_=self.drawing();vp=next(e for e in doc.layouts.get(LAYOUT).query('VIEWPORT') if e.dxf.flags&16384)
        vp.dxf.view_center_point=(1e8,1e8)
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);self.capture_fixture(root,doc)
            with self.assertRaisesRegex(ValueError,'clips the model'):proof(root,root/'proof.pdf')

    def test_model_names_visibility_empty_layers_and_legacy_paper_scope(self):
        doc,report=self.drawing()
        self.assertTrue(all(not row['empty'] for row in layer_spaces(doc) if not row['system']))
        room=doc.entitydb[report['room_labels'][0]['handle']]
        self.assertEqual(room.dxf.owner,doc.modelspace().layout_key)
        self.assertEqual(room.dxf.layer,PAPER_LAYERS['rooms'])
        doc.layers.get(PAPER_LAYERS['rooms']).off()
        self.assertFalse(audit_sheet(doc)['passed'])
        doc.layers.get(PAPER_LAYERS['rooms']).on()
        vp=next(e for e in doc.layouts.get(LAYOUT).query('VIEWPORT') if e.dxf.flags&16384)
        vp.frozen_layers=[PAPER_LAYERS['rooms']]
        self.assertFalse(audit_sheet(doc)['passed'])
        vp.frozen_layers=[];doc.layers.add('ActuallyEmpty')
        self.assertIn('空自定义图层：ActuallyEmpty',audit_sheet(doc)['issues'])
        old=ezdxf.new('R2018');old.units=4
        for name in LEGACY_NUMBERED.values():old.layers.add(name)
        old.modelspace().add_line((0,0),(10000,6000),dxfattribs={'layer':LEGACY_NUMBERED['bearing']})
        old_report=prepare_sheet(old,LEGACY_NUMBERED,dict(room_labels=[dict(name='客餐厅',point_mm=[5000,3000])]))
        old_room=old.entitydb[old_report['room_labels'][0]['handle']]
        self.assertEqual(old_report['room_label_space'],'paper')
        self.assertEqual(old_room.dxf.layer,LEGACY_PAPER_LAYERS['rooms'])
        self.assertEqual(old_room.dxf.height,3.5)
        self.assertEqual(old.layers.get(LEGACY_NUMBERED['dimension']).dxf.color,7)
        self.assertFalse(any(name in old.layers for name in NUMBERED.values()))

    def test_new_dimensions_inherit_cyan_layer_and_print_black(self):
        doc,_=self.drawing()
        doc.layers.add(NUMBERED['dimension'],color=7)
        doc.dimstyles.new('PLAN-MM',dxfattribs={'dimclrd':7,'dimclre':7,'dimclrt':7})
        dim=doc.modelspace().add_linear_dim(base=(0,1000),p1=(0,0),p2=(4000,0),
            dimstyle='PLAN-MM',dxfattribs={'layer':NUMBERED['dimension'],'color':7})
        dim.render()
        prepare_sheet(doc,NUMBERED,dict(room_labels=[dict(name='客餐厅',point_mm=[5000,3000])]))
        actual=dim.dimension
        self.assertEqual(doc.layers.get(NUMBERED['dimension']).dxf.color,4)
        self.assertEqual(actual.dxf.color,256)
        self.assertTrue(all(actual.override().get(key)==256 for key in ('dimclrd','dimclre','dimclrt')))
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);self.capture_fixture(root,doc);proof(root,root/'proof.pdf')
            with pymupdf.open(root/'proof.pdf') as pdf:
                self.assertTrue(all(item['color'] in (None,(0.,0.,0.)) for item in pdf[0].get_drawings()))

    def test_hidden_or_white_paper_annotation_is_not_complete(self):
        doc,_=self.drawing()
        doc.layers.get(PAPER_LAYERS['title']).off()
        self.assertFalse(audit_sheet(doc)['passed'])
        doc.layers.get(PAPER_LAYERS['title']).on()
        title=next(e for e in doc.layouts.get(LAYOUT).query('TEXT') if e.dxf.layer==PAPER_LAYERS['title'])
        title.dxf.color=7
        self.assertFalse(audit_sheet(doc)['passed'])


if __name__=='__main__':unittest.main()
