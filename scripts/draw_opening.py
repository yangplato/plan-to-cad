"""Place door/window symbols from the same WCS-millimetre a,b records used to cut walls."""
import math
from ezdxf.math import Vec2


def draw_opening(msp, item, door_layer="门", window_layer="窗", ancillary_layer="建筑附属"):
    """a,b = jamb centres in mm, in the author's registered order; toward = point in room of swing.
    kind swing: toward and leaves=[{hinge:"a"/"b", share:1.0, radius_mm:optional}].
    kind sliding: panels=[{start:0.0,end:0.55,track:0,arrow:1/-1/0},...], tracks_mm=[-15,15].
    kind window/rail: lines_mm=parallel offsets; symbol_depth_mm is the end-frame depth.
    """
    start, end = Vec2(item["a"]), Vec2(item["b"])
    span = (end - start).magnitude
    if span <= 0 or msp.doc.units != 4:
        raise ValueError(f"{item['id']}: expected two distinct mm jambs")
    axis = (end - start).normalize()
    left = Vec2(-axis.y, axis.x)
    kind = item["kind"]
    layer = {"swing": door_layer, "sliding": door_layer,
             "window": window_layer, "rail": ancillary_layer, "open": door_layer}[kind]
    if layer not in msp.doc.layers:
        raise ValueError(f"Missing layer: {layer}")
    attrs = {"layer": layer}

    def path(points, close=False):
        return msp.add_lwpolyline(points, close=close, dxfattribs=attrs)

    if kind == "open":
        return []
    drawn = []
    depth = float(item.get("symbol_depth_mm", item.get("wall_thickness_mm", 0)))
    if depth <= 0:
        raise ValueError(f"{item['id']}: record the host/frame depth")
    if kind == "swing":
        leaves = item["leaves"]
        if (not 1 <= len(leaves) <= 2 or len({leaf["hinge"] for leaf in leaves}) != len(leaves)
                or abs(sum(float(leaf["share"]) for leaf in leaves) - 1) > .001):
            raise ValueError(f"{item['id']}: give each leaf's jamb and fraction of the clear opening")
        for leaf in leaves:
            hinge, share = leaf["hinge"], float(leaf["share"])
            if hinge not in ("a", "b") or not 0 < share <= 1:
                raise ValueError(f"{item['id']}: invalid hinge or leaf share")
            jamb = start if hinge == "a" else end
            along = axis if hinge == "a" else -axis
            toward = Vec2(leaf["toward"] if "toward" in leaf else item["toward"])
            side = (toward - jamb).dot(left)
            if abs(side) < 0.001:
                raise ValueError(f"{item['id']}: identify a point inside the destination room")
            into = left if side > 0 else -left
            inset = float(leaf.get("jamb_inset_mm", item.get("jamb_inset_mm", 0)))
            radius = float(leaf.get("radius_mm", span * share - 2 * inset))
            thickness = float(leaf.get("thickness_mm", 35))
            if inset < 0 or not thickness < radius <= span * share + 1e-6:
                raise ValueError(f"{item['id']}: verify the door opening and leaf radius")
            face = 1
            if item.get("flush_door_face"):
                # Set both arc and leaf on the chosen opening face (blue-frame convention).
                face = {"approach":-1,"receiving":1}[leaf.get("hinge_face",item.get("hinge_face","approach"))]
            pivot = jamb + along * inset + into * (face * depth / 2)
            if "actual_pivot_mm" in leaf:
                pivot = Vec2(leaf["actual_pivot_mm"])
            tip = pivot + into * radius
            drawn.append(path([pivot, tip, tip + along * thickness, pivot + along * thickness], True))
            a0, a1 = along.angle_deg, into.angle_deg
            if along.det(into) < 0:
                a0, a1 = a1, a0
            drawn.append(msp.add_arc(pivot, radius, a0 % 360, a1 % 360, dxfattribs=attrs))
        if item.get("flush_door_face"):
            return drawn
        for pt in (start, end):
            drawn.append(path([pt - left*depth/2, pt + left*depth/2]))
        return drawn
    if kind == "sliding":
        offsets = item["tracks_mm"]
        panels = item["panels"]
        if not panels or not offsets:
            raise ValueError(f"{item['id']}: register visible panels and track positions")
        for offset in offsets:
            drawn.append(path([start+left*offset, end+left*offset]))
        thickness = float(item.get("panel_thickness_mm", 24))
        for panel in panels:
            i, j = float(panel["start"]), float(panel["end"])
            if not (0 <= i < j <= 1) or thickness <= 0:
                raise ValueError(f"{item['id']}: invalid panel interval or thickness")
            offset = offsets[panel["track"]]
            p = start + axis*(i*span) + left*offset
            q = start + axis*(j*span) + left*offset
            h = left * (thickness/2)
            drawn.append(path([p-h,q-h,q+h,p+h],True))
            arrow = panel.get("arrow",0)
            if arrow not in (-1,0,1):
                raise ValueError("arrow is relative to the registered a→b direction")
            if arrow:
                length = min(140.0,(q-p).magnitude*.25)
                tail = (p+q)*.5-axis*(length*arrow*.5)
                head = tail+axis*(length*arrow)
                drawn.extend((path([tail,head]),path([head-axis*(length*arrow*.28)+left*length*.14,head,
                                                     head-axis*(length*arrow*.28)-left*length*.14])))
    else:
        offsets = item["lines_mm"]
        if len(offsets)<2 or max(offsets)-min(offsets)>depth+1e-6:
            raise ValueError(f"{item['id']}: verify frame and parallel lines")
        for offset in offsets:
            drawn.append(path([start+left*offset,end+left*offset]))
    low,high = item.get("frame_face_offsets_mm",[-depth/2,depth/2])
    for point in (start,end):
        drawn.append(path([point+left*low,point+left*high]))
    return drawn
