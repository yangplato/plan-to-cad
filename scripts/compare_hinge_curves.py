"""Compare the observed curvature of a raster door symbol with both physical jambs.

Provide source-only portal data with endpoints and proposed hinges. Samples do not
use a CAD drawing or a judge's landmarks. The result guides a visual decision.
"""
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageOps
from scipy.ndimage import map_coordinates

from compare_source_readers import compare

ANGLES = np.deg2rad(np.linspace(12.,78.,36))


def profile(gray, xy, radial, r):
    """Two flanks distinguish a pencil stroke from a broad brightness gradient."""
    distance = min(3.5, max(1.6, .07*r))
    def sample(offset):
        pts=xy+offset*radial
        return map_coordinates(gray,[pts[:,1],pts[:,0]],order=1,mode='constant',cval=np.nan,prefilter=False)
    mid=sample(0.)
    flank1,flank2=sample(-distance),sample(distance)
    def evaluate(side):
        evidence=np.minimum((flank1-mid)*side,(flank2-mid)*side)
        bins=np.array([np.median(s) for s in np.array_split(evidence,6)])
        if not np.isfinite(bins).all():return (-1.,0,[])
        score=float(np.median(bins))
        return (score,int(np.count_nonzero(bins>3.)),list(np.round(bins,1)))
    return max((evaluate(1),evaluate(-1)),key=lambda m:m[:2])


def compare_jambs(gray,a,b,band):
    a,b=np.asarray(a,dtype=float),np.asarray(b,dtype=float)
    length=np.linalg.norm(b-a)
    along=(b-a)/length;normal=np.array([-along[1],along[0]])
    variable = np.unique(np.r_[0.,np.linspace(-band,band,5)])
    report=[]
    for end,pt,axis in [('A',a,along),('B',b,-along)]:
        for side,perp in [('+',normal),('-',-normal)]:
            radial=axis* np.cos(ANGLES)[:,None]+perp*np.sin(ANGLES)[:,None]
            best=None
            for dt in variable:
                for dn in variable:
                    pivot=pt+dt*along+dn*normal
                    for adjust in np.linspace(-min(.2*length,2*band),min(.2*length,2*band),9):
                        radius=length+adjust
                        trace=pivot+radius*radial
                        score,sectors,vals=profile(gray,trace,radial,radius)
                        candidate=(score,sectors,-abs(dt)-abs(dn)-abs(adjust))
                        if best is None or candidate>best[0]:
                            best=(candidate,dict(hinge=end,side=side,pivot_px=pivot.tolist(),radius_px=round(radius,3),
                                median_flank_contrast=round(score,2),sectors=sectors,sector_contrasts=vals,
                                closed_direction=axis.tolist(),open_direction=perp.tolist()))
            report.append(best[1])
    return sorted(report,key=lambda j:(-j['median_flank_contrast'],-j['sectors']))


def panel(img,rows,path):
    marks=[];curves=[]
    for m in rows:
        p=np.array(m['pivot_px']);r=m['radius_px'];u=np.array(m['closed_direction']);v=np.array(m['open_direction'])
        t=np.linspace(0,np.pi/2,70)[:,None];arc=p+r*(u*np.cos(t)+v*np.sin(t))
        curves.append((p,p+r*v,arc));marks.extend([p,p+r*v,*arc])
    boxlo=np.maximum(0,np.floor(np.min(marks,axis=0)-5)).astype(int)
    boxhi=np.minimum(img.size,np.ceil(np.max(marks,axis=0)+6)).astype(int)
    tile=img.crop((*boxlo,*boxhi));sc=min(6.,230/max(tile.size))
    size=tuple(max(1,round(v*sc)) for v in tile.size)
    tile=tile.resize(size,Image.Resampling.NEAREST);w,h=size
    sheet=Image.new('RGB',(w*5,h+28),'white')
    for i,title in enumerate(['SOURCE']+[f"{r['hinge']}{r['side']}  {r['median_flank_contrast']:.1f}  {r['sectors']}/6" for r in rows]):
        sheet.paste(tile,(i*w,28));dr=ImageDraw.Draw(sheet);dr.text((i*w+4,6),title,fill='#223640')
        if not i:continue
        p,t,arc=curves[i-1]
        def xy(q):return (i*w+(q[0]-boxlo[0]+.5)*sc,28+(q[1]-boxlo[1]+.5)*sc)
        dr.line([xy(p),xy(t)],fill='#2191a4',width=1)
        # Interrupt the curve so the actual source pixels are still visible beneath it.
        for j in range(0,len(arc)-2,6):dr.line([xy(pt) for pt in arc[j:j+3]],fill='#e74120',width=1)
    sheet.save(path)


def main(image, first, second, out, band):
    if band<=0 or band>24:raise ValueError('Use the original image edge uncertainty in pixels')
    out.mkdir(parents=True,exist_ok=False)
    sha=hashlib.sha256(image.read_bytes()).hexdigest()
    records=[json.loads(p.read_text()) for p in (first,second)]
    if any(s['source_sha256']!=sha for s in records):raise ValueError('The two readings require the same source image')
    img=ImageOps.exif_transpose(Image.open(image)).convert('RGB');gray=np.asarray(img.convert('L'),dtype=float)
    cases=[]
    for i,m in enumerate(compare(records[0]['portals'],records[1]['portals'],band),1):
        alternatives=[r for r in (m['first'],m['second']) if r]
        if not any(r['kind'] in ('swing','open') for r in alternatives):continue
        anchors=[]
        for r in alternatives:
            ab=np.array([r['a'],r['b']],dtype=float)
            if anchors and np.linalg.norm(anchors[0]-ab[::-1]) < np.linalg.norm(anchors[0]-ab):ab=ab[::-1]
            anchors.append(ab)
        a,b=np.mean(anchors,axis=0)
        if np.linalg.norm(a-b)<10:continue
        rows=compare_jambs(gray,a,b,band)
        fn=f'{i:03d}.png';panel(img,rows,out/fn)
        cases.append({'ids':[r['id'] for r in alternatives],'source_span_px':[a.tolist(),b.tolist()],
            'reader_issues':m['issues'],'panel':fn,'candidates':rows})
    (out/'curvature.json').write_text(json.dumps({'source_sha256':sha,'locations':cases},ensure_ascii=False,indent=2))
    return cases


if __name__=='__main__':
    arg=argparse.ArgumentParser(description=__doc__)
    for field in ('image','first','second','out'):arg.add_argument(field,type=Path)
    arg.add_argument('--band-px',type=float,required=True)
    a=arg.parse_args();print(len(main(a.image,a.first,a.second,a.out,a.band_px)),'source comparison cards')

