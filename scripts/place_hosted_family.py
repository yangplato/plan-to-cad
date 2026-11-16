#!/usr/bin/env python3
"""Place a bed or straight cabinet on a registered solid wall face in a millimetre DXF."""
import argparse
import json
import math
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import ezdxf
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import polygonize, unary_union

from align_furniture_to_wall import EPS, merge, require, wallsides
from build_observed_structure import LAYERS, create_structure
from check_wall_probes import AREA_ROUNDOFF, decode, numbers, outline, planar_entity
from place_plan_families import place_family


NORMALS = {"+x": (1, 0), "-x": (-1, 0), "+y": (0, 1), "-y": (0, -1)}
BEDS = {"bed_double", "bed_single"}


def _material(doc, ledger):
    wall, ambiguous, issues, _ = decode(doc, ledger)
    require(not issues and not ambiguous, f"无法可靠读取墙材：{issues}，有歧义的轮廓 {len(ambiguous)}")
    return wall


def _wall_face(ledger, item, wall, source_to_mm):
    hosts = [row for row in ledger["walls"] if row["id"] == item["host"]]
    require(len(hosts) == 1, f"{item['id']}：需指定本图唯一的 host 墙段")
    row = hosts[0]
    n = NORMALS[item["side"]]
    ni = 0 if n[0] else 1  # normal coordinate index; the other is the station
    ti = 1 - ni
    a, b = row["a"], row["b"]
    require(abs(a[ni] - b[ni]) < EPS and abs(a[ti] - b[ti]) > EPS,
            f"{item['id']}：房间侧必须垂直于 host 墙轴")
    face = (a[ni] + b[ni]) / 2 + n[ni] * row["width_mm"] / 2
    require(("near_mm" in item) != ("near_px" in item), "提供一个 near_mm 或 near_px")
    if "near_px" in item:
        u, v = numbers(item["near_px"], 2)
        aa, bb, dd, ee, tx, ty = numbers(source_to_mm, 6)
        near = (aa*u + bb*v + tx, dd*u + ee*v + ty)
    else:
        near = numbers(item["near_mm"], 2)
    station = near[ti]
    start, end = sorted((a[ti], b[ti]))
    spans = []
    for p, q, inward in wallsides(wall):
        if (abs(p[ni] - face) > EPS or abs(q[ni] - face) > EPS
                or inward[ni] * n[ni] > -1 + 1e-10):
            continue
        lo, hi = sorted((p[ti], q[ti]))
        lo, hi = max(lo, start), min(hi, end)
        if hi - lo > EPS:
            spans.append((lo, hi))
    # An opening registered to this host remains unavailable even if a faulty drawing filled it.
    for opening in ledger["openings"]:
        if row["id"] not in opening["hosts"]:
            continue
        x, y = sorted((opening["a"][ti], opening["b"][ti]))
        spans = [(l, h) for lo, hi in spans
                 for l, h in ((lo, min(hi, x)), (max(lo, y), hi)) if h - l > EPS]
    spans = merge(spans)
    selected = [ab for ab in spans if ab[0] - EPS <= station <= ab[1] + EPS]
    require(len(selected) == 1,
            f"{item['id']}：沿墙定位 {station:g} mm 落在洞口、墙外或非裸露墙面；可用区间 {spans}")
    return ti, ni, n, face, station, selected[0]


def _xy(ti, station, face):
    return (station, face) if ti == 0 else (face, station)


def _cabinet(layout, width, depth, back, facing, layer):
    """Draw a straight custom carcass. The closed perimeter is the hard footprint."""
    doc = layout.doc
    name = "MPC_HOSTED_CABINET_UNIT"
    if name not in doc.blocks:
        block = doc.blocks.new(name)
        block.block_record.dxf.units = 4
        block.add_lwpolyline([(-.5, 0), (.5, 0), (.5, -1), (-.5, -1)], close=True,
                             dxfattribs={"layer": "0", "lineweight": 13})
    if layer not in doc.layers:
        doc.layers.new(layer)
    return layout.add_blockref(name, back, dxfattribs={
        "rotation": math.degrees(math.atan2(facing[0], -facing[1])),
        "xscale": width, "yscale": depth, "layer": layer})


def live_body(ref):
    """Recover the hard footprint of a supported bed/carcass from its actual INSERT strokes."""
    require(ref.dxftype() == "INSERT" and ref.mcount == 1, "需要单个家具 INSERT")
    require(ref.dxf.name in {"MPC_HOSTED_CABINET_UNIT", *("MPC_F1_" + k.upper() for k in BEDS)},
            "自动占地仅支持本工具的直柜和目录床。其他对象请选取实际闭合边界")
    parts = []
    for e in ref.virtual_entities():
        planar_entity(e)
        line, _ = outline(e)
        # Independent paths meet after rotation to floating point precision.
        parts.append(LineString([(round(x, 8), round(y, 8)) for x, y in line.coords]))
    strokes = unary_union(parts)
    body = unary_union(list(polygonize(strokes)))
    require(body.geom_type == "Polygon" and body.is_valid and body.area > AREA_ROUNDOFF
            and body.buffer(EPS).covers(strokes), "无法从当前家具线条复原连续实物外轮廓")
    return body


def _live_entity(doc, handle):
    obj = doc.entitydb.get(handle)
    require(obj is not None and obj.is_alive and obj.dxf.owner == doc.modelspace().block_record_handle,
            f"{handle}：模型空间图元已缺失。请在当前图重新登记对象")
    planar_entity(obj)
    return obj


def live_leaves(doc, op):
    """Pair each actual opening arc with its physical leaf, including imported library symbols."""
    count = len(op['leaves'])
    entities = [_live_entity(doc, handle) for handle in op['symbol_handles']]
    require(count in (1, 2), f"{op['id']}：平开门需登记一扇或两扇")
    if not isinstance(op.get('symbol_source'), dict):
        require(len(entities) in (2*count, 2*count+2), f"{op['id']}：核对门扇与开启弧数量")
        result = []
        for leaf, arc in zip(entities[:2*count:2], entities[1:2*count:2]):
            require(leaf.dxftype()=='LWPOLYLINE' and leaf.closed and arc.dxftype()=='ARC',
                    f"{op['id']}：门扇或开启弧类型与登记不符")
            result.append((arc, Polygon(outline(leaf)[0].coords)))
        return result

    # Imported block members can arrive in any order and one leaf can consist of several strokes.
    from ezdxf.path import make_path
    strokes = []
    for entity in entities:
        if entity.dxftype() in ('LINE','LWPOLYLINE','POLYLINE'):
            points = [(round(p.x,6), round(p.y,6)) for p in make_path(entity).flattening(.01)]
            if len(set(points)) > 1:
                strokes.append(LineString(points))
    polygons = list(polygonize(unary_union(strokes)))
    arcs = [e for e in entities if e.dxftype()=='ARC']
    result = []
    for arc in arcs:
        pivot = arc.ocs().to_wcs(arc.dxf.center)
        candidates = []
        for poly in polygons:
            if not any(poly.distance(Point(p.x,p.y))<EPS for p in (arc.start_point,arc.end_point)):
                continue
            # A library's pin centre may sit beside the slab. Use its measured short edge
            # as the maximum offset, rather than assuming a generic hinge clearance.
            corners = list(poly.minimum_rotated_rectangle.exterior.coords)
            thickness = min(math.dist(p,q) for p,q in zip(corners,corners[1:]))
            if poly.distance(Point(pivot.x,pivot.y)) <= thickness + EPS:
                candidates.append(poly)
        if len(candidates)==1:
            result.append((arc, candidates[0]))
    require(len(result)==count, f"{op['id']}：请核准库门的实物扇边与开弧，或选能适配洞端的平面图例")
    return result


def screen_door_swings(doc, ledger, bodies):
    """Check live cabinet/bed polygons against live swept sectors and open leaf outlines."""
    rows = []
    for op in ledger["openings"]:
        if op["kind"] != "swing":
            continue
        for leaf_i,(arc,leaf_poly) in enumerate(live_leaves(doc,op)):
            require(leaf_poly.is_valid and leaf_poly.area > AREA_ROUNDOFF, "开启扇外轮廓无效")
            pivot = arc.ocs().to_wcs(arc.dxf.center)
            require(abs(pivot.z) < EPS, "开启弧须位于 XY 平面")
            center = (pivot.x,pivot.y)
            radius = arc.dxf.radius
            first = arc.start_point if arc.dxf.extrusion.z>0 else arc.end_point
            start = math.atan2(first.y-pivot.y,first.x-pivot.x)
            angle = math.radians((arc.dxf.end_angle - arc.dxf.start_angle) % 360)
            require(radius > 0 and 0 < angle < math.pi, "所选开启弧须为小于 180° 的平面扇形")
            # Clip to the angular wedge. The circle remains analytic: no polygonised chord
            # may hide a cabinet corner just inside its circumference.
            scale = 2*radius / math.sin(angle)
            ux, uy = scale*math.cos(start), scale*math.sin(start)
            vx, vy = scale*math.cos(start+angle), scale*math.sin(start+angle)
            cx, cy = center
            wedge = Polygon([center, (cx+ux, cy+uy), (cx+ux+vx, cy+uy+vy), (cx+vx, cy+vy)])
            for ident, body in bodies.items():
                require(body.geom_type in ("Polygon", "MultiPolygon") and body.is_valid and not body.is_empty,
                        f"{ident}：需提供有效的实际毫米占地面")
                part = body.intersection(wedge)
                members = getattr(part, "geoms", [part])
                areas = [p for p in members if p.area > AREA_ROUNDOFF]
                reach = max((radius - Point(center).distance(p) for p in areas), default=0.)
                overlap = body.intersection(leaf_poly).area
                collision = reach > EPS or overlap > AREA_ROUNDOFF
                rows.append({"opening": op["id"], "leaf": leaf_i + 1, "arc_handle": arc.dxf.handle,
                             "object": ident, "sector_radial_overlap_mm": max(0., reach),
                             "open_leaf_overlap_mm2": overlap, "collision": collision})
    return rows


def check_hosted_family(doc, ledger, item, ref, *, source_to_mm=None, obstacles=None):
    """Re-measure a live INSERT, including its actual back, hard footprint and door conflicts."""
    wall = _material(doc, ledger)
    ti, ni, n, face, station, span = _wall_face(ledger, item, wall, source_to_mm)
    body = live_body(ref)
    family = item["family"]
    expected = "MPC_HOSTED_CABINET_UNIT" if family == "cabinet" else "MPC_F1_" + family.upper()
    require(ref.dxf.name == expected, "指定句柄对应另一家具族")
    front = ref.matrix44().transform_direction((0, -1, 0)).normalize()
    require(front.x*n[0] + front.y*n[1] > 1 - 1e-10, "实物床头或柜背方向与房间侧不符")
    points = list(body.exterior.coords)
    back = min(p[ni] for p in points) if n[ni] > 0 else max(p[ni] for p in points)
    rear_segments = merge(sorted((a[ti], b[ti])) for a, b in zip(points, points[1:])
                          if max(abs(a[ni]-back), abs(b[ni]-back)) <= EPS and abs(a[ti]-b[ti]) > EPS)
    require(rear_segments, "未找到靠墙的实际平直背边")
    length = sum(b-a for a, b in rear_segments)
    gap = n[ni]*(back - face)
    contact = (sum(max(0., min(b, span[1])-max(a, span[0])) for a, b in rear_segments)
               if abs(gap) <= EPS else 0.)
    wall_overlap = body.intersection(wall).area
    collisions = {k: body.intersection(other).area for k, other in (obstacles or {}).items()}
    swing = screen_door_swings(doc, ledger, {item["id"]: body})
    lo, hi = min(p[ti] for p in points), max(p[ti] for p in points)
    offset = (lo+hi)/2 - (sum(span)/2 if item.get("mode") == "full_span" else station)
    span_ok = (item.get("mode") != "full_span" or max(abs(lo-span[0]), abs(hi-span[1])) <= EPS)
    fit = (abs(gap) <= EPS and length-contact <= EPS and abs(offset) <= EPS and span_ok
           and wall_overlap <= AREA_ROUNDOFF and not any(v > AREA_ROUNDOFF for v in collisions.values())
           and not any(r["collision"] for r in swing))
    return {"id": item["id"], "handle": ref.dxf.handle, "fit": fit, "host": item["host"],
            "normal_gap_mm": gap, "actual_rear_mm": length, "wall_contact_mm": contact,
            "rear_segments_mm": [[_xy(ti, a, back), _xy(ti, b, back)] for a, b in rear_segments],
            "normal_axis": "xy"[ni], "host_face_mm": face, "station_axis": "xy"[ti],
            "solid_span_mm": span, "body_span_mm": [lo, hi], "center_offset_mm": offset,
            "body_in_wall_mm2": wall_overlap, "body_in_obstacles_mm2": collisions, "door_swings": swing}


def place_hosted_family(doc, ledger, item, *, source_to_mm=None, layers=None, obstacles=None):
    """Place one supported piece. Return (INSERT, measurement); remove a failed INSERT and raise."""
    ti, _, n, face, station, span = _wall_face(ledger, item, _material(doc, ledger), source_to_mm)
    family = item["family"]
    mode = item.get("mode", "centered")
    require(mode in ("centered", "full_span") and family in {*BEDS, "cabinet"}, "不支持的定位方式或家具族")
    require(mode != "full_span" or (family == "cabinet" and "width_mm" not in item),
            "full_span 按整段净墙绘制直柜；无需提供 width_mm")
    width = span[1] - span[0] if mode == "full_span" else item.get("width_mm")
    depth = item.get("depth_mm")
    back = _xy(ti, sum(span)/2 if mode == "full_span" else station, face)
    layer_map = {**LAYERS, **(layers or {})}
    # Old standalone calls omit role. New schedules classify by use, including
    # freestanding bedside cabinets whose backs touch a real wall.
    role = item.get("role", "fixed" if family == "cabinet" else "movable")
    require(role in ("movable", "fixed", "ancillary"), f"{item['id']}：未知用途图层 {role}")
    layer = layer_map[role]
    if family == "cabinet":
        require(all(isinstance(v, (float, int)) and math.isfinite(v) and v > 0 for v in (width, depth)),
                "直柜需给出正的进深和宽度，full_span 模式按墙段自动取宽")
        ref = _cabinet(doc.modelspace(), width, depth, back, n, layer)
    else:
        ref = place_family(doc.modelspace(), family, back, n, width_mm=width, depth_mm=depth,
                           layer=layer)
    try:
        report = check_hosted_family(doc, ledger, item, ref, source_to_mm=source_to_mm, obstacles=obstacles)
        require(report["fit"], json.dumps(report, ensure_ascii=False))
        return ref, report
    except Exception:
        doc.modelspace().delete_entity(ref)
        raise


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("scene", type=Path, help="原图登记，含 source_to_mm")
    ap.add_argument("items", type=Path, help="各件家具组成的 JSON 数组")
    ap.add_argument("--drawing", type=Path, help="在已建的 DXF 中继续放置")
    ap.add_argument("--ledger", type=Path, help="与该 DXF 匹配的毫米结构台账")
    ap.add_argument("--write-new", type=Path, help="保存到一个尚不存在的 DXF，复开后复量")
    args = ap.parse_args()
    try:
        read = lambda p: json.loads(p.read_text(encoding="utf-8"))
        scene, items = read(args.scene), read(args.items)
        require(bool(args.drawing) == bool(args.ledger), "--drawing 和 --ledger 需配套使用")
        require(isinstance(items, list) and items and len({i["id"] for i in items}) == len(items),
                "提供非空家具数组，每件 id 应唯一")
        doc, ledger = ((ezdxf.readfile(args.drawing), read(args.ledger)) if args.drawing
                       else create_structure(scene))
        affine = scene["source_to_mm"]
        bodies, handles, results = {}, {}, []
        for item in items:
            ref, result = place_hosted_family(doc, ledger, item, source_to_mm=affine,
                                               layers=scene.get("layers"), obstacles=bodies)
            handles[item["id"]] = ref.dxf.handle
            bodies[item["id"]] = live_body(ref)
            results.append(result)
        if args.write_new:
            dest = args.write_new
            require(dest.suffix.lower() == ".dxf" and not dest.exists(), "输出需为新的 DXF 文件")
            with TemporaryDirectory(dir=dest.parent) as directory:
                tmp = Path(directory) / "placed.dxf"
                doc.saveas(tmp)
                again = ezdxf.readfile(tmp)
                bodies = {ident: live_body(_live_entity(again, h)) for ident, h in handles.items()}
                results = [check_hosted_family(again, ledger, item, _live_entity(again, handles[item["id"]]),
                            source_to_mm=affine, obstacles={k: v for k, v in bodies.items() if k != item["id"]})
                           for item in items]
                require(all(r["fit"] for r in results), json.dumps(results, ensure_ascii=False))
                os.link(tmp, dest)
        print(json.dumps({"saved_reopened": bool(args.write_new), "items": results}, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, TypeError, KeyError, IndexError, ezdxf.DXFError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
