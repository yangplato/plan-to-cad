#!/usr/bin/env python3
"""从四份原生 CAD 实际关层后读回的 DXF，比较独立选定的小区域。需要 ezdxf。"""
import argparse
import json
import math
import sys
from collections import Counter
from pathlib import Path

import ezdxf
from ezdxf.addons.drawing import Frontend, RenderContext
from ezdxf.addons.drawing.recorder import Recorder

STATES = {"both_on": (False, False), "movable_off": (True, False),
          "fixed_off": (False, True), "both_off": (True, True)}


def parse_spec(data):
    if data["unit"] != "mm":
        raise ValueError("区域须使用模型空间毫米坐标")
    layers = data["layers"]
    if not isinstance(layers, list) or len(layers) != 2 or any(not isinstance(s, str) or not s.strip() for s in layers) or layers[0].casefold() == layers[1].casefold():
        raise ValueError("layers 应为不同的活动层与固定层名")
    regions = {}
    for row in data["regions"]:
        name, cls, rect = row["id"], row["class"], row["rect_mm"]
        if not isinstance(name, str) or not name or name in regions or cls not in ("movable", "fixed", "permanent"):
            raise ValueError("区域需有唯一 id 和可识别的 class")
        if len(rect) != 4 or not all(isinstance(n, (float, int)) and not isinstance(n, bool) and math.isfinite(n) for n in rect):
            raise ValueError(f"{name}: rect_mm 应为四个有限数值")
        if rect[2] <= rect[0] or rect[3] <= rect[1]:
            raise ValueError(f"{name}: 请按 xmin,ymin,xmax,ymax 提供非空矩形")
        regions[name] = cls, rect
    return layers, regions


def sample(path, expected_off, layers, regions):
    doc = ezdxf.readfile(path)
    known = all(layer in doc.layers for layer in layers)
    flags = [doc.layers.get(layer).is_off() or doc.layers.get(layer).is_frozen() for layer in layers] if known else []
    units = doc.header.get("$INSUNITS")
    state_ok = units == 4 and tuple(flags) == expected_off
    ctx = RenderContext(doc)
    recorder = Recorder()
    Frontend(ctx, recorder).draw_layout(doc.modelspace())
    counts = {}
    for name, (_, box) in regions.items():
        crop = recorder.player().copy()
        crop.crop_rect(box[:2], box[2:], distance=1)
        counts[name] = dict(Counter(crop.properties[r.property_hash].layer for r in crop.records))
    return dict(insunits=units, layers_hidden=flags, state_ok=state_ok, regions=counts)


def run():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", type=Path, help="独立抽查区域和图层名 JSON")
    for state in STATES:
        parser.add_argument(state, type=Path, help=state + " 状态 DXF")
    args = parser.parse_args()
    try:
        layers, regions = parse_spec(json.loads(args.spec.read_text(encoding="utf-8")))
        views = {state: sample(getattr(args, state), flags, layers, regions) for state, flags in STATES.items()}
        standard = all(s["state_ok"] for s in views.values())
        report = []
        for name, (cls, _) in regions.items():
            baseline = views["both_on"]["regions"][name]
            issues = []
            for state, off in STATES.items():
                should_hide = cls != "permanent" and off[0 if cls == "movable" else 1]
                observed = views[state]["regions"][name]
                if observed != ({} if should_hide else baseline):
                    issues.append(state)
            if cls in ("movable", "fixed") and baseline:
                owner = layers[0 if cls == "movable" else 1].casefold()
                if any(s.casefold() != owner for s in baseline):
                    issues.append("全开时区域含其他层的图形")
            if cls == "permanent" and any(s.casefold() in {layer.casefold() for layer in layers} for s in baseline):
                issues.append("全开时应保留的区域含家具层图形")
            status = "未验证" if not standard or not baseline else ("不通过" if issues else "通过")
            report.append(dict(id=name, status=status, issues=issues))
        statuses = [r["status"] for r in report]
        status = "不通过" if "不通过" in statuses else ("未验证" if not statuses or "未验证" in statuses else "通过")
        result = dict(status=status, views=views, results=report,
                      scope="只比较四份模型空间读回图指定矩形的可见绘图记录及其图层。区域须避开无关对象。源 DWG 同一性、CAD 开关过程与对象形状另核。")
    except (OSError, UnicodeError, ezdxf.DXFError, KeyError, TypeError, ValueError) as exc:
        print(json.dumps({"status": "输入错误", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return {"通过": 0, "不通过": 1, "未验证": 3}[status]


if __name__ == "__main__":
    sys.exit(run())
