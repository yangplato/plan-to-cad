"""Build or import a structure from the author's own source-image observations.
Geometry records use original-image pixels for positions and mm for physical widths/depths.
This entry is for rectified, orthogonal plans. A room may contain test points, never wall fill.
"""
import argparse
import json
import math
from decimal import Decimal
from pathlib import Path

import ezdxf
from ezdxf.math import Vec2
from dimension_grid import prepare_plan
from wall_thickness import records as wall_thickness_records
from build_wall_runs import draw_wall_runs
from draw_opening import draw_opening
from drafting_version import version
from drawing_junctions import bind_hinges, draw_frame_path, resolve_wall_nodes
from draw_plan_parts import add_mm_dimension


from presentation import LEGACY as LAYERS, roles as plan_layers, current, perimeter


def create_structure(scene,library_root=None):
    scene,_=prepare_plan(scene, [])
    layers = plan_layers(scene)
    a,b,d,e,tx,ty = map(float,scene["source_to_mm"])
    if a<=0 or e>=0 or max(abs(b),abs(d))>1e-9:
        raise ValueError("Rectify the image first or use WCS operations for an oblique plan")
    def world(p):
        return [a*p[0]+b*p[1]+tx,d*p[0]+e*p[1]+ty]
    for required in ("walls","openings","rooms"):
        if not scene.get(required):
            raise ValueError(f"Record {required} from the original image before drafting")
    if not scene.get("dimension_chains") and scene.get("grid_registration",{}).get("mode")!="unmarked":
        raise ValueError("Record printed chains or identify the unmarked source scale reference")
    doc=ezdxf.new("R2018",setup=True)
    doc.units=4
    for name in dict.fromkeys(layers.values()):
        if name not in doc.layers:doc.layers.new(name)
    walls=[]
    for row in scene["walls"]:
        wall={**row, "a":world(row["a"]), "b":world(row["b"])}
        if row["material"] not in ("bearing","partition"):
            raise ValueError(f"{row['id']}: record the displayed dark/light role")
        wall["material"]=layers[row["material"]]
        if "proof_line" in row:wall["proof_line"]=[world(p) for p in row["proof_line"]]
        walls.append(wall)
    resolve_wall_nodes(walls, scene.get("wall_junctions", []))
    if version(scene)>=(1,6):
        from wall_geometry_v16 import bind_material_joints
        bind_material_joints(walls, scene)
    widths={w["id"]:w["width_mm"] for w in walls}
    rooms=[dict(id=o["id"],free_points=[world(p) for p in o.get("free_points",[])],
                free_polygons=[[world(p) for p in z] for z in o.get("free_polygons",[])]) for o in scene["rooms"]]
    by_room={row["id"]:row for row in scene["rooms"]}
    if len(by_room)!=len(rooms) or "outside" in by_room:
        raise ValueError("Room IDs must be unique; outside denotes the exterior approach")
    accessible={key for key,room in by_room.items() if room.get("accessible",True)}
    linked={key:set() for key in [*by_room,"outside"]}
    from design_dimensions import enabled, fit_door
    openings=[]
    for row in scene["openings"]:
        op={**row,"a":world(row["a"]),"b":world(row["b"])}
        if (enabled(scene) and op["kind"] in ("window", "rail")
                and row.get("boundary_context") == "open_lift_lobby_edge"
                and not row.get("enclosure_evidence")):
            if op["kind"] == "window" and (op.get("reference_id") or op.get("reference_block")):
                raise ValueError(f"{op['id']}: 临空边请选栏杆图例，或登记此处为封闭窗洞的依据")
            op["kind"] = "rail"
            op["boundary_use"] = "glass_guardrail"
        if row["hosts"]:
            op["wall_thickness_mm"]=max(widths[ident] for ident in row["hosts"])
        else:
            if not row.get("free_frame_reason") or row["kind"] not in ("rail","sliding","window"):
                raise ValueError(f"{row['id']}: identify the host, or document a genuinely free-standing frame")
            op["wall_thickness_mm"]=float(row["symbol_depth_mm"])
        if op["kind"] in ("swing","sliding","open") and not row.get("fixture_partition",False):
            link=row.get("connects",[])
            if len(link)!=2 or link[0]==link[1] or any(key not in linked for key in link):
                raise ValueError(f"{op['id']}: record the two rooms on either side (outside for the entrance)")
            linked[link[0]].add(link[1]);linked[link[1]].add(link[0])
        if "opens_into" in row and "toward" not in row:
            room=by_room.get(row["opens_into"],{})
            if not room.get("free_points"):
                raise ValueError(f"{op['id']}: give a toward point or register a free point in its receiving room")
            op["toward"]=world(room["free_points"][0])
        if "toward" in row:op["toward"]=world(row["toward"])
        if "leaves" in row:
            op["leaves"]=[{**leaf,**({"toward":world(leaf["toward"])} if "toward" in leaf else {})}
                          for leaf in row["leaves"]]
        if "solid_ends" in row:
            op["solid_ends"]={key:[world(p) for p in poly] for key,poly in row["solid_ends"].items()}
        if enabled(scene):
            fit_door(op)
            if op["kind"]=="swing": op["flush_door_face"]=True
        openings.append(op)
    for link in scene.get("open_room_links",[]):
        if (len(link.get("rooms",[]))!=2 or not link.get("evidence") or
                any(key not in by_room for key in link["rooms"])):
            raise ValueError("An openly connected space needs two room IDs and its source evidence")
        src,dst=link["rooms"];linked[src].add(dst);linked[dst].add(src)
    visited=set();todo=["outside"]
    while todo:
        key=todo.pop()
        if key not in visited:
            visited.add(key);todo.extend(linked[key]-visited)
    if accessible-visited:
        raise ValueError(f"Unconnected source rooms: {sorted(accessible-visited)}; re-read the approaches and their wall openings")
    msp=doc.modelspace()
    ledger={"wall_layers":{name:"fills" for name in {w["material"] for w in walls}},
            "walls":walls,"rooms":rooms,"openings":openings,"room_links":{k:sorted(v) for k,v in linked.items()},
            "nonwall_fill_layers":[layers[k] for k in ("movable","fixed","door","window","ancillary")]}
    ledger["drafting_version"] = scene.get("drafting_version", "")
    if version(scene)>=(1,6):
        ledger["wall_geometry_v16"] = scene.get("wall_geometry_v16")
        ledger["geometry_layers_v16"] = dict(layers)
        ledger["geometry_registration_v16"] = {k:scene[k] for k in ("source_to_mm","grid_registration") if k in scene}
    if scene.get("wall_thickness_policy"):
        ledger["wall_thickness_policy"] = scene["wall_thickness_policy"]
        ledger["wall_thickness_review"] = wall_thickness_records(scene)
    if "source_sha256" in scene:
        ledger["source_sha256"] = scene["source_sha256"]
    returns = scene.get("curved_wall_returns",[]) if version(scene)>=(1,6) else []
    from curved_wall_returns import footprint
    draw_wall_runs(msp,walls,[op for op in openings if op["hosts"]],{layers["bearing"]:66,layers["partition"]:178},
                   clear_points_mm=[point for room in rooms for point in room["free_points"]],
                   return_shapes=[footprint(ledger,row)[0] for row in returns])
    if scene.get("rounding_chain_ids"):
        ledger["rounding_chain_ids"] = list(scene["rounding_chain_ids"])
    if scene.get('printed_wall_tones'):
        from apply_wall_tones import apply_to_new_drawing
        apply_to_new_drawing(doc,ledger,scene)
    if returns:
        from curved_wall_returns import draw_returns
        draw_returns(doc,ledger,returns)
    if version(scene) >= (1, 4):
        from source_geometry_review import wall_material
        _, material, issues = wall_material(doc, list(ledger["wall_layers"]), [1,0,0,1,0,0])
        if issues:
            raise ValueError(f"Resolve wall material before placing openings: {issues}")
        for op in openings:
            if op["kind"] == "swing":
                bind_hinges(op, material)
            elif op["kind"] == "window" and version(scene)>=(1,6):
                from wall_geometry_v16 import bind_window
                bind_window(op, material, scene.get("source_sha256"))
    by_id = {op["id"]:op for op in openings}
    joined = set()
    for run in scene.get("frame_paths", []):
        group = [by_id[k] for k in run["openings"]]
        if (len(group)<2 or any(op["kind"]!="rail" or op["id"] in joined for op in group)
                or len({op["id"] for op in group})!=len(group)
                or any(Vec2(a["b"]).distance(Vec2(b["a"]))>1e-6 for a,b in zip(group,group[1:]))):
            raise ValueError("A frame path requires unique rails ordered end to start")
        if any(op.get("lines_mm",run["offsets_mm"])!=run["offsets_mm"] for op in group):
            raise ValueError("Check the rail offsets at the change of section")
        points = [op["a"] for op in group]+[group[-1]["b"]]
        drawn = draw_frame_path(msp,points,run["offsets_mm"],layers["ancillary"],
                                run.get("cap_start",True),run.get("cap_end",True))
        doc.groups.new().set_data(drawn)
        for op in group:
            op["symbol_handles"]=[e.dxf.handle for e in drawn]
            op["symbol_source"]="连续折角栏杆"
            joined.add(op["id"])
    for opening in openings:
        if opening["id"] in joined:
            continue
        ref=opening.get("reference_block")
        if opening.get("reference_id"):
            ref=scene["opening_catalog"][opening["reference_id"]]
        if opening.get("frame_face_offsets_mm") and opening["kind"]=="window":
            # Ordinary straight windows use the project four-rail convention at actual host depth.
            ref = None
        if ref and opening["kind"]!="open":
            from interior_block_library import BUNDLED
            from reference_opening import draw
            role={"swing":"door","sliding":"door","window":"window","rail":"ancillary"}[opening["kind"]]
            drawn=draw(msp,opening,ref,library_root,layers[role])
            opening["symbol_source"]={"file":ref.get("file", BUNDLED.name),"block":ref["block"]}
        else:
            drawn=draw_opening(msp,opening,layers["door"],layers["window"],layers["ancillary"])
            opening["symbol_source"]=opening.get("symbol_basis","按本图开向和所属墙厚套用现有参数化平面图例")
        if len(drawn)>1:
            doc.groups.new().set_data(drawn)
        opening["symbol_handles"]=[en.dxf.handle for en in drawn]
    precision=0
    if "grid_registration" in scene:
        for row in scene.get("dimension_chains",[]):
            for value in row["values_mm"]:
                precision=max(precision,-Decimal(str(value)).normalize().as_tuple().exponent)
        if precision>8:
            raise ValueError("AutoCAD dimensions can display at most eight decimal places; check the printed value")
    style=doc.dimstyles.new("PLAN-MM",dxfattribs={"dimtxsty":"Standard","dimtxt":140,
           "dimasz":85,"dimscale":1,"dimdec":precision,"dimzin":8,"dimexo":40,"dimexe":45})
    if current(scene):
        ledger["dimension_presentation"]=perimeter(doc,scene,layers,style.dxf.name)
    for chain in ([] if current(scene) else scene.get("dimension_chains",[])):
        if "grid_registration" in scene:
            witnesses=chain["grid_witness_mm"]
            for p,q in zip(witnesses,witnesses[1:]):
                add_mm_dimension(msp,p,q,chain["grid_dim_line_mm"],0 if chain["axis"]=="x" else 90,
                                 layer=layers["dimension"],dimstyle=style.dxf.name)
            continue
        axis=chain["axis"]
        ticks=chain["ticks_px"];values=chain["values_mm"]
        if axis not in ("x","y") or not values or len(ticks)!=len(values)+1:
            raise ValueError(f"{chain['id']}: use one chain per continuous sequence of printed values")
        coord=lambda t,n: (t,n) if axis=="x" else (n,t)
        station=Vec2(world(coord(ticks[0],chain["witness_px"])))
        direction=Vec2(1,0) if axis=="x" else Vec2(0,-1)
        normal=Vec2(world(coord(ticks[0],chain["line_px"])))-station
        for first,last,value in zip(ticks,ticks[1:],values):
            if value<=0 or first==last:raise ValueError("Expected distinct stations and positive printed value")
            next_station=station+direction*math.copysign(value,last-first)
            add_mm_dimension(msp, station,next_station,station+normal,0 if axis=="x" else 90,
                             layer=layers["dimension"],dimstyle=style.dxf.name)
            station=next_station
    if "grid_registration" in scene:
        ledger["grid_registration"]=scene["grid_registration"]
    return doc,ledger


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scene",type=Path);parser.add_argument("dxf",type=Path);parser.add_argument("ledger",type=Path)
    args=parser.parse_args()
    scene=json.loads(args.scene.read_text(encoding="utf-8"))
    doc,ledger=create_structure(scene);doc.saveas(args.dxf)
    args.ledger.write_text(json.dumps(ledger,ensure_ascii=False,indent=2),encoding="utf-8")
