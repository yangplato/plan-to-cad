#!/usr/bin/env python3
"""Find use-facing seat groups and measure true readback strokes without selecting paths."""
import argparse
import json
import math
from pathlib import Path
import sys

from dimension_grid import GridProjection
import ezdxf
from shapely.affinity import affine_transform
from shapely.geometry import LineString, Point
from shapely.ops import polygonize, unary_union

from check_seating_groups import read_lines
from check_use_clearance import EPS, atoms, pair, pretty, unit, vertices_x
from place_plan_families import catalog


def use_role(row, source):
    """Start with explicit purpose, then inspect one purpose-bearing field at a time."""
    explicit = row.get('use_role') or source.get('use_role')
    if explicit:
        return explicit, 'use_role'
    terms = (
        ('coffee_table', ('coffee_table','coffee table','茶几','矮几','长几')),
        ('side_table', ('side_table','side table','边几','端几','角几','圆几','小几')),
        ('low_cabinet', ('low_cabinet','low cabinet','电视柜','地柜','矮柜','低柜')),
        ('footstool', ('footstool','ottoman','脚凳','脚踏')),
        ('sofa', ('sofa','sectional','couch','沙发','沙發')),
        ('lounge_seat', ('shell_lounge','armchair','lounge chair','休闲椅','休閒椅','单人椅','躺椅')),
    )
    for field,value in [('family',row.get('family')),('source_use',source.get('use')),
                        ('function',row.get('function')),('library_block',row.get('library_block')),
                        ('id',row['id'])]:
        text = str(value or '').casefold()
        for role,options in terms:
            if any(word in text for word in options):
                return role,field
    return 'other','未识别专用功能'


def map_px(scene,p):
    return GridProjection(scene).world(pair(p))


def source_normal(row, source, scene, geom):
    facing = row.get('facing_px') or source.get('facing_px')
    if facing:
        if isinstance(facing[0],(list,tuple)):
            start,end = facing
            start,end = map_px(scene,start), map_px(scene,end)
        else:
            anchor = row.get('back_px') or row.get('center_px') or source.get('anchor_px')
            start = map_px(scene,anchor) if anchor is not None else geom.centroid.coords[0]
            end = map_px(scene,facing)
        method = 'facing_px'
    elif row.get('back_px') is not None and row.get('toward_px') is not None:
        start,end = map_px(scene,row['back_px']),map_px(scene,row['toward_px'])
        method = 'back_px → toward_px'
    else:
        return None,'补 objects.json 本件 facing_px，给原图上由背到前的两点。'
    return unit([end[0]-start[0],end[1]-start[1]]),method


def make_shape(ref):
    lines,notes = read_lines(ref)
    if not lines:
        raise ValueError('缺可分解的俯视实线')
    strokes = unary_union(list(lines.values()))
    faces = unary_union(list(polygonize(strokes)))
    closed = not faces.is_empty and faces.buffer(EPS).covers(strokes)
    if closed:
        # Polygonized faces fill internal engraving; holes/open frames are still
        # a projection hypothesis. Points marked inside remain explicitly flagged.
        return faces,'closed_projection',notes
    return strokes,'strokes_only',notes


def native_front(ref):
    name = ref.dxf.name.upper()
    if name.startswith('MPC_F1_'):
        key = name.removeprefix('MPC_F1_').removesuffix('_MASK').lower()
        if key not in catalog():
            return None
    elif not name.startswith('PLAN_LIB_'):
        return None
    vec = ref.matrix44().transform_direction((0,-1,0))
    return unit([vec.x,vec.y])


def section_y(shape, x):
    _,y0,_,y1 = shape.bounds
    cut = shape.intersection(LineString([(x,y0-1),(x,y1+1)]))
    return [(c.bounds[1],c.bounds[3]) for c in atoms(cut)]


def station(a,b,x):
    occupied = section_y(a,x)
    if not occupied:
        return None
    front = max(p[1] for p in occupied)
    ahead = [(max(front,lo), lo < front-EPS and hi > front+EPS)
             for lo,hi in section_y(b,x) if hi >= front-EPS]
    if not ahead:
        return None
    hit,inside = min(ahead)
    return front,hit,inside


def pair_result(a, b, normal):
    """A section profile is affine between vertices of noded polygonal boundaries."""
    lo = max(a.bounds[0],b.bounds[0]); hi = min(a.bounds[2],b.bounds[2])
    if hi-lo <= EPS:
        return 'side_only',None
    if b.bounds[3] < a.bounds[1]-EPS:
        return 'behind',None
    first = a.boundary if a.geom_type in ('Polygon','MultiPolygon') else a
    second = b.boundary if b.geom_type in ('Polygon','MultiPolygon') else b
    cross = first.intersection(second)
    cuts = sorted({lo,hi,*(x for g in [a,b,cross] for x in vertices_x(g) if lo < x < hi)})
    center = (a.bounds[0]+a.bounds[2])/2
    spans, pieces = [], []
    for l,r in zip(cuts,cuts[1:]):
        if r-l <= EPS:
            continue
        p = station(a,b,(2*l+r)/3)
        q = station(a,b,(l+2*r)/3)
        if p is None or q is None:
            continue
        v = [2*p[0]-q[0],2*p[1]-q[1]]
        w = [2*q[0]-p[0],2*q[1]-p[1]]
        inside = p[2] or q[2]
        if spans and l <= spans[-1][1]+EPS:
            spans[-1][1]=r
        else:
            spans.append([l,r])
        # Retain every change of slope; join repeated collinear DXF tessellation.
        if pieces:
            left,right,v0,w0,flag = pieces[-1]
            if (abs(right-l)<=EPS and flag==inside and max(abs(w0[k]-v[k]) for k in (0,1))<=EPS
                and max(abs((w[k]-v[k])*(right-left)-(w0[k]-v0[k])*(r-l)) for k in (0,1))<=EPS):
                pieces[-1] = (left,r,v0,w,flag)
                continue
        pieces.append((l,r,v,w,inside))
    if not pieces:
        return 'no_front_sections',None
    nx,ny = normal
    def sample(x,v):
        def world(y): return [pretty(ny*x+nx*y),pretty(-nx*x+ny*y)]
        return {'station_mm':pretty(x-center),'seat_edge_wcs':world(v[0]),'hit_wcs':world(v[1]),
                'gap_mm':pretty(max(0,v[1]-v[0]))}
    sections = []
    for l,r,v,w,inside in pieces:
        sections.append({'from':sample(l,v),'to':sample(r,w),'width_mm':pretty(r-l),
                         'starts_inside_projected_body':inside})
    gaps = [row[k]['gap_mm'] for row in sections for k in ('from','to')]
    return 'front',{'min_gap_mm':min(gaps),'max_gap_mm':max(gaps),
                    'continuous_width_mm':pretty(max(r-l for l,r in spans)),
                    'front_spans_mm':[[pretty(l-center),pretty(r-center)] for l,r in spans],
                    'sections':sections}


def screen(doc, scene, schedule, objects, inventory=None, bindings=None):
    """Existing compile_plan schedule and products['objects']; optional source inventory."""
    if doc.units != 4:
        raise ValueError('复开模型单位须为毫米')
    issues, sources, bodies, semantics = [], {}, {}, {}
    if inventory:
        if scene.get('source_sha256') and inventory.get('source_sha256') != scene['source_sha256']:
            issues.append('独立家具清单所指原图不同，本次未采用该清单')
        else:
            for r in inventory['items']:
                target = (bindings or {}).get(r['id'],r['id'])
                if target in sources:
                    raise ValueError('独立家具清单到实物的绑定重复 '+target)
                sources[target] = r
    scheduled = {r['id']:r for r in schedule}
    if len(scheduled)!=len(schedule):
        raise ValueError('对象编排编号重复')
    for key in sorted(set(scheduled)|set(objects)|set(sources)):
        row = scheduled.get(key,{'id':key})
        source = sources.get(key,{})
        role,method = use_role(row,source)
        room = source.get('room') or row.get('room')
        semantics[key] = (role,method,room)
        rec = objects.get(key)
        if not rec:
            issues.append({'id':key,'reason':'缺实际对象句柄'})
            continue
        ref = doc.entitydb.get(rec['handle'])
        if (ref is None or ref.dxftype()!='INSERT' or ref.dxf.owner!=doc.modelspace().layout_key
                or (rec.get('block') and ref.dxf.name.casefold()!=rec['block'].casefold())):
            issues.append({'id':key,'reason':'回读中的实际块与句柄登记不符'})
            continue
        try:
            geom,method,notes = make_shape(ref)
        except (ValueError,KeyError,TypeError,ezdxf.DXFError) as exc:
            issues.append({'id':key,'reason':str(exc)})
            continue
        if notes:
            issues.append({'id':key,'reason':'部分图元未解码或需另核','details':notes})
        bodies[key] = (ref,geom,method)
    seats = []
    open_rooms = {frozenset(r['rooms']) for r in scene.get('open_room_links',[]) if r.get('evidence')}
    for key,(role,method,room) in semantics.items():
        if role not in ('sofa','sofa_section','lounge_seat'):
            continue
        row = scheduled.get(key,{'id':key})
        result = {'id':key,'use_role':role,'identified_by':method,'room':room,
                  'candidates':[],'other_positions':[]}
        seats.append(result)
        if key not in bodies:
            result['direction_status']='missing_geometry'
            continue
        ref,geom,kind = bodies[key]
        result.update(handle=ref.dxf.handle,edge_method=kind)
        actual = native_front(ref)
        try:
            source,note = source_normal(row,sources.get(key,{}),scene,geom)
        except (ValueError,TypeError,IndexError) as exc:
            source,note = None,'核对本件 facing_px 的两端坐标：'+str(exc)
        result.update(source_direction=source,direction_basis=note,native_direction=actual)
        if not source and not actual:
            result['direction_status']='missing_facing_px'
            continue
        normal = actual or source
        if source and actual:
            angle = math.degrees(math.acos(max(-1,min(1,source[0]*actual[0]+source[1]*actual[1]))))
            result.update(direction_difference_deg=pretty(angle),
                          direction_status='source_and_native' if angle<=0.05 else 'direction_disagreement')
        else:
            result['direction_status']='source_only' if source else 'native_only_need_facing_px'
        matrix=[normal[1],-normal[0],normal[0],normal[1],0,0]
        seat = affine_transform(geom,matrix)
        for other,(oref,g,how) in bodies.items():
            if other==key:
                continue
            other_role,_,other_room=semantics[other]
            if room and other_room and room!=other_room and frozenset((room,other_room)) not in open_rooms:
                result['other_positions'].append({'id':other,'position':'different_room'})
                continue
            other_frame=affine_transform(g,matrix)
            position,measure=pair_result(seat,other_frame,normal)
            if measure is None:
                result['other_positions'].append({'id':other,'position':position})
                continue
            measure.update(id=other,handle=oref.dxf.handle,use_role=other_role,edge_method=how,
                           room=other_room,room_evidence='same' if room and room==other_room
                           else ('open_link' if room and other_room else 'unconfirmed'))
            result['candidates'].append(measure)
        result['candidates'].sort(key=lambda item:item['min_gap_mm'])
    return {'seats':seats,'coverage_issues':issues,'objects_examined':len(bodies),
            'scope':'按实际俯视线查每件位于使用正前的物件。净距是这两件之间的图面间距。其它物件、墙、人体姿态和高度另核。未读到沙发时请核对原图并给对象补 use_role。曲线以0.25毫米离散。'}


def main():
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument('job',type=Path);cli.add_argument('built_folder',type=Path)
    args=cli.parse_args()
    def read(file): return json.loads(file.read_text(encoding='utf-8'))
    try:
        job=read(args.job)
        def source(k): return read(args.job.parent/job[k]) if job.get(k) else None
        src=args.built_folder/'review/native/readback.dxf'
        report=screen(ezdxf.readfile(src),source('scene'),source('objects'),read(args.built_folder/'objects.json'),
                      source('furniture_source'),source('object_bindings'))
        from native_readback import digest
        report['readback_sha256']=digest(src)
        print(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False))
    except (OSError,ValueError,KeyError,TypeError,ezdxf.DXFError) as exc:
        print(str(exc),file=sys.stderr)
        return 2
    return 0


if __name__=='__main__':
    sys.exit(main())
