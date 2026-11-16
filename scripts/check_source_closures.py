#!/usr/bin/env python3
"""Compare source-pixel swing hypotheses with each reader, including agreed open passages."""
import argparse
import hashlib
import json
import math
import re
from collections import Counter
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps
from scipy.optimize import linear_sum_assignment

from compare_source_readers import compare
from door_patch_card import analyze, build_card, sample
from swing_evidence import evidence


KINDS = {"open", "swing", "sliding", "window", "rail"}
PRIVATE = re.compile(r"卧室|主卧|次卧|客卧|儿童房|卫生间|主卫|客卫|公卫|浴室|洗手间|厕所|衣帽间|\b(bed(room)?|bath(room)?|toilet|wc)\b", re.I)
OUTDOOR = re.compile(r"室外|户外|露台|庭院|天井|花园|\b(outside|exterior|outdoor|terrace|courtyard)\b", re.I)
BALCONY = re.compile(r"阳台|\bbalcony\b", re.I)


def load_inventory(path, digest, size):
    doc = json.loads(path.read_text(encoding="utf-8"))
    if doc.get("source_sha256") != digest:
        raise ValueError(f"{path}: source_sha256 does not match the original file")
    rows = doc.get("portals")
    if not isinstance(rows, list):
        raise ValueError(f"{path}: portals must be an array")
    ids = set()

    def point(p):
        return (isinstance(p, (list, tuple)) and len(p) == 2 and
                all(isinstance(v, (int, float)) and math.isfinite(v) and 0 <= v < s
                    for v, s in zip(p, size)))

    for row in rows:
        name = row["id"]
        if not isinstance(name, str) or not name or name in ids:
            raise ValueError(f"{path}: missing or duplicate portal id {name!r}")
        ids.add(name)
        if row["kind"] not in KINDS or not all(point(row[k]) for k in ("a", "b")) or row["a"] == row["b"]:
            raise ValueError(f"{path}: invalid type or opening endpoints at {name}")
        pair = row.get("between") or []
        if pair and (not isinstance(pair, list) or len(pair) != 2 or
                     not all(isinstance(label, str) and label.strip() for label in pair)):
            raise ValueError(f"{path}: between must have two room labels at {name}")
        for leaf in row.get("leaves", []):
            if not all(point(leaf[k]) for k in ("hinge", "tip")) or leaf["hinge"] == leaf["tip"]:
                raise ValueError(f"{path}: invalid opened leaf at {name}")
    return rows


def connection_cues(row, rooms, witness_ports, open_links):
    witness_pair = witness_ports.get(row["id"], [])
    declared = row.get("between") or []
    pairs = [pair for pair in (declared, witness_pair) if len(pair) == 2 and all(isinstance(r, str) for r in pair)]
    cues = set()
    if not pairs:
        cues.add("unregistered_room_pair")
    if len(pairs) == 2 and set(pairs[0]) != set(pairs[1]):
        cues.add("room_pair_disagreement")
    for pair in pairs:
        if any(rooms.get(r) == "closed" for r in pair):
            cues.add("registered_closed_room")
        if any(PRIVATE.search(r) for r in pair):
            cues.add("possible_privacy_boundary")
        exterior = [rooms.get(r) == "exterior" or bool(OUTDOOR.search(r)) for r in pair]
        if sum(exterior) == 1:
            cues.add("possible_envelope_boundary")
        if any(BALCONY.search(r) and rooms.get(r) != "exterior" for r in pair):
            cues.add("check_balcony_enclosure")
        if frozenset(pair) in open_links:
            cues.add("registered_open_link")
    return {"declared_pair": declared, "witness_pair": witness_pair, "cues": sorted(cues)}


def association(row, candidate, band):
    """Associate a pixel-detected trace with a physical jamb, never using the claimed leaf."""
    ab = np.array([row["a"], row["b"]], dtype=float)
    pivot = np.array(candidate["pivot_px"])
    closed = np.array(candidate["closed_vector"])
    distances = np.linalg.norm(ab - pivot, axis=1)
    i = int(np.argmin(distances))
    span = ab[1 - i] - ab[i]
    length = np.linalg.norm(span)
    tolerance = max(2.0, band)
    aligned = float(np.dot(closed, span) / length) >= math.cos(math.radians(15))
    at_jamb = distances[i] <= tolerance and aligned
    end_error = math.dist(candidate["closed_tip_px"], ab[1 - i])
    return dict(id=candidate["id"], jamb="AB"[i], near_error_px=round(float(distances[i]), 2),
                far_error_px=round(end_error, 2), near=bool(at_jamb), spans=bool(at_jamb and end_error <= tolerance))


def floor_crossings(source, row, band):
    """Check source RGB at five stations spanning both faces. Darkness has no special meaning."""
    a, b = np.asarray(row["a"]), np.asarray(row["b"])
    span = math.dist(a, b)
    tangent = (b-a)/span
    normal = np.array([-tangent[1], tangent[0]])
    depth = min(span*.32, max(5., span*.20, 1.5*band))
    stations = np.linspace(.18, .82, 5)
    across = np.linspace(-depth, depth, 2*math.ceil(depth)+1)
    centers = a + stations[:, None]*span*tangent
    pts = (centers[:, None, None, :] + across[None, :, None, None]*normal +
           np.array([-1., 0., 1.])[None, None, :, None]*tangent)
    lower = np.maximum(0, np.floor(pts.min(axis=(0, 1, 2))).astype(int)-1)
    upper = np.minimum(source.size, np.ceil(pts.max(axis=(0, 1, 2))).astype(int)+2)
    rgb = np.asarray(source.crop((*lower, *upper)).convert("RGB"), dtype=np.float32)
    values = np.stack([sample(rgb[:, :, k], pts-lower) for k in range(3)], axis=-1)
    observations = []
    for f, section in zip(stations, values):
        # Require the whole transect inside the actual source. Use the worst channel
        # including one-stroke excursions, so a fine frame cannot vanish in a gray average.
        spread = None
        if np.isfinite(section).all():
            spread = float(np.max(np.ptp(np.median(section, axis=1), axis=0)))
        observations.append(dict(at_fraction=round(float(f), 2),
                                 max_rgb_range=None if spread is None else round(spread, 2)))
    smooth = all(r["max_rgb_range"] is not None and r["max_rgb_range"] <= 10 for r in observations)
    return dict(continuous_tone_at_all_stations=smooth, half_depth_px=round(depth, 2),
                maximum_channel_range_allowed=10, samples=observations)


def compare_leaves(row, chosen, band):
    observed = row.get("leaves", [])
    if len(observed) != len(chosen):
        return [], ["leaf_count_conflict" if observed else "missing_observed_leaf"]
    cost = [[max(math.dist(leaf["hinge"], c["pivot_px"]), math.dist(leaf["tip"], c["open_tip_px"]))
             for c in chosen] for leaf in observed]
    indices = linear_sum_assignment(cost)
    residuals, reasons = [], set()
    for i, j in zip(*indices):
        leaf, c = observed[i], chosen[j]
        a, b = np.array(row["a"]), np.array(row["b"])
        d = b - a
        jamb = lambda p: int(np.dot(np.asarray(p) - (a + b)/2, d) > 0)
        same_hinge = jamb(leaf["hinge"]) == jamb(c["pivot_px"])
        opened = np.subtract(leaf["tip"], leaf["hinge"])
        cosine = float(np.dot(opened, c["open_vector"]) / np.linalg.norm(opened))
        h = math.dist(leaf["hinge"], c["pivot_px"])
        t = math.dist(leaf["tip"], c["open_tip_px"])
        if not same_hinge:
            reasons.add("opposite_hinge_evidence")
        if cosine < -0.7:
            reasons.add("opposite_open_side_evidence")
        elif cosine < 0.7:
            reasons.add("open_axis_disagreement")
        if same_hinge and cosine >= 0.7 and max(h, t) > max(2., band):
            reasons.add("leaf_positions_outside_reading_band")
        residuals.append(dict(reported_leaf=int(i + 1), candidate=c["id"], hinge_error_px=round(h, 2),
                              open_tip_error_px=round(t, 2), same_jamb=same_hinge, direction_cosine=round(cosine, 3)))
    return residuals, sorted(reasons)


def judge(row, report, fixed, band, connection, floor):
    clean = [c for c in report["candidates"] if not c["pixel_cautions"]]
    links = {c["id"]: association(row, c, band) for c in clean}
    by_id = {c["id"]: c for c in clean}
    full = [[c] for c in clean if links[c["id"]]["spans"]]
    for pair in report["complementary_pairs"]:
        first, second = [links[i] for i in pair["candidates"]]
        if first["near"] and second["near"] and first["jamb"] != second["jamb"]:
            full.append([by_id[i] for i in pair["candidates"]])
    notes, residuals = [], []
    kind = row["kind"]
    if kind == "open":
        notes.extend(connection["cues"])
    if math.dist(row["a"], row["b"]) <= max(6., 4*band):
        decision = "review"
        notes.append("jambs_not_separable_at_source_resolution")
    elif report["status"] == "same_arc_two_possible_axes" or len(full) > 1:
        decision = "review"
        notes.append("competing_pixel_hypotheses")
    elif len(full) == 1:
        chosen = full[0]
        aids = {c["id"] for c in chosen}
        if any(i not in aids and s["near"] for i, s in links.items()):
            decision = "review"
            notes.append("additional_trace_at_a_jamb")
        else:
            if kind != "swing":
                decision = "conflict"
                notes.append("visible_swing_conflicts_with_declared_type")
            else:
                residuals, reasons = compare_leaves(row, chosen, band)
                notes.extend(reasons)
                if not reasons:
                    decision = "supported"
                elif set(reasons) <= {"missing_observed_leaf", "leaf_positions_outside_reading_band"}:
                    decision = "review"
                else:
                    decision = "conflict"
            if len(chosen) == 2:
                notes.append("two_complementary_leaves")
            elif fixed["status"] == "single_supported_tendency":
                c = chosen[0]
                endpoint_choice = fixed["candidates"][fixed["supported_ids"][0] - 1]
                if (endpoint_choice["hinge"] != links[c["id"]]["jamb"] or
                    np.dot(endpoint_choice["unit_open_vector_xy"], c["open_vector"]) < .7):
                    decision = "review"
                    notes.append("fixed_jamb_and_free_trace_disagree")
    elif clean:
        decision = "review"
        notes.append("complete_trace_nearby_but_span_or_axis_unconfirmed")
    elif fixed["supported_ids"]:
        decision = "review"
        notes.append("endpoint_template_lacks_independent_curvature_confirmation")
    elif kind in {"window", "rail", "sliding"}:
        decision = "other_family"
        notes.append("verify_frame_tracks_panes_or_guard_in_source")
    elif (kind == "open" and set(connection["cues"]) == {"registered_open_link"} and
          floor["continuous_tone_at_all_stations"] and not report["candidates"]):
        decision = "compatible_open"
        notes.append("registered_open_link_with_continuous_source_floor")
    else:
        decision = "review"
        notes.append("no_decisive_arc_and_leaf")
        if kind == "open":
            notes.append("inspect_positive_evidence_for_open_passage")
    return dict(id=row["id"], claimed_kind=kind, claimed_a_px=row["a"], claimed_b_px=row["b"],
                claimed_leaves=row.get("leaves", []), decision=decision, reasons=notes,
                connection=connection, floor_crossings=floor,
                pixel_cautions=sorted({r for c in report["candidates"][:4] for r in c["pixel_cautions"]}),
                complete_trace_associations=list(links.values()),
                spanning_hypotheses=[[c["id"] for c in cc] for cc in full], compared_leaves=residuals)


def audit_portal(source, record, band, scale, stroke, room_data):
    rows = [r for r in (record["first"], record["second"]) if r]
    ends = np.asarray([p for row in rows for p in (row["a"], row["b"])])
    spans = [math.dist(row["a"], row["b"]) for row in rows]
    reach = max(3, math.ceil(band * 1.5))
    lo = np.maximum(0, np.floor(ends.min(0)) - reach).astype(int)
    hi = np.minimum(source.size, np.ceil(ends.max(0)) + reach + 1).astype(int)
    box = [*lo.tolist(), *hi.tolist()]
    # Include smaller companion leaves regardless of what either reader called the opening.
    min_r = max(8.5*scale, min(260., min(spans)*scale*.27))
    max_r = max(min_r, min(1550., (max(spans)+2*band)*scale*1.12))
    opening_hints = [r["a"]+r["b"] for r in rows]
    report = analyze(source, box, scale, (min_r, max_r), 40., stroke, openings=opening_hints)
    fixed_cache, assessments = {}, {}
    for label in ("first", "second"):
        row = record[label]
        if row is None:
            continue
        key = tuple(row["a"] + row["b"])
        if key not in fixed_cache:
            fixed_cache[key] = evidence(source, row["a"], row["b"], [1.], stroke)[0][0]
        fixed = fixed_cache[key]
        floor = floor_crossings(source, row, band) if row["kind"] == "open" else None
        result = judge(row, report, fixed, band, connection_cues(row, *room_data), floor)
        result["fixed_endpoint_hypotheses"] = fixed
        assessments[label] = result
    decisions = {r["decision"] for r in assessments.values()}
    issues = list(record["issues"])
    if len(rows) == 2:
        p, q = [r.get("between") or [] for r in rows]
        if p and q and set(p) != set(q):
            issues.append("room_pair")
    if "conflict" in decisions:
        status = "conflict"
    elif issues or "review" in decisions:
        status = "review"
    else:
        status = next(iter(decisions))
    return report, dict(status=status, reader_issues=issues, readers=assessments,
                        pixel_status=report["status"], complete_candidates=report["candidates_with_complete_trace"])


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    for name in ("image", "first", "second", "out"):
        cli.add_argument(name, type=Path)
    cli.add_argument("--band-px", type=float, required=True)
    cli.add_argument("--mm-per-px", type=float, required=True)
    cli.add_argument("--stroke-px", type=float, default=1.6)
    cli.add_argument("--room-witness", type=Path, help="Optional source-only rooms/modes, open_links and portal room pairs.")
    args = cli.parse_args()
    if not all(math.isfinite(v) and v > 0 for v in (args.band_px, args.mm_per_px, args.stroke_px)):
        cli.error("band, scale and stroke must be finite and positive")
    if args.out.exists() and (not args.out.is_dir() or any(args.out.iterdir())):
        cli.error("choose a new or empty output directory")
    digest = hashlib.sha256(args.image.read_bytes()).hexdigest()
    with Image.open(args.image) as f:
        rgba = ImageOps.exif_transpose(f).convert("RGBA")
        source = Image.new("RGB", rgba.size, "white")
        source.paste(rgba, mask=rgba.getchannel("A"))
    docs = [load_inventory(path, digest, source.size) for path in (args.first, args.second)]
    if not any(docs):
        cli.error("both inventories contain zero portal locations; no source area to inspect")
    matched = compare(*docs, args.band_px)
    space = json.loads(args.room_witness.read_text(encoding="utf-8")) if args.room_witness else {}
    if "source_sha256" in space and space["source_sha256"] != digest:
        cli.error("room witness identifies a different source")
    room_data = ({r["id"]: r.get("mode") for r in space.get("rooms", [])},
                 {p["id"]: p.get("rooms", []) for p in space.get("portals", [])},
                 {frozenset(pair) for pair in space.get("open_links", [])})
    args.out.mkdir(exist_ok=True, parents=True)
    records = []
    for i, record in enumerate(matched, 1):
        report, item = audit_portal(source, record, args.band_px, args.mm_per_px, args.stroke_px, room_data)
        stem = f"{i:03d}"
        (args.out / (stem + ".json")).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        build_card(source, report).save(args.out / (stem + ".png"))
        records.append(dict(index=i, panel=stem+".png", traces=stem+".json", **item))
    counts = dict(Counter(r["status"] for r in records))
    summary = dict(source_sha256=digest, displayed_size_px=list(source.size), band_px=args.band_px,
                   mm_per_px=args.mm_per_px, counts=counts, locations=records)
    (args.out/"source-closures.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{len(records)} locations; {counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
