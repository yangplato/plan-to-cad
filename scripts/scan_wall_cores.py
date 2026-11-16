"""Suggest wall-core colour intervals on operator-marked horizontal/vertical axes in an original image."""
import argparse
from itertools import groupby
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

PALETTE = ("#ff3864", "#009bcc", "#8b4de0", "#25a75e", "#db8165")


def _rgb_distance(values, colour):
    return np.linalg.norm(values - colour, axis=-1)


def classify(image, scope, samples):
    """Estimate colour centres from this image only. Pixel array covers the plan scope."""
    x0, y0, x1, y1 = scope
    pixels = np.asarray(image.crop((x0, y0, x1 + 1, y1 + 1)), dtype=np.float32)
    names, centres, tolerances = [], [], []
    for item in samples:
        name = item["material"]
        if not name or name in names:
            raise ValueError("Each sample group needs a distinct material name")
        radius = item.get("radius_px", 1)
        if not isinstance(radius, int) or radius < 0:
            raise ValueError(f"{name}: invalid swatch radius")
        patches = []
        for x, y in item["points_px"]:
            if not (x0 + radius <= x <= x1 - radius and y0 + radius <= y <= y1 - radius):
                raise ValueError(f"{name}: swatch outside scope")
            patches.append(pixels[y-y0-radius:y-y0+radius+1, x-x0-radius:x-x0+radius+1].reshape(-1, 3))
        if not patches:
            raise ValueError(f"{name}: missing wall-core swatch")
        values = np.concatenate(patches)
        centre = np.median(values, axis=0)
        tol = float(item.get("tolerance_rgb", max(8., 2 * np.percentile(_rgb_distance(values, centre), 90))))
        if not np.isfinite(tol) or tol <= 0:
            raise ValueError(f"{name}: invalid tolerance_rgb")
        names.append(name)
        centres.append(centre)
        tolerances.append(tol)
    if not names:
        raise ValueError("Supply wall-core sample groups")
    if len(names) > 1:
        for i, centre in enumerate(centres):
            nearest = min(np.linalg.norm(centre - c) for j, c in enumerate(centres) if i != j)
            if nearest == 0:
                raise ValueError("Two material groups have the same sampled RGB centre")
            tolerances[i] = min(tolerances[i], 0.48 * nearest)
    codes = np.full(pixels.shape[:2], -1, dtype=np.int32)
    for code, (centre, tol) in enumerate(zip(centres, tolerances)):
        codes[_rgb_distance(pixels, centre) <= tol] = code
    return names, codes, [dict(material=n, rgb=c.tolist(), tolerance_rgb=round(t, 2))
                          for n, c, t in zip(names, centres, tolerances)]


def runs(values):
    start = 0
    for label, members in groupby(values):
        end = start + sum(1 for _ in members)
        yield start, end - 1, label
        start = end


def scan_axis(axis, codes, scope, names, negatives):
    ident = axis["id"]
    x0, y0, x1, y1 = scope
    a, b = axis["a_px"], axis["b_px"]
    if a == b or (a[0] != b[0] and a[1] != b[1]):
        raise ValueError(f"{ident}: mark one horizontal or vertical rough axis")
    if not all(x0 <= x <= x1 and y0 <= y <= y1 for x, y in (a, b)):
        raise ValueError(f"{ident}: axis leaves plan scope")
    horizontal = a[1] == b[1]
    ti, ni = (0, 1) if horizontal else (1, 0)
    t0, t1 = sorted((a[ti], b[ti]))
    hint = a[ni]
    t_origin, n_origin = (x0, y0) if horizontal else (y0, x0)
    stripes = codes.T if horizontal else codes
    w0, w1 = axis["core_width_px"]
    search = axis["search_px"]
    minimum = axis.get("min_run_px", max(2, int(w0)))
    if not all(isinstance(v, int) for v in (w0,w1,search,minimum)) or not (0 < w0 <= w1 and search >= 0 and minimum > 0):
        raise ValueError(f"{ident}: invalid core width, normal search or minimum run")
    # Include both flanks plus one pixel to tell a wall from a wide patch at the search boundary.
    lo = max(0, hint-n_origin-search-w1-1)
    hi = min(stripes.shape[1]-1, hint-n_origin+search+w1+1)
    candidates = []
    support = {}
    for t in range(t0, t1+1):
        section = stripes[t-t_origin, lo:hi+1]
        options = []
        for j, k, code in runs(section):
            width = k-j+1
            centre = n_origin+lo+(j+k)/2
            if (code < 0 or j == 0 or k == len(section)-1 or not w0 <= width <= w1
                    or section[j-1] == -2 or section[k+1] == -2 or abs(centre-hint) > search):
                continue
            options.append((centre, width, int(code)))
            support[centre] = support.get(centre, 0)+1
        candidates.append(options)
    # Determine a stable straight line first; no tracing a sofa edge across a door gap.
    jitter = float(axis.get("jitter_px", max(0.5, w0/4)))
    if not np.isfinite(jitter) or jitter < 0:
        raise ValueError(f"{ident}: invalid jitter")
    ranked = sorted(((sum(n for d,n in support.items() if abs(d-c) <= jitter),
                      -abs(c-hint), c) for c in support), reverse=True)
    anchor = axis.get("anchor_px")
    if anchor is not None:
        at, an = anchor[ti], anchor[ni]
        options = candidates[at-t0] if t0 <= at <= t1 else []
        anchored = [c for c,w,_ in options if abs(c-an) <= (w-1)/2]
        if len(anchored) != 1:
            raise ValueError(f"{ident}: anchor does not lie inside one sampled core on this axis")
        base = anchored[0]
        competing = False
    else:
        base = ranked[0][2] if ranked else hint
        competing = any(score >= 0.90*ranked[0][0] and abs(c-base) > 2*jitter
                        and abs(abs(c-hint)-abs(base-hint)) <= jitter for score,_,c in ranked)
    selected, labels = [], []
    for options in candidates:
        near = [v for v in options if abs(v[0]-base) <= jitter]
        hit = near[0] if len(near) == 1 and not competing else None
        selected.append(hit)
        labels.append(hit[2] if hit else -1)
    for j,k,code in list(runs(labels)):
        if code >= 0 and k-j+1 < minimum:
            labels[j:k+1] = [-1]*(k-j+1)
    pieces = []
    for j,k,code in runs(labels):
        mid, width = base, None
        flags = []
        if code >= 0:
            section = selected[j:k+1]
            mid = float(np.median([s[0] for s in section]))
            width = float(np.median([s[1] for s in section]))
            for point in negatives:
                t, n = point[ti], point[ni]
                if t0+j <= t <= t0+k:
                    actual, w, _ = selected[t-t0]
                    if abs(n-actual) <= (w-1)/2:
                        flags.append(point)
        def xy(t):
            return [t, mid] if horizontal else [mid, t]
        pieces.append(dict(a_px=xy(t0+j), b_px=xy(t0+k), core_width_px=width,
                           material=names[code] if code >= 0 else None,
                           status="conflict" if flags else "proposal" if code >= 0 else "unresolved",
                           negative_hits_px=flags))
    changes = [dict(before_last_px=left["b_px"], after_first_px=right["a_px"],
                    before=left["material"], after=right["material"],
                    before_status=left["status"], after_status=right["status"])
               for left, right in zip(pieces, pieces[1:])]
    return dict(id=ident, orientation="H" if horizontal else "V", core_normal_px=base,
                competing_axes=competing, segments=pieces, changes=changes)


def scan_image(image, spec):
    image = image.convert("RGB")
    scope = spec["plan_box_px"]
    x0, y0, x1, y1 = scope
    if not (0 <= x0 < x1 < image.width and 0 <= y0 < y1 < image.height):
        raise ValueError("plan_box_px must be an inclusive pixel box within the original image")
    names, codes, samples = classify(image, scope, spec["samples"])
    for rect in spec.get("exclude_boxes_px", []):
        l,t,r,b = rect
        if not (x0 <= l <= r <= x1 and y0 <= t <= b <= y1):
            raise ValueError("exclude_boxes_px must be inside plan_box_px")
        codes[t-y0:b-y0+1, l-x0:r-x0+1] = -2
    negatives = spec.get("nonwall_points_px", [])
    if any(not (x0 <= x <= x1 and y0 <= y <= y1) for x,y in negatives):
        raise ValueError("Nonwall check point outside the plan")
    axes = [scan_axis(axis, codes, scope, names, negatives) for axis in spec["axes"]]
    if not axes or len({a["id"] for a in axes}) != len(axes):
        raise ValueError("Supply axes with unique identifiers")
    overlay = image.copy()
    draw = ImageDraw.Draw(overlay)
    for index, axis in enumerate(axes, start=1):
        for seg in axis["segments"]:
            a, b = seg["a_px"], seg["b_px"]
            if seg["status"] == "proposal":
                colour = PALETTE[names.index(seg["material"]) % len(PALETTE)]
            else:
                colour = "#ff9e00" if seg["status"] == "unresolved" else "#cf00ff"
            draw.line((tuple(a),tuple(b)), fill=colour, width=2)
            draw.ellipse((a[0]-2,a[1]-2,a[0]+2,a[1]+2), outline=colour)
        a = axis["segments"][0]["a_px"]
        # Number the axes so identifiers in any script map to the JSON without relying on OS fonts.
        axis["overlay_no"] = index
        label = str(axis["overlay_no"])
        draw.text((a[0]+3,a[1]+3),label,fill="white",stroke_width=1,stroke_fill="black")
    for x,y in negatives:
        draw.line((x-3,y-3,x+3,y+3),fill="#ff00b3",width=1)
        draw.line((x-3,y+3,x+3,y-3),fill="#ff00b3",width=1)
    return dict(units="source_px_y_down_inclusive", samples=samples, axes=axes,
                colours={name:PALETTE[i%len(PALETTE)] for i,name in enumerate(names)},
                unresolved_colour="#ff9e00", conflict_colour="#cf00ff"), overlay


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("image","spec","report","overlay"):
        p.add_argument(name)
    args = p.parse_args()
    with Image.open(args.image) as image:
        report, overlay = scan_image(image, json.loads(Path(args.spec).read_text()))
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n")
    overlay.save(args.overlay)
