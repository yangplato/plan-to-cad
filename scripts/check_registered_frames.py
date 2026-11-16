"""Inspect saved door, window and rail geometry at independently registered pixel spans."""
import argparse
from collections import defaultdict
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import sys
from xml.etree import ElementTree as ET

from dimension_grid import GridProjection
import ezdxf
from ezdxf import bbox
from shapely.affinity import affine_transform
from shapely.geometry import GeometryCollection, LineString, Point, box

from check_portal_rooms import indexed, points_world
from check_wall_probes import decode, numbers

PASS, FAIL, REVIEW = "通过", "失败", "待核"
ROLE = {"sliding": "door", "swing": "door", "window": "window", "rail": "ancillary"}
EDGE = .05  # CAD arithmetic and joining exploded, coincident line ends, mm


def status(fail, pending):
    return FAIL if fail else (REVIEW if pending else PASS)


def xy(z):
    return (z.real, z.imag)


def point(v):
    if abs(v.z) > EDGE or not all(math.isfinite(k) for k in v):
        raise ValueError("nonplanar or nonfinite geometry")
    return complex(v.x, v.y)


def bounds(entity):
    try:
        ext = bbox.extents([entity])
        if ext.has_data:
            return box(ext.extmin.x, ext.extmin.y, ext.extmax.x, ext.extmax.y).buffer(EDGE)
    except (ValueError, ezdxf.DXFError):
        pass
    return None


def expand(doc):
    """Return leaf entities with effective layers, including zero-layer nested members."""
    records, issues = [], []
    def walk(e, inherited, path, names):
        raw = e.dxf.layer
        layer = inherited if raw == "0" and inherited else raw
        if e.dxf.get("invisible", 0) or (layer in doc.layers and
                (doc.layers.get(layer).is_off() or doc.layers.get(layer).is_frozen())):
            return
        if e.dxftype() != "INSERT":
            records.append((e, layer, path))
            return
        name = e.dxf.name
        block = doc.blocks.get(name)
        if name in names or not block or block.block.is_xref or block.block.is_xref_overlay:
            issues.append((path, "unresolved/cyclic/external block " + name, bounds(e), layer))
            return
        def skipped(child, reason):
            issues.append((path, f"{child.dxftype()}: {reason}", bounds(e), layer))
        for k, ref in enumerate(e.multi_insert() if e.mcount > 1 else [e]):
            for i, child in enumerate(ref.virtual_entities(skipped_entity_callback=skipped)):
                walk(child, layer, f"{path}/{name}[{k}].{i}", (*names, name))
    for e in doc.modelspace():
        walk(e, None, e.dxf.handle, ())
    return records, issues


class FlatModel(list):
    def query(self, query):
        assert query == "INSERT"
        return []  # All resolvable blocks were already expanded in WCS.


class FlatDocument:
    def __init__(self, doc, records):
        self.units = doc.units
        self.entities = FlatModel()
        for entity, layer, path in records:
            if entity.dxf.layer != layer:
                entity = entity.copy()
                entity.dxf.layer = layer
            self.entities.append(entity)
    def modelspace(self):
        return self.entities


@dataclass
class Stroke:
    start: complex
    end: complex
    layer: str
    ref: str


@dataclass
class Curve:
    pivot: complex
    ends: tuple
    sweep: float
    ref: str
    layer: str


def symbols(records, names, issues):
    strokes, curves = [], []
    for e, layer, ref in records:
        if layer.casefold() not in names:
            continue
        try:
            kind = e.dxftype()
            if kind in ("POLYLINE", "LWPOLYLINE"):
                parts = list(e.virtual_entities())
                if not parts or getattr(e, "has_width", False):
                    raise ValueError("empty or wide polyline")
            else:
                parts = [e]
            for i, part in enumerate(parts):
                key = ref if len(parts) == 1 else f"{ref}:{i}"
                if part.dxftype() == "LINE":
                    strokes.append(Stroke(point(part.dxf.start), point(part.dxf.end), layer, key))
                elif part.dxftype() == "ARC":
                    pivot = point(part.ocs().to_wcs(part.dxf.center))
                    curves.append(Curve(pivot, (point(part.start_point), point(part.end_point)),
                                        (part.dxf.end_angle-part.dxf.start_angle) % 360, key, layer))
                elif part.dxftype() not in ("TEXT", "MTEXT", "ATTDEF", "POINT"):
                    raise ValueError("nonstandard symbol " + part.dxftype())
        except (ValueError, ezdxf.DXFError, TypeError, AttributeError) as exc:
            issues.append((ref, str(exc), bounds(e), layer))
    return strokes, curves


def intervals(geometry):
    if geometry.geom_type == "LineString" and not geometry.is_empty:
        yield geometry.bounds
    for piece in getattr(geometry, "geoms", ()):
        yield from intervals(piece)


def end_walls(wall, width, tol):
    # Three stations each side. Report real intersection ranges, not a guessed wall width.
    result = []
    for end, sign in ((0, -1), (width, 1)):
        samples = []
        for step in (max(1, tol), max(2, 2*tol), max(3, min(width*.12, 155))):
            station = end + sign * step
            line = LineString([(station, -900), (station, 900)])
            near = [(lo, hi) for _, lo, _, hi in intervals(wall.intersection(line))
                    if lo <= tol and hi >= -tol and hi-lo > 2 and hi-lo < 850]
            samples.append(dict(station_mm=round(station, 2), wall_at_axis=wall.covers(Point(station, 0)),
                                wall_ranges_mm=[[round(v, 2) for v in pair] for pair in near]))
        result.append(samples)
    return result


def merge_lines(runs):
    """Merge touching collinear native segments. Never bridge a pixel uncertainty gap."""
    levels = []
    for lo, hi, y, ref in sorted(runs, key=lambda s: (s[2], s[0])):
        if not levels or y-levels[-1][0] > EDGE:
            levels.append((y, []))
        levels[-1][1].append((lo, hi, ref))
    merged = []
    for y, group in levels:
        for lo, hi, ref in sorted(group):
            if merged and abs(merged[-1][2]-y) <= EDGE and lo <= merged[-1][1]+EDGE:
                merged[-1][1] = max(hi, merged[-1][1])
                merged[-1][3].append(ref)
            else:
                merged.append([lo, hi, y, [ref]])
    return merged


def has_cap(caps, x, y0, y1, tolerance=EDGE):
    return any(abs(x-at) <= tolerance and lo <= y0+EDGE and hi >= y1-EDGE
               for at, lo, hi, _ in caps)


def frame_metrics(lines, arcs, width, along_tol, normal_tol, low, high, wall, ambiguous_spans):
    horizontal, caps, observed, leaves = [], [], [], []
    for s in lines:
        x1,y1 = xy(s.start)
        x2,y2 = xy(s.end)
        lo,hi = sorted((x1,x2))
        bottom,top = sorted((y1,y2))
        if hi < -along_tol or lo > width+along_tol or top < low or bottom > high:
            continue
        observed.append(s.ref)
        if hi-lo > EDGE and abs(y2-y1) <= EDGE and low <= (y1+y2)/2 <= high:
            horizontal.append((lo, hi, (y1+y2)/2, s.ref))
        if top-bottom > EDGE and hi-lo <= EDGE:
            caps.append(((x1+x2)/2, bottom, top, s.ref))
        leaves.append(s)
    merged = merge_lines(horizontal)
    fits = lambda lo,hi: abs(lo) <= along_tol and abs(hi-width) <= along_tol
    tracks = [r for r in merged if fits(r[0], r[1])]
    # Use raw extents as well. An edge of a panel can coincide with longer linework.
    raw = [(lo,hi,y,[ref]) for lo,hi,y,ref in horizontal]
    partial = {}
    for lo,hi,y,refs in [*raw, *merged]:
        if (-along_tol <= lo and hi <= width+along_tol and 0 < (lo+hi)/2 < width
                and not fits(lo, hi) and hi-lo > 2*EDGE):
            partial[(round(lo, 4),round(hi, 4),round(y, 4))] = refs
    panels = []
    for (lo,hi,y),refs in sorted(partial.items()):
        if any(a-along_tol <= (lo+hi)/2 <= b+along_tol for a,b in ambiguous_spans):
            continue
        # Pair two parallel sides with transverses at BOTH ends. Never count tracks as panels.
        choices = [(y2, rr) for (l2,h2,y2),rr in partial.items()
                   if abs(lo-l2) <= EDGE and abs(hi-h2) <= EDGE and y2-y > EDGE
                   and y2-y < (hi-lo)*.35 and has_cap(caps, lo, y, y2) and has_cap(caps, hi, y, y2)]
        if choices:
            top, other = min(choices, key=lambda q:q[0])
            mid = (y+top)/2
            # A centre track crossing the same rectangle must not split a single panel in two.
            if any(abs(p["lo"]-lo) <= EDGE and abs(p["hi"]-hi) <= EDGE and
                   p["bottom"]-EDGE <= y <= p["top"]+EDGE for p in panels):
                continue
            panels.append(dict(lo=lo, hi=hi, bottom=y, top=top, centre=mid, refs=[*refs,*other]))
    panels.sort(key=lambda p:(p["lo"],p["hi"],p["centre"]))
    staggered = []
    for i,p in enumerate(panels):
        for j,q in enumerate(panels[:i]):
            # Partial intervals must occupy different stations, overlap or meet, and be on offset lanes.
            if (abs(p["centre"]-q["centre"]) > EDGE and abs(p["lo"]-q["lo"]) > EDGE and
                    abs(p["hi"]-q["hi"]) > EDGE and min(p["hi"],q["hi"]) >= max(p["lo"],q["lo"])-EDGE):
                staggered.extend([i,j])
    caps_at_end = []
    for end in (0,width):
        candidates = [c for c in caps if abs(c[0]-end)<=along_tol and c[1]<=normal_tol and c[2]>=-normal_tol
                      and (len(tracks)<2 or (c[1] <= min(r[2] for r in tracks)+EDGE and
                                             c[2] >= max(r[2] for r in tracks)-EDGE))]
        caps_at_end.append(sorted(candidates,key=lambda c:abs(c[0]-end))[:1])
    swing = []
    for arc in arcs:
        x,y = xy(arc.pivot)
        if not (min(abs(x),abs(x-width)) <= along_tol and low <= y <= high and 40 <= arc.sweep <= 145):
            continue
        for closed,opened in (arc.ends,tuple(reversed(arc.ends))):
            if (abs(closed.imag-y)>normal_tol or closed.real < -along_tol or closed.real > width+along_tol
                    or abs(opened.imag-y) < abs(opened-arc.pivot)*.5):
                continue
            for s in leaves:
                if min(max(abs(a-arc.pivot),abs(b-opened)) for a,b in
                       ((s.start,s.end),(s.end,s.start))) <= max(EDGE,normal_tol):
                    swing.append(dict(arc=arc.ref,leaf=s.ref,hinge_mm=xy(arc.pivot),
                                      shut_tip_mm=xy(closed),tip_mm=xy(opened)))
                    break
    # Detect wall behind the actual frame, including a short panel inside registration uncertainty.
    # A mere touch at an end returns zero length; noise beneath EDGE is immaterial.
    traces = [(lo,hi,y,refs) for lo,hi,y,refs in tracks]
    traces += [(p["lo"],p["hi"],p["centre"],p["refs"]) for p in panels]
    occlusion = []
    for lo,hi,y,refs in traces:
        hit = wall.intersection(LineString([(lo,y),(hi,y)]))
        if hit.length > EDGE:
            occlusion.append(dict(overlap_mm=round(hit.length,3),native_refs=refs,
                                  intervals_mm=[[round(l,2),round(r,2)] for l,_,r,_ in intervals(hit)]))
    simple_refs = {ref for run in tracks for ref in run[3]}
    for p in panels:
        simple_refs.update(p["refs"])
    for x,lo,hi,ref in caps:
        if min(abs(x),abs(x-width)) <= along_tol or any(
                min(abs(x-p["lo"]),abs(x-p["hi"]))<=EDGE and
                lo<=p["bottom"]+EDGE and hi>=p["top"]-EDGE for p in panels):
            simple_refs.add(ref)
    def row(v):
        return dict(from_a_mm=round(v[0],2), to_mm=round(v[1],2), offset_mm=round(v[2],2), native_refs=v[3])
    return dict(tracks=[row(r) for r in tracks], panels=panels, staggered_indices=sorted(set(staggered)),
                end_caps=[c[0] if c else None for c in caps_at_end], swings=swing, occlusion=occlusion,
                local_strokes=len(observed), partial_intervals=len(partial), observed_refs=observed,
                unclassified_stroke_refs=sorted(set(observed)-simple_refs))


def snapped_axis(a, b, strokes, along, across):
    """Deskew only within witnessed endpoint uncertainty, using a line at this span."""
    u = (b-a)/abs(b-a)
    w = abs(b-a)
    options = []
    for s in strokes:
        p,q = sorted(((v-a)*u.conjugate() for v in (s.start,s.end)),key=lambda z:z.real)
        if (abs(p.real) <= along and abs(q.real-w) <= along and
                abs(q.imag-p.imag) <= 2*across and abs((q.imag+p.imag)/2) < 170+across
                and q.real-p.real > w*.75):
            options.append((abs(q.imag-p.imag), abs(q.imag+p.imag), (q-p)/abs(q-p)*u))
    basis = min(options, key=lambda q:q[:2])[2] if options else u
    width = ((b-a)*basis.conjugate()).real
    return (a+b)/2-basis*width/2, basis, width


def classify(m, along_tol, width):
    tracks, panels = m["tracks"], m["panels"]
    tied = [p for p in panels if any(p["bottom"]-EDGE <= t["offset_mm"] <= p["top"]+EDGE for t in tracks)]
    cursor = 0
    for p in tied:
        if p["lo"] <= cursor+(along_tol if cursor==0 else EDGE):
            cursor = max(cursor,p["hi"])
    if m["swings"]:
        spans = [sorted([v["hinge_mm"][0],v["shut_tip_mm"][0]]) for v in m["swings"]]
        cursor = 0
        for lo,hi in sorted(spans):
            if lo <= cursor+2*along_tol:
                cursor = max(cursor,hi)
        if cursor>=width-2*along_tol:
            return "swing"
    if (len(tracks)>=2 and len(tied)>=2 and len(m["staggered_indices"])>=2 and
            cursor>=width-along_tol and all(m["end_caps"])):
        return "sliding"
    if len(tracks)>=2 and all(m["end_caps"]) and not panels:
        return "parallel_frame"
    return None


def positive(value):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<=0:
        raise ValueError("expected a positive finite measurement")
    return float(value)


def local_plot(lines, arcs, wall, low, high, width, margin):
    clip = box(-margin,low,width+margin,high)
    patches = wall.intersection(clip)
    def rings(g):
        if g.geom_type=="Polygon" and not g.is_empty:
            yield [list(r.coords) for r in [g.exterior,*g.interiors]]
        for z in getattr(g,"geoms", ()):
            yield from rings(z)
    segments = []
    for role, group in lines.items():
        for s in group:
            part = LineString([xy(s.start),xy(s.end)]).intersection(clip)
            if part.geom_type=="LineString" and not part.is_empty:
                segments.append((role,list(part.coords)))
    for role, group in arcs.items():
        for arc in group:
            z1,z2 = (z-arc.pivot for z in arc.ends)
            direction = 1 if abs(z1*complex(math.cos(math.radians(arc.sweep)),
                                             math.sin(math.radians(arc.sweep)))-z2)<EDGE else -1
            pts = [arc.pivot+z1*complex(math.cos(t),math.sin(t))
                   for t in [direction*math.radians(arc.sweep)*i/32 for i in range(33)]]
            line = LineString(list(map(xy,pts))).intersection(clip)
            if line.geom_type=="LineString" and not line.is_empty:
                segments.append((role,list(line.coords)))
    return dict(walls=list(rings(patches)),segments=segments,extent=clip.bounds,width=width)


def audit(doc, registration, observations, band_px=None, graphics=None):
    if doc.units != 4:
        raise ValueError("DXF model units must be millimetres")
    if (registration.get("source_sha256") and observations.get("source_sha256") != registration["source_sha256"]):
        raise ValueError("registration and reading refer to different source images")
    roles = registration["layers"]
    names = {roles[k].casefold():k for k in ("door","window","ancillary")}
    if len(names)!=3:
        raise ValueError("door, window and ancillary must have independently identifiable layers")
    records, issues = expand(doc)
    strokes, curves = symbols(records,names,issues)
    allwalls = {k.casefold() for k in (roles["bearing"],roles["partition"])}
    wall_names = {name for _,name,_ in records if name.casefold() in allwalls}
    profile = dict(wall_layers=registration.get("wall_layers", {n:"fills" for n in wall_names}),
                   nonwall_fill_layers=[s for s in roles.values() if s.casefold() not in allwalls])
    if profile["wall_layers"]:
        material, ambiguous, wall_issues, _ = decode(FlatDocument(doc,records),profile)
    else:
        material, ambiguous, wall_issues = GeometryCollection(), [], ["未在登记墙层找到墙材"]
    matrix = numbers(registration["source_to_mm"],6)
    projection = GridProjection(registration)
    world = lambda p: complex(*projection.world(p))
    aa,bb,dd,ee,_,_ = matrix
    rows = []
    inventory = indexed(observations["portals"])
    if not inventory:
        raise ValueError("empty independent portal inventory")
    spans = {key:(world(v["a"]),world(v["b"])) for key,v in inventory.items()}
    for key, entry in inventory.items():
        kind = entry["kind"]
        if kind not in {*ROLE,"open"}:
            raise ValueError(f"{key}: unknown portal kind {kind}")
        a,b = spans[key]
        if abs(b-a)<=EDGE:
            raise ValueError(f"{key}: a and b must be distinct")
        px = entry.get("uncertainty_px",entry.get("band_px",observations.get("band_px",band_px)))
        base = dict(id=key,kind=kind,source_ends_px=[entry["a"],entry["b"]],registered_wcs_mm=[xy(a),xy(b)])
        if px is None:
            rows.append(dict(**base,status=REVIEW,pending=["原图登记缺少局部读差 uncertainty_px 或 --band-px"]))
            continue
        px = positive(px)
        u = (b-a)/abs(b-a)
        def projected_tol(vec):
            if registration.get("grid_registration"):
                widths = []
                for x,y in (entry["a"],entry["b"]):
                    center = world((x,y))
                    dx = max(abs((world((x+s*px,y))-center).real) for s in (-1,1))
                    dy = max(abs((world((x,y+s*px))-center).imag) for s in (-1,1))
                    widths.append(math.hypot(dx*vec.real,dy*vec.imag))
                return max(EDGE,*widths)
            return max(EDGE,px*math.hypot(aa*vec.real+dd*vec.imag,bb*vec.real+ee*vec.imag))
        origin,u,width = snapped_axis(a,b,strokes,projected_tol(u),projected_tol(u*1j))
        tol, ntol = projected_tol(u), projected_tol(u*1j)
        def local(z):
            return (z-origin)*u.conjugate()
        trans = [u.real,u.imag,-u.imag,u.real,local(0).real,local(0).imag]
        wall = affine_transform(material,trans)
        ends = end_walls(wall,width,tol)
        nearby = [p for side in ends for probe in side for p in probe["wall_ranges_mm"]]
        limits = [entry.get("wall_thickness_mm"),entry.get("host_depth_mm")]
        depth = next((positive(v) for v in limits if v is not None),None)
        # Always cover both adjoining wall faces. A source width can extend this band.
        low = min([v[0] for v in nearby]+[-(depth/2 if depth else 0)])
        high = max([v[1] for v in nearby]+[(depth/2 if depth else 0)])
        low,high = (low-ntol,high+ntol) if high>low else (-150-ntol,150+ntol)
        # The jamb slice may continue around an orthogonal wall into the room.
        # Keep the wider band to search for symbols, but test blockage over the
        # registered normal depth of this opening when that measurement exists.
        clear_low, clear_high = (-depth/2, depth/2) if depth else (low, high)
        end_trim = min(tol,width*.24)
        core = box(end_trim,clear_low,width-end_trim,clear_high)
        solid = wall.intersection(core)
        lines = defaultdict(list)
        arcs = defaultdict(list)
        for s in strokes:
            lines[names[s.layer.casefold()]].append(Stroke(local(s.start),local(s.end),s.layer,s.ref))
        for arc in curves:
            arcs[names[arc.layer.casefold()]].append(Curve(local(arc.pivot),tuple(local(v) for v in arc.ends),
                                                         arc.sweep,arc.ref,arc.layer))
        neighbors = {}
        for other,(first,last) in spans.items():
            p,q=local(first), local(last)
            if other!=key and max(abs(p.imag),abs(q.imag))<=ntol:
                neighbors[other]=sorted((p.real,q.real))
        metrics = {role:frame_metrics(lines[role],arcs[role],width,tol,ntol,low,high,wall,neighbors.values())
                   for role in ("door","window","ancillary")}
        detected = {role:classify(m,tol,width) for role,m in metrics.items()}
        diag = dict(**base,span_mm=round(width,2),uncertainty_px=px,tolerance_mm=[round(tol,2),round(ntol,2)],
                    inspection_band_from_axis_mm=[round(low,2),round(high,2)],
                    clear_band_from_axis_mm=[round(clear_low,2),round(clear_high,2)],wall_in_clear_mm2=round(solid.area,3),
                    wall_extent_from_a_mm=None if solid.is_empty else [round(v,2) for v in solid.bounds],
                    side_probes=ends,by_layer={roles[k]:v for k,v in metrics.items()},geometry_by_layer=detected)
        fail, pending = [], []
        if wall_issues:
            pending.append("墙材解码有未解实体，见 wall_decode")
        if any(affine_transform(g,trans).intersects(core) for _,g in ambiguous):
            pending.append("洞内有未分类墙轮廓")
        local_issues = [(ref,why) for ref,why,bounds_,_ in issues
                        if bounds_ is None or affine_transform(bounds_,trans).intersects(core.buffer(tol))]
        if local_issues:
            pending.append("局部有未展开或不支持的原生实体")
        diag["unsupported_nearby"] = local_issues
        unresolved = bool(local_issues)
        if solid.area > EDGE*EDGE:
            fail.append("原图登记的净洞内有实墙")
        if any(m["occlusion"] for m in metrics.values()):
            fail.append("本洞框线或扇段中心落在实墙上")
        if not nearby and not depth:
            pending.append("邻近测线没有给出墙厚，按搜索带定位；需核两侧实际墙面或登记墙深")
        supports = entry.get("end_support",["unspecified","unspecified"])
        if len(supports)!=2 or any(v not in ("wall","frame","free","free_end","unspecified") for v in supports):
            raise ValueError(f"{key}: end_support must give a and b separately")
        contacts = []
        for i,(expected,side) in enumerate(zip(supports,ends)):
            # Do not count a distant solid station as a connection at this jamb.
            has_wall = side[0]["wall_at_axis"] or bool(side[0]["wall_ranges_mm"])
            # Look beyond this jamb, on its outer side. The symbol's own cap cannot
            # supply its own adjoining frame. Corner connections need a local reading.
            x = width if i else 0
            sign = 1 if i else -1
            external = []
            for group in lines.values():
                for s in group:
                    a1,b1 = sorted((s.start,s.end),key=lambda z:abs(z.real-x))
                    if (abs(a1.real-x)<=tol and (b1.real-x)*sign>2*tol and low<=a1.imag<=high
                            and abs(a1.imag-b1.imag)<=EDGE):
                        external.append((a1.imag,s.ref))
            levels = sorted(v[0] for v in external)
            contacts.append(dict(wall=has_wall,adjacent_strokes=[v[1] for v in external]))
            frame_end = bool(levels and levels[-1]-levels[0]>EDGE)
            if expected=="wall" and not has_wall:
                (pending if wall_issues or unresolved else fail).append(f"端点 {'ab'[i]} 未接登记墙材")
            elif expected=="frame" and not frame_end:
                pending.append(f"端点 {'ab'[i]} 的连接框需定位")
            elif expected=="unspecified" and not has_wall and not frame_end:
                pending.append(f"端点 {'ab'[i]} 没有量到紧邻墙材或相邻框")
        diag["end_contacts"] = contacts
        for other,(lo,hi) in neighbors.items():
            if min(hi,width)-max(lo,0)>2*tol:
                pending.append(f"与另一条原图登记 {other} 的洞内段重叠，需划分归属")
        unclear = width<=4*tol or entry.get("symbol_legible") is False
        if unclear:
            pending.append("原图分辨率或登记读差不足以独立确认此图符")
        issue = pending if unclear or unresolved else fail
        if kind=="open":
            if any(detected.values()):
                issue.append("净开口上出现完整门窗或栏框图符")
            elif any(m["local_strokes"] for m in metrics.values()):
                pending.append("开口附近有线段，核门槛、端头和框线归属")
        else:
            role = ROLE[kind]
            m = metrics[role]
            desired = "parallel_frame" if kind in ("rail","window") else kind
            good = detected[role]==desired
            conflicts = [roles[k] for k,v in detected.items() if v and k!=role]
            if conflicts:
                issue.append("同一洞内在其他图层量到完整图符："+ "、".join(conflicts))
            if not good:
                if detected[role] and (kind in ("swing","sliding") or detected[role]!="parallel_frame"):
                    issue.append("实际图符几何与登记种类不符")
                elif not m["local_strokes"] and not m["swings"]:
                    issue.append("预期图层在本洞缺图符")
                elif (kind=="sliding" and m["tracks"] and not m["unclassified_stroke_refs"] and
                      not m["swings"] and not m["panels"]):
                    issue.append("本洞只有轨线或端框，缺少可辨的错轨扇段")
                elif (not m["swings"] and not m["partial_intervals"] and not m["panels"] and
                      len(m["tracks"])<2 and m["local_strokes"]<=4):
                    issue.append("只见重线或残线，缺少完整图符")
                else:
                    pending.append("本洞有线条，但未识别为可独立确认的完整图符；需原生实量")
            for field,actual in (("min_panels",len(m["panels"])),("min_tracks",len(m["tracks"]))):
                if field not in entry:
                    continue
                if type(entry[field]) is not int or entry[field]<1:
                    raise ValueError(f"{key}: {field} must be a positive observed lower bound")
                if actual<entry[field]:
                    simple = m["tracks"] and not m["unclassified_stroke_refs"] and not m["swings"]
                    (issue if kind=="sliding" and (good or simple) else pending).append(
                        f"{field} 要求 {entry[field]}，已量到 {actual}")
            if kind=="swing":
                for leaf in entry.get("leaves",[]):
                    h,t = local(world(leaf["hinge"])),local(world(leaf["tip"]))
                    def within(z,coords):
                        delta = z-complex(*coords)
                        return math.hypot(delta.real/tol,delta.imag/ntol)<=1.000001
                    if not any(within(h,v["hinge_mm"]) and within(t,v["tip_mm"]) for v in m["swings"]):
                        (issue if m["swings"] else pending).append("已登记铰点与开启扇端未在同一实际门扇上命中")
        diag.update(status=status(fail,pending),failures=fail,pending=pending)
        rows.append(diag)
        if graphics is not None:
            margin = max(50,min(width*.15,250),tol)
            lo,hi = low-margin*.25,high+margin*.25
            for arc in metrics["door"]["swings"]:
                lo=min(lo,arc["tip_mm"][1]-margin*.1)
                hi=max(hi,arc["tip_mm"][1]+margin*.1)
            graphics[key] = local_plot(lines,arcs,wall,lo,hi,width,margin)
    return dict(status=status(any(r["status"]==FAIL for r in rows),any(r["status"]==REVIEW for r in rows)),
                wall_decode=wall_issues,portals=rows)


def write_svg(path, report, graphics):
    height = 65+200*len(graphics)
    svg = ET.Element("svg", xmlns="http://www.w3.org/2000/svg",width="1000",height=str(height))
    def tag(name, **attrs):
        return ET.SubElement(svg,name,{k.replace("_","-"):str(v) for k,v in attrs.items()})
    def label(x,y,text,size=14):
        tag("text",x=x,y=y,font_size=size,font_family="sans-serif",fill="#21262b").text=text
    tag("rect",width=1000,height=height,fill="white")
    label(16,23,"逐洞定位 · 蓝线 窗　红线 门　绿色 附属　灰色 墙材　橙虚线 原图洞端",16)
    label(16,45,"各格按登记 a → b 旋转，以毫米等比显示。用原图位置及 JSON 句柄定位 DWG。")
    for i,r in enumerate(r for r in report["portals"] if r["id"] in graphics):
        top=64+200*i
        g=graphics[r["id"]]
        label(18,top+15,f'{r["id"]}　{r["kind"]}　{r["status"]}　原图 {r["source_ends_px"]}')
        x0,y0,x1,y1=g["extent"]
        scale=min(950/(x1-x0),155/(y1-y0))
        def screen(p):
            return f"{28+(p[0]-x0)*scale:.2f},{top+35+(y1-p[1])*scale:.2f}"
        for rings in g["walls"]:
            data=" ".join("M"+" L".join(screen(p) for p in ring)+" Z" for ring in rings)
            tag("path",d=data,fill="#929aa4",fill_rule="evenodd")
        colors={"door":"#c02e31","window":"#14608d","ancillary":"#32815a"}
        for role,coords in g["segments"]:
            tag("polyline",points=" ".join(map(screen,coords)),fill="none",stroke=colors[role],stroke_width=1.4)
        for x in (0,g["width"]):
            tag("polyline",points=f"{screen((x,y0))} {screen((x,y1))}",fill="none",
                stroke="#e57924",stroke_width=1,stroke_dasharray="3 3")
    ET.ElementTree(svg).write(path,encoding="utf-8",xml_declaration=True)


def main():
    cli=argparse.ArgumentParser(description=__doc__)
    for k in ("dxf","registration","reading","out"):
        cli.add_argument(k,type=Path)
    cli.add_argument("--band-px",type=float,help="source uncertainty when individual rows omit it")
    cli.add_argument("--svg",type=Path)
    cli.add_argument("--source",type=Path,help="check the independent reading against its original image hash")
    opt=cli.parse_args()
    drawing={}
    try:
        read=lambda path:json.loads(path.read_text(encoding="utf-8"))
        digest=lambda path:hashlib.sha256(path.read_bytes()).hexdigest()
        obs=read(opt.reading)
        if opt.source and obs.get("source_sha256") != digest(opt.source):
            raise ValueError("reading does not match supplied source image")
        report=audit(ezdxf.readfile(opt.dxf),read(opt.registration),obs,opt.band_px,drawing if opt.svg else None)
        report["sha256"]={k:digest(getattr(opt,k)) for k in ("dxf","registration","reading")}
        if opt.svg:
            write_svg(opt.svg,report,drawing)
    except (OSError,ValueError,KeyError,TypeError,ezdxf.DXFError) as exc:
        report=dict(status=REVIEW,error=str(exc))
    opt.out.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(report["status"], opt.out)
    return {PASS:0,FAIL:1,REVIEW:2}[report["status"]]


if __name__=="__main__":
    sys.exit(main())
