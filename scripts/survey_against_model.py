"""Locate image-discovered wall, gap and parallel-line candidates that differ from saved CAD.

Reads an existing image-only survey. Results are search locations; a drafter must
decide whether the crop contains wall material, joinery, floor texture or furniture.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
from dimension_grid import GridProjection
import ezdxf
from shapely.geometry import LineString, Point
from shapely.ops import unary_union
from PIL import Image, ImageDraw
from check_wall_probes import decode

def compare(doc,reg,survey,tones):
    projection=GridProjection(reg)
    roles=reg['layers']
    byrole={}
    issues={}
    for role in ('bearing','partition'):
        name=roles[role]
        geometries,unclear,issues[role],_=decode(doc,{'wall_layers':{name:'fills'},
            'nonwall_fill_layers':[layer for layer in roles.values() if layer!=name]})
        byrole[role]=geometries
        if unclear:issues[role].append('有不能解码的墙轮廓')
    walls=unary_union(list(byrole.values()))
    rows=[]
    def assess(feature,group):
        p,q=feature['a_px'],feature['b_px']
        ori=feature['orientation']
        if group=='pair':
            normal=(p[1]+q[1])/2 if ori=='H' else (p[0]+q[0])/2
            if ori=='H':p,q=(p[0],normal),(q[0],normal)
            else:p,q=(normal,p[1]),(normal,q[1])
        line=LineString(projection.world_path([p,q]))
        scale=max(math.dist(projection.world((x,y)),projection.world((x+dx,y+dy)))
                  for x,y in (p,q) for dx,dy in ((1,0),(-1,0),(0,1),(0,-1)))
        if line.length<1:return
        # Exclude endpoint jambs; defer to the crop for exact ends and supported frames.
        trimmed=LineString([line.interpolate(t,normalized=True) for t in (.15,.85)])
        material=trimmed.intersection(walls).length/trimmed.length
        buf=trimmed.buffer(scale*.65,cap_style='flat')
        cov={k:round(geom.intersection(buf).area/buf.area,3) for k,geom in byrole.items()}
        chosen=tones.get(feature.get('tone'))
        if group=='core':
            score=1-(cov[chosen] if chosen else max(cov.values()))
            meaning='色带可能为实墙，检查 CAD 实体和墙层'
        else:
            score=material
            meaning='断口或平行细线后有实墙，检查窗、推拉扇、开放边或交叉墙'
        rows.append({'source_id':feature['id'],'class':group,'line_px':[p,q],'extent_mm':round(line.length),
            'material_center_fraction':round(material,3),'side_band_fractions':cov,'decision':meaning,
            'contrast_only':round(score,3),'source_stability':feature.get('line_evidence'),
            'source_tone':feature.get('tone'),'source_swing':feature.get('swing')})
    for group,items in [('core','wall_band_candidates'),('gap','gap_candidates'),('pair','parallel_pairs')]:
        for feature in survey[items]:
            assess(feature,group)
    return sorted(rows,key=lambda row:(-row['contrast_only'],-row['extent_mm'])),issues

def show(source,rows,path):
    scope=Image.open(source).convert('RGB')
    cells=[]
    for row in rows:
        (x,y),(xx,yy)=row['line_px']
        pad=max(16,min(70,math.dist((x,y),(xx,yy))*.35))
        box=(max(0,math.floor(min(x,xx)-pad)),max(0,math.floor(min(y,yy)-pad)),
            min(scope.width,math.ceil(max(x,xx)+pad)), min(scope.height,math.ceil(max(y,yy)+pad)))
        tile=scope.crop(box)
        local=Image.new('RGB',(216,167),'white');tile.thumbnail((212,135),Image.Resampling.NEAREST)
        local.paste(tile,((216-tile.width)//2,25))
        ImageDraw.Draw(local).text((5,4),f"{row['source_id']} / {row['class']} / {row['contrast_only']:.2f}",fill='black')
        cells.append(local)
    for i in range(0,len(cells),15):
        page=Image.new('RGB',(3*216,math.ceil(min(15,len(cells)-i)/3)*167),'white')
        for j,tile in enumerate(cells[i:i+15]):page.paste(tile,(j%3*216,j//3*167))
        page.save(path/f'{i//15+1:03d}.png')

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('source','survey','readback','registration','out'):p.add_argument(name,type=Path)
    p.add_argument('--tone',action='append',default=[],help='confirmed image tone=CAD role e.g. dark=bearing')
    args=p.parse_args()
    data=json.loads(args.survey.read_text())
    if hashlib.sha256(args.source.read_bytes()).hexdigest()!=data['sha256']:p.error('image differs from source-only survey')
    tones=dict(item.split('=',1) for item in args.tone)
    if any(value not in ('bearing','partition') for value in tones.values()):p.error('invalid role')
    rows,issues=compare(ezdxf.readfile(args.readback),json.loads(args.registration.read_text()),data,tones)
    args.out.mkdir(exist_ok=True,parents=True)
    (args.out/'comparison.json').write_text(json.dumps({'source_sha256':data['sha256'],'decode':issues,'observations':rows},ensure_ascii=False,indent=2))
    show(args.source,[row for row in rows if row['contrast_only']>=.45],args.out)
    print(len(rows),'raw-image candidates compared')
