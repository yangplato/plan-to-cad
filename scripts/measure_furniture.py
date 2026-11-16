#!/usr/bin/env python3
"""Report INSERT geometry envelopes along placed block axes from a saved millimetre DXF.
Exit 0: extracted the selected objects. 1: partial/empty. 2: invalid input.
The report alone does not establish physical identity, completeness or usability.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import sys

import ezdxf
from ezdxf import path
from ezdxf.math import BoundingBox, Matrix44
from ezdxf.path.tools import precise_bbox
from ezdxf.xclip import XClip


PATH_TYPES = {"LINE", "LWPOLYLINE", "POLYLINE", "ARC", "CIRCLE", "ELLIPSE", "SPLINE"}


def block_layout(ref):
    name = ref.dxf.name
    block = ref.doc.blocks.get(name)
    if not block or block.block.is_xref or block.block.is_xref_overlay:
        raise ValueError(f"{name}: 空块、缺定义或外部参照")
    if ref.mcount > 1:
        raise ValueError(f"{name}: 阵列需展开后逐件核对")
    if ref.attribs or ref.dxf.get("invisible", 0):
        raise ValueError(f"{name}: 有附属属性或隐藏图形，需识别实物边")
    clip = XClip(ref)
    if clip.has_clipping_path and clip.is_clipping_enabled:
        raise ValueError(f"{name}: 启用了剪裁，需区分可见及完整范围")
    if abs(ref.dxf.extrusion.normalize().z) < 1.0 - 1e-9:
        raise ValueError(f"{name}: 物件倾出平面，需选平面实物边")
    return block


def geometry_bounds(block, transform, active):
    key = block.name.casefold()
    if key in active:
        raise ValueError(f"{block.name}: 循环块")
    bounds = BoundingBox()
    for entity in block:
        kind = entity.dxftype()
        if kind == "INSERT":
            nested = block_layout(entity)
            child_bounds = geometry_bounds(nested, entity.matrix44() @ transform, active | {key})
            bounds.extend((child_bounds.extmin, child_bounds.extmax))
            continue
        if entity.dxf.get("invisible", 0):
            raise ValueError(f"{kind}: 隐藏部分需人工确认")
        if kind == "HATCH":
            curves = path.from_hatch(entity)
        elif kind in PATH_TYPES:
            if kind in {"POLYLINE", "LWPOLYLINE"} and entity.has_width:
                raise ValueError(f"{kind}: 有宽度，中心线不能代表实体边界")
            if kind == "POLYLINE" and not (entity.is_2d_polyline or entity.is_3d_polyline):
                raise ValueError("POLYLINE: 网格需人工取边")
            curves = [path.make_path(entity, segments=4)]
        else:
            raise ValueError(f"{kind}: 需人工区分实体、注释或遮罩")
        for curve in curves:
            box = precise_bbox(curve.transform(transform))
            if box.has_data:
                bounds.extend((box.extmin, box.extmax))
    if not bounds.has_data:
        raise ValueError(f"{block.name}: 无可量实体")
    return bounds


def measure(ref):
    layout = block_layout(ref)
    # The nested transformations are resolved in the root block's own coordinates.
    # Its final two orthogonal axes rotate/mirror with its insertion.
    box = geometry_bounds(layout, Matrix44(), set())
    dx, dy, dz = box.size
    if dz > 1e-5:
        raise ValueError("不共面，需查看俯视投影并识别所在高度")
    placement = ref.matrix44()
    axes = [placement.transform_direction(direction) for direction in ((1, 0, 0), (0, 1, 0))]
    lengths = [dx * axes[0].magnitude, dy * axes[1].magnitude]
    if not all(math.isfinite(value) and value > 0 for value in lengths):
        raise ValueError("外包缺一轴或含非法坐标")
    return {
        "block_axis_extent_mm": [round(n, 3) for n in lengths],
        "world_axis_degrees_xy": [round(math.degrees(math.atan2(v.y, v.x)), 4) for v in axes],
        "top_xy_scale": [ref.dxf.xscale, ref.dxf.yscale],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("readback", type=Path)
    parser.add_argument("--layers", nargs=2, default=["活动家具", "固定家具及设备"])
    args = parser.parse_args()
    selected = {name.casefold() for name in args.layers}
    if len(selected) != 2 or "" in selected:
        parser.error("需要两个不同的非空家具层名")
    try:
        doc = ezdxf.readfile(args.readback)
        if doc.header.get("$INSUNITS") != 4:
            raise ValueError("需要已声明毫米的读回文件")
        if any(layer not in doc.layers for layer in args.layers):
            raise ValueError("指定图层缺失")
        rows = []
        for entity in doc.modelspace():
            if entity.dxf.layer.casefold() not in selected:
                continue
            row = {"handle": entity.dxf.handle, "layer": entity.dxf.layer, "type": entity.dxftype()}
            rows.append(row)
            if entity.dxftype() != "INSERT":
                row["review"] = "散图元需分组并核实实际物件"
                continue
            row["block"] = entity.dxf.name
            try:
                row.update(measure(entity))
            except (ValueError, TypeError, ZeroDivisionError, ezdxf.DXFError, RecursionError) as exc:
                row["review"] = str(exc)
        result = {
            "readback_sha256": hashlib.sha256(args.readback.read_bytes()).hexdigest(),
            "layer_counts": {name: sum(row["layer"].casefold() == name.casefold() for row in rows)
                             for name in args.layers},
            "objects": rows,
            "scope": "仅选层顶层对象。沿已插入块自身两轴取几何线外包。异形、内部斜放、多物合块及前后方向须另查。不判断完整性、使用冲突、漏项或全户通过。",
        }
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return int(not rows or any("review" in row for row in rows))
    except (OSError, UnicodeError, ValueError, ezdxf.DXFError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
