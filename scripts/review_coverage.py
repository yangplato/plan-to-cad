"""Inventory dimension entities, source openings and decisions on a frozen editable plan."""
from math import isfinite
from ezdxf.math import Vec2
from presentation import APP, resolve_roles


def model_rulers(doc):
    """Inspect native modelspace dimensions, including actual block instances."""
    rows = {}
    def visit(ent, key, ancestry):
        if ent.dxftype() == "INSERT" and ent.dxf.name not in ancestry:
            for instance_no, ref in enumerate(ent.multi_insert() if ent.mcount > 1 else [ent]):
                for child in ref.virtual_entities():
                    if child.dxftype() not in ("DIMENSION", "INSERT"):
                        continue
                    original = child.source_of_copy
                    if original is None:
                        raise ValueError("块内量尺缺少可追溯的原始图元")
                    visit(child, f"{key}[{instance_no}]/{original.dxf.handle}", ancestry | {ent.dxf.name})
        elif ent.dxftype() == "DIMENSION":
            linear = ent.dimtype in (0, 1)
            row = {"layer":ent.dxf.layer, "style":ent.dxf.dimstyle, "type":ent.dimtype,
                   "perimeter":ent.has_xdata(APP), "text":ent.dxf.get("text") or "<>"}
            if linear:
                row.update(mm=float(ent.get_measurement()),
                           points_mm=[list(Vec2(ent.dxf.get(k))) for k in ("defpoint2","defpoint3")])
            rows[key] = row
    for entity in doc.modelspace().query("DIMENSION INSERT"):
        visit(entity, entity.dxf.handle, set())
    return rows


def audit(doc, ledger, record, findings, rulers, anchored):
    missing = set()
    checks = record.get("design_rulers", {})
    for h, ruler in rulers.items():
        if ruler["perimeter"]:
            continue  # Native perimeter measurements have a separate geometric audit.
        tag = "检查量尺/" + h
        row = checks.get(h, {})
        if not row.get("between") or not row.get("group"):
            missing.add(tag+"/缺被量对象及共用关系")
            continue
        pts = ruler.get("points_mm")
        if pts is None:
            if not row.get("method") or not row.get("evidence"):
                missing.add(tag+"/非线性量尺须按其角度、半径等语义另验")
            continue
        value = ruler["mm"]
        if not isinstance(row.get("mm"),(int,float)) or not isfinite(row["mm"]) or not isfinite(value) or abs(row["mm"]-value)>0.00001:
            missing.add(tag+"/实量与成图不符")
        pair = row.get("handles", [])
        if len(pair)!=2 or any(anchored(handle,pt)>.01 for handle,pt in zip(pair,pts)):
            missing.add(tag+"/量尺两端未落实体，需重新指定实量")
        if ruler["text"] != "<>" and not ("<>" in ruler["text"] and row.get("evidence")):
            missing.add(tag+"/检查标注文字覆盖，恢复实量或复核表达")
        if abs(value-round(value))>0.00001 and row.get("issue") not in findings:
            missing.add(tag+"/碎数未处理或无保留依据")
    if set(checks)-set(rulers):
        missing.add("检查量尺/记录与本次回读版本不一致")

    kinds = {o["id"]:o.get("kind") for o in ledger.get("openings", [])}
    listed = set(kinds)
    observed = record.get("source_openings")
    if not isinstance(observed,list) or len(observed)!=len(set(observed)):
        missing.add("开口/缺独立原图清点")
        observed = []
    verified = record.get("opening_reviews", {})
    covered_symbols = set()
    for ident in listed | set(observed):
        row = verified.get(ident,{})
        # The reviewer identifies the actual opening symbol or, for an open passage, actual end faces.
        handles = row.get("handles", [])
        covered_symbols.update(handles)
        kind = kinds.get(ident) or row.get("kind")
        if kind not in ("swing","sliding","open","window","rail"):
            missing.add("开口/"+ident+"/需登记实际开口类型")
        else:
            measured = {}
            for side in ("洞宽","首端","末端"):
                trace = row.get("sections",{}).get(side,{})
                pts, pair = trace.get("points_mm",[]), trace.get("handles",[])
                if side != "洞宽" and trace.get("joint") in ("框接框","转角接头","直接贴实墙面"):
                    try:
                        a=Vec2(trace["point_mm"])
                        if (len(pair)!=2 or len(set(pair))!=2 or not trace.get("evidence")
                                or not any(h in handles for h in pair)
                                or any(not isfinite(v) for v in a)
                                or any(anchored(h,a)>.01 for h in pair)):
                            raise ValueError
                    except (KeyError, TypeError, ValueError, IndexError):
                        missing.add(f"开口/{ident}/{side}/对接件未在实际同一点复量")
                    continue
                if trace.get("free_end") and side != "洞宽":
                    try:
                        endpoint = Vec2(trace["point_mm"])
                        if (not trace.get("evidence") or trace.get("handle") not in handles
                                or not all(isfinite(v) for v in endpoint)
                                or anchored(trace["handle"],endpoint)>.01):
                            raise ValueError
                    except (KeyError,TypeError,ValueError,IndexError):
                        missing.add(f"开口/{ident}/{side}/自由端须落在实际收口")
                    continue
                try:
                    pts = [Vec2(p) for p in pts]
                    dist = pts[0].distance(pts[1])
                    if (len(pts)!=2 or len(pair)!=2 or not trace.get("between")
                            or any(not isfinite(v) for pt in pts for v in pt)
                            or not isfinite(trace["mm"]) or abs(dist-trace["mm"])>.01
                            or any(anchored(h,pt)>.01 for h,pt in zip(pair,pts))):
                        raise ValueError
                    measured[side] = pts
                    if abs(dist-round(dist))>0.00001 and trace.get("issue") not in findings:
                        missing.add(f"开口/{ident}/{side}/碎数未处置")
                except (KeyError, TypeError, ValueError, IndexError):
                    missing.add(f"开口/{ident}/{side}/缺独立可复量的两端")

            if "洞宽" in measured:
                a,b = measured["洞宽"]
                if a.distance(b)<=.01:
                    missing.add("开口/"+ident+"/净洞宽应为正值")
                else:
                    axis = (b-a).normalize()
                    for side,jamb in [("首端",a),("末端",b)]:
                        trace = row["sections"].get(side,{})
                        if (trace.get("joint") or trace.get("free_end")) and "point_mm" in trace:
                            try:
                                if abs((Vec2(trace["point_mm"])-jamb).dot(axis))>.01:
                                    missing.add(f"开口/{ident}/{side}/接点须位于对应洞端")
                            except (TypeError, ValueError, IndexError):
                                missing.add(f"开口/{ident}/{side}/对接点坐标无效")
                    for side,jamb,sign in [("首端",a,-1),("末端",b,1)]:
                        if side not in measured:
                            continue
                        first,last=measured[side]
                        # The return may be taken on the other face of the wall.
                        if abs((first-jamb).dot(axis))>.01 or abs((last-first).det(axis))>.01 or (last-first).dot(axis)*sign < -.01:
                            missing.add(f"开口/{ident}/{side}/须从本洞对应端向外量")
        if not row.get("evidence") or not handles or any(
                not (ent:=doc.entitydb.get(h)) or not ent.is_alive or ent.dxf.owner!=doc.modelspace().layout_key
                or ent.dxftype() not in {"LINE","ARC","LWPOLYLINE","POLYLINE","INSERT","CIRCLE","SPLINE","ELLIPSE"}
                for h in handles):
            missing.add("开口/"+ident+"/缺实际构件与检查依据")
    if set(verified)!=(listed|set(observed)) or listed-set(observed):
        missing.add("开口/原图、几何清单与逐处复核未对齐")

    roles=resolve_roles(doc,ledger.get('layer_roles'))
    layers = ledger.get("opening_layers", [roles["door"], roles["window"]])
    for entity in doc.modelspace():
        if entity.dxf.layer in layers and entity.dxftype() not in ("TEXT","MTEXT","DIMENSION","POINT"):
            if entity.dxf.handle not in covered_symbols:
                missing.add("未认领门窗图元/"+entity.dxf.handle)

    for ident, issue in findings.items():
        if issue.get("kind")!="decision":
            continue
        alt=issue.get("alternatives", [])
        if (not issue.get("unresolved_evidence") or len({str(v).strip() for v in alt if str(v).strip()})<2):
            missing.add("待决定/"+ident+"/说明仍缺的判别证据和至少两种有依据的处置及其影响")
    return missing
