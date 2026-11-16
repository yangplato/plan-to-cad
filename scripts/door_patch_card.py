#!/usr/bin/env python3
"""Source-only orthogonal doorway evidence. Original displayed pixels, x right, y down."""
import argparse
import json
import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps
from scipy.optimize import least_squares
from scipy.ndimage import gaussian_filter, map_coordinates, maximum_filter, shift

VECT = np.array([(1, 0), (0, 1), (-1, 0), (0, -1)])
NAMES = ['right', 'down', 'left', 'up']
ANGLES = np.radians(np.arange(10, 81, 10))
QUADS = [(1, 1), (-1, 1), (-1, -1), (1, -1)]


def sample(im, pts):
    return map_coordinates(im, [pts[..., 1], pts[..., 0]], order=1, mode='constant', cval=np.nan,
                           prefilter=False)


def at_offset(im, x, y):
    return shift(im, (-y, -x), order=1, mode='nearest', prefilter=False)


def get_ink(src):
    return gaussian_filter(np.asarray(src.convert('L'), dtype=np.float32), .45)


def coarse(im, bounds, radii, stroke):
    """Vote at each possible hinge with oriented two-sided contrast. Cap isolated strong marks."""
    l, t, r, b = bounds
    yy, xx = im.shape
    dist = max(2., 1.0 + stroke * .65)
    candidates = []
    ridge = {}
    for sx, sy in QUADS:
        normals = [(sx * np.cos(a), sy * np.sin(a)) for a in ANGLES]
        for nx, ny in normals + [(1., 0.), (0., 1.)]:
            key = (round(nx * ny, 5), round(abs(nx), 5))
            if key not in ridge:
                a = at_offset(im, nx * dist, ny * dist) - im
                c = at_offset(im, -nx * dist, -ny * dist) - im
                ridge[key] = [np.clip(np.minimum(a * sign, c * sign), 0, 36)
                              for sign in (1, -1)]
        def votes(origin_x, origin_y, nx, ny, p):
            key = (round(nx * ny, 5), round(abs(nx), 5))
            dx, dy = round(origin_x), round(origin_y)
            # Read source outside the user box but never replicate outside the image into it.
            y0, y1 = max(t, -dy), min(b, yy-dy)
            x0, x1 = max(l, -dx), min(r, xx-dx)
            plane = np.zeros((b-t, r-l), np.float32)
            if y1>y0 and x1>x0:
                plane[y0-t:y1-t,x0-l:x1-l] = ridge[key][p][y0+dy:y1+dy,x0+dx:x1+dx]
            return plane
        for rad in radii:
            for pol in (0, 1):
                curved = [votes(rad*nx, rad*ny, nx, ny, pol) for nx, ny in normals]
                sectors = np.sort(np.stack(curved), axis=0)
                arc = np.mean(sectors[2:7], axis=0)
                for open_axis, (ox, oy) in enumerate(((sx,0),(0,sy))):
                    leaf = sum(votes(rad*f*ox, rad*f*oy, -oy, ox, pol) for f in (.24,.46,.64,.81)) * .25
                    score = arc * (.68 + .32*np.minimum(leaf/12, 1.))
                    peaks = (score == maximum_filter(score, size=7)) & (score>1.6)
                    y, x = np.nonzero(peaks)
                    for k in np.argsort(score[y,x])[-4:]:
                        candidates.append((float(score[y[k],x[k]]),float(x[k]+l),float(y[k]+t),
                                           float(rad),sx,sy,open_axis,1 if pol==0 else -1))
    candidates.sort(reverse=True)
    kept = []
    for row in candidates:
        _, x, y, r, sx, sy, axis, pol = row
        group = [old for old in kept if old[4:]==row[4:]]
        if len(group)>=7 or any(math.hypot(x-old[1],y-old[2])<max(2.5,.17*r) and
                                abs(r-old[3])<max(2.,.15*r) for old in group):
            continue
        kept.append(row)
    return kept


def profile(im, pts, normals, sign, width):
    """Signed stroke contrast requires both flanks, suppressing steps at area boundaries."""
    near = max(2., width*.65+1.)
    center = sample(im, pts)
    a, b = [sample(im, pts + normals*near*i) for i in (-1., 1.)]
    aa, bb = [sample(im, pts + normals*near*1.45*i) for i in (-1., 1.)]
    contrast = np.minimum(sign*((a+aa)/2-center), sign*((b+bb)/2-center))
    noise = .5 * (np.abs(a-aa) + np.abs(b-bb))
    return np.where(np.isfinite(contrast), contrast, -100.), np.nan_to_num(noise,nan=30.)


def summarize(values, noise):
    valid = values > -99.
    threshold = np.maximum(3., 1.25*noise)
    hits = values >= threshold
    sectors = [bool(np.mean(v)>=.45) for v in np.array_split(hits,6)]
    strength = np.mean(np.clip(values,0,36))*np.mean(hits)*sum(sectors)/6.
    return dict(contrast=round(float(np.median(values[valid])),1) if np.any(valid) else None,
                fraction=round(float(np.mean(hits)),2), sectors=''.join('1' if z else '0' for z in sectors),
                visible=round(float(np.mean(valid)),2), strength=round(float(strength),2)), hits


def vectors(sx,sy,axis):
    horizontal=np.array([sx,0.]); vertical=np.array([0.,sy])
    return (horizontal,vertical) if axis==1 else (vertical,horizontal)


def sweep(im, row, stroke):
    """Refine position and radius together using distributed arc and leaf samples."""
    _, x, y, r, sx, sy, axis, sign=row
    closed,opened=vectors(sx,sy,axis)
    angles=np.radians(np.linspace(8,82,28))
    unit=np.cos(angles)[:,None]*closed+np.sin(angles)[:,None]*opened
    fractions=np.linspace(.14,.88,22)
    shifts=np.array([(dx,dy,dr) for dx in (-1.,0.,1.) for dy in (-1.,0.,1.) for dr in (-1.25,0.,1.25)])
    def best(center,radius,offsets):
        piv=(center+offsets[:,:2])[:,None,:]
        rr=(radius+offsets[:,2])[:,None,None]
        av,an=profile(im,piv+rr*unit,unit,sign,stroke)
        lv,ln=profile(im,piv+rr*fractions[:,None]*opened,closed,sign,stroke)
        def quality(v,n):
            return np.mean(np.clip(v,0,25),axis=1)*(.22+.78*np.mean(v>=np.maximum(3.,n*1.25),axis=1))
        score=quality(av,an)*(.3+.7*np.minimum(quality(lv,ln)/11.,1.))
        i=np.argmax(score)
        return piv[i,0],float(rr[i,0,0])
    center,r=best(np.array([x,y]),r,shifts)
    center,r=best(center,r,shifts*.45)
    return center,r,closed,opened,sign


def inspect_curvature(im,center,r,closed,opened,stroke,sign):
    # Locate real radial ridge maxima on 27 rays. Refit their center without leaf,
    # radius, room or hinge constraints. Short tangential coincidences drift markedly.
    a=np.radians(np.linspace(5,85,27))
    unit=np.cos(a)[:,None]*closed+np.sin(a)[:,None]*opened
    offsets=np.linspace(-max(2.5,.12*r),max(2.5,.12*r),25)
    pts=center+(r+offsets[:,None,None])*unit
    val,noise=profile(im,pts,unit,sign,stroke)
    winner=np.argmax(val-.38*np.abs(offsets[:,None]),axis=0)
    k=np.arange(len(winner))
    use=(winner>0)&(winner<len(offsets)-1)&(val[winner,k]>=np.maximum(3.,noise[winner,k]*1.25))
    hits=pts[winner,k][use]
    if (len(hits)<13 or not np.any(use[:5]) or not np.any(use[-5:])):
        return {'status':'insufficient_radial_points','observations':len(hits)},hits
    def errors(z):
        return np.linalg.norm(hits-z[:2],axis=1)-z[2]
    sol=least_squares(errors,[*center,r],loss='soft_l1',f_scale=.32,max_nfev=36)
    drift=float(np.linalg.norm(sol.x[:2]-center))
    rshift=float(sol.x[2]-r)
    mad=float(np.median(np.abs(errors(sol.x))))
    stable=(drift <= max(2.4,r*.115) and abs(rshift)<=max(2.0,r*.10) and mad<=max(.68,.035*r))
    return {'status':'consistent' if stable else 'arc_center_drift',
            'observations':len(hits),'fitted_center_offset_px':np.round(sol.x[:2]-center,2).tolist(),
            'center_shift_px':round(drift,2),'radius_shift_px':round(rshift,2),
            'median_radial_error_px':round(mad,2)},hits


def assess(im, row, stroke):
    center,r,closed,opened,sign=sweep(im,row,stroke)
    def arc(angles, rad, pivot=center):
        a=np.radians(angles)
        n=np.cos(a)[:,None]*closed+np.sin(a)[:,None]*opened
        pts=pivot+rad*n
        v,e=profile(im,pts,n,sign,stroke)
        report,hits=summarize(v,e)
        return report,pts[hits]
    def leaf(fractions):
        pts=center+np.asarray(fractions)[:,None]*r*opened
        v,e=profile(im,pts,closed,sign,stroke)
        report,hits=summarize(v,e)
        return report,pts[hits]
    # Control samples ask whether neighboring radii or continuation of the same line/curve match equally well.
    angles=np.linspace(7,83,44)
    a,ap=arc(angles,r)
    le,lp=leaf(np.linspace(.13,.90,34))
    dr=max(3.5,stroke*1.7,r*.14)
    neighbor=max(arc(angles,r-dr)[0]['strength'],arc(angles,r+dr)[0]['strength'])
    pre,post=[arc(s,r)[0] for s in (np.linspace(-50,-9,22),np.linspace(99,140,22))]
    ext,_=leaf(np.linspace(1.14,1.57,20))
    before,_=leaf(np.linspace(-.59,-.15,20))
    # A leaf need not be inked all the way to its tip in a small alias-sampled original.
    arc_ok=(r>=8.5 and a['visible']>=.9 and a['fraction']>=.63 and a['sectors'].count('1')>=5 and
            a['strength']>=3.3 and a['strength']>=1.55*neighbor+1.)
    leaf_ok=(le['visible']>=.9 and le['fraction']>=.63 and le['sectors'].count('1')>=5 and le['strength']>=3.2)
    curvature,radial_hits=inspect_curvature(im,center,r,closed,opened,stroke,sign)
    if curvature['status']=='consistent':
        free_center=center+curvature['fitted_center_offset_px']
        free_r=r+curvature['radius_shift_px']
        alt_pre,alt_post=[arc(s,free_r,free_center)[0] for s in (np.linspace(-50,-9,22),np.linspace(99,140,22))]
        if min(alt_pre['strength'],alt_post['strength'])>min(pre['strength'],post['strength']):
            pre,post=alt_pre,alt_post
    circle=(min(pre['strength'],post['strength'])>=max(2.8,a['strength']*.31) and
            max(pre['strength'],post['strength'])>=max(4.,a['strength']*.6))
    run=ext['strength']>=max(3.,le['strength']*.61) and ext['visible']>=.8
    back_run=before['strength']>=max(3.,le['strength']*.61) and before['visible']>=.8
    reasons=[]
    if curvature['status']!='consistent':reasons.append(curvature['status'])
    if not arc_ok: reasons.append('weak_or_nonunique_arc')
    if not leaf_ok:reasons.append('weak_leaf')
    if circle:reasons.append('curve_continues_past_both_ends')
    if run:reasons.append('possible_line_continues_past_leaf_tip')
    if ext['visible']<.75 or min(pre['visible'],post['visible'])<.75:reasons.append('control_outside_source')
    score=math.sqrt(a['strength']*le['strength'])*(.42 if run else 1.)*(.29 if circle else 1.)
    return dict(pivot_px=np.round(center,2).tolist(),radius_px=round(r,2),wall_axis='H' if closed[0] else 'V',
                closed_vector=closed.astype(int).tolist(),open_vector=opened.astype(int).tolist(),
                closed_tip_px=np.round(center+r*closed,2).tolist(),open_tip_px=np.round(center+r*opened,2).tolist(),
                polarity='dark' if sign==1 else 'light',arc=a,leaf=le,independent_arc_fit=curvature,
                controls=dict(neighbor_arc_strength=round(neighbor,2),before_arc=pre['strength'],after_arc=post['strength'],
                    beyond_leaf=ext['strength'],behind_hinge=before['strength'],behind_line_continues=back_run),
                trace_score=round(score,2),pixel_cautions=reasons,
                arc_hit_px=np.round(radial_hits[::2],1).tolist(),leaf_hit_px=np.round(lp[::3],1).tolist())


def context(im,c,scale,rooms,openings,axes,origin):
    """Source context and author hints are secondary. No wall geometry is created."""
    p=np.array(c['pivot_px']); d=np.array(c['closed_vector']); n=np.array(c['open_vector'])
    r=c['radius_px'];tip=p+r*d
    axis=c['wall_axis']
    ctx={}
    # Outside jambs vs opening interior along the candidate host. A floor, wall or cabinet
    # can also produce the difference; report it without declaring that a wall exists.
    offs=np.arange(-max(2.,round(.08*r)),max(2.,round(.08*r))+1)
    def across(ts):
        pts=p-origin+np.asarray(ts)[:,None,None]*r*d+offs[None,:,None]*n
        vals=sample(im,pts)
        if np.mean(np.isfinite(vals))<.8:return None
        return float(np.nanmedian(vals))
    middle=across(np.linspace(.17,.85,10))
    outside=[across(np.linspace(-.5,-.12,8)),across(np.linspace(1.12,1.5,8))]
    ctx['middle_minus_beyond_jamb_gray']=[None if m is None or middle is None else round(middle-m,1) for m in outside]
    if axis in axes:
        idx=1 if axis=='H' else 0
        ctx['distance_to_given_wall_line_px']=round(float(abs(p[idx]-axes[axis])),1)
    if rooms:
        xy=np.array(rooms).reshape(2,2)
        side=(xy-p)@n
        ctx['room_signed_open_side_px']=np.round(side,1).tolist()
        ctx['open_toward_room']=int(np.argmax(side))+1 if np.min(side)<0<np.max(side) else None
        if np.min(side)<0<np.max(side):
            q=xy[0]+(xy[1]-xy[0])*side[0]/(side[0]-side[1])
            ctx['room_segment_crossing_fraction_of_leaf']=round(float((q-p)@d/r),2)
    align=[]
    for i,xy in enumerate(openings):
        ab=np.array(xy).reshape(2,2)
        if (abs(ab[1,0]-ab[0,0])>=abs(ab[1,1]-ab[0,1]))!=(axis=='H'):continue
        distances=np.linalg.norm(ab-p,axis=1)
        j=int(np.argmin(distances)); span=float((ab[1-j]-ab[j])@d)
        align.append(dict(input=i+1,near_jamb_px=round(float(min(distances)),1),
                          forward_span_px=round(span,1),radius_to_opening_ratio=round(r/span,2) if span>0 else None))
    ctx['given_openings_on_axis']=align
    ctx['one_or_both_given_room_points_on_same_side']=bool(rooms and ctx['open_toward_room'] is None)
    ctx['wall_line_readback']='one_or_both_outside_patches_darker' if any(
        v is not None and v>=14 for v in ctx['middle_minus_beyond_jamb_gray']) else 'no_clear_darker_outside_patch'
    c['radius_mm_approx']=round(r*scale)
    c['context']=ctx


def equivalent(a,b):
    r=min(a['radius_px'],b['radius_px'])
    return (a['closed_vector']==b['closed_vector'] and a['open_vector']==b['open_vector'] and
            math.dist(a['pivot_px'],b['pivot_px'])<=max(3.5,r*.20) and abs(a['radius_px']-b['radius_px'])<=max(3.5,r*.20))


def pairs(candidates):
    result=[]
    for i,a in enumerate(candidates):
        if a['pixel_cautions']:continue
        for b in candidates[i+1:]:
            if (b['pixel_cautions'] or a['open_vector']!=b['open_vector'] or a['wall_axis']!=b['wall_axis'] or
                a['closed_vector']==b['closed_vector']):continue
            gap=math.dist(a['closed_tip_px'],b['closed_tip_px'])
            if gap<=max(3.,min(a['radius_px'],b['radius_px'])*.18):
                result.append(dict(candidates=[a['id'],b['id']],closed_tips_gap_px=round(gap,2)))
    return result


def analyze(src,box,scale,interval,step,stroke,rooms=(),openings=(),axes=None):
    if not np.isfinite([*box,scale,*interval,step,stroke]).all() or not (0<scale and 0<step and 0<stroke and 0<interval[0]<=interval[1]):
        raise ValueError('Positive scale, radius interval, step and stroke required.')
    if not (0<=box[0]<box[2]<=src.width and 0<=box[1]<box[3]<=src.height):
        raise ValueError('Hinge search box must be inside the displayed source.')
    axes=axes or {}
    pad=math.ceil(1.75*interval[1]/scale)+8
    l,t=max(0,box[0]-pad),max(0,box[1]-pad)
    rb=(min(src.width,box[2]+pad),min(src.height,box[3]+pad))
    im=get_ink(src.crop((l,t,*rb)))
    local=(box[0]-l,box[1]-t,box[2]-l,box[3]-t)
    r0,r1=np.array(interval)/scale
    count=max(1,math.ceil((r1-r0)/max(.8,min(2.,step/scale))))
    radii=np.linspace(r0,r1,count+1) if r0!=r1 else [r0]
    raw=coarse(im,local,radii,stroke)
    origin=np.array([l,t])
    cs=[]
    for row in raw:
        c=assess(im,row,stroke)
        ph=c['pivot_px']
        if not (local[0]<=ph[0]<local[2] and local[1]<=ph[1]<local[3] and r0-.75<=c['radius_px']<=r1+.75):continue
        for key in ('pivot_px','closed_tip_px','open_tip_px','arc_hit_px','leaf_hit_px'):
            a=np.array(c[key])
            if len(a):c[key]=np.round(a+origin,2).tolist()
        context(im,c,scale,rooms,openings,axes,origin)
        cs.append(c)
    cs.sort(key=lambda c:(bool(c['pixel_cautions']),-c['trace_score']))
    kept=[]
    for c in cs:
        # Export alternatives only when the original contains distributed curved ink.
        # A lone straight edge admits a continuum of invented pivot/radius assignments.
        if c['pixel_cautions'] and (c['arc']['strength']<2.8 or c['arc']['fraction']<.48 or
                                    c['arc']['sectors'].count('1')<4):
            continue
        if any(equivalent(c,old) for old in kept):continue
        c['id']=len(kept)+1
        kept.append(c)
    clean=[c for c in kept if not c['pixel_cautions']]
    pair=pairs(clean)
    status='inspect_source'
    if clean:
        a=clean[0]
        # Even a partly supported perpendicular leaf can invalidate an apparently
        # unique interpretation of the same quarter-circle.
        rival=[b for b in kept[1:] if b['wall_axis']!=a['wall_axis'] and
               math.dist(a['pivot_px'],b['pivot_px'])<=max(4.,.20*a['radius_px']) and
               abs(a['radius_px']-b['radius_px'])<=max(4.,.20*a['radius_px']) and
               np.allclose(np.add(a['closed_vector'],a['open_vector']),np.add(b['closed_vector'],b['open_vector']))]
        def alternative_ink(b):
            if b['pixel_cautions'] and b['pixel_cautions']!=['possible_line_continues_past_leaf_tip']:
                return False
            # A wall changing weight beyond the jamb can touch an actual leaf.
            # Preserve ambiguity if both radii are visible stroke segments.
            return math.sqrt(b['arc']['strength']*b['leaf']['strength'])>=.68*a['trace_score']
        if any(alternative_ink(b) for b in rival):
            status='same_arc_two_possible_axes'
        elif len(clean)==1:
            status='one_arc_and_leaf_tendency'
        elif len(pair)==1 and len(clean)==2:
            status='two_complementary_leaf_traces'
        else:status='multiple_traces_inspect_source'
    checks=[]
    for c in clean:
        ctx=c['context']
        if ctx['one_or_both_given_room_points_on_same_side']:
            checks.append(dict(candidate=c['id'],issue='supplied_room_points_do_not_straddle_axis'))
        if ctx.get('distance_to_given_wall_line_px',0)>max(4.,.23*c['radius_px']):
            checks.append(dict(candidate=c['id'],issue='trace_offset_from_supplied_parallel_wall_line'))
    return dict(status=status,scale_mm_per_px=scale,hinge_search_box_px=list(box),radius_interval_mm=list(interval),
                author_rooms_px=rooms,author_openings_px=openings,author_wall_lines_px=axes,
                candidates_with_complete_trace=[c['id'] for c in clean],complementary_pairs=pair,
                poses_tested=8,coarse_candidates_refined=len(raw),context_to_review=checks,candidates=kept,
                interpretation='Coordinates refer to displayed original pixels. Closed vector follows the proposed opening; open vector points toward open tip. A traced leaf gives an axis tendency only. Room crossing, wall shading and supplied jambs are independent cues. Scores are not probabilities.')


def build_card(src,report,limit=6):
    """Display identical source area in every tile. Never draw candidate ink on the pristine copy."""
    box=report['hinge_search_box_px']
    pad=math.ceil(report['radius_interval_mm'][1]/report['scale_mm_per_px']*1.35)+3
    l,t=max(0,box[0]-pad),max(0,box[1]-pad)
    rr,bb=min(src.width,box[2]+pad),min(src.height,box[3]+pad)
    crop=src.crop((l,t,rr,bb))
    scale=min(420/crop.width,365/crop.height,8.)
    wh=tuple(max(1,round(z*scale)) for z in crop.size)
    base=crop.resize(wh,Image.Resampling.NEAREST)
    raw=np.asarray(crop.convert('L'),dtype=float)
    residual=raw-gaussian_filter(raw,max(2.,.018*max(crop.size)))
    enh=Image.fromarray(np.uint8(np.clip(127+6*residual,0,255))).convert('RGB').resize(wh,Image.Resampling.NEAREST)
    point=lambda p:((p[0]-l+.5)*wh[0]/crop.width-.5,(p[1]-t+.5)*wh[1]/crop.height-.5)
    def mark(draw,p,color,n=2):
        x,y=point(p)
        draw.ellipse((x-n,y-n,x+n,y+n),fill=color)
    guide=base.copy();gd=ImageDraw.Draw(guide)
    gd.rectangle([point(box[:2]),point(box[2:])],outline='#dd7f08',width=1)
    for axis,v in report['author_wall_lines_px'].items():
        pts=([l,v],[rr,v]) if axis=='H' else ([v,t],[v,bb])
        gd.line([point(p) for p in pts],fill='#aa68e2',width=1)
    for o in report['author_openings_px']:
        gd.line([point(o[:2]),point(o[2:])],fill='#197ccd',width=2)
    rooms=report['author_rooms_px']
    if rooms:
        pa,pb=point(rooms[:2]),point(rooms[2:])
        gd.line([pa,pb],fill='#3f8a4b',width=1)
        for i,p in enumerate((rooms[:2],rooms[2:])):
            mark(gd,p,'#197349',3)
            x,y=point(p);gd.text((x+3,y+1),f'R{i+1}',fill='black',stroke_width=1,stroke_fill='white')
    tiles=[('Displayed original','Nearest-neighbor enlargement',base),
           ('Local contrast x6','Gray = local baseline. Look for aligned pixels.',enh),
           ('Author input','Box / candidate H,V / opening ends / R1,R2',guide)]
    rows=report['candidates']
    selected=rows[:limit]
    # Reserve one tile for the opposite assignment of the *same* quarter circle.
    if rows:
        a=rows[0]
        rivals=[b for b in rows if b['wall_axis']!=a['wall_axis'] and
                math.dist(a['pivot_px'],b['pivot_px'])<=max(4.,.2*a['radius_px']) and
                abs(a['radius_px']-b['radius_px'])<=max(4.,.2*a['radius_px']) and
                np.allclose(np.add(a['closed_vector'],a['open_vector']),np.add(b['closed_vector'],b['open_vector']))]
        if rivals and rivals[0] not in selected and limit>=2:selected=selected[:-1]+[rivals[0]]
    for c in selected:
        tile=base.copy();d=ImageDraw.Draw(tile);p=np.array(c['pivot_px'])
        cv=np.array(c['closed_vector']);ov=np.array(c['open_vector'])
        rad=c['radius_px']
        ang=np.radians(np.arange(91))
        arc=p+rad*(np.cos(ang)[:,None]*cv+np.sin(ang)[:,None]*ov)
        for i in range(0,90,8): d.line([point(z) for z in arc[i:i+4]],fill='#db710b',width=1)
        for i in range(0,10,2):d.line([point(p+ov*rad*i/10),point(p+ov*rad*(i+1)/10)],fill='#1882ab',width=1)
        # Three geometric points have distinct symbols. Sample dots are measured, not extra fitted observations.
        for z in c['arc_hit_px']:mark(d,z,'#15965f',1)
        for z in c['leaf_hit_px']:mark(d,z,'#15965f',1)
        for title,z in [('P',p),('C',c['closed_tip_px']),('O',c['open_tip_px'])]:
            x,y=point(z);d.text((x+2,y+1),title,fill='#1b1b1b',stroke_width=1,stroke_fill='white')
        v=c['open_vector']; direction=NAMES[next(j for j,vec in enumerate(VECT) if list(vec)==v)]
        tag='check' if c['pixel_cautions'] else 'trace'
        tiles.append((f"#{c['id']} {tag}   opening {c['wall_axis']} / leaf {direction}",
                      f"arc {c['arc']['sectors']}    leaf {c['leaf']['sectors']}    radius {rad:.1f}px",tile))
    tw=max(wh[0]+16,280);th=wh[1]+52
    sheet=Image.new('RGB',(3*tw,44+math.ceil(len(tiles)/3)*th),'white')
    d=ImageDraw.Draw(sheet)
    d.text((7,4),report['status']+'   |   P pivot    C closed tip    O open tip',fill='black')
    d.text((7,21),'Dashes = geometric prediction. Green dots = samples passing two-sided contrast. Pixel grid: x right, y down.',fill='black')
    for i,(a,b,tile) in enumerate(tiles):
        x,y=(i%3)*tw+7,44+(i//3)*th
        d.text((x,y),a,fill='black');d.text((x,y+16),b,fill='#4e5255')
        sheet.paste(tile,(x,y+34))
    return sheet


def test_scene(mode,axis=0,signs=(1,1),light=False):
    """Synthetic, with subpixel rasterisation, a textured floor, text, panel seams and threshold walls."""
    sx,sy=signs
    rng=np.random.default_rng(3321+axis+3*(sx<0)+5*(sy<0))
    up=4;size=128
    yy,xx=np.indices((size*up,size*up))
    base=158 if light else 209
    paper=base+3*np.sin((2*xx+yy)*.035/up)+2.3*np.cos(yy*.17/up)+rng.normal(0,2.2,xx.shape)
    im=Image.fromarray(np.uint8(np.clip(paper,0,255)));d=ImageDraw.Draw(im)
    center=np.array([51.5 if sx>0 else 75.5,48.3 if sy>0 else 76.3])
    closed,opened=vectors(sx,sy,axis)
    def line(points,color,w):
        d.line([tuple(np.asarray(z)*up) for z in points],fill=int(color),width=max(1,round(w*up)))
    def grid(a,b,c):
        line([a,b],c,.72)
    for v in (9,32,57,92,117):
        grid([v,0],[v,size],base-10)
    for v in (14,43,82,106):
        grid([0,v],[size,v+1],base-8)
    try:font=ImageFont.load_default(size=7*up)
    except TypeError:font=ImageFont.load_default()
    d.text((84*up,31*up),'5.1',font=font,fill=base-60)
    d.text((17*up,97*up),'HALL',font=font,fill=base-43)
    line([center-closed*29,center-closed*3],base-87,4.6)
    r=27.8
    beyond=center+closed*(r if mode!='pair' else r+14.)
    line([beyond+closed*2,beyond+closed*30],base-87,4.)
    line([center+opened*6-closed*13,center+opened*46-closed*13],base-33,1.2)
    # A perpendicular intact cabinet edge inside the same ROI.
    line([center+opened*4-closed*6,center+opened*4+closed*18],base-26,1.1)
    color=base+(21 if light else -22)
    if mode=='below_contrast':color=base-2
    def swing(p,cv,ov,rad):
        a=np.linspace(0,math.pi/2,100)
        arc=p+rad*(np.cos(a)[:,None]*cv+np.sin(a)[:,None]*ov)
        if mode in ('door','pair','no_leaf','two_axes','circle','through_leaf','below_contrast','occluded'):line(arc,color,1.25)
        if mode in ('door','pair','two_axes','through_leaf','below_contrast','occluded','leaf_only'):line([p,p+rad*ov],color,1.4)
        if mode=='filled_sector':
            d.polygon([tuple(p*up)]+[tuple(z*up) for z in arc],fill=color)
        if mode=='occluded':
            a,b=p+rad*(cv*.15+ov*.15),p+rad*(cv*1.02+ov*1.02)
            d.rectangle((*np.minimum(a,b)*up,*np.maximum(a,b)*up),fill=base-3)
        if mode=='two_axes':line([p,p+rad*cv],color,1.4)
        if mode=='circle':
            a=np.linspace(0,2*math.pi,220)
            line(p+rad*(np.cos(a)[:,None]*cv+np.sin(a)[:,None]*ov),color,1.3)
            line([p,p+rad*ov],color,1.4)
        if mode=='through_leaf':line([p-rad*.7*ov,p+rad*1.66*ov],color,1.4)
    swing(center,closed,opened,r)
    pivots=[center]
    if mode=='pair':
        p=center+(r+14.)*closed
        swing(p,-closed,opened,14.)
        pivots.append(p)
    src=im.resize((size,size),Image.Resampling.LANCZOS).convert('RGB')
    picks=np.array(pivots)
    lo=np.floor(picks.min(0)-11).astype(int);hi=np.ceil(picks.max(0)+12).astype(int)
    return src,[*lo.tolist(),*hi.tolist()],center,closed,opened


def self_test(out):
    out.mkdir(parents=True,exist_ok=True)
    tests=[('door',ax,s,((i+ax)%2)==1) for ax in range(2) for i,s in enumerate(QUADS)]
    tests.extend((m,1,(1,1),False) for m in ('pair','no_leaf','two_axes','circle','through_leaf','seams_only','below_contrast','leaf_only','filled_sector','occluded'))
    summaries=[]
    for i,(mode,ax,sg,light) in enumerate(tests):
        src,box,p,d,n=test_scene(mode,ax,sg,light)
        scale=25.
        leaf=27.8; axis='H' if d[0] else 'V'
        room1=p+d*leaf*.56+n*38;room2=p+d*leaf*.56-n*31
        opening=np.r_[p+[1.8,-2.7],p+d*(41.8 if mode=='pair' else leaf)+[1.6,-1.9]].tolist()
        rec=analyze(src,box,scale,(290.,790.),43.,1.4,np.r_[room1,room2].tolist(),[opening],{'H':float(p[1]+1.5),'V':float(p[0]+2.6)})
        top=rec['candidates'][0] if rec['candidates'] else None
        pose_match=bool(top and top['wall_axis']==axis and top['open_vector']==n.tolist() and math.dist(p,top['pivot_px'])<=3.)
        if mode=='door':ok=pose_match and rec['status']=='one_arc_and_leaf_tendency'
        elif mode=='pair':ok=bool(rec['complementary_pairs'])
        elif mode=='two_axes':ok=rec['status']=='same_arc_two_possible_axes'
        else:ok=rec['status']=='inspect_source'
        summary=dict(case=i+1,mode=mode,axis=axis,opened=n.astype(int).tolist(),light=light,status=rec['status'],pose_match=pose_match,
                     complete=rec['candidates_with_complete_trace'],pairs=rec['complementary_pairs'],expected=ok)
        summaries.append(summary)
        if i in (0,1,8,9,10,11,12,13):
            build_card(src,rec,3).save(out/f'synthetic-card-{i+1}.png')
            (out/f'synthetic-{i+1}.json').write_text(json.dumps(rec,indent=2))
        print(json.dumps(summary),flush=True)
    (out/'synthetic-results.json').write_text(json.dumps(summaries,indent=2)+'\n')
    return all(z['expected'] for z in summaries)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('image',nargs='?',type=Path)
    p.add_argument('--out',type=Path)
    p.add_argument('--box',nargs=4,type=int,metavar=('LEFT','TOP','RIGHT','BOTTOM'),help='Broad original-pixel pivot search box; right/bottom exclusive.')
    p.add_argument('--rooms',nargs=4,type=float,metavar=('X1','Y1','X2','Y2'))
    p.add_argument('--opening',action='append',nargs=4,type=float,default=[],help='Rough candidate jamb endpoints; repeat for a perpendicular alternative.')
    p.add_argument('--h-line',type=float,help='Original Y of a suspect horizontal host.')
    p.add_argument('--v-line',type=float,help='Original X of a suspect vertical host.')
    p.add_argument('--mm-per-px',type=float)
    p.add_argument('--radii-mm',nargs=2,type=float,default=[300.,1150.],help='Source reader supplies plausible LEAF radii, including secondary leaf when needed.')
    p.add_argument('--step-mm',type=float,default=40.)
    p.add_argument('--stroke-px',type=float,default=1.6)
    p.add_argument('--tiles',type=int,default=5)
    p.add_argument('--self-test',type=Path,help='Run synthetic cases in a designated scratch directory.')
    a=p.parse_args()
    if a.self_test:return 0 if self_test(a.self_test) else 1
    if not (a.image and a.out and a.box and a.mm_per_px):p.error('image, --out, --box and --mm-per-px are required')
    if not 1<=a.tiles<=12:p.error('--tiles must be between 1 and 12')
    with Image.open(a.image) as f:
        rgba=ImageOps.exif_transpose(f).convert('RGBA')
        src=Image.new('RGB',rgba.size,'white');src.paste(rgba,mask=rgba.getchannel('A'))
    axes={axis:v for axis,v in [('H',a.h_line),('V',a.v_line)] if v is not None}
    for xy in ([a.rooms] if a.rooms else [])+a.opening:
        if not all(np.isfinite(xy)) or not all(0<=x<src.width and 0<=y<src.height for x,y in (xy[:2],xy[2:])):
            p.error('Room and opening marks must be inside displayed image.')
    try:rec=analyze(src,a.box,a.mm_per_px,a.radii_mm,a.step_mm,a.stroke_px,a.rooms or [],a.opening,axes)
    except ValueError as exc:p.error(str(exc))
    rec['source_file']=str(a.image.resolve());rec['displayed_size_px']=list(src.size)
    a.out.mkdir(exist_ok=True,parents=True)
    (a.out/'door-patch.json').write_text(json.dumps(rec,ensure_ascii=False,indent=2)+'\n')
    build_card(src,rec,a.tiles).save(a.out/'door-patch.png')
    print(rec['status'], 'complete traces:',rec['candidates_with_complete_trace'],'pairs:',rec['complementary_pairs'])
    return 0


if __name__=='__main__':
    raise SystemExit(main())
