"""Replay the 2026-11-11 Longxishan 132 four-chain check on the archived 132.png.

Recovered from the recorded final check_axes.py commands. CLI/file handling was
added for portability; scan bands, darkest-stroke selection, weights, precision,
and residual definitions are the historical ones. Output is JSON on stdout.
"""
import argparse
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import sys
from zipfile import ZipFile

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from vendor.house_plan_designer.axismap import Axis, chain

ORIGINAL_SHA256 = "def7f1fcc9c56bb47fb1de6389529782ff89fbf30c93cf7e9b2ad6374cfdd357"
# axis, approximate tick centres in printed order, printed spans in mm, inclusive scan band
CHAINS = {
    "上": ("x", [112, 205, 372, 462, 566], [3200, 5800, 3100, 3600], (185, 193)),
    "下": ("x", [112, 178, 256, 343, 400, 450, 502, 566], [2300, 2700, 3000, 2000, 1700, 1800, 2200], (634, 644)),
    "左": ("y", [228, 251, 410, 476, 491, 532, 585], [800, 5500, 2300, 500, 1400, 1850], (68, 76)),
    "右": ("y", [228, 251, 396, 476, 526, 585], [800, 5000, 2800, 1700, 2050], (628, 635)),
}
VERTICAL_END_BANDS = {"左": (49, 55), "右": (646, 658)}


def original_bytes(path):
    if path.suffix.lower() == ".zip":
        with ZipFile(path) as archive:
            matches = [n for n in archive.namelist() if Path(n).name == "132.png"]
            if len(matches) != 1:
                raise ValueError("本案复做包中应有一份 132.png")
            return archive.read(matches[0])
    return path.read_bytes()


def tick_center(data, guess, band):
    positions = np.arange(guess - 2, guess + 3)
    raw = data[band[0]:band[1]+1, positions].mean(axis=0)
    centre = int(positions[1 + np.argmin(raw[1:-1])])
    samples = np.arange(centre - 1, centre + 2)
    profile = data[band[0]:band[1]+1, samples].mean(axis=0)
    weights = np.maximum(0, 239 - profile)
    if weights.sum() <= 30:
        raise ValueError(f"刻线约位 {guess} 在条带 {band} 内缺少图像对比")
    return round(float(np.average(samples, weights=weights)), 3)


def replay(path):
    raw = original_bytes(path)
    if sha256(raw).hexdigest() != ORIGINAL_SHA256:
        raise ValueError("输入与这次独立复算使用的 132.png 哈希不符")
    with Image.open(BytesIO(raw)) as source:
        pixels = np.asarray(source.convert("L"), dtype=float)
    records, mappers = {}, {}
    for name, (axis, guesses, dims, band) in CHAINS.items():
        data = pixels if axis == "x" else pixels.T
        ticks = []
        for i, guess in enumerate(guesses):
            end = i in (0, len(guesses)-1)
            use_band = VERTICAL_END_BANDS[name] if axis == "y" and end else band
            ticks.append(tick_center(data, guess, use_band))
        pairs = chain(ticks, dims)
        mappers[name] = Axis(pairs)
        slope = sum(dims) / (ticks[-1] - ticks[0])
        offsets = [round((px - ticks[0]) * slope - mm, 2) for px, mm in pairs]
        records[name] = dict(axis=axis, profile_band_px=band, ticks_px=ticks, dimensions_mm=dims,
                             cumulative_mm=[mm for _, mm in pairs],
                             global_endpoint_fit_minus_labeled_mm=offsets,
                             max_abs_global_tick_residual_mm=max(map(abs, offsets)),
                             piecewise_tick_residual_mm=[round(mappers[name](px, snap=0)-mm, 5) for px, mm in pairs])
    for name, opposite in (("上", "下"), ("下", "上"), ("左", "右"), ("右", "左")):
        row = records[name]
        row["opposite_chain_prediction_minus_label_mm"] = [
            round(mappers[opposite](px, snap=0) - mm, 2) for px, mm in zip(row["ticks_px"], row["cumulative_mm"])]
    return dict(source="132.png", source_sha256=ORIGINAL_SHA256, axes=records)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("original", type=Path, help="本案复做包 ZIP 或其中的原始 132.png")
    args = parser.parse_args()
    try:
        print(json.dumps(replay(args.original), ensure_ascii=False, indent=2))
    except (OSError, ValueError) as exc:
        parser.exit(2, f"{exc}\n")
