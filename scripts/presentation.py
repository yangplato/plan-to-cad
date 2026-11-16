"""Project layer names and source-preserving two-row perimeter dimensions."""
import math
from drafting_version import version

from ezdxf import bbox
from ezdxf.math import Vec2
from dimension_grid import GridProjection

LEGACY_NUMBERED = dict(bearing="01-承重墙", partition="02-非承重墙", door="03-门", window="04-窗",
                movable="05-活动家具", fixed="06-固定家具及设备", ancillary="07-建筑附属",
                dimension="08-尺寸标注")
CURRENT = dict(bearing="A-WALL-DARK",partition="A-WALL-LITE",door="A-DOOR",window="A-DOOR-WIND",
               movable="A-FURN-MOVE",fixed="A-FURN-FIXD",ancillary="A-FLOR-AUXI",dimension="A-ANNO-DIMS")
# Keep the exported semantic mapping name used by existing callers. Frozen
# pre-1.2 build scripts must import LEGACY_NUMBERED explicitly.
NUMBERED = CURRENT
LEGACY = dict(bearing="承重墙", partition="构造墙", door="门", window="窗",
              movable="活动家具", fixed="固定家具及设备", ancillary="建筑附属", dimension="尺寸")
SIDES = {"north":(0,1), "south":(0,-1), "west":(1,-1), "east":(1,1)}
APP = "PLAN_PERIMETER"
# These are project conventions, not asserted national-standard layer names.
# In V1.2 the room layer contains model TEXT; the other three are paper-only.
PAPER_LAYERS = {"frame":"A-ANNO-TTLB-FRAM", "title":"A-ANNO-TTLB-TEXT", "rooms":"C-FLOR-IDEN",
                "legend":"A-ANNO-SYMB", "viewport":"A-ANNO-WINS"}
LEGACY_PAPER_LAYERS = {"frame":"PS-图框", "title":"PS-图签", "rooms":"PS-房名", "legend":"PS-图例"}
SOURCE_LAYER="A-XREF"
LEGACY_SOURCE_LAYER="10-原图参照"
SHEET_APP = "PLAN_SHEET"
CHAIN_MM, ROW_MM, STEM_MM = 13., 10., 9.  # paper millimetres


def current(scene):
    return version(scene) >= (0, 12)


def roles(scene):
    if version(scene)>=(1,2):return dict(CURRENT)
    if current(scene):return dict(LEGACY_NUMBERED)
    return {**LEGACY, **scene.get("layers",{})}


def modern_layers(layers):
    return layers.get('bearing')==CURRENT['bearing']


def resolve_roles(doc,declared=None):
    """Read a known schema without renaming an existing drawing."""
    if declared:return dict(declared)
    for mapping in (CURRENT,LEGACY_NUMBERED,LEGACY):
        if any(name in doc.layers for name in mapping.values()):return dict(mapping)
    return dict(CURRENT)


def annotation_layers(layers):
    return dict(PAPER_LAYERS if modern_layers(layers) else LEGACY_PAPER_LAYERS)


def layer_spaces(doc):
    """Count actual content by space; a paper-only layer is not an empty layer."""
    from collections import Counter
    model=Counter(e.dxf.layer.casefold() for e in doc.modelspace())
    paper=Counter(e.dxf.layer.casefold() for layout in doc.layouts if layout.name!='Model' for e in layout)
    blocks=Counter(e.dxf.layer.casefold() for block in doc.blocks if not block.is_any_layout for e in block)
    return [dict(name=layer.dxf.name,model=model[layer.dxf.name.casefold()],
                 paper=paper[layer.dxf.name.casefold()],blocks=blocks[layer.dxf.name.casefold()],
                 off=layer.is_off(),frozen=layer.is_frozen(),plot=bool(layer.dxf.plot),aci=layer.dxf.color,
                 system=layer.dxf.name.casefold() in {'0','defpoints','viewports'},
                 empty=not(model[layer.dxf.name.casefold()]+paper[layer.dxf.name.casefold()]+blocks[layer.dxf.name.casefold()]))
            for layer in doc.layers]


def prune_empty_layer_definitions(doc):
    """For a newly built drawing only; retain all actual and viewport references."""
    used=set()
    for entity in doc.entitydb.values():
        if not entity.is_alive:continue
        if entity.dxf.is_supported('layer'):used.add(entity.dxf.layer.casefold())
        if entity.dxftype()=='VIEWPORT':used.update(n.casefold() for n in entity.frozen_layers)
    removed=[]
    for layer in list(doc.layers):
        name=layer.dxf.name
        if name.casefold() not in {'0','defpoints','viewports'} and name.casefold() not in used:
            doc.layers.remove(name);removed.append(name)
    return removed


def tag(dim, side, kind, envelope):
    doc = dim.doc
    if APP not in doc.appids:
        doc.appids.add(APP)
    dim.set_xdata(APP, [(1000,side),(1000,kind),(1040,float(envelope))])


def position(dim, scale):
    """Keep dimension definition points anchored. Move only the baseline and visible stems."""
    side,kind,envelope = (t.value for t in dim.get_xdata(APP))
    axis,sign = SIDES[side]
    row = envelope + sign*scale*(CHAIN_MM + (ROW_MM if kind=="overall" else 0))
    point=list(dim.dxf.defpoint)
    point[1-axis]=row
    dim.dxf.defpoint=point
    style=dim.override()
    # Outer stems finish beyond the inner baseline; inner stems finish in open paper.
    style["dimfxlon"]=1
    style["dimfxl"]=STEM_MM*scale if kind=="segment" else 4*scale
    style["dimexo"]=3*scale
    style.render()


def perimeter(doc,scene,layers,style):
    from draw_plan_parts import add_mm_dimension
    msp=doc.modelspace()
    if modern_layers(layers):
        doc.layers.get(layers['dimension']).dxf.color=4
        for key in ('dimclrd','dimclre','dimclrt'):setattr(doc.dimstyles.get(style).dxf,key,256)
    visible=[e for e in msp if e.dxf.layer in (layers['bearing'],layers['partition'],layers['window'],layers['ancillary'])]
    ext=bbox.extents(visible)
    if not ext.has_data:
        raise ValueError("A perimeter needs measured model geometry")
    limits=dict(west=ext.extmin.x,east=ext.extmax.x,south=ext.extmin.y,north=ext.extmax.y)
    project=GridProjection(scene).world
    bounds=scene.get("overall_bounds_px")
    if bounds is not None and (len(bounds)!=4 or bounds[0]>=bounds[2] or bounds[1]>=bounds[3]):
        raise ValueError("overall_bounds_px must be [left,top,right,bottom] in original image pixels")
    lo,hi=(ext.extmin.xy,ext.extmax.xy) if bounds is None else (
        Vec2(project([bounds[0],bounds[3]])),Vec2(project([bounds[2],bounds[1]])))
    endpoints={"north":[(lo.x,hi.y),(hi.x,hi.y)],"south":[(lo.x,lo.y),(hi.x,lo.y)],
               "west":[(lo.x,lo.y),(lo.x,hi.y)],"east":[(hi.x,lo.y),(hi.x,hi.y)]}
    for side,pair in scene.get("overall_sides_px",{}).items():
        if side not in SIDES or len(pair)!=2:raise ValueError("Each overall side needs two source points")
        endpoints[side]=[project(p) for p in pair]
    if scene.get("overall_basis") is None:
        basis="户型建筑图元最外投影，模型量取" if bounds is None else "作者登记的原图外围控制线投影"
    else:basis=scene["overall_basis"]
    records=[]
    observed={s:[] for s in SIDES}
    def dimension(side,kind,p,q,evidence):
        axis,sign=SIDES[side];p,q=Vec2(p),Vec2(q);value=abs(q[axis]-p[axis])
        if not math.isfinite(value) or value<.001:raise ValueError(f"{side}: degenerate {kind} span")
        base=list(p);base[1-axis]=limits[side]+sign*50*(CHAIN_MM+(ROW_MM if kind=="overall" else 0))
        dim=add_mm_dimension(msp,p,q,base,90*axis,layer=layers["dimension"],dimstyle=style)
        tag(dim,side,kind,limits[side]);position(dim,50)
        records.append(dict(handle=dim.dxf.handle,side=side,kind=kind,measurement_mm=value,
                            p1_mm=list(p),p2_mm=list(q),basis=evidence))
    for chain in scene.get("dimension_chains",[]):
        if "grid_witness_mm" not in chain:
            raise ValueError("v0.12 requires calibrated dimension_grid witnesses")
        axis=0 if chain['axis']=='x' else 1
        line=chain["grid_dim_line_mm"][1-axis]
        side=chain.get("side")
        if side is None:
            side=min((s for s,(i,_) in SIDES.items() if i==axis),key=lambda s:abs(limits[s]-line))
        if side not in SIDES or SIDES[side][0]!=axis:
            raise ValueError(f"{chain['id']}: dimension side conflicts with its axis")
        pts=chain["grid_witness_mm"]
        for p,q in zip(pts,pts[1:]):
            dimension(side,"segment",p,q,"原营销图明标："+chain['id'])
            observed[side].append(sorted((p,q),key=lambda t:t[axis]))

    for side,(p,q) in endpoints.items():
        evidence=basis.get(side,"模型量取") if isinstance(basis,dict) else basis
        if side in scene.get('overall_sides_px',{}) and not scene.get('overall_basis'):
            evidence='本侧作者指定的建筑总端点之间的投影'
        if side not in scene.get("overall_sides_px",{}) and bounds is None and observed[side]:
            axis=SIDES[side][0]
            spans=sorted(observed[side],key=lambda pair:pair[0][axis])
            p=spans[0][0];q=max((span[1] for span in spans),key=lambda t:t[axis])
            has_gaps=any(abs(prev[1][axis]-nxt[0][axis])>.01 for prev,nxt in zip(spans,spans[1:]))
            evidence=("本侧最外两个已识读控制点之间的模型投影，含未标区间或重叠标注"
                      if has_gaps else "同侧连续分段的首末真实界线，等于分段和")
        dimension(side,"overall",p,q,evidence)
    return records
