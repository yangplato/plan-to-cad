#!/usr/bin/env python3
"""Batch audit registered furniture backs against solid walls in a saved millimetre DXF."""
import argparse
from collections import defaultdict
import json
import math
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import ezdxf
from ezdxf import bbox
from ezdxf.xclip import XClip
from shapely.affinity import translate
from shapely.geometry import LineString, Point, Polygon, box

from align_furniture_to_wall import (EPS, dot, evidence, footprints, merge, require,
                                     translation, unit, wallsides)
from build_observed_structure import LAYERS
from check_wall_probes import AREA_ROUNDOFF, numbers, outline, planar_entity
from place_hosted_family import BEDS, NORMALS, _live_entity, _material, live_body, screen_door_swings


def known_family(e):
    if e.dxftype() != "INSERT":
        return None
    name = e.dxf.name.upper()
    if name == "MPC_HOSTED_CABINET_UNIT":
        return "cabinet"
    for k in BEDS:
        if name == "MPC_F1_" + k.upper():
            return k
    if name.startswith("MPC_FIX1_CORNER_WARDROBE_"):
        return "corner_wardrobe"
    return None


def check_ref(e):
    planar_entity(e)
    require(not e.dxf.get("invisible", 0), "物件隐藏，需要核实")
    if e.dxftype() == "INSERT":
        clip = XClip(e)
        block = e.doc.blocks.get(e.dxf.name)
        require(e.mcount == 1 and not e.attribs and block is not None and not block.block.is_xref
                and not block.block.is_xref_overlay and not (clip.has_clipping_path and clip.is_clipping_enabled),
                "阵列、外参、带属性或裁剪块须另核")
        require(abs(e.dxf.insert.z) < EPS, "家具插入点不在平面上")
        require(not any(m.dxf.get("invisible", 0) for m in block), "块含隐藏实体")


def actual_body(doc, e, row):
    check_ref(e)
    name = known_family(e)
    if row.get("physical_outlines"):
        specs = row["physical_outlines"]
        require(all(part["path"][0] == e.dxf.handle for spec in specs
                    for part in spec.get("parts", [spec])), "外轮廓须全部属于同一待测物件")
        body = footprints(doc, specs)
    elif name in BEDS or name == "cabinet":
        body = live_body(e)
    else:
        skipped = []
        parts = (list(e.virtual_entities(skipped_entity_callback=lambda child, reason: skipped.append(reason)))
                 if e.dxftype() == "INSERT" else [e])
        require(not skipped, f"无法恢复全部块实体：{skipped}")
        # Require one real perimeter containing all strokes, never substitute its bounding box.
        strokes = [outline(part) for part in parts]
        rings = [Polygon(line.coords) for line, closed in strokes if closed]
        require(rings, "多实体非目录物件缺闭合外缘，须登记 physical_outlines")
        body = max(rings, key=lambda poly: poly.area)
        require(body.is_valid and all(body.buffer(EPS).covers(line) for line, _ in strokes),
                "有线段落在主轮廓外，须登记完整 physical_outlines")
    require(body.geom_type == "Polygon" and body.is_valid and body.area > AREA_ROUNDOFF,
            "实物外缘须为单一有效的面")
    return body


def vec(value):
    return unit(NORMALS[value] if isinstance(value, str) else numbers(value, 2))


def normal_hint(row):
    require(not ("side" in row and "toward_wall" in row), "side 与 toward_wall 选一个")
    if "toward_wall" in row:
        return vec(row["toward_wall"])
    if "side" in row:
        x, y = vec(row["side"])
        return -x, -y
    return None


def rear(body, n):
    """Only use exposed straight segments on the actual outermost rear plane."""
    points = list(body.exterior.coords)
    t = n[1], -n[0]
    h = max(dot(p, n) for p in points)
    spans = merge(sorted((dot(a, t), dot(b, t))) for a, b in zip(points, points[1:])
                  if max(abs(dot(a, n)-h), abs(dot(b, n)-h)) <= EPS and math.dist(a, b) > EPS)
    return [(tuple(t[i]*s + n[i]*h for i in (0, 1)),
             tuple(t[i]*e + n[i]*h for i in (0, 1))) for s, e in spans]


def rear_faces(e, row, body):
    family = known_family(e)
    n = normal_hint(row)
    if family:
        v = e.matrix44().transform_direction((0, 1, 0))
        require(abs(v.z) < EPS, "块方向不在 XY 平面")
        native = unit((v.x, v.y))
        require(n is None or math.dist(n, native) < 1e-8, "图上登记的床头／柜背方向与原生块相反或倾斜")
        n = native
    require(n is not None, "自定义物件须从原图给 side 或 toward_wall")
    segments = rear(body, n)
    require(segments, "外缘在该方向没有可测直背，点接触须另核")
    groups = [("主背", n, segments, row.get("host"))]
    if row["family"] == "corner_wardrobe":
        # A true L outline has a recessed corner. Its long outside return reaches both end planes.
        require(body.convex_hull.area - body.area > AREA_ROUNDOFF, "原生外缘没有 L 形内角")
        depth = max(dot(p, n) for p in body.exterior.coords)-min(dot(p, n) for p in body.exterior.coords)
        t = n[1], -n[0]
        choices = []
        for direction in (t, (-t[0], -t[1])):
            edges = rear(body, direction)
            if len(edges) == 1 and abs(math.dist(*edges[0])-depth) < EPS:
                choices.append((direction, edges))
        require(len(choices) == 1, "无法唯一确定 L 形柜实际外侧长背")
        turn, edges = choices[0]
        groups.append(("返回翼", turn, edges, row.get("return_host")))
    return groups


def near_point(row, cfg):
    require(not ("near_mm" in row and "near_px" in row), "near_mm 和 near_px 选一个")
    if "near_px" in row:
        u, v = numbers(row["near_px"], 2)
        a, b, d, e, x, y = numbers(cfg["source_to_mm"], 6)
        require(abs(a*e-b*d) > 1e-12, "原图转换不可逆")
        return (a*u+b*v+x, d*u+e*v+y)
    return numbers(row["near_mm"], 2) if "near_mm" in row else None


def cabinet_shape(body, family):
    # Simplify only redundant collinear vertices at CAD arithmetic precision.
    corners = list(body.simplify(EPS).exterior.coords)[:-1]
    if len(corners) != (4 if family == "cabinet" else 6) or body.interiors:
        return False
    for a, b, c in zip(corners[-1:]+corners[:-1], corners, corners[1:]+corners[:1]):
        if abs(dot(unit((a[0]-b[0], a[1]-b[1])), unit((c[0]-b[0], c[1]-b[1])))) > 1e-8:
            return False
    return True


def locate(doc, row, cfg, native):
    point = near_point(row, cfg)
    if row.get("handle"):
        obj = _live_entity(doc, str(row["handle"]).upper())
        if point is not None:
            radius, = numbers([row.get("pick_radius_mm", cfg.get("pick_radius_mm", 250))], 1)
            require(actual_body(doc, obj, row).distance(Point(point)) <= radius,
                    "所选句柄与原图登记位置不符，请核对物件")
    else:
        require(point is not None, "提供当前图 handle 或本图 near_mm/near_px")
        where = Point(point)
        choices = []
        family = row["family"]
        candidates = [e for e in native if known_family(e) == family]
        if family in ("cabinet", "corner_wardrobe") and normal_hint(row) is not None:
            layers = {layer.casefold() for layer in cfg.get("cabinet_layers", [LAYERS["fixed"]])}
            candidates.extend(e for e in doc.modelspace() if e.dxf.layer.casefold() in layers
                              and e.dxftype() in ("INSERT", "LWPOLYLINE", "POLYLINE")
                              and known_family(e) is None)
        for obj in candidates:
            try:
                body = actual_body(doc, obj, {})
            except (ValueError, KeyError, TypeError, IndexError, ezdxf.DXFError):
                continue
            if known_family(obj) or cabinet_shape(body, family):
                choices.append((body.distance(where), obj.dxf.handle))
        choices.sort()
        radius, = numbers([row.get("pick_radius_mm", cfg.get("pick_radius_mm", 250))], 1)
        require(choices and 0 <= choices[0][0] <= radius, "近点附近没有该功能的目录物件")
        require(len(choices) == 1 or choices[1][0]-choices[0][0] > EPS, "近点对应多件物品，请改用句柄")
        obj = doc.entitydb[choices[0][1]]
    require(known_family(obj) in (None, row["family"]), "句柄属于另一种家具")
    require(row["family"] in BEDS | {"cabinet", "corner_wardrobe"}, "未支持的家具功能")
    return obj


def host_segment(ledger, host, n):
    if not host:
        return None
    rows = [w for w in ledger["walls"] if w["id"] == host]
    require(len(rows) == 1, f"{host} 未唯一对应本图墙段")
    w = rows[0]
    a, b = numbers(w["a"], 2), numbers(w["b"], 2)
    require(abs(dot((b[0]-a[0], b[1]-a[1]), n)) <= EPS, "登记的墙向和主背不相容")
    t = (n[1], -n[0])
    return dot(a, n) - float(w["width_mm"])/2, sorted((dot(a, t), dot(b, t)))


def wall_face(ledger, sides, group, options):
    label, n, segments, host = group
    t = (n[1], -n[0])
    height = dot(segments[0][0], n)
    low = min(dot(p, t) for seg in segments for p in seg)
    high = max(dot(p, t) for seg in segments for p in seg)
    chosen = host_segment(ledger, host, n)
    planes = []
    for a, b, inward in sides:
        plane = dot(a, n)
        if math.dist(n, inward) > 1e-8 or abs(plane-dot(b, n)) > EPS:
            continue
        l, r = sorted((dot(a, t), dot(b, t)))
        if chosen:
            expected, (first, last) = chosen
            if abs(expected-plane) > EPS:
                continue
            l, r = max(l, first), min(r, last)
        if r-l > EPS:
            planes.append((plane, l, r))
    # Keep every continuous solid span on the selected plane, including both sides of an opening.
    groups = []
    for h, l, r in sorted(planes):
        if not groups or abs(h-groups[-1][0]) > EPS:
            groups.append((h, []))
        groups[-1][1].append((l, r))
    choices = []
    for h, spans in groups:
        d = min(max(low-r, l-high, 0.) for l, r in spans)
        choices.append((math.hypot(h-height, d), h, merge(spans)))
    choices.sort()
    require(choices and choices[0][0] <= options["wall_search_mm"], f"{label} 附近没有匹配的真实墙面")
    require(len(choices) == 1 or choices[1][0]-choices[0][0] > EPS, f"{label} 有等距墙面，需提供 host")
    _, h, bands = choices[0]
    faces = []
    for idx, (p, q) in enumerate(segments, 1):
        start = dot(p, t)
        faces.append(dict(id=f"{label}{idx}", p=p, q=q, n=n, t=t, length=math.dist(p, q),
                          offset=h-height, spans=[(a-start, b-start) for a, b in bands]))
    return faces


def openings(doc, ledger):
    zones, symbols, handles = {}, {}, set()
    for op in ledger["openings"]:
        ident = op["id"]
        require(ident not in zones, "洞口 id 重复")
        a, b = numbers(op["a"], 2), numbers(op["b"], 2)
        width, = numbers([op.get("wall_thickness_mm", op.get("symbol_depth_mm"))], 1)
        require(width > 0 and math.dist(a, b) > EPS, f"{ident} 缺净洞或墙厚")
        zones[ident] = LineString([a, b]).buffer(width/2, cap_style=2)
        members = op["symbol_handles"]
        require(op["kind"] == "open" or members, f"{ident} 缺原生门窗句柄")
        for h in members:
            obj = _live_entity(doc, h)
            require(h not in handles, "门窗句柄重复")
            handles.add(h)
            if obj.dxftype() == "ARC" and op["kind"] == "swing":
                continue  # checked analytically by screen_door_swings
            line, closed = outline(obj)
            symbols[h] = Polygon(line.coords) if closed else line
    return zones, symbols, handles


def envelope(e):
    ext = bbox.extents([e])
    require(ext.has_data, f"#{e.dxf.handle} 占地未知，请登记真实边界")
    if e.dxftype() == "ARC":
        # Unregistered door arcs may sweep inside their chords. Include their hinge.
        ext.extend([e.dxf.center])
    return box(ext.extmin.x, ext.extmin.y, ext.extmax.x, ext.extmax.y).convex_hull


def overlap(body, other):
    if other.geom_type in ("Polygon", "MultiPolygon"):
        return body.intersection(other).area > AREA_ROUNDOFF
    return body.buffer(-EPS).intersects(other)


def inventory(doc, ledger, cfg, native, bodies, opening_handles):
    hard = {n.casefold() for n in cfg.get("hard_layers", ledger.get("nonwall_fill_layers", []))}
    hard.update(LAYERS[k].casefold() for k in ("fixed", "movable", "ancillary", "door", "window"))
    native_handles = {e.dxf.handle for e in native}
    bounded, uncertain = dict(bodies), {}
    for e in doc.modelspace():
        h = e.dxf.handle
        if h in bounded or h in opening_handles:
            continue
        if h not in native_handles and e.dxf.layer.casefold() not in hard:
            continue
        if e.dxftype() in ("TEXT", "MTEXT", "DIMENSION"):
            continue
        try:
            bounded[h] = actual_body(doc, e, {})
        except (ValueError, TypeError, KeyError, ezdxf.DXFError):
            uncertain[h] = envelope(e)
    return bounded, uncertain


def state(doc, ledger, handle, body, faces, shift, ctx):
    wall, zones, symbols, bodies, uncertain, opts = ctx
    moved = translate(body, *shift)
    details = []
    for face in faces:
        result = evidence(face, shift, opts["max_slide_mm"])
        along = dot(shift, face["t"])
        intervals = [(max(a, along), min(b, along+face["length"])) for a, b in face["spans"]
                     if a <= along+face["length"] and b >= along]
        on_face = abs(face["offset"]-dot(shift, face["n"])) < EPS
        result["actual_endpoints_mm"] = [[p[i]+shift[i] for i in (0, 1)] for p in (face["p"], face["q"])]
        result["endpoints_in_contact"] = [on_face and any(a-EPS <= x <= b+EPS for a, b in intervals)
                                          for x in (along, along+face["length"])]
        result["longest_continuous_contact_mm"] = max((b-a for a, b in intervals), default=0.) if on_face else 0.
        details.append(result)
    hits = [key for key, other in bodies.items() if key != handle and overlap(moved, other)]
    unknown = [key for key, area in uncertain.items() if overlap(moved, area)]
    opening_hits = [key for key, area in zones.items() if overlap(moved, area)]
    stroke_hits = [key for key, shape in symbols.items() if overlap(moved, shape)]
    swings = [s for s in screen_door_swings(doc, ledger, {handle: moved}) if s["collision"]]
    into_wall = moved.intersection(wall).area
    fits = all(r["whole_straight_back"] and all(r["endpoints_in_contact"]) for r in details)
    return dict(fit=bool(faces) and fits and into_wall <= AREA_ROUNDOFF and not any(
        (hits, unknown, opening_hits, stroke_hits, swings)), backs=details, body_in_wall_mm2=into_wall,
        hard_collisions=hits, uncertain_obstacles=unknown, openings=opening_hits, opening_symbols=stroke_hits,
        door_swings=swings)


def shifts(faces, body, handle, ctx):
    _, zones, symbols, bodies, uncertain, opts = ctx
    dx, dy = translation(faces)
    t = faces[0]["t"]
    slides = {0.}
    if all(abs(dot(f["n"], t)) < 1e-8 for f in faces):
        for f in faces:
            for a, b in f["spans"]:
                slides.update((a, b-f["length"]))
        projected = translate(body, dx, dy)
        lo, hi = sorted((min(dot(p, t) for p in projected.exterior.coords),
                         max(dot(p, t) for p in projected.exterior.coords)))
        for objects in (bodies, uncertain, zones, symbols):
            for key, item in objects.items():
                if key == handle or item.distance(projected) > opts["max_slide_mm"]+EPS:
                    continue
                hull = item.convex_hull
                vertices = hull.exterior.coords if hull.geom_type == "Polygon" else hull.coords
                values = [dot(p, t) for p in vertices]
                slides.update((min(values)-hi, max(values)-lo))
    vectors = [(dx+s*t[0], dy+s*t[1]) for s in slides if abs(s) <= opts["max_slide_mm"]+EPS]
    return sorted((v for v in vectors if math.hypot(*v) <= opts["max_move_mm"]+EPS),
                  key=lambda v: (math.hypot(*v), v))


def inspect(doc, ledger, cfg, apply=False):
    opts = {key: numbers([cfg.get(key, default)], 1)[0] for key, default in
            (("max_move_mm", 20), ("max_slide_mm", 10), ("wall_search_mm", 300))}
    require(min(opts.values()) >= 0, "移动和选墙范围不能为负")
    rows = cfg["items"]
    require(isinstance(rows, list) and rows and all(isinstance(r["id"], str) and r["id"].strip() for r in rows)
            and len({r["id"] for r in rows}) == len(rows), "家具 id 需非空且唯一")
    wall = _material(doc, ledger)
    zones, symbols, opening_handles = openings(doc, ledger)
    # Only native wall faces can provide support. Mask registered openings on long faces,
    # without creating artificial jamb faces when a malformed native wall spans a window.
    sides = []
    for a, b, n in wallsides(wall):
        t = (n[1], -n[0])
        l, r = sorted((dot(a, t), dot(b, t)))
        level = dot(a, n)
        spans = [(l, r)]
        for op in ledger["openings"]:
            p, q = op["a"], op["b"]
            if abs(dot(p, n)-dot(q, n)) > EPS:
                continue
            width = op.get("wall_thickness_mm", op.get("symbol_depth_mm"))
            if abs(dot(p, n)-level) > width/2 + EPS:
                continue
            lo, hi = sorted((dot(p, t), dot(q, t)))
            spans = [(x, y) for left, right in spans for x, y in
                     ((left, min(right, lo)), (max(left, hi), right)) if y-x > EPS]
        for l, r in spans:
            sides.append((tuple(t[i]*l+n[i]*level for i in (0, 1)),
                          tuple(t[i]*r+n[i]*level for i in (0, 1)), n))
    native = [e for e in doc.modelspace().query("INSERT") if known_family(e)]
    resolved, issues, source = {}, {}, {}
    data = {}
    for row in rows:
        ident = row["id"]
        try:
            e = locate(doc, row, cfg, native)
        except (ValueError, KeyError, TypeError, IndexError) as exc:
            issues[ident] = str(exc)
            continue
        h = e.dxf.handle
        require(h not in resolved.values(), f"多条原图登记对应同一物件 #{h}")
        resolved[ident] = h
        try:
            source[h] = actual_body(doc, e, row)
            groups = rear_faces(e, row, source[h])
            faces, missing = [], []
            for group in groups:
                try:
                    faces.extend(wall_face(ledger, sides, group, opts))
                except ValueError as exc:
                    for start, end in group[2]:
                        missing.append(dict(id=group[0], actual_endpoints_mm=[start, end],
                                            back_mm=math.dist(start, end), normal_gap_mm=None,
                                            physical_contact_mm=None, longest_continuous_contact_mm=None,
                                            endpoints_in_contact=[None, None], reason=str(exc)))
            data[ident] = row, e, h, source[h], faces, missing
        except (ValueError, KeyError, TypeError, IndexError) as exc:
            issues[ident] = str(exc)
    bodies, uncertain = inventory(doc, ledger, cfg, native, source, opening_handles)
    ctx = wall, zones, symbols, bodies, uncertain, opts
    results = {}
    for ident, (row, e, h, body, faces, missing) in data.items():
        before = state(doc, ledger, h, body, faces, (0, 0), ctx)
        results[ident] = dict(handle=h, family=row["family"], before=before, move_world_mm=[0., 0.])
        if missing:
            before["fit"] = False
            before["backs"].extend(missing)
        if "exception" in row:
            require(isinstance(row["exception"], str) and row["exception"].strip(), "留空例外须有原图或构造依据")
            # The exception excuses only the missing contact, never a clash or penetration.
            blocked = before["body_in_wall_mm2"] > AREA_ROUNDOFF or any(before[k] for k in
                        ("hard_collisions", "uncertain_obstacles", "openings", "opening_symbols", "door_swings"))
            results[ident].update(status="pending" if blocked else "intentional_clearance", reason=row["exception"])
        elif missing:
            results[ident].update(status="pending", reason="部分背边未能定位实墙面")
        elif before["fit"]:
            results[ident]["status"] = "in_contact"
        else:
            results[ident]["status"] = "pending"
    # A neighbour may itself be repaired during this pass. Retry blocked items after a successful move.
    waiting = [k for k, (row, _, _, _, _, missing) in data.items()
               if results[k]["status"] == "pending" and not missing and "exception" not in row]
    while waiting:
        remaining = []
        for ident in waiting:
            row, e, h, body, faces, _ = data[ident]
            try:
                good = []
                for v in shifts(faces, body, h, ctx):
                    length = math.hypot(*v)
                    if good and length > good[0][0]+EPS:
                        break
                    after = state(doc, ledger, h, body, faces, v, ctx)
                    if after["fit"] and all(math.dist(v, prev[1]) > EPS for prev in good):
                        good.append((length, v, after))
                require(good, "允许范围内未找到有连续实墙且无干涉的位置")
                require(len(good) == 1, "存在两种等距离可行位置，需核原图定位")
                _, v, after = good[0]
                bodies[h] = translate(body, *v)
                if apply:
                    e.translate(*v, 0)
                results[ident].update(status="repairable", after=after, move_world_mm=v)
                results[ident].pop("reason", None)
            except (ValueError, TypeError, KeyError, IndexError) as exc:
                results[ident]["reason"] = str(exc)
                remaining.append(ident)
        if len(remaining) == len(waiting):
            break
        waiting = remaining
    for ident, reason in issues.items():
        results[ident] = dict(status="pending", handle=resolved.get(ident), reason=reason)
    unmarked = [dict(handle=e.dxf.handle, family=known_family(e)) for e in native
                if e.dxf.handle not in resolved.values()]
    # Summarise measured offsets at the same precision as the contact report. A shared
    # offset is a repeated CAD measurement, not proof of how its author produced it.
    shared = defaultdict(list)
    for ident, row in results.items():
        if row.get("status") == "intentional_clearance":
            continue
        for face in row.get("before", {}).get("backs", []):
            gap = face["normal_gap_mm"]
            if gap is not None and abs(gap) >= EPS:
                shared[gap].append(dict(item=ident, back=face["id"]))
    repetitions = [dict(normal_gap_mm=g, occurrences=entries) for g, entries in sorted(shared.items())
                   if len({e["item"] for e in entries}) > 1]
    return dict(items=results, unregistered=unmarked, repeated_normal_offsets=repetitions, roundoff_mm=EPS,
                all_registered_resolved=not any(r["status"] == "pending" for r in results.values()),
                hard_handles_checked=sorted(bodies), conservatively_bounded_handles=sorted(uncertain))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("drawing", type=Path)
    p.add_argument("ledger", type=Path, help="同一张图的毫米墙体、洞口及图符登记")
    p.add_argument("job", type=Path, help="从原图登记的家具用途、靠墙方向或例外")
    p.add_argument("--write-new", type=Path)
    p.add_argument("--partial", action="store_true", help="仅发布已复量通过的移动，其余原位保留并报待核")
    args = p.parse_args()
    try:
        read = lambda path: json.loads(path.read_text(encoding="utf-8"))
        cfg, ledger = read(args.job), read(args.ledger)
        doc = ezdxf.readfile(args.drawing)
        dest = args.write_new
        require(not args.partial or dest, "--partial 需同时指定新的输出 DXF")
        require(not dest or (dest.suffix.lower() == ".dxf" and not dest.exists()), "输出须为不存在的 DXF")
        report = inspect(doc, ledger, cfg, apply=bool(dest))
        complete = report["all_registered_resolved"] and not report["unregistered"]
        changed = [i for i, row in report["items"].items() if row["status"] == "repairable"]
        report["saved_reopened"] = False
        if dest and (complete or (args.partial and changed)):
            locked = {**cfg, "items": [{**row, **({"handle": report["items"][row["id"]]["handle"]}
                                if report["items"][row["id"]].get("handle") else {})} for row in cfg["items"]]}
            with TemporaryDirectory(dir=dest.parent) as tmp:
                staged = Path(tmp)/"verified.dxf"
                doc.saveas(staged)
                again = inspect(ezdxf.readfile(staged), ledger, locked)
                for ident, initial in report["items"].items():
                    saved = again["items"][ident]
                    if initial["status"] in ("in_contact", "repairable"):
                        require(saved["status"] == "in_contact", f"{ident} 保存复开后未实际全长贴墙")
                    elif initial["status"] == "intentional_clearance":
                        require(saved["status"] == "intentional_clearance", f"{ident} 留空件新增干涉")
                os.link(staged, dest)
                report.update(saved_reopened=True, saved=str(dest.resolve()), readback=again)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if complete and (report["saved_reopened"] or not changed) else 1
    except (OSError, ValueError, KeyError, TypeError, IndexError, ezdxf.DXFError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
