"""Regression for POLYLINE end-marker layers reappearing during native save."""
from pathlib import Path
import sys,tempfile,unittest
import ezdxf
from ezdxf.math import Vec2

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import interior_block_library as library


class LibraryInheritance(unittest.TestCase):
    def test_saved_import_keeps_polyline_vertices_and_end_marker_on_zero(self):
        src=library.library(library.BUNDLED)
        candidates=[]
        for name in library.index():
            polys=[e for e in src.blocks[name] if e.dxftype()=='POLYLINE' and e.dxf.layer!=library.GUIDES]
            if polys:candidates.append((any(e.seqend is not None and e.seqend.dxf.layer!='0' for e in polys),name))
        if not candidates:self.skipTest('No legacy POLYLINE geometry in this library version')
        name=sorted(candidates,reverse=True)[0][1]
        doc=ezdxf.new();doc.units=4;doc.layers.new('MOVABLE')
        row=dict(library_block=name,library_front_vector=[0,-1],width_mm=1500,depth_mm=1900)
        ref=library.place_bundled(doc,row,Vec2(0,0),Vec2(0,-1),'MOVABLE')
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/'saved.dxf';doc.saveas(p);read=ezdxf.readfile(p)
            polys=[e for e in read.blocks[ref.dxf.name] if e.dxftype()=='POLYLINE']
            self.assertTrue(polys)
            for poly in polys:
                self.assertEqual(poly.dxf.layer,'0')
                self.assertTrue(all(v.dxf.layer=='0' for v in poly.vertices))
                self.assertIsNotNone(poly.seqend)
                self.assertEqual(poly.seqend.dxf.layer,'0')


if __name__=='__main__':unittest.main()
