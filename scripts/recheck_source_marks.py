"""Compare an author's source-only marks with actual geometry in a saved drawing.

marks in original image pixels, registration as in render_source_overlay.py. This
is an independent reading aid, not an estimator of true construction dimensions.
"""
import argparse
import json
import math
from pathlib import Path
from dimension_grid import GridProjection
import ezdxf
from ezdxf import bbox
from ezdxf.math import Vec2
from PIL import Image, ImageDraw, ImageFont
from shapely.geometry import LineString, Point
from check_wall_probes import decode


def audit(doc, registration, marks):
    projection = GridProjection(registration)
    def world(p):
        return Vec2(projection.world(p))
    layers = registration['layers']
    by_role = {}
    limitations = {}
    for role in ('bearing','partition'):
        layer = layers[role]
        mat,uncertain,issues,_ = decode(doc,{'wall_layers':{layer:'fills'},
            'nonwall_fill_layers':[name for name in layers.values() if name!=layer]})
        by_role[role] = mat
        limitations[role] = [*issues,*[name for name,_ in uncertain]]
    walls = by_role['bearing'].union(by_role['partition'])
    msp = doc.modelspace()
    arcs = [entity for entity in msp.query('ARC') if entity.dxf.layer==layers['door']]
    inserts = [entity for entity in msp.query('INSERT') if entity.dxf.layer in
               (layers['fixed'],layers['movable'],layers['ancillary'])]
    results = []
    ids = set()
    def append(row, state, **metrics):
        results.append(dict(id=row['id'],kind=row['kind'],status=state,**metrics))
    for row in marks['marks']:
        if row['id'] in ids:
            raise ValueError('Duplicate mark id: '+row['id'])
        ids.add(row['id'])
        kind = row['kind']
        if kind in ('clear','bearing','partition'):
            segment = LineString(projection.world_path(row['line']))
            if segment.length == 0:
                raise ValueError('Use two distinct source points')
            difference = (segment.intersection(walls) if kind=='clear' else segment.difference(by_role[kind])).length
            unknown = any(limitations.values()) if kind=='clear' else bool(limitations[kind])
            append(row,'复查' if difference>0.01 or unknown else '吻合',difference_mm=round(difference,2))
        elif kind=='leaf':
            hinge,tip = world(row['hinge']),world(row['tip'])
            px = float(row['uncertainty_px'])
            tol = max(math.dist(projection.world((x,y)),projection.world((x+dx,y+dy)))
                      for x,y in (row['hinge'],row['tip']) for dx,dy in ((px,0),(-px,0),(0,px),(0,-px))) if px>0 else 0
            if tol<=0 or hinge.distance(tip)<=tol:
                raise ValueError('Give legible endpoints and a local pixel uncertainty')
            candidates=[]
            for arc in arcs:
                center=Vec2(arc.dxf.center)
                ends=(Vec2(arc.start_point),Vec2(arc.end_point))
                pivot_error=center.distance(hinge)
                end_error=min(tip.distance(p) for p in ends)
                # A 90-degree arc includes both the open and the closed endpoint.
                # Verify a real narrow leaf reaches the chosen endpoint.
                if pivot_error<=tol and end_error<=tol:
                    near_tip=min(ends,key=lambda pt:pt.distance(tip))
                    for leaf in nearby_leaves(doc,layers['door']):
                        if any(p.distance(center)<=65 for p in leaf) and any(p.distance(near_tip)<=65 for p in leaf):
                            candidates.append(arc.dxf.handle)
                            break
            append(row,'吻合' if candidates else '复查',matches=candidates,tolerance_mm=round(tol,2))
        elif kind in ('object','furniture'):
            pt=world(row['point'])
            role=row.get('layer_role')
            if role not in ('movable','fixed','ancillary') and not (kind=='furniture' and role is None):
                raise ValueError('Set the expected layer role')
            hits=[]
            for ref in inserts:
                if role is not None and ref.dxf.layer!=layers[role]:
                    continue
                box=bbox.extents([ref],fast=False)
                if box.has_data and box.extmin.x<=pt.x<=box.extmax.x and box.extmin.y<=pt.y<=box.extmax.y:
                    hits.append({'handle':ref.dxf.handle,'block':ref.dxf.name})
            append(row,'核对象' if hits else '复查',matches=hits,expected_role=role)
        else:
            raise ValueError('Unknown mark kind: '+str(kind))
    return {'marks':results,'wall_decode':limitations,'limits':'标记由作者独立读源图。家具命中仅证明所在位置有候选块，须核类别、件数和占地。异形门、嵌套门及压线处用原生实量或局部图判断。'}


def nearby_leaves(doc,layer):
    for entity in doc.modelspace().query('LWPOLYLINE'):
        if entity.dxf.layer==layer and entity.closed and 3<=len(entity)<=8:
            yield [Vec2(p) for p in entity.get_points('xy')]


def contact_sheet(source, overlay, plain, regions, folder):
    with Image.open(source) as raw:
        base=raw.convert('RGB')
    views=[base]
    for path in (overlay,plain):
        with Image.open(path) as raw:
            if abs(raw.width/raw.height-base.width/base.height)>0.001:
                raise ValueError('All images must share the original frame')
            views.append(raw.convert('RGB'))
    if not regions:
        raise ValueError('Register at least one room or junction')
    folder.mkdir(exist_ok=True,parents=True)
    labels=('SOURCE','OVERLAY','CAD')
    for index,region in enumerate(regions,1):
        coords=region['box']
        if len(coords)!=4 or not (0<=coords[0]<coords[2]<=base.width and 0<=coords[1]<coords[3]<=base.height):
            raise ValueError(f"Out-of-frame region {region['id']}")
        w,h=coords[2]-coords[0],coords[3]-coords[1]
        zoom=min(5,600/max(w,h))
        size=tuple(max(1,round(val*zoom)) for val in (w,h))
        out=Image.new('RGB',(3*size[0],size[1]+26),'white')
        draw=ImageDraw.Draw(out)
        for col,im in enumerate(views):
            x=im.width/base.width;y=im.height/base.height
            box=tuple(int(round(v*(x if i%2==0 else y))) for i,v in enumerate(coords))
            tile=im.crop(box).resize(size,Image.Resampling.NEAREST if col==0 else Image.Resampling.LANCZOS)
            out.paste(tile,(col*size[0],26))
            draw.text((col*size[0]+3,4),f'{index:02d}  {labels[col]}',fill='black')
        out.save(folder/f'{index:02d}.png')
    (folder/'index.json').write_text(json.dumps([{'file':f'{i:02d}.png',**r} for i,r in enumerate(regions,1)],ensure_ascii=False,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('readback',type=Path)
    parser.add_argument('registration',type=Path)
    parser.add_argument('observations',type=Path)
    parser.add_argument('output',type=Path)
    parser.add_argument('--pictures',nargs=3,type=Path,metavar=('SOURCE','OVERLAY','PLAIN'))
    args=parser.parse_args()
    marks=json.loads(args.observations.read_text())
    report=audit(ezdxf.readfile(args.readback),json.loads(args.registration.read_text()),marks)
    args.output.mkdir(parents=True,exist_ok=True)
    (args.output/'source-vs-cad.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    if args.pictures:
        contact_sheet(*args.pictures,marks['regions'],args.output/'views')
    print(json.dumps({state:sum(x['status']==state for x in report['marks']) for state in ('吻合','复查','核对象')},ensure_ascii=False))
