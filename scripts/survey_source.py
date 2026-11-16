#!/usr/bin/env python3
"""Survey a marketing floor plan from source pixels before any CAD or drawing ledger exists."""
import argparse
from collections import defaultdict
from hashlib import sha256
import json
import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageOps

from swing_evidence import evidence as swing_evidence


def bounds_ok(box, size):
    x0, y0, x1, y1 = box
    return 0 <= x0 < x1 <= size[0] and 0 <= y0 < y1 <= size[1]


def get_scope(image, rect, polygon):
    if polygon:
        points = [tuple(map(int, p.split(","))) for p in polygon.split()]
        if len(set(points)) < 3 or any(len(p) != 2 or not
                (0 <= p[0] < image.width and 0 <= p[1] < image.height) for p in points):
            raise ValueError("polygon requires at least three distinct in-image x,y vertices")
        area = abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(points, points[1:] + points[:1])))
        if area == 0:
            raise ValueError("polygon area must be positive; use a simple non-crossing polygon")
        xs, ys = zip(*points)
        box = [min(xs), min(ys), max(xs) + 1, max(ys) + 1]
    else:
        box = list(rect or (0, 0, image.width, image.height))
        points = None
    if not bounds_ok(box, image.size):
        raise ValueError("rect must be a nonempty half-open rectangle inside the original")
    mask = Image.new("1", (box[2] - box[0], box[3] - box[1]), not points)
    if points:
        ImageDraw.Draw(mask).polygon([(x - box[0], y - box[1]) for x, y in points], fill=1)
    return box, points, np.asarray(mask)


def tonal_masks(rgb, colors):
    # Thresholds identify raster tones, not structural material.
    mono = np.asarray(Image.fromarray(rgb).convert("L"))
    chroma = np.ptp(rgb, axis=-1)
    yield "dark", (mono <= 100), {"luma_max": 100}
    yield "mid-neutral", (mono > 100) & (mono <= 207) & (chroma <= 42), {
        "luma_min_exclusive": 100, "luma_max": 207, "max_rgb_range": 42}
    names = {"dark", "mid-neutral"}
    for spec in colors:
        name, code = spec.split("=", 1)
        value, *tolerance = code.lstrip("#").split(":")
        if not name or name in names or len(value) != 6 or len(tolerance) > 1:
            raise ValueError("color must use a unique NAME=RRGGBB[:RGB_DISTANCE] label")
        names.add(name)
        center = tuple(bytes.fromhex(value))
        tol = float(tolerance[0]) if tolerance else 18.
        if len(center) != 3 or not math.isfinite(tol) or not 0 < tol <= 442:
            raise ValueError("invalid RGB colour or tolerance")
        distance2 = np.zeros(mono.shape, dtype=np.float32)
        for channel, anchor in enumerate(center):
            distance2 += (rgb[:, :, channel].astype(np.float32) - anchor) ** 2
        yield name, distance2 <= tol ** 2, {"rgb": list(center), "max_distance": tol}


def intervals(bits):
    changes = np.flatnonzero(np.diff(np.r_[False, bits, False]))
    return zip(changes[::2], changes[1::2])  # half-open


def find_strokes(mask, orientation, width_bounds, minimum, jitter):
    """Collect normal-band widths first. A long single edge of a broad floor patch is not a core."""
    strips = mask.T if orientation == "H" else mask  # rows = longitudinal positions
    # A scan through a polygon boundary is not a measured band with two visible flanks.
    edges = np.diff(np.pad(strips, ((0, 0), (1, 1))).astype(np.int8), axis=1)
    long_indices, start = np.nonzero(edges == 1)
    _, end = np.nonzero(edges == -1)
    widths = end - start
    use = (start > 0) & (end < strips.shape[1]) & (widths >= width_bounds[0]) & (widths <= width_bounds[1])
    mids = (start[use] + end[use] - 1) // 2
    bands = np.zeros((strips.shape[1], strips.shape[0]), dtype=np.int32)
    bands[mids, long_indices[use]] = widths[use]
    suggested = []
    for n in np.flatnonzero(np.any(bands, axis=1)):
        n0, n1 = max(0, n - jitter), min(len(bands), n + jitter + 1)
        nearby = bands[n0:n1]
        support = np.any(nearby, axis=0)
        if support.sum() < minimum:
            continue
        for a, end in intervals(support):
            if end - a < minimum:
                continue
            ns, ts = np.nonzero(nearby[:, a:end])
            w = nearby[ns, a + ts]
            # Even-width bands are centred between pixels. Restore the half-pixel here.
            centers = ns + n0 + ((w % 2) == 0) * 0.5
            median = np.median(centers)
            suggested.append({"normal": float(median), "lo": int(a), "hi": int(end - 1),
                              "width_px": float(np.median(w)), "center_spread_px": round(float(
                                  np.percentile(np.abs(centers - median), 90)), 2)})
    return suggested


def distinct_strokes(strokes, jitter):
    kept = []
    by_normal = defaultdict(list)
    # First prefer longest support, then the measured swatch where the geometry ties.
    for s in sorted(strokes, key=lambda r: (r["hi"] - r["lo"], r["tone"] not in ("dark", "mid-neutral")),
                    reverse=True):
        neighbors = [k for n in range(math.floor(s["normal"] - jitter - 1), math.ceil(s["normal"] + jitter + 1) + 1)
                     for k in by_normal[n]]
        covered = None
        for k in neighbors:
            intersection = min(k["hi"], s["hi"]) - max(k["lo"], s["lo"]) + 1
            if (abs(k["normal"] - s["normal"]) <= jitter + 0.5 and
                    intersection >= 0.9 * (s["hi"] - s["lo"] + 1)):
                covered = k
                break
        if covered is not None:
            if s["tone"] != covered["tone"]:
                overlap = {"tone": s["tone"], "lo": max(covered["lo"], s["lo"]),
                           "hi": min(covered["hi"], s["hi"])}
                if overlap not in covered["tone_overlaps"]:
                    covered["tone_overlaps"].append(overlap)
            continue
        s["tone_overlaps"] = []
        kept.append(s)
        by_normal[math.floor(s["normal"])].append(s)
    return sorted(kept, key=lambda r: (r["normal"], r["lo"], r["hi"]))


def point(orientation, t, n):
    return [t, n] if orientation == "H" else [n, t]


def descriptive_extent(item):
    a, b = item["a_px"], item["b_px"]
    return [math.floor(min(a[0], b[0])), math.floor(min(a[1], b[1])),
            math.ceil(max(a[0], b[0])) + 1, math.ceil(max(a[1], b[1])) + 1]


def indexed_strokes(rgb, inside, origin, scale, colors):
    thick_low = max(2, math.ceil(55 / scale))
    thick_high = max(thick_low, math.floor(420 / scale))
    thin_high = max(1, math.floor(75 / scale))
    jitter = min(2, max(0, round(16 / scale)))
    buckets = defaultdict(list)
    swatches = {}
    for tone, raw_mask, rule in tonal_masks(rgb, colors):
        swatches[tone] = rule
        # Requiring both neighbouring pixels inside prevents the polygon edge from manufacturing a band.
        for orient, cross_axis in (("H", 0), ("V", 1)):
            mask = raw_mask & inside
            # Fill outside with foreground to exclude a band truncated at the scope edge.
            mask = mask | ~inside
            for kind, width, length_mm in (("W", (thick_low, thick_high), 500),
                                             ("L", (1, thin_high), 540)):
                minimum = max(6, math.ceil(length_mm / scale))
                t0, n0 = (origin[0], origin[1]) if orient == "H" else (origin[1], origin[0])
                for s in find_strokes(mask, orient, width, minimum, jitter):
                    # Concave ROI boundaries can create a narrow strip of outside pixels.
                    ts = np.arange(s["lo"], s["hi"] + 1)
                    n = round(s["normal"])
                    sample = inside[n, ts] if cross_axis == 0 else inside[ts, n]
                    if not sample.all():
                        continue
                    s.update(tone=tone, orientation=orient, normal=s["normal"] + n0,
                             lo=s["lo"] + t0, hi=s["hi"] + t0)
                    buckets[kind, orient].append(s)
    all_strokes = {}
    for kind in ("W", "L"):
        items = []
        for orient in ("H", "V"):
            for s in distinct_strokes(buckets[kind, orient], jitter):
                s.update(id=f"{kind}{len(items)+1:03d}", a_px=point(orient, s["lo"], s["normal"]),
                         b_px=point(orient, s["hi"], s["normal"]),
                         width_mm_approx=round(s["width_px"] * scale, 1))
                length = s["hi"] - s["lo"] + 1
                s["line_evidence"] = "long_stable" if (length * scale >= 1000 and
                        length >= 5 * s["width_px"] and s["center_spread_px"] <= 1) else "inspect"
                items.append(s)
        all_strokes[kind] = items
    return all_strokes["W"], all_strokes["L"], swatches


def pair_thin_lines(lines, scale):
    pairs = []
    for i, a in enumerate(lines):
        for b in lines[i+1:]:
            if b["orientation"] != a["orientation"]:
                break
            offset = b["normal"] - a["normal"]
            if offset * scale > 350:
                break
            lo, hi = max(a["lo"], b["lo"]), min(a["hi"], b["hi"])
            if (hi - lo + 1) * scale < 500 or offset <= (a["width_px"] + b["width_px"]) / 2:
                continue
            pairs.append({"id": f"P{len(pairs)+1:03d}", "orientation": a["orientation"],
                          "lines": [a["id"], b["id"]], "spacing_mm_approx": round(offset * scale, 1),
                          "a_px": point(a["orientation"], lo, a["normal"]),
                          "b_px": point(a["orientation"], hi, b["normal"]),
                          "interpretation": "parallel strokes; inspect for window, rail, track, furniture or dimension"})
    return pairs


def find_gaps(walls, lines, source, scope, inside, scale, enable_swing):
    gaps = []
    for a in walls:
        tolerance = max(1.0, min(65 / scale, a["width_px"] * 0.6))
        others = [b for b in walls if b["orientation"] == a["orientation"]
                  and abs(a["normal"] - b["normal"]) <= tolerance and b["hi"] > a["hi"]]
        if not others or any(b["lo"] <= a["hi"] + 1 for b in others):
            continue
        b = min(others, key=lambda c: (c["lo"], abs(c["normal"] - a["normal"])))
        lo, hi = a["hi"]+1, b["lo"]-1
        size_mm = (hi-lo+1)*scale
        if size_mm > 5000 or hi-lo < 1:
            continue
        n = (a["normal"] + b["normal"]) / 2
        ori = a["orientation"]
        x0, y0 = scope[:2]
        along = np.arange(lo, hi+1)
        in_scope = inside[round(n)-y0, along-x0] if ori == "H" else inside[along-y0, round(n)-x0]
        if not in_scope.all():
            continue
        tracks = [s["id"] for s in lines if s["orientation"] == ori and
                  abs(s["normal"]-n) * scale <= 220 and
                  (min(s["hi"], hi) - max(s["lo"], lo) + 1) >= (hi-lo+1) * 0.6]
        axy, bxy = point(ori, lo, n), point(ori, hi, n)
        gap = {"id": f"G{len(gaps)+1:03d}", "orientation": ori, "brackets": [a["id"], b["id"]],
               "a_px": axy, "b_px": bxy, "length_mm_approx": round(size_mm, 1),
               "kind": "short_interruption" if size_mm < 450 else "opening_or_obscured_core",
               "parallel_line_ids": tracks, "swing": "unexamined"}
        # A complete single leaf usually fits this range; it is only a source-pixel search filter.
        if enable_swing and 550 <= size_mm <= 1300 and hi-lo >= 12:
            x, y = axy
            x2, y2 = bxy
            r = hi-lo
            box = [math.floor(min(x,x2) - (r if ori == "V" else 0) - 4),
                   math.floor(min(y,y2) - (r if ori == "H" else 0) - 4),
                   math.ceil(max(x,x2) + (r if ori == "V" else 0) + 5),
                   math.ceil(max(y,y2) + (r if ori == "H" else 0) + 5)]
            if (bounds_ok(box, source.size) and box[0] >= scope[0] and box[1] >= scope[1]
                    and box[2] <= scope[2] and box[3] <= scope[3]
                    and inside[box[1]-y0:box[3]-y0, box[0]-x0:box[2]-x0].all()):
                image = source.crop(box)
                results, _ = swing_evidence(image, [x-box[0],y-box[1]], [x2-box[0],y2-box[1]], [1.0], 2)
                result = results[0]
                gap["swing"] = result["status"]
                gap["possible_leaf"] = [{"hinge": c["hinge"], "unit_open_vector_image_xy":
                                         c["unit_open_vector_xy"]} for c in result["candidates"] if c["supported"]]
            else:
                gap["swing"] = "insufficient_scope_for_four_candidates"
        gaps.append(gap)
    return gaps


def collect_conflicts(walls, gaps, pairs, scale):
    issues = []
    def add(kind, ids, reason):
        issues.append({"kind":kind, "feature_ids":ids, "inspect":reason})
    for wall in walls:
        # Detached-looking bars are common furniture edges. The same cue can be a real wall stub.
        tolerance = max(2, 260 / scale)
        joined = any(other["orientation"] != wall["orientation"] and
                     other["lo"] - tolerance <= wall["normal"] <= other["hi"] + tolerance and
                     wall["lo"] - tolerance <= other["normal"] <= wall["hi"] + tolerance
                     for other in walls)
        if not joined:
            add("isolated_wall_width_band", [wall["id"]], "Check furniture, step, counter or detached wall.")
        if wall["tone_overlaps"]:
            add("overlapping_tone_masks", [wall["id"]],
                "Use the recorded overlap intervals to inspect colour changes and both physical wall faces.")
    for gap in gaps:
        if gap["parallel_line_ids"]:
            add("gap_with_thin_strokes", [gap["id"], *gap["parallel_line_ids"]],
                "Look for jambs, mullions, tracks and an exterior edge; compare window, slider and railing.")
        across = []
        for other in walls:
            if other["orientation"] == gap["orientation"]:
                continue
            ti, ni = (0,1) if gap["orientation"] == "H" else (1,0)
            a, b = gap["a_px"], gap["b_px"]
            if (a[ti]-1 <= other["normal"] <= b[ti]+1 and
                    other["lo"]-other["width_px"] <= a[ni] <= other["hi"]+other["width_px"]):
                across.append(other["id"])
        if across:
            add("perpendicular_band_at_gap", [gap["id"], *across],
                "Check T-junction or crossing core before interpreting this interruption as an opening.")
    for pair in pairs:
        if not any(set(pair["lines"]).issubset(gap["parallel_line_ids"]) for gap in gaps):
            add("parallel_strokes_without_bracketed_gap", [pair["id"]],
                "Check exterior balcony edge, dimension or furniture; bracketed masonry evidence is absent.")
    return issues


def overlaps(a,b):
    return a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]


def make_crops(source, scope, inside, tile_size, rooms, features, out):
    tiles, cells = [], []
    covered = 0
    for name, box in [("scope", scope)] + rooms:
        if not bounds_ok(box, source.size):
            raise ValueError(f"invalid source box for {name}")
        for y in range(box[1], box[3], tile_size):
            for x in range(box[0], box[2], tile_size):
                core = [x, y, min(x + tile_size, box[2]), min(y + tile_size, box[3])]
                if name == "scope":
                    count = int(inside[y-scope[1]:core[3]-scope[1], x-scope[0]:core[2]-scope[0]].sum())
                    if not count:
                        continue
                    covered += count
                context = [max(0,x-12), max(0,y-12), min(source.width,core[2]+12), min(source.height,core[3]+12)]
                index = len(tiles)
                tag = f"T{index+1:03d}"
                tiles.append({"id":tag, "region":name, "core_xyxy":core, "crop_xyxy":context,
                              "sheet":f"crops-{index//4+1:03d}.png", "slot":index%4+1,
                              "features":[f["id"] for f in features if overlaps(core, descriptive_extent(f))]})
                cells.append((tag, context))
    if covered != int(inside.sum()):
        raise ValueError("survey failed full scope coverage check")
    cell_size = tile_size+24
    for i in range(0, len(cells), 4):
        group = cells[i:i+4]
        col_width = max(max(box[2]-box[0] for _,box in group), 218)
        row_height = min(cell_size, max(box[3]-box[1] for _,box in group))+23
        page = Image.new("RGB",(col_width*min(len(group),2),row_height*math.ceil(len(group)/2)), "#ebeced")
        draw = ImageDraw.Draw(page)
        for j,(tag,box) in enumerate(group):
            x, y = (j%2)*col_width, (j//2)*row_height
            draw.text((x+4,y+3), f"{tag}   ({box[0]}, {box[1]}) - ({box[2]}, {box[3]})", fill="black")
            page.paste(source.crop(box), (x, y+23))
        page.save(out / f"crops-{i//4+1:03d}.png")
    return tiles,covered


def overview(image, scope, polygon, features, tiles, out):
    panel = image.crop(scope)
    # Panel is an annotated index. The fixed-aspect raw sheets retain the original pixels.
    draw = ImageDraw.Draw(panel)
    x0,y0 = scope[:2]
    if polygon:
        draw.line([(x-x0,y-y0) for x,y in polygon+[polygon[0]]], fill="#da2caf", width=2)
    for tile in tiles:
        if tile["region"] == "scope":
            l,t,r,b = tile["core_xyxy"]
            draw.rectangle((l-x0,t-y0,r-x0-1,b-y0-1),outline="#397bf6")
            draw.text((l-x0+2,t-y0+1),tile["id"],fill="#195ad1",stroke_width=1,stroke_fill="white")
    colors = {"W":"#e73753","G":"#fa9b17","P":"#0a8f71"}
    for f in features:
        prefix = f["id"][0]
        if prefix in colors:
            a,b = f["a_px"], f["b_px"]
            a,b = (a[0]-x0,a[1]-y0), (b[0]-x0,b[1]-y0)
            if prefix == "P":
                draw.rectangle((*a,*b), outline=colors[prefix])
            else:
                draw.line((a,b),fill=colors[prefix],width=2)
            draw.text((a[0]+2,a[1]+2), f["id"], fill=colors[prefix], stroke_width=1, stroke_fill="white")
    panel.save(out / "overview.png")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("--mm-per-px", type=float, required=True)
    roi = parser.add_mutually_exclusive_group()
    roi.add_argument("--rect", type=int, nargs=4, metavar=("LEFT","TOP","RIGHT","BOTTOM"))
    roi.add_argument("--polygon", help="quoted simple polygon, e.g. '0,0 300,0 290,200 0,200'")
    parser.add_argument("--color", action="append", default=[], help="additional tonal mask, NAME=RRGGBB[:tolerance]")
    parser.add_argument("--rooms", type=Path, help='optional JSON object mapping names to source-pixel [l,t,r,b] boxes')
    parser.add_argument("--tile-px", type=int, default=512, help="core size for native pixel crop sheets")
    parser.add_argument("--skip-swing", action="store_true", help="omit four-way arc/leaf comparison")
    parser.add_argument("--out", type=Path, required=True, help="new or empty output directory")
    args = parser.parse_args()
    try:
        if not math.isfinite(args.mm_per_px) or args.mm_per_px <= 0 or not 32 <= args.tile_px <= 2048:
            raise ValueError("scale must be finite and positive; tile size must be 32..2048")
        with Image.open(args.image) as original:
            rgba = ImageOps.exif_transpose(original).convert("RGBA")
            source = Image.new("RGB",rgba.size,"white")
            source.paste(rgba,mask=rgba.getchannel("A"))
        scope,polygon,inside = get_scope(source, args.rect, args.polygon)
        rooms = []
        if args.rooms:
            for key,box in json.loads(args.rooms.read_text(encoding="utf8")).items():
                if (not key or key == "scope" or len(box) != 4 or any(type(v) is not int for v in box)
                        or not bounds_ok(box, source.size)):
                    raise ValueError(f"invalid named room or room box: {key}")
                rooms.append((key,box))
        if args.out.exists() and (not args.out.is_dir() or any(args.out.iterdir())):
            raise ValueError("use a new or empty output directory")
        rgb = np.asarray(source.crop(scope))
        walls,lines,swatches = indexed_strokes(rgb, inside, scope[:2], args.mm_per_px, args.color)
        pairs = pair_thin_lines(lines, args.mm_per_px)
        gaps = find_gaps(walls,lines,source,scope,inside,args.mm_per_px,not args.skip_swing)
        features = walls + lines + pairs + gaps
        args.out.mkdir(exist_ok=True,parents=True)
        tiles,covered = make_crops(source, scope, inside, args.tile_px, rooms, features, args.out)
        overview(source, scope, polygon, features, tiles, args.out)
        report = {"source":args.image.name, "sha256":sha256(args.image.read_bytes()).hexdigest(),
                  "size_px":source.size, "coordinates":"displayed source pixels, y down, rectangles half-open; segment endpoints inclusive",
                  "scope_xyxy":scope,"polygon_xy":polygon,"mm_per_px":args.mm_per_px,"tone_rules":swatches,
                  "locators":{"W":"wall-width tonal stroke","L":"thin tonal stroke","P":"parallel pair","G":"between two wall-width strokes","T":"unmarked original crop"},
                  "limits":["Tones do not identify masonry type. Pixel-only line confidence is not architectural confidence.",
                            "Furniture, hatching, dimensions, shadows and intersecting wall bands can generate false candidates.",
                            "Check every T tile including those with zero candidates. Diagonal walls, pale strokes, covered cores and edge openings need visual reading."],
                  "wall_band_candidates":walls,"thin_lines":lines,"parallel_pairs":pairs,
                  "gap_candidates":gaps, "manual_conflicts":collect_conflicts(walls, gaps, pairs, args.mm_per_px),
                  "tiles":tiles,"covered_scope_pixels":covered,"scope_pixels":int(inside.sum())}
        (args.out / "source-survey.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf8")
        print(f"{len(tiles)} source tiles; {covered} scope pixels covered; {len(walls)} wall-width strokes, "
              f"{len(lines)} thin strokes, {len(pairs)} parallel pairs, {len(gaps)} gaps")
    except (OSError,ValueError,TypeError,AttributeError) as e:
        parser.error(str(e))


if __name__ == "__main__":
    main()
