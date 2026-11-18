"""Source-bound wall policy and geometric checks on the actual millimetre model."""
from collections import defaultdict
from itertools import combinations
import math

from ezdxf.math import Vec2
from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import unary_union

from build_wall_runs import _axis, _opening_cuts, _strip, _wall_footprints
from check_registered_frames import expand, symbols
from curved_window_ends import curve_path, inspect_ends
from source_geometry_review import parts, valid_hash, wall_material

TOL = 0.1  # numerical comparison in model mm
SAMPLE = 0.001  # just inside the jamb cheek; never the shared topology line
WALL_EXCEPTIONS = {"column", "wall_pier", "shaft", "documented_thickness", "intentional_step"}
WINDOW_EXCEPTIONS = {"recessed_frame", "curtain_wall", "independent_frame", "corner_frame"}


def evidence(record, source):
    """Bind a local observation to a finite, positive-area crop on the same original."""
    if not isinstance(record, dict) or not valid_hash(source) or record.get("source_sha256") != source:
        return False
    bounds = record.get("bbox_px")
    return (isinstance(record.get("locator"), str) and bool(record["locator"].strip())
            and isinstance(bounds, (list, tuple)) and len(bounds) == 4
            and all(type(v) in (float, int) and math.isfinite(v) and v >= 0 for v in bounds)
            and bounds[0] < bounds[2] and bounds[1] < bounds[3])


def exception(row, source, allowed):
    record = row.get("geometry_exception")
    return evidence(record, source) and record.get("kind") in allowed


def ordinary_preference(scene, wall):
    spec = scene.get("wall_geometry_v16") or {}
    selected = spec.get("exterior_width_mm")
    if (wall.get("boundary_role") == "exterior" and wall.get("element_kind", "wall") == "wall"
            and not wall.get("geometry_exception") and selected in (200, 240)
            and evidence(wall.get("boundary_evidence"), scene.get("source_sha256"))):
        return selected
    return None


def frame_geometry(op):
    start, end = Vec2(op["a"]), Vec2(op["b"])
    axis = (end - start).normalize()
    return start, end, axis, axis.orthogonal()


def station_range(material, origin, normal, radius):
    cut = LineString([origin-normal*radius, origin+normal*radius])
    hits = [seg for seg in parts(material.intersection(cut), "LineString")
            if seg.distance(Point(origin)) < SAMPLE/2]
    if len(hits) != 1:
        return None
    values = [(Vec2(p)-origin).dot(normal) for p in hits[0].coords]
    if max(abs(min(values)), abs(max(values))) >= radius-SAMPLE:
        return None
    return [min(values), max(values)]


def jamb_faces(op, material):
    start, end, axis, normal = frame_geometry(op)
    radius = max(1500., 4.*float(op["wall_thickness_mm"]))
    return [station_range(material, start-axis*SAMPLE, normal, radius),
            station_range(material, end+axis*SAMPLE, normal, radius)]


def bind_window(op, material, source):
    """A straight ordinary window uses the two real cheek faces, in signed a→b coordinates."""
    op.pop("frame_face_offsets_mm", None)
    if exception(op, source, WINDOW_EXCEPTIONS) or not op.get("hosts"):
        return
    faces = jamb_faces(op, material)
    if None in faces or any(abs(a-b) > TOL for a, b in zip(*faces)):
        width = float(op["wall_thickness_mm"])
        op.setdefault("lines_mm",[-width/2,-width/6,width/6,width/2])
        op.setdefault("symbol_depth_mm",width)
        op["frame_binding_issue"] = "两端墙面不一致或未探到。确认柱头、墙垛及框接框收口。"
        return
    low, high = faces[0]
    depth = high-low
    op["frame_face_offsets_mm"] = [low, high]
    op["lines_mm"] = [low, low+depth/3, low+2*depth/3, high]
    op["symbol_depth_mm"] = depth


def shape_outside(expected, actual):
    """Length-independent tolerance applies to boundary displacement, never gross overlap area."""
    return expected.difference(actual.buffer(TOL, join_style=2)).area


class Results:
    def __init__(self):
        self.rows = []

    def add(self, ident, status, **data):
        self.rows.append(dict(id=ident, status=status, **data))

    def test(self, ident, ok, **data):
        self.add(ident, "pass" if ok else "fail", **data)

    def report(self):
        failed = [r["id"] for r in self.rows if r["status"] == "fail"]
        pending = [r["id"] for r in self.rows if r["status"] == "pending"]
        return dict(status="fail" if failed else "pending" if pending else "pass",
                    failures=failed, pending=pending, checks=self.rows, tolerance_mm=TOL)


def useful_intervals(wall, walls, openings):
    """Return solid stations excluding openings and cross walls/columns; use every remaining run."""
    ti, n, a, b = _axis(wall)
    lo, hi = sorted((a, b))
    breaks = {lo, hi}
    excluded = []
    for op in openings:
        if wall["id"] in op.get("hosts", []):
            _, _, first, last = _axis(op)
            excluded.append(sorted((first, last)))
    for peer in walls:
        if peer["id"] == wall["id"]:
            continue
        j, pn, first, last = _axis(peer)
        half = peer["width_mm"]/2
        if j != ti:
            if min(first,last)-wall["width_mm"]/2 <= n <= max(first,last)+wall["width_mm"]/2:
                excluded.append((pn-half, pn+half))
        elif abs(pn-n) < (peer["width_mm"] + wall["width_mm"])/2:
            excluded.append(sorted((first, last)))
    for a,b in excluded:
        breaks.update([max(lo,min(hi,a)), max(lo,min(hi,b))])
    ticks = sorted(breaks)
    return [(a,b) for a,b in zip(ticks,ticks[1:]) if b-a>2*SAMPLE
            and not any(c < (a+b)/2 < d for c,d in excluded)]


def measured_local_thickness(wall, ledger, source):
    from dimension_grid import GridProjection
    from wall_thickness import pixel_selection
    ev = wall.get("thickness_evidence") or {}
    if ev.get("kind") in ("printed_dimension", "building_document"):
        return (type(ev.get("value_mm")) in (float, int)
                and abs(ev["value_mm"] - wall["width_mm"]) <= TOL)
    reg = ledger.get("geometry_registration_v16") or {}
    scale = (ledger.get("grid_registration") or reg.get("grid_registration") or {}).get("scale_provenance")
    if not (scale == "printed" or isinstance(scale, dict) and scale.get("kind") == "known_length"):
        return False
    if ev.get("kind") != "calibrated_pixels":
        return False
    try:
        projection = GridProjection(reg)
        measurement = pixel_selection(wall, source, projection)
        points = ev["face_points_px"]
        crop = box(*wall["geometry_exception"]["bbox_px"])
        if not all(crop.covers(Point(p)) for p in points):
            return False
        section = LineString([projection.world(p) for p in points])
        wall_axis = LineString([wall["a"], wall["b"]])
        return measurement["candidates_mm"] == [wall["width_mm"]] and section.intersects(wall_axis)
    except (ValueError, KeyError, TypeError):
        return False


def wall_sections(out, ledger, material, source):
    walls, openings = ledger["walls"], ledger["openings"]
    standard = (ledger.get("wall_geometry_v16") or {}).get("exterior_width_mm")
    if standard is None:
        out.add("exterior/standard", "pending")
    else:
        out.test("exterior/standard", standard in (200,240))
    exterior = []
    measures = {}
    for wall in walls:
        key, role = wall["id"], wall.get("boundary_role")
        valid = evidence_at(wall.get("boundary_evidence"), source, wall, ledger)
        special = (exception(wall, source, WALL_EXCEPTIONS) and
                   evidence_at(wall["geometry_exception"],source,wall,ledger))
        if role not in ("interior", "exterior", "detail") or not valid:
            out.add("classification/"+key,"pending",reason="须对照原图分别登记内墙、外墙或局部构造")
        exc = wall.get("geometry_exception") or {}
        if special:
            kind = exc["kind"]
            if kind == "column" and wall.get("element_kind")!="column":
                special = False
            elif kind == "documented_thickness":
                special = measured_local_thickness(wall, ledger, source)
            if special and not evidence_at(exc,source,wall,ledger,whole=True):
                special = False
        if (role == "detail" or wall.get("geometry_exception") or wall.get("element_kind") == "column") and not special:
            out.add("exception/"+key,"pending",reason="局部构造缺同版原图位置和类别")
        ordinary = role == "exterior" and not special
        if ordinary:
            ev = wall.get("thickness_evidence") or {}
            review = wall.get("wall_thickness_review") or {}
            if not ((ev.get("kind") in ("calibrated_pixels","printed_dimension") and ev.get("source_sha256")==source
                     and valid_hash(source) and ev.get("locator")) or
                    (ev.get("kind")=="building_document" and valid_hash(ev.get("source_sha256")) and ev.get("source_ref"))
                    or review.get("basis")=="assumed_wall_thickness"):
                out.add("thickness-evidence/"+key,"pending")
            exterior.append(key)
            if standard in (200,240):
                out.test("exterior/"+key, abs(wall["width_mm"]-standard)<=TOL,
                         scheduled_mm=wall["width_mm"], standard_mm=standard)
        ti, n, _, _ = _axis(wall)
        normal = Vec2(0,1) if ti==0 else Vec2(1,0)
        expected = (standard if ordinary and standard in (200,240) else wall["width_mm"])
        rows = []
        for a,b in useful_intervals(wall, walls, openings):
            for fraction in (.15,.5,.85):
                t = a+(b-a)*fraction
                origin = Vec2(t,n) if ti==0 else Vec2(n,t)
                measured = station_range(material, origin, normal, max(1500.,4*expected))
                rows.append(dict(station_mm=t, faces_mm=[n+v for v in measured] if measured else None))
        measures[key] = rows
        if rows:
            out.test("section/"+key, all(r["faces_mm"] and
                max(abs(v-p) for v,p in zip(r["faces_mm"],[n-expected/2,n+expected/2])) <= TOL for r in rows),
                sections=rows)
        else:
            out.add("section/"+key, "pending", reason="整段受洞口或节点占用，需提供独立实墙截面")
    if not exterior:
        out.add("exterior/inventory","pending",reason="全户缺少普通外墙分类")
    return measures


def controls(out, ledger, measured):
    registered = (ledger.get("grid_registration") or {}).get("controls", {})
    walls = {w["id"]:w for w in ledger["walls"]}
    for ident, row in registered.items():
        for bind in row.get("bindings", []):
            wall, face = bind.get("wall"), bind.get("at")
            if wall not in walls or face not in ("min_face","max_face","center"):
                continue
            coords = [r["faces_mm"] for r in measured[wall] if r["faces_mm"]]
            key = f"control/{ident}/{wall}/{face}"
            if not coords:
                out.add(key, "pending")
                continue
            actual = [lo if face=="min_face" else hi if face=="max_face" else (lo+hi)/2 for lo,hi in coords]
            out.test(key, max(abs(v-row["mm"]) for v in actual) <= TOL,
                     expected_mm=row["mm"], actual_mm=actual)


def bind_controls(scene):
    """Add source-proven memberships to an existing control before the joint solver runs."""
    controls = scene.get("dimension_grid", {}).get("controls", {})
    if not controls:
        return
    for group in (scene.get("wall_geometry_v16") or {}).get("continuous_faces", []):
        if not evidence(group, scene.get("source_sha256")):
            continue
        control = controls.get(group.get("control"))
        if not control or control["kind"] not in ("wall_center", "wall_face", "column_edge"):
            raise ValueError(f"{group.get('id')}: continuous run requires an existing physical control")
        members = group.get("members") or []
        if len({m["wall"] for m in members}) < 2:
            raise ValueError("A continuous group needs at least two distinct named walls")
        existing = control.setdefault("bindings", [])
        for member in members:
            at = member["at"]
            if at not in ("center","min_face","max_face") or (at=="center") != (control["kind"]=="wall_center"):
                raise ValueError("A continuous face and its control must refer to the same physical feature")
            pair = dict(wall=member["wall"], at=at)
            if pair not in existing:
                existing.append(pair)


def expected_material(walls, openings):
    footprints, axes = _wall_footprints(walls)
    cuts = defaultdict(list)
    for hosts, cutter in _opening_cuts([op for op in openings if op.get("hosts")], axes).values():
        for key in hosts:
            cuts[key].append(cutter)
    return unary_union([poly.difference(unary_union(cuts[key])) for key, (_,poly) in footprints.items()])


def model_topology(out, walls, material, records, openings, source, ledger):
    expected = expected_material(walls, openings)
    from curved_wall_returns import footprint
    for row in ledger.get("curved_wall_returns",[]):
        try:
            expected=expected.union(footprint(ledger,row)[0])
        except (TypeError,KeyError,ValueError,ArithmeticError) as exc:
            out.add("wall-envelope/return/"+row["id"],"pending",reason=str(exc))
    for name, a, b in [("missing", expected, material), ("extra", material, expected)]:
        area = shape_outside(a, b)
        out.test("wall-envelope/"+name, area < 1e-6, area_beyond_tolerance_mm2=area)
    for a,b in combinations(records, 2):
        overlap = a["shape"].intersection(b["shape"])
        if overlap.area > 1e-6:
            out.add("material-overlap/"+a["handles"][0]+"/"+b["handles"][0], "fail",
                    layers=[a["layer"],b["layer"]], overlap_mm2=overlap.area)
    for a,b in combinations(walls,2):
        ta,na,a1,a2 = _axis(a)
        tb,nb,b1,b2 = _axis(b)
        a1,a2=sorted((a1,a2)); b1,b2=sorted((b1,b2))
        key = "joint/"+a["id"]+"/"+b["id"]
        if ta==tb:
            overlap = min(a2,b2)-max(a1,b1)
            if (overlap>TOL and abs(na-nb)<=TOL and
                    not any(exception(w,source,WALL_EXCEPTIONS) and
                            evidence_at(w["geometry_exception"],source,w,ledger,whole=True) for w in (a,b))):
                out.add(key+"/axis-overlap","fail",overlap_mm=overlap,
                        reason="同轴普通墙段在材料分界重复占据长度")
            ends = min(abs(a2-b1), abs(b2-a1))
            edges = abs(na-nb) + abs(a["width_mm"]-b["width_mm"])/2
            if ends<=25 and abs(na-nb)<=max(a["width_mm"],b["width_mm"])/2:
                # Only ordinary equal-width runs are expected to share BOTH faces.
                # Unequal walls can have one continuous face; their shared control is checked separately.
                if abs(a["width_mm"]-b["width_mm"])<=TOL and not any(exception(w,source,WALL_EXCEPTIONS) for w in (a,b)):
                    if ends>TOL or edges>TOL:
                        out.add(key,"pending",end_displacement_mm=ends,face_displacement_mm=edges,
                                reason="邻墙存在错台或未共端，按同一物理控制或局部原图构造确认")
            continue
        if a1-25<=nb<=a2+25 and b1-25<=na<=b2+25:
            aend = min(abs(nb-a1), abs(nb-a2))<=25
            bend = min(abs(na-b1), abs(na-b2))<=25
            # Local material rectangle at an L/T/+ must be present end to end.
            p = (nb,na) if ta==0 else (na,nb)
            dx,dy = (b["width_mm"],a["width_mm"]) if ta==0 else (a["width_mm"],b["width_mm"])
            joint = box(p[0]-dx/2,p[1]-dy/2,p[0]+dx/2,p[1]+dy/2)
            # An adjacent registered opening may remove part of a pier, but cannot justify a void at the node.
            for op in openings:
                if not set(op.get("hosts",[])).intersection((a["id"],b["id"])):
                    continue
                ti,n,first,last = _axis(op)
                joint = joint.difference(_strip(ti,n,min(first,last),max(first,last),op["wall_thickness_mm"]))
            missing = shape_outside(joint, material)
            if not joint.is_empty:
                out.test(key,missing<1e-6,kind="L" if aend and bend else "T" if aend or bend else "+",
                         point_mm=p,missing_mm2=missing)
    from professional_review import wall_steps
    approved = (ledger.get("wall_geometry_v16") or {}).get("step_details", [])
    for step in wall_steps(material,25):
        confirmed = any(evidence(r,source) and r.get("kind") in WALL_EXCEPTIONS and
                        step_matches(r, step, ledger) for r in approved)
        out.add(step["id"], "pass" if confirmed else "pending", ends_mm=step["ends_mm"], offset_mm=step["offset_mm"])


def step_matches(detail, step, ledger):
    """Match independently read original end pixels, including reversed order."""
    from dimension_grid import GridProjection
    if not ledger.get("geometry_registration_v16"):
        return False
    points = detail.get("ends_px")
    radius = detail.get("edge_uncertainty_px")
    if not (isinstance(points,list) and len(points)==2 and type(radius) in (float,int) and 0<=radius<=2):
        return False
    projection = GridProjection(ledger["geometry_registration_v16"])
    bounds = detail["bbox_px"]
    for p in points:
        if len(p)!=2 or not (bounds[0]<=p[0]<=bounds[2] and bounds[1]<=p[1]<=bounds[3]):
            return False
    for sequence in (points,points[::-1]):
        if all(box(*rect_bounds(projection.world([p[0]-radius,p[1]-radius]),
                                projection.world([p[0]+radius,p[1]+radius]))).buffer(TOL).covers(Point(end))
               for p,end in zip(sequence,step["ends_mm"])):
            return True
    return False


def rect_bounds(p,q):
    return min(p[0],q[0]), min(p[1],q[1]), max(p[0],q[0]), max(p[1],q[1])


def read_symbols(doc, ledger, out):
    layers = ledger.get("geometry_layers_v16") or {}
    roles = {layers.get(k,"").casefold() for k in ("door","window","ancillary")}
    entities, issues = expand(doc)
    curve_groups, measured = defaultdict(list), set()
    for entity, layer, ref in entities:
        if layer != layers.get("window") or entity.dxftype() not in ("ARC", "ELLIPSE"):
            continue
        try:
            path = curve_path(entity, TOL)
        except (ValueError, ArithmeticError):
            path = None
        if path is not None:
            curve_groups[ref.split("/")[0]].append(path)
            measured.add(ref)
    strokes, arcs = symbols(entities,roles,issues)
    groups = defaultdict(list)
    for row in strokes:
        groups[row.ref.split("/")[0].split(":")[0]].append(
            LineString([(row.start.real,row.start.imag),(row.end.real,row.end.imag)]))
    for op in ledger["openings"]:
        op_handles = set(op.get("symbol_handles",[]))
        role = {"window":"window","swing":"door","sliding":"door","rail":"ancillary"}.get(op["kind"])
        if role and any(h not in doc.entitydb or doc.entitydb[h].dxf.layer != layers.get(role) for h in op_handles):
            out.add("symbol-layer/"+op["id"],"fail",reason="开口符号句柄缺失或归属图层错误")
        if any(str(issue[0]).split("/")[0].split(":")[0] in op_handles
               and not (issue[0] in measured and issue[1] == "nonstandard symbol ELLIPSE") for issue in issues):
            out.add("symbol-decode/"+op["id"],"pending",reason="登记的符号有未能解码的图元")
    # Unlisted real symbols must not disappear when the schedule omits a window or door.
    handles = {h for op in ledger["openings"] for h in op.get("symbol_handles",[])}
    for e in doc.modelspace():
        if e.dxf.layer in {layers.get("window"),layers.get("door")} and e.dxf.handle not in handles:
            out.add("unregistered-opening/"+e.dxf.handle,"pending",layer=e.dxf.layer)
    straight = {op["id"]:[s for h in op.get("symbol_handles",[]) for s in groups[h]]
                for op in ledger["openings"]}
    curves = {op["id"]:[s for h in op.get("symbol_handles",[]) for s in curve_groups[h]]
              for op in ledger["openings"]}
    return straight, curves


def line_bounds(lines, start, axis, normal):
    return [[((Vec2(p)-start).dot(axis),(Vec2(p)-start).dot(normal)) for p in s.coords] for s in lines]


def is_cap(line, station):
    return all(abs(p[0]-station)<=TOL for p in line) and abs(line[0][1]-line[-1][1])>TOL


def aperture(out, op, lines, all_lines, material, source, ledger):
    key = op["id"]
    start, end, axis, normal = frame_geometry(op)
    span = start.distance(end)
    local = line_bounds(lines, start, axis, normal)
    special = exception(op, source, WINDOW_EXCEPTIONS) and evidence_at(op["geometry_exception"],source,op,ledger)
    if op.get("geometry_exception") and not special:
        out.add("frame-exception/"+key,"pending")
    if op["kind"]=="window" and not op.get("hosts") and not special:
        out.add("free-frame/"+key,"pending",reason="独立框须有原图局部依据")
    faces = jamb_faces(op, material)
    if not lines:
        out.add("symbol/"+key,"fail", reason="原生回读缺少该开口的线形符号")
        return
    if op["kind"]=="window" and not special:
        # Count distinct full-span straight rails; a shifted or duplicated rail fails even with unchanged total depth.
        rails = sorted((line[0][1]+line[-1][1])/2 for line in local if
                       abs(line[0][1]-line[-1][1])<=TOL and
                       abs(min(p[0] for p in line))<=TOL and abs(max(p[0] for p in line)-span)<=TOL)
        out.test("four-rails/"+key, len(rails)==4 and all(b-a>TOL for a,b in zip(rails,rails[1:]))
                 and len(local)==6, actual_offsets_mm=rails)
        for i, station in enumerate((0,span)):
            cheek = faces[i]
            label = f"flush/{key}/{'ab'[i]}"
            if cheek is None:
                out.add(label,"pending",reason="此端缺唯一实际墙面，需确认框接框或局部节点")
            else:
                caps = [sorted((seg[0][1],seg[-1][1])) for seg in local if is_cap(seg,station)]
                ok = len(caps)==1 and len(rails)==4 and max(abs(v-w) for v,w in zip(
                     [rails[0], rails[-1], *caps[0]], [*cheek, *cheek]))<=TOL
                out.test(label, ok, wall_face_offsets_mm=cheek, cap_offsets_mm=caps,
                         frame_face_offsets_mm=[rails[0],rails[-1]] if rails else [],
                         actual_wall_depth_mm=cheek[1]-cheek[0])
    if op["kind"]=="sliding" or special:
        support = op.get("jamb_support",["wall","wall"])
        # Only a structural wall/window/frame, including a true neighbouring rail, can support an end.
        neighbors = unary_union([s for k,paths in all_lines.items() if k!=key for s in paths])
        for i, station in enumerate((0,span)):
            expected_faces = (op.get("geometry_exception") or {}).get("face_offsets_mm") if special else None
            if not expected_faces:
                depth = op.get("symbol_depth_mm",op.get("wall_thickness_mm",0))
                expected_faces = [-depth/2,depth/2]
                if special:
                    out.add("special-profile/"+key+"/"+"ab"[i],"pending",reason="缺特殊框原图对应的两侧有符号偏距")
            caps = []
            for candidate,segment in zip(lines,local):
                if not is_cap(segment,station):
                    continue
                ends = sorted([segment[0][1],segment[-1][1]])
                if len(expected_faces)==2 and all(abs(a-b)<=TOL for a,b in zip(ends,expected_faces)):
                    caps.append(candidate)
            target = support[i]
            id_ = f"end/{key}/{'ab'[i]}"
            if target=="free_end":
                confirmed = evidence(op.get("end_evidence",{}).get("ab"[i]),source)
                out.add(id_, "pass" if confirmed and caps else "pending",support=target)
            elif not caps:
                out.add(id_,"fail",reason="登记端面缺实际端封线")
            elif target=="wall":
                # The complete exposed cap must land on its supporting material face.
                missing = max(s.difference(material.buffer(TOL)).length for s in caps)
                out.test(id_, missing<=TOL, unsupported_length_mm=missing)
            elif target=="frame":
                if neighbors.is_empty:
                    out.add(id_,"fail",reason="端封线邻近没有框线")
                else:
                    matches = {other for other,paths in all_lines.items() if other!=key and paths
                               and min(s.distance(unary_union(paths)) for s in caps)<=TOL}
                    target_id = op.get("frame_neighbors",{}).get("ab"[i])
                    if target_id:
                        out.test(id_,target_id in matches,neighbor_ids=sorted(matches),expected_id=target_id)
                    elif len(matches)==1:
                        out.add(id_,"pass",neighbor_ids=sorted(matches))
                    else:
                        out.add(id_,"pending" if matches else "fail",neighbor_ids=sorted(matches),
                                reason="框接框端头须指向唯一真实邻框")
            else:
                out.add(id_,"pending",reason="端部支承用途待核")
    embedded = unary_union(lines).intersection(material.buffer(-TOL)).length
    out.test("frame-in-wall/"+key,embedded<=TOL, crossing_length_mm=embedded)


def neighbors(out, doc, ledger, edge_job, material, strokes):
    from object_edge_relations import audit_edges, physical
    layers = ledger.get("geometry_layers_v16") or {}
    registered = ledger.get("geometry_objects_v16")
    if registered is None or not isinstance(edge_job,dict) or "items" not in edge_job:
        out.add("neighbors/inventory","pending",reason="缺同版家具句柄表或接边关系表")
        return
    expected = {ident: row["handle"] for ident,row in registered.items()}
    received = {ident: row["handle"] for ident,row in edge_job["items"].items()}
    out.test("neighbors/inventory", expected==received)
    handles = set(received.values())
    for ent in doc.modelspace():
        if ent.dxf.layer in (layers.get("fixed"), layers.get("movable")) and ent.dxf.handle not in handles:
            out.add("unregistered-object/"+ent.dxf.handle,"pending",layer=ent.dxf.layer)
    if not edge_job["items"]:
        return
    bodies, notes = physical(doc, edge_job)
    for key in received:
        if key not in bodies or key in notes:
            out.add("body/"+key,"pending",reason=notes.get(key,["未解码真实占地"]))
    edge = audit_edges(doc,ledger,edge_job,ledger.get("geometry_projections_v16",[]))
    for i,row in enumerate(edge["coverage_issues"]):
        out.add("edge-coverage/"+str(i),"pending",evidence=row)
    for row in edge["relations"]:
        out.add("edge/"+row["id"],{"pass":"pass","fail":"fail","review":"pending"}[row["status"]],measurement=row)
    for i,row in enumerate(edge["collisions"]):
        out.add("collision/"+str(i),"fail",measurement=row)
    # Test actual linework, including long sliding end caps, against every actual object body.
    for op in ledger["openings"]:
        if op["kind"] not in ("sliding","window","rail"):
            continue
        path = unary_union(strokes.get(op["id"],[]))
        if path.is_empty:
            continue
        for key,body in bodies.items():
            embedded = path.intersection(body.buffer(-TOL)).length
            if embedded>TOL:
                out.add(f"frame-in-body/{op['id']}/{key}","fail",crossing_length_mm=embedded)


def audit(doc, ledger, edge_job=None, *, source_sha256=None):
    """Remeasure an actual DWG readback. Caller verifies its DWG/DXF digests."""
    out = Results()
    if not valid_hash(source_sha256) or source_sha256 != ledger.get("source_sha256"):
        out.add("source-binding","pending",reason="缺本次原图摘要或与制图台账不符")
        source_sha256 = None
    if doc.units!=4:
        out.add("units","fail",reason="图形的模型单位必须为毫米")
    if not ledger.get("walls") or "openings" not in ledger:
        out.add("wall-inventory","pending",reason="缺墙或开口清单")
        return out.report()
    spec = ledger.get("wall_geometry_v16")
    if not isinstance(spec,dict):
        out.add("wall-policy","pending",reason="缺本户墙体控制记录")
    layers = ledger.get("geometry_layers_v16") or {}
    if not all(layers.get(k) for k in ("bearing","partition","window","door","fixed","movable","ancillary")):
        out.add("layer-inventory","pending",reason="缺模型层与构件类别关系")
    wall_layers = set(ledger.get("wall_layers",{}))
    for role in ("bearing","partition"):
        name = layers.get(role)
        if name and any(e.dxf.layer==name for e in doc.modelspace()):
            wall_layers.add(name)
    try:
        records, material, issues = wall_material(doc, sorted(wall_layers), [1,0,0,1,0,0])
        for i,issue in enumerate(issues):
            out.add("wall-decode/"+str(i),"pending",reason=issue)
        if material.is_empty:
            out.add("wall-material","fail",reason="回读中缺可核验的实墙")
            return out.report()
        from curved_wall_returns import inspect_returns
        inspect_returns(out,ledger,doc,records)
        sections = wall_sections(out,ledger,material,source_sha256)
        controls(out,ledger,sections)
        for group in (spec or {}).get("continuous_faces", []):
            members = group.get("members",[])
            bindings = (ledger.get("grid_registration") or {}).get("controls",{}).get(group.get("control"),{}).get("bindings",[])
            valid = (evidence(group,source_sha256) and len({m.get("wall") for m in members})>=2 and
                     all(m in bindings for m in members))
            out.add("continuous/"+str(group.get("id")), "pass" if valid else "pending",
                    reason="核验各墙对应的控制面实量见 control 记录")
        for joint in (spec or {}).get("material_joints",[]):
            key = "material-joint/"+str(joint.get("id"))
            members = joint.get("members") or []
            wall_table = {w["id"]:w for w in ledger["walls"]}
            if not (len(members)==2 and evidence(joint,source_sha256)
                    and all(m.get("wall") in wall_table for m in members)):
                out.add(key,"pending")
                continue
            rows = [(wall_table[m["wall"]],m["end"]) for m in members]
            (first,ea),(second,eb) = rows
            ti,normal,_,_ = _axis(first)
            tj,m,_,_ = _axis(second)
            if ti==tj and ea!="through" and eb!="through":
                delta = abs(first[ea][ti]-second[eb][ti])
                at = (first[ea][ti]+second[eb][ti])/2
                probe = Point(at,normal) if ti==0 else Point(normal,at)
                # Unlike a union check, this catches a misplaced division between colours.
                seams = [any(r["shape"].distance(probe)<=TOL for r in records if r["layer"]==w["material"])
                         for w,_ in rows]
                out.test(key, delta<=TOL and all(seams),end_delta_mm=delta,material_at_joint=seams)
            else:
                dist = [abs(w[end][_axis(w)[0]]-target) for (w,end),target in zip(rows,(m,normal)) if end!="through"]
                out.test(key, bool(dist) and max(dist)<=TOL, endpoint_delta_mm=dist)
        model_topology(out, ledger["walls"], material, records, ledger["openings"], source_sha256, ledger)
        straight, curves = read_symbols(doc,ledger,out)
        strokes = {key: paths + curves[key] for key, paths in straight.items()}
        frames = {op["id"]:strokes[op["id"]] for op in ledger["openings"] if op["kind"] in ("window","sliding","rail")}
        handles = {op["id"]:set(op.get("symbol_handles",[])) for op in ledger["openings"]}
        for op in ledger["openings"]:
            if op["kind"] in ("window","sliding"):
                key = op["id"]
                if (op["kind"] == "window" and curves[key]
                        and exception(op, source_sha256, {"corner_frame"})
                        and evidence_at(op["geometry_exception"], source_sha256, op, ledger)):
                    inspect_ends(out, op, curves[key], straight[key], frames, material, TOL,
                                 lambda side: evidence(op.get("end_evidence",{}).get(side), source_sha256),handles)
                else:
                    aperture(out,op,strokes[key],frames,material,source_sha256,ledger)
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, ArithmeticError) as exc:
        out.add("measurement-incomplete","pending",reason=str(exc))
    else:
        # Independent checks must each run, even when a localized junction cannot be decoded.
        for key, run in (("wall-to-window", lambda: window_wall_ends(out,ledger,straight,material,source_sha256,curves)),
                         ("neighbors", lambda: neighbors(out,doc,ledger,edge_job,material,strokes))):
            try:
                run()
            except (ValueError, TypeError, KeyError, IndexError, AttributeError, ArithmeticError) as exc:
                out.add(key+"/measurement-incomplete","pending",reason=str(exc))
    return out.report()


def bind_material_joints(walls, scene):
    """Resolve proven collinear butts, L turns and T contacts before strips are cut."""
    table = {w["id"]:w for w in walls}
    registrations = scene.get("grid_registration",{}).get("controls",{})
    reserved = set()
    for row in (scene.get("wall_geometry_v16") or {}).get("material_joints",[]):
        if not evidence(row,scene.get("source_sha256")):
            continue
        pairs = [(table[m["wall"]],m["end"]) for m in row["members"]]
        if len(pairs)!=2 or pairs[0][0] is pairs[1][0] or any(end not in ("a","b","through") for _,end in pairs):
            raise ValueError("A material joint needs two distinct walls and their physical ends")
        (left,a),(right,b) = pairs
        ti,n,_,_ = _axis(left)
        tj,m,_,_ = _axis(right)
        if ti==tj:
            if "through" in (a,b) or row.get("anchor_wall") not in (left["id"],right["id"]):
                raise ValueError("Collinear material joint requires a named fixed wall end")
            face = row.get("shared_face")
            if face not in ("min_face","max_face","center"):
                raise ValueError("Record the common physical face at the material change")
            sign = {"min_face":-1,"max_face":1,"center":0}[face]
            if abs(n+sign*left["width_mm"]/2-m-sign*right["width_mm"]/2)>TOL:
                raise ValueError("Named material faces differ; bind both walls to their physical control first")
            station = left[a][ti] if row["anchor_wall"]==left["id"] else right[b][ti]
            destination = [(wall,end,station) for wall,end in pairs]
        else:
            if a==b=="through":
                raise ValueError("Use one actual branch end or an L junction")
            destination = [(left,a,m),(right,b,n)]
        for wall,end,target in destination:
            t,_,first,last = _axis(wall)
            if end=="through":
                if not min(first,last)<target<max(first,last):
                    raise ValueError("The T connection is outside the declared through member")
                continue
            pair = wall["id"],end
            if pair in reserved:
                raise ValueError(f"Wall end belongs to two material joints: {pair}")
            reserved.add(pair)
            before,opposite = (first,last) if end=="a" else (last,first)
            if (before-opposite)*(target-opposite)<=0:
                raise ValueError("Material junction would erase or reverse a wall")
            for control in registrations.values():
                if (control["axis"]=="xy"[t] and {"wall":wall["id"],"at":end} in control.get("bindings",[])
                        and abs(target-control["mm"])>TOL):
                    raise ValueError("Material end conflicts with a registered source control")
            wall[end] = list(wall[end])
            wall[end][t] = target


def evidence_at(record, source, row, ledger, whole=False):
    if not evidence(record,source):
        return False
    registration = ledger.get("geometry_registration_v16")
    if not registration:
        return False
    from dimension_grid import GridProjection
    projection = GridProjection(registration)
    x0,y0,x1,y1 = record["bbox_px"]
    patch = box(*rect_bounds(projection.world([x0,y0]), projection.world([x1,y1])))
    line = LineString([row["a"],row["b"]])
    reach = row.get("width_mm",row.get("wall_thickness_mm",0))/2 + TOL
    return patch.buffer(reach).covers(line) if whole else patch.distance(line)<=reach


def window_wall_ends(out, ledger, strokes, material, source, curves=None):
    """Measure BOTH sides of a partition ending against the inside track of a long window."""
    rules = (ledger.get("wall_geometry_v16") or {}).get("frame_wall_ends",[])
    declared = {(r["wall"],r["end"],r["window"]):r for r in rules}
    used = set()
    for window in ledger["openings"]:
        if window["kind"]!="window":
            continue
        if (curves or {}).get(window["id"]):
            # Curved tracks have no single longitudinal axis. Their real caps and
            # penetration are measured by inspect_ends. Explicit partition contacts
            # stay pending below until both curved cross-sections are measured.
            continue
        ti, _, first, last = _axis(window)
        ni = 1-ti
        paths = [list(line.coords) for line in strokes.get(window["id"],[])]
        for wall in ledger["walls"]:
            wt, station, _, _ = _axis(wall)
            if wt!=ni or wall["id"] in window.get("hosts",[]):
                continue
            for end,other in (("a","b"),("b","a")):
                key = wall["id"],end,window["id"]
                rule = declared.get(key)
                at = wall[end][ni]
                far = wall[other][ni]
                direction = 1 if at>far else -1
                # The two inspection stations are just inside both wall flanks.
                delta = max(0,wall["width_mm"]/2-SAMPLE)
                cuts = [station-delta,station+delta]
                if not rule and not all(min(first,last)+TOL<x<max(first,last)-TOL for x in cuts):
                    continue
                frames = []
                for x in cuts:
                    levels = [p[ni] for p,q in paths if abs(q[ni]-p[ni])<=TOL and
                              min(p[ti],q[ti])-TOL<=x<=max(p[ti],q[ti])+TOL and abs(p[ti]-q[ti])>TOL]
                    if levels:
                        frames.append(min(levels) if direction>0 else max(levels))
                if not rule and (len(frames)!=2 or max(abs(at-f) for f in frames)>max(120,wall["width_mm"]/2)):
                    continue
                used.add(key)
                id_ = "wall-to-window/"+"/".join(key)
                if len(frames)!=2:
                    out.add(id_,"pending",reason="两条局部截线缺少完整窗框")
                    continue
                gaps=[]
                for x, target in zip(cuts,frames):
                    inner = at - direction*min(abs(far-at)/2, max(100,wall["width_mm"]))
                    lo,hi = sorted((inner,target+direction*max(300,wall["width_mm"])))
                    p,q = ([x,lo],[x,hi]) if ti==0 else ([lo,x],[hi,x])
                    probe = Point(x,inner) if ti==0 else Point(inner,x)
                    candidates=[part for part in parts(material.intersection(LineString([p,q])),"LineString")
                                if part.distance(probe)<TOL]
                    if len(candidates)==1:
                        coords = [point[ni] for point in candidates[0].coords]
                        actual = max(coords) if direction>0 else min(coords)
                        gaps.append(direction*(target-actual))
                if len(gaps)!=2:
                    out.add(id_,"pending",reason="未在两侧取得支墙实体端面")
                    continue
                accepted = rule and evidence(rule,source)
                wanted = rule.get("gap_mm",0) if accepted else 0
                if type(wanted) not in (int,float) or not math.isfinite(wanted) or wanted<0:
                    out.add(id_,"pending",reason="接窗收口净距无效")
                    continue
                if min(gaps)<-TOL:
                    status = "fail"
                elif rule and not accepted:
                    status = "pending"
                elif max(abs(g-wanted) for g in gaps)<=TOL:
                    status = "pass"
                else:
                    status = "fail" if accepted else "pending"
                out.add(id_,status,actual_gaps_mm=gaps,expected_mm=wanted,cut_stations_mm=cuts)
    for key in set(declared)-used:
        out.add("wall-to-window/"+"/".join(key),"pending",reason="已登记的墙端或邻窗未对应到模型")
