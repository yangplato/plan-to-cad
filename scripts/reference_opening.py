"""Insert geometry from a registered neutral opening block on its true jamb stations."""
from interior_block_library import BUNDLED, GUIDES, index, library, resolve_file
from ezdxf.disassemble import recursive_decompose
from ezdxf.math import Matrix44, Vec2


def draw(msp, item, ref, root, layer):
    file = resolve_file(root, ref.get("file"))
    using_preset = file == BUNDLED and "a" not in ref and "b" not in ref
    if using_preset:
        record = index().get(ref["block"], {}).get("opening")
        if not record:
            raise ValueError(f"{item['id']}: select a registered opening symbol")
        if record["type"] == "folding":
            raise ValueError(f"{item['id']}: select the folding reference explicitly with its jambs for a traced folding opening")
        leaves = item.get("leaves", [])
        expected = 1 if record["type"] == "single_swing" else 2
        if item["kind"] != "swing" or len(leaves) != expected:
            raise ValueError(f"{item['id']}: selected block does not match the observed door type")
        if expected == 2 and any(abs(leaf["share"]-.5)>1e-6 for leaf in leaves):
            raise ValueError(f"{item['id']}: select a block matching the registered unequal leaves")
        ref = {**record, **ref}
        if expected == 1 and leaves[0]["hinge"] == "b":
            ref["a"],ref["b"]=ref["b"],ref["a"]
    block=library(file).blocks.get(ref["block"])
    if block is None or block.block.is_xref or block.block.is_xref_overlay:
        raise ValueError(f"{item['id']}: expected a self-contained reference block")
    source,target=Vec2(ref["a"]),Vec2(item["a"])
    v=Vec2(ref["b"])-source;w=Vec2(item["b"])-target
    if min(v.magnitude,w.magnitude)<.01:raise ValueError("Register two different jamb ends")
    scale=w.magnitude/v.magnitude
    mirror=1
    if item["kind"]=="swing":
        into=item.get("toward") or item["leaves"][0].get("toward")
        if not into or "toward" not in ref:raise ValueError("Register the opening side in both frames")
        s=v.det(Vec2(ref["toward"])-source);t=w.det(Vec2(into)-target)
        if abs(s*t)<1e-6:raise ValueError("Swing direction cannot lie on the jamb line")
        mirror=1 if s*t>0 else -1
        if using_preset:
            # Place the swing-side face of the physical frame flush with its host.
            # Do not stretch the frame depth or the leaf to fill a thicker wall.
            host_depth = float(item["wall_thickness_mm"])
            frame_depth = float(ref["jamb_depth_mm"]) * scale
            if frame_depth > host_depth + .001:
                raise ValueError(f"{item['id']}: selected frame is deeper than the host wall")
            into_side = w.orthogonal().normalize() * (1 if t > 0 else -1)
            target += into_side * ((host_depth-frame_depth)/2)
    matrix=Matrix44.chain(Matrix44.translate(-source.x,-source.y,0),Matrix44.z_rotate(-v.angle),
                          Matrix44.scale(scale,scale*mirror,1),Matrix44.z_rotate(w.angle),
                          Matrix44.translate(target.x,target.y,0))
    drawn=[]
    for e in recursive_decompose(block):
        if file == BUNDLED and e.dxf.layer == GUIDES:continue
        if e.dxftype() in {"TEXT","MTEXT","ATTDEF","ATTRIB","POINT"}:continue
        if e.dxftype() not in {"LINE","ARC","CIRCLE","ELLIPSE","LWPOLYLINE","POLYLINE","SPLINE"}:
            raise ValueError(f"{item['id']}: select a clean, plan-view line block ({e.dxftype()})")
        fresh=e.copy();fresh.transform(matrix);fresh.dxf.layer=layer;fresh.dxf.color=256
        fresh.dxf.discard('true_color');fresh.dxf.linetype="BYLAYER";fresh.dxf.lineweight=-1
        msp.add_entity(fresh);drawn.append(fresh)
    if not drawn:raise ValueError(f"{item['id']}: reference has no editable line geometry")
    return drawn
