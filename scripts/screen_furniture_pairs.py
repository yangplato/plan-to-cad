"""Screen actual plan-block outlines, both furniture layers, door sweeps and containment.

Supply semantic contact exemptions per object pair; spatial overlap does not establish
purpose. Callers must check seat structure and elevation in the source before a verdict.
"""
import argparse
import json
from pathlib import Path
from itertools import combinations
import ezdxf
from ezdxf.path import make_path
from shapely.geometry import LineString
from shapely.ops import polygonize, unary_union
from check_wall_probes import decode
from place_hosted_family import screen_door_swings


def recover(ref):
    lines=[];skipped=set()
    def visit(obj):
        if obj.dxftype()=='INSERT':
            for child in obj.virtual_entities():visit(child)
        elif obj.dxftype() in ('ARC','CIRCLE','ELLIPSE','LWPOLYLINE','POLYLINE','LINE','SPLINE'):
            pts=[(round(v.x,5),round(v.y,5)) for v in make_path(obj).flattening(.25)]
            if len(pts)>1:lines.append(LineString(pts))
        elif obj.dxftype() not in ('WIPEOUT','TEXT','MTEXT','ATTDEF','ATTRIB'):
            skipped.add(obj.dxftype())
    visit(ref)
    if not lines:return None,sorted(skipped|{'no strokes'})
    hull=unary_union(lines);pieces=list(polygonize(hull))
    if not pieces:return None,sorted(skipped|{'no closed area'})
    body=unary_union(pieces)
    if not body.buffer(.02).covers(hull):skipped.add('strokes outside enclosed outline')
    return body,sorted(skipped)


def screen(doc,ledger,roles,relations):
    mat,unclear,issues,_=decode(doc,{'wall_layers':ledger['wall_layers'],
        'nonwall_fill_layers':ledger.get('nonwall_fill_layers',[])})
    layers={roles['movable'],roles['fixed']}
    relations_by_pair={}
    for row in relations:
        key=frozenset(row['handles'])
        if len(key)!=2 or key in relations_by_pair or not row.get('basis'):raise ValueError('Each declared contact needs two distinct handles and a physical reason')
        relations_by_pair[key]=row
    bodies={};coverage=[];against_wall=[]
    for obj in doc.modelspace().query('INSERT'):
        if obj.dxf.layer not in layers:continue
        handle=obj.dxf.handle;body,limitations=recover(obj)
        coverage.append({'handle':handle,'block':obj.dxf.name,'layer':obj.dxf.layer,'notes':limitations,'area_found':body is not None})
        if body is None:continue
        bodies[handle]=body;overlap=body.intersection(mat).area
        if overlap>0.01:against_wall.append({'handle':handle,'model_overlap_mm2':round(overlap,2)})
    pairs=[]
    for (h,a),(k,b) in combinations(bodies.items(),2):
        overlap=a.intersection(b).area
        if overlap<=.01:continue
        relation=relations_by_pair.get(frozenset((h,k)))
        item={'handles':[h,k],'overlap_mm2':round(overlap,2)}
        if relation is not None:item['declared_relationship']=relation.get('basis')
        if relation is not None and relation.get('host') in (h,k):
            host=relation['host'];thing=k if host==h else h
            item['outside_host_mm2']=round(bodies[thing].difference(bodies[host]).area,2)
        pairs.append(item)
    missing_hosts=[]
    for row in relations:
        if row.get('host') and not any(set(item['handles'])==set(row['handles']) for item in pairs):
            missing_hosts.append(row)
    sweeps=screen_door_swings(doc,ledger,bodies)
    return {'units':'mm','coverage':coverage,'wall_decoding_issues':issues+[n for n,_ in unclear],
        'wall_intersections':against_wall,'furniture_intersections':pairs,'host_pairs_without_overlap':missing_hosts,
        'door_body_pairs_checked':len(sweeps),'door_body_intersections':[s for s in sweeps if s['collision']],
        'limits':'只用当前回读实体闭合线，凹面、无闭环、嵌入设备、桌下收椅及高低重叠另逐件核。碰撞面积均未自动判可用。'}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('dxf','ledger','registration','output'):p.add_argument(name,type=Path)
    p.add_argument('--relations',type=Path)
    args=p.parse_args()
    report=screen(ezdxf.readfile(args.dxf),json.loads(args.ledger.read_text()),json.loads(args.registration.read_text())['layers'],
        json.loads(args.relations.read_text()) if args.relations else [])
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(len(report['coverage']),'inserts;',len(report['furniture_intersections']),'overlapping pairs;',len(report['door_body_intersections']),'door/body contacts')
