#!/usr/bin/env python3
"""Place visually reviewed plan blocks from their rear envelope. All distances in mm."""
import argparse
from functools import lru_cache
import json
import os
from math import atan2, cos, degrees, hypot, isfinite, radians, sin
from pathlib import Path

import ezdxf

ASSETS = Path(__file__).resolve().parents[1] / "assets" / "plan-families.json"
MOVABLE = "活动家具"
FIXED = "固定家具及设备"

def _data():
    """Use the job's configured catalog first, then the local catalog, then the bundled symbols."""
    override = os.environ.get("MPCAD_FAMILY_CATALOG")
    if override:
        path = Path(override).expanduser()
    else:
        config_root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        local = config_root / "plan-to-cad" / "plan-families.json"
        path = local if local.is_file() else ASSETS
    return _load_catalog(path)


@lru_cache(maxsize=2)
def _load_catalog(path):
    return json.loads(path.read_text(encoding="utf-8"))


def catalog():
    """Return dimensions, source view, forward direction and actual rear contacts."""
    return {key: {k: v for k, v in rec.items() if k != "paths"}
            for key, rec in _data().items()}


def _number(value):
    value = float(value)
    if not isfinite(value):
        raise ValueError("坐标和尺寸须为有限数值")
    return value


def _pair(values):
    x, y = values
    return _number(x), _number(y)


def _sizes(rec, width, depth):
    w, d = rec["width_mm"], rec["depth_mm"]
    sx = _number(width) / w if width is not None else None
    sy = _number(depth) / d if depth is not None else None
    if sx is None:
        sx = sy if sy is not None else 1.
    if sy is None:
        sy = sx
    if min(sx, sy) <= 0:
        raise ValueError("宽和深须大于零")
    return sx * w, sy * d, sx, sy


def _put(layout, key, back, facing, width, depth, layer, mask=False):
    doc = layout.doc
    if doc.units != 4:
        raise ValueError("先在目标 CAD 声明毫米单位：doc.units = 4")
    rec = _data()[key]
    x, y = _pair(back)
    fx, fy = _pair(facing)
    if hypot(fx, fy) == 0:
        raise ValueError("前向不能为零向量")
    angle = degrees(atan2(fx, -fy))
    _, _, sx, sy = _sizes(rec, width, depth)
    block_name = "MPC_F1_" + key.upper() + ("_MASK" if mask else "")
    if block_name not in doc.blocks:
        block = doc.blocks.new(block_name)
        block.block_record.dxf.units = 4
        if mask:
            # These reviewed tabletop paths are rectangles. The native mask is drawn
            # first INSIDE the table. Its own outline and any future details draw above.
            if rec["kind"] != "table":
                raise ValueError("只有本目录的矩形桌板支持遮挡")
            a, d = rec["width_mm"] / 2, rec["depth_mm"]
            block.add_wipeout([(-a, 0), (a, 0), (a, -d), (-a, -d)])
        for path in rec["paths"]:
            attrs = {"layer": "0", "lineweight": 13}
            if len(path) == 2:
                block.add_line(*path, dxfattribs=attrs)
            else:
                closed = path[0] == path[-1]
                block.add_lwpolyline(path[:-1] if closed else path, close=closed,
                                     dxfattribs=attrs)
    if layer is None:
        layer = FIXED if rec["kind"] in {"toilet", "sink"} else MOVABLE
    if layer not in doc.layers:
        doc.layers.new(layer)
    return layout.add_blockref(block_name, (x, y), dxfattribs={
        "layer": layer, "rotation": angle, "xscale": sx, "yscale": sy})


def place_family(layout, key, back, facing=(0, -1), *, width_mm=None, depth_mm=None,
                 layer=None):
    """back = midpoint of rear envelope; facing = vector from rear toward user/foot/bowl.
    One dimension scales uniformly. Both dimensions explicitly scale each axis.
    """
    return _put(layout, key, back, facing, width_mm, depth_mm, layer)


def place_dining(layout, table, chair, center, *, upper_offsets_mm, lower_offsets_mm,
                 tuck_mm, angle_deg=0., table_width_mm=None, table_depth_mm=None,
                 chair_width_mm=None, chair_depth_mm=None, layer=None):
    """Offsets along the long X axis, measured from table center, independently per side.
    Upper is local +Y, lower is local -Y. angle_deg rotates the entire group CCW.
    Required tuck is the amount each complete chair extends under the tabletop.
    """
    data = _data()
    if data[table]["kind"] != "table" or data[chair]["kind"] != "chair":
        raise ValueError("需要矩形桌板和独立单椅")
    tw, td, _, _ = _sizes(data[table], table_width_mm, table_depth_mm)
    cw, cd, _, _ = _sizes(data[chair], chair_width_mm, chair_depth_mm)
    tuck = _number(tuck_mm)
    upper = [_number(v) for v in upper_offsets_mm]
    lower = [_number(v) for v in lower_offsets_mm]
    if not 0 <= tuck < cd:
        raise ValueError("推入桌内的长度须在零到单椅进深之间，且须露出椅背")
    if any(abs(v) > tw / 2 for v in upper + lower):
        raise ValueError("椅子的中心超出所选桌边")
    cx, cy = _pair(center)
    angle = radians(_number(angle_deg))
    cs, sn = cos(angle), sin(angle)

    def world(x, y):
        return cx + cs*x - sn*y, cy + sn*x + cs*y

    chairs = []
    for side, offsets in [(1, upper), (-1, lower)]:
        for offset in offsets:
            anchor = world(offset, side * (td/2 + cd - tuck))
            chairs.append(_put(layout, chair, anchor, (side*sn, -side*cs),
                               cw, cd, layer))
    tabletop = _put(layout, table, world(0, td/2), (sn, -cs), tw, td, layer,
                    mask=tuck > 0 and bool(chairs))
    # Exact world extents of the retained member strokes, including concealed chair parts.
    xs, ys = [], []
    for key, members in [(table, [tabletop]), (chair, chairs)]:
        for ref in members:
            matrix = ref.matrix44()
            for path in data[key]["paths"]:
                for p in path:
                    q = matrix.transform((p[0], p[1], 0))
                    xs.append(q.x)
                    ys.append(q.y)
    return {"table": tabletop, "chairs": chairs,
            "bounds_mm": (min(xs), min(ys), max(xs), max(ys))}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="列出已核对俯视块的尺寸、实际后靠范围和朝向")
    parser.add_argument("--full", action="store_true", help="输出图例来源和后靠线段/切点坐标")
    args = parser.parse_args()
    for name, item in catalog().items():
        if args.full:
            print(name, json.dumps(item, ensure_ascii=False))
        else:
            print(f"{name:19} {item['width_mm']:9.2f} × {item['depth_mm']:8.2f} mm   {item['label']}")
