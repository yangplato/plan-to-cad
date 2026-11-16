#!/usr/bin/env python3
"""Check registered clear openings and solid wall patches in a modelspace DXF (mm)."""
import argparse
from collections import Counter, defaultdict
from hashlib import sha256
import json
import math
from pathlib import Path
import sys

import ezdxf
from ezdxf.entities.boundary_paths import LineEdge, PolylinePath
from shapely.affinity import affine_transform
from shapely.geometry import GeometryCollection, LineString, Polygon
from shapely.ops import unary_union

AREA_ROUNDOFF = 1e-4  # mm², only for overlay arithmetic
EDGE_ROUNDOFF = 1e-5  # mm, only for recognising duplicate fill outlines
FILL_TYPES = {'HATCH', 'SOLID', 'TRACE'}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def numbers(value, count):
    require(isinstance(value, (tuple, list)) and len(value) == count and
            all(type(x) in (float, int) and math.isfinite(x) for x in value),
            f'expected {count} finite coordinates')
    return tuple(value)


def polygon(coords):
    poly = Polygon(coords)
    require(poly.is_valid and not poly.is_empty and math.isfinite(poly.area) and poly.area > 0,
            'invalid or empty polygon')
    return poly


def nested_area(rings):
    """Even/odd fill of simple, strictly nested or disjoint loops."""
    require(rings, 'no boundary rings')
    area = GeometryCollection()
    for i, loop in enumerate(rings):
        require(not any(loop.boundary.intersects(prev.boundary) for prev in rings[:i]),
                'ring boundaries touch, coincide or cross')
        area = area.symmetric_difference(loop)
    return area


def planar_entity(e):
    extrusion = e.dxf.get('extrusion', (0, 0, 1)) if e.dxf.is_supported('extrusion') else (0, 0, 1)
    require(abs(extrusion[0]) < 1e-12 and abs(extrusion[1]) < 1e-12 and abs(abs(extrusion[2])-1) < 1e-12,
            'tilted entity')
    if e.dxf.is_supported('thickness'):
        require(e.dxf.get('thickness', 0) == 0, '3D thickness')


def xy_plane(vertices):
    result = []
    for v in vertices:
        require(all(math.isfinite(t) for t in v) and abs(v.z) <= EDGE_ROUNDOFF,
                'non-finite geometry or entity outside z=0')
        result.append((v.x, v.y))
    return result


def hatch_area(e):
    require(e.dxf.hatch_style == 0, 'unsupported island style')
    rings = []
    for boundary in e.paths:
        if isinstance(boundary, PolylinePath):
            require(boundary.is_closed and all(v[2] == 0 for v in boundary.vertices),
                    'open or curved hatch polyline')
            points = [(x, y) for x, y, _ in boundary.vertices]
        else:
            edges = boundary.edges
            require(edges and all(isinstance(edge, LineEdge) for edge in edges), 'curved or empty edge path')
            require(all(edge.end.distance(edges[(i + 1) % len(edges)].start) <= EDGE_ROUNDOFF for i, edge in enumerate(edges)),
                    'unordered or disconnected hatch edges')
            points = [edge.start for edge in edges]
        vertices = (e.ocs().to_wcs((x, y, e.dxf.elevation.z)) for x, y in points)
        rings.append(polygon(xy_plane(vertices)))
    return nested_area(rings)


def outline(e):
    kind = e.dxftype()
    if kind == 'LINE':
        return LineString(xy_plane((e.dxf.start, e.dxf.end))), False
    require(kind in ('LWPOLYLINE', 'POLYLINE') and (kind != 'POLYLINE' or e.is_2d_polyline),
            f'unsupported {kind}')
    require(not e.has_arc and not e.has_width, 'curved or wide polyline')
    vertices = e.vertices_in_wcs() if kind == 'LWPOLYLINE' else e.points_in_wcs()
    points = xy_plane(vertices)
    require(len(points) >= 2, 'empty outline')
    closed = e.is_closed or points[0] == points[-1]
    if closed and points[0] != points[-1]:
        points.append(points[0])
    return LineString(points), closed


def block_warnings(doc, wall_names):
    """An unexpanded block on another layer may hold named wall-layer members."""
    visited = set()
    issues = []

    def inspect(name):
        if name in visited:
            return
        visited.add(name)
        block = doc.blocks.get(name)
        if block is None or block.block.is_xref or block.block.is_xref_overlay:
            issues.append(f'unresolved or external INSERT {name}')
            return
        for child in block:
            if child.dxf.layer.casefold() in wall_names:
                issues.append(f'wall-layer member inside INSERT {name}')
                return
            if child.dxftype() == 'INSERT':
                inspect(child.dxf.name)

    for ref in doc.modelspace().query('INSERT'):
        if ref.dxf.layer.casefold() not in wall_names:
            inspect(ref.dxf.name)
    return issues


def decode(doc, profile):
    layers = profile['wall_layers']
    require(isinstance(layers, dict) and layers, 'wall_layers is empty')
    modes = {k.casefold(): v for k, v in layers.items()}
    require(len(modes) == len(layers) and all(v in ('fills', 'nested_loops') for v in modes.values()),
            'duplicate wall layer or unsupported mode')
    nonwall = {s.casefold() for s in profile.get('nonwall_fill_layers', [])}
    require(not nonwall.intersection(modes), 'layer also declared nonwall')
    require(doc.units == 4, f'millimetre modelspace required, $INSUNITS={doc.units}')
    grouped = defaultdict(list)
    issues = block_warnings(doc, modes)
    other = set()
    for e in doc.modelspace():
        name = e.dxf.layer.casefold()
        if name in modes:
            grouped[name].append(e)
        elif e.dxftype() in FILL_TYPES and name not in nonwall:
            other.add(e.dxf.layer)
    issues.extend(f'unclassified modelspace fill layer {s}' for s in sorted(other))
    material, ambiguous, inventory = [], [], {}
    for name, mode in modes.items():
        areas, lines, rings = [], [], []
        for e in grouped[name]:
            kind = e.dxftype()
            try:
                planar_entity(e)
                if mode == 'fills' and kind in FILL_TYPES:
                    area = hatch_area(e) if kind == 'HATCH' else polygon(xy_plane(e.wcs_vertices()))
                    areas.append(area)
                else:
                    line, closed = outline(e)
                    if mode == 'nested_loops':
                        require(closed, 'open wall contour in nested_loops mode')
                        rings.append((e.dxf.handle, polygon(line.coords)))
                    else:
                        lines.append((e.dxf.handle, line, closed))
            except Exception as exc:
                issues.append(f'{e.dxf.layer} #{e.dxf.handle} {kind}: {exc}')
        if mode == 'nested_loops' and rings:
            try:
                loops = [shape for _, shape in rings]
                decoded = nested_area(loops)
                for i, (handle, shape) in enumerate(rings):
                    related = any(shape.contains(other) or other.contains(shape)
                                  for j, other in enumerate(loops) if i != j)
                    if not related:
                        # A single perimeter does not specify wall thickness or a room hole.
                        ambiguous.append((f'{name} #{handle}', shape))
                        decoded = decoded.difference(shape)
                if not decoded.is_empty:
                    areas.append(decoded)
            except ValueError as exc:
                issues.append(f'{name}: {exc}')
        if not areas:
            issues.append(f'{name}: no reliably decoded wall material')
        boundaries = unary_union([area.boundary for area in areas]).buffer(EDGE_ROUNDOFF)
        for handle, line, closed in lines:
            if boundaries.covers(line):
                continue
            if closed and line.is_ring:
                ambiguous.append((f'{name} #{handle}', polygon(line.coords)))
            else:
                issues.append(f'{name} #{handle}: unmatched open/invalid wall outline')
        inventory[name] = dict(Counter(e.dxftype() for e in grouped[name]))
        material.extend(areas)
    return unary_union(material), ambiguous, sorted(set(issues)), inventory


def register(expected, profile):
    require(expected['source'] and expected['source'] == profile['source'], 'source identifiers differ or missing')
    # Shapely order is a,b,d,e,xoff,yoff: X=a*u+b*v+xoff, Y=d*u+e*v+yoff.
    matrix = numbers(profile['source_to_mm'], 6)
    a, b, d, e, xoff, yoff = matrix
    require(abs(a*e-b*d) > 1e-12, 'singular source_to_mm')
    limit, = numbers([profile['holdout_limit_mm']], 1)
    require(limit >= 0 and profile['holdouts'], 'independent registration holdout required')
    residuals = []
    for item in profile['holdouts']:
        u, v = numbers(item['source_xy'], 2)
        ref = numbers(item['cad_mm'], 2)
        delta = math.dist((a*u+b*v+xoff, d*u+e*v+yoff), ref)
        require(math.isfinite(delta) and delta <= limit, f'registration holdout residual {delta:.3f} > {limit} mm')
        residuals.append(round(delta, 4))
    probes = {}
    for item in expected['probes']:
        ident, role = item['id'], item['kind']
        require(isinstance(ident, str) and ident.strip() and ident not in probes, 'empty or duplicate probe id')
        require(role in ('clear', 'solid'), f'{ident}: unknown probe kind')
        source_poly = polygon([numbers(v, 2) for v in item['polygon']])
        geom = affine_transform(source_poly, matrix)
        require(geom.is_valid and math.isfinite(geom.area) and geom.area > AREA_ROUNDOFF, f'{ident}: probe too small')
        supports = item.get('supports', [])
        pending = item.get('unverified_ends', [])
        require(isinstance(supports, list) and isinstance(pending, list) and
                all(isinstance(v, str) and v.strip() for v in supports + pending),
                f'{ident}: supports and unverified_ends must be lists of nonempty strings')
        if role == 'clear' and not supports and not pending:
            pending = ['no adjacent wall supports or CAD connection evidence registered']
        probes[ident] = (role, geom, supports, pending)
    require(probes, 'empty probe inventory')
    for ident, (role, geom, supports, _) in probes.items():
        if role == 'clear':
            require(all(p in probes and probes[p][0] == 'solid' for p in supports),
                    f'{ident}: identify adjacent solid support(s)')
            require(all(geom.intersection(probes[p][1]).area <= AREA_ROUNDOFF for p in supports),
                    f'{ident}: contradictory overlapping clear and solid areas')
    return residuals, probes


def audit(doc, expected, profile):
    residuals, probes = register(expected, profile)
    wall, ambiguous, issues, inventory = decode(doc, profile)
    rows = {}
    for ident, (role, region, _, _) in probes.items():
        occupied = region.intersection(wall).area
        missing = region.difference(wall).area
        possible = [h for h, area in ambiguous if area.intersection(region).area > AREA_ROUNDOFF]
        unknown = bool(issues or possible)
        if role == 'clear':
            status = 'fail' if occupied > AREA_ROUNDOFF else ('unverified' if unknown else 'pass')
        else:
            status = 'pass' if missing <= AREA_ROUNDOFF else ('unverified' if unknown else 'fail')
        rows[ident] = {'kind': role, 'status': status, 'region_mm2': round(region.area, 4),
                       'wall_mm2': round(occupied, 4), 'missing_mm2': round(missing, 4),
                       'wall_percent': round(100 * occupied / region.area, 4)}
        if possible:
            rows[ident]['ambiguous_wall_contours'] = possible
    for ident, (role, _, supports, pending) in probes.items():
        if role == 'clear':
            row = rows[ident]
            row['clear_zone_status'] = row['status']
            row['supports'] = {s: rows[s]['status'] for s in supports}
            states = {row['status'], *row['supports'].values()}
            if pending:
                row['unverified_ends'] = pending
                states.add('unverified')
            row['status'] = 'fail' if 'fail' in states else ('unverified' if 'unverified' in states else 'pass')
    states = {r['status'] for r in rows.values()}
    status = 'fail' if 'fail' in states else ('unverified' if issues or 'unverified' in states else 'pass')
    return {'scope': 'registered wall-material probes only', 'status': status, 'wall_layers': inventory,
            'holdout_residuals_mm': residuals, 'global_uncertainties': issues,
            'local_uncertain_contours': [h for h, _ in ambiguous], 'probes': rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ('dxf', 'expected', 'mapping'):
        parser.add_argument(arg, type=Path)
    args = parser.parse_args()
    try:
        result = audit(ezdxf.readfile(args.dxf), json.loads(args.expected.read_text(encoding='utf-8')),
                       json.loads(args.mapping.read_text(encoding='utf-8')))
        result['sha256'] = {k: sha256(p.read_bytes()).hexdigest() for k, p in vars(args).items()}
    except Exception as exc:
        result = {'status': 'unverified', 'error': str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return {'pass': 0, 'fail': 1, 'unverified': 2}[result['status']]


if __name__ == '__main__':
    sys.exit(main())
