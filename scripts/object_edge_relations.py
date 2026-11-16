"""Position and remeasure named furniture faces against real wall material or another INSERT."""
import argparse
from collections import defaultdict
from itertools import combinations
import json
import math
from pathlib import Path

import ezdxf
import numpy as np
from ezdxf.path import make_path
from shapely.geometry import LineString, Point, Polygon

from align_furniture_to_wall import dot, footprints, merge, require, unit, vertices, wallsides
from check_wall_probes import decode
from screen_furniture_pairs import recover

# Polygon recovery rounds actual modelspace strokes to 0.00001 mm.
TOL = .0001
AREA_TOL = .02
DIRECTIONS = {'+x': (1., 0.), '-x': (-1., 0.), '+y': (0., 1.), '-y': (0., -1.)}
OPPOSITE = {'+x': '-x', '-x': '+x', '+y': '-y', '-y': '+y'}


def normal(side):
    if isinstance(side, str):
        require(side in DIRECTIONS, 'Use a named world side or its two-component outward normal')
        return DIRECTIONS[side]
    require(len(side) == 2 and all(math.isfinite(float(v)) for v in side), 'Invalid outward normal')
    return unit(side)


def edge_job(schedule, drawing_objects, review_reach_mm=75):
    """Resolve semantic IDs once. Store observed intent separately from measured positions."""
    rows = {r['id']: r for r in schedule}
    dining = [r['id'] for r in schedule if r.get('type') == 'dining']
    items = {}
    rules = []
    for ident, entry in drawing_objects.items():
        row = rows.get(ident, {})
        declared = row.get('edge_relations', [])
        require(isinstance(declared, list), f'{ident}: edge_relations must be an array')
        resize = row.get('edge_adjustment')
        if resize is not None:
            require(row.get('role') == 'fixed' and (row['type'] == 'outline' or
                    (row['type'] == 'hosted' and row.get('family') == 'cabinet')),
                    f'{ident}: only an explicitly adjustable fitted cabinet or custom outline can change width')
            require(resize['width_axis'] in ('x', 'y') and resize.get('basis') and
                    0 < resize['min_width_mm'] <= resize['max_width_mm'] < math.inf,
                    f'{ident}: give the adjustable run direction, real width limits and basis')
        owner = next((name for name in dining if ident.startswith(name + '/chair-')), None)
        source = rows[owner] if owner else row
        free = source.get('freestanding_basis')
        if free is not None:
            require(isinstance(free, str) and bool(free.strip()), f'{ident}: describe the source evidence for a freestanding object or dining group')
        installed = row.get('type') in ('hosted', 'hosted_corner') or row.get('role') == 'fixed'
        items[ident] = {'handle': entry['handle'], 'block': entry.get('block'), 'moves_with': owner,
                        'freestanding_basis': free, 'installed': installed,
                        'resize': resize, 'fixed_sides': row.get('fixed_sides', []),
                        'physical_outlines': row.get('edge_physical_outlines'),
                        'free_sides': row.get('free_sides', {}), 'explicit': bool(declared)}
        for side, basis in items[ident]['free_sides'].items():
            require(side in DIRECTIONS and isinstance(basis, str) and basis.strip(), f'{ident}: give the basis for each free side')
        rels = list(declared)
        if row.get('type') in ('hosted', 'hosted_corner'):
            for pref in (('', 'return_') if row['type'] == 'hosted_corner' else ('',)):
                side = OPPOSITE[row[pref + 'side']]
                hosts = row.get(pref + 'support_segments') or [row[pref + 'host']]
                same = [r for r in declared if r['side'] == side and r.get('target', {}).get('walls',
                        [r.get('target', {}).get('wall')]) == hosts]
                if not same:
                    rels.append({'side': side, 'kind': 'touch', 'target': {'walls': hosts},
                                 'basis': '家具表所定实际支承墙面', 'automatic': True})
        seen = set()
        for i, rule in enumerate(rels, 1):
            side = rule['side']; normal(side)
            kind = rule.get('kind', 'touch'); target = rule.get('target', {})
            key = str(rule.get('id', f'{ident}/{i}'))
            require(key not in seen and rule.get('basis'), f'{ident}: give each relation a distinct ID and source basis')
            seen.add(key)
            require(kind in ('touch', 'clearance', 'soft_point', 'unknown'), f'{key}: invalid relation kind')
            require(not isinstance(side, str) or side not in items[ident]['free_sides'], f'{key}: the same face is declared free')
            if kind != 'unknown':
                require(sum(k in target for k in ('wall', 'walls', 'object')) == 1, f'{key}: name one wall face or object edge')
                if 'object' in target:
                    require(target['object'] in drawing_objects and target['object'] != ident, f'{key}: invalid target object')
                gap = float(rule.get('gap_mm', 0))
                require(math.isfinite(gap) and (gap > 0 if kind == 'clearance' else gap == 0), f'{key}: use a positive intentional clearance or zero contact')
                if 'min_shared_mm' in rule:
                    require(math.isfinite(rule['min_shared_mm']) and rule['min_shared_mm'] > 0, f'{key}: invalid length')
            rules.append({**rule, 'id': key, 'object': ident, 'kind': kind})
    for rule in rules:
        target = rule.get('target', {}).get('object'); side = rule['side']
        if target and isinstance(side, str) and rule['kind'] != 'unknown':
            require(OPPOSITE[side] not in items[target]['free_sides'],
                    f"{rule['id']}: target face was also declared free")
    require(len({r['id'] for r in rules}) == len(rules), 'Edge relation IDs must be unique across the plan')
    require(math.isfinite(review_reach_mm) and review_reach_mm >= 0, 'Invalid review reach')
    return {'items': items, 'relations': rules, 'review_reach_mm': review_reach_mm}


def exact_perimeter(ref, body):
    """Use an exact closed DXF outline when it matches the polygonized physical body."""
    for ent in ref.virtual_entities():
        if ent.dxftype() not in ('LWPOLYLINE', 'POLYLINE') or not ent.is_closed:
            continue
        pts = [(v.x, v.y) for v in make_path(ent).flattening(.25)]
        if len(pts) < 4:
            continue
        poly = Polygon(pts)
        if poly.is_valid and poly.boundary.hausdorff_distance(body.boundary) <= TOL:
            return poly
    return body


def curved_strokes(ref):
    for ent in ref.virtual_entities():
        kind = ent.dxftype()
        if kind in ('ARC', 'CIRCLE', 'ELLIPSE', 'SPLINE') or getattr(ent, 'has_arc', False):
            return True
        if kind == 'INSERT' and curved_strokes(ent): return True
    return False


def physical(doc, job):
    bodies, notes = {}, {}
    for ident, spec in job['items'].items():
        ref = doc.entitydb.get(spec['handle'])
        if (ref is None or not ref.is_alive or ref.dxf.owner != doc.modelspace().block_record_handle or
                ref.dxftype() != 'INSERT' or (spec.get('block') and ref.dxf.name != spec['block'])):
            notes[ident] = ['登记的模型空间实物块缺失或已更换']; continue
        if spec.get('physical_outlines'):
            for outline in spec['physical_outlines']:
                for part in outline.get('parts', [outline]):
                    require(part['path'][0] == spec['handle'], f'{ident}: physical outline belongs to another INSERT')
            bodies[ident] = footprints(doc, spec['physical_outlines'])
        else:
            shape, issues = recover(ref)
            if shape is not None:
                bodies[ident] = exact_perimeter(ref, shape)
            if issues:
                notes[ident] = issues
    return bodies, notes


def face(doc, shape, ref_handle, outward, selector=None):
    """Get actual exterior segments on the named face. Never fill a gap between segments."""
    tangent = (-outward[1], outward[0])
    rings = [list(p.exterior.coords) for p in getattr(shape, 'geoms', [shape])]
    if selector:
        coords = vertices(doc, {'path': [ref_handle, *selector['member_path']], **{
            k: selector[k] for k in ('segment',) if k in selector}}, straight=True)
        c = dot(coords[0], outward)
        require(max(abs(dot(p, outward)-c) for p in coords) < TOL, 'Selected member is not perpendicular to the stated normal')
        require(shape.boundary.buffer(TOL).covers(LineString(coords)), 'Selected edge is not on the real physical perimeter')
        middle = LineString(coords).interpolate(.5, normalized=True)
        inside = Point(middle.x - outward[0]*.01, middle.y - outward[1]*.01)
        require(shape.covers(inside), 'Selected edge normal points into the object')
        rings = [coords]
    else:
        c = max(dot(p, outward) for points in rings for p in points)
    spans, points = [], []
    for ring in rings:
        for p in ring:
            if abs(dot(p, outward) - c) <= TOL:
                points.append(dot(p, tangent))
        for a, b in zip(ring, ring[1:]):
            if max(abs(dot(v, outward) - c) for v in (a, b)) <= TOL:
                spans.append(sorted((dot(a, tangent), dot(b, tangent))))
    return c, merge(spans), points


def wall_face(wall_sides, ledger, target, n):
    tangent = (-n[1], n[0]); ids = target.get('walls') or [target['wall']]
    by_id = {w['id']: w for w in ledger['walls']}; planes = []; spans = []
    require(len(set(ids)) == len(ids), 'A wall face contains duplicate segment IDs')
    for key in ids:
        require(key in by_id, f'Wall {key} is missing from the ledger')
        w = by_id[key]; a, b = w['a'], w['b']
        require(abs(dot(a, n) - dot(b, n)) <= TOL, f'{key}: the named wall is not parallel to the object edge')
        c = dot(a, n) - w['width_mm']/2; planes.append(c)
        lo, hi = sorted((dot(a, tangent), dot(b, tangent)))
        # Include terminal caps, if present, without bridging a door or an absent face.
        start_cap, end_cap = (w.get('cap_a_mm', 0), w.get('cap_b_mm', 0))
        if dot(a, tangent) > dot(b, tangent): start_cap, end_cap = end_cap, start_cap
        lo -= start_cap; hi += end_cap
        for p, q, inward in wall_sides:
            if dot(inward, n) < 1 - 1e-9 or max(abs(dot(v, n)-c) for v in (p, q)) > TOL: continue
            u, v = sorted((dot(p, tangent), dot(q, tangent)))
            if min(v, hi) - max(u, lo) > TOL: spans.append((max(u, lo), min(v, hi)))
    require(planes and max(planes) - min(planes) <= TOL, 'Named wall faces do not share a plane')
    require(spans, 'No exposed material found on the named wall face')
    return planes[0], merge(spans), []


def geometry(doc, ledger, job, bodies, wall_sides, rule):
    n = normal(rule['side']); ident = rule['object']; target = rule['target']
    own = face(doc, bodies[ident], job['items'][ident]['handle'], n, rule.get('edge'))
    if 'object' in target:
        other = target['object']; rev = (-n[0], -n[1])
        c, spans, points = face(doc, bodies[other], job['items'][other]['handle'], rev, target.get('edge'))
        opposing = (-c, sorted((-b, -a) for a, b in spans), [-p for p in points])
    else:
        opposing = wall_face(wall_sides, ledger, target, n)
    return own, opposing


def length(spans):
    return sum(b - a for a, b in spans)


def measure(rule, own, target):
    c, a, points = own; d, b, ends = target
    gap = d - c; wanted = rule.get('gap_mm', 0)
    overlap = sum(max(0., min(v, y) - max(u, x)) for u, v in a for x, y in b)
    needed = rule.get('min_shared_mm', min(length(a), length(b)) if 'object' in rule['target'] else length(a))
    point_contact = any(u-TOL <= p <= v+TOL for p in points for u, v in b) or any(abs(p-q) <= TOL for p in points for q in ends)
    valid_span = point_contact if rule['kind'] == 'soft_point' else (needed > TOL and overlap >= needed-TOL)
    return {'normal_gap_mm': round(gap, 6), 'intended_gap_mm': wanted,
            'own_face_mm': round(length(a), 5), 'target_face_mm': round(length(b), 5),
            'projected_shared_mm': round(overlap, 5), 'required_shared_mm': round(needed, 5),
            'physical_shared_mm': round(overlap, 5) if abs(gap) <= TOL else 0.,
            'point_on_target': point_contact, 'status': 'pass' if abs(gap-wanted) <= TOL and valid_span else 'fail'}


def material(doc, ledger):
    wall, unclear, issues, _ = decode(doc, ledger)
    require(not issues and not unclear, f'Cannot identify physical wall material: {issues}, ambiguous={len(unclear)}')
    return wall


def intersections(wall, bodies, job, projections):
    exempt = {frozenset(x['handles']) for x in projections if x.get('basis')}
    seams = {frozenset((rule['object'], rule['target']['object'])) for rule in job['relations']
             if 'object' in rule.get('target', {}) and rule['kind'] != 'unknown'}
    found = []
    for ident, shape in bodies.items():
        area = shape.intersection(wall).area
        if area > AREA_TOL: found.append({'items': [ident], 'type': 'wall', 'overlap_mm2': round(area, 5)})
    for (i, a), (j, b) in combinations(bodies.items(), 2):
        if not a.intersects(b): continue
        area = a.intersection(b).area
        if area <= AREA_TOL: continue
        handles = frozenset((job['items'][i]['handle'], job['items'][j]['handle']))
        if handles in exempt and frozenset((i, j)) not in seams: continue
        found.append({'items': [i, j], 'type': 'objects', 'overlap_mm2': round(area, 5)})
    return found


def review_neighbours(doc, ledger, job, bodies, segments, classified, projections):
    """List nearby real opposing faces for every unclassified side, without inferring intent."""
    exempt = {frozenset(r['handles']) for r in projections if r.get('basis')}
    result = defaultdict(list)
    for side, n in DIRECTIONS.items():
        reach = job.get('review_reach_mm', 75)
        targets = []
        for w in ledger['walls']:
            if abs(dot(w['a'], n)-dot(w['b'], n)) > TOL: continue
            try:
                c, spans, _ = wall_face(segments, ledger, {'wall': w['id']}, n)
            except ValueError:
                continue
            targets.append(({'wall': w['id']}, c, spans))
        for ident, shape in bodies.items():
            rev = (-n[0], -n[1]); c, spans, _ = face(doc, shape, job['items'][ident]['handle'], rev)
            targets.append(({'object': ident}, -c, [(-b, -a) for a, b in spans]))
        for ident, shape in bodies.items():
            if side in classified[ident]: continue
            c, spans, points = face(doc, shape, job['items'][ident]['handle'], n)
            for target, plane, other in targets:
                gap = plane-c
                if gap < -TOL or gap > reach+TOL or target.get('object') == ident: continue
                peer = target.get('object')
                if peer and frozenset((job['items'][ident]['handle'], job['items'][peer]['handle'])) in exempt:
                    continue
                overlap = sum(max(0., min(b, v)-max(a, u)) for a, b in spans for u, v in other)
                if overlap <= TOL and not any(u-TOL <= p <= v+TOL for p in points for u, v in other): continue
                result[ident].append({'side': side, 'target': target, 'normal_gap_mm': round(gap, 5),
                                      'projected_shared_mm': round(overlap, 5)})
    return result


def audit_edges(doc, ledger, job, projections=()):
    wall = material(doc, ledger); segments = list(wallsides(wall))
    bodies, notes = physical(doc, job)
    curves = {ident for ident in bodies if curved_strokes(doc.entitydb[job['items'][ident]['handle']])}
    coverage = [{'object': k, 'geometry_issues': v} for k, v in notes.items()]
    results = []; classified = {k: set(s['free_sides']) for k, s in job['items'].items()}
    collisions = intersections(wall, bodies, job, projections)
    for rule in job['relations']:
        ident = rule['object']; target = rule.get('target', {}); side = rule['side']
        row = {k: rule[k] for k in ('id', 'object', 'side', 'target', 'kind', 'basis') if k in rule}
        if isinstance(side, str) and rule['kind'] != 'unknown':
            classified[ident].add(side)
            if target.get('object'): classified[target['object']].add(OPPOSITE[side])
        if rule['kind'] == 'unknown':
            row.update(status='review', reason='接边用途待原图判读')
        else:
            try:
                require(ident in bodies and target.get('object', ident) in bodies, '缺完整实物占地')
                own, other = geometry(doc, ledger, job, bodies, segments, rule)
                row.update(measure(rule, own, other))
                if rule['kind'] == 'soft_point' and (ident in curves or target.get('object') in curves):
                    row.update(status='review', reason='弧线实物的切点需在原生 CAD 独立捕捉，所列近似值来自离散轮廓')
                if ident in notes or target.get('object') in notes:
                    row.update(status='review', reason='外轮廓未涵盖全部图线，需选择真实实物轮廓')
                shape = bodies[ident]; row['body_in_wall_mm2'] = round(shape.intersection(wall).area, 5)
                row['body_in_target_mm2'] = round(shape.intersection(bodies[target['object']]).area, 5) if 'object' in target else row['body_in_wall_mm2']
                row['penetrations'] = [hit for hit in collisions if ident in hit['items']]
                if row['penetrations'] or row['body_in_target_mm2'] > AREA_TOL: row['status'] = 'fail'
            except (ValueError, KeyError) as e:
                row.update(status='review', reason=str(e))
        results.append(row)
    nearby = review_neighbours(doc, ledger, job, bodies, segments, classified, projections)
    inventory = []
    intended = {r['object'] for r in job['relations']} | {
        r['target']['object'] for r in job['relations'] if 'object' in r.get('target', {})}
    for ident, sides in classified.items():
        spec = job['items'][ident]; unresolved = sorted({r['side'] for r in nearby[ident]})
        inventory.append({'object': ident, 'reviewed_sides': sorted(sides),
                          'freestanding_basis': spec.get('freestanding_basis'), 'contains_curved_strokes': ident in curves,
                          'sides_without_nearby_faces': sorted(set(DIRECTIONS)-sides-set(unresolved))})
        if unresolved:
            coverage.append({'object': ident, 'unclassified_sides': unresolved, 'nearby_faces': nearby[ident],
                             'action': '看原图所指的近邻位置，登记贴合、设计净距或活动自由面。'})
        elif (spec.get('installed') and ident not in intended and not spec.get('freestanding_basis')
              and len(sides) < len(DIRECTIONS)):
            coverage.append({'object': ident, 'action': '固定件尚无支承或接边记录，看原图确认其安装方式。'})
    failed = sum(r['status'] == 'fail' for r in results)
    pending = sum(r['status'] == 'review' for r in results) + len(coverage)
    return {'ok': not (failed or pending or collisions), 'failed_count': failed, 'unresolved_count': pending,
            'object_count': len(job['items']), 'coverage_inventory': inventory, 'relations': results, 'coverage_issues': coverage, 'collisions': collisions,
            'geometry_tolerance_mm': TOL, 'intersection_tolerance_mm2': AREA_TOL}


def apply_edges(doc, ledger, job, projections=()):
    """Solve all named endpoints together. Rigid placements keep their rotation and sizes."""
    from scipy.optimize import lsq_linear
    active = {r['object'] for r in job['relations'] if not r.get('automatic') and r['kind'] != 'unknown'}
    if not active: return {'moves': []}
    rules = [r for r in job['relations'] if r['kind'] != 'unknown' and
             (r['object'] in active or r.get('target', {}).get('object') in active)]
    wall = material(doc, ledger); segments = list(wallsides(wall)); bodies, notes = physical(doc, job)
    var = {}; bounds = {}; stretch = {}; positions = {}; axes = {}
    for ident in sorted(active):
        require(ident in bodies and ident not in notes, f'{ident}: choose a complete real outline before placing its edges')
        for k in ('x', 'y'): var[ident, k] = len(var); bounds[ident, k] = (-np.inf, np.inf)
        box = bodies[ident].bounds; positions[ident] = ((box[0]+box[2])/2, (box[1]+box[3])/2)
        spec = job['items'][ident]; adjust = spec['resize']
        if adjust:
            # Only scale straight adjustable modules on the corresponding physical block axis.
            axis = 'xy'.index(adjust['width_axis']); dim = box[2+axis] - box[axis]
            require(abs(bodies[ident].area-bodies[ident].envelope.area) <= AREA_TOL,
                    f'{ident}: a complex object needs its own parameterized block before resizing')
            ref = doc.entitydb[spec['handle']]; axes_local = [ref.matrix44().transform_direction(v).normalize()
                    for v in ((1, 0, 0), (0, 1, 0))]
            matches = [j for j, u in enumerate(axes_local) if abs(u[axis]) >= 1-1e-10]
            require(len(matches) == 1, f'{ident}: the adjustable axis must follow a block axis')
            require(adjust['min_width_mm']-TOL <= dim <= adjust['max_width_mm']+TOL,
                    f'{ident}: source width is outside the declared adjustable range')
            if adjust['max_width_mm']-adjust['min_width_mm'] > TOL:
                stretch[ident] = (axis, dim); axes[ident] = ('xscale', 'yscale')[matches[0]]
                var[ident, 'width'] = len(var)
                bounds[ident, 'width'] = (adjust['min_width_mm']-dim, adjust['max_width_mm']-dim)
    def coeff(ident, n, c):
        v = np.zeros(len(var)); owner = job['items'][ident].get('moves_with') or ident
        if owner not in active: return v
        v[var[owner, 'x']] = n[0]; v[var[owner, 'y']] = n[1]
        if owner in stretch:
            axis, dim = stretch[owner]
            require(abs(n[axis]) < TOL or abs(abs(n[axis])-1) < TOL,
                    f'{ident}: oblique faces on a resizable part need a parameterized family')
            if abs(n[axis]) > .5:
                v[var[owner, 'width']] = (c-dot(positions[owner], n))/dim
        return v
    equations = []; offsets = []
    for rule in rules:
        ident = rule['object']; n = normal(rule['side']); other = rule['target'].get('object')
        require(ident in bodies and ident not in notes and (other is None or (other in bodies and other not in notes)),
                f"{rule['id']}: select a complete exterior before automatic contact placement")
        own, target = geometry(doc, ledger, job, bodies, segments, rule)
        v = coeff(ident, n, own[0]); goal = target[0] - own[0] - rule.get('gap_mm', 0)
        if other: v -= coeff(other, n, target[0])
        equations.append(v); offsets.append(goal)
    for ident in sorted(active):
        for side in job['items'][ident]['fixed_sides']:
            if side in ('center_x', 'center_y'):
                n = DIRECTIONS['+' + side[-1]]; c = dot(positions[ident], n)
            else:
                n = normal(side); c = face(doc, bodies[ident], job['items'][ident]['handle'], n)[0]
            equations.append(coeff(ident, n, c)); offsets.append(0.)
    matrix = np.array(equations); rhs = np.array(offsets)
    rigid = [col for (i, k), col in var.items() if k != 'width']
    solution = np.zeros(len(var)); solution[rigid] = np.linalg.lstsq(matrix[:, rigid], rhs, rcond=None)[0]
    if np.max(np.abs(matrix@solution-rhs)) > TOL and stretch:
        lo, hi = zip(*(bounds[k] for k in var))
        solution = lsq_linear(matrix, rhs, bounds=(lo, hi), tol=1e-13).x
    require(np.max(np.abs(matrix@solution-rhs)) <= TOL,
            'The declared endpoints cannot meet with these rigid parts and width limits; check the source dimensions')
    moved_ids = active | {k for k, item in job['items'].items() if item.get('moves_with') in active}
    refs = [doc.entitydb[job['items'][k]['handle']] for k in moved_ids]
    original = [(ref, ref.dxf.insert, ref.dxf.xscale, ref.dxf.yscale) for ref in refs]
    try:
        moves = []
        for ident in sorted(active):
            dx, dy = (float(solution[var[ident, k]]) for k in ('x', 'y'))
            item = job['items'][ident]; ref = doc.entitydb[item['handle']]; change = 0.
            if ident in stretch:
                change = float(solution[var[ident, 'width']]); axis, dim = stretch[ident]
                if abs(change) > 1e-10:
                    factor = 1 + change/dim; name = axes[ident]; ref.dxf.set(name, ref.dxf.get(name, 1.) * factor)
                    # Changing INSERT scale acts about its insertion, not its physical centre.
                    shift = (positions[ident][axis] - ref.dxf.insert[axis]) * (1-factor)
                    ref.translate(shift if axis == 0 else 0, shift if axis == 1 else 0, 0)
            ref.translate(dx, dy, 0)
            for child in job['items'].values():
                if child.get('moves_with') == ident: doc.entitydb[child['handle']].translate(dx, dy, 0)
            moves.append({'object': ident, 'center_translation_mm': [dx, dy], 'width_change_mm': change})
        after, _ = physical(doc, job)
        for rule in rules:
            own, target = geometry(doc, ledger, job, after, segments, rule)
            measured = measure(rule, own, target)
            require(measured['status'] == 'pass', f"{rule['id']}: there is insufficient actual shared edge after placement: {measured}")
        conflicts = [r for r in intersections(wall, after, job, projections) if moved_ids.intersection(r['items'])]
        require(not conflicts, f'Edge placement intersects another body or a wall: {conflicts}')
        from place_hosted_family import screen_door_swings
        swings = screen_door_swings(doc, ledger, {k: after[k] for k in moved_ids if k in after})
        require(not any(r['collision'] for r in swings), 'An edge-constrained object intersects a door swing')
        return {'moves': moves}
    except Exception:
        for ref, position, sx, sy in original:
            ref.dxf.insert, ref.dxf.xscale, ref.dxf.yscale = position, sx, sy
        raise


if __name__ == '__main__':
    cli = argparse.ArgumentParser(description=__doc__)
    for name in ('drawing', 'ledger', 'edge_job', 'output'): cli.add_argument(name, type=Path)
    cli.add_argument('--projections', type=Path)
    args = cli.parse_args(); load = lambda p: json.loads(p.read_text(encoding='utf-8'))
    result = audit_edges(ezdxf.readfile(args.drawing), load(args.ledger), load(args.edge_job),
                         load(args.projections) if args.projections else [])
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f"{len(result['relations'])} relations, {result['failed_count']} failed, {result['unresolved_count']} pending")
    raise SystemExit(0 if result['ok'] else 1)
