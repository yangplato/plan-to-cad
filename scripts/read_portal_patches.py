"""Create local source-first cards for the whole set of windows, doors and free edges.

A drafter and a source-only reader may disagree. Include both declared positions.
Contours and possible doors are evidence to examine; the programme never draws a door.
"""
import argparse,json,math,hashlib
from pathlib import Path
import numpy as np
from PIL import Image,ImageDraw,ImageOps
from scipy.ndimage import gaussian_filter
from compare_source_readers import compare
from door_patch_card import analyze,build_card


def crops(src,box):
    tile=src.crop(box)
    mono=np.asarray(tile.convert('L'),dtype='float32')
    shade=gaussian_filter(mono,max(2.5,min(tile.size)/14))
    local=Image.fromarray(np.uint8(np.clip(140+(mono-shade)*5,0,255))).convert('RGB')
    contrast=ImageOps.autocontrast(tile.convert('L'),cutoff=(4,3)).convert('RGB')
    return [tile,contrast,local]

def triple(src,box,q,dst):
    views=crops(src,box);scale=min(6,318/max(views[0].size))
    w,h=[max(1,round(v*scale)) for v in views[0].size]
    out=Image.new('RGB',(w*3,h+32),'white');d=ImageDraw.Draw(out)
    for i,tile in enumerate(views):
        d.text((i*w+2,3),('RAW','CONTRAST','EDGE')[i]+'   '+str(q),fill='black')
        out.paste(tile.resize((w,h),Image.Resampling.NEAREST),(i*w,32))
    out.save(dst)

def build(src,first,second,band,scale,out):
    rows=compare(first['portals'],second['portals'],band)
    for idx,row in enumerate(rows,1):
        obs=[o for o in (row['first'],row['second']) if o]
        all_points=[np.array(t) for o in obs for t in (o['a'],o['b'])]
        for o in obs:
            for f in o.get('leaves',[]):all_points.extend(np.array(f[t]) for t in ('hinge','tip'))
        points=np.array(all_points);lo=points.min(0);hi=points.max(0);r=max(np.linalg.norm(np.array(o['a'])-o['b']) for o in obs)
        # Keep neighbouring floor, perpendicular returns and a full possible opposite-facing leaf in view.
        c=(hi+lo)/2;half=np.maximum((hi-lo)/2+9,np.minimum(1100/scale,r*.95))
        def bound(lo,hi):
            lo=np.maximum(0,np.floor(lo)).astype(int);hi=np.minimum(src.size,np.ceil(hi)).astype(int)
            return [int(lo[0]),int(lo[1]),int(hi[0]),int(hi[1])]
        box=bound(c-half,c+half);folder=out/f'{idx:03d}';folder.mkdir()
        triple(src,box,idx,folder/'source.png')
        result={'index':idx,'source_box_px':box,'originals':obs,'disagreement':row['issues']}
        if any(o['kind']=='swing' for o in obs):
            # Hint only the vicinity, test both orthogonal axes even if the two readers agree.
            jambs=np.array([p for o in obs for p in (o['a'],o['b'])]);jlo=jambs.min(0);jhi=jambs.max(0)
            hinge_box=bound(jlo-max(3,band*1.7),jhi+max(3,band*1.7))
            spans=[list(o['a'])+list(o['b']) for o in obs]
            multi=any(len(o.get('leaves',[]))>1 for o in obs)
            interval=(max(scale*3,min(260 if multi else 480,r*scale*.55)),max(750,min(1450,r*scale*1.22)))
            report=analyze(src,hinge_box,scale,interval,40,1.6,openings=spans)
            (folder/'traces.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
            build_card(src,report,5).save(folder/'choices.png')
            result['signal_assessment']=report['status']
        yield result

if __name__=='__main__':
    cli=argparse.ArgumentParser(description=__doc__)
    for n in ('image','first','second','out'):cli.add_argument(n,type=Path)
    cli.add_argument('--band-px',type=float,required=True);cli.add_argument('--mm-per-px',type=float,required=True)
    a=cli.parse_args();src=ImageOps.exif_transpose(Image.open(a.image)).convert('RGB');digest=hashlib.sha256(a.image.read_bytes()).hexdigest()
    docs=[json.loads(p.read_text()) for p in (a.first,a.second)]
    if any(d['source_sha256']!=digest for d in docs):cli.error('Use independent inventories from this source')
    if a.out.exists() and any(a.out.iterdir()):cli.error('Choose an empty output directory')
    a.out.mkdir(exist_ok=True,parents=True)
    results=list(build(src,*docs,a.band_px,a.mm_per_px,a.out))
    (a.out/'index.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
    print(len(results),'source areas prepared')
