"""Resolve registered wall junctions, opening faces and multitrack corners in WCS millimetres."""
import math

from ezdxf.math import Vec2
from shapely.geometry import LineString, Point, Polygon
from build_wall_runs import _axis
from source_geometry_review import parts

EPS = 1e-6


def draw_frame_path(msp, points, offsets, layer, cap_start=True, cap_end=True):
    """Draw one continuous polyline per rail. Positive offset is left of travel."""
    points = [Vec2(p) for p in points]
    offsets = sorted(map(float, offsets))
    if (len(points) < 2 or len(offsets) < 2 or len(set(offsets)) != len(offsets)
            or not all(math.isfinite(v) for v in offsets)
            or not all(math.isfinite(v) for p in points for v in p)):
        raise ValueError("Supply a path and distinct finite offsets in millimetres")
    vectors = [b-a for a, b in zip(points, points[1:])]
    if any(v.magnitude <= EPS for v in vectors) or not LineString(points).is_simple:
        raise ValueError("Review the self-crossing or zero-length frame path")
    axes = [v.normalize() for v in vectors]
    normals = [v.orthogonal() for v in axes]
    tracks = []
    for offset in offsets:
        track = [points[0] + normals[0]*offset]
        for i, p in enumerate(points[1:-1], 1):
            bisector = normals[i-1] + normals[i]
            divisor = bisector.dot(normals[i])
            if divisor < EPS:
                raise ValueError("Frame reverses at a corner")
            track.append(p + bisector*(offset/divisor))
        track.append(points[-1] + normals[-1]*offset)
        if (any((b-a).dot(axis) <= EPS for a, b, axis in zip(track, track[1:], axes))
                or not LineString(track).is_simple):
            raise ValueError("Track cannot turn within its registered length")
        tracks.append(track)
    attrs = {"layer": layer}
    result = [msp.add_lwpolyline(track, dxfattribs=attrs) for track in tracks]
    for index, cap in [(0, cap_start), (-1, cap_end)]:
        if cap:
            result.append(msp.add_line(tracks[0][index], tracks[-1][index], dxfattribs=attrs))
    return result


def draw_stair_break(msp, treads, edge_a, edge_b, layer):
    """Draw traced break edges. Interrupt only the treads inside this flight's break band."""
    first, second = list(edge_a), list(edge_b)
    if min(len(first),len(second))<2:
        raise ValueError("Trace both open edges of the stair break")
    band = Polygon(first+list(reversed(second)))
    if not band.is_valid or band.area<=EPS:
        raise ValueError("Register both edges in the same direction")
    visible = []
    for start, end in treads:
        tread = LineString([start,end])
        if tread.length<=EPS:
            raise ValueError("A stair tread needs two distinct ends")
        visible.extend(parts(tread.difference(band),"LineString"))
    attrs={"layer":layer}
    entities=[msp.add_line(line.coords[0],line.coords[-1],dxfattribs=attrs)
              for line in visible if line.length>EPS]
    entities.extend(msp.add_lwpolyline(edge,dxfattribs=attrs) for edge in (first,second))
    return entities


def resolve_wall_nodes(walls, nodes):
    """Move only the named wall ends onto their common orthogonal node."""
    by_id = {w["id"]: w for w in walls}
    used = set()
    for node in nodes:
        members = node["members"]
        if len(members) < 2:
            raise ValueError("A wall junction requires two or more wall ends")
        location = {}
        ends = []
        for member in members:
            key = (member["wall"], member["end"])
            if key in used or key[1] not in ("a", "b"):
                raise ValueError(f"Repeated or invalid wall end {key}")
            used.add(key)
            wall = by_id[key[0]]
            axis, normal, _, _ = _axis(wall)
            if 1-axis in location and abs(location[1-axis] - normal) > EPS:
                raise ValueError("Wall ends on parallel distinct axes need a separate offset detail")
            location[1-axis] = normal
            ends.append((wall, key[1]))
        if len(location) != 2:
            raise ValueError("Supply both orthogonal directions at a wall corner")
        p = [location[0], location[1]]
        for wall, end in ends:
            opposite = wall["b" if end == "a" else "a"]
            if (Vec2(p)-Vec2(opposite)).dot(Vec2(wall[end])-Vec2(opposite)) <= EPS:
                raise ValueError(f"{wall['id']}: junction would reverse or erase a wall")
            wall[end] = p.copy()


def bind_hinges(opening, material):
    """Read the actual local support face behind each swing jamb before inserting the symbol."""
    a, b = Vec2(opening["a"]), Vec2(opening["b"])
    axis = (b-a).normalize()
    normal = axis.orthogonal()
    for leaf in opening["leaves"]:
        start = leaf["hinge"] == "a"
        jamb = a if start else b
        along = axis if start else -axis
        toward = Vec2(leaf.get("toward") or opening["toward"])
        normal_into = normal if (toward-jamb).dot(normal) > 0 else -normal
        station = jamb - along * .001
        reach = max(opening["wall_thickness_mm"]*2, 1000.)
        cut = LineString([station-normal_into*reach, station+normal_into*reach])
        hits = [line for line in parts(material.intersection(cut), "LineString")
                if line.distance(Point(station)) <= EPS]
        if len(hits) != 1:
            raise ValueError(f"{opening['id']}: no unique material at hinge {leaf['hinge']}. Check pier or model its frame")
        limits = [(Vec2(p)-station).dot(normal_into) for p in hits[0].coords]
        face = leaf.get("hinge_face", opening.get("hinge_face", "approach"))
        if face not in ("approach", "receiving"):
            raise ValueError(f"{opening['id']}: ambiguous door face or crossing wall. Locate actual support")
        offset = min(limits) if face == "approach" else max(limits)
        if abs(offset) >= reach-EPS:
            raise ValueError(f"{opening['id']}: selected face runs into a crossing wall")
        inset = float(leaf.get("jamb_inset_mm", opening.get("jamb_inset_mm", 0)))
        if inset and not (opening.get("reference_block") or opening.get("reference_id")):
            raise ValueError(f"{opening['id']}: an inset hinge needs a drawn, measured frame")
        leaf["actual_pivot_mm"] = list(jamb + normal_into*offset)
        leaf["jamb_face_range_mm"] = [list(jamb+normal_into*min(limits)),list(jamb+normal_into*max(limits))]
