"""Keep source panels distinct and compare two source-only inventories with saved CAD."""
import argparse
from collections import Counter
from itertools import pairwise, permutations
import json
import math
from pathlib import Path

import ezdxf
from ezdxf import bbox
from PIL import Image, ImageDraw, ImageOps
from shapely.geometry import LineString, Point, box
from shapely.ops import unary_union

from check_registered_frames import audit as check_frames
from check_source_objects import check_anchor
from check_wall_tone_stations import run as check_tones
from dimension_grid import GridProjection
from presentation import annotation_layers
from source_geometry_review import digest, section, wall_material

PANEL_SCHEMA = "plan-source-panels/1"
INVENTORY_SCHEMA = "plan-source-inventory/1"
REQUIRED_KINDS = ("room", "wall", "door", "window", "rail", "furniture")
PORTALS = {"door": {"swing", "sliding"}, "window": {"window"}, "rail": {"rail"},
           "opening": {"open"}}
STRUCTURAL_CLASSES = {"bearing", "partition", "other"}
INFERENCE_KINDS = {"annotation", "structural_symbol", "junction", "continuity",
                   "structural_panel_match", "section_geometry"}
STRONG_CUES = {"annotation", "structural_symbol"}
INDEPENDENT_PAIRS = {"junction", "continuity", "structural_panel_match", "section_geometry"}
EXPECTED_FURNITURE_LAYERS = {"movable", "fixed", "ancillary"}
ERRORS = (OSError, ValueError, TypeError, KeyError, IndexError, ezdxf.DXFError)


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                          encoding="utf-8")


def words(value):
    return isinstance(value, str) and bool(value.strip())


def positive(value):
    return type(value) in (float, int) and math.isfinite(value) and value > 0


def nonnegative(value):
    return type(value) in (float, int) and math.isfinite(value) and value >= 0


def require(condition, message):
    if not condition:
        raise ValueError(message)


def object_map(value, name):
    require(isinstance(value, dict), f"{name} 应为 JSON 对象")
    return value


def rectangle(value, enclosing, name):
    require(isinstance(value, list) and len(value) == 4 and all(type(v) is int for v in value),
            f"{name} 需要原图整像素裁片 [左,上,右,下]")
    a, b, c, d = value
    l, t, r, u = enclosing
    require(l <= a < c <= r and t <= b < d <= u, f"{name} 裁片超出所属图版")
    return value


def pixel(value, within, name):
    require(isinstance(value, list) and len(value) == 2 and
            all(type(v) in (int, float) and math.isfinite(v) for v in value),
            f"{name} 需要有限原图像素点")
    x, y = value
    l, t, r, b = within
    require(l <= x < r and t <= y < b, f"{name} 超出所属图版或裁片")
    return value


def manifest(path, source_sha, size):
    value = object_map(read(path), "图版清单")
    require(value.get("schema") == PANEL_SCHEMA and value.get("source_sha256") == source_sha,
            "图版格式或原图 SHA-256 不符")
    panels = {}
    rows = value.get("panels")
    require(isinstance(rows,list), "图版清单须给出 panels 数组")
    for row in rows:
        row = object_map(row, "图版")
        ident, role = row["id"], row["role"]
        require(words(ident) and ident not in panels and words(row.get("label")) and
                role in ("target", "reference", "legend"), "图版缺唯一编号、标题或角色")
        rectangle(row["box_px"], [0, 0, *size], ident)
        panels[ident] = row
    target_ids = [k for k, v in panels.items() if v["role"] == "target"]
    require(len(target_ids) == 1, "只指定一个落图目标图版")
    target = target_ids[0]
    for ident, row in panels.items():
        if ident != target and row["role"] == "reference":
            require(box(*row["box_px"]).intersection(box(*panels[target]["box_px"])).area == 0,
                    "目标图版与参考图版不能混用同一像素域")
    legend = {}
    for row in value.get("wall_legend", []):
        row = object_map(row, "墙材图例")
        ident = row["id"]
        require(words(ident) and ident not in legend and row.get("classification") in STRUCTURAL_CLASSES
                and words(row.get("text")), "墙材图例缺唯一编号、原图文字或明确类别")
        panel = panels[row["panel"]]
        require(panel["role"] != "reference", "参考图版的独有图例不能解释目标图版")
        rectangle(row["box_px"], panel["box_px"], ident)
        if panel["role"] == "legend":
            require(row.get("applies_to") == target, "页边图例须说明适用的目标图版")
        legend[ident] = row
    return panels, target, legend


def card(original, bounds, path):
    raw = original.crop(bounds)
    # Enhance a display copy at original sampling, then enlarge without new edge interpolation.
    gray = ImageOps.autocontrast(raw.convert("L"), cutoff=1).convert("RGB")
    zoom = min(5, 600 / max(raw.size))
    size = tuple(max(1, round(v * zoom)) for v in raw.size)
    w, h = size
    plate = Image.new("RGB", (2*w + 12, h + 25), "white")
    plate.paste(raw.resize(size, Image.Resampling.NEAREST), (0, 25))
    plate.paste(gray.resize(size, Image.Resampling.NEAREST), (w+12, 25))
    draw = ImageDraw.Draw(plate)
    draw.text((2, 4), "SOURCE (unchanged RGB)", fill="black")
    draw.text((w+14, 4), "ENHANCED (reading only)", fill="black")
    plate.save(path)


def input_cards(original, panels, target, legend, output):
    def make(owner, role, bounds):
        name = f"panel-{len(views)+1:03}.png"
        card(original, bounds, output/name)
        views.append({"panel": owner, "role": role, "box_px": bounds, "file": name})
    views = []
    for key, panel in panels.items():
        make(key, panel["role"], panel["box_px"])
    l, t, r, b = panels[target]["box_px"]
    if min(r-l, b-t) >= 64:
        x, y = (l+r)//2, (t+b)//2
        pad_x, pad_y = max(4, (r-l)//12), max(4, (b-t)//12)
        for a, c in ((l,x+pad_x), (x-pad_x,r)):
            for u, v in ((t,y+pad_y), (y-pad_y,b)):
                make(target, "target_detail", [a,u,c,v])
    for row in legend.values():
        make(row["panel"], "wall_legend", row["box_px"])
    return views


def prepare(image, panels_file, output):
    """Generate the source-only crop set before either reader gets the CAD."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    if not panels_file:
        report = {"status": "review", "pending": ["缺图版清单"]}
    else:
        with Image.open(image) as raw:
            original = ImageOps.exif_transpose(raw).convert("RGB")
        source_sha = digest(image)
        panels, target, legend = manifest(panels_file, source_sha, original.size)
        report = dict(status="pass", source_sha256=source_sha, panels_sha256=digest(panels_file),
                      image_size_px=list(original.size), target_panel=target,
                      panels=list(panels.values()), wall_legend=list(legend.values()),
                      views=input_cards(original, panels, target, legend, output))
    write(output/"source-panel-evidence.json", report)
    return report


def inventory(path, source_sha, panels, target):
    result = object_map(read(path), "原图清单")
    require(result.get("schema") == INVENTORY_SCHEMA and result.get("source_sha256") == source_sha
            and words(result.get("reader")) and words(result.get("session_id"))
            and result.get("source_only") is True, "清单须绑定原图和独立读图会话，并声明仅凭原图清点")
    items = {}
    source_items = result.get("items")
    require(isinstance(source_items,list), "原图清单须给出 items 数组")
    for row in source_items:
        row = object_map(row, "原图清单项")
        ident, kind = row["id"], row["kind"]
        require(words(ident) and ident not in items and kind in (*REQUIRED_KINDS, "opening")
                and words(row.get("subtype")) and words(row.get("evidence")),
                "清单项须有唯一编号、类别、细类及读图依据")
        panel = panels[row["panel"]]
        bounds = rectangle(row["box_px"], panel["box_px"], ident)
        points = row["points_px"]
        require(isinstance(points, list) and len(points) >= (1 if kind in ("room", "furniture") else 2)
                and (kind not in ("room", "furniture") or len(points) == 1),
                f"{ident} 须登记一个实物锚点，或一条有两个以上点的边")
        for p in points:
            pixel(p, bounds, ident)
        if len(points) > 1:
            require(all(a != b for a, b in pairwise(points)), f"{ident} 线段不能退化")
        if "uncertainty_px" in row:
            require(positive(row["uncertainty_px"]), f"{ident} 读差须为正值")
        if kind == "room":
            require(words(row.get("label")), f"{ident} 缺房间名称")
        if kind == "wall" and "section_px" in row:
            p, q = row["section_px"]
            pixel(p,bounds,ident)
            pixel(q,bounds,ident)
            require((p[0]==q[0]) != (p[1]==q[1]) and
                    LineString([p,q]).crosses(LineString(points)),
                    f"{ident} 须取横过墙芯线的正交墙面截线")
        if kind == "furniture":
            require(row.get("role") in EXPECTED_FURNITURE_LAYERS or row.get("role") == "unknown",
                    f"{ident} 须确认家具用途层或保留 unknown")
        if kind in PORTALS:
            require(row["subtype"] in PORTALS[kind] or row["subtype"] == "unknown",
                    f"{ident} 图符细类与类别不一致")
            leaves = row.get("leaves", [])
            require(isinstance(leaves,list) and len(leaves)<=2 and
                    (not leaves or row["subtype"] == "swing"), f"{ident} 门扇须对应平开门")
            require(kind != "door" or len(points)==2, f"{ident} 平开或移门须登记一对实洞端")
            for leaf in leaves:
                pixel(leaf["hinge"], bounds, ident)
                pixel(leaf["tip"], bounds, ident)
                require(leaf["hinge"]!=leaf["tip"], f"{ident} 门轴与开启扇端不能重合")
            if len(leaves)==2:
                require(leaves[0]["hinge"]!=leaves[1]["hinge"], f"{ident} 双扇的门轴须分别登记")
        items[ident] = row
    rows = [r for r in items.values() if r["panel"] == target]
    coverage = Counter(r["kind"] for r in rows)
    absent = object_map(result.get("absent", {}), "明确未见的类别")
    for kind, reason in absent.items():
        require(kind in (*REQUIRED_KINDS, "opening") and words(reason) and not coverage[kind],
                f"{kind} 的缺无声明与清单内容冲突")
    result.update(target_items=rows, reference_items=[r for r in items.values() if r["panel"] != target],
                  missing_categories=[k for k in REQUIRED_KINDS if not coverage[k] and k not in absent])
    return result


def shape(row):
    pts = row["points_px"]
    return Point(pts[0]) if len(pts) == 1 else LineString(pts)


def best_pairs(first, second, radius):
    """Match by geometry, reserving tied cases for the readers."""
    left = [shape(r) for r in first]
    right = [shape(r) for r in second]
    candidates = {}
    for i, a in enumerate(first):
        for j, b in enumerate(second):
            mismatch = a["kind"] != b["kind"]
            if mismatch and (a["kind"] not in PORTALS or b["kind"] not in PORTALS):
                continue
            dist = left[i].hausdorff_distance(right[j])
            if dist <= radius:
                candidates[i, j] = (int(mismatch), int(a["subtype"] != b["subtype"]), dist)

    def preferences(index):
        lookup = {}
        for keys, rank in candidates.items():
            lookup.setdefault(keys[index], []).append((rank, keys[1-index]))
        for key, values in lookup.items():
            values.sort()
            if len(values) == 1 or values[0][0][:2] != values[1][0][:2] or (
                    values[1][0][2]-values[0][0][2] > .25):
                yield key, values[0][1]

    left_best, right_best = dict(preferences(0)), dict(preferences(1))
    pairs = [(i, j, candidates[i, j][2]) for i, j in left_best.items() if right_best.get(j) == i]
    open_left = set(range(len(first))) - {i for i, _, _ in pairs}
    open_right = set(range(len(second))) - {j for _, j, _ in pairs}
    ambiguous_left = {i for i, j in candidates if i in open_left and j in open_right}
    ambiguous_right = {j for i, j in candidates if i in open_left and j in open_right}
    return pairs, open_left, open_right, ambiguous_left, ambiguous_right


def wall_nature(row, panels, legend):
    claim = object_map(row.get("wall_nature", {}), row["id"] + " 墙性依据")
    basis, classification = claim.get("basis"), claim.get("classification")
    if basis in (None, "tone_only", "unknown"):
        return {"status": "review", "basis": "tone_only_or_unknown"}
    require(classification in STRUCTURAL_CLASSES, f"{row['id']} 缺墙性候选类别")
    if basis == "legend":
        entry = legend.get(claim.get("legend_id"))
        if not entry:
            return {"status": "review", "basis": "legend_not_located"}
        if entry["classification"] != classification:
            return {"status": "different", "basis": "legend_conflicts", "legend": entry}
        return {"status": "pass", "basis": "printed_legend", "classification": classification, "legend": entry}
    require(basis == "inference", f"{row['id']} 无效的墙性依据")
    if legend:
        return {"status": "review", "basis": "use_applicable_wall_legend"}
    cues = {}
    for cue in claim.get("cues", []):
        cue = object_map(cue, row["id"]+" 墙性图证")
        key = cue["kind"]
        if key not in INFERENCE_KINDS or not words(cue.get("evidence")):
            continue
        if key == "annotation" and not words(cue.get("text")):
            continue
        panel = panels[cue["panel"]]
        where = rectangle(cue["box_px"], panel["box_px"], row["id"])
        require(key != "structural_panel_match" or panel["role"] == "reference",
                f"{row['id']} 对照结构版依据须定位到参考图版")
        cues[key] = (cue["panel"], tuple(where))
    if set(cues) & STRONG_CUES or (len(set(cues) & INDEPENDENT_PAIRS) >= 2 and
                                  len(set(cues.values())) >= 2 and
                                  set(cues) & {"junction", "structural_panel_match"}):
        return {"status": "pass", "basis": "source_inference", "classification": classification,
                "cue_kinds": sorted(cues)}
    return {"status": "review", "basis": "insufficient_non_tonal_evidence", "cue_kinds": sorted(cues)}


def compare(first, second, radius, add):
    left, right = first["target_items"], second["target_items"]
    pairs, missing, extras, ambiguous_left, ambiguous_right = best_pairs(left, right, radius)
    results = []
    for i, j, dist in pairs:
        a, b = left[i], right[j]
        ident = f"{a['id']} ↔ {b['id']}"
        flags = []
        if a["kind"] != b["kind"] or a["subtype"] != b["subtype"]:
            if "unknown" in (a["subtype"], b["subtype"]):
                add("review", f"check/{ident}/类别", "至少一份原图清单保留细类待核")
            else:
                flags.append("kind_or_subtype")
        tolerances = [r.get("uncertainty_px") for r in (a, b)]
        if all(tolerances):
            if dist > sum(tolerances):
                comparable = {"room":{"clear_area_centroid","label_center"},
                              "furniture":{"footprint_center","back_edge_center","front_edge_center"}}
                if (a["kind"]==b["kind"] and a["kind"] in comparable and
                        (a.get("anchor_kind")!=b.get("anchor_kind") or
                         a.get("anchor_kind") not in comparable[a["kind"]])):
                    add("review",f"check/{ident}/锚点","两个内部点缺少相同的定位定义",
                        {"distance_px":round(dist,3),"anchors":[a.get("anchor_kind"),b.get("anchor_kind")]})
                else:
                    flags.append("position")
        else:
            add("review", f"check/{ident}/读差", "原图锚点或端点缺少局部读差")
        if a["kind"] == b["kind"] == "furniture" and a["role"] != b["role"]:
            flags.append("furniture_role")
        if a["kind"] == b["kind"] == "door" and a["subtype"] == b["subtype"] == "swing":
            leaves = [r.get("leaves", []) for r in (a, b)]
            if not all(leaves):
                add("review", f"check/{ident}/门扇", "至少一份清单缺门扇实证")
            elif len(leaves[0]) != len(leaves[1]):
                flags.append("leaf_count")
            elif all(tolerances) and not any(
                    all(max(math.dist(q[p], leaf[p]) for p in ("hinge", "tip")) <= sum(tolerances)
                        for leaf,q in zip(leaves[0], ordering))
                    for ordering in permutations(leaves[1])):
                flags.append("hinge_or_open_tip")
        if flags:
            add("different", f"check/{ident}", "两份原图清单不一致", flags)
        results.append(dict(author=a["id"], independent=b["id"], distance_px=round(dist, 3),
                            issues=flags))
    unmatched = []
    for key, indices, ambiguities, rows in (
            ("only_author", missing, ambiguous_left, left),
            ("only_independent", extras, ambiguous_right, right)):
        for i in sorted(indices):
            row = rows[i]
            uncertain = i in ambiguities or row["subtype"] == "unknown"
            add("review", f"check/{key}/{row['id']}", "原图清单缺项或对应未唯一",
                {"location": row["box_px"], "ambiguous_candidate": uncertain})
            unmatched.append(dict(side=key, id=row["id"], box_px=row["box_px"], ambiguous=uncertain))
    return dict(pairs=results, unmatched=unmatched)


def read_capture(native, hashes):
    native = Path(native)
    capture = read(native/"capture.json")
    path = native/"readback.dxf"
    hashes.update(dwg_sha256=digest(native/"plan.dwg"), readback_dxf_sha256=digest(path))
    require(capture["files"].get("plan.dwg") == hashes["dwg_sha256"]
            and capture["files"].get("readback.dxf") == hashes["readback_dxf_sha256"],
            "DWG 或原生回读与采集指纹不符")
    with (native/"plan.dwg").open("rb") as drawing:
        header = drawing.read(6)
    require(header.startswith(b"AC10") and header[4:].isdigit(), "冻结文件缺原生 DWG 文件头")
    doc = ezdxf.readfile(path)
    require(doc.units == 4, "原生模型须以毫米为单位")
    return path, doc


def local_tolerance(projection, points, radius):
    return max(math.dist(projection.world(p), projection.world((p[0]+dx, p[1]+dy)))
               for p in points for dx, dy in ((radius,0),(-radius,0),(0,radius),(0,-radius)))


def is_visible(doc, entity):
    layer = doc.layers.get(entity.dxf.layer)
    return not entity.dxf.get("invisible", 0) and not layer.is_off() and not layer.is_frozen()


def actual_walls(doc, registration):
    layer_names = {r.casefold() for r in (registration["layers"]["bearing"], registration["layers"]["partition"])}
    visible = {e.dxf.layer for e in doc.modelspace()
               if e.dxf.layer.casefold() in layer_names and is_visible(doc, e)
               and e.dxftype() in ("HATCH", "SOLID", "TRACE", "LWPOLYLINE", "POLYLINE")}
    return wall_material(doc, sorted(visible), [1,0,0,1,0,0])


def wall_witness(doc, row, projection, material, contours, tolerance, add):
    ident = f"native/wall/{row['id']}"
    line = LineString(projection.world_path(row["points_px"]))
    found = [r for r in contours if line.intersection(r["shape"].buffer(tolerance)).length > 0]
    uncovered = line.difference(material.buffer(tolerance)).length
    record = dict(id=row["id"], length_mm=line.length, uncovered_axis_mm=uncovered,
                  handles=sorted({h for r in found for h in r["handles"]}))
    if uncovered > .01 or not found:
        add("different", ident, "原生实墙未覆盖此原图连续墙段", record)
    if "section_px" in row:
        p, q = row["section_px"]
        axis = int(p[0] == q[0])
        b = [projection.world(r) for r in (p, q)]
        low, high = sorted(v[axis] for v in b)
        probe = dict(id=ident, kind="wall_section", axis="xy"[axis], at_mm=b[0][1-axis],
                     search_mm=[low-tolerance, high+tolerance], expected_faces_mm=[low, high],
                     image_uncertainty_mm=tolerance)
        result = section(probe, doc, contours, material, [1,0,0,1,0,0])
        record["section"] = result
        if result["failures"]:
            add("different", ident+"/截面", "原图墙面与原生实体墙面超出读差", result["failures"])
        if result["pending"]:
            add("review", ident+"/截面", "原生墙截面缺唯一有效边界", result["pending"])
    else:
        add("review", ident+"/截面", "缺原图两侧墙面共截线证据")
    return record


def inspect_native(doc, registration, rows, add):
    projection = GridProjection(registration)
    roles = registration["layers"]
    contours, material, decode_issues = actual_walls(doc, registration)
    if decode_issues or not contours:
        add("review", "native/墙材解码", "缺可完整解码的实际墙实体", decode_issues)
    model = doc.modelspace()
    room_layer = annotation_layers(roles)["rooms"].casefold()
    room_labels = [e for e in model.query("TEXT") if e.dxf.layer.casefold() == room_layer and is_visible(doc,e)]
    furniture_layers = {roles[k].casefold(): k for k in EXPECTED_FURNITURE_LAYERS}
    furniture = []
    for e in model.query("INSERT"):
        if e.dxf.layer.casefold() in furniture_layers and is_visible(doc, e) and e.mcount == 1:
            limits = bbox.extents([e])
            if limits.has_data:
                geometry = box(limits.extmin.x, limits.extmin.y, limits.extmax.x, limits.extmax.y)
                furniture.append((e, geometry))
    used_objects, used_rooms = set(), set()
    portals, observed, records = [], [], []
    for row in rows:
        key, kind = row["id"], row["kind"]
        px = row.get("uncertainty_px")
        if not px or row["subtype"] == "unknown":
            add("review", f"native/{kind}/{key}", "缺可判定的原图细类或局部读差")
            continue
        points = row["points_px"]
        tol = local_tolerance(projection, points, px)
        if kind == "wall":
            if not decode_issues and contours:
                records.append(wall_witness(doc, row, projection, material, contours, tol, add))
        elif kind == "room":
            ident = f"native/room/{key}"
            raw_box = box(*row["box_px"]).buffer(px)
            matches = []
            for e in room_labels:
                _, insert, alignment_point = e.get_placement()
                point = alignment_point if alignment_point is not None else insert
                if (e.dxf.text.strip() == row["label"].strip() and
                        raw_box.covers(Point(projection.source((point.x,point.y))))):
                    matches.append(e.dxf.handle)
            if len(matches) != 1 or matches[0] in used_rooms:
                add("review", ident, "未在房间范围内对应唯一的原生房名", matches)
            else:
                used_rooms.add(matches[0])
            anchor = Point(projection.world(points[0]))
            if not decode_issues and material.covers(anchor):
                state = "different" if material.buffer(-tol).covers(anchor) else "review"
                add(state, ident, "原图房间通行点落入原生墙材或墙边读差")
            records.append(dict(id=key, kind=kind, label_handles=matches))
        elif kind == "furniture":
            ident = f"native/furniture/{key}"
            role = row["role"]
            if role not in EXPECTED_FURNITURE_LAYERS:
                add("review", ident, "家具安装关系待核")
                continue
            anchor = Point(projection.world(points[0]))
            found = []
            for e, envelope in furniture:
                if envelope.distance(anchor) > tol or e.dxf.layer.casefold() != roles[role].casefold():
                    continue
                candidate = {"anchor_px":points[0], "issues":[]}
                check_anchor(e, candidate, projection)
                proof = candidate.get("anchor_check")
                if proof and proof["distance_mm"] <= max(.25,tol):
                    found.append((e, proof))
            handles = [e.dxf.handle for e, _ in found]
            if len(found) != 1 or handles[0] in used_objects:
                add("review", ident, "原图区位未唯一对应实物块和用途层", handles)
            else:
                used_objects.add(handles[0])
                e, proof = found[0]
                observed.append(dict(id=key, handle=handles[0], block=e.dxf.name, role=role, **proof))
                if proof["method"] == "stroke_hull":
                    add("review", ident+"/占地", "仅从块内原生线包络定位，实物外轮廓待核")
        else:
            if row.get("shape") == "curved":
                add("review", f"native/{kind}/{key}", "曲框保留原生局部实量")
                continue
            if kind == "door" and row["subtype"] == "swing" and not row.get("leaves"):
                add("review", f"native/{kind}/{key}/门扇", "原图铰点及开启端待核")
            for i, (a,b) in enumerate(pairwise(points)):
                ident = key if len(points) == 2 else f"{key}/{i+1}"
                data = dict(id=ident, kind=row["subtype"], a=a, b=b, uncertainty_px=px)
                for k in ("wall_thickness_mm", "end_support", "leaves", "symbol_legible", "min_panels", "min_tracks"):
                    if k in row:
                        data[k] = row[k]
                portals.append(data)
    frame_report = None
    if portals:
        frame_report = check_frames(doc, registration, dict(source_sha256=registration["source_sha256"], portals=portals))
        expected = {row["id"]:row for row in portals}
        for row in frame_report["portals"]:
            ident = f"native/frame/{row['id']}"
            if row["failures"]:
                add("different", ident, "原生框、洞或门扇与原图登记不一致", row["failures"])
            if row["pending"]:
                add("review", ident, "原生框、洞或门扇尚待核", row["pending"])
            leaves = expected[row["id"]].get("leaves", [])
            if leaves and "by_layer" in row:
                found = len(row["by_layer"][roles["door"]]["swings"])
                if found != len(leaves):
                    add("different" if found else "review", ident+"/扇数",
                        "原图清点与原生已辨门扇数量不同", {"expected":len(leaves),"actual":found})
    return dict(walls_and_rooms=records, furniture=observed, frames=frame_report,
                unmatched_room_label_handles=sorted({e.dxf.handle for e in room_labels}-used_rooms),
                unmatched_furniture_handles=sorted({e.dxf.handle for e,_ in furniture
                    if furniture_layers[e.dxf.layer.casefold()] in ("fixed","movable")}-used_objects))


def report_axes_in_panel(result, target_box):
    require(isinstance(result.get("axes"), list) and result["axes"], "墙色报告缺原图轴测区")
    for row in result["axes"]:
        require(row["orientation"] in ("H","V"), "墙色报告轴向无效")
        runs = row.get("runs")
        require(isinstance(runs,list) and runs, "墙色报告缺稳定区或待核区")
        for part in runs:
            for t in part["stations_px"]:
                n = row["normal_px"]
                pixel([t,n] if row["orientation"]=="H" else [n,t], target_box, "墙色轴")


def guides_in_panel(path, target_box):
    guides = object_map(read(path), "墙色导线")
    # The existing scanner's plan_box_px is inclusive; panel crop bounds are half open.
    if "plan_box_px" in guides:
        l,t,r,b = guides["plan_box_px"]
        rectangle([l,t,r+1,b+1], target_box, "墙色取样范围")
    else:
        require("observations" in guides["axes"][0], "墙色导线须明确主图取样范围")
        report_axes_in_panel({**guides, "axes":[{**a, "normal_px":a["normal"], "runs":a["observations"]}
                                               for a in guides["axes"]]}, target_box)
        return
    for sample in guides["samples"]:
        for x,y in sample["points_px"]:
            pad = sample.get("radius_px",1)
            pixel([x-pad,y-pad], target_box, "墙芯样带")
            pixel([x+pad,y+pad], target_box, "墙芯样带")
    for axis in guides["axes"]:
        pixel(axis["a_px"], target_box, "墙色母轴")
        pixel(axis["b_px"], target_box, "墙色母轴")


def tone_report(image, guides, existing, dxf, registration_file, hashes, target_box, out, band, normal):
    expected = {"image": hashes["source_sha256"], "saved_dxf": hashes["readback_dxf_sha256"],
                "registration": hashes["registration_sha256"]}
    require(guides is not None, "缺墙芯原色导线，不能绑定墙色测区")
    guides_in_panel(guides, target_box)
    expected["reading"] = digest(guides)
    require(nonnegative(band), "墙芯原色核验需给本图沿轴读差")
    normal = band if normal is None else normal
    require(nonnegative(normal), "墙芯原色核验需给本图法向读差")
    if existing is None:
        tag = "-".join([v[:12] for v in expected.values()]+[str(band),str(normal)])
        directory = out/("wall-colours-"+tag)
        existing = directory/"wall-tone-stations.json"
        if not existing.is_file():
            check_tones(Path(image), Path(guides), dxf, Path(registration_file), directory, band, normal)
    result = read(existing)
    require(all(result["sha256"].get(k) == h for k,h in expected.items()),
            "墙色报告与原图、回读、配准或导线证据非同版")
    require(result.get("source_read_band_px") == band and result.get("normal_read_band_px") == normal,
            "墙色报告的读取允许带不同")
    report_axes_in_panel(result, target_box)
    require(result.get("status") in ("within_band","different","review"), "墙色报告缺有效验收状态")
    return {"file":str(existing), "sha256":digest(existing), "status":result["status"]}, result["axes"]


def check_tone_coverage(rows, axes, add):
    """A clean report from one sampled wall cannot clear unobserved walls."""
    observed = []
    for axis in axes:
        n = axis["normal_px"]
        for run in axis["runs"]:
            if run["label"] not in ("bearing","partition") or not run.get("sufficient_tone_evidence"):
                continue
            a,b = run["span_px"]
            observed.append(LineString([(a,n),(b,n)] if axis["orientation"]=="H" else [(n,a),(n,b)]))
    stripes = unary_union(observed)
    for row in rows:
        if row["kind"] == "wall":
            distance = row.get("uncertainty_px",0)
            if stripes.is_empty or not stripes.buffer(distance).covers(shape(row)):
                add("review", "native/tone-scope/"+row["id"], "墙芯原色核验未覆盖本段完整原图墙轴")


def run(image, panels_file, author_file, independent_file, registration_file, native_dir, output, *,
        ledger_file=None, wall_guides=None, read_band_px=None, normal_band_px=None, wall_tone_report=None):
    """Audit available evidence and preserve the distinction between gaps and contradictions."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    report = dict(schema="plan-source-evidence/1", status="review", failures=[], pending=[],
                  issues=[], hashes=dict.fromkeys(("source_sha256","panels_sha256","author_sha256",
                      "independent_sha256","dwg_sha256","readback_dxf_sha256","registration_sha256")))

    def add(state, ident, message, details=None):
        report["issues"].append(dict(id=ident,status=state,message=message,details=details))
        if state in ("different","review"):
            ids = report["failures" if state=="different" else "pending"]
            if ident not in ids:
                ids.append(ident)

    hashes = report["hashes"]
    panels = target = legend = original = registration = dxf = doc = None
    for key, path in (("source",image),("panels",panels_file),("author",author_file),
                      ("independent",independent_file),("registration",registration_file)):
        if path:
            try:
                hashes[key+"_sha256"] = digest(path)
            except OSError as exc:
                add("review", "input/"+key, "证据文件不可读", str(exc))
        else:
            add("review", "input/"+key, "缺少证据文件")
    if hashes["panels_sha256"] and hashes["source_sha256"]:
        try:
            with Image.open(image) as raw:
                original = ImageOps.exif_transpose(raw).convert("RGB")
            panels, target, legend = manifest(panels_file, hashes["source_sha256"], original.size)
            report["target_panel"] = target
            report["panels"] = list(panels.values())
            report["input_views"] = input_cards(original, panels, target, legend, output)
        except ERRORS as exc:
            add("review", "input/panels", "无法确认原图图版与测区", str(exc))
    if native_dir:
        try:
            dxf, doc = read_capture(native_dir, hashes)
        except ERRORS as exc:
            add("review", "input/native", "缺有效的毫米制 DWG 原生回读", str(exc))
    else:
        add("review", "input/native", "缺原生回读目录")

    inventories = {}
    if panels:
        report["wall_nature"] = {}
        report["inventory"] = {}
        report["item_views"] = []
        for key,path in (("author",author_file),("independent",independent_file)):
            if not hashes[key+"_sha256"]:
                continue
            try:
                reading = inventory(path, hashes["source_sha256"], panels, target)
                inventories[key] = reading
            except ERRORS as exc:
                add("review", "input/"+key, "原图清单不可用于比对", str(exc))
                continue
            rows = reading["target_items"]
            report["inventory"][key] = {"reader":reading["reader"],"session_id":reading["session_id"],
                "target_counts":dict(Counter(r["kind"] for r in rows)), "absent":reading.get("absent",{}),
                "reference_items":[r["id"] for r in reading["reference_items"]]}
            if not rows:
                add("review", f"inventory/{key}/target", "缺目标图版原图项")
            for kind in reading["missing_categories"]:
                add("review", f"inventory/{key}/{kind}", "目标图版缺清点或有证据的未见声明")
            for row in (*rows,*reading["reference_items"]):
                if row["kind"]=="wall" and row["panel"]==target:
                    try:
                        result = wall_nature(row,panels,legend)
                    except ERRORS as exc:
                        result = {"status":"review","basis":str(exc)}
                    report["wall_nature"][f"{key}/{row['id']}"] = result
                    if result["status"] != "pass":
                        add(result["status"], f"nature/{key}/{row['id']}", "墙性依据待核或与图例冲突",result)
                # One card per source location. Reference items only receive cards, not CAD probes.
                views = report["item_views"]
                name = f"item-{len(views)+1:03}.png"
                card(original,row["box_px"],output/name)
                views.append(dict(reader=key, id=row["id"], panel=row["panel"],
                                  box_px=row["box_px"], file=name))
    first, second = inventories.get("author"), inventories.get("independent")
    if first and second:
        if (hashes["author_sha256"]==hashes["independent_sha256"] or
                first["session_id"]==second["session_id"] or first["items"]==second["items"]):
            add("review", "input/independence", "同一清单、同一会话或复制的观察记录不能形成独立复核")
        radius = target and panels[target].get("match_radius_px",12)
        if not positive(radius):
            add("review", "input/match_radius", "配对搜索距离无效")
        else:
            report["comparison"] = compare(first,second,radius,add)
            for pair in report["comparison"]["pairs"]:
                if pair["issues"]:
                    continue
                left = report["wall_nature"].get("author/"+pair["author"])
                right = report["wall_nature"].get("independent/"+pair["independent"])
                if left and right and left.get("classification") != right.get("classification"):
                    if left["status"] == right["status"] == "pass":
                        add("different", f"nature/{pair['author']} ↔ {pair['independent']}",
                            "原图两位读者推定的墙性不一致", {"author":left,"independent":right})

    if hashes["registration_sha256"] and hashes["source_sha256"] and target:
        try:
            current = object_map(read(registration_file), "分段配准")
            require(current.get("source_sha256")==hashes["source_sha256"], "配准与原图 SHA-256 不符")
            require(current.get("source_panel",target)==target, "现版配准未指向目标图版")
            if ledger_file:
                ledger = object_map(read(ledger_file), "成品台账")
                require(ledger.get("source_sha256",hashes["source_sha256"])==hashes["source_sha256"],
                        "成品台账与原图 SHA-256 不符")
                if ledger.get("wall_layers"):
                    current = {**current, "wall_layers":ledger["wall_layers"]}
                hashes["ledger_sha256"] = digest(ledger_file)
            GridProjection(current)
            registration = current
        except ERRORS as exc:
            add("review", "input/registration", "当前原图到原生模型的配准无效",str(exc))
    reading = second or first
    if reading and doc is not None and registration is not None:
        try:
            report["native_inventory_reader"] = "independent" if second else "author"
            native = inspect_native(doc,registration,reading["target_items"],add)
            report["native"] = native
            for field, name in (("unmatched_room_label_handles","房名"),("unmatched_furniture_handles","家具")):
                for handle in native[field]:
                    add("review", f"native/extra/{handle}", f"原生{ name }尚未对应原图清单")
        except ERRORS as exc:
            add("review", "native/compare", "原生几何对照不能完整执行",str(exc))
    if dxf is not None and registration is not None and target:
        try:
            report["wall_tones"], axes = tone_report(image,wall_guides,wall_tone_report,dxf,registration_file,
                hashes,panels[target]["box_px"],output,read_band_px,normal_band_px)
            state = report["wall_tones"]["status"]
            if state in ("different","review"):
                add(state, "native/wall_tones", "保存墙芯有异色、分界偏移或尚待核",
                    report["wall_tones"]["file"])
            if reading:
                check_tone_coverage(reading["target_items"], axes, add)
        except ERRORS as exc:
            add("review", "native/wall_tones", "缺已绑定的墙芯原色分段及换色点核验",str(exc))
    else:
        add("review", "native/wall_tones", "缺目标图版、配准或原生回读")
    report["status"] = "different" if report["failures"] else ("review" if report["pending"] else "pass")
    write(output/"source-evidence.json",report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="task", required=True)
    for task in ("prepare","review"):
        command = sub.add_parser(task)
        command.add_argument("image",type=Path)
        command.add_argument("panels",type=Path)
        command.add_argument("output",type=Path)
        if task=="review":
            for name in ("author","independent","registration","native","ledger","wall-guides","wall-tone-report"):
                command.add_argument("--"+name,type=Path)
            command.add_argument("--read-band-px",type=float)
            command.add_argument("--normal-band-px",type=float)
    args = parser.parse_args()
    if args.task=="prepare":
        report = prepare(args.image,args.panels,args.output)
    else:
        report = run(args.image,args.panels,args.author,args.independent,args.registration,args.native,args.output,
            ledger_file=args.ledger,wall_guides=args.wall_guides,wall_tone_report=args.wall_tone_report,
            read_band_px=args.read_band_px,normal_band_px=args.normal_band_px)
    print(report["status"])
    return {"pass":0,"different":1,"review":2}[report["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
