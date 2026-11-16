"""Match two source-only portal inventories in original pixels before CAD authoring."""
import argparse
import json
from pathlib import Path
import numpy as np
from scipy.optimize import linear_sum_assignment
from PIL import Image, ImageDraw


def geometry(row):
    a,b=np.asarray(row['a'],dtype=float),np.asarray(row['b'],dtype=float)
    extent=np.linalg.norm(a-b)
    if extent<=0:raise ValueError('Portal endpoints must be distinct: '+row['id'])
    return a,b,(a+b)/2,extent


def compare(first, second, band):
    if not np.isfinite(band) or band<=0:raise ValueError('Use the local source-image reading uncertainty in pixels')
    first_points=[geometry(v) for v in first];second_points=[geometry(v) for v in second]
    cost=np.full((len(first),len(second)+len(first)),1.01)
    for i,(a,b,c,length) in enumerate(first_points):
        for j,(aa,bb,cc,len2) in enumerate(second_points):
            gap=np.linalg.norm(c-cc)
            reach=max(3*band,min(length,len2)*.55)
            normal_1=(a-b)/length;normal_2=(aa-bb)/len2
            orient=1-abs(normal_1@normal_2)
            if gap<=reach:cost[i,j]=min(3,gap/reach)+.15*orient
    pairs=linear_sum_assignment(cost) if len(first) else ([],[])
    used=set();records=[]
    for i,j in zip(*pairs):
        f=first[i];a,b,c,length=first_points[i]
        s=second[j] if j<len(second) and cost[i,j]<1.01 else None
        issues=[]
        if s is None:issues.append('only_first')
        else:
            used.add(j);aa,bb,cc,len2=second_points[j]
            if f['kind']!=s['kind']:issues.append('type')
            direct=max(np.linalg.norm(a-aa),np.linalg.norm(b-bb))
            reverse=max(np.linalg.norm(a-bb),np.linalg.norm(b-aa))
            if min(direct,reverse)>band:issues.append('jamb_positions')
            if abs((a-b)@(aa-bb)/(length*len2))<.85:issues.append('host_axis')
            f_leaves=f.get('leaves',[]);s_leaves=s.get('leaves',[])
            if len(f_leaves)!=len(s_leaves):issues.append('leaf_count')
            elif f_leaves:
                # Compare actual hinge and open-tip pixels, independent of how endpoints were named.
                dist=[[max(np.linalg.norm(np.array(v['hinge'])-q['hinge']),np.linalg.norm(np.array(v['tip'])-q['tip']))
                       for q in s_leaves] for v in f_leaves]
                aa_i,bb_i=linear_sum_assignment(dist)
                if any(dist[x][y]>band for x,y in zip(aa_i,bb_i)):issues.append('hinge_or_open_tip')
        records.append({'first':f,'second':s,'issues':issues})
    records.extend({'first':None,'second':s,'issues':['only_second']} for j,s in enumerate(second) if j not in used)
    return records


def frames(image,rows,out):
    with Image.open(image) as f:base=f.convert('RGB')
    for idx,rec in enumerate(rows,1):
        if not rec['issues']:continue
        both=[s for s in [rec['first'],rec['second']] if s is not None]
        points=np.array([p for s in both for p in (s['a'],s['b'],*[m for l in s.get('leaves',[]) for m in (l['hinge'],l['tip'])])])
        lo=np.floor(points.min(axis=0)-12);hi=np.ceil(points.max(axis=0)+13)
        l,t=np.maximum(lo,0);rr,bb=np.minimum(hi,base.size)
        crop=base.crop(tuple(map(int,(l,t,rr,bb))))
        if not crop.width or not crop.height:raise ValueError('A portal is outside the source image')
        sc=min(5,390/max(crop.size));sz=tuple(max(1,round(v*sc)) for v in crop.size)
        raw=crop.resize(sz,Image.Resampling.NEAREST);outim=Image.new('RGB',(3*sz[0],sz[1]+24),'white')
        xy=lambda p:((p[0]-l+.5)*sc,(p[1]-t+.5)*sc)
        for col,person in enumerate((None,rec['first'],rec['second'])):
            tile=raw.copy();dr=ImageDraw.Draw(tile)
            if person:
                dr.line([xy(person['a']),xy(person['b'])],fill='#f15b20',width=2)
                for leaf in person.get('leaves',[]):dr.line([xy(leaf['hinge']),xy(leaf['tip'])],fill='#1978a5',width=2)
            outim.paste(tile,(col*sz[0],24))
            ImageDraw.Draw(outim).text((col*sz[0]+2,3),('raw','first','second')[col],fill='black')
        outim.save(out/f'{idx:03d}.png')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('image','first','second','out'):p.add_argument(name,type=Path)
    p.add_argument('--band-px',type=float,required=True)
    args=p.parse_args();first=json.loads(args.first.read_text());second=json.loads(args.second.read_text())
    for o in [first,second]:
        if not isinstance(o.get('portals'),list) or not o['portals']:p.error('Both source inventories need portals')
    rows=compare(first['portals'],second['portals'],args.band_px)
    args.out.mkdir(exist_ok=True,parents=True);frames(args.image,rows,args.out)
    (args.out/'disagreements.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2))
    print(len(rows),'locations;',sum(bool(r['issues']) for r in rows),'to resolve from pixels')
