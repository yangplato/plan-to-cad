"""Inspect the actual saved DXF against the drawing author's own independent source ledger.

Expected WCS millimetres; observation file has wall_layers, rooms and openings.
This preflight catches geometry contradictions before first submission. It does not
supply observations or establish completeness against a new marketing image.
"""
import argparse
import json
from pathlib import Path
import sys

import ezdxf
from shapely.geometry import Point, Polygon, LineString
from shapely.ops import unary_union
from check_wall_probes import decode


def check(doc, ledger):
    if doc.units != 4:
        raise ValueError("Set and check model units in millimetres")
    profile = {"wall_layers": ledger["wall_layers"],
               "nonwall_fill_layers": ledger.get("nonwall_fill_layers", [])}
    material, uncertain_contours, problems, inventory = decode(doc, profile)
    checks = []
    def add(key, role, overlap, message, verified=True):
        state = "失败" if overlap > 1e-4 else ("通过" if verified else "待核")
        checks.append(dict(id=key,check=role,status=state,value=round(overlap,4),basis=message))
    if not ledger.get("rooms") or not ledger.get("openings"):
        raise ValueError("Supply the visible rooms, free interior points/patches and openings")
    for room in ledger["rooms"]:
        points=room.get("free_points",[])
        patches=room.get("free_polygons",[])
        if not (points or patches):
            raise ValueError(f"{room['id']}: record an actual circulation area")
        for x,y in points:
            region=Point(x,y).buffer(1)
            add(room["id"],"室内通行点误填面积mm2",region.intersection(material).area,
                "原图中应空的室内地面",not problems and not any(g.intersects(region) for _,g in uncertain_contours))
        for vertices in patches:
            region=Polygon(vertices)
            if not region.is_valid or region.area<=0:raise ValueError("Invalid room clearance polygon")
            add(room["id"],"室内净区误填面积mm2",region.intersection(material).area,
                "只登记能确认应空的空间",not problems and not any(g.intersects(region) for _,g in uncertain_contours))
    for op in ledger["openings"]:
        a=complex(*op["a"]);b=complex(*op["b"]);width=abs(b-a);depth=float(op['wall_thickness_mm'])
        if width<=0 or depth<=0:raise ValueError(f"{op['id']}: empty opening or missing host thickness")
        u=(b-a)/width; n=u*1j
        # Test the unambiguous clear region, allowing end-frame returns to be assessed separately.
        end_margin=float(op.get("clear_margin_mm",min(20,width*.05)))
        if not 0<=end_margin<width/2:raise ValueError(f"{op['id']}: invalid clear margin")
        corners=[a+u*end_margin+n*(depth/2+2),b-u*end_margin+n*(depth/2+2),
                 b-u*end_margin-n*(depth/2+2),a+u*end_margin-n*(depth/2+2)]
        region=Polygon([(p.real,p.imag) for p in corners])
        add(op["id"],"宿主墙净洞残材mm2",material.intersection(region).area,
            "留足两墙面的贯穿检查",not problems and not any(g.intersects(region) for _,g in uncertain_contours))
        for end,poly in op.get("solid_ends",{}).items():
            box=Polygon(poly)
            if not box.is_valid or box.area<=0:raise ValueError("Invalid jamb witness polygon")
            add(op["id"]+"/"+end,"墙垛缺少实材mm2",box.difference(material).area,"原图可辨墙芯区",
                not problems and not any(g.intersects(box) for _,g in uncertain_contours))
    for wall in ledger.get("walls",[]):
        if 'proof_line' not in wall:continue
        line=LineString(wall['proof_line'])
        if not line.is_valid or line.length<=0:raise ValueError("Invalid wall witness line")
        delta=line.difference(material).length
        add(wall['id'],"连续墙材缺长mm",delta,"仅指定可辨实墙连续段，遇洞断开",
            not problems and not any(g.intersects(line) for _,g in uncertain_contours))
    states={c['status'] for c in checks}
    status='失败' if '失败' in states else ('待核' if '待核' in states or problems else '通过')
    return dict(status=status,decode_issues=problems,inventory=inventory,checks=checks)


if __name__=='__main__':
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument('dxf',type=Path);cli.add_argument('ledger',type=Path)
    a=cli.parse_args()
    try:
        result=check(ezdxf.readfile(a.dxf),json.loads(a.ledger.read_text()))
    except (OSError,ValueError,KeyError,TypeError,ezdxf.DXFError) as error:
        result={'status':'待核','reason':str(error)}
    print(json.dumps(result,ensure_ascii=False,indent=2))
    sys.exit({'通过':0,'失败':1,'待核':2}[result['status']])
