"""Merge reviewed DXF readbacks at 1:1. CLI: panels.json combined.dxf.

The panel list contains name, readback and offset_mm [dx, dy] in left-to-right order.
Input paths are relative to the JSON file. The CAD layers are shared. Named blocks,
linetypes and styles are isolated for each panel. No external drawing is attached.
"""
import argparse
import json
from pathlib import Path

import ezdxf
from ezdxf import bbox, xref
from ezdxf.enums import TextEntityAlignment
from ezdxf.math import Vec3

from presentation import CURRENT,LEGACY_NUMBERED,LEGACY,PAPER_LAYERS
LABEL_LAYER = 'A-ANNO-TEXT'
CATEGORIES = tuple(dict.fromkeys([*CURRENT.values(),*LEGACY_NUMBERED.values(),*LEGACY.values(),PAPER_LAYERS['rooms']]))
SYSTEM = {'0', 'defpoints'}
TRACE = 'PLANMERGE_SOURCE'


def merge(panels, base, output):
    if not panels or output.exists():
        raise ValueError('Supply nonempty panels and a new output file')
    doc = ezdxf.new('R2018', units=4)
    doc.layers.new(LABEL_LAYER, dxfattribs={'color': 4})
    labels = []
    report = []
    for i, panel in enumerate(panels, 1):
        source = (base / panel['readback']).resolve()
        src = ezdxf.readfile(source)
        if src.units != 4 or any(b.block.is_xref or b.block.is_xref_overlay for b in src.blocks):
            raise ValueError(f'{source}: expected a millimetre drawing with internal block definitions')
        if any(e.dxftype()=='IMAGE' for e in src.modelspace()):
            raise ValueError('Attach marketing rasters once the comparison drawing has its final destination')
        delta = Vec3(*panel['offset_mm'])
        for layer in src.layers:
            name = layer.dxf.name
            if name.casefold() in SYSTEM:
                continue
            if name not in CATEGORIES:
                if any(e.dxf.layer==name for e in src.modelspace()):
                    raise ValueError(f'{source}: unmapped layer {name}')
                continue
            attrs = {k:v for k,v in layer.dxfattribs().items() if k in ('color','true_color','lineweight','plot','flags')}
            if name not in doc.layers:
                doc.layers.new(name, dxfattribs=attrs)
            else:
                for k,v in attrs.items():
                    if getattr(doc.layers.get(name).dxf,k)!=v:
                        raise ValueError(f'Conflicting {name} layer attribute {k}')
        if TRACE in src.appids:
            raise ValueError(f'Source already uses reserved marker {TRACE}')
        src.appids.new(TRACE)
        for entity in src.modelspace():
            entity.set_xdata(TRACE, [(1000,entity.dxf.handle)])
        prefix = f'P{i}'
        loader = xref.Loader(src,doc,conflict_policy=xref.ConflictPolicy.XREF_PREFIX)
        loader.load_modelspace()
        loader.execute(prefix)
        mapping = {}
        for entity in doc.modelspace():
            if not entity.has_xdata(TRACE):
                continue
            handle = entity.get_xdata(TRACE)[0].value
            entity.discard_xdata(TRACE)
            mapping[handle] = entity
            entity.translate(*delta)
            if entity.dxftype()=='HATCH':
                for path in entity.paths:
                    for h in path.source_boundary_objects:
                        doc.entitydb[h].append_reactor_handle(entity.dxf.handle)
        if len(mapping)!=len(src.modelspace()):
            raise ValueError('Incomplete model-space transfer')
        for j,(_,group) in enumerate(src.groups):
            if len(group) and all(e.dxf.handle in mapping for e in group):
                doc.groups.new(name=f'{prefix}_G{j+1}').set_data([mapping[e.dxf.handle] for e in group])
        # Imported reference names must not remain visible in the layer manager.
        imported = {}
        for layer in list(doc.layers):
            name = layer.dxf.name
            if name.startswith(prefix+'$'):
                orig = name.split('$', 2)[-1]
                if orig not in CATEGORIES and orig.casefold() not in SYSTEM:
                    raise ValueError(f'Unknown imported layer {orig}')
                imported[name] = orig
        for ent in doc.entitydb.values():
            if ent.is_alive and ent.dxf.is_supported('layer') and ent.dxf.layer in imported:
                ent.dxf.layer = imported[ent.dxf.layer]
        for name in imported:
            doc.layers.remove(name)
        ext = bbox.extents(mapping.values())
        labels.append((panel['name'],ext.center.x,ext.extmax.y))
        report.append(dict(name=panel['name'],source=str(source),translation_mm=list(delta),
                           x_range=[ext.extmin.x-100,ext.extmax.x+100],handles={h:e.dxf.handle for h,e in mapping.items()}))
    top = max(y for _,_,y in labels)+1200
    for name,x,_ in labels:
        doc.modelspace().add_text(name,dxfattribs={'layer':LABEL_LAYER,'height':420}).set_placement((x,top),align=TextEntityAlignment.MIDDLE_CENTER)
    ext = bbox.extents(doc.modelspace())
    doc.set_modelspace_vport(height=max(ext.size.y*1.1,ext.size.x*.65),center=ext.center.xy)
    doc.saveas(output)
    return report


if __name__=='__main__':
    args=argparse.ArgumentParser(description=__doc__)
    args.add_argument('panels',type=Path)
    args.add_argument('output',type=Path)
    opts=args.parse_args()
    data=json.loads(opts.panels.read_text())
    merge(data,opts.panels.parent,opts.output)
