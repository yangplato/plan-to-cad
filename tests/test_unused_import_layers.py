"""Keep case-insensitive DXF layer references when pruning new imported plans."""
from pathlib import Path
import sys
import tempfile
import unittest

import ezdxf

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from build_plan_objects import prune_unused_import_layers
from presentation import NUMBERED, PAPER_LAYERS,LEGACY_NUMBERED,LEGACY_PAPER_LAYERS


def saved_dxf(doc):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'pruned.dxf'
        doc.saveas(path)
        return ezdxf.readfile(path)


class UnusedImportLayers(unittest.TestCase):
    def test_saved_model_and_nested_block_keep_used_layer_properties(self):
        doc = ezdxf.new('R2018')
        doc.layers.new('Imported-Model', dxfattribs={'color': 3, 'lineweight': 35})
        doc.layers.new('Imported-Nested', dxfattribs={'color': 5, 'lineweight': 18})
        doc.layers.new('Unused-OriginalCase')
        line = doc.modelspace().add_line((0, 0), (100, 0),
            dxfattribs={'layer': 'imported-model'})
        block = doc.blocks.new('IMPORTED-BLOCK')
        block.add_lwpolyline([(0, 0), (20, 0), (20, 30)],
            dxfattribs={'layer': 'IMPORTED-NESTED'})
        ref = doc.modelspace().add_blockref(block.name, (200, 0))

        self.assertEqual(prune_unused_import_layers(doc, NUMBERED), ['Unused-OriginalCase'])
        read = saved_dxf(doc)
        actual_line = read.entitydb[line.dxf.handle]
        self.assertEqual(actual_line.dxf.layer, 'imported-model')
        model_layer = read.layers.get(actual_line.dxf.layer)
        self.assertEqual((model_layer.dxf.name, model_layer.dxf.color, model_layer.dxf.lineweight),
                         ('Imported-Model', 3, 35))
        actual_ref = read.entitydb[ref.dxf.handle]
        actual_member = next(iter(read.blocks[actual_ref.dxf.name]))
        nested_layer = read.layers.get(actual_member.dxf.layer)
        self.assertEqual((nested_layer.dxf.name, nested_layer.dxf.color, nested_layer.dxf.lineweight),
                         ('Imported-Nested', 5, 18))
        self.assertNotIn('Unused-OriginalCase', read.layers)

    def test_saved_viewport_keeps_a_layer_referenced_only_by_freeze(self):
        doc = ezdxf.new('R2018')
        doc.layers.new('Imported-Frozen', dxfattribs={'color': 6})
        vp = doc.layouts.get('Layout1').add_viewport(center=(20, 20), size=(30, 30),
            view_center_point=(0, 0), view_height=100)
        vp.frozen_layers = ['IMPORTED-frozen']

        self.assertEqual(prune_unused_import_layers(doc, NUMBERED), [])
        read = saved_dxf(doc)
        actual_vp = read.entitydb[vp.dxf.handle]
        self.assertEqual(len(actual_vp.frozen_layers), 1)
        actual_layer = read.layers.get(actual_vp.frozen_layers[0])
        self.assertEqual((actual_layer.dxf.name, actual_layer.dxf.color), ('Imported-Frozen', 6))

    def test_saved_allowed_layers_are_kept_without_geometry_regardless_of_case(self):
        doc = ezdxf.new('R2018')
        roles = dict(LEGACY_NUMBERED, ancillary='Business-Custom')
        doc.layers.new('BUSINESS-CUSTOM', dxfattribs={'color': 4})
        paper_name = LEGACY_PAPER_LAYERS['frame'].lower()
        doc.layers.new(paper_name)
        doc.layers.new('viewports')
        doc.layers.new('Actually-Unused')

        self.assertEqual(prune_unused_import_layers(doc, roles), ['Actually-Unused'])
        read = saved_dxf(doc)
        for name in ('0', 'Defpoints', 'Business-Custom', LEGACY_PAPER_LAYERS['frame'], 'VIEWPORTS'):
            self.assertIn(name, read.layers)
        self.assertEqual(read.layers.get('Business-Custom').dxf.color, 4)

    def test_new_schema_drops_empty_application_layers_but_retains_system(self):
        doc=ezdxf.new('R2018')
        for name in NUMBERED.values():doc.layers.add(name)
        doc.layers.add('VIEWPORTS')
        doc.modelspace().add_line((0,0),(20,0),dxfattribs={'layer':NUMBERED['bearing']})
        removed=prune_unused_import_layers(doc,NUMBERED)
        self.assertEqual(set(removed),set(NUMBERED.values())-{NUMBERED['bearing']})
        self.assertIn('VIEWPORTS',doc.layers)


if __name__ == '__main__':
    unittest.main()
