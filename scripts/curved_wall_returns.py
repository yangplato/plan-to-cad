"""Source-registered quarter-elliptic solid return between a straight wall and a straight window."""
from math import ceil, cos, pi, sin, sqrt

from ezdxf.math import Vec2
from shapely.geometry import Polygon
from shapely.ops import unary_union

from draw_plan_parts import add_wall_material
from source_geometry_review import wall_material


def endpoint(rows, key, side):
    matches = [row for row in rows if row["id"] == key]
    if len(matches) != 1 or side not in ("a", "b"):
        raise ValueError(f"{key}: identify one registered segment and its physical end")
    row = matches[0]
    point = Vec2(row[side])
    return row, point, (point-Vec2(row["b" if side=="a" else "a"])).normalize()


def footprint(ledger, row):
    from wall_geometry_v16 import evidence_at
    wall, start, incoming = endpoint(ledger["walls"],row["wall_id"],row["wall_end"])
    window, end, opposite = endpoint(ledger["openings"],row["window_id"],row["window_end"])
    width = wall["width_mm"]
    if (window["kind"]!="window" or abs(window["wall_thickness_mm"]-width)>.1 or
            window.get("geometry_exception") or window.get("jamb_support",["wall","wall"])["ab".index(row["window_end"])]!="wall"):
        raise ValueError("Register a straight window supported at the return, with matching nominal thickness")
    if (abs(incoming.dot(opposite))>1e-8 or min(abs(incoming.x),abs(incoming.y))>1e-8 or
            min(abs(opposite.x),abs(opposite.y))>1e-8):
        raise ValueError("A quarter-turn requires perpendicular axis-aligned straight segments")
    if not evidence_at(row,ledger.get("source_sha256"),dict(a=start,b=end,width_mm=width),ledger,whole=True):
        raise ValueError("Bind the complete turn to a local crop of this source image")
    chord=end-start
    v=incoming*chord.dot(incoming)
    u=v-chord
    if v.dot(incoming)<=0 or u.dot(opposite)<=0:
        raise ValueError("The return must leave the wall and arrive in the direction of the window")
    centre=start-u
    small,large=sorted([u.magnitude,v.magnitude])
    if small**2/large<=width/2:
        raise ValueError("The inner offset folds at this bend radius; register the actual profile separately")
    # The offset curve also turns by pi/2. Increase angular resolution for
    # elongated ellipses to keep the parallel walls smooth near both tangencies.
    steps=max(96,ceil((pi/2)*sqrt((large+width*(large/small)**2)/.08)))
    if steps>4096:
        raise ValueError("Source radii exceed the supported resolution for this quarter-turn")
    left,right=[],[]
    for i in range(steps+1):
        t=pi*i/(2*steps)
        c,s=(1.,0.) if i==0 else (0.,1.) if i==steps else (cos(t),sin(t))
        point=centre+u*c+v*s
        half=(-u*s+v*c).normalize(width/2).orthogonal()
        left.append(tuple(point+half));right.append(tuple(point-half))
    shape=Polygon([*left,*reversed(right)])
    if not shape.is_valid or shape.area<=0:
        raise ValueError("Invalid closed solid return")
    return shape,wall["material"]


def draw_returns(doc, ledger, specs):
    from wall_geometry_v16 import TOL
    _, material, problems=wall_material(doc,list(ledger["wall_layers"]),[1,0,0,1,0,0])
    if problems:
        raise ValueError(f"Resolve wall geometry before drawing a curved return: {problems}")
    occupied_ids={r["id"] for r in [*ledger["walls"],*ledger["openings"]]}
    rows=[]
    for row in specs:
        ident=row["id"]
        if not ident or ident in occupied_ids:
            raise ValueError(f"Duplicate or empty curved wall ID {ident!r}")
        shape,layer=footprint(ledger,row)
        if material.intersection(shape).area>TOL**2:
            raise ValueError(f"{ident}: solid return overlaps existing wall material")
        msp=doc.modelspace()
        add_wall_material(msp,{ident:(layer,shape)}, {},{layer:66 if layer==ledger["geometry_layers_v16"]["bearing"] else 178})
        hatch=msp[-2]; boundary=msp[-1]
        rows.append({**row,"native_handles":[hatch.dxf.handle,boundary.dxf.handle]})
        material=material.union(shape)
        occupied_ids.add(ident)
    ledger["curved_wall_returns"]=rows


def inspect_returns(out, ledger, doc, records):
    for row in ledger.get("curved_wall_returns",[]):
        key="solid-return/"+row["id"]
        try:
            shape,layer=footprint(ledger,row)
            handles=row["native_handles"]
            if len(handles)!=2:
                raise ValueError("Missing wall fill / closed editable contour")
            hatch,boundary=(doc.entitydb.get(h) for h in handles)
            matched=[r["shape"] for r in records if r["layer"]==layer and handles[0] in r["handles"]]
            linked=(hatch is not None and hatch.dxftype()=="HATCH" and hatch.dxf.associative and hatch.dxf.layer==layer and
                    boundary is not None and boundary.dxftype()=="LWPOLYLINE" and boundary.closed and boundary.dxf.layer==layer and
                    any(handles[1] in path.source_boundary_objects for path in hatch.paths))
            actual=unary_union(matched)
            delta=shape.hausdorff_distance(actual) if not actual.is_empty else None
            outline=Polygon([(p[0],p[1]) for p in boundary.get_points()]) if linked else None
            outline_delta=shape.boundary.hausdorff_distance(outline.boundary) if linked else None
            out.test(key,linked and delta is not None and delta<=.1 and outline_delta<=.1,
                     matched_handles=handles,profile_delta_mm=delta,contour_delta_mm=outline_delta)
        except (ValueError,KeyError,TypeError,ArithmeticError) as error:
            out.add(key,"pending",reason=str(error))
