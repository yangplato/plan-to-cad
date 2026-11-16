#!/usr/bin/env python3
"""Measure an actual furniture back against the room side of actual wall material.
Dry-run by default. --write-new creates a new DXF only after a complete fit.
"""
import argparse
import json
import math
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import ezdxf
from ezdxf import path as dxfpath
from ezdxf.math import Matrix44
from ezdxf.xclip import XClip
from shapely.affinity import translate
from shapely.geometry import LineString, Point, Polygon
from shapely.geometry.polygon import orient
from shapely.ops import unary_union
from check_wall_probes import decode

EPS = 1e-5  # geometric roundoff in mm, independent of raster registration

def require(ok, msg):
    if not ok:
        raise ValueError(msg)

def dot(a,b):
    return a[0]*b[0]+a[1]*b[1]

def vector(a,b):
    return (b[0]-a[0], b[1]-a[1])

def unit(v):
    n=math.hypot(*v)
    require(n>EPS,"零方向")
    return (v[0]/n,v[1]/n)

def curves(doc, addr):
    """Address starts at an actual modelspace INSERT, followed by member handles."""
    require(len(addr)>=2,"需要顶层INSERT句柄和实际图元句柄")
    here=doc.modelspace()
    transform=Matrix44()
    for h in addr[:-1]:
        obj=doc.entitydb.get(h)
        require(obj is not None and obj.dxf.owner==here.block_record_handle and obj.dxftype()=="INSERT",
                f"{h}: 块链不匹配")
        require(obj.mcount==1 and not obj.dxf.get('invisible',0),"阵列或隐藏块需人工拆分")
        clip=XClip(obj)
        require(not (clip.has_clipping_path and clip.is_clipping_enabled),"剪裁块需先核实完整物件")
        transform=obj.matrix44() @ transform
        here=doc.blocks.get(obj.dxf.name)
        require(here is not None and not here.block.is_xref and not here.block.is_xref_overlay,"缺块或外部参照")
    leaf=doc.entitydb.get(addr[-1])
    require(leaf is not None and leaf.dxf.owner==here.block_record_handle,"末级实体不属于该块")
    require(leaf.dxftype() in ('LINE','LWPOLYLINE','POLYLINE','ARC','ELLIPSE','CIRCLE','SPLINE'),
            "需选取真实边界线")
    require(not getattr(leaf,'has_width',False) and not leaf.dxf.get('invisible',0),"不能用隐藏线、宽线中心代替边界")
    return leaf,transform

def vertices(doc, spec, straight=False):
    entity,matrix=curves(doc,spec['path'])
    if 'segment' in spec:
        require(entity.dxftype() in ('LWPOLYLINE','POLYLINE'),"segment仅用于多段线")
        require(entity.dxftype()!='POLYLINE' or entity.is_2d_polyline,"仅支持二维多段线索引")
        idx=spec['segment']
        poly=list(entity.vertices_in_wcs() if entity.dxftype()=='LWPOLYLINE' else entity.points_in_wcs())
        require(type(idx) is int and 0<=idx<(len(poly) if entity.is_closed else len(poly)-1),"多段线边索引超界")
        bulge=entity[idx][4] if entity.dxftype()=='LWPOLYLINE' else entity.vertices[idx].dxf.get('bulge',0)
        require(abs(bulge)<1e-12,"设计接触边含弧，另选实际直段")
        vs=list(matrix.transform_vertices([poly[idx],poly[(idx+1)%len(poly)]]))
    else:
        vs=list(dxfpath.make_path(entity,segments=4).transform(matrix).flattening(0.05))
    require(len(vs)>=2 and max(v.z for v in vs)-min(v.z for v in vs)<EPS,"需要同一水平面的实体边")
    coords=[(v.x,v.y) for v in vs]
    if straight:
        chord=LineString((coords[0],coords[-1])); seq=LineString(coords)
        require(chord.length>EPS and seq.difference(chord.buffer(EPS)).is_empty and abs(seq.length-chord.length)<EPS,
                "所选线并非一条连续直边，选择直段或多段线边号")
    return coords

def footprints(doc, specs):
    polys=[]
    for spec in specs:
        if isinstance(spec,list):
            spec={'path':spec}
        # Several real line/curve entities may form a single closed physical perimeter.
        parts=spec.get('parts',[spec])
        coords=[]
        for part in parts:
            segment=vertices(doc,part)
            if part.get('reverse'):segment.reverse()
            require(not coords or math.dist(coords[-1],segment[0])<EPS,"实体外轮廓有断点")
            coords.extend(segment if not coords else segment[1:])
        require(len(coords)>=4 and math.dist(coords[0],coords[-1])<EPS,"实物占地须由独立选取的闭合实物边界组成")
        p=Polygon(coords)
        require(p.is_valid and p.area>EPS,"无效占地边界")
        polys.append(p)
    return unary_union(polys)

def wallsides(walls):
    for poly in getattr(walls,'geoms',[walls]):
        require(poly.geom_type=="Polygon","墙材须是面")
        poly=orient(poly,sign=1)
        for ring in [poly.exterior,*poly.interiors]:
            coords=list(ring.coords)
            for a,b in zip(coords,coords[1:]):
                if math.dist(a,b)<=EPS: continue
                tx,ty=unit(vector(a,b))
                yield a,b,(-ty,tx)  # solid material is on the left of every oriented ring

def merge(spans):
    result=[]
    for a,b in sorted(spans):
        if not result or a>result[-1][1]+EPS: result.append([a,b])
        else:result[-1][1]=max(result[-1][1],b)
    return result

def face_for(doc,spec,walls):
    pts=vertices(doc,spec,straight=True); p,q=pts[0],pts[-1]
    t=unit(vector(p,q)); n=(-t[1],t[0]); hint=unit(spec['toward_wall'])
    if dot(n,hint)<0:n=(-n[0],-n[1])
    require(dot(n,hint)>.90,"朝墙方向与所选背边不相容")
    guide=Point(spec['near_face_mm']); pick=float(spec['pick_radius_mm'])
    require(pick>0,"以实际墙面上的一点及正搜索半径选面")
    choices=[]
    for a,b,inward in wallsides(walls):
        if dot(n,inward)<1-1e-10 or abs(dot(vector(a,b),n))>EPS:continue
        dist=LineString((a,b)).distance(guide)
        choices.append((dot(vector(p,a),n),dist,sorted((dot(vector(p,a),t),dot(vector(p,b),t)))))
    eligible=sorted((dist,offset) for offset,dist,span in choices if dist<=pick)
    require(eligible,"指示点附近没有同向的平直实墙面；检查朝向、门窗和墙材")
    best,offset=eligible[0]
    require(all(abs(d-best)>EPS or abs(h-offset)<EPS for d,h in eligible),"墙面选点等距歧义；缩小搜索半径或换选点")
    return dict(id=spec['id'],p=p,q=q,t=t,n=n,length=math.dist(p,q),offset=offset,
                spans=merge(span for h,d,span in choices if abs(h-offset)<EPS))

def translation(faces):
    first=faces[0]; nx,ny=first['n'];h=first['offset']
    dx,dy=nx*h,ny*h
    for f in faces[1:]:
        x,y=f['n'];det=nx*y-ny*x
        if abs(det)>1e-8:
            dx=(h*y-ny*f['offset'])/det;dy=(nx*f['offset']-h*x)/det;break
    require(all(abs(dot(f['n'],(dx,dy))-f['offset'])<EPS for f in faces),
            "多个真实背边无法以同一次刚性平移对墙；拆分柜肢、重选直边或修改允许调整的家具")
    return (dx,dy)

def evidence(f,shift,limit):
    nn=f['n'];tt=f['t'];start=dot(shift,tt);end=start+f['length']
    residual=f['offset']-dot(shift,nn)
    overlap=[[max(a,start),min(b,end)] for a,b in f['spans'] if b>start and a<end]
    supported=sum(b-a for a,b in overlap);missing=[];cur=start
    for a,b in overlap:
        if a-cur>EPS:missing.append([cur-start,a-start])
        cur=b
    if end-cur>EPS:missing.append([cur-start,end-start])
    alternatives=[]
    for a,b in f['spans']:
        if b-a<f['length']-EPS:continue
        delta=min(max(start,a),b-f['length'])-start
        if abs(delta)<=limit+EPS and abs(delta)>EPS:
            alternatives.append({'slide_mm':round(delta,3),'add_world_xy_mm':[t*delta for t in tt]})
    return {'id':f['id'],'back_mm':round(f['length'],5),'normal_gap_mm':round(residual,5),
            'physical_contact_mm':round(supported if abs(residual)<EPS else 0,5),
            'unsupported_intervals_from_back_start_mm':[[round(v,4) for v in ab] for ab in missing],
            'back_intervals_over_solid_mm':[[round(a-start,4),round(b-start,4)] for a,b in overlap],
            'wall_solid_projected_mm':round(supported,5),
            'whole_straight_back':abs(residual)<EPS and f['length']-supported<EPS,
            'nearby_along_face_options':sorted(alternatives,key=lambda a:abs(a['slide_mm']))[:4]}

def audit(doc,settings,alternative=None):
    wall,ambiguous,issues,_=decode(doc,settings)
    require(not ambiguous and not issues,f"墙材不完整：{issues}, ambiguous={len(ambiguous)}")
    specs=settings['backs'];require(specs,"缺实际背边")
    roots={item['path'][0] for item in specs}
    physical=settings['physical_outlines']
    def root_handles(specs):
        return {part['path'][0] for s in specs for part in s.get('parts',[s])}
    require(root_handles(physical)==roots,"每个待移动INSERT均须有完整的实体外轮廓及设计背边")
    bodies=footprints(doc,physical)
    obstacles=footprints(doc,settings.get('obstacles',[]))
    faces=[face_for(doc,s,wall) for s in specs]
    dx,dy=translation(faces);limit=float(settings['max_slide_mm'])
    require(limit>=0 and float(settings['max_move_mm'])>=0,"调整上限不能为负")
    def state(delta):
        shape=translate(bodies,*delta)
        reports=[evidence(f,delta,limit) for f in faces]
        wall_area=shape.intersection(wall).area;obs_area=shape.intersection(obstacles).area
        return {'backs':reports,'body_in_wall_mm2':round(wall_area,5),
                'body_in_selected_obstacles_mm2':round(obs_area,5),
                'fit':all(r['whole_straight_back'] for r in reports) and wall_area<1e-4 and obs_area<1e-4}
    after=state((dx,dy)); suggestions=[]
    if not after['fit']:
        for row in after['backs']:
            for slide in row['nearby_along_face_options']:
                x,y=slide['add_world_xy_mm'];vec=(dx+x,dy+y)
                if math.hypot(*vec)>settings['max_move_mm']+EPS:continue
                if state(vec)['fit'] and vec not in suggestions:suggestions.append(vec)
    if alternative is not None:
        require(1<=alternative<=len(suggestions),"沿墙替代序号无效；先运行只读诊断")
        dx,dy=suggestions[alternative-1]
        after=state((dx,dy))
    return {'move_world_xy_mm':[dx,dy],'move_length_mm':math.hypot(dx,dy),'before':state((0,0)),
            'after_proposed_move':after,'within_allowed_move':math.hypot(dx,dy)<=settings['max_move_mm']+EPS,
            'verified_alternative_shifts_world_xy_mm':suggestions,'moving_insert_handles':sorted(roots)}

def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('drawing',type=Path);ap.add_argument('job',type=Path)
    ap.add_argument('--write-new',type=Path);ap.add_argument('--alternative',type=int);args=ap.parse_args()
    try:
        cfg=json.loads(args.job.read_text());doc=ezdxf.readfile(args.drawing);r=audit(doc,cfg,args.alternative)
        can=r['after_proposed_move']['fit'] and r['within_allowed_move']
        if args.write_new and can:
            dest=args.write_new
            require(dest.suffix.lower()=='.dxf' and not dest.exists(),"输出须为新的DXF")
            for h in r['moving_insert_handles']:doc.entitydb[h].translate(*r['move_world_xy_mm'],0)
            with TemporaryDirectory(dir=dest.parent) as loc:
                temp=Path(loc)/'confirm.dxf';doc.saveas(temp)
                again=audit(ezdxf.readfile(temp),cfg)
                require(again['before']['fit'],"从实际保存文件复量未通过")
                os.link(temp,dest)  # exclusive publication; never overwrite
                r['saved_reopened']=again['before']
        print(json.dumps(r,ensure_ascii=False,indent=2))
        return 0 if can else 1
    except (ValueError,TypeError,KeyError,IndexError,OSError,ezdxf.DXFError) as ex:
        print(str(ex),file=sys.stderr); return 2
if __name__=='__main__':
    sys.exit(main())
