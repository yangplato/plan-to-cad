"""Check the room on each side of a registered opening against reopened DXF wall material."""
import argparse
from collections import defaultdict
from html import escape
import io
import json
import math
from pathlib import Path
import sys

from dimension_grid import GridProjection
import ezdxf
from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import unary_union
from check_wall_probes import decode, numbers

PASS, FAIL, REVIEW = "通过", "失败", "待核"
PASSAGES = {"swing", "sliding", "open"}
EPS = 1e-5  # arithmetic tolerance in millimetres; never joins separated walls


def xy(z):
    return (z.real, z.imag)


def zpoint(p):
    return complex(*numbers(p, 2))


def points_world(matrix):
    a, b, d, e, tx, ty = numbers(matrix, 6)
    if abs(a * e - b * d) <= 1e-12:
        raise ValueError("singular source_to_mm")
    def transform(p):
        u, v = numbers(p, 2)
        return complex(a * u + b * v + tx, d * u + e * v + ty)
    return transform


def indexed(rows):
    data = {}
    for row in rows:
        key = row["id"]
        if not isinstance(key, str) or not key.strip() or key in data:
            raise ValueError(f"empty or duplicate id {key!r}")
        data[key] = row
    return data


def polygons(geom):
    if geom.geom_type == "Polygon" and not geom.is_empty:
        yield geom
    elif hasattr(geom, "geoms"):
        for child in geom.geoms:
            yield from polygons(child)


def strip(a, b, depth):
    if abs(b-a) < EPS or not math.isfinite(depth) or depth <= 0:
        raise ValueError("opening needs distinct jambs and positive depth in mm")
    n = 1j * (b-a) / abs(b-a) * depth / 2
    return Polygon([xy(a+n), xy(b+n), xy(b-n), xy(a-n)])


def pair_ids(pair, known):
    if not isinstance(pair, list) or len(pair) != 2 or len(set(pair)) != 2 or set(pair) - known:
        raise ValueError(f"expected two distinct, registered room IDs in {pair!r}")
    return tuple(pair)


def axis_position(z, origin, u):
    local = (z-origin) * u.conjugate()
    return local.real, local.imag


def route(a, b, depth, ends, material):
    """The witnesses explicitly describe a short, straight crossing local to this opening."""
    width = abs(b-a)
    if width < EPS or not math.isfinite(depth) or depth <= 0:
        raise ValueError("invalid local candidate")
    u = (b-a) / width
    stations = [axis_position(p, a, u) for p in ends]
    normal = [p[1] for p in stations]
    opposite = (min(normal) < -depth/2 - EPS and max(normal) > depth/2 + EPS)
    crossing = None
    if min(normal) < 0 < max(normal):
        t = -normal[0] / (normal[1] - normal[0])
        crossing = stations[0][0] + t * (stations[1][0] - stations[0][0])
    within = crossing is not None and EPS < crossing < width - EPS
    line = LineString([xy(p) for p in ends])
    return dict(signed_offsets_mm=[round(v, 2) for v in normal],
                cross_station_from_a_mm=None if crossing is None else round(crossing, 2),
                span_mm=round(width, 2), crosses_clear_span=bool(opposite and within),
                route_in_saved_wall_mm=round(line.intersection(material).length, 4))


def actual_frame(op, doc, a, b, depth):
    """Verify that saved frame strokes extend along this span. Handles alone are insufficient."""
    width = abs(b-a)
    u = (b-a)/width
    intervals = []
    model = doc.modelspace()
    for handle in op.get("symbol_handles", []):
        en = doc.entitydb.get(handle)
        if en is None or not en.is_alive or en.dxf.owner != model.layout_key:
            continue
        if en.dxftype() == "LINE":
            coords = [en.dxf.start, en.dxf.end]
        elif en.dxftype() == "LWPOLYLINE" and not en.has_arc:
            coords = list(en.vertices_in_wcs())
            if en.closed and coords:
                coords.append(coords[0])
        else:
            continue
        local = [axis_position(complex(p[0], p[1]), a, u) for p in coords]
        for (x1, y1), (x2, y2) in zip(local, local[1:]):
            if max(abs(y1), abs(y2)) <= depth/2 + EPS and abs(y1-y2) <= EPS:
                lo, hi = max(0, min(x1, x2)), min(width, max(x1, x2))
                if hi > lo:
                    intervals.append((lo, hi))
    end = 0.
    for lo, hi in sorted(intervals):
        if lo > end + EPS:
            break
        end = max(end, hi)
    return end >= width - EPS


def sides_at(gate, fraction):
    base = gate["a"] + (gate["b"] - gate["a"]) * fraction
    offset = gate["n"] * (gate["depth"]/2 + min(40., gate["width"]*.08))
    return base-offset, base+offset


def in_cell(p, parts):
    point = Point(xy(p))
    return next((i for i, cell in enumerate(parts) if cell.contains(point)), None)


def partition_rooms(material, gates, ops, envelope, samples):
    """Test 4 and 8 local coordinate ULPs at the ends of credible virtual seals.

    Only seal ends move along the opening axis. The wall polygon is identical
    in every overlay. Compare cell counts and witness groups independently of
    backend component numbering. All physical checks use the original CAD.
    """
    ceiling = 1e-6  # Maximum virtual vertex travel in mm (one nanometre).
    credible = {k: g for k, g in gates.items() if g["credible"]}
    bounds = unary_union([material, *(g["cap"] for g in credible.values())]).bounds
    magnitude = max(*map(abs, bounds), bounds[2]-bounds[0], bounds[3]-bounds[1])
    ulp = math.ulp(magnitude)
    steps = (4, 8)
    enough = (steps[-1]+2)*ulp <= ceiling
    evidence = dict(method="virtual jamb end extension only; saved walls immutable",
                    coordinate_magnitude_mm=magnitude, coordinate_ulp_mm=ulp,
                    tested_local_ulp_multipliers=list(steps),
                    maximum_vertex_travel_mm=ceiling,
                    resolution_sufficient=enough, virtual_jambs={}, modes={})
    expanded = {m: {} for m in (0, *steps)}
    for key, g in gates.items():
        ring = [complex(*p) for p in list(g["cap"].exterior.coords)[:-1]]
        ends = [(ring[0], ring[3]), (ring[1], ring[2])]
        gaps = [LineString([xy(p), xy(q)]).distance(material) for p, q in ends]
        # Project coordinate rounding uncertainty onto the seal's axial direction.
        # A large Y datum must not enlarge the X budget for a horizontal opening.
        ux, uy = abs(g["u"].real), abs(g["u"].imag)
        xulp = math.ulp(max(abs(p.real) for p in ring))
        yulp = math.ulp(max(abs(p.imag) for p in ring))
        local_ulp = max(math.ulp(g["width"]), ux*xulp + uy*yulp)
        record = dict(raw_jamb_to_wall_distance_mm=gaps, used_for_virtual_seal=g["credible"],
                      axial_coordinate_ulp_mm=local_ulp,
                      candidate_jamb_extensions_mm=[v*local_ulp for v in steps],
                      observed_vertex_travel_mm={})
        evidence["virtual_jambs"][key] = record
        if not g["credible"]:
            continue
        expanded[0][key] = g["cap"]
        if (steps[-1]+2)*local_ulp > ceiling:
            enough = False
        if enough:
            for step in steps:
                delta = step * local_ulp * g["u"]
                adjusted = [p + sign*delta for p, sign in zip(ring, (-1, 1, 1, -1))]
                travel = max(abs(p-q) for p, q in zip(ring, adjusted))
                record["observed_vertex_travel_mm"][str(step)] = travel
                if travel > ceiling:
                    raise ValueError("virtual seal exceeds coordinate precision budget")
                # Include the original to protect against last-bit normal drift on
                # oblique axes. A wall-wall gap away from a seal cannot be bridged.
                expanded[step][key] = g["cap"].union(Polygon([xy(p) for p in adjusted]))

    evidence["resolution_sufficient"] = enough

    def split(keys, step):
        solid = unary_union([material, *(expanded[step][k] for k in keys)])
        parts = list(polygons(envelope.difference(solid)))
        return sorted(parts, key=lambda p: (not p.touches(envelope.boundary), *p.bounds, -p.area))

    def signature(parts):
        groups = defaultdict(list)
        for name, point in samples:
            groups[in_cell(point, parts)].append(name)
        missing = sorted(groups.pop(None, ()))
        return dict(cell_count=len(parts), witness_groups=sorted(sorted(v) for v in groups.values()),
                    on_boundary_or_solid=missing)

    def solve(keys):
        raw = split(keys, 0)
        original = signature(raw)
        detail = dict(raw=original)
        if not enough:
            detail["decision"] = "review_coordinate_resolution"
            return raw, detail
        if not keys:
            detail["decision"] = "raw_no_virtual_seals"
            return raw, detail
        small, large = (split(keys, step) for step in steps)
        s, l = signature(small), signature(large)
        if s == l == original:
            detail["decision"] = "raw_confirmed_at_both_scales"
            return raw, detail
        detail.update(at_4_ulp=s, at_8_ulp=l)
        if s != l or original["on_boundary_or_solid"] != s["on_boundary_or_solid"]:
            detail["decision"] = "review_scale_disagreement_or_witness_on_seal"
            return raw, detail
        # Extending a seal may split connected floors. Previously separate
        # witness groups cannot become connected by adding solid material.
        raw_groups = [set(labels) for labels in original["witness_groups"]]
        if s["cell_count"] < len(raw) or any(
            not any(set(labels) <= before for before in raw_groups) for labels in s["witness_groups"]
        ):
            detail["decision"] = "review_nonmonotone_overlay"
            return raw, detail
        detail["decision"] = "use_4_ulp_confirmed_at_8_ulp"
        return small, detail

    closed, closed_note = solve(tuple(credible))
    open_keys = tuple(k for k in credible if ops[k]["kind"] != "open")
    opened, open_note = (closed, closed_note) if open_keys == tuple(credible) else solve(open_keys)
    evidence["modes"].update(closed=closed_note, permanent_openings=open_note)
    evidence["requires_review"] = any(note["decision"].startswith("review_")
                                      for note in (closed_note, open_note))
    return closed, opened, evidence


def audit(doc, ledger, witness, *, drawing=None):
    """Return a JSON-safe report. Optional drawing dict receives geometry for the SVG."""
    material, uncertain, issues, inventory = decode(doc, ledger)
    if material.is_empty:
        raise ValueError("no decoded wall material")
    projection = GridProjection(witness)
    transform = lambda p: complex(*projection.world(p))
    rooms = indexed(witness["rooms"])
    if not rooms:
        raise ValueError("register independent room seeds")
    known = set(rooms) | {"outside"}
    ops = indexed(ledger["openings"])
    expected = indexed(witness.get("portals", []))
    links = {tuple(sorted(pair_ids(pair, known))) for pair in witness.get("open_links", [])}
    by_room = {}
    tests = []
    uncertain_topology = bool(uncertain or issues)
    def mark(code, ident, status, **note):
        tests.append(dict(code=code, id=ident, status=status, **note))
    for key, row in rooms.items():
        if row["mode"] not in {"open", "closed", "exterior", "unknown"}:
            raise ValueError(f"{key}: invalid independent enclosure mode")
        if key == "outside" and row["mode"] != "exterior":
            raise ValueError("outside must have exterior mode")
        seeds = [transform(p) for p in row["seeds_px"]]
        if not seeds:
            raise ValueError(f"{key}: record at least one clear floor seed")
        by_room[key] = seeds

    for one, two in links:
        if any(rooms.get(k, {}).get("mode") == "closed" for k in (one, two)):
            raise ValueError(f"independent witnesses disagree about open link: {one}, {two}")
    gates = {}
    for key, op in ops.items():
        a, b = zpoint(op["a"]), zpoint(op["b"])
        depth = float(op["wall_thickness_mm"])
        cap = strip(a, b, depth)
        width = abs(b-a)
        u = (b-a)/width
        n = u*1j
        margin = float(op.get("clear_margin_mm", min(20., width*.05)))
        if not 0 <= margin < width/2:
            raise ValueError(f"{key}: invalid clear_margin_mm")
        clear = strip(a + u*margin, b - u*margin, depth)
        # a/b are the CAD's own net-opening endpoints. Check the whole CAD
        # strip, retaining trimmed core area separately as a local diagnostic.
        area = cap.intersection(material).area
        core_area = clear.intersection(material).area
        jamb = [material.covers(Point(xy(p))) for p in (a-u*EPS*10, b+u*EPS*10)]
        roles = op.get("jamb_support", ["wall", "wall"])
        if len(roles) != 2 or any(s not in {"wall", "frame", "free_end"} for s in roles):
            raise ValueError(f"{key}: invalid jamb_support")
        if op["kind"] not in PASSAGES | {"rail", "window"}:
            raise ValueError(f"{key}: unknown opening kind")
        hosted = bool(op.get("hosts"))
        needs_spanning_frame = op["kind"] in {"window", "rail", "sliding"}
        framed = (needs_spanning_frame or not hosted or not all(jamb)) and actual_frame(op, doc, a, b, depth)
        if not hosted:
            credible = bool(op.get("free_frame_reason")) and framed
        else:
            credible = all(jamb) or (framed and all(ok or role != "wall" for ok, role in zip(jamb, roles)))
        if needs_spanning_frame and not framed:
            credible = False
        if op.get("fixture_partition"):
            # A shower screen inside one room cannot form the boundary of an architectural room.
            credible = False
        elif not credible:
            mark("unconfirmed_boundary", key, REVIEW, jamb_wall=jamb, saved_frame_spans=framed)
            uncertain_topology = True
        if area > 1e-4:
            mark("material_in_opening", key, FAIL, blocked_area_mm2=round(area, 4), trimmed_core_mm2=round(core_area, 4))
        gates[key] = dict(a=a, b=b, u=u, n=n, depth=depth, width=width, cap=cap, credible=credible,
                          blocked=round(area, 4), jamb=jamb, frame=framed)

    # Close each believable threshold in memory. The original wall material stays available.
    caps = [g["cap"] for g in gates.values() if g["credible"]]
    geometry = [material, *caps, *(Point(xy(p)) for pts in by_room.values() for p in pts)]
    xmin, ymin, xmax, ymax = unary_union(geometry).bounds
    pad = max(1000., .1 * max(xmax-xmin, ymax-ymin))
    envelope = box(xmin-pad, ymin-pad, xmax+pad, ymax+pad)
    exterior_point = complex(xmin-pad/2, ymin-pad/2)
    samples = [("unbounded exterior", exterior_point)]
    for key, pts in by_room.items():
        samples.extend((f"room {key} seed {i}", p) for i, p in enumerate(pts))
    for key, g in gates.items():
        if ops[key]["kind"] in PASSAGES and not ops[key].get("fixture_partition"):
            for fraction in (.25, .5, .75):
                for i, p in enumerate(sides_at(g, fraction)):
                    samples.append((f"opening {key} at {fraction} side {i}", p))
            for i, p in enumerate(expected.get(key, {}).get("approach_px") or []):
                samples.append((f"opening {key} approach {i}", transform(p)))
    cells, open_cells, partition = partition_rooms(material, gates, ops, envelope, samples)
    boundary_uncertainty = uncertain_topology
    if partition["requires_review"]:
        uncertain_topology = True
        mark("partition_coordinate_precision", "drawing", REVIEW,
             decisions={mode: note["decision"] for mode, note in partition["modes"].items()})
    def locate(p, parts=cells):
        return in_cell(p, parts)
    exterior = locate(exterior_point)
    if exterior is None:
        raise ValueError("unable to classify the unbounded exterior witness")
    groups = defaultdict(set)
    if "outside" not in by_room:
        groups[exterior].add("outside")
    seed_cells = {"outside": {exterior}}
    uncertainty_status = REVIEW if uncertain_topology else FAIL
    for key, pts in by_room.items():
        hits = [locate(p) for p in pts]
        good = {i for i in hits if i is not None}
        seed_cells[key] = good
        for i in good:
            groups[i].add(key)
        if None in hits:
            mark("seed_on_material_or_virtual_cap", key, uncertainty_status,
                 seed_cells=hits, seeds_mm=[xy(p) for p in pts])
        if len(good) > 1:
            mark("seeds_in_separate_cells", key, REVIEW if rooms[key]["mode"]=="unknown" else uncertainty_status,
                 seed_cells=sorted(good))

    for i, ids in sorted(groups.items()):
        for key in sorted(ids):
            if rooms.get(key, {}).get("mode") == "closed" and (len(ids)>1 or i==exterior):
                mark("closed_room_leaks", key, uncertainty_status, cell=i, also_reaches=sorted(ids-{key}),
                     reaches_unbounded_exterior=i==exterior)
    def open_ids(key):
        if key == "outside" and key not in by_room:
            return {locate(exterior_point, open_cells)}
        return {locate(p, open_cells) for p in by_room[key]} - {None}
    for key, row in rooms.items():
        if row["mode"] != "closed":
            continue
        leaks = sorted(other for other in known if other != key and open_ids(key) & open_ids(other)
                       and not seed_cells[key] & seed_cells[other])
        if leaks:
            mark("closed_room_has_permanent_connection", key, uncertainty_status, also_reaches=leaks)
    for one, two in sorted(links):
        first, last = open_ids(one), open_ids(two)
        state = PASS if first and first==last and len(first)==1 else uncertainty_status
        mark("source_open_link", [one, two], REVIEW if state==PASS and uncertain_topology else state,
             open_floor_cells=[sorted(first), sorted(last)])

    measured = []
    verified_pairs = set()
    approach_lines = {}
    for key in sorted(set(expected)-set(ops)):
        mark("missing_source_opening", key, FAIL, expected_rooms=expected[key]["rooms"])
    for key, op in ops.items():
        g = gates[key]
        w = expected.get(key, {})
        if w and "kinds" in w and op["kind"] not in w["kinds"]:
            mark("source_kind_conflict", key, FAIL, source_kinds=w["kinds"], ledger_kind=op["kind"])
        if op["kind"] not in PASSAGES or op.get("fixture_partition"):
            if w.get("rooms"):
                mark("source_passage_unavailable",key,FAIL,ledger_kind=op["kind"],fixture=bool(op.get("fixture_partition")))
            continue
        raw = op.get("connects", [])
        if len(raw) != 2 or len(set(raw)) != 2 or set(raw)-known:
            mark("unregistered_room_pair", key, REVIEW, connects=raw)
            continue
        promised = pair_ids(w["rooms"], known) if w else tuple(raw)
        if set(promised) != set(raw):
            mark("source_room_pair_conflict", key, FAIL, source_rooms=promised, ledger_rooms=raw)
        left, right, probes = [], [], []
        for fraction in (.25, .5, .75):
            neg, pos = sides_at(g, fraction)
            left.append(locate(neg))
            right.append(locate(pos))
            probes.append(dict(station_mm=round(g["width"]*fraction, 2),
                               minus_mm=xy(neg), plus_mm=xy(pos),
                               wall_crossing_mm=round(LineString([xy(neg), xy(pos)]).intersection(material).length, 3)))
        side_cells = [left, right]
        sides = [sorted({x for x in side if x is not None}) for side in side_cells]
        label_sides = [sorted(set().union(*(groups[x] for x in side))) for side in sides]
        stable = all(len(side)==1 and None not in raw_side for side, raw_side in zip(sides, side_cells))
        assignments = []
        if stable:
            for first, second in (promised, promised[::-1]):
                if first in label_sides[0] and second in label_sides[1]:
                    assignments.append([first, second])
        approaches = w.get("approach_px")
        local = None
        candidates = []
        local_state = None
        if approaches is not None:
            if len(approaches) != 2:
                raise ValueError(f"{key}: need two local, ordered approach points")
            pts = [transform(p) for p in approaches]
            approach_lines[key] = pts
            local = route(g["a"], g["b"], g["depth"], pts, material)
            local["point_cells"] = [locate(p) for p in pts]
            local["ties_to_seeds"] = [hit is not None and hit in seed_cells.get(room, set())
                                      for room, hit in zip(promised, local["point_cells"])]
            if not local["crosses_clear_span"] or local["route_in_saved_wall_mm"] > EPS:
                local_state = FAIL
            elif not all(local["ties_to_seeds"]):
                local_state = uncertainty_status if all(local["point_cells"][i] is not None and
                    seed_cells.get(promised[i]) for i in (0, 1)) else REVIEW
            else:
                local_state = PASS
            mark("local_approach", key, local_state, **local)
            for c in w.get("candidates", []):
                candidate = route(transform(c["a_px"]), transform(c["b_px"]),
                                  float(c.get("depth_mm",g["depth"])), pts, material)
                candidates.append({"id": c["id"], **candidate})
        elif w.get("candidates"):
            raise ValueError(f"{key}: host candidates require a local approach pair")
        if not stable or not g["credible"] or not all(label_sides):
            state = REVIEW
        elif not assignments:
            state = uncertainty_status
        elif sides[0] == sides[1]:
            state = REVIEW
        elif any(len(labels)>1 for labels in label_sides) and local_state != PASS:
            state = REVIEW
        else:
            state = REVIEW if uncertain_topology else PASS
        if local_state==FAIL or (state==PASS and local_state==REVIEW):
            state = local_state
        if g["blocked"] > .0001:
            state = FAIL
        mark("rooms_across_actual_opening", key, state, expected_rooms=promised,
             side_room_ids=label_sides, side_cell_ids=sides)
        if state==PASS:
            pair = tuple(sorted(promised))
            verified_pairs.add(pair)
        measured.append(dict(id=key, source_portal=key in expected, rooms=promised,
                             width_mm=round(g["width"],2), depth_mm=g["depth"], axis_mm=[xy(g["a"]),xy(g["b"])],
                             blocked_area_mm2=g["blocked"], saved_wall_at_jambs=g["jamb"], probes=probes,
                             measured_side_labels=label_sides, local_route=local, candidate_routes=candidates))

    # The legacy logical graph may contain a non-door "open" edge through a completely solid wall.
    # Respect independent open links and report an actual separated cell pair without a recorded passage.
    registered_pairs = {tuple(sorted(op["connects"])) for op in ops.values() if op["kind"] in PASSAGES and
                        not op.get("fixture_partition") and len(op.get("connects", []))==2}
    graph = {tuple(sorted((a,b))) for a, neighbors in ledger.get("room_links", {}).items() for b in neighbors}
    for pair in sorted(graph-registered_pairs):
        if set(pair)-known:
            mark("unknown_logical_link", list(pair), REVIEW)
        else:
            ac, bc = [open_ids(k) for k in pair]
            if not ac or not bc:
                mark("logical_link_without_seed", list(pair), REVIEW)
            elif ac.isdisjoint(bc):
                mark("logical_link_through_barrier", list(pair), uncertainty_status)
            elif pair not in links:
                mark("unwitnessed_open_link", list(pair), REVIEW)
    for key in sorted(set(rooms)-{"outside"}):
        if rooms[key]["mode"]=="unknown":
            mark("enclosure_unspecified", key, REVIEW)
        elif rooms[key]["mode"]=="closed" and rooms[key].get("accessible",True) and not any(key in p for p in registered_pairs):
            mark("closed_room_without_portal", key, uncertainty_status)
    for key in sorted({r["id"] for r in ledger.get("rooms", [])}-known):
        mark("room_missing_independent_seed", key, REVIEW)
    if boundary_uncertainty:
        mark("partial_boundary_decode", "drawing", REVIEW, issues=issues, uncertain_contours=[s for s,_ in uncertain])
    if not tests:
        mark("no_observed_relationships", "drawing", REVIEW)
    if drawing is not None:
        displayed = [material, *(Point(xy(p)) for pts in [*by_room.values(),*approach_lines.values()] for p in pts)]
        drawing.update(material=material, gates=gates, seeds=by_room,
                       approaches=approach_lines, bounds=unary_union(displayed).bounds)
    status = max((t["status"] for t in tests), key={PASS:0, REVIEW:1, FAIL:2}.get)
    return dict(status=status, checks=tests, openings=measured, wall_inventory=inventory,
                topology_precision=partition,
                cells=[dict(id=i, room_seeds=sorted(groups[i]), area_mm2=round(cell.area,2), exterior=i==exterior)
                       for i,cell in enumerate(cells)], geometrically_verified_pairs=sorted(verified_pairs))


def write_svg(path, geometry, report):
    """One vector sheet with an overview plus a zoom for each conflicted or uncertain passage."""
    gates, seeds = geometry["gates"], geometry["seeds"]
    codes = defaultdict(list)
    for item in report["checks"]:
        if item["status"] != PASS and isinstance(item["id"], str):
            codes[item["id"]].append(item)
    width = 1000
    zooms = [key for key in gates if key in codes]
    height = 650 + 255 * math.ceil(len(zooms)/2)
    panels = []
    svg = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
           '<rect width="100%" height="100%" fill="white"/>',
           '<style>text{font:13px sans-serif;fill:#242e35}.title{font:bold 16px sans-serif}</style>']
    def panel(bounds, X, Y, W, H, name, focus=None):
        x0,y0,x1,y1=bounds
        xw,yh=max(1,x1-x0),max(1,y1-y0)
        scale=min((W-30)/xw,(H-39)/yh)
        def screen(p):
            return (W/2+(p.real-(x0+x1)/2)*scale, (H+21)/2-(p.imag-(y0+y1)/2)*scale)
        clip = f"panel{len(panels)}"
        panels.append(clip)
        svg.append(f'<defs><clipPath id="{clip}"><rect width="{W}" height="{H}"/></clipPath></defs>')
        svg.append(f'<g transform="translate({X},{Y})"><g clip-path="url(#{clip})">')
        svg.append(f'<rect x="0" y="0" width="{W}" height="{H}" fill="#fbfaf6" stroke="#bec5c7"/>')
        def line(p,q,color,width=2,dash=""):
            a,b=screen(p);c,d=screen(q)
            svg.append(f'<line x1="{a:.2f}" y1="{b:.2f}" x2="{c:.2f}" y2="{d:.2f}" stroke="{color}" stroke-width="{width}" stroke-dasharray="{dash or "none"}"/>')
        def text_at(p,label,dx=5,dy=-4):
            x,y=screen(p)
            svg.append(f'<text x="{x+dx:.2f}" y="{y+dy:.2f}" style="fill:#1f3037">{escape(label)}</text>')
        def shape(p):
            runs=[]
            for ring in (p.exterior,*p.interiors):
                pts=[screen(complex(x,y)) for x,y in ring.coords]
                runs.append("M"+" L".join(f"{x:.2f},{y:.2f}" for x,y in pts)+" Z")
            return " ".join(runs)
        for p in polygons(geometry["material"].intersection(box(x0,y0,x1,y1))):
            svg.append(f'<path d="{shape(p)}" fill="#47545d" fill-rule="evenodd"/>')
        for key,g in gates.items():
            middle=(g["a"]+g["b"])/2
            if not x0<=middle.real<=x1 or not y0<=middle.imag<=y1:continue
            color = "#c33e42" if key in codes else "#188981"
            line(g["a"],g["b"],color,4)
            if focus is None or focus==key:
                text_at(middle,key)
            if key in geometry["approaches"]:
                p,q=geometry["approaches"][key]
                line(p,q,"#9a54b1",2,"4 4")
                for point in (p,q):
                    x,y=screen(point)
                    svg.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="3" fill="#9a54b1"/>')
        for key,pts in seeds.items():
            for p in pts:
                if not x0<=p.real<=x1 or not y0<=p.imag<=y1:continue
                x,y=screen(p)
                svg.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="4" fill="#416fba"/>')
                text_at(p,key)
        svg.append(f'<rect x="0" y="0" width="{W}" height="23" fill="white"/><text class="title" x="9" y="17">{escape(name)}</text></g></g>')
    panel(geometry["bounds"],0,30,1000,610,"房间与洞口几何核对")
    svg.append('<text x="8" y="20">灰色为保存图墙材　蓝点为独立房间种子　紫线为独立局部穿行　绿色为登记洞口　红色为洞口有冲突或待核项</text>')
    for i,key in enumerate(zooms):
        g=gates[key]
        marks=[g["a"],g["b"],*geometry["approaches"].get(key, [])]
        margin=max(400.,g["width"]*.4)
        bounds=(min(p.real for p in marks)-margin,min(p.imag for p in marks)-margin,
                max(p.real for p in marks)+margin,max(p.imag for p in marks)+margin)
        labels={"material_in_opening":"洞内有墙材","rooms_across_actual_opening":"洞口两侧房间","local_approach":"局部穿行",
                "source_kind_conflict":"源图类型","unconfirmed_boundary":"墙端或自由框","source_room_pair_conflict":"房间记录"}
        tag = " / ".join(labels.get(t["code"],t["code"]) for t in codes[key])
        panel(bounds,500*(i%2),650+255*(i//2),498,252,f"{key} · {tag}",key)
    svg.append('</svg>')
    path.write_text("\n".join(svg),encoding="utf-8")


def self_test(svg=None):
    """All dimensions below are from an invented L-shaped living area. No project files."""
    from copy import deepcopy
    from draw_opening import draw_opening

    def op(key, kind, a, b, rooms=None):
        row=dict(id=key,kind=kind,a=a,b=b,wall_thickness_mm=200,hosts=["synthetic"])
        if rooms:row["connects"]=rooms
        return row
    bedroom = op("B","swing",(800,4000),(1700,4000),["bed","living"])
    other = op("B","swing",(4000,4400),(4000,5300),["bed","living"])
    entrance=op("E","swing",(4500,0),(5400,0),["outside","living"])
    kitchen=op("K","swing",(6500,900),(6500,1700),["kitchen","living"])
    rail=op("R","rail",(7000,8000),(9000,8000))
    rail.update(hosts=[], free_frame_reason="free balcony rail between end posts")
    def make(variant):
        ops=deepcopy([other if variant=="wrong" else bedroom, entrance, kitchen, rail])
        if variant=="wrong_room":
            ops[2]["a"],ops[2]["b"]=other["a"],other["b"]
        if variant=="fake_link":
            ops.pop(0)
        if variant=="false_type":
            ops[0]["kind"]="open"
        if variant=="open_kitchen":
            next(o for o in ops if o["id"]=="K")["kind"]="open"
        shape=unary_union([box(0,0,10000,8000).boundary.buffer(100,join_style=2),
                           box(0,3900,4000,4100),box(3900,3900,4100,8000),
                           box(6400,0,6600,3100),box(6400,2900,10000,3100)])
        if variant=="landing":
            shape=unary_union([shape,box(3700,-1800,3900,0),box(6100,-1800,6300,0),box(3700,-1800,6300,-1600)])
        for o in ops:
            if variant=="blocked" and o["id"]=="E":continue
            shape=shape.difference(strip(zpoint(o["a"]),zpoint(o["b"]),200))
        if variant=="leak":
            shape=shape.difference(strip(4000+6500j,4000+7050j,200))
        doc=ezdxf.new();doc.units=4
        for layer in ("wall","rail","door","window"):
            doc.layers.new(layer)
        for poly in polygons(shape):
            hatch=doc.modelspace().add_hatch(dxfattribs={"layer":"wall"})
            for ring in [poly.exterior,*poly.interiors]:
                hatch.paths.add_polyline_path([p for p in ring.coords][:-1],is_closed=True)
        for o in ops:
            if o["kind"]=="swing":
                o["leaves"]=[{"hinge":"a","share":1.0}]
                o["toward"]={"B":(2000,5450),"E":(4900,7000),"K":(8000,2000)}[o["id"]]
                o["symbol_handles"]=[e.dxf.handle for e in draw_opening(doc.modelspace(),o,"door","window","rail")]
        r=next(o for o in ops if o["id"]=="R")
        if variant!="missing_frame":
            e=doc.modelspace().add_line(r["a"],r["b"],dxfattribs={"layer":"rail"})
            r["symbol_handles"]=[e.dxf.handle]
        buf=io.StringIO();doc.write(buf);buf.seek(0)
        return ezdxf.read(buf), dict(wall_layers={"wall":"fills"},openings=ops,
               room_links={"living":["dining","bed","kitchen","outside"]})
    # Anisotropic reflection+translation tests source registration; seeds are NOT local routes.
    def px(p):
        return [(p[0]-350)/2,(p[1]-9000)/-3]
    def room(key,mode,pts):
        return dict(id=key,mode=mode,seeds_px=[px(p) for p in pts])
    def witness(o,pts):
        return dict(id=o["id"],rooms=o["connects"],kinds=["swing"],approach_px=[px(p) for p in pts])
    obs=dict(source_to_mm=[2,0,0,-3,350,9000],
             rooms=[room("living","open",[(2200,1850),(4900,7000)]),
                    room("dining","open",[(7600,6200)]),room("bed","closed",[(2000,5450)]),
                    room("kitchen","closed",[(8000,2000)])],open_links=[["living","dining"]],
             portals=[witness(bedroom,[(1200,4600),(1200,3500)]),
                      witness(entrance,[(4950,-400),(4950,450)]),
                      witness(kitchen,[(7000,1320),(5950,1320)])])
    obs["portals"][0]["candidates"]=[dict(id="horizontal",a_px=px(bedroom["a"]),b_px=px(bedroom["b"])),
                                     dict(id="perpendicular",a_px=px(other["a"]),b_px=px(other["b"]))]
    cases=[("normal",PASS,None),("landing",PASS,None),("wrong_room",FAIL,"rooms_across_actual_opening"),("wrong",FAIL,"local_approach"),("fake_link",FAIL,"logical_link_through_barrier"),
           ("blocked",FAIL,"material_in_opening"),("leak",FAIL,"closed_room_leaks"),
           ("open_kitchen",PASS,None),("false_type",FAIL,"source_kind_conflict"),("missing_frame",REVIEW,"unconfirmed_boundary")]
    results=[]
    for variant,status,code in cases:
        doc,ledger=make(variant);w=deepcopy(obs);drawing={}
        if variant=="wrong_room":
            w["portals"][-1].pop("approach_px")
        if variant=="landing":
            w["rooms"].append(room("outside","exterior",[(4950,-650)]))
        if variant=="open_kitchen":
            w["rooms"][-1]["mode"]="open"
            w["open_links"].append(["kitchen","living"])
            w["portals"][-1]["kinds"]=["open"]
        result=audit(doc,ledger,w,drawing=drawing)
        flagged=[(t["code"],t["id"],t["status"]) for t in result["checks"] if t["status"]!=PASS]
        if result["status"]!=status or (code and not any(row[0]==code and row[2]==status for row in flagged)):
            raise AssertionError((variant, status, result["status"], flagged))
        if variant=="wrong":
            record=next(o for o in result["openings"] if o["id"]=="B")
            assert record["measured_side_labels"] == [["dining","living"],["bed"]]
            assert [p["crosses_clear_span"] for p in record["candidate_routes"]]==[True,False]
            if svg:write_svg(svg,drawing,result)
        results.append(dict(case=variant,status=status,observed=flagged))
    return results


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dxf",type=Path,nargs="?")
    parser.add_argument("ledger",type=Path,nargs="?")
    parser.add_argument("independent",type=Path,nargs="?")
    parser.add_argument("--svg",type=Path,help="one scalable overview with local conflict panels")
    parser.add_argument("--json",type=Path)
    parser.add_argument("--self-test",action="store_true")
    args=parser.parse_args()
    if args.self_test:
        print(json.dumps(self_test(args.svg),ensure_ascii=False,indent=2))
        return 0
    if not all((args.dxf,args.ledger,args.independent)):
        parser.error("supply saved.dxf ledger.json independent.json, or --self-test")
    if len({p.resolve() for p in (args.dxf,args.ledger,args.independent,args.svg,args.json) if p}) != sum(p is not None for p in (args.dxf,args.ledger,args.independent,args.svg,args.json)):
        parser.error("input and output paths must be distinct")
    try:
        drawing={}
        result=audit(ezdxf.readfile(args.dxf),json.loads(args.ledger.read_text()),
                     json.loads(args.independent.read_text()),drawing=drawing)
        if args.svg:write_svg(args.svg,drawing,result)
    except (OSError,KeyError,ValueError,TypeError,ezdxf.DXFError) as exc:
        result=dict(status=REVIEW,reason=str(exc))
    value=json.dumps(result,ensure_ascii=False,indent=2)
    if args.json:args.json.write_text(value,encoding="utf-8")
    print(value)
    return {PASS:0, FAIL:1, REVIEW:2}[result["status"]]


if __name__=="__main__":
    sys.exit(main())
