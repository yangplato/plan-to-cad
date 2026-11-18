"""Wall-default policy and preserved-reference tests; no native CAD claim."""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest

import ezdxf

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from functools import partial
from wall_thickness import resolve_width as _resolve_width, records, LEGACY_POLICY as POLICY
resolve_width=partial(_resolve_width,drafting_version='1.2.0')
from dimension_grid import prepare_plan, linear
from build_observed_structure import create_structure
from check_wall_probes import hatch_area

SOURCE = 'a' * 64


def wall(**changes):
    row = dict(id='west', a=[100, 0], b=[100, 2000], width_mm=200,
               wall_class='main', material='bearing', thickness_reference='min_face')
    return dict(row, **changes)


def scene(**changes):
    row = dict(drafting_version='1.2.0', source_sha256=SOURCE,
               source_to_mm=[1, 0, 0, -1, 0, 0], walls=[wall()],
               openings=[dict(id='door', kind='open', a=[100, 500], b=[100, 1400],
                              hosts=['west'], connects=['outside', 'room'])],
               rooms=[dict(id='room', free_points=[[500, 1000]])],
               dimension_chains=[dict(id='overall', axis='x', ticks_px=[0, 1000],
                                     values_mm=[1000], witness_px=0, line_px=-200)])
    return dict(row, **changes)


def grid_scene(at='min_face'):
    base = scene()
    base['walls'][0].pop('thickness_reference')
    px = {'min_face': 0, 'center': 100, 'max_face': 200}[at]
    base['dimension_grid'] = dict(mode='overall', controls={
        'fixed': dict(axis='x', kind='wall_center' if at == 'center' else 'wall_face',
                      px=px, evidence='source tick', bindings=[dict(wall='west', at=at)]),
        'far': dict(axis='x', kind='datum', px=px+1000, evidence='marketing datum')})
    base['dimension_chains'] = [dict(id='overall', axis='x', controls=['fixed', 'far'],
                                    values_mm=[1000], witness_px=0, line_px=-200)]
    return base


class WallThicknessTests(unittest.TestCase):
    def test_defaults_ignore_previous_inferred_width_and_fill_missing_width(self):
        self.assertEqual(resolve_width(wall()), 240)
        self.assertEqual(resolve_width(wall(wall_class='partition')), 120)
        row = wall(); row.pop('width_mm')
        self.assertEqual(resolve_width(row), 240)

    def test_drawing_class_is_required_and_does_not_certify_structure(self):
        row = wall(); row.pop('wall_class')
        with self.assertRaisesRegex(ValueError, 'wall_class'):
            resolve_width(row)
        self.assertEqual(resolve_width(wall(material='partition')), 240)

    def test_explicit_image_thickness_requires_value_hash_and_locator(self):
        evidence = dict(kind='printed_dimension', value_mm=200, source_sha256=SOURCE,
                        locator='original image, wall thickness 200 at upper left')
        self.assertEqual(resolve_width(wall(thickness_evidence=evidence), SOURCE), 200)
        for change in (dict(value_mm=220), dict(value_mm=float('nan')), dict(locator=''),
                       dict(source_sha256='b'*64), dict(source_sha256='missing'),
                       dict(kind='pixel_estimate'), dict(kind='model_value')):
            with self.subTest(change=change), self.assertRaises(ValueError):
                resolve_width(wall(thickness_evidence=dict(evidence, **change)), SOURCE)
        with self.assertRaises(ValueError):
            resolve_width(wall(thickness_evidence=evidence))

    def test_document_exception_has_its_own_hash_and_traceable_reference(self):
        evidence = dict(kind='building_document', value_mm=200, source_sha256='b'*64,
                        locator='sheet A-02 / wall W7 / thickness dimension', source_ref='building.pdf')
        self.assertEqual(resolve_width(wall(thickness_evidence=evidence), SOURCE), 200)
        evidence.pop('source_ref')
        with self.assertRaisesRegex(ValueError, 'source_ref'):
            resolve_width(wall(thickness_evidence=evidence), SOURCE)

    def test_columns_remain_separate_and_bad_numbers_fail(self):
        self.assertEqual(resolve_width(dict(id='column', element_kind='column', width_mm=425)), 425)
        for value in (True, 0, -120, float('inf'), float('nan'), '240'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                resolve_width(wall(width_mm=value))
        with self.assertRaises(ValueError):
            resolve_width(wall(element_kind='frame'))

    def test_old_frozen_scene_is_not_changed(self):
        for version in ('1.1.0', '1.0.2', '0.15'):
            original = scene(drafting_version=version)
            original['walls'][0].pop('wall_class')
            result, objects = prepare_plan(original, [])
            self.assertIs(result, original)
            self.assertEqual(result['walls'][0]['width_mm'], 200)
            self.assertFalse(records(result))

    def test_older_grid_preparation_remains_compatible(self):
        original = grid_scene(); original['drafting_version'] = '1.1.0'
        original['walls'][0].pop('wall_class')
        result, _ = prepare_plan(original, [])
        self.assertEqual(result['walls'][0]['a'][0], 100)
        self.assertEqual(result['walls'][0]['width_mm'], 200)

    def test_changed_width_requires_a_fixed_reference(self):
        original = scene(); original['walls'][0].pop('thickness_reference')
        with self.assertRaisesRegex(ValueError, 'thickness_reference'):
            prepare_plan(original, [])

    def test_fixed_faces_and_center_in_affine_working_frame(self):
        for reference, center in (('min_face', 120), ('center', 100), ('max_face', 80)):
            original = scene(); original['walls'][0]['thickness_reference'] = reference
            before = deepcopy(original)
            result, _ = prepare_plan(original, [])
            self.assertEqual(result['walls'][0]['a'][0], center)
            self.assertEqual(result['walls'][0]['width_mm'], 240)
            self.assertEqual(result['openings'][0]['a'][0], center)
            self.assertEqual(original, before)

    def test_horizontal_face_sign_uses_world_coordinates(self):
        base = scene(walls=[wall(a=[0, 100], b=[2000, 100])], openings=[])
        result, _ = prepare_plan(base, [])
        center = linear(result['source_to_mm'], result['walls'][0]['a'])[1]
        self.assertEqual(center, -80)  # old min face -200, new min face -200
        self.assertEqual(center-120, -200)

    def test_named_control_keeps_its_station_with_new_width(self):
        for at, center in (('min_face', 120), ('center', 100), ('max_face', 80)):
            result, _ = prepare_plan(grid_scene(at), [])
            self.assertAlmostEqual(result['walls'][0]['a'][0], center)
            self.assertEqual(result['walls'][0]['width_mm'], 240)
            self.assertEqual(result['wall_thickness_policy'], POLICY)
            self.assertEqual(records(result)['west']['fixed_reference'], 'bound_controls')
            self.assertTrue(records(result)['west']['applied'])

    def test_shared_corner_and_hosted_opening_follow_preserved_face(self):
        base = scene()
        base['walls'].append(wall(id='south', a=[100, 2000], b=[1500, 2000],
                                  width_mm=100, wall_class='partition', thickness_reference='center'))
        result, _ = prepare_plan(base, [])
        self.assertEqual(result['walls'][0]['b'], [120, 2000])
        self.assertEqual(result['walls'][1]['a'], [120, 2000])
        self.assertEqual(result['walls'][1]['width_mm'], 120)
        self.assertEqual(result['openings'][0]['a'], [120, 500])

    def test_conflicting_connected_preserved_faces_are_rejected(self):
        base = scene()
        base['walls'].append(wall(id='west2', a=[100, 2000], b=[100, 3000],
                                  thickness_reference='max_face'))
        with self.assertRaisesRegex(ValueError, 'conflicting'):
            prepare_plan(base, [])

    def test_two_bound_faces_cannot_hold_an_obsolete_inferred_width(self):
        base = grid_scene()
        base['dimension_grid']['controls']['far'].update(
            px=200, kind='wall_face', bindings=[dict(wall='west', at='max_face')])
        base['dimension_chains'][0]['values_mm'] = [200]
        with self.assertRaisesRegex(ValueError, 'conflicting'):
            prepare_plan(base, [])

    def test_prepared_scene_is_idempotent_with_and_without_dimension_grid(self):
        for original in (scene(), grid_scene()):
            first, _ = prepare_plan(original, [])
            second, _ = prepare_plan(first, [])
            self.assertEqual(second, first)
            second['walls'][0]['width_mm'] = 200
            with self.assertRaisesRegex(ValueError, 'stale'):
                prepare_plan(second, [])

    def test_prepared_record_requires_valid_class_width_and_applied_state(self):
        prepared, _ = prepare_plan(scene(), [])
        for field, value in (('applied', 'true'), ('selected_mm', '240'),
                             ('selected_mm', float('nan')), ('wall_class', 'partition')):
            broken = deepcopy(prepared)
            broken['walls'][0]['wall_thickness_review'][field] = value
            with self.subTest(field=field, value=value), self.assertRaisesRegex(ValueError, 'stale'):
                prepare_plan(broken, [])

    def test_recommended_smoke_job_creates_current_candidate_without_fake_review(self):
        import importlib.util
        path = Path(__file__).resolve().parents[1]/'examples/smoke_job.py'
        spec = importlib.util.spec_from_file_location('wall_policy_smoke_job', path)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)/'candidate'; module.make_job(root)
            import json
            candidate = json.loads((root/'scene.json').read_text())
            result, _ = prepare_plan(candidate, [])
            from drafting_version import RELEASE
            self.assertEqual(candidate['drafting_version'], RELEASE)
            self.assertEqual({w['id']: w['width_mm'] for w in result['walls']},
                             dict(north=200, west=200, east=200, south=200, divider=120, column=200))
            self.assertNotIn('source_geometry_review', candidate)
            from check_cad_logic import inspect
            doc, _ = create_structure(candidate)
            report = inspect(doc)
            self.assertEqual(report['coverage_issues'], [])
            self.assertFalse([r for r in report['findings'] if r['check']=='门轴缺支承'])

    def test_saved_dxf_uses_actual_default_faces_and_updated_opening_depth(self):
        doc, ledger = create_structure(grid_scene())
        self.assertEqual(ledger['wall_thickness_policy'], POLICY)
        self.assertEqual(ledger['walls'][0]['width_mm'], 240)
        self.assertEqual(ledger['openings'][0]['wall_thickness_mm'], 240)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'actual-walls.dxf'; doc.saveas(path)
            readback = ezdxf.readfile(path)
            hatches = list(readback.modelspace().query('HATCH'))
            self.assertTrue(hatches)
            shapes = [hatch_area(h) for h in hatches]
            self.assertEqual(min(p.bounds[0] for p in shapes), 0)
            self.assertEqual(max(p.bounds[2] for p in shapes), 240)
            self.assertAlmostEqual(sum(p.area for p in shapes), 240*(2000-900))


if __name__ == '__main__':
    unittest.main()
