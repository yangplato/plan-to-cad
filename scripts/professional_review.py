"""Find small steps on exposed wall faces and verify independent whole-plan review records."""
import argparse
import json
import math
from pathlib import Path

import ezdxf
from shapely.geometry import LineString, Point
from ezdxf.math import Vec2
from ezdxf.path import make_path
from ezdxf.disassemble import recursive_decompose
from check_wall_probes import decode
from native_readback import digest
from presentation import resolve_roles
import review_coverage
import source_geometry_review
from drafting_version import version

# Millimetres. Geometry comparison tolerances do not define acceptable construction errors.
EPS = 1e-5


def wall_steps(material, limit):
    if not math.isfinite(limit) or limit <= 0:
        raise ValueError("扫描步距必须为正数")
    steps = []
    polygons = getattr(material, "geoms", [material])
    for poly in polygons:
        if poly.geom_type != "Polygon" or poly.is_empty:
            continue
        for ring in [poly.exterior, *poly.interiors]:
            coords = list(ring.simplify(EPS).coords)[:-1]
            for i in range(len(coords)):
                a,b,c,d = [Vec2(coords[(i+j) % len(coords)]) for j in (-1,0,1,2)]
                prev, step, following = b-a, c-b, d-c
                if not EPS < step.magnitude <= limit or min(prev.magnitude, following.magnitude) < EPS:
                    continue
                # Short transverse return between parallel faces running in the same direction.
                if abs(prev.normalize().dot(step.normalize())) > EPS or prev.normalize().dot(following.normalize()) < 1-EPS:
                    continue
                ends = sorted([list(b), list(c)])
                key = "/".join(f"{v:.5f}" for pt in ends for v in pt)
                steps.append(dict(id="错台/"+key, ends_mm=ends, offset_mm=step.magnitude))
    return sorted(steps, key=lambda row: row["id"])


def screen(doc, ledger, limit=25.0):
    material, uncertain, problems, _ = decode(doc, ledger)
    return dict(search_mm=limit, rulers=review_coverage.model_rulers(doc), steps=wall_steps(material,limit),
                material_questions=[*problems, *(ident for ident,_ in uncertain)])


def anchored_distance(doc, handle, xy, cache):
    """Distance from a witness to a physical modelspace boundary. INSERTs include their transforms."""
    if handle not in cache:
        ent=doc.entitydb.get(handle)
        paths=[]
        if (ent and ent.is_alive and ent.dxf.owner == doc.modelspace().layout_key and
                ent.dxftype() in {"INSERT","LINE","ARC","CIRCLE","ELLIPSE","SPLINE","LWPOLYLINE","POLYLINE"}):
            for shape in recursive_decompose([ent]):
                if shape.dxftype() in {"LINE","ARC","CIRCLE","ELLIPSE","SPLINE","LWPOLYLINE","POLYLINE"}:
                    pts=[(v.x,v.y) for v in make_path(shape).flattening(0.005)]
                    if len(pts)>1:
                        paths.append(LineString(pts))
        cache[handle]=paths
    return min((path.distance(Point(xy)) for path in cache[handle]),default=math.inf)


def rounding_evidence(doc, ledger, record, cache):
    """Check chosen nominal spans against real, ordered faces and neighbouring controls."""
    missing = set()
    chains = record.get("rounding_chains", {})
    expected = ledger.get("rounding_chain_ids", [])
    if not isinstance(chains, dict) or not isinstance(expected, list):
        return {"取整事项清单格式有误"}
    if not all(isinstance(key, str) and key.strip() for key in [*expected, *chains]):
        return {"取整事项需具名"}
    if len(set(expected)) != len(expected):
        missing.add("取整事项 ID 重复")
    for key in set(expected) | set(chains):
        chain = chains.get(key, {})
        tag = f"尺寸取整/{key}"
        if not isinstance(chain, dict) or not all(
            isinstance(chain.get(field), str) and chain[field].strip()
            for field in ("basis", "fixed", "change")
        ):
            missing.add(tag + "/缺取值依据、固定基准或联动说明")
            continue
        sections = set()
        checks = chain.get("checks", [])
        if not isinstance(checks, list):
            missing.add(tag + "/缺截线实量记录")
            continue
        for span in checks:
            try:
                points = [Vec2(p) for p in span["points_mm"]]
                handles = span["handles"]
                targets = span["segments_mm"]
                total = span["total_mm"]
                if (not span.get("between") or len(points) < 2 or len(handles) != len(points)
                        or len(targets) != len(points)-1 or not math.isfinite(total) or total <= 0
                        or not all(math.isfinite(v) for point in points for v in point)
                        or not all(math.isfinite(v) and v > 0 for v in targets)):
                    raise ValueError
                direction = (points[-1]-points[0]).normalize()
                if abs(sum(targets)-total) > 0.01 or abs(points[-1].distance(points[0])-total) > 0.01:
                    raise ValueError
                for point, handle in zip(points, handles):
                    if anchored_distance(doc, handle, point, cache) > 0.01:
                        raise ValueError
                for start, end, target in zip(points, points[1:], targets):
                    segment = end-start
                    if abs(segment.dot(direction)-target) > 0.01 or abs(segment.det(direction)) > 0.01:
                        raise ValueError
                # Reversing or renaming the same witness does not add a regression check.
                endpoints = (points[0], points[-1])
                sections.add(tuple(sorted((round(v.x, 2), round(v.y, 2)) for v in endpoints)))
            except (TypeError, KeyError, ValueError, IndexError, ZeroDivisionError):
                missing.add(tag + "/复核实体端点、各段与控制总长")
        if len(sections) < 2:
            missing.add(tag + "/需有效的主尺寸链及关联截线")
    return missing


def private_room(name, use):
    identity = (str(name)+" "+str(use)).casefold()
    return any(key in identity for key in ("卧","卫生间","洗手间","主卫","客卫","公卫","次卫","套卫","卫浴",
               "厕所","浴室","儿童房","老人房","客房","bedroom","bathroom","toilet","ensuite","restroom"))


def open_axes(room, clear_regions, missing):
    """Accept a direction without a second face only with a checked, source-located open region."""
    sides = room.get("open_sides", {})
    if not isinstance(sides, dict):
        missing.add(f"{room['id']}/开敞方向记录无效")
        return []
    axes = []
    for axis, side in sides.items():
        tag = f"{room['id']}/{axis}向开敞"
        if not isinstance(side, dict) or axis not in ("x","y"):
            missing.add(tag+"/需明确方向与边界")
            continue
        use = room.get("room_use", "")
        if private_room(room['id'],use):
            missing.add(tag+"/卧室或卫生间需检查围护与门洞")
            continue
        if not isinstance(use,str) or not use.strip() or any(
                not isinstance(side.get(k),str) or not side[k].strip()
                for k in ("boundary","source_evidence","adjacent","use_check")):
            missing.add(tag+"/缺空间用途、边缘位置、相邻关系、源图或使用判断")
            continue
        if side.get("context") not in ("perimeter","continuous_interior"):
            missing.add(tag+"/说明外缘或有图证的室内连续空间")
            continue
        if clear_regions.get(side.get("clear_region_id")) != (room['id'], axis):
            missing.add(tag+"/缺同版同房间同方向的净区实查")
            continue
        axes.append(Vec2(1,0) if axis=="x" else Vec2(0,1))
    return axes


def verify(doc, ledger, record, findings, scan, clear_regions=None):
    """Return missing/invalid evidence; previously dispositioned observations link to review issues."""
    missing = set()
    rows = record.get("rooms", [])
    expected = {r["id"] for r in ledger["rooms"]}
    inventory=record.get("source_rooms", [])
    if not inventory or not all(isinstance(k,str) and k.strip() for k in inventory):
        missing.add("缺独立原图空间清单")
    else:
        expected.update(inventory)
    names = [r.get("id") for r in rows]
    if not expected:
        missing.add("原图与底稿未列出房间")
    if len(names)!=len(set(names)):
        missing.add("房间记录重复")
    missing.update(f"缺房间/{name}" for name in expected-set(names))
    # The reviewer may discover additional rooms absent from the author's ledger.
    cache={}
    for r in rows:
        name=r.get("id") or "未命名"
        for key, label in [("faces","墙柱与洞口接头"),("use","门扇、家具和通行")]:
            if not isinstance(r.get(key),str) or not r[key].strip():
                missing.add(f"{name}/{label}缺实查说明")
        axes=[]
        for i,span in enumerate(r.get("spans",[]),1):
            tag=f"{name}/实量{i}"
            try:
                a,b=(Vec2(xy) for xy in span["points_mm"])
                v=b-a
                if len(span["handles"])!=2 or len(span["points_mm"])!=2:
                    raise ValueError
                if not all(math.isfinite(x) for x in (*a,*b,span["mm"])) or v.magnitude<EPS:
                    raise ValueError
                if abs(v.magnitude-span["mm"])>0.01 or not str(span.get("between") or "").strip():
                    raise ValueError
                for h,point in zip(span["handles"],(a,b)):
                    if anchored_distance(doc,h,point,cache)>0.01:
                        raise ValueError
            except (KeyError,ValueError,TypeError,IndexError):
                missing.add(tag+"/查端点、实体句柄及实量")
                continue
            axes.append(v.normalize())
            if abs(v.magnitude-round(v.magnitude))>EPS and span.get("issue") not in findings:
                missing.add(tag+"/小数须登记处理依据")
        if version(ledger)>=(1,5):
            use = r.get("room_use")
            enclosure = r.get("enclosure")
            if not isinstance(use,str) or not use.strip() or enclosure not in ("enclosed","open"):
                missing.add(name+"/需独立判断用途及围护，未明处待核")
            if enclosure=="open" and not r.get("open_sides"):
                missing.add(name+"/需指定实际开敞方向与证据")
            if enclosure=="enclosed" and r.get("open_sides"):
                missing.add(name+"/围合判断与开敞方向冲突")
            if r.get("open_sides"):
                axes.extend(open_axes(r, clear_regions or {}, missing))
        if not any(abs(a.det(b))>0.05 for i,a in enumerate(axes) for b in axes[i+1:]):
            missing.add(name+"/缺两个不同方向的有效实量")
    resolutions = record.get("steps", {})
    for step in scan["steps"]:
        row=resolutions.get(step["id"],{})
        if not isinstance(row.get("evidence"),str) or not row["evidence"].strip() or (
                row.get("result")!="保留构造" and row.get("issue") not in findings):
            missing.add(step["id"]+"/查两侧面及原图")
    for i in scan["material_questions"]:
        if i not in findings:
            missing.add(i+"/核实墙材解析")
    if version(ledger) >= (0, 15) or "design_rulers" in record:
        missing.update(review_coverage.audit(doc,ledger,record,findings,scan["rulers"],
                       lambda h,p: anchored_distance(doc,h,p,cache)))
    missing.update(rounding_evidence(doc, ledger, record, cache))
    return sorted(missing)


def assess_capture(native, ledger, assessment, findings, limit=25):
    cap=json.loads((native/"capture.json").read_text())
    for name in ("plan.dwg","readback.dxf"):
        if digest(native/name) != cap["files"][name]:
            raise ValueError(f"{name} 与原生复开记录不符")
    if assessment.get("dwg_sha256") != cap["files"]["plan.dwg"] or not assessment.get("reviewer","").strip():
        raise ValueError("请提供审图者和实际复开的 DWG 哈希")
    doc=ezdxf.readfile(native/"readback.dxf")
    scan=screen(doc,ledger,limit)
    missing=[]
    if version(ledger) >= (1, 0):
        overlay = assessment.get("overlay_review") or {}
        source_hash = ledger.get("source_sha256", "")
        if len(source_hash) != 64 or overlay.get("source_sha256") != source_hash:
            missing.append("叠图/提供同一张原图的哈希")
        if overlay.get("dwg_sha256") != cap["files"]["plan.dwg"]:
            missing.append("叠图/使用本版 DWG 复查")
        observations = overlay.get("observations") or {}
        for key in ("junctions", "furniture", "uncertainties"):
            if not isinstance(observations.get(key),str) or not observations[key].strip():
                missing.append("叠图/缺少实际观察：" + key)
    review=assessment.get("professional_review",{})
    linked={span.get("issue") for row in review.get("rooms",[]) for span in row.get("spans",[])}
    linked.update(row.get("issue") for row in review.get("steps",{}).values())
    for row in review.get("opening_reviews",{}).values():
        linked.update(span.get("issue") for span in row.get("sections",{}).values())
    checks=review.get("design_rulers",{})
    linked.update(row.get("issue") for row in checks.values())
    geometry_spec = source_geometry_review.declared_spec(ledger, assessment)
    geometry_result = None
    if geometry_spec is not None:
        geometry_result = source_geometry_review.audit(
            doc, geometry_spec, dxf_sha256=cap["files"]["readback.dxf"],
            source_sha256=ledger.get("source_sha256"), dwg_sha256=cap["files"]["plan.dwg"])
        if geometry_result["pending"]:
            missing.extend("源图几何/待核/" + key for key in geometry_result["pending"])
    clear_regions = {}
    if version(ledger)>=(1,5) and geometry_result and isinstance(geometry_spec.get("checks"),list):
        for check in geometry_spec["checks"]:
            if not isinstance(check,dict) or check.get("kind")!="clear_region":
                continue
            result = geometry_result["results"].get(check.get("id"),{})
            if result.get("status")=="通过":
                clear_regions[check["id"]] = (check.get("room_id"),check.get("axis"))
    missing.extend(verify(doc,ledger,review,findings,scan,clear_regions))
    result = {"ruler_count":len(scan["rulers"]), "non_perimeter_rulers":sum(not r["perimeter"] for r in scan["rulers"].values()),
            "reviewed_openings":len(review.get("opening_reviews",{})),
            "missing":missing, "candidate_count":len(scan["steps"]),
            "room_count":len(review.get("rooms",[])),
            "linked_repairs":sorted(k for k in linked if k in findings and findings[k]["kind"]=="repair")}
    if geometry_result is not None:
        result["source_geometry_review"] = geometry_result
    return result


if __name__=="__main__":
    cli=argparse.ArgumentParser(description=__doc__)
    mode=cli.add_mutually_exclusive_group(required=True)
    mode.add_argument("readback", type=Path,nargs="?")
    mode.add_argument("--capture",type=Path, help="原生复开目录，包含 capture.json")
    cli.add_argument("--assessment",type=Path,help="对已编辑的 CAD 签署专业复查")
    cli.add_argument("--ledger", type=Path, help="本案房间及墙层清单")
    cli.add_argument("--step-mm",type=float, default=25)
    args=cli.parse_args()
    if args.ledger:profile=json.loads(args.ledger.read_text())
    else:
        roles=resolve_roles(ezdxf.readfile(args.readback or args.capture/'readback.dxf'))
        profile={"wall_layers":{roles[k]:"fills" for k in ("bearing","partition")},
                 "nonwall_fill_layers":[roles[k] for k in ("door","window","movable","fixed","ancillary")]}
    if args.assessment:
        if not args.capture or not args.ledger:
            cli.error("签署需要 --capture、--ledger 及 --assessment")
        assessment=json.loads(args.assessment.read_text())
        issues={i["id"]:i for i in assessment["issues"]}
        if len(issues)!=len(assessment["issues"]) or not all(
                i.get("kind") in {"repair","decision","inference"} and
                all(str(i.get(k) or "").strip() for k in ("id","location","evidence","finding")) for i in issues.values()):
            cli.error("每个审查问题应有唯一 ID、分类、位置、依据和判断")
        result=assess_capture(args.capture,profile,assessment,issues,args.step_mm)
        result["open_repairs"]=[key for key,i in issues.items() if i["kind"]=="repair"]
        result["decisions"]=[key for key,i in issues.items() if i["kind"]=="decision"]
        result["status"]=("待修正" if result.get("source_geometry_review", {}).get("failed") else
                          "待补证" if result["missing"] else "待修正" if result["open_repairs"] else
                          "待用户决定" if result["decisions"] else "本轮专业审查通过")
        print(json.dumps(result,ensure_ascii=False,indent=2))
        raise SystemExit(0 if result["status"]=="本轮专业审查通过" else 2)
    path=args.readback or args.capture/"readback.dxf"
    result=screen(ezdxf.readfile(path),profile,args.step_mm)
    result["readback_sha256"]=digest(path)
    print(json.dumps(result,ensure_ascii=False,indent=2))
