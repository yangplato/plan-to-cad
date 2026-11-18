"""Screen an editable, native DXF readback alone. This command never opens an image or a ledger."""
import argparse
import json
from collections import defaultdict
from itertools import combinations
from pathlib import Path

import ezdxf
from shapely import union_all
from shapely.affinity import rotate
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import nearest_points, polygonize, unary_union
from shapely.geometry.polygon import orient

from check_registered_frames import expand
from check_seating_groups import read_lines
from presentation import resolve_roles, annotation_layers, SOURCE_LAYER, LEGACY_SOURCE_LAYER
from source_geometry_review import digest, parts, wall_material

EPS = .05  # mm, for numeric coincidence only
SHORT = 120.  # mm, candidate search radius; it is not an acceptance tolerance


def straight_parts(entity):
    if entity.dxftype() == "LINE":
        return [LineString([tuple(entity.dxf.start)[:2], tuple(entity.dxf.end)[:2]])]
    if entity.dxftype() != "LWPOLYLINE":
        return []
    points = list(entity.get_points("xyb"))
    pairs = zip(points, points[1:] + (points[:1] if entity.closed else []))
    return [LineString([a[:2], b[:2]]) for a, b in pairs if not a[2] and a[:2] != b[:2]]


def direction(line):
    x, y, xx, yy = line.bounds
    return 0 if yy-y <= EPS else 1 if xx-x <= EPS else None


def inspect(doc):
    roles = resolve_roles(doc)
    layers = [roles[k] for k in ("bearing", "partition") if roles[k] in doc.layers]
    records, material, incomplete = wall_material(doc, layers, [1, 0, 0, 1, 0, 0])
    if doc.units != 4 or not records:
        incomplete.append("Require millimetres and readable solid wall material")
    for layer in layers:
        if doc.layers.get(layer).is_off() or doc.layers.get(layer).is_frozen():
            incomplete.append(f"{layer}: structural wall layer hidden")
    model, skipped = expand(doc)
    watched = {roles[k] for k in ("window", "ancillary", "door", "bearing", "partition")}
    incomplete.extend(f"{h}: {s}" for h, s, _, layer in skipped if layer in watched)
    for name in (SOURCE_LAYER, LEGACY_SOURCE_LAYER):
        if name in doc.layers:
            layer = doc.layers.get(name)
            if not layer.is_off() and not layer.is_frozen() and any(
                    e.dxftype()=="IMAGE" and e.dxf.layer==name for e in doc.modelspace()):
                incomplete.append(f"{name}: 交付视图仍开底图；关层并复开检查")
    strokes, arcs, small_frames = defaultdict(list), [], []
    isolated_segments = set()
    room_layer = annotation_layers(roles)["rooms"]
    findings = []

    def add(kind, handles, point, value=None, message="", definite=False):
        xy = [round(v, 3) for v in point.coords[0][:2]]
        item = dict(check=kind, handles=sorted(set(handles)), point_mm=xy,
                    status="需返修" if definite else "待判读", reason=message)
        item["id"] = f"{kind}/{'-'.join(item['handles'])}/{xy[0]},{xy[1]}"
        if value is not None:
            item["measured_mm"] = round(value, 3)
        findings.append(item)

    for e, layer, ref in model:
        if layer not in watched | {room_layer}:
            continue
        lines = straight_parts(e)
        if len(lines)==1 and (e.dxftype()=="LINE" or (e.dxftype()=="LWPOLYLINE" and not e.closed)):
            isolated_segments.add(ref)
        if layer == roles["door"] and e.dxftype() == "ARC":
            arcs.append((e, ref))
        strokes[layer].extend((ref, s) for s in lines)
        if (layer == roles["door"] and e.dxftype() == "LWPOLYLINE"
                and e.closed and 3 <= len(lines) <= 8):
            shape = Polygon([s.coords[0] for s in lines])
            if shape.is_valid:
                small_frames.append((ref, shape))

    for arc, ref in arcs:
        center = Point(tuple(arc.ocs().to_wcs(arc.dxf.center))[:2])
        gap = center.distance(material)
        if gap > EPS:
            # A separate jamb casing can carry the hinge. A full swinging leaf cannot.
            radius = arc.dxf.radius
            supported = any(max(g.bounds[2]-g.bounds[0], g.bounds[3]-g.bounds[1]) < radius*.45
                            and center.distance(g) <= EPS and g.distance(material) <= EPS
                            for _, g in small_frames)
            if not supported:
                # A library symbol can model a casing with several short strokes.
                # Follow real contacts from the pivot. Never use the long swinging blade.
                pool = [(r,g) for r,g in strokes[roles["door"]] if g.length < radius*.45
                        and g.distance(center) < radius*.45]
                reached, refs = center, set()
                while pool:
                    linked=[(r,g) for r,g in pool if g.distance(reached)<=EPS]
                    if not linked:
                        break
                    refs.update(r for r,_ in linked)
                    reached=unary_union([reached]+[g for _,g in linked])
                    pool=[item for item in pool if item not in linked]
                supported=len(refs)>=2 and reached.distance(material)<=EPS
            if not supported:
                add("门轴缺支承", [ref], center, gap, "量门轴到实墙，核对独立门框或特殊五金")
        sweep = LineString([tuple(v)[:2] for v in arc.flattening(.5)])
        penetration = sweep.intersection(material.buffer(-EPS)).length
        if penetration > EPS:
            add("门弧穿墙", [ref], center, penetration, "量开启轨迹进入墙内的弧长")

    frame_lines = strokes[roles["window"]] + strokes[roles["ancillary"]]
    for layer in (roles["window"], roles["ancillary"]):
        indexed = defaultdict(list)
        for ref, line in strokes[layer]:
            key = tuple(sorted(tuple(round(v, 5) for v in p) for p in line.coords))
            indexed[key].append(ref)
        for key, refs in indexed.items():
            if len(refs) > 1:
                line = LineString(key)
                add("框线重复", refs, line.centroid, line.length, "同层两条框线完全重合；核实是否共用收口")
    caps = []
    rails = strokes[roles["ancillary"]]
    for ref, line in rails:
        dr = direction(line)
        if dr is None or line.length <= EPS:
            continue
        ends = [r for r, s in rails if s.length > line.length*2 and direction(s) == 1-dr
                and any(line.distance(Point(p)) <= EPS for p in s.coords)]
        if len(set(ends)) >= 2:
            caps.append((ref, line, dr))
    for (ra, a, da), (rb, b, db) in combinations(caps, 2):
        if da != db and a.centroid.distance(b.centroid) <= EPS:
            add("栏杆转角双封头", [ra, rb], a.centroid, message="两道垂直封头在同一轴点重叠，逐根检查内外轨")

    # Parallel door tracks ending at the same short transverse line identify a slide end.
    # Cabinet partitions and external jambs may provide alternative support; never fail by distance alone.
    door_lines = strokes[roles["door"]]
    for ref, cap in door_lines:
        dr = direction(cap)
        if ref not in isolated_segments or dr is None or not 5 < cap.length <= 300:
            continue
        far_stations = defaultdict(set)
        for other, track in door_lines:
            if (other not in isolated_segments or other == ref or direction(track)!=1-dr
                    or track.length<=max(350,cap.length*3)):
                continue
            first, last = sorted(track.coords, key=lambda p:cap.distance(Point(p)))
            if cap.distance(Point(first))<=EPS:
                far_stations[round(last[1-dr], 3)].add(round(first[dr], 3))
        if not any(len(levels)>=2 for levels in far_stations.values()):
            continue
        gap = cap.distance(material)
        if gap>SHORT and not any(s.distance(cap)<=EPS for _,s in frame_lines):
            add("移门轨端缺支承", [ref], cap.centroid, gap, "检查两轨公共端是否接墙、固定侧框或柜侧")
    for ref, line in strokes[room_layer]:
        length = line.intersection(material.buffer(-EPS)).length
        through = [r for r, s in frame_lines if line.crosses(s)]
        if length > EPS or through:
            add("房名引线穿构件", [ref, *through], line.centroid, length, "将房名移入净空或调整引线")
    solids = parts(material, "Polygon")
    for a, b in combinations(solids, 2):
        gap = a.distance(b)
        if EPS < gap < SHORT:
            p, q = nearest_points(a,b)
            add("墙端小间隙", [], LineString([p,q]).centroid, gap, "查门框、材料分界或漏接墙")
    for solid in solids:
        x, y, xx, yy = solid.bounds
        short = min(xx-x, yy-y)
        if short < SHORT/2:
            add("孤立窄墙", [], solid.centroid, short, "核对实体是墙、窗梃还是重复墙皮")
        for ring in [orient(solid).exterior, *orient(solid).interiors]:
            pts = list(ring.coords)[:-1]
            for i, p in enumerate(pts):
                prev, nxt = pts[i-1], pts[(i+1)%len(pts)]
                u, v = (p[0]-prev[0], p[1]-prev[1]), (nxt[0]-p[0], nxt[1]-p[1])
                if u[0]*v[1]-u[1]*v[0] >= -EPS:
                    continue
                legs = [Point(p).distance(Point(q)) for q in [prev,nxt]]
                if EPS < min(legs) and max(legs) <= SHORT+EPS:
                    add("墙角短缺口", [], Point(p), min(legs),
                        f"凹角两边 {legs[0]:.1f}、{legs[1]:.1f} mm，核对柱凸或墙角缺料")
        for ring in [solid.exterior, *solid.interiors]:
            for p, q in zip(ring.coords, list(ring.coords)[1:]):
                edge = LineString([p,q])
                dr = direction(edge)
                if dr is None or not 65 < edge.length <= 300:
                    continue
                # At a wall end measure to the nearest longitudinal frame stroke
                # across the face centre, never to a remote jamb cap.
                # At a segmented inside frame the centre of a real partition has
                # no inner stroke below it. Check contact at BOTH lateral shoulders.
                window = strokes[roles["window"]]
                def shoulder_contact(tip):
                    return any(any(Point(tip).distance(Point(end))<=EPS for end in s.coords)
                               and s.intersection(edge).length<=EPS for _,s in window)
                if all(shoulder_contact(tip) for tip in (p,q)):
                    continue
                options = []
                for ref, s in window:
                    if direction(s) != dr or s.length <= edge.length*2:
                        continue
                    station = edge.centroid.coords[0][dr]
                    if s.bounds[dr]+EPS < station < s.bounds[dr+2]-EPS:
                        d = edge.centroid.distance(s)
                        if d < SHORT:
                            options.append((d,ref,s))
                if options:
                    gap, ref, s = min(options, key=lambda row:row[0])
                    if gap <= EPS:
                        continue
                    near = nearest_points(edge.centroid,s)[1]
                    probe = LineString([edge.centroid,near])
                    if probe.intersection(material).length < EPS:
                        add("短墙端到连续窗", [ref], edge.centroid, gap, "沿墙端中心量到最近的长窗框，看轴线两侧墙面")
    furniture = []
    for ref in doc.modelspace().query("INSERT"):
        if ref.dxf.layer not in (roles["fixed"],roles["movable"]):
            continue
        layer = doc.layers.get(ref.dxf.layer)
        if layer.is_off() or layer.is_frozen():
            incomplete.append(f"{ref.dxf.layer}: restore visible furniture for the blind pass")
            continue
        members, notes = read_lines(ref)
        incomplete.extend(f"{ref.dxf.handle}: {note}" for note in notes)
        if members:
            strokes_in_block = union_all(list(members.values()), grid_size=EPS)
            footprint = unary_union(list(polygonize(strokes_in_block)))
            if not footprint.is_empty:
                angle = ref.dxf.rotation
                envelope = rotate(rotate(strokes_in_block,-angle,origin=(0,0)).envelope,angle,origin=(0,0))
                furniture.append((ref.dxf.handle, footprint, envelope))
            else:
                incomplete.append(f"{ref.dxf.handle}: no closed furniture face; inspect actual outlines")
    for (a, first, outer_a), (b, second, outer_b) in combinations(furniture,2):
        overlap = first.intersection(second)
        if overlap.area<=EPS*EPS:
            for closed, sparse, envelope in ((first,second,outer_b),(second,first,outer_a)):
                if sparse.area<envelope.area*.6 and envelope.buffer(EPS).covers(closed):
                    add("未封闭构件包络含家具", [a,b], closed.representative_point(),
                        message="另一件完整落在轮廓稀疏构件的外包范围内；看柜背、侧板或凹位。仅凭外包不能定错")
                    break
            continue
        # Use closed outlines. Convex hulls would falsely fill the empty side of an L-shaped sofa.
        contained = min(first.area,second.area)-overlap.area <= EPS*EPS
        add("家具包含" if contained else "家具占地相交", [a,b], overlap.representative_point(),
            message=f"封闭轮廓重叠 {overlap.area:.0f} mm²；核对是否同一高度，或为盆嵌台、椅入桌等组合")
    return {"schema":1, "findings":findings, "coverage_issues":incomplete,
            "door_arcs":len(arcs), "wall_pieces":len(records)}


from blind_observations import SCHEMA as FORM_SCHEMA, VISUAL_FIELDS, form, normalize


def review_gaps(report, record, readback_hash, source_recheck=None, source_hash=None):
    """Each accepted exception needs an actual finding and a measured reason."""
    missing = []
    def written(value):
        return isinstance(value,str) and bool(value.strip())
    if (not isinstance(record,dict) or record.get("dxf_sha256") != readback_hash
            or not written(record.get("reviewer"))):
        return ["缺同版成品盲审"]
    for key in VISUAL_FIELDS:
        if not written(record.get(key)):
            missing.append("未查/"+key)
    for item in report["findings"]:
        response = record.get("dispositions",{}).get(item["id"], {})
        if response.get("status") in ("可接受", "已核实") and written(response.get("evidence")):
            continue
        # An explicitly deferred question can acquire source evidence later;
        # keep the original observations and real defects on record.
        later = (source_recheck or {}).get("dispositions",{}).get(item["id"],{})
        if (response.get("status")=="待对图" and written(response.get("evidence"))
                and source_hash and (source_recheck or {}).get("source_sha256")==source_hash
                and source_recheck.get("dxf_sha256")==readback_hash
                and later.get("status")=="可接受" and written(later.get("evidence"))):
            continue
        missing.append(item["id"])
    # Reviewer-added questions may have no automated candidate ID.
    automated = {item["id"] for item in report["findings"]}
    for row in record.get("checks", []):
        if row["id"] in automated or row["kind"] != "needs_source":
            continue
        later = (source_recheck or {}).get("dispositions", {}).get(row["id"], {})
        if not (source_hash and (source_recheck or {}).get("source_sha256") == source_hash
                and source_recheck.get("dxf_sha256") == readback_hash
                and later.get("status") == "可接受" and written(later.get("evidence"))):
            missing.append(row["id"])
    return missing + report["coverage_issues"] + [str(v) for v in record.get("open_defects",[])]


def freeze_review(output, source, report, observations, *, require_form=False):
    """Preserve the first CAD-only observations, including findings still open."""
    if observations.get("dxf_sha256") != digest(source):
        raise ValueError("Reviewer observations refer to another native readback")
    for key in (*VISUAL_FIELDS, "reviewer"):
        if not isinstance(observations.get(key),str) or not observations[key].strip():
            raise ValueError("Record an actual CAD observation for "+key)
    if require_form or "schema" in observations and observations["schema"] == FORM_SCHEMA:
        observations = normalize(observations, report)
    captured = dict(observations, candidate_ids=[r["id"] for r in report["findings"]])
    with output.open("x",encoding="utf-8") as stream:
        json.dump(captured,stream,ensure_ascii=False,indent=2)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("dxf", type=Path)
    p.add_argument("--json", type=Path)
    p.add_argument("--freeze",type=Path,help="CAD-only reviewer observations; --json names a new frozen file")
    p.add_argument("--form", type=Path, help="create a blank V1.6 observation form for this readback")
    p.add_argument("--legacy", action="store_true", help="accept a V1.5 untyped freeze for an older job")
    args = p.parse_args()
    result = inspect(ezdxf.readfile(args.dxf))
    result["dxf_sha256"] = digest(args.dxf)
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.form:
        if args.freeze or args.json:
            p.error("--form takes its own new output filename")
        with args.form.open("x", encoding="utf-8") as stream:
            json.dump(form(result, result["dxf_sha256"]), stream, ensure_ascii=False, indent=2)
    elif args.freeze:
        if not args.json:
            p.error("--freeze requires a new --json output")
        freeze_review(args.json,args.dxf,result,json.loads(args.freeze.read_text(encoding="utf-8")),
                      require_form=not args.legacy)
    elif args.json:
        args.json.write_text(payload,encoding="utf-8")
    else:
        print(payload)
    raise SystemExit(0 if args.form or args.freeze else 2 if result["coverage_issues"] or result["findings"] else 0)
