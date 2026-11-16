"""Interior-plan design dimensions and independent probes on the saved model (mm)."""
import math
from drafting_version import version
from ezdxf import bbox
from ezdxf.math import Vec2
from shapely.geometry import LineString, Point
from check_wall_probes import decode

BED_WIDTHS = (900, 1200, 1500, 1800)
EPS = 1e-5


def enabled(scene):
    return version(scene) >= (0, 13)


def whole(value):
    return math.isfinite(value) and abs(value-round(value)) <= EPS


def is_bed(row):
    return (row.get("is_bed") or row.get("family") in ("bed_double", "bed_single")
            or "_床_" in str(row.get("library_block", "")) or "床宽档" in row)


def valid_bed_width(row):
    width = row.get("床宽档", row.get("width_mm"))
    return width in BED_WIDTHS or (bool(row.get("bed_size_evidence"))
            and isinstance(width, (int, float)) and width > 0 and whole(width)
            and width == row.get("printed_bed_width_mm"))


def fit_door(op):
    """Use a designer-chosen clear opening for BOTH the wall cutter and symbol."""
    if op["kind"] not in ("swing", "sliding", "open"):
        return
    a, b = Vec2(op["a"]), Vec2(op["b"])
    vector = b-a
    span = vector.magnitude
    width = float(op.get("clear_width_mm", span))
    if width <= 0 or not whole(width) or span <= EPS:
        raise ValueError(f"{op['id']}: 洞宽 {width:g} mm，请据门型、门垛和通行选定整数毫米，指定 clear_width_mm 与 fixed_jamb")
    if abs(width-span) <= EPS:
        return
    fixed = op.get("fixed_jamb")
    if fixed not in ("a","b"):
        raise ValueError(f"{op['id']}: 写明保留哪个洞端 fixed_jamb=a/b，再复查相邻墙和尺寸链")
    if fixed == "a":
        op["b"] = list(a+vector.normalize(round(width)))
    else:
        op["a"] = list(b-vector.normalize(round(width)))
    op["observed_span_mm"] = span


def actual_span(material, p, axis, solid):
    """Intersect a section with actual wall fill; select the connected interval at p."""
    u = Vec2(axis).normalize()
    p = Vec2(p)
    if material.is_empty:
        return None
    bounds = material.bounds
    reach = max((Vec2(x,y)-p).magnitude for x in bounds[::2] for y in bounds[1::2])+1000
    line = LineString([p-u*reach,p+u*reach])
    shape = line.intersection(material) if solid else line.difference(material)
    for part in getattr(shape,"geoms",[shape]):
        if part.geom_type == "LineString" and part.length and part.distance(Point(p)) < EPS:
            if any((Vec2(c)-p).magnitude >= reach-EPS for c in (part.coords[0],part.coords[-1])):
                return None
            return part.length
    return None


def audit(doc, ledger, schedule, objects, checks):
    material, uncertain, errors, _ = decode(doc, {"wall_layers":ledger["wall_layers"],
                                        "nonwall_fill_layers":ledger["nonwall_fill_layers"]})
    rows = []
    def log(ident, kind, value, ok):
        rows.append(dict(id=ident, kind=kind, actual_mm=value,
                         status="待核" if ok is None else ("通过" if ok else "失败")))
    for op in ledger["openings"]:
        if op["kind"] not in ("swing","sliding","open"):
            continue
        a,b = Vec2(op["a"]),Vec2(op["b"])
        if list(op.get("jamb_support", ["wall","wall"])) != ["wall","wall"]:
            log(op["id"],"框端或自由端：复量实际支承边",None,None)
        else:
            value = actual_span(material,(a+b)/2,b-a,False)
            log(op["id"],"实墙间洞宽",value, None if value is None else
                whole(value) and abs(value-(b-a).magnitude)<EPS)
        if op["kind"]=="swing":
            axis=(b-a).normalize()
            vertices = []
            arcs = []
            for handle in op.get("symbol_handles",[]):
                e=doc.entitydb.get(handle)
                if e is None:
                    log(op["id"],"缺少已登记门图元",None,False)
                    continue
                pts=([Vec2(e.dxf.start),Vec2(e.dxf.end)] if e.dxftype()=="LINE" else
                     [Vec2(v) for v in e.get_points("xy")] if e.dxftype()=="LWPOLYLINE" else [])
                if e.dxftype()=="ARC":
                    arcs.append(e)
                vertices.extend(pts)
                if e.dxftype()=="LWPOLYLINE" and e.closed and pts: pts.append(pts[0])
                for p,q in zip(pts,pts[1:]):
                    if abs((q-p).magnitude-op["wall_thickness_mm"])<EPS and any(
                            abs((p-j).dot(axis))<EPS and abs((q-j).dot(axis))<EPS
                            and (p-j).dot(q-j)<-EPS for j in (a,b)):
                        if isinstance(op.get("symbol_source"), dict):
                            log(op["id"], "复核库块洞端横线是否为实际门框", (q-p).magnitude, None)
                        else:
                            log(op["id"], "门层重画了整段墙厚封口", (q-p).magnitude, False)
            if isinstance(op.get("symbol_source"), dict):
                log(op["id"], "库门实量铰点、门框及弧端", None, None)
            else:
                leaves = op.get("leaves", [])
                aligned = len(arcs) == len(leaves) and bool(vertices)
                for arc, leaf in zip(arcs, leaves):
                    jamb, other = (a,b) if leaf["hinge"] == "a" else (b,a)
                    along = (other-jamb).normalize()
                    normal = along.orthogonal()
                    if normal.dot(Vec2(leaf.get("toward") or op["toward"])-jamb)<0:
                        normal = -normal
                    inset = leaf.get("jamb_inset_mm", op.get("jamb_inset_mm", 0))
                    depth = op.get("symbol_depth_mm", op["wall_thickness_mm"])
                    face = leaf.get("hinge_face", op.get("hinge_face", "approach"))
                    pivot = jamb + along*inset + normal*depth*(0.5 if face == "receiving" else -0.5)
                    tip = pivot + normal*arc.dxf.radius
                    ends = [Vec2(arc.start_point), Vec2(arc.end_point)]
                    aligned = aligned and (Vec2(arc.dxf.center)-pivot).magnitude < EPS and all(
                        any(point.distance(v)<EPS for v in candidates)
                        for point, candidates in [(pivot,vertices), (tip,vertices), (tip,ends)])
                log(op["id"], "门扇铰点、自由端与开启弧对齐", None, aligned)
    for ck in checks:
        if ck["kind"] not in ("pier","room") or ck["design_mm"] <= 0 or not whole(ck["design_mm"]):
            raise ValueError(f"{ck['id']}: 请填写 room/pier 及正整数毫米尺寸")
        value=actual_span(material,ck["probe_mm"],ck["axis"],ck["kind"]=="pier")
        log(ck["id"],"墙垛实体长度" if ck["kind"]=="pier" else "室内净距",value,
            None if value is None else whole(value) and abs(value-ck["design_mm"])<EPS)
    for row in schedule:
        if not is_bed(row):
            continue
        width=row.get("床宽档",row.get("width_mm"))
        ref=doc.entitydb.get(objects[row["id"]]["handle"])
        if ref is None or ref.dxftype()!="INSERT":
            log(row["id"],"床块缺失",None,False)
            continue
        clone=ref.copy();clone.dxf.rotation=0
        ext=bbox.extents(clone.virtual_entities())
        outer=min(ext.size.x,ext.size.y) if ext.has_data else None
        if not valid_bed_width(row):
            log(row["id"],"床宽档位",width,False)
        else:
            log(row["id"],"床块短边；含床框时另验床垫净宽",outer,
                True if outer is not None and abs(outer-width)<EPS else None)
    pending=[r for r in rows if r["status"]=="待核"]
    failed=[r for r in rows if r["status"]=="失败"]
    return {"items":rows,"failed":failed,"pending":pending,"wall_decode_issues":errors,
            "uncertain_contours":len(uncertain),"room_pier_probes":len(checks)}
