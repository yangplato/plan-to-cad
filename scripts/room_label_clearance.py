"""Inspect saved room-name glyph outlines against visible furniture strokes."""
from collections import defaultdict
from math import isfinite
from pathlib import Path

import ezdxf
from ezdxf.addons import text2path
from ezdxf.fonts import fonts
from ezdxf.path import make_path
from shapely.geometry import LineString, MultiLineString

from check_registered_frames import expand
from measure_furniture import PATH_TYPES
from presentation import annotation_layers, resolve_roles

FLATTEN_MM = 0.05
READ_ERRORS = (OSError, TypeError, ValueError, KeyError, ArithmeticError, ezdxf.DXFError,
               fonts.FontNotFoundError, fonts.UnsupportedFont)


def visible(doc, entity):
    layer = doc.layers.get(entity.dxf.layer) if entity.dxf.layer in doc.layers else None
    return not entity.dxf.get("invisible", 0) and not (layer and (layer.is_off() or layer.is_frozen()))


def outline_lines(paths):
    for path in paths:
        points = list(path.flattening(FLATTEN_MM))
        if any(abs(p.z) > FLATTEN_MM or not all(map(isfinite, p)) for p in points):
            raise ValueError("图元超出模型 XY 平面或坐标无效")
        if len(points) >= 2:
            segment = LineString([(p.x, p.y) for p in points])
            if segment.length:
                yield segment


def label_outline(entity):
    if entity.dxftype() != "TEXT":
        raise ValueError("仅测量原生单行 TEXT")
    style = entity.doc.styles.get(entity.dxf.style)
    requested = entity.font_name()
    filename = requested.replace(chr(92), "/").rsplit("/", 1)[-1]
    manager = fonts.font_manager
    if style.dxf.bigfont or style.dxf.flags & 5 or Path(filename).suffix.lower() not in fonts.SUPPORTED_TTF_TYPES:
        raise ValueError(f"未支持的房名字体或字型：{requested or style.dxf.name}")
    if not manager.has_font(filename):
        raise ValueError(f"本机缺房名原字体：{requested}")
    face = fonts.get_font_face(requested, map_shx=False)
    selected = manager.find_font_name(face)
    if face.filename.casefold() != filename.casefold() or selected.casefold() != filename.casefold():
        raise ValueError(f"检测到字体替代：{requested} → {selected}")
    font = manager.get_ttf_font(selected)
    file_name = getattr(font.reader.file, "name", "")
    if requested != filename and (not file_name or Path(requested).resolve() != Path(file_name).resolve()):
        raise ValueError(f"字体目录与房名指定文件不一致：{requested}")
    cmap = font.getBestCmap() or {}
    missing = sorted({char for char in entity.plain_text() if not char.isspace() and ord(char) not in cmap})
    if missing:
        raise ValueError(f"房名原字体缺字：{''.join(missing)}")
    lines = list(outline_lines(text2path.make_paths_from_entity(entity)))
    if not lines:
        raise ValueError("房名没有可测字形路径")
    return MultiLineString(lines), selected


def furniture_outline(entity):
    kind = entity.dxftype()
    if kind not in PATH_TYPES or (kind == "POLYLINE" and not (entity.is_2d_polyline or entity.is_3d_polyline)):
        raise ValueError(f"未测类型：{kind}")
    if kind in ("LWPOLYLINE", "POLYLINE") and entity.has_width:
        raise ValueError("未测带宽多段线的实际边")
    lines = list(outline_lines([make_path(entity)]))
    if not lines:
        raise ValueError("线形没有可量的长度")
    return lines


def simple_contacts(shape):
    if shape.is_empty:
        return
    if shape.geom_type in ("Point", "LineString"):
        yield shape
    else:
        for part in getattr(shape, "geoms", ()):
            yield from simple_contacts(part)


def pair_contacts(outline, strokes):
    points, overlaps, paths = set(), [], set()
    for path, stroke in strokes:
        for part in simple_contacts(outline.intersection(stroke)):
            paths.add(path)
            if part.geom_type == "Point":
                points.add((round(part.x, 6), round(part.y, 6)))
            else:
                ends = [[round(x, 6) for x in p] for p in (part.coords[0], part.coords[-1])]
                overlaps.append({"path": path, "ends_mm": ends, "length_mm": round(part.length, 6)})
    return [list(p) for p in sorted(points)], overlaps, sorted(paths)


def audit(doc, ledger):
    """Return review candidates, layer/geometry coverage, and measured WCS intersections."""
    roles = resolve_roles(doc, ledger.get("geometry_layers_v16") or ledger.get("layers"))
    room_layer = roles.get("rooms", annotation_layers(roles)["rooms"]).casefold()
    furniture_layers = {roles.get(name, "").casefold() for name in ("fixed", "movable")}
    result = dict(status="pass", scope="模型空间可见房名字形边线与固定或活动家具块的线形",
                  flattening_mm=FLATTEN_MM, candidates=[], coverage_issues=[],
                  counts=dict(labels=0, checked_labels=0, furniture=0, checked_furniture=0))
    coverage = {}

    def unmeasured(key, reason):
        coverage.setdefault(key, {"id": key, "reasons": []})["reasons"].append(str(reason))

    def finish():
        result["coverage_issues"] = list(coverage.values())
        if coverage or result["candidates"]:
            result["status"] = "review"
        return result

    if doc.units != 4:
        unmeasured("room-label/units", "模型单位未确认是毫米")
        return finish()

    model = [e for e in doc.modelspace() if visible(doc, e)]
    labels = [e for e in model if e.dxf.layer.casefold() == room_layer and e.dxftype() in ("TEXT", "MTEXT")]
    furniture = {e.dxf.handle: e for e in model
                 if e.dxf.layer.casefold() in furniture_layers and e.dxftype() == "INSERT"}
    result["counts"].update(labels=len(labels), furniture=len(furniture))
    if not labels:
        unmeasured("room-label/inventory", "无可见原生房名")
    if not furniture:
        unmeasured("room-furniture/inventory", "无可见固定或活动家具块")
    if not labels or not furniture:
        return finish()

    strokes = defaultdict(list)
    rows, expansion_issues = expand(doc)
    for entity, _, path in rows:
        root = path.split("/", 1)[0]
        if root not in furniture:
            continue
        try:
            lines = furniture_outline(entity)
        except READ_ERRORS as exc:
            unmeasured(f"room-furniture/{path}", exc)
        else:
            strokes[root].extend((path, line) for line in lines)
    for path, reason, _, _ in expansion_issues:
        if path.split("/", 1)[0] in furniture:
            unmeasured(f"room-furniture/{path}", reason)
    for handle in furniture.keys() - strokes.keys():
        unmeasured(f"room-furniture/{handle}", "无可量的可见线形")
    result["counts"]["checked_furniture"] = len(strokes)

    for label in labels:
        handle = label.dxf.handle
        try:
            outline, resolved_font = label_outline(label)
        except READ_ERRORS as exc:
            unmeasured(f"room-label/{handle}", exc)
            continue
        result["counts"]["checked_labels"] += 1
        for root, lines in strokes.items():
            points, overlaps, paths = pair_contacts(outline, lines)
            if points or overlaps:
                item = furniture[root]
                result["candidates"].append(dict(id=f"room-label/{handle}/{root}", label_handle=handle,
                    text=label.plain_text(), font=resolved_font, object_handle=root, block=item.dxf.name,
                    object_layer=item.dxf.layer, member_paths=paths, intersections_mm=points, overlaps=overlaps))
    return finish()
