"""Measure each actual chair moved away from its own table in disposable geometry.

Use stable IDs/handles from drafting; do not infer which table a chair serves from
proximity alone. Existing furniture remains unchanged. Results are geometry evidence.
"""
import argparse,json,math
from pathlib import Path
import ezdxf
from shapely.affinity import translate
from shapely.geometry import LineString, MultiPoint
from shapely.ops import unary_union
from screen_furniture_pairs import recover
from check_wall_probes import decode


def item_body(doc,handle):
    if not handle:return None
    ref=doc.entitydb.get(handle)
    if ref is None or ref.dxftype()!='INSERT':return None
    body,notes=recover(ref)
    if body is not None and not notes:return body,False
    from ezdxf.path import make_path
    points=[]
    def collect(e):
        if e.dxftype()=='INSERT':
            for child in e.virtual_entities():collect(child)
        elif e.dxftype() in ('LINE','ARC','LWPOLYLINE','POLYLINE','CIRCLE','ELLIPSE','SPLINE'):
            points.extend((v.x,v.y) for v in make_path(e).flattening(.25))
    collect(ref)
    conservative=MultiPoint(points).convex_hull if points else None
    return (conservative,True) if conservative is not None and conservative.area>.01 else None


def sweep(body,delta):
    # Exact translation extrusion for each straight outer-ring edge of the
    # flattened boundary. Do not fill a concave chair with its convex hull.
    regions=[body,translate(body,*delta)]
    pieces=body.geoms if body.geom_type=='MultiPolygon' else [body]
    for part in pieces:
        points=list(part.exterior.coords)
        for a,b in zip(points,points[1:]):
            face=MultiPoint([a,b,(b[0]+delta[0],b[1]+delta[1]),(a[0]+delta[0],a[1]+delta[1])]).convex_hull
            if face.area:regions.append(face)
    return unary_union(regions)


def run(doc,ledger,roles,groups):
    walls,unclear,issues,_=decode(doc,{'wall_layers':ledger['wall_layers'],'nonwall_fill_layers':ledger.get('nonwall_fill_layers',[])})
    handles={r.dxf.handle for r in doc.modelspace().query('INSERT') if r.dxf.layer in (roles['movable'],roles['fixed'])}
    shapes={h:item_body(doc,h) for h in handles};known={h:(v[0] if v else None) for h,v in shapes.items()};rows=[]
    for group in groups:
        table=known.get(group['table_handle'])
        for seat in group['seats']:
            h=seat['handle'];chair=known.get(h)
            if chair is None or table is None:
                rows.append({'handle':h,'status':'待核','reason':'桌或椅的实际轮廓未还原'});continue
            cx,cy=chair.centroid.coords[0];tx,ty=table.centroid.coords[0]
            vector=seat.get('pull_vector_mm') or [cx-tx,cy-ty]
            length=math.hypot(*vector);dist=float(group['pull_mm'])
            if length<=0 or dist<=0:raise ValueError('Give an actual pull direction and positive distance')
            dx,dy=dist*vector[0]/length,dist*vector[1]/length
            end=translate(chair,dx,dy);swept=sweep(chair,(dx,dy))
            allowed={h,group['table_handle'],*seat.get('intentional_projection_handles',[])}
            hits={k:round(swept.intersection(b).area,2) for k,b in known.items() if k not in allowed and b is not None and swept.intersection(b).area>.01}
            wall=swept.intersection(walls).area;orig=chair.intersection(table).area;after=end.intersection(table).area
            if after>orig+.01: hits['toward_table_wrong_direction']=round(after-orig,2)
            rows.append(dict(handle=h,table=group['table_handle'],pull_mm=dist,pull_vector_wcs=[dx,dy],
                initial_tuck_mm2=round(orig,2),end_tuck_mm2=round(after,2),swept_wall_mm2=round(wall,2),obstacle_handles=hits,
                geometry='保守外包' if shapes[h][1] else '闭合实体',status=('需复量外包重叠' if shapes[h][1] or any(shapes.get(k) and shapes[k][1] for k in hits) else '有碰撞') if hits or wall>.01 else ('待核' if issues or unclear else '通过')))
    return {'chairs':rows,'unrecoverable_handles':[h for h,b in known.items() if b is None],
            'limits':'每次只移动一把椅子。拉动方向由登记指定，缺省取桌中心向椅中心方向；倾斜或端座另核。桌面原有遮挡不算椅子撞桌。'}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for arg in ('dxf','ledger','registration','groups','out'):p.add_argument(arg,type=Path)
    a=p.parse_args();data=lambda path:json.loads(path.read_text())
    q=run(ezdxf.readfile(a.dxf),data(a.ledger),data(a.registration)['layers'],data(a.groups))
    a.out.write_text(json.dumps(q,ensure_ascii=False,indent=2));print([(v['handle'],v['status']) for v in q['chairs']])
