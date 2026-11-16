"""Use actual quarter-arc trace pixels and the open leaf tip to infer an opening axis.

The fit chooses among all eight orthogonal swing poses in original x-right,
y-down pixels. Never infer a host, wall thickness or exact jamb width from an
unclear arc. The input points must come from the original, before seeing CAD.
"""
import argparse
import json
import math
from pathlib import Path
import numpy as np
from scipy.optimize import least_squares
from PIL import Image, ImageDraw

VECTORS=np.array([(1,0),(0,1),(-1,0),(0,-1)],dtype=float)


def fit(trace,tip,band):
    points=np.array(trace,dtype=float);tip=np.array(tip,dtype=float)
    if points.ndim!=2 or points.shape[1]!=2 or len(points)<3 or tip.shape!=(2,) or not (np.isfinite(points).all() and np.isfinite(tip).all() and 0<band):
        raise ValueError('Supply at least three different arc pixels from closed end through midarc to open end, the visible open leaf tip, and a positive source uncertainty')
    chord=points[-1]-points[0]
    if np.linalg.norm(chord)<=3*band:
        raise ValueError('The observed arc is too small relative to its pixel uncertainty')
    candidates=[]
    for i,opened in enumerate(VECTORS):
        for j in ((i-1)%4,(i+1)%4):
            along=VECTORS[j];initial_r=np.linalg.norm(chord)/math.sqrt(2)
            def errors(z):
                center=z[:2];r=z[2];rad=points-center
                nearest=np.hypot(rad@opened,rad@along)-r
                # The sample supplied first is the observed end on the closing line;
                # last is the end approached by the opened leaf. Avoid order ambiguity.
                return np.r_[nearest,points[0]-(center+r*along),points[-1]-(center+r*opened),
                        tip-(center+r*opened),np.minimum(rad@along,0),np.minimum(rad@opened,0)]
            solution=least_squares(errors, [*(tip-opened*initial_r),initial_r],
                bounds=([-np.inf,-np.inf,band],[np.inf,np.inf,np.inf]),loss='soft_l1',f_scale=band)
            center=solution.x[:2];radius=solution.x[2];resid=errors(solution.x)
            candidates.append({'pivot_px':np.round(center,3).tolist(),'radius_px':round(radius,3),
                'expected_closed_tip_px':np.round(center+along*radius,3).tolist(),
                'expected_open_tip_px':np.round(center+opened*radius,3).tolist(),
                'wall_axis':'H' if along[0] else 'V','closed_direction':along.tolist(),'open_direction':opened.tolist(),
                'max_error_px':round(float(np.max(np.abs(resid))),3),'mean_abs_error_px':round(float(np.mean(np.abs(resid))),3)})
    candidates.sort(key=lambda q:(q['mean_abs_error_px'],q['max_error_px']))
    winner=candidates[0]
    # Never pass an almost straight fragment fitted to a corner with no curvature evidence.
    discr=np.max(abs(chord[0]*(points[:,1]-points[0,1])-chord[1]*(points[:,0]-points[0,0])))/np.linalg.norm(chord)
    good=winner['max_error_px']<=band and discr>=band and candidates[1]['mean_abs_error_px']-winner['mean_abs_error_px']>band
    return {'status':'supported' if good else 'inspect','uncertainty_px':band,'sagitta_px':round(float(discr),3),
        'chosen':winner,'runner_up':candidates[1],'limits':'拟合源图弧与开启扇的图示轴向。铰支位置、洞口净端和平移到实际墙轴时须另看。弧不可见或被遮挡时按通行与其它图证登记推断。'}


def sheet(image,record,report):
    src=Image.open(image).convert('RGB');p=report['chosen'];ori=np.array(p['pivot_px'])
    rad=p['radius_px'];mark=np.array(record['arc_px']+[record['open_tip_px']])
    xmin,ymin=np.minimum(mark.min(axis=0),ori-rad*.2)-8
    xmax,ymax=np.maximum(mark.max(axis=0),ori+rad*.2)+8
    box=(max(0,math.floor(xmin)),max(0,math.floor(ymin)),min(src.width,math.ceil(xmax)),min(src.height,math.ceil(ymax)))
    crop=src.crop(box);scale=min(6,440/max(crop.size));sz=[max(1,round(n*scale)) for n in crop.size]
    raw=crop.resize(sz,Image.Resampling.NEAREST);right=raw.copy();dr=ImageDraw.Draw(right)
    xy=lambda v:((v[0]-box[0]+.5)*scale,(v[1]-box[1]+.5)*scale)
    closed=np.array(p['closed_direction']);opened=np.array(p['open_direction'])
    arc=[xy(ori+rad*(closed*math.cos(t)+opened*math.sin(t))) for t in np.linspace(0,math.pi/2,45)]
    dr.line(arc,fill='#e35f32',width=2);dr.line([xy(ori),xy(p['expected_open_tip_px'])],fill='#247ab0',width=2)
    for v in mark:
        x,y=xy(v);dr.ellipse((x-2,y-2,x+2,y+2),fill='#14804f')
    out=Image.new('RGB',(2*sz[0],sz[1]),'white');out.paste(raw,(0,0));out.paste(right,(sz[0],0));return out


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('image','marks','out'):p.add_argument(name,type=Path)
    a=p.parse_args();records=json.loads(a.marks.read_text());a.out.mkdir(exist_ok=True,parents=True)
    results=[]
    for index,record in enumerate(records,1):
        found=fit(record['arc_px'],record['open_tip_px'],record['uncertainty_px'])
        filename=f'{index:03d}.png';sheet(a.image,record,found).save(a.out/filename)
        results.append(dict(id=record['id'],panel=filename,**found))
    (a.out/'swing-fits.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
