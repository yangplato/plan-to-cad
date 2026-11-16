"""Place and remeasure an editable L-shaped cabinet on two observed orthogonal wall faces."""
import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

import ezdxf
from shapely.geometry import LineString, Point, Polygon

from align_furniture_to_wall import EPS, dot, merge, require, wallsides
from build_observed_structure import LAYERS, create_structure
from check_wall_probes import AREA_ROUNDOFF, numbers
from place_hosted_family import NORMALS, _live_entity, _material, screen_door_swings
from place_plan_fixtures import place_fixture
from repair_wall_contacts import actual_body, cabinet_shape, known_family, openings, overlap, rear


@dataclass
class Arm:
    label: str
    hosts: tuple
    normal: tuple  # positive direction into the room
    along: tuple   # positive direction away from the inner corner, parallel to this wall
    face: float    # signed normal coordinate
    length: float
    depth: float
    declared_spans: list


def _arm(ledger, row, label, host_key, segments_key, normal, along, length, depth):
    host = row[host_key]
    ids = row.get(segments_key, [host])
    require(isinstance(ids, (list, tuple)) and host in ids and len(ids) == len(set(ids)),
            f"{label}：{segments_key} 须包含主墙段，每个 ID 只列一次")
    spans = []
    face = None
    for ident in ids:
        matches = [w for w in ledger['walls'] if w['id'] == ident]
        require(len(matches) == 1, f"{label}：墙段 {ident} 须唯一")
        wall = matches[0]
        a, b = numbers(wall['a'], 2), numbers(wall['b'], 2)
        thickness, = numbers([wall['width_mm']], 1)
        h1, h2 = dot(a, normal), dot(b, normal)
        lo, hi = sorted((dot(a, along), dot(b, along)))
        require(abs(h1-h2) <= EPS and hi-lo > EPS and thickness > 0,
                f"{label}：{ident} 的墙轴、厚度与指定房间侧不相容")
        plane = (h1+h2+thickness)/2
        if face is None:
            face = plane
        require(abs(face-plane) <= EPS, f"{label}：墙段 {ident} 与前段的实际室内面不共面")
        spans.append((lo, hi))
    spans = merge(spans)
    require(len(spans) == 1, f"{label}：指定墙段间有断口，沿墙区间 {spans}")
    return Arm(label, tuple(ids), normal, along, face, length, depth, spans)


def _at(corner, direction, distance):
    return tuple(corner[i]+direction[i]*distance for i in (0, 1))


def _context(doc, ledger, item, source_to_mm):
    require(item.get('family', 'corner_wardrobe') == 'corner_wardrobe' and
            item.get('role', 'fixed') == 'fixed', '转角柜须为固定家具 corner_wardrobe')
    side, turn = item['side'], item['return_side']
    require(side in NORMALS and turn in NORMALS, '房间侧用 +x、-x、+y 或 -y')
    n, m = NORMALS[side], NORMALS[turn]
    require(dot(n, m) == 0, '主翼与返回翼的房间侧须互相垂直')
    w, d, b, r = numbers([item[k] for k in ('width_mm', 'depth_mm', 'back_depth_mm',
                                             'return_depth_mm')], 4)
    require(0 < b < d and 0 < r < w, '两翼须有正的柜深及独立伸出段；请核对四项尺寸')
    primary = _arm(ledger, item, '主翼', 'host', 'support_segments', n, m, w, b)
    secondary = _arm(ledger, item, '返回翼', 'return_host', 'return_support_segments', m, n, d, r)
    corner = tuple(n[i]*primary.face + m[i]*secondary.face for i in (0, 1))
    require(('inside_mm' in item) != ('inside_px' in item), '须登记一个局部室内点 inside_px 或 inside_mm')
    if 'inside_px' in item:
        u, v = numbers(item['inside_px'], 2)
        a, b1, d1, e, x, y = numbers(source_to_mm, 6)
        require(abs(a*e-b1*d1) > 1e-12, '原图坐标转换不可逆')
        inside = (a*u+b1*v+x, d1*u+e*v+y)
    else:
        inside = numbers(item['inside_mm'], 2)
    for arm in (primary, secondary):
        require(dot(inside, arm.normal)-arm.face > EPS,
                f"{arm.label}：局部室内点 {inside} 在所选墙面错误一侧或恰在墙面上")
    material = _material(doc, ledger)
    require(not material.covers(Point(inside)) and
            LineString([corner, inside]).intersection(material).length <= EPS,
            f"内角 {corner} 到局部室内点 {inside} 遇墙；重核本房间墙面和见证点")
    return corner, (primary, secondary), material


def _intersect(first, second):
    return merge((max(a, c), min(b, d)) for a, b in first for c, d in second
                 if min(b, d)-max(a, c) > EPS)


def _without(spans, cuts):
    for left, right in merge(cuts):
        spans = [(x, y) for lo, hi in spans for x, y in
                 ((lo, min(hi, left)), (max(lo, right), hi)) if y-x > EPS]
    return spans


def _coordinates(arm, corner, point):
    return dot(point, arm.along)-dot(corner, arm.along)


def _arm_measure(arm, corner, body, material_faces, ledger):
    # Work in signed stations starting from the intersection of the two room-side faces.
    origin = dot(corner, arm.along)
    host_spans = [(a-origin, b-origin) for a, b in arm.declared_spans]
    requested = [(0., arm.length)]
    raw = []
    for a, b, into_wall in material_faces:
        if (dot(arm.normal, into_wall) < -1+1e-10 and
                max(abs(dot(p, arm.normal)-arm.face) for p in (a, b)) <= EPS):
            raw.append(sorted(_coordinates(arm, corner, p) for p in (a, b)))
    cuts = {}
    for op in ledger['openings']:
        if set(arm.hosts).intersection(op['hosts']):
            interval = sorted(_coordinates(arm, corner, op[k]) for k in ('a', 'b'))
            hit = _intersect(requested, [interval])
            if hit:
                cuts[op['id']] = hit[0]
    solids = _without(_intersect(_intersect(requested, raw), host_spans), cuts.values())
    toward_wall = tuple(-v for v in arm.normal)
    actual = rear(body, toward_wall)
    gap = min(dot(p, arm.normal) for p in body.exterior.coords)-arm.face
    edges = merge(sorted(_coordinates(arm, corner, p) for p in seg) for seg in actual)
    contact = _intersect(solids, edges) if abs(gap) <= EPS else []
    missing = _without(requested, contact)
    tip = _at(corner, arm.along, arm.length)
    wing = Polygon([corner, tip, _at(tip, arm.normal, arm.depth), _at(corner, arm.normal, arm.depth)])
    missing_body = wing.difference(body).area
    return {
        'wing': arm.label, 'hosts': list(arm.hosts), 'rear_endpoints_mm': [corner, tip],
        'expected_back_mm': arm.length, 'actual_back_intervals_mm': edges,
        'normal_gap_mm': gap, 'solid_intervals_mm': solids, 'contact_intervals_mm': contact,
        'wall_contact_mm': sum(b-a for a, b in contact), 'unsupported_intervals_mm': missing,
        'openings_on_back_mm': cuts, 'missing_cabinet_area_mm2': missing_body,
        'fit': not missing and missing_body <= AREA_ROUNDOFF,
    }, wing


def _measure(doc, ledger, item, ref, context, obstacles):
    corner, arms, material = context
    require(known_family(ref) == 'corner_wardrobe', f"{item['id']}：句柄须对应原生可编辑转角柜块")
    body = actual_body(doc, ref, {})
    faces = list(wallsides(material))
    results, wings = zip(*(_arm_measure(arm, corner, body, faces, ledger) for arm in arms))
    excess = body.difference(wings[0].union(wings[1])).area
    is_l = cabinet_shape(body, 'corner_wardrobe')
    into_wall = body.intersection(material).area
    zones, symbols, _ = openings(doc, ledger)
    blocked = [k for k, v in zones.items() if overlap(body, v)]
    touched = [k for k, v in symbols.items() if overlap(body, v)]
    obstacles_hit = [k for k, v in (obstacles or {}).items() if overlap(body, v)]
    swings = screen_door_swings(doc, ledger, {item['id']: body})
    fit = (all(r['fit'] for r in results) and is_l and excess <= AREA_ROUNDOFF and
           into_wall <= AREA_ROUNDOFF and not (blocked or touched or obstacles_hit) and
           not any(r['collision'] for r in swings))
    return {'id': item['id'], 'handle': ref.dxf.handle, 'fit': fit, 'corner_mm': corner,
            'wings': list(results), 'has_L_outline': is_l, 'excess_cabinet_area_mm2': excess,
            'body_in_wall_mm2': into_wall, 'blocked_openings': blocked,
            'hit_opening_symbol_handles': touched, 'hit_obstacles': obstacles_hit, 'door_swings': swings}


def check_corner_on_walls(doc, ledger, item, ref, *, source_to_mm=None, obstacles=None):
    """Remeasure the actual transformed block and actual wall material in an open drawing."""
    return _measure(doc, ledger, item, ref, _context(doc, ledger, item, source_to_mm), obstacles)


def place_corner_on_walls(doc, ledger, item, *, source_to_mm=None, layers=None, obstacles=None):
    """Build one editable cabinet; return its INSERT and two measured backs or raise."""
    context = _context(doc, ledger, item, source_to_mm)
    corner, (main, turn), _ = context
    back = _at(corner, main.along, main.length/2)
    local_right = (-main.normal[1], main.normal[0])
    hand = 'left' if dot(main.along, local_right) > 0 else 'right'
    ref = place_fixture(doc.modelspace(), 'corner_wardrobe', back, main.normal, main.length,
                        turn.length, back_depth_mm=main.depth, return_depth_mm=turn.depth,
                        return_side=hand, layer={**LAYERS, **(layers or {})}['fixed'], library=False)
    try:
        result = _measure(doc, ledger, item, ref, context, obstacles)
        require(result['fit'], json.dumps(result, ensure_ascii=False))
        return ref, result
    except Exception:
        doc.modelspace().delete_entity(ref)
        raise


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('scene', type=Path)
    ap.add_argument('items', type=Path, help='从原图独立登记的转角柜数组')
    ap.add_argument('--drawing', type=Path)
    ap.add_argument('--ledger', type=Path, help='与 drawing 配套的毫米结构台账')
    ap.add_argument('--write-new', type=Path, help='复开实量后写新 DXF')
    args = ap.parse_args()
    try:
        require(bool(args.drawing) == bool(args.ledger), '--drawing 和 --ledger 须配套')
        read = lambda path: json.loads(path.read_text(encoding='utf-8'))
        scene, items = read(args.scene), read(args.items)
        require(isinstance(items, list) and items and len(items) == len({i['id'] for i in items}),
                '须提供非空转角柜列表，每件 ID 唯一')
        doc, ledger = ((ezdxf.readfile(args.drawing), read(args.ledger)) if args.drawing
                       else create_structure(scene))
        affine = scene['source_to_mm']
        reports, bodies = [], {}
        for item in items:
            ref, report = place_corner_on_walls(doc, ledger, item, source_to_mm=affine,
                                                layers=scene.get('layers'), obstacles=bodies)
            reports.append(report)
            bodies[item['id']] = actual_body(doc, ref, {})
        if args.write_new:
            dest = args.write_new
            require(dest.suffix.lower() == '.dxf' and not dest.exists(), '请选择尚不存在的 DXF')
            with TemporaryDirectory(dir=dest.parent) as folder:
                stage = Path(folder)/'corner.dxf'
                doc.saveas(stage)
                reopened = ezdxf.readfile(stage)
                refs = {r['id']: _live_entity(reopened, r['handle']) for r in reports}
                bodies = {k: actual_body(reopened, v, {}) for k, v in refs.items()}
                reports = [check_corner_on_walls(reopened, ledger, item, refs[item['id']],
                           source_to_mm=affine, obstacles={k: v for k, v in bodies.items() if k != item['id']})
                           for item in items]
                require(all(r['fit'] for r in reports), json.dumps(reports, ensure_ascii=False))
                os.link(stage, dest)
        print(json.dumps({'saved_reopened': bool(args.write_new), 'items': reports}, ensure_ascii=False, indent=2))
    except (ValueError, KeyError, OSError, TypeError, ezdxf.DXFError) as error:
        ap.exit(1, f'{error}\n')


if __name__ == '__main__':
    main()
