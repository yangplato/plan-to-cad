#!/usr/bin/env python3
"""Fit editable wall segments along author-marked orthogonal guides in the original image."""
import argparse
import hashlib
import json
import math
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
from PIL import Image, ImageDraw, ImageOps

from scan_wall_cores import classify, runs, scan_axis


ROLES = {"bearing", "partition"}
COLOURS = {"bearing": "#dc3159", "partition": "#238ac6", "no_sampled_core": "#dddddd",
           "open_edge": "#238156", "opening": "#27976d", "short_core": "#e8a22b",
           "junction": "#9665b4", "review": "#b9803b", "excluded": "#656565", "conflict": "#cc22a6"}


def point(axis, t, n):
    return [t, n] if axis["orientation"] == "H" else [n, t]


def pair(value, what, integers=False):
    if (not isinstance(value, list) or len(value) != 2 or
            not all(type(v) in (int, float) and math.isfinite(v) for v in value)
            or (integers and not all(type(v) is int for v in value))):
        raise ValueError(f"{what}: expected a finite coordinate pair")
    return value


def write_json(path, content):
    def native_scalar(value):
        if isinstance(value, (np.integer, np.floating, np.bool_)):
            return value.item()
        raise TypeError(f"Unsupported report value: {type(value).__name__}")
    path.write_text(json.dumps(content, ensure_ascii=False, indent=2, allow_nan=False, default=native_scalar) + "\n", encoding="utf-8")


def prepare(image, spec):
    scope = spec.get("plan_box_px", [0, 0, image.width-1, image.height-1])
    x, y, r, b = scope
    if not all(type(p) is int for p in scope) or not (0 <= x < r < image.width and 0 <= y < b < image.height):
        raise ValueError("plan_box_px must be an inclusive pixel box inside the oriented image")
    names, codes, samples = classify(image, scope, spec["samples"])
    if set(names) != ROLES:
        raise ValueError("Sample bearing and partition separately, using this plan's legend or stated assumption")
    for l, t, right, bot in spec.get("exclude_boxes_px", []):
        if not (x <= l <= right <= r and y <= t <= bot <= b):
            raise ValueError("exclude_boxes_px must lie inside the plan box")
        codes[t-y:bot-y+1, l-x:right-x+1] = -2
    negatives = spec.get("nonwall_points_px", [])
    for p in negatives:
        px, py = pair(p, "nonwall_points_px", True)
        if not (x <= px <= r and y <= py <= b):
            raise ValueError("nonwall point outside the plan box")
    guides = [{**spec.get("defaults", {}), **item} for item in spec["axes"]]
    by_id = {g["id"]: g for g in guides}
    if not guides or len(by_id) != len(guides) or any(not isinstance(i, str) or not i for i in by_id):
        raise ValueError("Supply unique, nonempty axis IDs")
    for g in guides:
        a = pair(g["a_px"], g["id"], True)
        bpt = pair(g["b_px"], g["id"], True)
        if (a[0] == bpt[0]) == (a[1] == bpt[1]):
            raise ValueError(f"{g['id']}: mark a nonzero horizontal or vertical guide")
        g["ti"] = int(a[0] == bpt[0])
    for joint in spec.get("axis_intersections", []):
        a, bpt = (by_id[i] for i in joint["axes"])
        p = pair(joint["point_px"], "axis intersection")
        if a["ti"] == bpt["ti"]:
            raise ValueError("Supply perpendicular guides for each marked axis intersection")
        for g in (a, bpt):
            ti = g["ti"]
            lo, hi = sorted([g["a_px"][ti], g["b_px"][ti]])
            n = p[1-ti]
            if not lo-0.5 <= p[ti] <= hi+0.5 or abs(n-g["a_px"][1-ti]) > g["search_px"]:
                raise ValueError(f"{g['id']}: intersection outside this guide/search range")
            if "normal" in g and g["normal"] != n:
                raise ValueError(f"{g['id']}: inconsistent marked perpendicular intersections")
            g["normal"] = n
    return scope, names, codes, samples, guides, negatives


def observe(g, scope, codes, names, negatives):
    # Keep even a one-pixel hit in this pass, then distinguish noise, short piers and crossings below.
    raw = scan_axis({**g, "min_run_px": 1}, codes, scope, names, negatives)
    ti = g["ti"]
    start, end = sorted((g["a_px"][ti], g["b_px"][ti]))
    jitter = float(g.get("jitter_px", max(0.5, g["core_width_px"][0]/4)))
    normal = g.get("normal", raw["core_normal_px"])
    if "normal" in g and (abs(normal-raw["core_normal_px"]) > jitter or raw["competing_axes"]):
        raise ValueError(f"{g['id']}: marked crossing and fitted core disagree; inspect the strip or use anchor_px")
    axis = dict(id=g["id"], orientation=raw["orientation"], start=start, end=end,
                normal=normal, fitted_normal=raw["core_normal_px"], competing_axes=raw["competing_axes"])
    stripes = codes.T if ti == 0 else codes
    stripes = stripes[start-scope[ti]:end-scope[ti]+1]
    origin = scope[1-ti]
    # Check the fitted core and its immediate centreline neighbourhood, independently of floor colour.
    radius = max(0.5, g["core_width_px"][0]/2)
    centre = stripes[:, max(0, math.ceil(normal-origin-radius)):math.floor(normal-origin+radius)+1]
    negative_stations = {p[ti] for p in negatives if abs(p[1-ti]-normal) <= g["core_width_px"][1]/2}
    marks = []
    for seg in raw["segments"]:
        lo, hi = int(seg["a_px"][ti])-start, int(seg["b_px"][ti])-start
        for i in range(lo, hi+1):
            code = seg["material"]
            width = seg["core_width_px"]
            if seg["status"] == "proposal":
                # Recover local width so a narrower same-tone pier does not inherit a long wall's thickness.
                section = stripes[i]
                seed = min(max(round(seg["a_px"][1-ti])-origin, 0), len(section)-1)
                if section[seed] != names.index(code):
                    hits = np.flatnonzero(section == names.index(code))
                    if len(hits):
                        seed = int(hits[np.argmin(abs(hits-(normal-origin)))])
                l = r = seed
                while l > 0 and section[l-1] == section[seed]:
                    l -= 1
                while r+1 < len(section) and section[r+1] == section[seed]:
                    r += 1
                width = r-l+1
                if (section[seed] != names.index(code) or not g["core_width_px"][0] <= width <= g["core_width_px"][1]
                        or abs((l+r)/2+origin-raw["core_normal_px"]) > jitter):
                    code = None
                status = "core" if code else "review"
            elif seg["status"] == "conflict":
                code, status = None, "conflict"
            elif raw["competing_axes"]:
                code, status = None, "review"
            elif np.any(centre[i] == -2):
                status = "excluded"
            elif np.any(centre[i] >= 0):
                status = "review"
            else:
                status = "no_sampled_core"
            if i+start in negative_stations and status in ("review", "junction"):
                code, status = None, "conflict"
            marks.append((status, code, width))
    return axis, marks, centre


def crossing_ranges(axis, all_axes, observations):
    for other in all_axes:
        if axis["orientation"] == other["orientation"]:
            continue
        met = axis["normal"] - other["start"]
        station = other["normal"]
        if not (-0.5 <= met <= other["end"]-other["start"]+0.5 and
                axis["start"]-0.5 <= station <= axis["end"]+0.5):
            continue
        # A drawn guide without perpendicular material nearby is insufficient evidence of a crossing.
        nearby = []
        for lo, hi, item in runs(observations[other["id"]]):
            status, role, width = item
            if status == "core" and hi-lo+1 >= max(3, width):
                nearby.append((max(lo-met, met-hi, 0), width))
        if not nearby:
            continue
        distance, width = min(nearby)
        nominal = observations[axis["id"]]
        own_widths = [m[2] for m in nominal if m[0] == "core"]
        allowance = max(own_widths, default=width)/2 + 1.5
        if distance <= allowance:
            lo = max(0, math.ceil(station-width/2)-axis["start"])
            hi = min(len(nominal)-1, math.floor(station+width/2)-axis["start"])
            if lo <= hi:
                yield lo, hi, other["id"]


def fit_pieces(g, axis, marks, centre, crossings, names):
    marks = list(marks)
    junctions = []
    for lo, hi, cross in crossings:
        junctions.append(dict(axis=cross, stations_px=[lo+axis["start"], hi+axis["start"]]))
        # Only carry a continuing same-tone core over a junction when BOTH approaches and
        # the centre pixels through the junction show that material. Never span ordinary gaps.
        flank = [marks[lo-1] if lo > 0 else None, marks[hi+1] if hi+1 < len(marks) else None]
        if all(m and m[0] == "core" for m in flank) and flank[0][1] == flank[1][1]:
            code = names.index(flank[0][1])
            w = min(flank[0][2], flank[1][2])
            depth = max(2, math.ceil(w/2))
            shoulders = marks[max(0, lo-depth):lo] + marks[hi+1:hi+1+depth]
            if (len(shoulders) == 2*depth and all(m[0] == "core" and m[1] == names[code] for m in shoulders)
                    and abs(flank[0][2]-flank[1][2]) <= max(1, w*0.25)
                    and np.all(np.mean(centre[lo:hi+1] == code, axis=1) >= 0.75)
                    and not any(m[0] in ("conflict", "excluded") for m in marks[lo:hi+1])):
                marks[lo:hi+1] = [("core", names[code], w)] * (hi-lo+1)
                continue
        for start, end, material in list(runs([m[1] if m[0] == "core" else None for m in marks])):
            if start >= lo and end <= hi and material is not None:
                marks[start:end+1] = [("junction", material, None)] * (end-start+1)
        for i in range(lo, hi+1):
            if marks[i][0] == "review":
                marks[i] = ("junction", None, None)
    ti = g["ti"]
    keeps = [pair(p, f"{g['id']} keep_points_px", True) for p in g.get("keep_points_px", [])]
    used = set()
    minimum = g.get("min_run_px", 4)
    if type(minimum) is not int or minimum < 2:
        raise ValueError(f"{g['id']}: min_run_px must be an integer >= 2")
    pieces = []
    for lo, hi, key in runs([(s, m) for s, m, w in marks]):
        status, role = key
        # Suppress one-pixel width oscillation. Split a sustained same-colour change in width.
        ranges = [(lo, hi)]
        if status == "core":
            widths = np.array([m[2] for m in marks[lo:hi+1]])
            smoothed = np.median(np.lib.stride_tricks.sliding_window_view(np.pad(widths, 1, mode="edge"), 3), axis=1)
            ranges = []
            first = 0
            for j, width in enumerate(smoothed):
                if abs(width-smoothed[first]) > max(1, smoothed[first]*0.2):
                    ranges.append((lo+first, lo+j-1))
                    first = j
            ranges.append((lo+first, hi))
        for start, end in ranges:
            first, last = start+axis["start"], end+axis["start"]
            width, state = None, status
            if status == "core":
                width = float(np.median([m[2] for m in marks[start:end+1]]))
                keep = {i for i, p in enumerate(keeps) if first <= p[ti] <= last
                        and abs(p[1-ti]-axis["normal"]) <= (marks[p[ti]-axis["start"]][2]-1)/2}
                used.update(keep)
                state = ("confirmed_core" if keep else "stable_core" if end-start+1 >= max(minimum, 1.5*width)
                         else "short_core")
            pieces.append(dict(stations_px=[first, last], span_px=[first-0.5, last+0.5],
                               state=state, material=role, width_px=width))
    if len(used) != len(keeps):
        raise ValueError(f"{g['id']}: keep_points_px must fall inside a measured core outside a crossing-only patch")
    axis["crossings"] = junctions
    axis["observations"] = pieces


def calibration(spec):
    matrix = spec.get("source_to_mm")
    if matrix is not None and (len(matrix) != 6 or not all(math.isfinite(i) for i in matrix)
                              or matrix[0] <= 0 or matrix[3] >= 0 or matrix[1:3] != [0, 0]):
        raise ValueError("Expected source_to_mm=[positive_sx,0,0,negative_sy,tx,ty] for a rectified image")
    return matrix


def apply_edits(g, axis, scale):
    # Half-pixel faces represent the shared boundary of adjoining pixel cells.
    spans = []
    for item in g.get("open_edges_px", []):
        spans.append((pair(item, "open edge"), "open_edge", {}))
    for op in g.get("openings", []):
        if op.get("kind") not in ("open", "window", "swing", "sliding", "rail") or not op.get("id"):
            raise ValueError(f"{g['id']}: specify ID and supported kind for each confirmed opening")
        spans.append((pair(op["span_px"], op["id"]), "opening", op))
    overrides = sorted([(min(ab), max(ab), state, op, ab) for ab, state, op in spans], key=lambda s:s[0])
    for i, (a, b, state, op, ab) in enumerate(overrides):
        if (not axis["start"]-0.5 <= a < b <= axis["end"]+0.5 or (i and overrides[i-1][1] > a)):
            raise ValueError(f"{g['id']}: confirmed openings/free edges must be distinct and inside the guide")
    axis["confirmed_voids"] = [dict(span_px=ab, kind=op.get("kind", state), id=op.get("id"))
                               for a, b, state, op, ab in overrides]
    walls, openings = [], []
    solid = []
    for obs in axis["observations"]:
        if obs["state"] not in ("stable_core", "confirmed_core"):
            continue
        lo, hi = obs["span_px"]
        cursor = lo
        for a, b, state, op, ab in overrides:
            if b <= cursor or a >= hi:
                continue
            if a > cursor:
                solid.append((cursor, a, obs))
            cursor = max(cursor, b)
        if cursor < hi:
            solid.append((cursor, hi, obs))
    for i, (a, b, obs) in enumerate(solid, 1):
        walls.append(dict(id=f"{axis['id']}-W{i}", a=point(axis,a,axis["normal"]), b=point(axis,b,axis["normal"]),
                          material=obs["material"], width_px=obs["width_px"], cap_a_mm=0, cap_b_mm=0))
    for a, b, state, op, ab in overrides:
        if state == "open_edge":
            continue
        # This host exists only together with its exact matching cut. Give it zero caps so nothing remains.
        adjacent = [obs for lo, hi, obs in solid if abs(hi-a) < 1e-6 or abs(lo-b) < 1e-6]
        material = op.get("material") or (adjacent[0]["material"] if adjacent else None)
        width = op.get("width_px", max((obs["width_px"] for obs in adjacent), default=None))
        if material not in ROLES or width is None or not math.isfinite(width) or width <= 0:
            raise ValueError(f"{op['id']}: supply material and width_px for a confirmed opening without adjoining measured walls")
        if set(op) & {"hosts", "a", "b"}:
            raise ValueError(f"{op['id']}: hosts and jamb coordinates are derived from the guide")
        axy = point(axis,ab[0],axis["normal"])
        bxy = point(axis,ab[1],axis["normal"])
        host = f"{axis['id']}-cut-{op['id']}"
        walls.append(dict(id=host, a=axy, b=bxy, material=material, width_px=width, cap_a_mm=0, cap_b_mm=0))
        extra = {k:v for k,v in op.items() if k not in ("span_px", "material", "width_px")}
        openings.append(dict(**extra, a=axy, b=bxy, hosts=[host]))
    if scale is not None:
        for wall in walls:
            wall["width_mm"] = round(wall.pop("width_px") * scale, 6)
    return walls, openings


def strip_image(image, axis, max_width, path):
    x0, x1 = axis["start"], axis["end"]
    vertical = axis["orientation"] == "V"
    whole = image.transpose(Image.Transpose.TRANSPOSE) if vertical else image
    n = axis["normal"]
    pad = max(12, math.ceil(2*max_width))
    top, bottom = max(0, math.floor(n-pad)), min(whole.height, math.ceil(n+pad+1))
    raw = whole.crop((x0, top, x1+1, bottom))
    # Give short piers enough screen space and keep long axes at their original resolution.
    zoom = min(4, max(1, math.ceil(620/raw.width)))
    raw = raw.resize((raw.width*zoom, raw.height*zoom), Image.Resampling.NEAREST)
    sheet = Image.new("RGB", (max(290, raw.width+28), raw.height+101), "white")
    sheet.paste(raw, (14, 18))
    draw = ImageDraw.Draw(sheet)
    draw.text((12, 2), f"{axis['orientation']}  {'y' if vertical else 'x'}  {x0} ... {x1} px   normal {n:g}", fill="black")
    bar = raw.height+23
    for obs in axis["observations"]:
        lo, hi = obs["stations_px"]
        key = obs["material"] if obs["state"] in ("stable_core", "confirmed_core") else obs["state"]
        draw.rectangle((14+(lo-x0)*zoom, bar, 14+(hi-x0+1)*zoom-1, bar+6), fill=COLOURS[key])
    for op in axis["confirmed_voids"]:
        lo, hi = sorted(op["span_px"])
        left = 14+(lo-x0+0.5)*zoom
        right = max(left, 14+(hi-x0+0.5)*zoom-1)
        draw.rectangle((left, bar+9, right, bar+15),
                       fill=COLOURS["open_edge" if op["kind"] == "open_edge" else "opening"])
    step = max(1, math.ceil(55/zoom/5)*5)
    for t in range(x0, x1+1, step):
        x = 14+(t-x0)*zoom
        draw.line((x,bar+18,x,bar+22),fill="black")
        draw.text((x-5,bar+23),str(t),fill="black")
    draw.text((12,bar+40),"red bearing / blue partition / gray no core",fill="black")
    draw.text((12,bar+53),"amber review / purple joint / green confirmed void",fill="black")
    draw.text((12,bar+66),"magenta conflict / dark gray excluded", fill="black")
    sheet.save(path)


def run(image_path, spec_path, out):
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise ValueError("Use a new or empty output directory")
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    with Image.open(image_path) as img:
        image = ImageOps.exif_transpose(img).convert("RGB")
    scope, names, codes, samples, guides, negatives = prepare(image, spec)
    matrix = calibration(spec)
    axes, observations, centres = [], {}, {}
    for g in guides:
        axis, marks, centre = observe(g, scope, codes, names, negatives)
        axes.append(axis)
        observations[g["id"]] = marks
        centres[g["id"]] = centre
    groups = {"walls": [], "openings": []}
    if matrix is not None:
        groups["source_to_mm"] = matrix
    out.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=out.parent) as temp:
        stage = Path(temp)/"output"
        stage.mkdir()
        for i, (g, axis) in enumerate(zip(guides, axes), 1):
            fit_pieces(g, axis, observations[g["id"]], centres[g["id"]], crossing_ranges(axis, axes, observations), names)
            scale = abs(matrix[3] if axis["orientation"] == "H" else matrix[0]) if matrix is not None else None
            walls, ops = apply_edits(g, axis, scale)
            groups["walls"].extend(walls)
            groups["openings"].extend(ops)
            axis["strip"] = f"axis-{i:03}.png"
            strip_image(image, axis, g["core_width_px"][1], stage/axis["strip"])
        for kind in ("walls", "openings"):
            ids = [o["id"] for o in groups[kind]]
            if len(ids) != len(set(ids)):
                raise ValueError(f"Duplicate {kind} IDs; give axes and openings unambiguous names")
        report = dict(image_sha256=hashlib.sha256(image_path.read_bytes()).hexdigest(), plan_box_px=scope,
                      samples=samples, source_to_mm=matrix, axes=axes)
        write_json(stage/"guide-report.json", report)
        write_json(stage/("cad-fragment.json" if matrix else "pixel-fragment.json"), groups)
        stage.replace(out)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    for arg in ("image", "guides", "out"):
        p.add_argument(arg, type=Path)
    args = p.parse_args()
    try:
        run(args.image, args.guides, args.out)
    except (ValueError, KeyError, OSError, TypeError) as exc:
        p.exit(2, f"fit_wall_guides: {exc}\n")
