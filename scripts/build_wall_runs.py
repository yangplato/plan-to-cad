"""Build straight horizontal/vertical millimetre wall strips and cut the same openings used by symbols."""
from collections import defaultdict
from math import isclose, isfinite

from shapely.geometry import Point, box
from shapely.ops import unary_union

from draw_plan_parts import add_wall_material


EPS = 1e-6  # mm for coincident coordinates; not a construction tolerance


def _xy(point):
    x, y = map(float, point)
    if not isfinite(x) or not isfinite(y):
        raise ValueError("Expected a finite millimetre point")
    return x, y


def _axis(item):
    a, b = _xy(item["a"]), _xy(item["b"])
    same_x = isclose(a[0], b[0], abs_tol=EPS, rel_tol=0)
    same_y = isclose(a[1], b[1], abs_tol=EPS, rel_tol=0)
    if same_x == same_y:
        raise ValueError(f"{item['id']}: expected a nonzero horizontal/vertical axis in mm")
    ti = 0 if same_y else 1
    ni = 1-ti
    return ti, (a[ni]+b[ni])/2, a[ti], b[ti]


def _value(item, field):
    value = float(item[field])
    if not isfinite(value) or value < 0:
        raise ValueError(f"{item['id']}: invalid {field}")
    return value


def _strip(ti, n, start, end, width):
    low, high = n-width/2, n+width/2
    return box(start, low, end, high) if ti == 0 else box(low, start, high, end)


def _wall_footprints(walls):
    by_id, endpoint_widths = {}, defaultdict(list)
    for wall in walls:
        ident = wall["id"]
        if not ident or ident in by_id:
            raise ValueError(f"Duplicate or empty wall ID: {ident}")
        ti, n, at, bt = _axis(wall)
        width = _value(wall, "width_mm")
        if width <= 0:
            raise ValueError(f"{ident}: width_mm must be positive")
        by_id[ident] = (wall, ti, n, at, bt, width)
        for end in ("a","b"):
            xy = tuple(round(v, 6) for v in _xy(wall[end]))
            endpoint_widths[(xy,ti)].append(width)
    footprints, axes = {}, {}
    for ident, (wall,ti,n,at,bt,width) in by_id.items():
        # Where two perpendicular strips END at one axis intersection, extend each to the
        # far face of its neighbour. Collinear material changes and crossing T axes keep butt ends.
        caps = []
        for end in ("a","b"):
            field = f"cap_{end}_mm"
            key = tuple(round(v, 6) for v in _xy(wall[end]))
            caps.append(_value(wall, field) if field in wall
                        else max(endpoint_widths.get((key,1-ti), [0]))/2)
        sign = 1 if bt > at else -1
        start, stop = sorted((at-sign*caps[0], bt+sign*caps[1]))
        footprints[ident] = wall["material"], _strip(ti, n, start, stop, width)
        axes[ident] = (ti, n, start, stop, width)
    return footprints, axes


def _opening_cuts(openings, axes):
    cutters = {}
    for op in openings:
        ident = op["id"]
        if not ident or ident in cutters:
            raise ValueError(f"Duplicate or empty opening ID: {ident}")
        ti,n,at,bt = _axis(op)
        lo,hi = sorted((at,bt))
        hosts = op["hosts"]
        if not isinstance(hosts, (list,tuple)) or not hosts or len(hosts) != len(set(hosts)):
            raise ValueError(f"{ident}: supply a nonempty list of distinct host IDs")
        widths, spans = [], []
        for host in hosts:
            if host not in axes:
                raise ValueError(f"{ident}: unknown host {host}")
            direction, normal, start, stop, width = axes[host]
            if direction != ti or abs(normal-n) > EPS:
                raise ValueError(f"{ident}: jambs and host {host} must share a centreline")
            if min(stop,hi)-max(start,lo) <= EPS:
                raise ValueError(f"{ident}: no net opening length on host {host}")
            widths.append(width)
            spans.append((max(start,lo), min(stop,hi)))
        cursor = lo
        for begin, end in sorted(spans):
            if begin-cursor > EPS:
                raise ValueError(f"{ident}: missing host through part of the opening")
            cursor = max(cursor, end)
        if hi-cursor > EPS or min(begin for begin,_ in spans)-lo > EPS:
            raise ValueError(f"{ident}: net opening extends beyond its host(s)")
        cutter = _strip(ti,n,lo,hi,max(widths))
        for previous, (_,other) in cutters.items():
            if other.intersection(cutter).area > EPS:
                raise ValueError(f"{ident}: net opening overlaps {previous}")
        cutters[ident] = (hosts,cutter)
    return cutters


def draw_wall_runs(msp, walls, openings, layer_gray, *, clear_points_mm=(), return_shapes=()):
    """Draw after validation. 'a', 'b', 'width_mm' use WCS mm; no polygons/room fills are accepted."""
    footprints, axes = _wall_footprints(walls)
    cutters = _opening_cuts(openings, axes)
    cuts = defaultdict(list)
    for hosts, cutter in cutters.values():
        for host in hosts:
            cuts[host].append(cutter)
    final = unary_union([*(poly.difference(unary_union(cuts[ident]))
                           for ident,(_,poly) in footprints.items()), *return_shapes])
    for point in clear_points_mm:
        if final.covers(Point(_xy(point))):
            raise ValueError(f"Known clear point {point} lies in a wall")
    for op in openings:
        ti,n,at,bt = _axis(op)
        sign = 1 if bt>at else -1
        supports = op.get("jamb_support", ["wall","wall"])
        if len(supports) != 2 or any(s not in ("wall","frame","free_end") for s in supports):
            raise ValueError(f"{op['id']}: jamb_support must specify wall, frame or free_end for a and b")
        for t, support in zip((at-sign*0.001, bt+sign*0.001), supports):
            point = (t,n) if ti == 0 else (n,t)
            if support == "wall" and not final.covers(Point(point)):
                raise ValueError(f"{op['id']}: missing wall at jamb near {point}")
    # Existing writer rejects any undisclosed crossing wall in a cavity, resolves material
    # overlaps by priority, and emits closed LWPOLYLINEs and HATCHes from identical rings.
    add_wall_material(msp, footprints, cutters, layer_gray)
    return {"wall_count":len(footprints), "opening_count":len(cutters)}
