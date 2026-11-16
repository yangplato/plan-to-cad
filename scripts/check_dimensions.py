#!/usr/bin/env python3
"""核对原图独立抄录的线性标注与最终 DWG 读回 DXF。需要 ezdxf。"""
import argparse
import json
import math
import re
import sys
from pathlib import Path

import ezdxf


def number(value, label, minimum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label} 应为有限数值")
    if minimum is not None and value < minimum:
        raise ValueError(f"{label} 须大于或等于 {minimum}")
    return float(value)


def point(value, label):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{label} 应为两个坐标值")
    return tuple(number(x, label) for x in value)


def sub(a, b):
    return a[0] - b[0], a[1] - b[1]


def dot(a, b):
    return a[0] * b[0] + a[1] * b[1]


def unit(v):
    length = math.hypot(*v)
    if length < 1e-10:
        raise ValueError("方向向量长度为零")
    return v[0] / length, v[1] / length


def transform(frame):
    origin = point(frame["origin_mm"], "origin_mm")
    x = point(frame["x_direction"], "x_direction")
    y = point(frame["y_direction"], "y_direction")
    if any(abs(math.hypot(*v) - 1) > 1e-6 for v in (x, y)) or abs(dot(x, y)) > 1e-6:
        raise ValueError("配准只允许单位长度且垂直的两轴；不得缩放模型")
    def apply(p, vector=False):
        off = (0, 0) if vector else origin
        return tuple(off[i] + x[i] * p[0] + y[i] * p[1] for i in range(2))
    return apply


def references(data, frame):
    if data["unit"] != "mm":
        raise ValueError("参考账本单位须为 mm")
    apply = transform(frame)
    rows = []
    ids = set()
    if not isinstance(data["dimensions"], list):
        raise ValueError("dimensions 应为数组")
    for item in data["dimensions"]:
        ident = item["id"]
        if not isinstance(ident, str) or not ident.strip() or ident in ids:
            raise ValueError("每条尺寸需要唯一非空 id")
        ids.add(ident)
        mode = item["mode"]
        if mode not in ("point", "station"):
            raise ValueError(f"{ident}: mode 可用 point 或 station")
        a, b = [apply(point(item[k], k)) for k in ("p1_mm", "p2_mm")]
        axis = unit(apply(point(item["axis"], "axis"), vector=True))
        normal = -axis[1], axis[0]
        length = number(item["value_mm"], "value_mm", 0)
        if length <= 0 or abs(dot(sub(b, a), axis)) < 1e-8:
            raise ValueError(f"{ident}: 尺寸值和沿轴跨度须大于零")
        endpoint_tol = number(item["endpoint_tolerance_mm"], "endpoint_tolerance_mm", 0)
        value_tol = number(item["value_tolerance_mm"], "value_tolerance_mm", 0)
        angle_tol = number(item.get("angle_tolerance_deg", 0.5), "angle_tolerance_deg", 0)
        if angle_tol >= 90:
            raise ValueError(f"{ident}: 方向容差必须小于 90 度")
        baseline = None
        if mode == "station" or "dimension_line_point_mm" in item:
            baseline = apply(point(item["dimension_line_point_mm"], "dimension_line_point_mm"))
            line_tol = number(item["dimension_line_tolerance_mm"], "dimension_line_tolerance_mm", 0)
        else:
            line_tol = 0
        rows.append(dict(id=ident, mode=mode, a=a, b=b, axis=axis, normal=normal, length=length,
                         endpoint_tol=endpoint_tol, value_tol=value_tol, angle_tol=angle_tol,
                         baseline=baseline, line_tol=line_tol))
    return rows


def visible_texts(entity):
    block = entity.get_geometry_block()
    if block is None:
        return []
    return [t.plain_text().strip() for t in block.query("TEXT MTEXT") if not t.dxf.get("invisible", 0)]


def read_entities(doc):
    objects = []
    other = []
    for entity in doc.modelspace().query("DIMENSION"):
        dxf = entity.dxf
        reason = None
        if entity.dimtype not in (0, 1):
            reason = "非线性/对齐标注"
        elif not all(dxf.hasattr(k) for k in ("defpoint2", "defpoint3", "defpoint")):
            reason = "缺少尺寸或尺寸线定义点"
        elif not dxf.extrusion.isclose((0, 0, 1)) or abs(dxf.defpoint2.z - dxf.defpoint3.z) > 1e-5:
            reason = "非水平二维平面或挤出方向不受支持"
        else:
            p1, p2, base = [tuple(getattr(dxf, k))[:2] for k in ("defpoint2", "defpoint3", "defpoint")]
            value = entity.get_measurement()
            angle = math.radians(dxf.get("angle", 0.0))
            direction = (math.cos(angle), math.sin(angle)) if entity.dimtype == 0 else sub(p2, p1)
            if not all(math.isfinite(x) for x in (*p1, *p2, *base, *direction, value)) or math.hypot(*direction) < 1e-10:
                reason = "定义点或量测方向无效"
            else:
                raw = dxf.get("text", "")
                mode = "自动" if raw in ("", "<>") else ("占位符加注" if "<>" in raw else "显式文字")
                objects.append(dict(handle=dxf.handle, p1_mm=p1, p2_mm=p2, line_mm=base,
                                    axis=unit(direction), actual_mm=value, raw_text=raw,
                                    text_mode=mode, block_text=visible_texts(entity)))
        if reason:
            other.append(dict(handle=dxf.handle, dimtype=entity.dimtype, reason=reason))
    return objects, other


def numeric_text(text):
    match = re.fullmatch(r"\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*(?:mm|毫米)?\s*", text, re.I)
    return float(match.group(1)) if match else None


def assess(ref, obj):
    def offsets(first, second):
        diffs = sub(first, ref["a"]), sub(second, ref["b"])
        axial = [abs(dot(v, ref["axis"])) for v in diffs]
        spatial = [math.hypot(*v) for v in diffs]
        return (axial if ref["mode"] == "station" else spatial), axial, spatial
    order = min((offsets(obj["p1_mm"], obj["p2_mm"]), offsets(obj["p2_mm"], obj["p1_mm"])),
                key=lambda parts: (max(parts[0]), sum(parts[0]), sum(parts[2])))
    error, axial, spatial = order
    line_error = None if ref["baseline"] is None else abs(dot(sub(obj["line_mm"], ref["baseline"]), ref["normal"]))
    angle_err = math.degrees(math.acos(min(1.0, abs(dot(obj["axis"], ref["axis"])))))
    endpoint_ok = max(error) <= ref["endpoint_tol"] + 1e-7
    line_ok = line_error is None or line_error <= ref["line_tol"] + 1e-7
    position_ok = endpoint_ok and line_ok and angle_err <= ref["angle_tol"] + 1e-7
    value_error = abs(obj["actual_mm"] - ref["length"])
    value_ok = value_error <= ref["value_tol"] + 1e-7
    issues = []
    if not endpoint_ok:
        issues.append("端点超差")
    if not line_ok:
        issues.append("尺寸线位置超差")
    if angle_err > ref["angle_tol"] + 1e-7:
        issues.append("测量方向超差")
    if not value_ok:
        issues.append("模型实量超差")
    unknown_text = False
    raw = obj["raw_text"]
    printed = numeric_text(raw)
    if raw and raw.isspace():
        issues.append("尺寸文字已隐藏")
    elif printed is not None and abs(printed - ref["length"]) > ref["value_tol"] + 1e-7:
        issues.append("显式文字与原图不符")
    elif raw and "<>" not in raw and printed is None:
        unknown_text = True
    caption = obj["block_text"][0] if len(obj["block_text"]) == 1 else ""
    shown = numeric_text(caption)
    if shown is None and raw.count("<>") == 1:
        prefix, suffix = raw.split("<>")
        if caption.startswith(prefix) and caption.endswith(suffix):
            end = len(caption) - len(suffix)
            shown = numeric_text(caption[len(prefix):end])
    if shown is None:
        unknown_text = True
    elif abs(shown - ref["length"]) > ref["value_tol"] + 1e-7:
        issues.append("显示块数值与原图不符")
    return dict(handle=obj["handle"], position_ok=position_ok, actual_ok=value_ok,
                match_distance_mm=max(error), status="不通过" if issues else ("未验证" if unknown_text else "通过"),
                issues=issues, text_unverified=unknown_text, axial_error_mm=axial, point_error_mm=spatial,
                angle_error_deg=angle_err, dimension_line_error_mm=line_error, actual_error_mm=value_error)


def assign(grid):
    """矩形最小费用指派（匈牙利算法）。先最多配位，再最多通过，最后按偏差择近。"""
    n = len(grid)
    if not n:
        return {}
    real = len(grid[0])
    cols = real + n  # 每行均可认领一个空位，代表没有匹配。
    missing = (n + 2) ** 2
    costs = []
    for row in grid:
        weights = []
        for cell in row:
            if not cell["position_ok"]:
                weights.append(2 * missing)
                continue
            rank = {"通过": 0, "未验证": n + 1, "不通过": n + 2}[cell["status"]]
            dist = cell["match_distance_mm"] + (cell["dimension_line_error_mm"] or 0)
            weights.append(rank + dist / (1 + dist) / (n + 1))
        costs.append(weights + [missing] * n)
    row_potential = [0.] * (n + 1)
    col_potential = [0.] * (cols + 1)
    owner = [0] * (cols + 1)
    for row in range(1, n + 1):
        owner[0] = row
        column = 0
        trail = [0] * (cols + 1)
        cost_left = [math.inf] * (cols + 1)
        seen = set()
        while True:
            seen.add(column)
            i = owner[column]
            for j in range(1, cols + 1):
                if j in seen:
                    continue
                cost = costs[i - 1][j - 1] - row_potential[i] - col_potential[j]
                if cost < cost_left[j]:
                    cost_left[j], trail[j] = cost, column
            next_col = min((j for j in range(1, cols + 1) if j not in seen), key=cost_left.__getitem__)
            delta = cost_left[next_col]
            for j in range(cols + 1):
                if j in seen:
                    row_potential[owner[j]] += delta
                    col_potential[j] -= delta
                else:
                    cost_left[j] -= delta
            column = next_col
            if not owner[column]:
                break
        while column:
            prev = trail[column]
            owner[column] = owner[prev]
            column = prev
    return {i - 1: j - 1 for j, i in enumerate(owner[1:real + 1], 1)
            if i and grid[i - 1][j - 1]["position_ok"]}


def audit(doc, refs, require_dynamic=False):
    objects, unsupported = read_entities(doc)
    grid = [[assess(ref, obj) for obj in objects] for ref in refs]
    chosen = assign(grid)
    results = []
    used = set(chosen.values())
    for i, ref in enumerate(refs):
        rec = dict(id=ref["id"], mode=ref["mode"], expected_mm=ref["length"])
        if i in chosen:
            j = chosen[i]
            rec.update(grid[i][j])
        else:
            rec["status"] = "不通过" if objects else "未验证"
            rec["issues"] = ["无可一对一配对的原生线性标注" if objects else "无支持的原生线性标注"]
            # 供定位，不计入任何覆盖率；优先展示同方向、同值的错位尺寸。
            if objects:
                rec["nearest_only"] = min(grid[i], key=lambda t: (t["angle_error_deg"] > ref["angle_tol"] + 1e-7,
                    t["actual_error_mm"] > ref["value_tol"] + 1e-7, max(t["axial_error_mm"] if ref["mode"] == "station" else t["point_error_mm"]),
                    t["dimension_line_error_mm"] or 0))
        results.append(rec)
    duplicate = [objects[j]["handle"] for j in range(len(objects)) if j not in used and any(row[j]["position_ok"] for row in grid)]
    statuses = [r["status"] for r in results]
    status = "不通过" if "不通过" in statuses or duplicate else ("未验证" if not refs or "未验证" in statuses else "通过")
    units = doc.header.get("$INSUNITS")
    if units != 4:
        status = "未验证"
        for row in results:
            row["status"] = "未验证"
            row["issues"].append("模型单位未声明为毫米，数值仅供排查")
    geometry_status = status
    fixed = [objects[j]["handle"] for j in chosen.values() if objects[j]["text_mode"] == "显式文字"]
    dynamic_count = len(chosen) - len(fixed)
    dynamic_status = "未验证" if units != 4 or not refs or not chosen else (
        "不通过" if fixed else ("未验证" if len(chosen) != len(refs) else "通过"))
    if require_dynamic:
        states = (geometry_status, dynamic_status)
        status = "不通过" if "不通过" in states else ("未验证" if "未验证" in states else "通过")
    dynamic = dict(status=dynamic_status, dynamic_count=dynamic_count, fixed_text_count=len(fixed),
                   fixed_handles=fixed, unmatched_count=len(refs)-len(chosen),
                   basis="仅对一一配对的标注核对 DXF 文字字段。空串及含 <> 的文字随原生测量值更新；未执行交互编辑。")
    return dict(status=status, geometry_and_display_status=geometry_status, require_dynamic=require_dynamic,
                dynamic_display=dynamic, insunits=units, unit_verified=units == 4, expected_count=len(refs),
                supported_native_count=len(objects), matched_count=len(chosen), passed_count=statuses.count("通过") if units == 4 else 0,
                duplicate_or_conflicting_handles=duplicate, unpaired_handles=[o["handle"] for j, o in enumerate(objects) if j not in used],
                unsupported_entities=unsupported, results=results, native_entities=objects,
                scope="仅核对二维模型空间顶层原生线性/对齐标注。point 检查二维定义点；station 检查两端投影及尺寸线。实体关联、建筑构件与原图重合情况另核。")


def run():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dxf", type=Path, help="最终 DWG 读回的 DXF")
    parser.add_argument("reference", type=Path, help="原图独立抄录的毫米 JSON")
    parser.add_argument("--frame", type=Path, help="参考毫米系至 CAD 的刚性配准 JSON；默认同坐标系")
    parser.add_argument("--require-dynamic", action="store_true", help="同时要求每段显示值能跟随原生标注编辑更新")
    args = parser.parse_args()
    def load(path):
        return json.loads(path.read_text(encoding="utf-8"))
    try:
        frame = load(args.frame) if args.frame else dict(origin_mm=[0, 0], x_direction=[1, 0], y_direction=[0, 1])
        refs = references(load(args.reference), frame)
        result = audit(ezdxf.readfile(args.dxf), refs, args.require_dynamic)
    except (OSError, UnicodeError, ezdxf.DXFError, TypeError, KeyError, ValueError, RecursionError) as exc:
        print(json.dumps({"status": "输入错误", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return {"通过": 0, "不通过": 1, "未验证": 3}[result["status"]]


if __name__ == "__main__":
    sys.exit(run())
