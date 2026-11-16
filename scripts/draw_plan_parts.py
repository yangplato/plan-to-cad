"""Write planar wall boundaries, wall fill and native dimensions to a millimetre DXF.

Wall input: {wall_id: (layer_name, Shapely Polygon or MultiPolygon)}.
Opening input: {opening_id: ([host_wall_ids], cutting_polygon)}.
Use native CAD curves for walls that must retain an analytic arc.
"""
from collections import defaultdict
from math import isfinite

from shapely.geometry import MultiPolygon, Polygon
from shapely.ops import unary_union


def _positive_area(geometry, label):
    if (not isinstance(geometry, (Polygon, MultiPolygon)) or not geometry.is_valid
            or geometry.is_empty or not isfinite(geometry.area) or geometry.area <= 0):
        raise ValueError(f"{label}: expected a valid polygon with positive area")


def _components(geometry):
    if isinstance(geometry, Polygon):
        return [geometry] if not geometry.is_empty else []
    return [polygon for part in getattr(geometry, "geoms", ())
            for polygon in _components(part)]


def add_wall_material(msp, walls, openings, layer_gray):
    """Draw once. layer_gray lists material precedence at a junction, highest first."""
    if msp.doc.units != 4:
        raise ValueError("Set the drawing model units to millimetres first")
    if not walls:
        raise ValueError("Supply the wall footprints")
    for layer, gray in layer_gray.items():
        if layer not in msp.doc.layers or not isinstance(gray, int) or not 0 <= gray <= 255:
            raise ValueError(f"{layer}: set up the drawing layer and its grey level")
    for wall_id, (layer, footprint) in walls.items():
        _positive_area(footprint, wall_id)
        if layer not in layer_gray:
            raise ValueError(f"{wall_id}: unknown wall layer {layer}")

    cuts = defaultdict(list)
    for opening_id, (hosts, cutter) in openings.items():
        _positive_area(cutter, opening_id)
        if isinstance(hosts, str) or not hosts:
            raise ValueError(f"{opening_id}: supply a list of host wall IDs")
        for wall_id in hosts:
            if wall_id not in walls or walls[wall_id][1].intersection(cutter).area <= 0:
                raise ValueError(f"{opening_id}: cutting region misses host {wall_id}")
            cuts[wall_id].append(cutter)

    groups = defaultdict(list)
    for wall_id, (layer, footprint) in walls.items():
        groups[layer].append(footprint.difference(unary_union(cuts[wall_id])))
    all_material = unary_union([item for group in groups.values() for item in group])
    for opening_id, (_, cutter) in openings.items():
        overlap = all_material.intersection(cutter).area
        if overlap > 1e-6:  # mm², numerical noise only
            raise ValueError(f"{opening_id}: another wall occupies {overlap:g} mm² of the opening")

    occupied = Polygon()
    for layer, gray in layer_gray.items():
        raw = unary_union(groups[layer])
        face = raw.difference(occupied)
        occupied = occupied.union(raw)
        for polygon in _components(face):
            hatch = msp.add_hatch(dxfattribs={"layer": layer})
            hatch.rgb = (gray, gray, gray)
            for index, ring in enumerate([polygon.exterior, *polygon.interiors]):
                points = list(ring.coords)[:-1]
                boundary = msp.add_lwpolyline(points, close=True, dxfattribs={"layer": layer})
                path = hatch.paths.add_polyline_path(points, is_closed=True, flags=1 if index == 0 else 0)
                hatch.associate(path, [boundary])


def add_mm_dimension(msp, p1, p2, base, angle, layer="尺寸", dimstyle="Standard"):
    """p1/p2 are measured points. base locates the dimension line. angle is in degrees."""
    if msp.doc.units != 4 or layer not in msp.doc.layers or dimstyle not in msp.doc.dimstyles:
        raise ValueError("Check drawing units, dimension layer and dimension style")
    style = msp.doc.dimstyles.get(dimstyle)
    font_style = style.dxf.get("dimtxsty", None)
    if not font_style:
        style.dxf.dimtxsty = "Standard"
    elif font_style not in msp.doc.styles:
        raise ValueError(f"{dimstyle}: missing text style {font_style!r}")
    dim = msp.add_linear_dim(base=base, p1=p1, p2=p2, angle=angle, text="<>",
                             dimstyle=dimstyle, dxfattribs={"layer": layer})
    dim.render()
    return dim.dimension
