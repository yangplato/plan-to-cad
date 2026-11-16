#!/usr/bin/env python3
"""Read printed wall colours from original pixels and prepare a rebuildable copy of the draft."""
import argparse
from collections import defaultdict
from copy import deepcopy
from hashlib import sha256
import json
import math
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
from PIL import Image, ImageOps
from shapely.geometry import box
from shapely.ops import unary_union

from presentation import roles as plan_layers
from build_wall_runs import _opening_cuts, _wall_footprints
from check_wall_probes import hatch_area
from check_wall_tone_stations import digest, merged, registration, source_axes, stable_runs
from dimension_grid import GridProjection, prepare_plan
from draw_plan_parts import add_wall_material
from fit_wall_guides import calibration, write_json
from scan_wall_cores import runs

ROLES = ("bearing", "partition")
EPS = 1e-7  # arithmetic tolerance in station comparisons, not a source-reading tolerance
AREA_EPS = 1e-6  # mm², numerical roundoff only


def base_hash(scene):
    # Later room, door-leaf or furniture observations do not alter the solid wall domains.
    base = {k: scene[k] for k in ("source_to_mm", "source_sha256", "confirmed_structural_wall_ids") if k in scene}
    if "grid_registration" in scene:
        base["grid_axes"] = scene["grid_registration"]["axes"]
    if "dimension_grid" in scene:
        base["dimension_grid"] = scene["dimension_grid"]
        base["dimension_chains"] = scene.get("dimension_chains", [])
    wall_keys = ("id", "a", "b", "width_mm", "cap_a_mm", "cap_b_mm", "material", "material_locked", "structural_status")
    base["walls"] = [{k: wall[k] for k in wall_keys if k in wall} for wall in scene["walls"]]
    base["openings"] = [{k: op[k] for k in ("id", "a", "b", "hosts")} for op in scene.get("openings", [])]
    layers = plan_layers(scene)
    base["wall_layers"] = {r: layers[r] for r in ROLES}
    return sha256(json.dumps(base, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def source_geometry(scene):
    """Original logical footprints including their actual end caps; convert mm back to source stations."""
    sx, _, _, sy, tx, ty = scene["source_to_mm"]
    def millimetres(row):
        return {**row, **{k: [sx*row[k][0]+tx, sy*row[k][1]+ty] for k in ("a", "b")}}
    walls = [millimetres(w) for w in scene["walls"]]
    footprints, axes = _wall_footprints(walls)
    cuts = _opening_cuts([millimetres(o) for o in scene.get("openings", []) if o["hosts"]], axes)
    return footprints, axes, cuts


def station_span(shape, ti, projection):
    left, bottom, right, top = shape.bounds
    return sorted(projection.source(p)[ti] for p in ((left,bottom), (right,top)))


def solid_domains(ident, ti, footprints, cuts, projection):
    shape = footprints[ident][1]
    start, end = station_span(shape, ti, projection)
    protected = []
    for opening, (hosts, cutter) in cuts.items():
        if ident in hosts:
            protected.append({"span_px": station_span(shape.intersection(cutter), ti, projection),
                              "reason": "native_opening", "id": opening})
    for other, (_, footprint) in footprints.items():
        if other == ident:
            continue
        intersection = shape.intersection(footprint)
        if intersection.area > AREA_EPS:
            protected.append({"span_px": station_span(intersection, ti, projection),
                              "reason": "shared_wall_footprint", "id": other})
    cursor, domains = start, []
    for a, b in merged(p["span_px"] for p in protected):
        if a > cursor + EPS:
            domains.append([cursor, a])
        cursor = max(cursor, b)
    if end > cursor + EPS:
        domains.append([cursor, end])
    return [start, end], domains, protected


def read_axes(image, guides, image_hash):
    if any("observations" in a for a in guides["axes"]):
        raise ValueError("Use original-colour swatch guides. A previous station report cannot authorize edits.")
    axes, samples = source_axes(image, guides, image_hash)
    colours = {s["material"]: (np.asarray(s["rgb"], dtype=float), s["tolerance_rgb"]) for s in samples}
    pixels = np.asarray(image)
    readings = []
    for axis in axes:
        ti = int(axis["orientation"] == "V")
        pieces, widths = stable_runs(axis, pixels.transpose(1, 0, 2) if ti else pixels, colours)
        readings.append((axis, ti, pieces, widths))
    return readings, samples


def fit_to_wall(reading, domain, native_width, normal_band, along_band):
    """Clip measured colour strips. Both ends must be observed changes or physical boundaries."""
    axis, _, pieces, widths = reading
    rows = []
    for p in pieces:
        first, last = p["stations_px"]
        if p["span_px"][1] <= domain[0] or p["span_px"][0] >= domain[1]:
            continue
        labels = [p["label"]]*(last-first+1)
        if p["label"] in ROLES:
            # Confirm each cross-section separately; a long correct part must not hide a local width mismatch.
            allowed = max(1., min(native_width*.3, 2*normal_band))
            for i, w in enumerate(widths[first-axis["start"]:last-axis["start"]+1]):
                if abs(w-native_width) > allowed:
                    labels[i] = "core_width_disagrees"
        for i, j, label in runs(labels):
            a, b = max(domain[0], first+i-.5), min(domain[1], first+j+.5)
            if b > a+EPS:
                if rows and rows[-1]["label"] == label and abs(a-rows[-1]["span_px"][1]) < EPS:
                    rows[-1]["span_px"][1] = b
                else:
                    rows.append({"span_px": [a, b], "label": label})
    # Record absent source coverage, including an entirely unobserved end cap.
    for pos, edge in ((0, domain[0]), (-1, domain[1])):
        if not rows:
            return [{"span_px": domain, "label": "outside_guide", "status": "review"}]
        limit = rows[pos]["span_px"][0 if pos == 0 else 1]
        if abs(edge-limit) > EPS:
            gap = {"span_px": sorted((edge, limit)), "label": "outside_guide"}
            if pos == 0:
                rows.insert(0, gap)
            else:
                rows.append(gap)
    minimum = max(4, 1.5*native_width, 2*along_band+1)
    def supported(row):
        a, b = row["span_px"]
        return row["label"] in ROLES and b-a+EPS >= minimum
    for i, row in enumerate(rows):
        if not supported(row):
            row["status"] = "review"
            if row["label"] in ROLES:
                row["reason"] = "insufficient_solid_shoulder"
            continue
        left = i == 0 or supported(rows[i-1])
        right = i+1 == len(rows) or supported(rows[i+1])
        row["status"] = "measured" if left and right else "review"
        if not (left and right):
            row["reason"] = "change_boundary_not_observed"
    return rows


def locked(scene, wall):
    return (wall["id"] in scene.get("confirmed_structural_wall_ids", []) or wall.get("material_locked")
            or wall.get("structural_status") == "confirmed")


def measure(image, guides, reg, scene, image_hash, along, normal):
    if not all(math.isfinite(v) and v >= 0 for v in (along, normal)):
        raise ValueError("The original-image reading tolerances must be finite, nonnegative pixel values.")
    scene, _ = prepare_plan(scene, [])
    def ticks_by_axis(mapping):
        return {name: [tuple(tick) for tick in ticks] for name,ticks in
                mapping.get("grid_registration",{}).get("axes",{}).items()}
    if ticks_by_axis(reg) != ticks_by_axis(scene):
        raise ValueError("Draft and measurement registration must use the same original-image grid axes.")
    matrix = calibration(scene)
    if not matrix or matrix != reg["source_to_mm"] or guides.get("source_to_mm", matrix) != matrix:
        raise ValueError("Draft, original guides and registration must use the same rectified source_to_mm.")
    if image_hash not in (scene.get("source_sha256"), reg.get("source_sha256")):
        raise ValueError("The draft or registration must identify the original image by its SHA-256.")
    if scene.get("source_sha256", image_hash) != image_hash:
        raise ValueError("The draft belongs to another original image.")
    projection, errors, residuals = registration(reg, image_hash)
    readings, samples = read_axes(image, guides, image_hash)
    footprints, axes, cuts = source_geometry(scene)
    if any(w["material"] not in ROLES for w in scene["walls"]):
        raise ValueError("Expected bearing/partition printed roles in the logical wall table.")
    scope = guides.get("printed_tone_scope", {})
    ids = scope.get("provisional_wall_ids", [])
    if (not isinstance(ids, list) or len(set(ids)) != len(ids) or set(ids)-set(axes)
            or (ids and not (isinstance(scope.get("basis"), str) and scope["basis"].strip()))):
        raise ValueError("List the authorized provisional wall IDs once and record the basis of authorization.")
    proposals, edits = [], []
    for wall in scene["walls"]:
        ident = wall["id"]
        ti, center_mm, first_mm, _, width_mm = axes[ident]
        def raw_normal(mm):
            point = [first_mm, mm] if ti == 0 else [mm, first_mm]
            return projection.source(point)[1-ti]
        n = raw_normal(center_mm)
        width = abs(raw_normal(center_mm + width_mm/2) - raw_normal(center_mm - width_mm/2))
        along_band, normal_band = along+errors[ti], normal+errors[1-ti]
        extent, domains, protected = solid_domains(ident, ti, footprints, cuts, projection)
        options = [r for r in readings if r[1] == ti
                   and abs(r[0]["normal"]-n) <= min(normal_band, width*.25)+EPS
                   and r[0]["start"]-.5 < extent[1] and r[0]["end"]+.5 > extent[0]]
        is_locked = bool(locked(scene, wall))
        authorized = ident in ids and not is_locked
        report = {"wall": ident, "base_material": wall["material"], "extent_px": extent,
                  "protected": protected, "permission": "confirmed_or_locked" if is_locked else
                  "provisional" if authorized else "suggest_only", "intervals": []}
        proposals.append(report)
        if not domains:
            report["status"] = "no_unique_solid_wall"
            continue
        if len(options) != 1 or normal_band >= width/2:
            report.update(status="review", reason="guide_not_unique_or_alignment_uncertain",
                          candidate_axes=[r[0]["id"] for r in options])
            continue
        reading = options[0]
        report.update(axis=reading[0]["id"], effective_along_band_px=along_band,
                      effective_normal_band_px=normal_band)
        for domain in domains:
            for row in fit_to_wall(reading, domain, width, normal_band, along_band):
                if row["status"] == "measured":
                    if row["label"] == wall["material"]:
                        row["status"] = "matches_draft"
                    elif authorized:
                        row["status"] = "applied_to_copy"
                        edits.append({"wall": ident, "axis": report["axis"], "span_px": row["span_px"],
                                      "material": row["label"]})
                    else:
                        row["status"] = "suggest_only"
                report["intervals"].append(row)
        report["status"] = "read"
    evidence = [{"id": a["id"], "orientation": a["orientation"], "normal_px": a["normal"], "runs": p}
                for a, _, p, _ in readings]
    result = {"source_sha256": image_hash, "source_samples": samples, "source_axes": evidence,
              "registration_residuals_mm": residuals, "scope": scope, "walls": proposals, "applied_spans": edits}
    return result


def apply_to_new_drawing(doc, ledger, scene):
    """Recolour only fresh native wall faces. Keep the original logical host and every net opening."""
    schedule = scene["printed_wall_tones"]
    if (schedule["base_sha256"] != base_hash(scene) or
            scene.get("source_sha256", schedule["source_sha256"]) != schedule["source_sha256"]):
        raise ValueError("The walls, openings, layers or registration changed. Re-read the original colours.")
    by_id = {wall["id"]: wall for wall in scene["walls"]}
    # Stored span_px uses the linear working frame consumed by the wall writer.
    matrix = scene["source_to_mm"]
    working_projection = GridProjection({"source_to_mm":matrix})
    roles = plan_layers(scene)
    names = {role: roles[role] for role in ROLES}
    if len({n.casefold() for n in roles.values()}) != len(roles):
        raise ValueError("Wall roles need their own distinct native layers.")
    footprints, axes = _wall_footprints(ledger["walls"])
    cuts = _opening_cuts([o for o in ledger["openings"] if o["hosts"]], axes)
    msp = doc.modelspace()
    original = [e for e in msp if e.dxf.layer in names.values()]
    if any(e.dxftype() not in ("HATCH", "LWPOLYLINE") for e in original):
        raise ValueError("Apply colour runs only to a fresh structure from the common writer.")
    material = {r: unary_union([hatch_area(e) for e in original
                               if e.dxftype() == "HATCH" and e.dxf.layer == name]) for r, name in names.items()}
    extracts = defaultdict(list)
    seen = defaultdict(list)
    for row in schedule["spans"]:
        ident, role = row["wall"], row["material"]
        wall = by_id[ident]
        if (ident not in schedule["provisional_wall_ids"] or locked(scene, wall)
                or role not in ROLES or role == wall["material"]):
            raise ValueError(f"{ident}: invalid or locked printed material change.")
        ti, _, _, _, _ = axes[ident]
        a, b = row["span_px"]
        if not (math.isfinite(a) and math.isfinite(b) and a < b):
            raise ValueError(f"{ident}: invalid colour stations.")
        span, domains, _ = solid_domains(ident, ti, footprints, cuts, working_projection)
        if not any(lo-EPS <= a and b <= hi+EPS for lo, hi in domains):
            raise ValueError(f"{ident}: printed colour would cross a net opening, junction or wall end {span}.")
        if any(min(b, old_b)-max(a, old_a) > EPS for old_a, old_b in seen[ident]):
            raise ValueError(f"{ident}: overlapping printed colour spans.")
        seen[ident].append((a, b))
        shape = footprints[ident][1]
        low, high = sorted(matrix[3*ti]*t+matrix[4+ti] for t in (a, b))
        bounds = list(shape.bounds)
        bounds[ti], bounds[ti+2] = low, high
        mask = shape.intersection(box(*bounds))
        if mask.is_empty or mask.difference(material[wall["material"]]).area > AREA_EPS:
            raise ValueError(f"{ident}: colour override is outside the actual original wall material.")
        extracts[role].append(mask)
    if not seen:
        return
    dark, light = ROLES
    desired = {r: material[r].difference(unary_union(extracts[other])).union(unary_union(extracts[r]))
               for r, other in ((dark, light), (light, dark))}
    union = unary_union(list(material.values()))
    if (union.symmetric_difference(unary_union(list(desired.values()))).area > AREA_EPS
            or desired[dark].intersection(desired[light]).area > AREA_EPS):
        raise ValueError("Wall union or material separation changed during the colour-only rebuild.")
    faces = {r: (names[r], g) for r, g in desired.items() if not g.is_empty}
    add_wall_material(msp, faces, {}, {names[dark]: 66, names[light]: 178})
    for entity in original:
        msp.delete_entity(entity)
    ledger["wall_layers"] = {names[r]: "fills" for r in faces}
    ledger["printed_wall_tones"] = deepcopy(schedule)


def run(source, guide_path, registration_path, scene_path, out, along, normal):
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise ValueError("Use a new or empty output directory.")
    read = lambda p: json.loads(p.read_text(encoding="utf-8"))
    guides, reg, scene = map(read, (guide_path, registration_path, scene_path))
    if scene.get("printed_wall_tones"):
        raise ValueError("Use the original logical draft. Keep the previous colour schedule as a separate version.")
    with Image.open(source) as original:
        image = ImageOps.exif_transpose(original).convert("RGB")
    report = measure(image, guides, reg, scene, digest(source), along, normal)
    report["input_sha256"] = {k: digest(p) for k, p in
                              (("guides", guide_path), ("registration", registration_path), ("scene", scene_path))}
    work = deepcopy(scene)
    if report["applied_spans"]:
        changes = deepcopy(report["applied_spans"])
        if "grid_registration" in work:
            projection = GridProjection(work)
            axes = {a["id"]: int(a["orientation"]=="V") for a in report["source_axes"]}
            for change in changes:
                i = axes[change["axis"]]
                change["original_span_px"] = change["span_px"]
                change["span_px"] = [projection.working([0,t] if i else [t,0])[i] for t in change["original_span_px"]]
        work["printed_wall_tones"] = {"source_sha256": report["source_sha256"], "base_sha256": base_hash(scene),
                                      "provisional_wall_ids": report["scope"]["provisional_wall_ids"],
                                      "spans": changes}
    out.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=out.parent) as tmp:
        stage = Path(tmp)/"result"
        stage.mkdir()
        write_json(stage/"wall-tone-schedule.json", report)
        write_json(stage/"scene-working.json", work)
        stage.replace(out)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ("image", "guides", "registration", "scene", "out"):
        parser.add_argument(arg, type=Path)
    parser.add_argument("--read-band-px", type=float, required=True)
    parser.add_argument("--normal-band-px", type=float)
    args = parser.parse_args()
    try:
        report = run(args.image, args.guides, args.registration, args.scene, args.out, args.read_band_px,
                     args.read_band_px if args.normal_band_px is None else args.normal_band_px)
    except (KeyError, ValueError, TypeError, IndexError, OSError, np.linalg.LinAlgError) as exc:
        parser.exit(2, f"apply-wall-tones: {exc}\n")
    print(f'{len(report["applied_spans"])} provisional colour spans applied to {args.out/"scene-working.json"}')
