"""Measure a corner window at its two actual rail ends. Coordinates are in model mm."""
from itertools import combinations
import math

from shapely.geometry import LineString, Point, Polygon
from shapely.ops import substring, unary_union


def curve_path(entity, tolerance):
    if entity.dxftype() not in ("ARC", "ELLIPSE"):
        return None
    pts = list(entity.flattening(tolerance / 8))
    if len(pts) < 3 or any(abs(v.z) > tolerance or not all(math.isfinite(c) for c in v) for v in pts):
        return None
    return LineString([(v.x, v.y) for v in pts])


def curved_aperture(rails, op, margin, tolerance=.1):
    """Get the interior of a four-track bend from its real outer and inner rails."""
    if len(rails) != 4 or any(r is None or r.length <= 2 * margin for r in rails):
        return None
    a, b = Point(op["a"]), Point(op["b"])
    paths = []
    for rail in rails:
        p, q = Point(rail.coords[0]), Point(rail.coords[-1])
        forward, backward = p.distance(a)+q.distance(b), q.distance(a)+p.distance(b)
        if abs(forward-backward) <= tolerance or p.distance(q) <= tolerance:
            return None
        paths.append(rail if forward < backward else LineString(rail.coords[::-1]))
    if any(p.distance(q) <= tolerance for p,q in combinations(paths,2)):
        return None
    outer_pair = None
    for end in (0,-1):
        points = [Point(p.coords[end]) for p in paths]
        pair = max(combinations(range(len(points)),2), key=lambda ij: points[ij[0]].distance(points[ij[1]]))
        section = LineString([points[i] for i in pair])
        if any(p.distance(section)>tolerance for p in points) or outer_pair not in (None,pair):
            return None
        outer_pair = pair
    left, right = (paths[i] for i in outer_pair)
    full = Polygon([*left.coords,*reversed(right.coords)])
    if not full.is_valid or any(not full.buffer(tolerance).covers(p) for p in paths):
        return None
    first, last = (substring(r, margin, r.length-margin) for r in (left,right))
    region = Polygon([*first.coords,*reversed(last.coords)])
    return region if region.is_valid and region.area > tolerance**2 else None


def inspect_ends(out, op, rails, local_lines, all_lines, material, tolerance, free_end_supported,
                 registered_handles=None):
    ident = op["id"]
    if len(rails) < 2:
        out.add("curved-rails/"+ident, "pending", reason="缺可独立对应的同向曲框轨线")
        return
    a, b = Point(op["a"]), Point(op["b"])
    ends = [[], []]
    for rail in rails:
        p, q = Point(rail.coords[0]), Point(rail.coords[-1])
        forward, reverse = p.distance(a)+q.distance(b), q.distance(a)+p.distance(b)
        if abs(forward-reverse) <= tolerance or p.distance(q) <= tolerance:
            out.add("curved-rails/"+ident, "pending", reason="闭环或分段轨线无法定位到两处洞端")
            return
        if reverse < forward:
            p, q = q, p
        ends[0].append(p)
        ends[1].append(q)
    supports = op.get("jamb_support", ["wall", "wall"])
    for i, points in enumerate(ends):
        side = "ab"[i]
        key = f"end/{ident}/{side}"
        p, q = max(combinations(points, 2), key=lambda pair: pair[0].distance(pair[1]))
        section = LineString([p, q])
        duplicate = any(first.distance(second) <= tolerance for first, second in combinations(points, 2))
        if duplicate or section.length <= tolerance or any(v.distance(section) > tolerance for v in points):
            out.add(key, "pending", reason="曲轨端点不在同一截面，须实查分段、退台或错位",
                    rail_endpoints_mm=[list(v.coords[0]) for v in points])
            continue
        cap = any(section.hausdorff_distance(s) <= tolerance for s in local_lines)
        values = dict(rail_count=len(rails), section_mm=[list(p.coords[0]), list(q.coords[0])],
                      depth_mm=section.length, own_cap=cap)
        if supports[i] == "wall":
            missing = section.difference(material.buffer(tolerance)).length
            out.test(key, cap and missing <= tolerance, unsupported_length_mm=missing, **values)
        elif supports[i] == "frame":
            named = op.get("frame_neighbors", {}).get(side)
            matching = []
            duplicate_caps = []
            for other, paths in all_lines.items():
                if other == ident or (named and other != named):
                    continue
                # A shared complete cap, or a cap joining every distinct rail end.
                shared = any(section.hausdorff_distance(s) <= tolerance for s in paths)
                tips = {tuple(t) for path in paths for t in (path.coords[0], path.coords[-1])}
                continuous = all(any(v.distance(Point(t)) <= tolerance for t in tips) for v in points)
                if shared or cap and continuous:
                    matching.append(other)
                if (shared and cap and registered_handles is not None and
                        registered_handles.get(ident) and registered_handles.get(other) and
                        registered_handles[ident].isdisjoint(registered_handles[other])):
                    duplicate_caps.append(other)
            state = "pass" if len(matching)==1 else "pending" if len(matching)>1 else "fail"
            out.add(key, state, adjacent_frames=matching, **values)
            if duplicate_caps:
                out.add(f"duplicate-cap/{ident}/{side}", "fail", adjacent_frames=duplicate_caps,
                        reason="两个独立登记的窗实体在同一截面各画了一条完整端封")
        elif supports[i] == "free_end":
            out.add(key, "pass" if cap and free_end_supported(side) else "pending", **values)
        else:
            out.add(key, "pending", reason="待核曲框支承类别", **values)
    embedded = unary_union([*rails, *local_lines]).intersection(material.buffer(-tolerance)).length
    out.test("frame-in-wall/"+ident, embedded<=tolerance, crossing_length_mm=embedded)
