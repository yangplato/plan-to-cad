"""Place editable plan fixtures by rear midpoint, front direction and actual mm footprint."""
import argparse
from functools import lru_cache
import hashlib
import json
from math import atan2, degrees, isfinite
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory

import ezdxf
from ezdxf.path import make_path

from place_plan_families import FIXED, place_family

ATTACHED = "建筑附属"
FAMILIES = ("sink", "hob", "washer", "shower_screen", "basin", "x_frame", "corner_wardrobe")
LIBRARY_BLOCKS = {"electric_range": "E19-081-02", "washer": "E19-058-03", "basin": "E19-016-02"}
STROKE = {"layer": "0", "lineweight": 13}


def positive(value):
    number = float(value)
    if not isfinite(number) or number <= 0:
        raise ValueError("实物尺寸必须为有限正数")
    return number


@lru_cache(maxsize=2)
def read_library(file):
    if file.suffix.lower() == ".dxf":
        return ezdxf.readfile(file)
    if file.suffix.lower() != ".dwg":
        raise ValueError("通用设备库须为 DWG 或 DXF")
    with TemporaryDirectory(prefix="fixture-library-") as folder:
        converted = Path(folder) / "symbols.dxf"
        subprocess.run(["dwg2dxf", "-o", str(converted), str(file)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=120)
        return ezdxf.readfile(converted)


@lru_cache(maxsize=16)
def library_paths(file, code):
    blocks = [b for b in read_library(file).blocks if b.name.startswith(code + "_")]
    if len(blocks) != 1:
        raise ValueError(f"通用 E19 图库中应有一个 {code} 俯视块，找到 {len(blocks)} 个")
    paths = []
    for entity in blocks[0]:
        path = make_path(entity)
        points = [(p.x, p.y) for p in path.flattening(0.35)]
        if len(points) >= 2:
            paths.append(points)
    xs = [x for points in paths for x, y in points]
    ys = [y for points in paths for x, y in points]
    left, right, front, rear = min(xs), max(xs), min(ys), max(ys)
    return [[((x - left) / (right - left) - .5, (y - rear) / (rear - front))
             for x, y in points] for points in paths]


def outline(block, points, closed=False):
    if points[0] == points[-1]:
        points = points[:-1]
        closed = True
    block.add_lwpolyline(points, close=closed, dxfattribs=STROKE)


def draw_simple(block, key, w, d, burners, corner, washer_loading):
    left, right = -w / 2, w / 2
    if key == "corner_wardrobe":
        back_depth, return_depth, side = corner
        path = [(left, 0), (right, 0), (right, -back_depth),
                (left + return_depth, -back_depth), (left + return_depth, -d), (left, -d)]
        if side == "right":
            path = [(-x, y) for x, y in path]
        outline(block, path, True)
        return
    outline(block, [(left, 0), (right, 0), (right, -d), (left, -d)], True)
    if key == "x_frame":
        for a, b in [((left, 0), (right, -d)), ((right, 0), (left, -d))]:
            block.add_line(a, b, dxfattribs=STROKE)
    elif key == "hob":
        rows = [.5] if burners == 2 else [.30, .70]
        radius = min(.16 * w, (.30 if burners == 2 else .16) * d)
        for x in [-.25 * w, .25 * w]:
            for y in rows:
                block.add_circle((x, -y * d), radius, dxfattribs=STROKE)
                block.add_circle((x, -y * d), radius * .45, dxfattribs=STROKE)
    elif key == "washer" and washer_loading == "top":
        outline(block, [(-.39*w, -.20*d), (.39*w, -.20*d), (.39*w, -.88*d), (-.39*w, -.88*d)], True)
        outline(block, [(left, -.13*d), (right, -.13*d)])
    elif key == "washer":
        # Front-load top view: the handle recess is on the front lip.
        outline(block, [(left, -.90*d), (right, -.90*d)])
        outline(block, [(-.09*w, -d), (-.07*w, -.94*d), (.07*w, -.94*d), (.09*w, -d)])
    elif key == "basin":
        block.add_ellipse((0, -.55*d), major_axis=(.43*w, 0),
                          ratio=min(.36*d, .43*w) / (.43*w), dxfattribs=STROKE)
        block.add_circle((0, -.50*d), .035*min(w, d), dxfattribs=STROKE)
        block.add_circle((0, -.095*d), .028*min(w, d), dxfattribs=STROKE)
    # A straight fixed shower pane is represented by its actual length and thickness.


def place_fixture(layout, family, back_mm, facing, width_mm, depth_mm, *,
                  burners=None, back_depth_mm=None, return_depth_mm=None, return_side=None,
                  layer=None, library=None, electric_range=False, washer_loading=None):
    """An explicit library has priority over MPCAD_FIXTURE_LIBRARY; False selects built-in symbols."""
    doc = layout.doc
    if doc.units != 4 or family not in FAMILIES:
        raise ValueError(f"图纸须为毫米；可用名称为 {', '.join(FAMILIES)}")
    w, d = positive(width_mm), positive(depth_mm)
    x, y = map(float, back_mm)
    fx, fy = map(float, facing)
    if not all(map(isfinite, (x, y, fx, fy))) or (fx == 0 and fy == 0):
        raise ValueError("请提供有限的后边中点和非零前向")
    if family == "hob" and burners not in (2, 4):
        raise ValueError("请按图选择 2 或 4 个炉头；其它布局可另外选用匹配的块")
    if family != "hob" and burners is not None:
        raise ValueError("burners 只适用于灶具")
    if type(electric_range) is not bool or (electric_range and (family != "hob" or burners != 4)):
        raise ValueError("electric_range 仅用于已认出四区一体电炉的灶具")
    if washer_loading is not None and (family != "washer" or washer_loading not in ("front", "top")):
        raise ValueError("washer_loading 只适用于洗衣机，可填 front 或 top")
    corner = None
    if family == "corner_wardrobe":
        if back_depth_mm is None or return_depth_mm is None or return_side not in ("left", "right"):
            raise ValueError("转角柜须给 back_depth_mm、return_depth_mm 和 return_side (left/right)")
        b, r = positive(back_depth_mm), positive(return_depth_mm)
        if b >= d or r >= w:
            raise ValueError("两段柜深须分别小于对应的整组外包进深和宽度")
        corner = b, r, return_side
    elif any(v is not None for v in (back_depth_mm, return_depth_mm, return_side)):
        raise ValueError("转角字段只适用于转角衣柜")
    layer = layer or (ATTACHED if family == "x_frame" else FIXED)
    if family == "sink":
        return place_family(layout, "sink_inset", (x, y), (fx, fy),
                            width_mm=w, depth_mm=d, layer=layer)
    code = LIBRARY_BLOCKS.get("electric_range" if electric_range else family)
    if washer_loading == "top":
        code = None
    if library is None:
        library = os.environ.get("MPCAD_FIXTURE_LIBRARY")
    paths = None
    if code and library:
        paths = library_paths(Path(library).resolve(), code)
    source = code if paths else "SCHEM"
    params = json.dumps([family, w, d, burners, corner, source, washer_loading or "front"]).encode()
    tag = hashlib.sha256(params).hexdigest()[:12]
    name = f"MPC_FIX1_{family.upper()}_{source}_{tag}"
    if name not in doc.blocks:
        block = doc.blocks.new(name)
        block.block_record.dxf.units = 4
        if paths:
            for path in paths:
                outline(block, [(nx*w, ny*d) for nx, ny in path])
        else:
            draw_simple(block, family, w, d, burners, corner, washer_loading)
    if layer not in doc.layers:
        doc.layers.new(layer)
    return layout.add_blockref(name, (x, y), dxfattribs={
        "layer": layer, "rotation": degrees(atan2(fx, -fy))})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("items", type=Path, help="原图观察所得的设备放置列表 JSON")
    parser.add_argument("--drawing", type=Path, help="已有毫米制 DXF。省略时新建空图")
    parser.add_argument("--write-new", type=Path, required=True, help="新建输出 DXF")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--library", type=Path, help="通用 E19 的 DWG 或已转换的 DXF")
    source.add_argument("--simple", action="store_true", help="使用内置符号与简化图形")
    args = parser.parse_args()
    try:
        if args.write_new.exists():
            raise FileExistsError(args.write_new)
        if args.library is not None and not args.library.is_file():
            raise FileNotFoundError(args.library)
        doc = ezdxf.readfile(args.drawing) if args.drawing else ezdxf.new(units=4)
        rows = json.loads(args.items.read_text(encoding="utf-8"))
        if not isinstance(rows, list) or not rows:
            raise ValueError("需要非空的设备列表")
        expected = {}
        for row in rows:
            item = dict(row)
            label = item.pop("id")
            if not isinstance(label, str) or not label.strip() or label in expected:
                raise ValueError(f"设备 id 应为唯一非空文字：{label}")
            ref = place_fixture(doc.modelspace(), **item, library=False if args.simple else args.library)
            expected[label] = (ref.dxf.handle, ref.dxf.name, ref.dxf.layer)
        with TemporaryDirectory(dir=args.write_new.parent) as folder:
            staged = Path(folder) / "fixtures.dxf"
            doc.saveas(staged)
            check = ezdxf.readfile(staged)
            for label, (handle, name, layer) in expected.items():
                ent = check.entitydb[handle]
                if ent.dxftype() != "INSERT" or (ent.dxf.name, ent.dxf.layer) != (name, layer):
                    raise ValueError(f"{label} 的块参照复读失败")
                if any(e.dxf.layer != "0" for e in check.blocks[name]):
                    raise ValueError(f"{label} 含有非 0 层线条")
            os.link(staged, args.write_new)
        print(json.dumps(expected, ensure_ascii=False, indent=2))
    except (KeyError, OSError, TypeError, ValueError, ezdxf.DXFError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
