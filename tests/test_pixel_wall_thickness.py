"""Current wall selection through calibrated source pixels and saved geometry."""
from copy import deepcopy
from pathlib import Path
import sys,tempfile,unittest
import ezdxf

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from dimension_grid import GridProjection,prepare_plan
from wall_thickness import resolve_width,records,POLICY
from build_observed_structure import create_structure
from check_wall_probes import hatch_area
from test_wall_thickness import wall,scene,grid_scene,SOURCE


def evidence(points,uncertainty=.1,**changes):
    return dict(dict(kind='calibrated_pixels',face_points_px=points,
        edge_uncertainty_px=uncertainty,source_sha256=SOURCE,locator='source section W1',
        calibration_basis='printed chain controls X0-X1000'),**changes)


class PixelWallThicknessTests(unittest.TestCase):
    def test_all_four_sizes_are_selected_without_thick_thin_defaults(self):
        projection=GridProjection({'source_to_mm':[10,0,0,-10,0,0]})
        for width in (240,200,120,100):
            row=wall(width_mm=240,thickness_evidence=evidence([[0,500],[width/10,500]]))
            self.assertEqual(resolve_width(row,SOURCE,projection=projection),width)

    def test_wall_colour_and_class_do_not_force_the_choice(self):
        row=wall(wall_class='main',material='bearing',thickness_evidence=evidence([[0,500],[100,500]]))
        self.assertEqual(resolve_width(row,SOURCE,projection=GridProjection(scene())),100)

    def test_piecewise_local_scale_is_used_instead_of_affine_average(self):
        projection=GridProjection(dict(source_to_mm=[20,0,0,-10,0,0],
            grid_registration={'axes':{'x':[[0,0],[100,2000],[300,4000]],'y':[[0,0],[100,-1000]]}}))
        row=wall(a=[200,0],b=[200,100],thickness_evidence=evidence([[190,50],[210,50]]))
        self.assertEqual(resolve_width(row,SOURCE,projection=projection),200)

    def test_horizontal_wall_uses_vertical_calibration(self):
        projection=GridProjection({'source_to_mm':[20,0,0,-10,0,0]})
        row=wall(a=[0,100],b=[200,100],thickness_evidence=evidence([[50,95],[50,105]]))
        self.assertEqual(resolve_width(row,SOURCE,projection=projection),100)

    def test_missing_or_wrong_source_observation_is_not_a_default(self):
        for ev in (None,{},evidence([[0,0],[200,0]],source_sha256='b'*64),
                   evidence([[0,0],[200,0]],source_sha256=7),
                   evidence([[0,0],[200,0]],calibration_basis='')):
            with self.subTest(ev=ev),self.assertRaises(ValueError):
                resolve_width(wall(thickness_evidence=ev),SOURCE,projection=GridProjection(scene()))

    def test_ambiguous_pixels_keep_candidates_and_require_other_constraints(self):
        base=scene(drafting_version='1.3.0')
        row=wall(width_mm=120,thickness_evidence=evidence([[0,500],[110,500]],10))
        with self.assertRaisesRegex(ValueError,'ambiguous'):
            resolve_width(row,SOURCE,projection=GridProjection(base))
        row['thickness_evidence']['selection_basis']='same face as the clear 120mm adjoining wall; room span checked separately'
        base['walls']=[row]
        result,_=prepare_plan(base,[])
        record=records(result)['west']
        self.assertEqual(record['candidates_mm'],[100,120])
        self.assertEqual(record['selected_mm'],120)

    def test_no_matching_size_does_not_silently_snap_to_240(self):
        row=wall(thickness_evidence=evidence([[0,500],[220,500]],1))
        with self.assertRaisesRegex(ValueError,'no 100/120/200/240'):
            resolve_width(row,SOURCE,projection=GridProjection(scene()))

    def test_explicit_thickness_still_has_priority(self):
        row=wall(width_mm=220,thickness_evidence=dict(kind='printed_dimension',value_mm=220,
                    source_sha256=SOURCE,locator='wall thickness dimension 220'))
        self.assertEqual(resolve_width(row,SOURCE),220)

    def test_same_section_and_real_pixel_uncertainty_are_required(self):
        for ev in (evidence([[0,0],[200,1]]),evidence([[0,0],[0,0]]),
                   evidence([[0,0],[200,0]],-1),evidence([[0,0],[200,0]],float('nan'))):
            with self.subTest(ev=ev),self.assertRaises(ValueError):
                resolve_width(wall(thickness_evidence=ev),SOURCE,projection=GridProjection(scene()))

    def test_actual_200_wall_faces_and_opening_remain_200_after_save(self):
        base=grid_scene();base['drafting_version']='1.3.0'
        base['walls'][0].update(width_mm=240,thickness_evidence=evidence([[0,500],[200,500]]))
        doc,ledger=create_structure(base)
        self.assertEqual(ledger['wall_thickness_policy'],POLICY)
        self.assertEqual(ledger['openings'][0]['wall_thickness_mm'],200)
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'actual.dxf';doc.saveas(path)
            areas=[hatch_area(h) for h in ezdxf.readfile(path).modelspace().query('HATCH')]
            self.assertEqual(min(p.bounds[0] for p in areas),0)
            self.assertEqual(max(p.bounds[2] for p in areas),200)
            self.assertAlmostEqual(sum(p.area for p in areas),200*(2000-900))

    def test_current_preparation_is_idempotent_and_rejects_stale_evidence(self):
        base=scene(drafting_version='1.3.0')
        base['walls'][0]['thickness_evidence']=evidence([[0,500],[200,500]])
        first,_=prepare_plan(base,[]);second,_=prepare_plan(first,[])
        self.assertEqual(first,second)
        broken=deepcopy(first);broken['walls'][0]['thickness_evidence']['face_points_px'][1][0]=240
        with self.assertRaisesRegex(ValueError,'stale'):
            prepare_plan(broken,[])

    def test_uncalibrated_axis_and_circular_wall_scale_are_rejected(self):
        base=grid_scene();base['drafting_version']='1.3.0'
        base['walls'][0].update(a=[0,100],b=[2000,100],thickness_evidence=evidence([[500,0],[500,200]]))
        with self.assertRaisesRegex(ValueError,'no independently calibrated'):
            prepare_plan(base,[])
        base=scene(drafting_version='1.3.0')
        base['walls'][0]['thickness_evidence']=evidence([[0,500],[200,500]])
        base['grid_registration']=dict(axes={'x':[[0,0],[1000,1000]],'y':[[0,0],[1000,-1000]]},
            scale_provenance={'kind':'assumed_wall_thickness'})
        with self.assertRaisesRegex(ValueError,'assumed wall thickness'):
            prepare_plan(base,[])


if __name__=='__main__':unittest.main()
