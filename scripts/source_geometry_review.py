"""Compare independent source expectations with saved DXF geometry.

CLI: source_geometry_review.py FINAL.dxf SPEC.json --source IMAGE --report REPORT.json
Spec review_version is 'source-geometry/1'. No drawing is changed. Model and
source hashes are required. A rigid frame maps reference coordinates into CAD.
Image uncertainty is supplied per check; EPS is only numerical roundoff.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

import ezdxf
from ezdxf.disassemble import recursive_decompose
from ezdxf.entities.boundary_paths import PolylinePath
from ezdxf.path import make_path
from shapely.affinity import affine_transform
from shapely.geometry import LineString, Polygon
from shapely.ops import unary_union

from check_wall_probes import hatch_area, outline, planar_entity

VERSION = "source-geometry/1"
EPS = 0.01  # mm of numerical comparison, NOT source or construction tolerance
KINDS = {"wall_section", "line_section", "span", "control", "clear_region"}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def number(value, name, minimum=None):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"{name}: expected finite number")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name}: must be >= {minimum}")
    return float(value)


def pair(value, name):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{name}: expected two finite coordinates")
    return [number(v, name) for v in value]


def names(value, name, nonempty=True):
    if (not isinstance(value, list) or (nonempty and not value) or
            not all(isinstance(v, str) and v.strip() for v in value) or
            len(value) != len(set(value))):
        raise ValueError(f"{name}: expected distinct nonempty names")
    return value


def valid_hash(value):
    return (isinstance(value, str) and len(value) == 64 and
            all(c in "0123456789abcdef" for c in value.lower()))


def frame(spec):
    f = spec.get("frame", {})
    o = pair(f.get("origin_mm", [0, 0]), "frame.origin_mm")
    x = pair(f.get("x_direction", [1, 0]), "frame.x_direction")
    y = pair(f.get("y_direction", [0, 1]), "frame.y_direction")
    if (abs(math.hypot(*x)-1) > 1e-8 or abs(math.hypot(*y)-1) > 1e-8 or
            abs(x[0]*y[0]+x[1]*y[1]) > 1e-8):
        raise ValueError("frame must be rigid; no model scaling is permitted")
    matrix = [x[0], x[1], y[0], y[1], -sum(a*b for a, b in zip(o, x)),
              -sum(a*b for a, b in zip(o, y))]

    def point(p):
        p = pair(p, "CAD point")
        return [sum((p[i]-o[i])*v[i] for i in range(2)) for v in (x, y)]
    return matrix, point


def uncertainty(row, count=2):
    value = row.get("image_uncertainty_mm")
    if type(value) in (int, float):
        return [number(value, "image_uncertainty_mm", 0)]*count
    if isinstance(value, list) and len(value) == count:
        return [number(v, "image_uncertainty_mm", 0) for v in value]
    raise ValueError("image_uncertainty_mm must explicitly specify each source uncertainty")


def parts(geometry, kind):
    if geometry.is_empty:
        return []
    if geometry.geom_type == kind:
        return [geometry]
    if hasattr(geometry, "geoms"):
        return [p for child in geometry.geoms for p in parts(child, kind)]
    return []


def interval_geometry(axis, at, low, high):
    return LineString([(low, at), (high, at)]) if axis == "x" else LineString([(at, low), (at, high)])


def wall_material(doc, layers, matrix):
    """Prefer actual fills, so an outline and its hatch are not counted twice."""
    records, questions = [], []
    for layer in layers:
        entities = [e for e in doc.modelspace() if e.dxf.layer == layer]
        fills = [e for e in entities if e.dxftype() in {"HATCH", "SOLID", "TRACE"}]
        candidates = fills or [e for e in entities if e.dxftype() in {"LWPOLYLINE", "POLYLINE"}]
        if not candidates:
            questions.append(f"wall layer {layer}: no decodable material")
        for e in candidates:
            try:
                planar_entity(e)
                equivalent_style = None
                if e.dxftype() == "HATCH":
                    style = e.dxf.hatch_style
                    if style in (1, 2):
                        paths = list(e.paths)
                        # With exactly one simple closed polyline and no islands,
                        # normal/outer/ignore-islands fill the same planar polygon.
                        # Copy only in memory; retain the original entity and style.
                        if (len(paths) != 1 or not isinstance(paths[0], PolylinePath)
                                or not paths[0].is_closed
                                or any(vertex[2] != 0 for vertex in paths[0].vertices)):
                            raise ValueError("non-normal hatch style needs one closed straight polyline, no islands")
                        normalized = e.copy()
                        normalized.dxf.hatch_style = 0
                        shape = hatch_area(normalized)  # also proves valid, simple, nonempty geometry
                        equivalent_style = style
                    else:
                        shape = hatch_area(e)
                elif e.dxftype() in {"SOLID", "TRACE"}:
                    vertices = list(e.wcs_vertices())
                    if any(abs(v.z) > EPS for v in vertices):
                        raise ValueError("wall outside model XY plane")
                    shape = Polygon([(v.x, v.y) for v in vertices])
                else:
                    line, closed = outline(e)
                    if not closed:
                        raise ValueError("open wall outline")
                    shape = Polygon(line.coords)
                if shape.is_empty or not shape.is_valid or shape.area <= EPS*EPS:
                    raise ValueError("invalid wall polygon")
                handles = [e.dxf.handle]
                if e.dxftype() == "HATCH":
                    handles += [h for path in e.paths for h in path.source_boundary_objects]
                records.append({"shape": affine_transform(shape, matrix), "handles": list(dict.fromkeys(handles)),
                                "layer": layer, "equivalent_hatch_style": equivalent_style})
            except Exception as exc:
                questions.append(f"{layer}/{e.dxf.handle}: {exc}")
        if fills:
            boundaries = unary_union([r["shape"].boundary for r in records if r["layer"] == layer]).buffer(EPS)
            for e in entities:
                if e.dxftype() not in {"LINE", "LWPOLYLINE", "POLYLINE"}:
                    continue
                try:
                    line, _ = outline(e)
                    if not boundaries.covers(affine_transform(line, matrix)):
                        questions.append(f"{layer}/{e.dxf.handle}: wall outline differs from actual fill")
                except Exception as exc:
                    questions.append(f"{layer}/{e.dxf.handle}: undecodable wall outline/{exc}")
        if not fills:
            for i, a in enumerate(records):
                if a["layer"] != layer:
                    continue
                if any(a["shape"].contains(b["shape"]) or b["shape"].contains(a["shape"])
                       for b in records[:i] if b["layer"] == layer):
                    questions.append(f"{layer}: nested unfilled outlines have ambiguous wall material")
    return records, unary_union([r["shape"] for r in records]), questions


def section(row, doc, records, material, matrix):
    axis = row.get("axis")
    if axis not in {"x", "y"}:
        raise ValueError("section axis must be x or y")
    at = number(row.get("at_mm"), "at_mm")
    low, high = pair(row.get("search_mm"), "search_mm")
    if low >= high:
        raise ValueError("search_mm must increase")
    expected = pair(row.get("expected_faces_mm"), "expected_faces_mm")
    if expected[0] > expected[1] or (row["kind"] == "wall_section" and expected[0] == expected[1]):
        raise ValueError("expected faces must be ordered; wall thickness must be positive")
    if expected[0] < low-EPS or expected[1] > high+EPS:
        raise ValueError("search interval must contain both independently expected faces")
    u = uncertainty(row)
    index = 0 if axis == "x" else 1
    result = {"axis": axis, "at_mm": at, "expected_faces_mm": expected,
              "image_uncertainty_mm": u, "handles": [], "pending": [], "failures": []}
    # Find complete material intervals, not intervals clipped to the search box.
    if row["kind"] == "wall_section":
        if material.is_empty:
            result["pending"].append("missing_wall_boundary")
            return result
        bounds = material.bounds
        start, end = (bounds[0], bounds[2]) if axis == "x" else (bounds[1], bounds[3])
        probe = interval_geometry(axis, at, min(start, low)-1, max(end, high)+1)
        candidates = []
        for line in parts(material.intersection(probe), "LineString"):
            values = [p[index] for p in line.coords]
            span = [min(values), max(values)]
            if min(span[1], high)-max(span[0], low) > EPS:
                candidates.append(span)
        if len(candidates) != 1:
            result["pending"].append("missing_wall_boundary" if not candidates else "multiple_wall_boundaries")
            result["candidates_mm"] = candidates
            return result
        actual = candidates[0]
        selected = interval_geometry(axis, at, *actual)
        contributions = []
        for record in records:
            length = record["shape"].intersection(selected).length
            if length > EPS:
                contributions.append(length)
                result["handles"] += record["handles"]
        overlap = max(0.0, sum(contributions)-(actual[1]-actual[0]))
        result.update(actual_faces_mm=actual, union_thickness_mm=actual[1]-actual[0],
                      summed_individual_thickness_mm=sum(contributions), overlap_mm=overlap)
        role = row.get("section_role", "body")
        if role not in {"body", "junction"}:
            raise ValueError("section_role must be body or junction")
        if overlap > EPS:
            if role == "junction":
                if not row.get("junction_evidence"):
                    result["pending"].append("junction_overlap_requires_source_evidence")
            elif row.get("allow_overlap") is True and row.get("overlap_evidence"):
                result["retained_overlap"] = row["overlap_evidence"]
            else:
                result["failures"].append("unexpected_overlapping_wall_material_at_body_section")
    else:
        layers = names(row.get("layers"), "line_section.layers")
        probe = interval_geometry(axis, at, low, high)
        values = []
        for ent in doc.modelspace():
            if ent.dxf.layer not in layers:
                continue
            try:
                for child in recursive_decompose([ent]):
                    if child.dxftype() not in {"LINE", "LWPOLYLINE", "POLYLINE", "ARC", "CIRCLE", "ELLIPSE", "SPLINE"}:
                        continue
                    points = [(v.x, v.y) for v in make_path(child).flattening(0.005)]
                    if len(points) < 2:
                        continue
                    line = affine_transform(LineString(points), matrix)
                    intersection = line.intersection(probe)
                    if parts(intersection, "LineString"):
                        result["pending"].append("probe_runs_along_boundary_choose_transverse_section")
                    points = parts(intersection, "Point")
                    if points:
                        values += [p.coords[0][index] for p in points]
                        result["handles"].append(ent.dxf.handle)
            except Exception as exc:
                result["pending"].append(f"unsupported_boundary/{ent.dxf.handle}/{exc}")
        if row.get("allow_wall_material", False) is not True and material.intersection(probe).length > EPS:
            result["failures"].append("wall_material_occupies_expected_frame_or_rail_section")
        values = sorted(values)
        unique = []
        for value in values:
            if not unique or abs(value-unique[-1]) > EPS:
                unique.append(value)
        result["actual_lines_mm"] = unique
        if not unique:
            result["pending"].append("missing_frame_or_rail_boundary")
            return result
        if expected[1]-expected[0] > EPS and len(unique) < 2:
            result["pending"].append("incomplete_frame_or_rail_band")
        actual = [unique[0], unique[-1]]
        result["actual_faces_mm"] = actual
    result["handles"] = list(dict.fromkeys(result["handles"]))
    result["face_error_mm"] = [a-b for a, b in zip(actual, expected)]
    if any(abs(a-b) > limit+EPS for a, b, limit in zip(actual, expected, u)):
        result["failures"].append("actual_boundary_outside_source_reading_range")
    if "expected_thickness_mm" in row:
        target = number(row["expected_thickness_mm"], "expected_thickness_mm", 0)
        limit = number(row.get("thickness_uncertainty_mm", sum(u)), "thickness_uncertainty_mm", 0)
        if abs(actual[1]-actual[0]-target) > limit+EPS:
            result["failures"].append("actual_union_thickness_outside_source_range")
    return result


def selected(ref, sections):
    if not isinstance(ref, dict) or ref.get("section") not in sections:
        return None, ["missing_section_reference"], []
    s = sections[ref["section"]]
    if "actual_faces_mm" not in s:
        return None, ["referenced_boundary_missing_or_ambiguous"], []
    choices = ref.get("position_candidates")
    if choices is None:
        choices = [ref.get("position")]
    if not isinstance(choices, list) or not choices or any(p not in {"min", "max", "center"} for p in choices):
        raise ValueError("boundary position must be min, max or center")
    lo, hi = s["actual_faces_mm"]
    values = [{"min": lo, "max": hi, "center": (lo+hi)/2}[p] for p in choices]
    if len(set(choices)) > 1:
        return None, ["source_attachment_has_multiple_candidates"], values
    pending = ["referenced_boundary_has_unresolved_checks"] if s.get("pending") else []
    return values[0], pending, values


def span_check(row, doc, sections, transform_point, material):
    a, pa, ca = selected(row.get("from"), sections)
    b, pb, cb = selected(row.get("to"), sections)
    result = {"failures": [], "pending": pa+pb, "from_candidates_mm": ca, "to_candidates_mm": cb}
    first = sections.get(row.get("from", {}).get("section"), {})
    second = sections.get(row.get("to", {}).get("section"), {})
    axis = row.get("axis", first.get("axis"))
    if axis not in {"x", "y"} or first.get("axis") != axis or second.get("axis") != axis:
        result["pending"].append("boundary_axes_do_not_match_measurement")
    reference = row.get("measurement_kind") == "reference_span"
    if row["kind"] == "span" and not reference:
        if abs(first.get("at_mm", math.inf)-second.get("at_mm", -math.inf)) > EPS:
            result["pending"].append("net_distance_requires_same_transverse_section")
    elif reference and not row.get("reference_reason"):
        result["pending"].append("reference_span_requires_explicit_non_net_distance_reason")
    result["measurement_kind"] = "reference_span_not_room_net" if reference else row.get("measurement_kind", "net_span" if row["kind"] == "span" else "marketing_control_span")
    target = number(row.get("expected_mm"), "expected_mm", 0)
    u = uncertainty(row, 1)[0]
    result.update(expected_mm=target, image_uncertainty_mm=u)
    if a is not None and b is not None:
        result["actual_endfaces_mm"] = [a, b]
        result["actual_span_mm"] = abs(b-a)
        result["source_error_mm"] = abs(b-a)-target
        if abs(result["source_error_mm"]) > u+EPS:
            result["failures"].append("actual_span_outside_source_reading_range")
        if row["kind"] == "span" and b <= a:
            result["failures"].append("net_span_has_reversed_or_overlapping_boundaries")
        if row["kind"] == "span" and not reference and axis in {"x", "y"} and first.get("at_mm") is not None:
            probe = interval_geometry(axis, first["at_mm"], min(a, b), max(a, b))
            occupied = material.intersection(probe).length
            result["wall_material_inside_claimed_net_span_mm"] = occupied
            if occupied > EPS:
                result["failures"].append("wall_material_blocks_claimed_net_span")
    if row["kind"] == "control":
        ent = doc.entitydb.get(row.get("dimension_handle", ""))
        if (not ent or not ent.is_alive or ent.dxf.owner != doc.modelspace().layout_key or
                ent.dxftype() != "DIMENSION" or ent.dimtype not in (0, 1)):
            result["failures"].append("missing_supported_native_dimension")
        elif axis in {"x", "y"}:
            i = 0 if axis == "x" else 1
            points = [transform_point(list(getattr(ent.dxf, key))[:2]) for key in ("defpoint2", "defpoint3")]
            projected = abs(points[1][i]-points[0][i])
            measured = number(float(ent.get_measurement()), "native dimension measurement")
            result.update(dimension_handle=ent.dxf.handle, dimension_definition_points_mm=points,
                          dimension_object_value_mm=measured, definition_projected_span_mm=projected,
                          dimension_text=ent.dxf.get("text", ""))
            tolerance = number(row.get("definition_tolerance_mm", EPS), "definition_tolerance_mm", 0)
            if tolerance > 0.1:
                raise ValueError("definition tolerance is numerical (<=0.1mm), not image uncertainty")
            if abs(measured-target) > tolerance or abs(projected-target) > tolerance:
                result["failures"].append("dimension_definition_or_direction_disagrees_with_source_value")
            if ent.dxf.get("text", "") not in ("", "<>"):
                result["failures"].append("dimension_has_text_override")
            if a is not None and b is not None:
                errors = min(([abs(points[0][i]-a), abs(points[1][i]-b)],
                              [abs(points[1][i]-a), abs(points[0][i]-b)]), key=max)
                result["definition_to_actual_boundary_error_mm"] = errors
                if max(errors) > tolerance:
                    result["failures"].append("dimension_definitions_do_not_land_on_selected_actual_controls")
    return result


def audit(doc, spec, *, dxf_sha256, source_sha256, dwg_sha256=None):
    """Compute actual geometry. Caller supplies hashes computed from actual input files."""
    report = {"review_version": VERSION, "status": "待核", "failed": [], "pending": [],
              "issues": [], "results": {}, "retained_inferences": [], "missing_boundaries": [],
              "hashes": {"readback_dxf_sha256": dxf_sha256, "source_sha256": source_sha256,
                         "dwg_sha256": dwg_sha256}, "geometry_roundoff_mm": EPS,
              "coverage": {"required": 0, "provided": 0, "processed": 0, "missing_ids": []},
              "scope": "declared independent source geometry checks; no engineering axis or site certification"}
    try:
        if not isinstance(spec, dict) or spec.get("review_version") != VERSION:
            raise ValueError("explicit source-geometry/1 schema required")
        required = names(spec.get("required_ids"), "required_ids")
        checks = spec.get("checks")
        if not isinstance(checks, list) or not checks:
            raise ValueError("checks must be a nonempty independent-source inventory")
        ids = names([c.get("id") if isinstance(c, dict) else None for c in checks], "check IDs")
        missing = sorted(set(required)-set(ids))
        extra = sorted(set(ids)-set(required))
        report["coverage"] = {"required": len(required), "provided": len(ids), "missing_ids": missing,
                              "undeclared_ids": extra, "processed": 0}
        for ident in missing:
            report["pending"].append(ident)
            report["issues"].append({"id": ident, "kind": "missing_coverage", "message": "required source check missing"})
        if extra:
            report["pending"].append("coverage/undeclared")
        for name, actual in (("source_sha256", source_sha256), ("dxf_sha256", dxf_sha256)):
            if not valid_hash(spec.get(name)) or not valid_hash(actual) or spec[name].lower() != actual.lower():
                report["pending"].append("identity/"+name)
                report["issues"].append({"id": "identity/"+name, "kind": "hash_mismatch_or_missing"})
        if dwg_sha256 is not None or "dwg_sha256" in spec:
            if not valid_hash(spec.get("dwg_sha256")) or spec.get("dwg_sha256") != dwg_sha256:
                report["pending"].append("identity/dwg_sha256")
        if doc.units != 4:
            raise ValueError("model units must be declared millimetres")
        layers = names(spec.get("wall_layers"), "wall_layers")
        matrix, transform_point = frame(spec)
        records, material, questions = wall_material(doc, layers, matrix)
        report["hatch_style_equivalences"] = [
            {"handle": r["handles"][0], "original_style": r["equivalent_hatch_style"],
             "basis": "one valid closed straight polyline boundary; no islands; original unchanged"}
            for r in records if r.get("equivalent_hatch_style") is not None]
        for q in questions:
            report["pending"].append("wall_decode/"+q)
            report["issues"].append({"id": "wall_decode", "kind": "pending", "message": q})
        sections = {}
        ordered = sorted(checks, key=lambda c: c.get("kind") not in {"wall_section", "line_section"})
        for row in ordered:
            ident = row["id"]
            try:
                if row.get("kind") not in KINDS:
                    raise ValueError("unsupported check kind")
                if not isinstance(row.get("source_evidence"), str) or not row["source_evidence"].strip():
                    raise ValueError("independent source_evidence required")
                if row["kind"] in {"wall_section", "line_section"}:
                    result = section(row, doc, records, material, matrix)
                    sections[ident] = result
                elif row["kind"] in {"span", "control"}:
                    result = span_check(row, doc, sections, transform_point, material)
                else:
                    pts = row.get("polygon_mm")
                    if not isinstance(pts, list) or len(pts) < 3:
                        raise ValueError("clear_region requires a source polygon")
                    region = Polygon([pair(p, "polygon_mm point") for p in pts])
                    if not region.is_valid or region.is_empty or region.area <= EPS*EPS:
                        raise ValueError("invalid clear polygon")
                    occupied = region.intersection(material).area
                    result = {"wall_material_mm2": occupied, "source_region_mm2": region.area,
                              "failures": ["wall_material_blocks_source_open_region"] if occupied > EPS*EPS else [], "pending": []}
                    result["handles"] = list(dict.fromkeys(h for r in records if r["shape"].intersection(region).area > EPS*EPS for h in r["handles"]))
                result.update(id=ident, kind=row["kind"], source_evidence=row["source_evidence"])
                state = "需返修" if result["failures"] else ("待核" if result["pending"] else "通过")
                result["status"] = state
                report["results"][ident] = result
                report["coverage"]["processed"] += 1
                if result["failures"]:
                    report["failed"].append(ident)
                if result["pending"]:
                    report["pending"].append(ident)
                    if any("missing" in v for v in result["pending"]):
                        report["missing_boundaries"].append(ident)
                    if isinstance(row.get("inference_reason"), str) and row["inference_reason"].strip():
                        report["retained_inferences"].append({"id": ident, "reason": row["inference_reason"],
                                                               "candidates": {k: v for k, v in result.items() if "candidates" in k}})
                report["issues"] += [{"id": ident, "kind": "repair", "message": m} for m in result["failures"]]
                report["issues"] += [{"id": ident, "kind": "pending", "message": m} for m in result["pending"]]
            except Exception as exc:
                report["pending"].append(ident)
                report["results"][ident] = {"id": ident, "status": "待核", "error": str(exc)}
                report["issues"].append({"id": ident, "kind": "invalid_expectation", "message": str(exc)})
    except Exception as exc:
        report["pending"].append("schema_or_input")
        report["issues"].append({"id": "schema_or_input", "kind": "invalid_input", "message": str(exc)})
    report["failed"] = sorted(set(report["failed"]))
    report["pending"] = sorted(set(report["pending"]))
    report["coverage"].update(
        passed=sum(r.get("status") == "通过" for r in report["results"].values()),
        failed=sum(r.get("status") == "需返修" for r in report["results"].values()),
        pending=sum(r.get("status") == "待核" for r in report["results"].values()))
    report["status"] = "需返修" if report["failed"] else ("待核" if report["pending"] else "通过")
    return report


def declared_spec(ledger, assessment=None):
    """None means legacy, {} means strict review was requested but omitted."""
    assessment = assessment or {}
    for record in (assessment, ledger):
        if "source_geometry_review" in record:
            return record["source_geometry_review"] if isinstance(record["source_geometry_review"], dict) else {}
    if any("source_geometry_review_required" in r and r["source_geometry_review_required"] is not False
           for r in (assessment, ledger)):
        return {}
    if any(r.get("review_version") == VERSION for r in (assessment, ledger)):
        return {}
    return None


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("dxf", type=Path)
    p.add_argument("spec", type=Path)
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--report", type=Path, required=True)
    p.add_argument("--dwg", type=Path)
    a = p.parse_args()
    try:
        spec = json.loads(a.spec.read_text(encoding="utf-8"))
        result = audit(ezdxf.readfile(a.dxf), spec, dxf_sha256=digest(a.dxf), source_sha256=digest(a.source),
                       dwg_sha256=digest(a.dwg) if a.dwg else None)
    except Exception as exc:
        result = {"review_version": VERSION, "status": "待核", "failed": [], "pending": ["input"],
                  "coverage": {}, "issues": [{"id": "input", "message": str(exc)}], "results": {}}
    a.report.parent.mkdir(parents=True, exist_ok=True)
    a.report.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({"status": result["status"], "failed": result["failed"], "pending": result["pending"], "report": str(a.report)}, ensure_ascii=False))
    return {"通过": 0, "需返修": 1, "待核": 2}[result["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
