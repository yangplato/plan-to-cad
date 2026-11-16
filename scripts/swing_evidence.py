#!/usr/bin/env python3
"""Compare doorway swing traces in source pixels. Requires Pillow. Image y increases downward."""
import argparse
import json
import math
import statistics
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps


def median(values):
    return statistics.median(values) if values else 0


def sample(image, x, y):
    w, h = image.size
    if not (0 <= x < w - 1 and 0 <= y < h - 1):
        return None
    ix, iy = math.floor(x), math.floor(y)
    dx, dy = x - ix, y - iy
    return sum(image.getpixel((ix + i, iy + j)) * wx * wy
               for i, wx in ((0, 1 - dx), (1, dx))
               for j, wy in ((0, 1 - dy), (1, dy)))


def measure(image, points, thickness, radius):
    # Choose a single registration offset and polarity for the whole trace.
    tolerance = min(5, max(1.5, thickness * 0.5))
    distance = min(max(2.6, thickness * 0.65 + 1), max(2.6, radius * 0.12))
    count = len(points)
    best = None
    for offset in (0, -0.5 * tolerance, 0.5 * tolerance, -tolerance, tolerance):
        profiles = []
        for x, y, nx, ny in points:
            def at(d):
                return sample(image, x + (offset + d) * nx, y + (offset + d) * ny)
            profile = [at(d) for d in (0, -distance, -distance * 1.45, distance, distance * 1.45)]
            if None in profile:
                profiles.append(None)
            else:
                c, l1, l2, r1, r2 = profile
                profiles.append((c, (l1 + l2) / 2, (r1 + r2) / 2,
                                 (abs(l1 - l2) + abs(r1 - r2)) / 2))
        for polarity in (1, -1):
            contrasts = [None if p is None else
                         min((p[1] - p[0]) * polarity, (p[2] - p[0]) * polarity)
                         for p in profiles]
            valid = [v for v in contrasts if v is not None]
            noise = median([p[3] for p in profiles if p is not None])
            threshold = max(8, 2.3 * noise)
            fraction = sum(v is not None and v >= threshold for v in contrasts) / count
            bins = [contrasts[k * count // 6:(k + 1) * count // 6] for k in range(6)]
            covered = sum(sum(v is not None and v >= threshold for v in b) >= len(b) * 0.5 for b in bins)
            contrast = max(0, median(valid))
            score = max(0, contrast - noise) * fraction * (covered / 6)
            report = {"contrast": round(contrast, 1), "coverage": round(fraction, 3),
                      "supported_sectors": covered, "visible_fraction": round(len(valid) / count, 3),
                      "polarity": "dark" if polarity == 1 else "light",
                      "offset_px": round(offset, 2), "score": round(score, 2)}
            if best is None or score > best[0]:
                best = (score, report)
    return best[1]


def get_traces(hinge, direction, normal, radius):
    hx, hy = hinge
    dx, dy = direction
    nx, ny = normal
    def arc(angle):
        vx = math.cos(angle) * dx + math.sin(angle) * nx
        vy = math.cos(angle) * dy + math.sin(angle) * ny
        return (hx + radius * vx, hy + radius * vy, vx, vy)
    def leaf(t):
        return (hx + radius * t * nx, hy + radius * t * ny, dx, dy)
    num = max(20, min(130, int(radius * 1.1)))
    arc_points = [arc(math.radians(8 + 74 * i / (num - 1))) for i in range(num)]
    leaf_points = [leaf(0.12 + 0.76 * i / (num - 1)) for i in range(num)]
    outline = [arc(math.radians(i))[:2] for i in range(91)]
    return arc_points, leaf_points, outline, [hinge, leaf(1)[:2]]


def evidence(source, a, b, ratios, thickness):
    image = source.convert("L")
    length = math.dist(a, b)
    dx, dy = (b[0] - a[0]) / length, (b[1] - a[1]) / length
    result, traces = [], []
    for ratio in ratios:
        group = []
        for ix, (name, h, f) in enumerate((("A", a, 1), ("B", b, -1))):
            for j, sign in enumerate((1, -1)):
                r = length * ratio
                n = (-dy * sign, dx * sign)
                arc, leaf, outline, segment = get_traces(h, (dx * f, dy * f), n, r)
                ar = measure(image, arc, thickness, r)
                le = measure(image, leaf, thickness, r)
                def supported(m):
                    return (m["visible_fraction"] >= 0.80 and m["coverage"] >= 0.60
                            and m["supported_sectors"] >= 4 and m["contrast"] >= 12)
                good = r >= 12 and supported(ar) and supported(le) and ar["polarity"] == le["polarity"]
                group.append({"id": 1 + 2 * ix + j, "hinge": name, "side": "+" if sign > 0 else "-",
                              "unit_open_vector_xy": [round(v, 4) for v in n], "arc": ar, "leaf": le,
                              "supported": good,
                              "relative_strength": round(math.sqrt(ar["score"] * le["score"]), 1) if good else None})
                traces.append((outline, segment))
        supported_ids = [c["id"] for c in group if c["supported"]]
        # Consider near misses before proclaiming a unique tendency. Multiple supported
        # candidates can correspond to symmetric leaves or existing drawing clutter.
        status = "inspect_source"
        if len(supported_ids) > 1:
            status = "multiple_supported_inspect_source"
        elif len(supported_ids) == 1:
            lead = group[supported_ids[0] - 1]
            runner = max(math.sqrt(c["arc"]["score"] * c["leaf"]["score"])
                         for c in group if c is not lead)
            if lead["relative_strength"] >= max(12, runner * 1.5, runner + 6):
                status = "single_supported_tendency"
        result.append({"radius_ratio": ratio, "radius_px": round(length * ratio, 2), "status": status,
                       "supported_ids": supported_ids, "candidates": group})
    return result, traces


def panel(source, a, b, results, traces):
    padding = 10
    all_points = [a, b] + [p for outline, segment in traces for p in outline + segment]
    x0 = max(0, math.floor(min(p[0] for p in all_points) - padding))
    y0 = max(0, math.floor(min(p[1] for p in all_points) - padding))
    x1 = min(source.width, math.ceil(max(p[0] for p in all_points) + padding + 1))
    y1 = min(source.height, math.ceil(max(p[1] for p in all_points) + padding + 1))
    crop = source.crop((x0, y0, x1, y1))
    scale = min(420 / crop.width, 420 / crop.height, 4)
    w, h = max(1, round(crop.width * scale)), max(1, round(crop.height * scale))
    base = crop.resize((w, h), Image.Resampling.LANCZOS)
    try:
        font = ImageFont.load_default(size=14)
    except TypeError:  # Pillow versions before scalable default fonts
        font = ImageFont.load_default()
    tile_w, tile_h = max(w, 184), h + 30
    sheet = Image.new("RGB", (tile_w * 3, tile_h * 2 * len(results)), "white")
    def mappt(p):
        return ((p[0] - x0 + 0.5) * w / crop.width - 0.5,
                (p[1] - y0 + 0.5) * h / crop.height - 0.5)
    for k, result in enumerate(results):
        for i in range(6):
            tx, ty = tile_w * (i % 3), tile_h * (2 * k + i // 3)
            draw = ImageDraw.Draw(sheet)
            if i == 5:
                draw.multiline_text((tx + 6, ty + 4), "Gray: opening A-B\nOrange: predicted arc\nBlue: predicted leaf\n+ = clockwise from A>B\nImage y increases down", font=font, fill="black", spacing=6)
                continue
            tile = base.copy()
            td = ImageDraw.Draw(tile)
            title = f"Source  |  radius {result['radius_ratio']:g}x"
            if i:
                c = result["candidates"][i - 1]
                outline, segment = traces[4 * k + i - 1]
                title = f"{i}  |  hinge {c['hinge']}   side {c['side']}"
                aa, bb = mappt(a), mappt(b)
                td.line((aa, bb), fill="#7b8389", width=1)
                points = list(map(mappt, outline))
                for start in range(0, 90, 6):
                    td.line(points[start:start + 4], fill="#ee7513", width=2)
                s, e = mappt(segment[0]), mappt(segment[1])
                for tick in range(0, 10, 2):
                    lerp = lambda t: (s[0] + (e[0] - s[0]) * t, s[1] + (e[1] - s[1]) * t)
                    td.line((lerp(tick / 10), lerp((tick + 1) / 10)), fill="#157ad4", width=2)
                for label, p in (("A", aa), ("B", bb)):
                    td.text((min(max(0, p[0] + 2), w - 11), min(max(0, p[1] - 16), h - 15)),
                            label, font=font, fill="#191919", stroke_width=1, stroke_fill="white")
            sheet.paste(tile, (tx, ty + 26))
            draw.text((tx + 4, ty + 3), title, font=font, fill="#191919")
    return sheet, [x0, y0, x1, y1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("--a", nargs=2, type=float, metavar=("X", "Y"), required=True)
    parser.add_argument("--b", nargs=2, type=float, metavar=("X", "Y"), required=True)
    parser.add_argument("--axis", choices=("h", "v"), help="Optional snap to horizontal or vertical at midpoint")
    parser.add_argument("--ratios", default="1", help="Comma-separated radii as fractions of opening, e.g. 1,0.68,0.32")
    parser.add_argument("--thickness", type=float, default=2, help="Apparent stroke/leaf thickness in source pixels")
    parser.add_argument("--out", type=Path, required=True, help="Directory for swing-overlay.png and swing-evidence.json")
    args = parser.parse_args()
    try:
        ratios = list(dict.fromkeys(float(s) for s in args.ratios.split(",")))
    except ValueError:
        parser.error("ratios must be comma-separated numbers")
    if not ratios or any(not 0 < r <= 1.2 for r in ratios):
        parser.error("radius ratios must be greater than zero and at most 1.2")
    a, b = list(args.a), list(args.b)
    with Image.open(args.image) as raw:
        displayed = ImageOps.exif_transpose(raw).convert("RGBA")
        source = Image.new("RGB", displayed.size, "white")
        source.paste(displayed, mask=displayed.getchannel("A"))
    if any(not (0 <= x < source.width and 0 <= y < source.height) for x, y in (a, b)):
        parser.error("opening endpoints must be within displayed image")
    if args.axis:
        idx = 1 if args.axis == "h" else 0
        a[idx] = b[idx] = (a[idx] + b[idx]) / 2
    if math.dist(a, b) < 3 or not (0 < args.thickness <= 100):
        parser.error("opening must span at least 3 pixels and thickness must be in (0,100]")
    result, traces = evidence(source, a, b, ratios, args.thickness)
    sheet, box = panel(source, a, b, result, traces)
    args.out.mkdir(parents=True, exist_ok=True)
    sheet.save(args.out / "swing-overlay.png")
    report = {"a_xy": a, "b_xy": b, "crop_xyxy_exclusive": box,
              "convention": "Image y increases down. Positive normal = (-dy, dx) from A to B.",
              "interpretation": "仅作原图像素辅助；比较同宽度的候选，强度不是概率。遮挡、低对比、彩色等亮线、非90度画法及多解均须回看原图自行判断。",
              "results": result}
    (args.out / "swing-evidence.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for entry in result:
        print(f"ratio={entry['radius_ratio']:g} {entry['status']} supported={entry['supported_ids']}")


if __name__ == "__main__":
    main()
