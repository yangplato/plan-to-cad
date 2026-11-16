#!/usr/bin/env python3
"""Check registered seating groups against the complete INSERTs in a saved mm DXF."""
import argparse
from collections import Counter
from dataclasses import dataclass, replace
import hashlib
from itertools import combinations
import json
import math
from pathlib import Path
import sys

import ezdxf
from ezdxf.path import make_path
from shapely.affinity import affine_transform, translate
from shapely.geometry import LineString, MultiPoint, Point, Polygon
from shapely.ops import polygonize, unary_union

from measure_furniture import PATH_TYPES, block_layout
from place_plan_families import catalog
from screen_furniture_pairs import recover
from seat_motion import sweep

TOL = .25  # mm, world-space curve flattening and contact uncertainty
CLEAR, REVIEW, HIT = '图示范围未见冲突', '待复核', '需调整'


def finite_pair(value):
    x, y = map(float, value)
    if not math.isfinite(x) or not math.isfinite(y):
        raise ValueError('坐标须为有限数值')
    return x, y


def read_lines(ref):
    """Use definition handles as stable paths. Compose matrices before flattening."""
    lines, notes = {}, []

    def visit(insert, matrix, prefix, ancestors):
        block = block_layout(insert)
        key = block.name.casefold()
        if key in ancestors:
            raise ValueError('循环块 ' + key)
        for e in block:
            path = prefix + e.dxf.handle
            kind = e.dxftype()
            if kind == 'INSERT':
                visit(e, e.matrix44() @ matrix, path + '/', ancestors | {key})
            elif kind in PATH_TYPES:
                if e.dxf.get('invisible', 0) or (kind in {'POLYLINE', 'LWPOLYLINE'} and e.has_width):
                    notes.append(path + ' 隐藏或有宽度，须另取边')
                if kind == 'POLYLINE' and not (e.is_2d_polyline or e.is_3d_polyline):
                    notes.append(path + ' 网格未取边')
                    continue
                p = list(make_path(e, segments=4).transform(matrix).flattening(TOL))
                if len(p) < 2:
                    notes.append(path + ' 空路径')
                    continue
                if max(q.z for q in p) - min(q.z for q in p) > TOL:
                    notes.append(path + ' 线条倾出平面')
                lines[path] = LineString([(round(q.x, 7), round(q.y, 7)) for q in p])
            elif kind not in {'WIPEOUT', 'TEXT', 'MTEXT', 'ATTDEF', 'ATTRIB'}:
                notes.append(path + ' 未解码 ' + kind)
    try:
        visit(ref, ref.matrix44(), '', set())
    except (ValueError, TypeError, ArithmeticError, ezdxf.DXFError) as e:
        notes.append(str(e))
    return lines, notes


@dataclass
class Part:
    role: str
    plane: str
    shape: object
    paths: tuple
    basis: str


@dataclass
class Item:
    handle: str
    spec: dict
    anchor: tuple
    face: tuple
    parts: list
    raw: dict
    hull: object
    feet: list
    support: object
    notes: list


def make_item(ref, spec):
    raw, notes = read_lines(ref)
    parts, feet, used = [], [], set()
    for entry in spec.get('parts', []):
        paths = tuple(entry['paths'])
        role, plane = entry['role'], entry.get('plane', '')
        if role not in {'seat', 'foot', 'frame', 'top', 'detail', 'solid'}:
            raise ValueError('未知部位 ' + role)
        if not paths or not all(p in raw for p in paths):
            notes.append('部位缺少实际路径 ' + str(paths))
            continue
        if role == 'foot' and plane != 'floor':
            notes.append('脚点须核定为共同的 floor 平面 ' + str(paths))
            continue
        if not entry.get('basis') or (not plane and role != 'detail'):
            notes.append('部位缺少取边或高度关系依据 ' + str(paths))
            continue
        lines = [raw[p] for p in paths]
        shape = unary_union(lines)
        if role in {'seat', 'top', 'solid', 'foot'}:
            at = entry.get('at')
            if role == 'foot' and at in {'start', 'end'} and len(lines) == 1:
                shape = Point(lines[0].coords[0 if at == 'start' else -1])
            else:
                pieces = list(polygonize(shape))
                if len(pieces) != 1 or not pieces[0].boundary.buffer(TOL).covers(shape):
                    notes.append('此部位未形成唯一明确闭边 ' + str(paths))
                    continue
                shape = pieces[0]
            if role == 'foot':
                feet.append(shape.representative_point())
        parts.append(Part(role, plane, shape, paths, entry['basis']))
        used.update(paths)
    if len(used) != len(raw):
        notes.append('尚有未识别用途的完整图线 ' + str(sorted(set(raw)-used)))
    if not spec.get('basis'):
        notes.append('缺少本图物件辨识依据')
    roles = {p.role for p in parts}
    kind = spec['kind']
    if kind not in {'chair', 'stool', 'table', 'obstacle'}:
        raise ValueError('未知物件类型 ' + kind)
    if kind in {'chair', 'stool', 'table'}:
        if ('top' if kind == 'table' else 'seat') not in roles:
            notes.append('未取得明确的座面或几面')
        base = any(p.role in {'solid', 'frame'} and p.plane in {'floor', '*'} for p in parts)
        if 'foot' not in roles and not (base and spec.get('base_basis')):
            notes.append('缺少脚位或底座构造依据')
    elif 'solid' not in roles:
        notes.append('未识别障碍物的实际实体')
    matrix = ref.matrix44()
    origin = ref.ocs().to_wcs(ref.dxf.insert)
    face = spec.get('forward_local')
    key = ref.dxf.name.removeprefix('MPC_F1_').removesuffix('_MASK').lower()
    if face is None and ref.dxf.name.startswith('MPC_F1_') and key in catalog():
        face = (0, -1)
    if face is None and kind == 'chair':
        notes.append('缺少座椅朝向')
    if face is not None:
        q = matrix.transform_direction((*finite_pair(face), 0))
        if math.hypot(q.x, q.y) <= 1e-9:
            raise ValueError('朝向为零')
        face = (q.x / math.hypot(q.x, q.y), q.y / math.hypot(q.x, q.y))
    support = MultiPoint(feet).convex_hull if len(feet) >= 3 else Polygon()
    if support.geom_type != 'Polygon':
        support = Polygon()
    hull = unary_union(list(raw.values())).convex_hull
    if hull.is_empty:
        notes.append('未读到实线')
    return Item(ref.dxf.handle, spec, (origin.x, origin.y), face, parts, raw,
                hull, feet, support, notes)


def pose(item, dx, dy, angle):
    a = math.radians(angle)
    c, s = math.cos(a), math.sin(a)
    x, y = item.anchor
    m = [c, -s, s, c, x*(1-c)+y*s+dx, y*(1-c)-x*s+dy]
    transform = lambda g: affine_transform(g, m)
    f = item.face
    return replace(item, anchor=(x+dx, y+dy),
                   face=(c*f[0]-s*f[1], s*f[0]+c*f[1]) if f else None,
                   parts=[replace(p, shape=transform(p.shape)) for p in item.parts],
                   raw={k: transform(g) for k, g in item.raw.items()},
                   hull=transform(item.hull), feet=[transform(p) for p in item.feet],
                   support=transform(item.support))


def swept(g, delta):
    if not delta or g.is_empty:
        return g
    if g.geom_type == 'Polygon':
        return sweep(g, delta)
    if g.geom_type == 'Point':
        return LineString([g.coords[0], translate(g, *delta).coords[0]])
    if hasattr(g, 'geoms'):
        return unary_union([swept(child, delta) for child in g.geoms])
    coords = list(g.coords)
    dx, dy = delta
    return unary_union([MultiPoint([a, b, (a[0]+dx, a[1]+dy),
                                    (b[0]+dx, b[1]+dy)]).convex_hull
                        for a, b in zip(coords, coords[1:])])


def components(g):
    if hasattr(g, 'geoms'):
        for child in g.geoms:
            yield from components(child)
    else:
        yield g


def line_cross(a, b):
    """Four alternating rays distinguish a transverse crossing from a touching V."""
    if not a.crosses(b):
        return False
    for point in components(a.intersection(b)):
        if point.geom_type != 'Point':
            continue
        rays = []
        for owner, line in enumerate((a, b)):
            t, length = line.project(point), line.length
            if length <= 2*TOL or (not line.is_closed and min(t, length-t) <= TOL):
                break
            for offset in (-TOL, TOL):
                q = line.interpolate((t+offset) % length if line.is_closed else t+offset)
                rays.append((math.atan2(q.y-point.y, q.x-point.x), owner))
        rays.sort()
        if len(rays) == 4 and all(rays[i][1] != rays[i-1][1] and
                (rays[i][0]-rays[i-1][0]) % math.tau > 1e-7 for i in range(4)):
            return True
    return False


def penetration(a, b):
    """Boundary grazing stays a contact. Exclude sub-tolerance penetrations."""
    if a.disjoint(b):
        return False
    if a.geom_type == b.geom_type == 'Point':
        return a.equals(b)
    for area, other in [(a, b), (b, a)]:
        if area.geom_type in {'Polygon', 'MultiPolygon'}:
            if area.buffer(-TOL).intersects(other):
                return True
    if 'LineString' in a.geom_type and 'LineString' in b.geom_type:
        return any(line_cross(x, y) for x in components(a) for y in components(b))
    return False


def pair(a, b, allowed, delta=None):
    hard, review, projections = [], [], []
    first_hull = swept(a.hull, delta)
    report = {'handles': [a.handle, b.handle], 'hard': hard, 'review': review,
              'declared_projections': projections,
              'candidate_hull_overlap_mm2': round(first_hull.intersection(b.hull).area, 3),
              'axis_box_overlap_mm2': round(first_hull.envelope.intersection(b.hull.envelope).area, 3)}
    intersections = []
    if first_hull.intersects(b.hull):
        for k, g in a.raw.items():
            moved = swept(g, delta)
            for l, h in b.raw.items():
                hit = moved.intersection(h)
                if hit.is_empty:
                    continue
                line_only = moved.geom_type == 'LineString'
                intersections.append({'paths': [k, l], 'type': hit.geom_type,
                                      'crosses': line_cross(moved, h) if line_only else moved.crosses(h),
                                      'common_mm': round(hit.length, 3),
                                      'points_wcs': [list(x.coords[0]) for x in components(hit) if x.geom_type == 'Point'],
                                      'representative_wcs': list(hit.representative_point().coords[0])})
        classified = set()
        for p in a.parts:
            if p.role == 'detail':
                continue
            moved = swept(p.shape, delta)
            for q in b.parts:
                if q.role == 'detail' or moved.disjoint(q.shape):
                    continue
                classified.update((k, l) for k in p.paths for l in q.paths)
                roles = [p.role, q.role]
                rec = {'roles': roles, 'paths': [p.paths, q.paths],
                       'overlap_mm2': round(moved.intersection(q.shape).area, 3)}
                same_plane = p.plane == q.plane or '*' in (p.plane, q.plane)
                if same_plane:
                    if penetration(moved, q.shape):
                        hard.append(dict(rec, reason='同层实体或构件线相交'))
                    else:
                        review.append(dict(rec, reason='同层边缘接触，需识别相贴或测量误差'))
                elif allowed and 'top' in roles:
                    chair_part = q if p.role == 'top' else p
                    if chair_part.role in {'seat', 'foot'} or set(chair_part.paths) <= set(allowed.get('extra_paths', [])):
                        projections.append(dict(rec, basis=allowed['basis']))
                    else:
                        review.append(dict(rec, reason='高几与该部位重叠，尚无该部位可进入桌下的依据'))
                elif not ({p.role, q.role} == {'foot', 'foot'}):
                    review.append(dict(rec, reason='不同高度的部位投影重合，需核构造关系'))
        for hit in intersections:
            k, l = hit['paths']
            if (k,l) in classified:
                continue
            if any(k in p.paths and p.role == 'detail' for p in a.parts):
                continue
            if any(l in q.paths and q.role == 'detail' for q in b.parts):
                continue
            review.append({'reason':'图线相交位置的部位或高度未辨明', 'paths':[k,l]})
        for src, dst, move in ((a, b, delta), (b, a, tuple(-v for v in delta) if delta else None)):
            if dst.spec['kind'] not in {'chair', 'stool'} or dst.support.is_empty:
                continue
            inside = dst.support.buffer(-TOL)
            for i, foot in enumerate(src.feet):
                path = swept(foot, move)
                if path.intersects(inside):
                    rec = {'reason': '脚位进入对方脚点围合区', 'foot_owner': src.handle,
                           'support_owner': dst.handle, 'foot_index': i,
                           'foot_wcs_at_start': list(foot.coords[0]),
                           'start_depth_mm': round(foot.distance(dst.support.boundary), 3) if dst.support.contains(foot) else 0.,
                           'inside_travel_mm': round(path.intersection(inside).length, 3)}
                    basis = dst.spec.get('exclusive_support_basis')
                    if basis:
                        hard.append(dict(rec, basis=basis))
                    else:
                        review.append(rec)
    report['stroke_contacts'] = intersections
    if a.notes or b.notes:
        review.append({'reason': '部位、姿态或图线未完整识别，见物件记录'})
    report['status'] = HIT if hard else REVIEW if review else CLEAR
    return report


def pull_vector(item):
    spec = item.spec.get('pull')
    if spec is None:
        return None
    if not spec.get('basis'):
        raise ValueError('退椅行程须有本图使用依据')
    distance = float(spec['mm'])
    if not math.isfinite(distance) or distance <= 0:
        raise ValueError('退椅距离须为正数')
    if 'wcs' in spec:
        x, y = finite_pair(spec['wcs'])
    elif item.face is not None:
        x, y = (-v for v in item.face)
    else:
        raise ValueError('退椅须登记方向')
    length = math.hypot(x, y)
    if length == 0:
        raise ValueError('退椅向量为零')
    return (x*distance/length, y*distance/length)


def measure_group(items, under, area):
    rows = []
    if area is not None:
        for a in items:
            if a.spec['kind'] != 'obstacle' and a.raw and not area.covers(unary_union(list(a.raw.values()))):
                rows.append({'phase':'原位', 'handles':[a.handle], 'status':HIT,
                             'reason':'完整图线超出登记可用区域'})
    for a, b in combinations(items, 2):
        rows.append(dict(pair(a, b, under.get(frozenset((a.handle, b.handle)))), phase='原位'))
    for a in items:
        try:
            delta = pull_vector(a)
        except ValueError as exc:
            rows.append({'phase':'退椅', 'handles':[a.handle], 'status':REVIEW, 'reason':str(exc)})
            continue
        if not delta:
            continue
        if area is not None and not area.covers(swept(unary_union(list(a.raw.values())), delta)):
            rows.append({'phase': '退椅', 'handles': [a.handle], 'status': HIT,
                         'reason': '完整图线扫出登记可用区域', 'pull_wcs': delta})
        for b in items:
            if a is b:
                continue
            basis = under.get(frozenset((a.handle, b.handle)))
            row = pair(a, b, basis, delta)
            if basis and b.spec['kind'] == 'table':
                seat = unary_union([p.shape for p in a.parts if p.role == 'seat'])
                top = unary_union([p.shape for p in b.parts if p.role == 'top'])
                start = seat.intersection(top).area
                end = translate(seat, *delta).intersection(top).area
                row.update(start_tuck_mm2=round(start, 3), end_tuck_mm2=round(end, 3))
                max_tuck = a.spec['pull'].get('max_end_tuck_mm2')
                if max_tuck is not None and (not math.isfinite(float(max_tuck)) or float(max_tuck) < 0):
                    raise ValueError('允许的终点遮挡面积须为非负有限数')
                if max_tuck is not None and end > float(max_tuck) + TOL*TOL:
                    row['status'] = HIT
                    row['hard'].append({'reason':'终点座面遮挡量超过登记的就坐要求'})
                if start > TOL*TOL and end >= start - TOL*TOL:
                    row['status'] = HIT
                    row['hard'].append({'reason': '登记的退椅动作未减少座面遮挡'})
            rows.append(dict(row, phase='退椅', pull_wcs=delta))
    return rows


def propose(items, group, under, area, implicated):
    repair = group.get('repair', {})
    if area is None or not repair.get('basis'):
        return {'review': '须定位同一区域的可用边界及允许改位的依据，再试移；并回看原图逐件复点。'}
    step, limit = float(repair['step_mm']), float(repair['max_shift_mm'])
    if not (math.isfinite(step) and math.isfinite(limit) and 0 < step <= limit):
        raise ValueError('离散搜索步长或上限无效')
    angles = {0., *map(float, repair.get('angles_deg', []))}
    if not all(math.isfinite(v) for v in angles):
        raise ValueError('转角须为有限数值')
    candidates = []
    directions = [finite_pair(v) for v in repair.get('directions_wcs', [(1,0), (-1,0), (0,1), (0,-1)])]
    if any(math.hypot(*d) == 0 for d in directions):
        raise ValueError('移动方向为零')
    for i, a in enumerate(items):
        if a.handle not in implicated or not a.spec.get('movable') or a.hull.is_empty:
            continue
        vertices = list(a.hull.exterior.coords if a.hull.geom_type == 'Polygon' else a.hull.coords)
        offsets = {(0., 0.)}
        for vx, vy in directions:
            length = math.hypot(vx, vy)
            for j in range(1, math.floor(limit / step)+1):
                offsets.add((step*j*vx/length, step*j*vy/length))
        for angle in sorted(angles):
            c, s = math.cos(math.radians(angle)), math.sin(math.radians(angle))
            for dx, dy in offsets:
                if not (angle or dx or dy):
                    continue
                travel = max(math.hypot(dx+(c-1)*(x-a.anchor[0])-s*(y-a.anchor[1]),
                                        dy+s*(x-a.anchor[0])+(c-1)*(y-a.anchor[1])) for x,y in vertices)
                candidates.append((travel, i, dx, dy, angle))
    for travel, i, dx, dy, angle in sorted(candidates):
        a = pose(items[i], dx, dy, angle)
        if not area.covers(unary_union(list(a.raw.values()))):
            continue
        variant = list(items)
        variant[i] = a
        if all(row['status'] == CLEAR for row in measure_group(variant, under, area)):
            return {'handle': a.handle, 'translation_wcs_mm': [dx, dy], 'rotation_deg': angle,
                    'rotation_pivot_wcs': items[i].anchor, 'max_point_travel_mm': round(travel, 3),
                    'basis': repair['basis'], 'scope': '登记离散姿态中，整组原位与已登记动作均过检的最小单件改位'}
    return {'review': '登记的区域、姿态和行程内未找到可验证的单件改位。按原图露出的座面、背或脚逐件复点，检查是否多计，或联动调整。'}


def check(doc, groups):
    if doc.units != 4:
        raise ValueError('读回 DXF 必须声明毫米')
    refs = {r.dxf.handle:r for r in doc.modelspace().query('INSERT')}
    reports = []
    for group in groups:
        items, missing = [], []
        specs = group['members']
        if len(specs) < 2 or len({s['handle'] for s in specs}) != len(specs):
            raise ValueError('每组至少登记两件不同的物件')
        for spec in specs:
            h = spec['handle']
            if h in refs:
                items.append(make_item(refs[h], spec))
            else:
                missing.append(h)
        area = None
        if group.get('area_wcs'):
            area = Polygon(group['area_wcs'])
            if area.is_empty or not area.is_valid or area.area <= 0 or not group.get('area_basis'):
                raise ValueError('可用区须为有本图依据的有效多边形')
        kinds = {s['handle']:s['kind'] for s in specs}
        under = {}
        for r in group.get('under_table', []):
            key = frozenset([r['table'], r['seat']])
            if not (kinds.get(r['table']) == 'table' and kinds.get(r['seat']) in {'chair','stool'}
                    and r.get('basis') and key not in under):
                raise ValueError('上下遮挡需指定本组桌、座具和唯一的净高构造依据')
            under[key] = r
        inventory = []
        for a in items:
            # Legacy closed-line result is supplementary; it cannot replace open strokes.
            closed = warnings = None
            if not a.notes:
                try:
                    closed, warnings = recover(refs[a.handle])
                except (ValueError, TypeError, ArithmeticError, ezdxf.DXFError) as e:
                    warnings = [str(e)]
            inventory.append({'handle':a.handle, 'block': refs[a.handle].dxf.name,
                'kind':a.spec['kind'], 'basis':a.spec.get('basis'), 'forward_wcs':a.face,
                'pull_registered':bool(a.spec.get('pull')),
                'feet_wcs':[list(p.coords[0]) for p in a.feet], 'support_basis':a.spec.get('exclusive_support_basis'),
                'support_hull_mm2':round(a.support.area,3),
                'parts':[{'role':p.role, 'plane':p.plane, 'basis':p.basis, 'paths':p.paths, 'area_mm2':round(p.shape.area,3)} for p in a.parts],
                'source_paths':list(a.raw), 'closed_outline_mm2':round(closed.area,3) if closed is not None else None,
                'closed_outline_notes':warnings, 'review':a.notes})
        rows = measure_group(items, under, area)
        implicated = {h for row in rows if row['status'] == HIT for h in row['handles']}
        counts = dict(Counter(row['status'] for row in rows))
        result = {'id':group['id'], 'items':inventory, 'unread_handles':missing, 'measurements':rows,
                  'status':HIT if implicated else REVIEW if missing or any(a.notes for a in items) or REVIEW in counts else CLEAR,
                  'counts':counts}
        if implicated:
            result['possible_revision'] = (propose(items, group, under, area, implicated) if not missing else
                                           {'review':'先核对缺失句柄并按原图复点件数。'})
        reports.append(result)
    if not reports:
        raise ValueError('尚未登记组')
    return reports


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('dxf', type=Path)
    p.add_argument('--groups', type=Path, help='作者登记的组、部位和允许姿态 JSON')
    p.add_argument('--inspect', nargs='+', metavar='HANDLE', help='列出块内取边路径和世界坐标')
    args = p.parse_args()
    if bool(args.groups) == bool(args.inspect):
        p.error('选择 --groups 或 --inspect')
    doc = ezdxf.readfile(args.dxf)
    if doc.units != 4:
        p.error('需要毫米 DXF')
    if args.inspect:
        report = {}
        refs = {r.dxf.handle:r for r in doc.modelspace().query('INSERT')}
        for h in args.inspect:
            if h not in refs:
                raise ValueError('无模型空间物件 ' + h)
            raw, notes = read_lines(refs[h])
            report[h] = {'block':refs[h].dxf.name, 'review':notes, 'paths':{
                k:{'bounds_wcs':g.bounds, 'start_wcs':g.coords[0], 'end_wcs':g.coords[-1], 'closed':g.is_ring}
                for k,g in raw.items()}}
    else:
        report = {'sha256':hashlib.sha256(args.dxf.read_bytes()).hexdigest(),
                  'units':'mm', 'curve_tolerance_mm':TOL,
                  'groups':check(doc, json.loads(args.groups.read_text(encoding='utf-8')))}
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError, ezdxf.DXFError) as exc:
        sys.exit(str(exc))
