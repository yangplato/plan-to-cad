#!/usr/bin/env python3
"""Measure registered use edges on a saved millimetre drawing. Never changes CAD."""
import argparse
from dataclasses import dataclass, replace
from hashlib import sha256
from itertools import combinations
import json
import math
from pathlib import Path
import sys

import ezdxf
from ezdxf import bbox
from ezdxf.path import make_path
from shapely.affinity import affine_transform, translate
from shapely.geometry import LineString, Polygon
from shapely.ops import polygonize, unary_union

from check_seating_groups import read_lines
from check_wall_probes import decode
from measure_furniture import PATH_TYPES
from seat_motion import sweep

EPS = 1e-6  # arithmetic tolerance in mm, not a design threshold
CURVE_TOL = 0.25


def require(ok, message):
    if not ok:
        raise ValueError(message)


def num(value):
    require(type(value) in (int, float) and math.isfinite(value), '坐标和尺寸须为有限数值')
    return float(value)


def pair(values):
    a, b = values
    return num(a), num(b)


def interval(values):
    a, b = pair(values)
    require(b > a, '区间上限须大于下限')
    return a, b


def unit(values):
    x, y = pair(values)
    length = math.hypot(x, y)
    require(length > EPS, '方向不可为零')
    return x / length, y / length


def atoms(shape):
    if not shape.is_empty:
        if hasattr(shape, 'geoms'):
            for child in shape.geoms:
                yield from atoms(child)
        else:
            yield shape


def outline(lines):
    net = unary_union(lines)
    polygons = list(polygonize(net))
    require(len(polygons) == 1 and polygons[0].boundary.buffer(EPS).covers(net),
            '所选路径未组成唯一、无内部支线的实体闭边')
    return polygons[0]


def paths_of(entity):
    if entity.dxftype() == 'INSERT':
        return read_lines(entity)
    kind = entity.dxftype()
    require(kind in PATH_TYPES and not entity.dxf.get('invisible', 0), '需实际可见平面边或 INSERT')
    if kind in ('LWPOLYLINE', 'POLYLINE'):
        require(not entity.has_width, '有线宽，不能以中心线代替实体边')
        require(kind != 'POLYLINE' or entity.is_2d_polyline, '需二维轮廓')
    pts = list(make_path(entity, segments=4).flattening(CURVE_TOL))
    require(pts and max(v.z for v in pts)-min(v.z for v in pts) <= EPS, '需同平面路径')
    return {'self': LineString([(v.x, v.y) for v in pts])}, []


@dataclass
class Part:
    obj: str
    name: str
    geom: object
    height: object
    curved: bool = False


def hidden_layer(doc, name):
    layer = doc.layers.get(name)
    return layer.is_frozen() or layer.is_off()


def parts_of(doc, record):
    ref = doc.entitydb.get(record['handle'])
    require(ref is not None and ref.dxf.owner == doc.modelspace().layout_key,
            '失配的模型空间句柄 ' + record['handle'])
    require(record.get('basis') and record['kind'] in ('movable', 'fixed', 'structure'),
            '需登记物件类别、轮廓与高度依据')
    raw, issues = paths_of(ref)
    require(not issues, ' / '.join(issues))
    require(not hidden_layer(doc, ref.dxf.layer), '物件在关层或冻层')
    parts, used = {}, set()
    for p in record['parts']:
        name, paths = p['id'], p['paths']
        require(name and name not in parts and isinstance(paths, list) and paths, '重复或缺少部位、路径')
        require(set(paths) <= raw.keys(), '无此块内路径，请从本次实际回读重取')
        require(len(paths) == len(set(paths)) and not used.intersection(paths), '重复实体边')
        shape = outline([raw[k] for k in paths])
        curved = False
        for k in paths:
            chain = ([doc.entitydb.get(h) for h in k.split('/')] if k != 'self' else [ref])
            for ent in chain:
                require(ent is not None and not hidden_layer(doc, ent.dxf.layer), '块内有关闭或冻结的构件')
            leaf = chain[-1]
            curved |= leaf.dxftype() in {'ARC','CIRCLE','SPLINE','ELLIPSE'} or bool(getattr(leaf,'has_arc',False))
        height = interval(p['z_mm']) if p.get('z_mm') is not None else None
        parts[name] = Part(record['id'], name, shape, height, curved)
        used.update(paths)
    require(parts, '未给实物部位')
    remaining = set(raw)-used
    if remaining:
        require(record.get('interior_detail_basis'), '实际图中还有未辨用途的图线 ' + str(sorted(remaining)))
        covered = unary_union([p.geom for p in parts.values()]).buffer(EPS)
        require(all(covered.covers(raw[k]) for k in remaining), '有图线伸出所选实体边')
    return ref, list(parts.values())


def load_scene(doc, spec):
    require(doc.units == 4, '要求以毫米保存的模型空间')
    require(spec.get('scope_basis'), '需声明使用范围、相邻实物及其取证依据')
    scope = Polygon([pair(pt) for pt in spec['scope_wcs']])
    require(scope.is_valid and scope.area > EPS, '检查范围须是有效的局部封闭区域')
    layers = set(spec['physical_layers'])
    require(layers and all(k in doc.layers for k in layers), '实际物件图层缺失')
    require(not any(hidden_layer(doc,k) for k in layers), '请从家具及设备全开的回读图取边')
    require(set(spec.get('movable_layers', [])) <= layers, '活动家具层须在核对范围内')
    issues, parts, records, refs, handles = [], [], {}, {}, set()
    for record in spec['objects']:
        key, h = record['id'], record['handle']
        require(key and key not in records and h not in handles and key != '@walls', '物件或句柄重复')
        records[key] = record
        handles.add(h)
        try:
            ref, recovered = parts_of(doc, record)
            require(ref.dxf.layer in layers, '登记物件不在所列实物图层')
            refs[key] = ref
            parts.extend(recovered)
        except (ValueError, KeyError, TypeError, ezdxf.DXFError) as exc:
            issues.append(f'{key} / {h}: {exc}')
    # A bounding box may skip an entirely out-of-scope entity, never clear a local item.
    for entity in doc.modelspace():
        if entity.dxf.layer not in layers or entity.dxf.handle in handles:
            continue
        box = bbox.extents([entity])
        if box.has_data:
            x0,y0,x1,y1 = scope.bounds
            if box.extmax.x < x0 or box.extmin.x > x1 or box.extmax.y < y0 or box.extmin.y > y1:
                continue
        issues.append(f'范围内图层 {entity.dxf.layer} 还有未登记对象 {entity.dxf.handle}')
    if spec.get('wall_layers'):
        require(not layers.intersection(spec['wall_layers']), '结构解码层和逐件实物层重复')
        require(all(k in doc.layers and not hidden_layer(doc,k) for k in spec['wall_layers']), '登记墙层缺失或已关闭')
        walls, unclear, notes, _ = decode(doc, spec)
        issues.extend(notes + [n for n, _ in unclear])
        if not walls.is_empty:
            parts.append(Part('@walls', '结构', walls, (-math.inf, math.inf)))
    else:
        require(spec.get('structure_basis'), '须指定实墙层，或说明局部结构如何逐件覆盖')
    return scope, parts, records, refs, issues


def overlap_z(a, b):
    if a is None or b is None:
        return 'unknown'
    return 'overlap' if min(a[1],b[1])-max(a[0],b[0]) > EPS else 'separate'


def collisions(parts, scope):
    found = []
    for a,b in combinations(parts,2):
        if a.obj == b.obj or not a.geom.intersects(b.geom):
            continue
        hit = a.geom.intersection(b.geom).intersection(scope)
        if hit.area <= EPS:
            continue
        rel = overlap_z(a.height,b.height)
        verdict = {'unknown':'height_unknown','separate':'height_separated','overlap':'physical_overlap'}[rel]
        if rel == 'overlap' and (a.curved or b.curved):
            pa = a.geom.buffer(-CURVE_TOL) if a.curved else a.geom
            pb = b.geom.buffer(-CURVE_TOL) if b.curved else b.geom
            if pa.intersection(pb).intersection(scope).area <= EPS:
                verdict = 'curve_contact_review'
        found.append({'objects':[a.obj,b.obj], 'parts':[a.name,b.name],
                      'relation':verdict,
                      'area_mm2':round(hit.area,3),'at_wcs':list(hit.representative_point().coords[0])})
    return found


def vertices_x(g):
    for item in atoms(g):
        if item.geom_type == 'Polygon':
            yield from (c[0] for c in item.exterior.coords)
            for ring in item.interiors:
                yield from (c[0] for c in ring.coords)
        else:
            yield from (c[0] for c in item.coords)


def slice_y(shape, x):
    y0,y1 = shape.bounds[1::2]
    return shape.intersection(LineString([(x,y0-1),(x,y1+1)]))


def front_y(shape, x):
    sl = slice_y(shape,x)
    require(not sl.is_empty, '使用边范围超出了实物')
    return sl.bounds[3]


def strip(shape, lo, hi, depth):
    # In each open interval between polygon vertices the forward envelope is linear.
    cuts = sorted({lo, hi, *(x for x in vertices_x(shape) if lo < x < hi)})
    bands = []
    for a,b in zip(cuts,cuts[1:]):
        if b-a < EPS:
            continue
        ya = front_y(shape, (2*a+b)/3)
        yb = front_y(shape, (a+2*b)/3)
        left, right = 2*ya-yb, 2*yb-ya
        bands.append(Polygon([(a,left),(b,right),(b,right+depth),(a,left+depth)]))
    return unary_union(bands)


def free_width(lo, hi, blocks):
    start, free = lo, []
    for a,b in sorted(blocks):
        if b-a <= EPS:
            continue
        if a > start+EPS:
            free.append((start,a))
        start = max(start,b)
    if hi > start+EPS:
        free.append((start,hi))
    return free


def pretty(value):
    return round(value,3)


def use_geometry(use, owner, ref, parts, scope, detailed):
    nlocal = unit(use['forward_local'])
    if ref.dxftype() == 'INSERT':
        vec = ref.matrix44().transform_direction((*nlocal,0))
        normal = unit([vec.x,vec.y])
    else:
        normal = nlocal
    n0,n1 = normal
    tx,ty = n1,-n0
    expected = unit(use['source_direction_wcs'])
    tolerance = num(use.get('max_axis_error_deg',15))
    require(0 <= tolerance < 90, '方向容差须小于直角')
    angle = math.degrees(math.acos(max(-1,min(1,n0*expected[0]+n1*expected[1]))))
    result = {'id':use['id'],'object':owner.obj,'part':owner.name,'basis':use['basis'],
              'forward_wcs':list(normal),'direction_error_deg':pretty(angle)}
    if angle > tolerance+EPS:
        return dict(result,status='wrong_direction')
    matrix = [tx,ty,n0,n1,0,0]
    frame = affine_transform(owner.geom,matrix)
    x0,_,x1,_ = frame.bounds
    center = (x0+x1)/2
    a,b = interval(use['span_mm'])
    lo,hi = center+a,center+b
    require(x0-EPS <= lo < hi <= x1+EPS, '登记宽度超出该部位真实侧向范围')
    lo,hi = max(lo,x0),min(hi,x1)
    depth = num(use['required_depth_mm'])
    width = num(use['required_width_mm'])
    scan = num(use.get('lookahead_mm',depth*2))
    require(0 < width <= hi-lo+EPS and 0 < depth <= scan, '所需宽深或测量范围不合理')
    z = interval(use['use_z_mm'])
    zone = strip(frame,lo,hi,depth)
    local_scope = affine_transform(scope,matrix)
    inside = local_scope.buffer(EPS).covers(zone)
    # A body exactly at the required distance legally bounds that distance.
    # Line/point-only contact with the required-depth limit has zero area.
    blocked, possible, neighbors, projected, sections_x = [], [], [], [], {lo,hi,(lo+hi)/2}
    for p in parts:
        if (p.obj,p.name) == (owner.obj,owner.name):
            continue
        s = affine_transform(p.geom,matrix)
        xmin, ymin, xmax, ymax = s.bounds
        reach = overlap_z(z,p.height)
        # Bound near-threshold decisions for flattened curved outlines. Clip the
        # projection back to the registered span when either margin grows laterally.
        margin = CURVE_TOL * (int(owner.curved)+int(p.curved))
        def spans(area):
            return [(max(lo,c.bounds[0]),min(hi,c.bounds[2])) for c in atoms(area)
                    if c.geom_type=='Polygon' and c.area>EPS and
                    min(hi,c.bounds[2])-max(lo,c.bounds[0])>EPS]
        if xmax < lo-margin-EPS or xmin > hi+margin+EPS:
            relation = 'side_only'
        elif ymax < zone.bounds[1]-margin-EPS:
            relation = 'behind'
        else:
            crossed = spans(s.intersection(zone))
            relation = 'outside_required_zone'
            if crossed:
                relation = {'overlap':'in_use_zone','unknown':'height_unknown','separate':'height_separated'}[reach]
            if reach != 'separate':
                # Buffer the solid relative to the access strip. The span limits
                # are declared use limits; a curve margin must not shrink them.
                could = spans(s.buffer(margin).intersection(zone)) if margin else crossed
                sure = spans(s.buffer(-margin).intersection(zone)) if margin else crossed
                possible.extend(could)
                if reach == 'overlap':
                    blocked.extend(sure)
                if could and not sure and margin:
                    relation = 'curve_contact_review'
                for l,r in crossed:
                    sections_x.update([l,r,(l+r)/2])
            if reach != 'separate' and ymin <= zone.bounds[3]+scan and detailed:
                projected.append((p,s,reach))
                sections_x.update(x for x in vertices_x(s) if lo < x < hi)
        if detailed:
            neighbors.append({'object':p.obj,'part':p.name,'relation':relation})
    definite = free_width(lo,hi,blocked)
    conservative = free_width(lo,hi,possible)
    max_width = lambda spans: max((r-l for l,r in spans),default=0)
    if max_width(definite)+EPS < width:
        status = 'insufficient_width_at_depth'
    elif not inside or max_width(conservative)+EPS < width:
        status = 'review'
    elif blocked or possible:
        status = 'partial_clear_width'
    else:
        status = 'clear'
    result.update(status=status,scope_covers_required_zone=inside,required_depth_mm=depth,
                  required_width_mm=width,
                  known_clear_spans_mm=[[pretty(l-center),pretty(r-center)] for l,r in conservative],
                  longest_known_clear_width_mm=pretty(max_width(conservative)),
                  longest_possible_clear_width_mm=pretty(max_width(definite)))
    if not detailed:
        return result
    result.update(neighbors=neighbors,lookahead_mm=scan,sections=[])
    sections_x.update(x for x in vertices_x(frame) if lo < x < hi)
    for i in range(1,9):
        sections_x.add(lo+(hi-lo)*i/9)
    def world(x,y):
        return [pretty(tx*x+n0*y),pretty(ty*x+n1*y)]
    for x in sorted(sections_x):
        y = front_y(frame,x)
        ray = LineString([(x,y),(x,y+scan)])
        cover = ray.intersection(local_scope)
        ranges = [g.bounds[3]-y for g in atoms(cover)
                  if g.bounds[1] <= y+EPS and g.bounds[3] >= y-EPS]
        limit = min(scan,max(ranges, default=0))
        if limit < scan-EPS and limit > EPS:
            ray = LineString([(x,y),(x,y+limit)])
        choices = []
        for p,s,reach in projected:
            if limit > EPS and s.bounds[0]-EPS <= x <= s.bounds[2]+EPS:
                hit = s.intersection(ray)
                if not hit.is_empty:
                    choices.append((max(0,hit.bounds[1]-y),p.obj,p.name,reach))
        gaps = sorted(choices)
        row = {'station_mm':pretty(x-center),'use_edge_wcs':world(x,y)}
        if gaps:
            d,key,name,level = gaps[0]
            row.update(gap_mm=pretty(d),hit_wcs=world(x,y+d),object=key,part=name,height=level)
        else:
            row['clear_to_mm'] = pretty(limit)
            if limit < scan-EPS:
                row['limit'] = 'scope_boundary'
        result['sections'].append(row)
    return result


def evaluate(spec, parts, refs, scope, detailed=True):
    by_id = {(p.obj,p.name):p for p in parts}
    uses, seen = [], set()
    for u in spec['uses']:
        require(u['id'] and u['id'] not in seen and u.get('basis'), '用途须有唯一编号与本案采用依据')
        seen.add(u['id'])
        try:
            result = use_geometry(u,by_id[u['object'],u['part']],refs[u['object']],parts,scope,detailed)
        except (KeyError,ValueError,TypeError) as e:
            result = {'id':u['id'],'status':'review','reason':str(e)}
        uses.append(result)
    require(uses,'至少须登记一个实际用途')
    hits = collisions(parts,scope)
    ok = all(u['status'] in ('clear','partial_clear_width') for u in uses)
    ok &= all(h['relation']=='height_separated' for h in hits)
    return uses,hits,ok


def propose(spec, request, parts, refs, records, scope):
    key = request['object']
    require(key in records and records[key]['kind']=='movable' and refs[key].dxftype()=='INSERT' and
            refs[key].dxf.layer in spec.get('movable_layers',[]), '仅可对登记的活动家具层上的独立活动块试移')
    require(request.get('basis'), '缺少原图允许的沿轴局部位移依据')
    axis = unit(request['axis_wcs'])
    a,b = interval(request['range_mm'])
    step = num(request['step_mm'])
    require(a <= 0 <= b and step > 0 and (b-a)/step <= 2000,'区间须含原位，最多核对 2001 个格点')
    envelope = Polygon([pair(pt) for pt in request['allowed_footprint_wcs']])
    require(envelope.is_valid and scope.covers(envelope), '允许的落位区域须在核对范围内')
    originals = [p for p in parts if p.obj == key]
    require(all(envelope.buffer(EPS).covers(p.geom) for p in originals), '原位须处于原图允许的落位区域内')
    candidates = sorted({a,b,0.0, *(i*step for i in range(math.ceil(a/step),math.floor(b/step)+1))},
                        key=lambda v:(abs(v),v))
    for index,d in enumerate(candidates,1):
        dx,dy = d*axis[0],d*axis[1]
        moved = [replace(p,geom=translate(p.geom,dx,dy)) if p.obj==key else p for p in parts]
        if any(not envelope.buffer(EPS).covers(p.geom) for p in moved if p.obj==key):
            continue
        uses,_,ok = evaluate(spec,moved,refs,scope,False)
        if not ok:
            continue
        # Verify the translation itself and its entire source-supported footprint.
        # Existing penetration may be withdrawn, but do not enter additional matter.
        safe = True
        for original in originals:
            path = sweep(original.geom,(dx,dy))
            if not envelope.buffer(EPS).covers(path):
                safe = False
                break
            added = path.difference(original.geom)
            if any(p.obj != key and overlap_z(p.height,original.height) != 'separate'
                   and added.intersection(p.geom).area > EPS for p in parts):
                safe = False
                break
        if safe:
            return {'object':key,'status':'candidate','delta_wcs_mm':[pretty(dx),pretty(dy)],'signed_offset_mm':d,
                    'checked_count':index,'basis':request['basis'],'verified_uses':uses}
    return {'object':key,'status':'no_candidate','checked_count':len(candidates),'basis':request['basis']}


def audit(doc,spec):
    scope,parts,records,refs,issues = load_scene(doc,spec)
    uses,hits,ok = evaluate(spec,parts,refs,scope)
    repairs = []
    if not issues and not ok:
        for req in spec.get('repairs',[]):
            repairs.append(propose(spec,req,parts,refs,records,scope))
    return {'ok':bool(ok and not issues),'coverage_issues':issues,'uses':uses,'physical_pairs':hits,
            'proposals':repairs,'curve_flattening_mm':CURVE_TOL,
            'scope':'所登记的俯视轮廓、局部结构和使用姿态。候选仅为所给沿轴格点及区间端点中的最小位移。'}


def inspect(doc,handles):
    rows = []
    for h in handles:
        e = doc.entitydb.get(h)
        require(e is not None and e.dxf.owner == doc.modelspace().layout_key,'不是模型空间对象 ' + h)
        raw,notes = paths_of(e)
        rows.append({'handle':h,'layer':e.dxf.layer,'notes':notes,'paths':{
            k:{'closed':line.is_ring,'bounds_wcs':line.bounds,'start_wcs':line.coords[0],'end_wcs':line.coords[-1]}
            for k,line in raw.items()}})
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('readback',type=Path)
    parser.add_argument('registration',type=Path,nargs='?')
    parser.add_argument('--inspect',nargs='+',metavar='HANDLE')
    args = parser.parse_args()
    if bool(args.inspect)==bool(args.registration):
        parser.error('选择登记 JSON 或 --inspect 句柄')
    try:
        doc = ezdxf.readfile(args.readback)
        if args.inspect:
            result = {'objects':inspect(doc,args.inspect)}
        else:
            result = audit(doc,json.loads(args.registration.read_text(encoding='utf-8')))
        result['readback_sha256'] = sha256(args.readback.read_bytes()).hexdigest()
        print(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False))
        return 0 if result.get('ok',True) else 1
    except (OSError,ValueError,KeyError,TypeError,ArithmeticError,ezdxf.DXFError) as exc:
        print(str(exc),file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
