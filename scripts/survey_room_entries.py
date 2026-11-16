#!/usr/bin/env python3
"""Scan complete room perimeters on the displayed source, with no supplied door locations."""
import argparse
from collections import Counter
from hashlib import sha256
import json
import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageOps
from scipy.ndimage import gaussian_filter, maximum_filter1d

from door_patch_card import assess, at_offset, get_ink, profile, sample
from scan_wall_cores import runs, scan_axis


def valid_point(p, size):
    return (isinstance(p, list) and len(p) == 2 and all(type(x) is int for x in p)
            and all(0 <= x < bound for x, bound in zip(p, size)))


def axis_codes(ink, axis, scale):
    # Local flanks reveal a darker core even on a tinted floor. The later width
    # and straight-run test rejects thin or diffuse lines. These are tonal bands.
    normal = 0 if axis == 'H' else 1
    half = max(4, math.ceil(410 / scale))
    # Take the brighter neighbour on either side so a wall on a floor-colour boundary
    # does not need the same background on both faces.
    bg = maximum_filter1d(ink, 2 * half + 1, axis=normal)
    return np.where((bg - ink) >= 14, 0, -1).astype('int8')


def seed_bounds(seed, codes, scale):
    # Find independently supported bands on a fan of parallel probes. Keep the
    # search geometry explicitly provisional when only a seed is available.
    limits, found = {}, {}
    for axis, names in [('V', ('left', 'right')), ('H', ('top', 'bottom'))]:
        stripes = codes[axis].T if axis == 'H' else codes[axis]
        ti, ni = (0, 1) if axis == 'H' else (1, 0)
        t, n = seed[ti], seed[ni]
        peaks = []
        for span_mm in (750, 1650):
            # A seed opposite the clear part of a portal sees no wall on the
            # central ray. Use a wider independent fan that reaches both jambs.
            span = max(6, round(span_mm/scale))
            lo, hi = max(0, t-span), min(stripes.shape[0], t+span+1)
            hist = np.zeros(stripes.shape[1])
            for u in range(lo, hi):
                for a,b,v in runs(stripes[u]):
                    if v == 0 and max(2, 65 / scale) <= b-a+1 <= max(3, 400 / scale):
                        hist[(a+b)//2] += 1
            support = np.convolve(hist, [1,1,1], mode='same') / (hi-lo)
            peaks.extend((a+int(np.argmax(support[a:b+1])), max(support[a:b+1]))
                         for a,b,v in runs(support >= .52) if v)
        for sign, name in zip((-1, 1), names):
            nearby = [(abs(k-n), k, strength) for k,strength in peaks if (k-n)*sign >= max(5, 400/scale)]
            if nearby:
                _, k, _ = min(nearby)
                limits[name], found[name] = k, True
            else:
                limits[name], found[name] = (0 if sign < 0 else len(hist)-1), False
    return [limits[x] for x in ('left','top','right','bottom')], found


def transform_point(p, factors, size):
    return [int(np.clip(round((v+.5)*factor-.5), 0, size[i]-1)) for i,(v,factor) in enumerate(zip(p,factors))]


def unmap(p, factors):
    return [round(float((v+.5)/f-.5), 2) for v,f in zip(p,factors)]


def rectangle(box):
    l,t,r,b = box
    return [[l,t],[r,t],[r,b],[l,b]]


def inside(point, poly):
    # Even-odd for the centre of a crossing on either side of an axis.
    x,y = point
    crossing = 0
    for a,b in zip(poly, poly[1:] + poly[:1]):
        if (a[1] > y) != (b[1] > y):
            crossing += (x < a[0] + (b[0]-a[0])*(y-a[1])/(b[1]-a[1]))
    return bool(crossing % 2)


def existing_rooms(data, physical, region_ids, size):
    """Allowlist room-only observations before giving anything to the detector."""
    if physical:
        records = []
        for r in data['rooms']:
            if r['mode']=='exterior':
                continue
            records.append(dict(id=r['id'], mode={'closed':'enclosed','open':'open','unknown':'unknown'}[r['mode']],
                                seeds_px=r['seeds_px']))
        return {'rooms':records}
    if region_ids:
        by_id={r['id']:r for r in data['regions']}
        if len(by_id)!=len(data['regions']) or len(set(region_ids))!=len(region_ids):
            raise ValueError('duplicate source region ID')
        records=[]
        for ident in region_ids:
            box=by_id[ident]['box']
            if len(box)!=4 or not all(isinstance(v,(int,float)) and math.isfinite(v) for v in box):
                raise ValueError(f'{ident}: expected a finite source crop rectangle')
            l,t,r,b=[round(v) for v in box]
            if not (0<=l<r<=size[0] and 0<=t<b<=size[1]):
                raise ValueError(f'{ident}: source region crop outside displayed image')
            records.append(dict(id=ident,box_px=[l,t,r-1,b-1]))
        return {'rooms':records}
    return data


def room_inputs(spec, size, factors, worksize, codes, scale):
    if not isinstance(spec, dict) or set(spec) != {'rooms'} or not isinstance(spec['rooms'], list) or not spec['rooms']:
        raise ValueError('spec requires only a nonempty rooms array')
    rooms = []
    for s in spec['rooms']:
        allowed = {'id','box_px','polygon_px','edges_px','seed_px','seeds_px','mode'}
        if not isinstance(s, dict) or set(s)-allowed:
            raise ValueError('only room identifiers, modes, perimeters and interior seeds are accepted')
        ident = s.get('id')
        if not isinstance(ident,str) or not ident.strip() or any(r['id']==ident for r in rooms):
            raise ValueError('room identifiers must be unique nonempty strings')
        mode = s.get('mode','unknown')
        if mode not in ('enclosed','open','unknown'):
            raise ValueError(f'{ident}: mode must be enclosed, open or unknown')
        if 'seed_px' in s and 'seeds_px' in s:
            raise ValueError(f'{ident}: use seed_px or seeds_px')
        points=s.get('seeds_px', [s['seed_px']] if 'seed_px' in s else [])
        if not isinstance(points,list):
            raise ValueError(f'{ident}: seeds_px must be an array of source pixel positions')
        seeds=[]
        for p in points:
            if (not isinstance(p,list) or len(p)!=2 or not all(isinstance(v,(int,float)) and math.isfinite(v)
                    and 0<=v<=size[k]-1 for k,v in enumerate(p))):
                raise ValueError(f'{ident}: seed must be inside the source')
            seeds.append(transform_point(p,factors,worksize))
        forms = [k for k in ('box_px','polygon_px','edges_px') if k in s]
        if len(forms)>1 or (not forms and not seeds):
            raise ValueError(f'{ident}: use one complete perimeter or an interior seed')
        source = forms[0] if forms else 'seed_estimate'
        limits = {}
        if not forms:
            def coverage(estimate):
                box,found=estimate
                l,t,r,b=box
                return (sum(found.values()),sum(l<x<r and t<y<b for x,y in seeds))
            box,limits=max((seed_bounds(seed,codes,scale) for seed in seeds),key=coverage)
            poly=rectangle(box)
        else:
            if source=='box_px':
                b=s[source]
                if not isinstance(b,list) or len(b)!=4 or not valid_point(b[:2],size) or not valid_point(b[2:],size) or b[0]>=b[2] or b[1]>=b[3]:
                    raise ValueError(f'{ident}: box uses inclusive source pixel axes [left,top,right,bottom]')
                original=rectangle(b)
            elif source=='edges_px':
                edges=s[source]
                if not edges or any(len(v)!=2 for v in edges) or any(b!=following[0] for (_,b),following in zip(edges,edges[1:]+edges[:1])):
                    raise ValueError(f'{ident}: edges must form an ordered, closed perimeter')
                original=[v[0] for v in edges]
            else:
                original=s[source]
            if len(original)<4 or any(not valid_point(p,size) for p in original):
                raise ValueError(f'{ident}: invalid perimeter vertices')
            poly=[transform_point(p,factors,worksize) for p in original]
        segments=list(zip(poly,poly[1:]+poly[:1]))
        if any((a[0]==b[0])==(a[1]==b[1]) for a,b in segments):
            raise ValueError(f'{ident}: orthogonal nonzero perimeter edges required')
        for i,(a,b) in enumerate(segments):
            for j,(c,d) in enumerate(segments[:i]):
                if i-j==1 or (i==len(segments)-1 and j==0):
                    continue
                if all(max(min(a[k],b[k]),min(c[k],d[k]))<=min(max(a[k],b[k]),max(c[k],d[k])) for k in (0,1)):
                    raise ValueError(f'{ident}: perimeter intersects itself')
        outside=[unmap(p,factors) for p in seeds if not inside(p,poly)]
        if outside and forms:
            raise ValueError(f'{ident}: interior seed falls outside perimeter')
        rooms.append(dict(id=ident,mode=mode,source=source,poly=poly,seed_bounds_found=limits,uncovered_seeds_px=outside))
    return rooms


def host_axis(a,b,codes,scale,tol):
    horizontal=a[1]==b[1]
    axis='H' if horizontal else 'V'
    ti,ni=(0,1) if horizontal else (1,0)
    lo,hi=sorted([a[ti],b[ti]])
    hint=a[ni]
    h,w=codes.shape
    spec=dict(id='side',a_px=a,b_px=b,core_width_px=[max(2,math.ceil(62/scale)),max(3,math.floor(410/scale))],
              search_px=tol,min_run_px=max(3,round(105/scale)),jitter_px=max(1,30/scale))
    result=scan_axis(spec,codes,[0,0,w-1,h-1],['tonal_band'],[])
    host=float(result['core_normal_px'])
    spans=[(s['a_px'][ti],s['b_px'][ti],s['core_width_px']) for s in result['segments'] if s['status']=='proposal']
    return dict(axis=axis,lo=lo,hi=hi,hint=hint,normal=host,spans=spans,competing=result['competing_axes'])


ANGLES=np.radians(np.arange(8,83,9))
def search_strip(gray, host, scale, tol, radius_limits, stroke, curve_only=False):
    """All hinge pixels in a side strip; retain a few peaks in each reach of the side."""
    axis=host['axis']
    # In the transposed view the host is always horizontal. Convert votes back
    # before calling the same arc/leaf fit used for a source patch.
    im=gray if axis=='H' else gray.T
    h,w=im.shape
    r0,r1=np.array(radius_limits,dtype=float)/scale
    radii=np.arange(max(8.5,r0),r1+.01,min(2.,max(.9,35/scale)))
    if r1>=8.5 and (not len(radii) or radii[-1]<r1-.5):
        radii=np.append(radii,r1)
    if not len(radii):
        return [],0
    reach=max(4,round(110/scale))
    l=max(0,host['lo']-reach); rr=min(w,host['hi']+reach+1)
    # The hinge can land on a wall face or near a return; never search only the
    # fitted centreline, which can belong to a nearby frame or a different band.
    t=max(0,math.floor(min(host['hint'],host['normal'])-tol))
    bb=min(h,math.ceil(max(host['hint'],host['normal'])+tol+1))
    margin=math.ceil(r1*1.7)+8
    cx,cy=max(0,l-margin),max(0,t-margin)
    sub=np.ascontiguousarray(im[cy:min(h,bb+margin),cx:min(w,rr+margin)])
    gh,gw=bb-t,rr-l
    offset_t,offset_l=t-cy,l-cx
    tiles=max(1,math.ceil(gw/max(18,1.45*r1)))
    cuts=np.linspace(0,gw,tiles+1,dtype=int)
    rows=[]
    def gather(plane, dx,dy):
        x,y=offset_l+round(dx),offset_t+round(dy)
        # All complete samples use a view. Missing source is zero evidence.
        if x>=0 and y>=0 and x+gw<=sub.shape[1] and y+gh<=sub.shape[0]:
            return plane[y:y+gh,x:x+gw]
        arr=np.zeros((gh,gw),dtype=np.float32)
        i,j=max(0,-x),max(0,-y)
        ii,jj=min(gw,sub.shape[1]-x),min(gh,sub.shape[0]-y)
        if i<ii and j<jj:
            arr[j:jj,i:ii]=plane[y+j:y+jj,x+i:x+ii]
        return arr
    def ridge(nx,ny,sign):
        d=max(2.,1.+.65*stroke)
        a=sign*(at_offset(sub,nx*d,ny*d)-sub)
        b=sign*(at_offset(sub,-nx*d,-ny*d)-sub)
        return np.clip(np.minimum(a,b),0,34)
    for sign in (1,-1):
        leaf_ridge=ridge(1,0,sign)
        for side in (-1,1):
            units=[(np.cos(v),side*np.sin(v)) for v in ANGLES]
            bands=[ridge(nx,ny,sign) for nx,ny in units]
            for along in (-1,1):
                pool=[[] for _ in range(tiles)]
                for r in radii:
                    curved=np.stack([gather(p,along*r*nx,along*r*ny) for p,(nx,ny) in zip(bands,units)])
                    # Require distributed support, while allowing local watermark overlap.
                    arc=np.mean(np.sort(curved,axis=0)[2:-1],axis=0)
                    leaf=sum(gather(leaf_ridge,0,along*side*r*f) for f in (.21,.38,.57,.78))*.25
                    score=arc*(.82+.18*np.minimum(leaf/10.,1.)) if curve_only else arc*(.35+.65*np.minimum(leaf/10.,1.))
                    for k,(start,end) in enumerate(zip(cuts,cuts[1:])):
                        tile=score[:,start:end]
                        y,x=np.unravel_index(np.argmax(tile),tile.shape)
                        val=float(tile[y,x])
                        if val>=2.2:
                            xx,yy=l+start+int(x),t+int(y)
                            if axis=='H':
                                row=(val,xx,yy,float(r),along,along*side,1,sign)
                            else:
                                row=(val,yy,xx,float(r),along*side,along,0,sign)
                            pool[k].append(row)
                for candidates in pool:
                    reps=[]
                    for row in sorted(candidates,reverse=True):
                        if any(math.dist(row[1:3],p[1:3])<max(2.5,.15*row[3]) and abs(row[3]-p[3])<max(2.5,.14*row[3]) for p in reps):
                            continue
                        reps.append(row)
                        if len(reps)>=2:
                            break
                    rows.extend(reps)
    return rows,tiles


def occupied(spans,l,r):
    if r<=l:
        return 0.
    return min(1.,sum(max(0.,min(b+1,r)-max(a,l)) for a,b,_ in spans)/(r-l))


def crossing_labels(c, rooms, owner):
    p=np.array(c['pivot_px']); d=np.array(c['closed_vector']); n=np.array(c['open_vector'])
    mid=p+d*c['radius_px']*.5
    dist=max(7.,c['radius_px']*.55)
    sides=[[room['id'] for room in rooms if inside(mid+n*dist*s,room['poly'])] for s in (-1,1)]
    return dict(opposite_open_side=sides[0],open_leaf_side=sides[1],owner_on_one_side=(owner in sides[0]) != (owner in sides[1]))


def host_read(c,h,scale):
    ti,ni=(0,1) if h['axis']=='H' else (1,0)
    p=c['pivot_px']; a=p[ti]; r=c['radius_px']; b=c['closed_tip_px'][ti]
    lo,hi=sorted((a,b))
    reach=max(4, min(.46*r,320/scale))
    # A wall return or adjacent window commonly leaves only one clear short jamb.
    near=min(max(1.5,45/scale),.12*r)
    left=occupied(h['spans'],lo-reach,lo-near)
    right=occupied(h['spans'],hi+near,hi+reach)
    middle=occupied(h['spans'],lo+.14*r,hi-.14*r)
    nearest=abs(h['normal']-p[ni])
    same=not h['competing'] and nearest<=max(4.,175/scale)
    return dict(jamb_support=[round(left,2),round(right,2)],middle_core_fraction=round(middle,2),
                axis_offset_mm=round(nearest*scale),host_agrees=bool(same and max(left,right)>=.39 and middle<=.36),
                span=[lo,hi])


def same_pose(a,b):
    return (a['closed_vector']==b['closed_vector'] and a['open_vector']==b['open_vector']
            and math.dist(a['pivot_px'],b['pivot_px'])<=max(3,.18*min(a['radius_px'],b['radius_px']))
            and abs(a['radius_px']-b['radius_px'])<=max(3,.17*min(a['radius_px'],b['radius_px'])))


def classify_trace(c, support, scale):
    flags=set(c['pixel_cautions'])
    if ('curve_continues_past_both_ends' in flags or c['arc']['fraction']<.53 or
            c['independent_arc_fit']['status']!='consistent' or c['arc']['sectors'].count('1')<4):
        return None
    good_arc='weak_or_nonunique_arc' not in flags
    good_leaf='weak_leaf' not in flags and 'possible_line_continues_past_leaf_tip' not in flags
    if not good_arc and not good_leaf:
        return None
    if support['middle_core_fraction']>.60:
        return None
    if good_leaf and good_arc and support['host_agrees'] and 'control_outside_source' not in flags:
        if c['radius_px']*scale<560:
            return 'short_leaf_trace'
        return 'arc_leaf_and_host'
    if good_leaf and good_arc:
        return 'arc_leaf_host_unresolved'
    if good_arc and support['host_agrees']:
        return 'partial_swing_at_band_gap'
    return None


def suppress_diagonal_texture(gray):
    """Estimate a repeated oblique stroke direction; remove only its long-range continuation."""
    # Bound the cost of estimating one global direction by using a spatial sample.
    residual=gray-gaussian_filter(gray,2.1)
    probe=np.clip(residual,-12,12)
    stride=max(1,math.ceil(max(probe.shape)/430))
    y,x=np.mgrid[22:probe.shape[0]-22:stride,22:probe.shape[1]-22:stride]
    if x.size<200:
        return gray,None
    xy=np.stack((x,y),axis=-1)
    middle=probe[y,x]
    denom=max(.1,float(np.mean(middle*middle)))
    angles=np.r_[np.arange(12,80,3),np.arange(101,170,3)]
    scores=[]
    for angle in angles:
        a=np.radians(angle); u=np.array([np.cos(a),np.sin(a)])
        vals=[np.mean(middle*sample(probe,xy+k*u))/denom for k in (7.3,12.8,18.3)]
        scores.append(float(np.median(vals)))
    best=int(np.argmax(scores)); score=scores[best]
    if score<.39 or score<np.median(scores)+.23:
        return gray,None
    a=np.radians(angles[best]); dx,dy=np.cos(a),np.sin(a)
    align=np.median(np.stack([at_offset(residual,k*dx,k*dy) for k in (-19,-13,-8,8,13,19)]),axis=0)
    # If a curved stroke coincides with the diagonal, keep its excess local contrast.
    common=np.clip(align*1.24,np.minimum(0.,residual),np.maximum(0.,residual))
    return gray-common,dict(angle_degrees=int(angles[best]),directional_correlation=round(score,3))


def normalized_gray(gray):
    # Local brightness removal and bounded gain bring faint strokes up without
    # requiring a light floor. Apply this only to the separate visual search.
    residual=gray-gaussian_filter(gray,3.3)
    local=gaussian_filter(np.abs(residual),5.5)
    gain=np.clip(8./(1.1+local),1.3,5.)
    return np.asarray(np.clip(136.+gain*residual,0,255),dtype=np.float32)


def opposite_controls(im,c):
    """Compare the two possible bows through the same endpoints on original ink."""
    p=np.array(c['pivot_px']); r=c['radius_px']
    d=np.array(c['closed_vector']); n=np.array(c['open_vector'])
    sign=1 if c['polarity']=='dark' else -1
    # Endpoints of the two curves coincide. Compare only their separated interiors.
    angles=np.radians(np.linspace(16,74,42))
    normals=np.cos(angles)[:,None]*d+np.sin(angles)[:,None]*n
    reverse=-(np.sin(angles)[:,None]*d+np.cos(angles)[:,None]*n)
    def contrast(pts,norm):
        values,noise=profile(im,pts,norm,sign,1.45)
        quality=np.clip(values-.52*noise,0,24)
        return float(np.median(quality)),np.array([float(np.mean(chunk>=1.35)) for chunk in np.array_split(quality,5)])
    bow,bow_parts=contrast(p+r*normals,normals)
    other,other_parts=contrast(p+r*(d+n)+r*reverse,reverse)
    return dict(source_curve_median=round(bow,2),opposite_curve_median=round(other,2),
                curve_sector_fractions=bow_parts.round(2).tolist(),opposite_sector_fractions=other_parts.round(2).tolist(),
                favored_sectors=int(np.sum((bow_parts>=.45)&(bow_parts>=other_parts+.15))),
                separated_sectors=int(np.sum(bow_parts>=.45)))


def weak_curves(gray,enhanced,h,scale,tol,radius_limits,rooms,room_id):
    visual_radii=[max(520,radius_limits[0]),radius_limits[1]]
    raw,_=search_strip(enhanced,h,scale,tol,visual_radii,1.45,curve_only=True) if visual_radii[0]<=visual_radii[1] else ([],0)
    # Partial traces remain review only, including their host, leaf and registration.
    found=[]
    for row in raw:
        c=assess(enhanced,row,1.45)
        flags=c['pixel_cautions']
        arc=c['arc']; ctrl=c['controls']
        if (arc['fraction']<.44 or arc['sectors'].count('1')<3 or
                arc['strength']<1.35 or arc['strength']<1.22*ctrl['neighbor_arc_strength']+.4 or
                'curve_continues_past_both_ends' in flags or 'control_outside_source' in flags or
                c['independent_arc_fit']['status']!='consistent' or c['radius_px']*scale<500):
            continue
        if min(ctrl['before_arc'],ctrl['after_arc'])>=4 and (ctrl['behind_line_continues'] or
                min(ctrl['before_arc'],ctrl['after_arc'])>=.30*arc['strength']):
            continue
        support=host_read(c,h,scale)
        if (min(support['span'])<h['lo']-max(3,120/scale) or max(support['span'])>h['hi']+max(3,120/scale)
                or not h['spans'] or h['competing'] or support['axis_offset_mm']>185):
            continue
        # A continuous core may be an inked sill. Keep this as ambiguity only;
        # the independent curve direction and the room must still agree.
        if max(support['jamb_support'])<.33:
            continue
        labels=crossing_labels(c,rooms,room_id)
        if not labels['owner_on_one_side']:
            continue
        direction=opposite_controls(gray,c)
        if (direction['source_curve_median']<1.75 or
            direction['source_curve_median']<1.45*direction['opposite_curve_median']+1. or
            direction['favored_sectors']<3 or direction['separated_sectors']<4):
            continue
        # An unbacked free centre or a continuing line provides weaker support.
        # Never promote this class even when a normalized fit looks complete.
        c.update(kind='visual_curve_only_unconfirmed',rooms=labels,host=support,curve_direction=direction)
        c['visual_score']=direction['source_curve_median']*direction['favored_sectors']*(.65+.35*c['leaf']['fraction'])
        if c['radius_px']*scale<510:
            c['visual_score']*=.65
        found.append(c)
    return sorted(found,key=lambda c:-c['visual_score']),len(raw)


def scan_side(gray, enhanced, h, scale, tol, radius_limits, rooms, room_id, cap):
    raw,tiles=search_strip(gray,h,scale,tol,radius_limits,1.45)
    observations=[]
    flags=Counter()
    for row in raw:
        c=assess(gray,row,1.45)
        c['host']=host_read(c,h,scale)
        lo,hi=c['host']['span']
        if lo<h['lo']-max(3,120/scale) or hi>h['hi']+max(3,120/scale):
            continue
        kind=classify_trace(c,c['host'],scale)
        if not kind:
            flags.update(c['pixel_cautions'])
            continue
        c['kind']=kind
        c['rooms']=crossing_labels(c,rooms,room_id)
        if not c['rooms']['owner_on_one_side'] and kind=='arc_leaf_and_host':
            c['kind']='arc_leaf_room_crossing_unresolved'
        observations.append(c)
    rank={'arc_leaf_and_host':5,'short_leaf_trace':4,'arc_leaf_room_crossing_unresolved':3,
          'arc_leaf_host_unresolved':2,'partial_swing_at_band_gap':1}
    observations.sort(key=lambda c:(rank[c['kind']],c['trace_score']),reverse=True)
    traces=[]
    for c in observations:
        if not any(same_pose(old,c) for old in traces):
            traces.append(c)
    choices=[]
    for i,c in enumerate(traces):
        if c['kind'] not in ('arc_leaf_and_host','short_leaf_trace','arc_leaf_host_unresolved'):
            continue
        for d in traces[i+1:]:
            if d['kind'] not in ('arc_leaf_and_host','short_leaf_trace','arc_leaf_host_unresolved'):
                continue
            if (c['open_vector']!=d['open_vector'] or c['closed_vector']==d['closed_vector'] or
                    math.dist(c['closed_tip_px'],d['closed_tip_px'])>max(3.,.19*min(c['radius_px'],d['radius_px']))):
                continue
            lo=min(c['host']['span'][0],d['host']['span'][0])
            hi=max(c['host']['span'][1],d['host']['span'][1])
            jambs=[occupied(h['spans'],lo-max(4,225/scale),lo-1),occupied(h['spans'],hi+1,hi+max(4,225/scale))]
            if (max(jambs)<.4 or occupied(h['spans'],lo+2,hi-2)>.35 or
                    not c['rooms']['owner_on_one_side'] or not d['rooms']['owner_on_one_side']):
                continue
            choices.append((6,c['trace_score']+d['trace_score'],'complementary_swing_traces',[c,d],[lo,hi]))
    for c in traces:
        choices.append((rank[c['kind']],c['trace_score'],c['kind'],[c],c['host']['span']))
    kept=[]
    for _,_,kind,group,span in sorted(choices,key=lambda x:(x[0],x[1]),reverse=True):
        if any(max(0,min(span[1],old['span'][1])-max(span[0],old['span'][0]))>.55*min(span[1]-span[0],old['span'][1]-old['span'][0]) for old in kept):
            continue
        kept.append(dict(kind=kind,span=span,traces=group))
    weak_count=0
    if len(kept)<cap:
        partial,weak_count=weak_curves(gray,enhanced,h,scale,tol,radius_limits,rooms,room_id)
        for c in partial:
            span=c['host']['span']
            if any(max(0,min(span[1],k['span'][1])-max(span[0],k['span'][0]))>.43*min(span[1]-span[0],k['span'][1]-k['span'][0]) for k in kept):
                continue
            kept.append(dict(kind=c['kind'],span=span,traces=[c]))
    alternatives=max(0,len(kept)-cap)
    kept=kept[:cap]
    gaps=[]
    ti=0 if h['axis']=='H' else 1
    for left,right in zip(h['spans'],h['spans'][1:]):
        lo,hi=left[1]+1,right[0]-1
        width=(hi-lo+1)*scale
        if not 530<=width<=3600 or min(left[1]-left[0]+1,right[1]-right[0]+1)*scale<100:
            continue
        if any(max(0,min(hi,k['span'][1])-max(lo,k['span'][0]))>.4*min(hi-lo,k['span'][1]-k['span'][0]) for k in kept):
            continue
        a=np.asarray(gray) if ti==0 else np.asarray(gray).T
        mid=round(h['normal']); hw=max(3,round(160/scale))
        swatch=a[max(0,mid-hw):min(a.shape[0],mid+hw+1),lo:hi+1]
        line_guess=False
        if min(swatch.shape)>=3:
            profile=np.median(swatch,axis=1)
            line_guess=bool(np.ptp(profile)>=14 and np.min(profile[1:-1])<=np.percentile(profile,80)-12)
        gaps.append(dict(kind='gap_with_linear_ink' if line_guess else 'band_gap_no_swing',
                         span=[lo,hi],traces=[],width_mm=round(width)))
    gaps.sort(key=lambda z:z['width_mm'],reverse=True)
    gap_cap=2
    return kept+gaps[:gap_cap],dict(tiles=tiles,local_fits=len(raw),visual_only_fits=weak_count,curve_sites_over_budget=alternatives,
                                   gap_sites_over_budget=max(0,len(gaps)-gap_cap),rejected_trace_flags=dict(flags))


def original_side(h, choices, factors, scale):
    ti,ni=(0,1) if h['axis']=='H' else (1,0)
    def point(t,n):
        return unmap([t,n] if ti==0 else [n,t],factors)
    geometry=dict(axis=h['axis'],rough_edge_px=[point(h['lo'],h['hint']),point(h['hi'],h['hint'])],
                  suggested_host_px=[point(h['lo'],h['normal']),point(h['hi'],h['normal'])],
                  competing_tonal_axes=bool(h['competing']),tonal_runs_px=[[point(l,h['normal']),point(r,h['normal'])]
                        for l,r,_ in h['spans']])
    items=[]
    for i,item in enumerate(choices,1):
        label=item['kind']
        result=dict(id=i,kind=label,approx_span_px=[point(t,h['normal']) for t in item['span']],traces=[])
        if 'width_mm' in item:
            result['approx_band_gap_mm']=item['width_mm']
        for c in item['traces']:
            host={k:v for k,v in c['host'].items() if k!='span'}
            curve=np.array(c['closed_vector'])*np.cos(np.linspace(0,np.pi/2,43))[:,None]
            curve+=np.array(c['open_vector'])*np.sin(np.linspace(0,np.pi/2,43))[:,None]
            center=c['pivot_px']+c['radius_px']*(np.array(c['closed_vector'])+np.array(c['open_vector']))
            reverse=center-c['radius_px']*curve[::-1]
            result['traces'].append(dict(opposite_arc_px=[unmap(x,factors) for x in reverse],
                     curve_direction=c.get('curve_direction'),hinge_px=unmap(c['pivot_px'],factors),closed_tip_px=unmap(c['closed_tip_px'],factors),
                     open_tip_px=unmap(c['open_tip_px'],factors),radius_mm=round(c['radius_px']*scale),polarity=c['polarity'],
                     predicted_arc_px=[unmap(x,factors) for x in c['pivot_px']+c['radius_px']*curve],
                     observed_arc_px=[unmap(x,factors) for x in c['arc_hit_px']],observed_leaf_px=[unmap(x,factors) for x in c['leaf_hit_px']],
                     arc_sectors=c['arc']['sectors'],leaf_sectors=c['leaf']['sectors'],controls=c['controls'],
                     cautions=c['pixel_cautions'],host_readback=host,room_crossing=c['rooms']))
        items.append(result)
    return geometry,items


def make_card(source,geometry,items,scales,rmax,edge_id,path):
    edge=geometry['rough_edge_px']
    # Show the full side plus the floor on each side. No resampling or markings
    # are applied to the clean source until the three identical views are scaled.
    box=[max(0,math.floor(min(p[k] for p in edge)-1.22*rmax/scales[k])) for k in (0,1)]
    box += [min(source.size[k],math.ceil(max(p[k] for p in edge)+1.22*rmax/scales[k]+1)) for k in (0,1)]
    raw=source.crop(box)
    z=min(680/raw.width,470/raw.height,4.)
    wh=(max(1,round(raw.width*z)),max(1,round(raw.height*z)))
    base=raw.resize(wh,Image.Resampling.NEAREST)
    kinds=', '.join(f"{v['id']} {v['kind']}" for v in items) or 'No arc/leaf or bracketed gap retained'
    header=48; footer=24
    sheet=Image.new('RGB',(3*max(wh[0],190)+12,wh[1]+header+footer),'#f8f8f8')
    draw=ImageDraw.Draw(sheet)
    draw.text((6,4),f'{edge_id}   |   {kinds}',fill='#111111')
    def xy(p):
        return tuple((p[i]-box[i]+.5)*wh[i]/raw.size[i]-.5 for i in (0,1))
    for i,name in enumerate(('Source original','Tonal bands + observed pixels','Predicted swing / gap only')):
        panel=base.copy()
        pd=ImageDraw.Draw(panel)
        if i==1:
            for a,b in geometry['tonal_runs_px']:
                pd.line([xy(a),xy(b)],fill='#005bbb',width=2)
        if i:
            for item in items:
                if not item['traces']:
                    a,b=item['approx_span_px']
                    pd.line([xy(a),xy(b)],fill='#c47710',width=2)
                for tr in item['traces']:
                    if i==1:
                        for key,color in [('observed_arc_px','#b00070'),('observed_leaf_px','#00775b')]:
                            for p in tr[key]:
                                x,y=xy(p);pd.ellipse((x-1,y-1,x+1,y+1),fill=color)
                    else:
                        pd.line([xy(p) for p in tr['predicted_arc_px']],fill='#b00070',width=2)
                        if item['kind']!='visual_curve_only_unconfirmed':
                            pd.line([xy(tr['hinge_px']),xy(tr['open_tip_px'])],fill='#00775b',width=2)
                x,y=xy(item['approx_span_px'][0]);pd.text((max(2,x),max(2,y-14)),str(item['id']),fill='black',stroke_width=2,stroke_fill='white')
        x=i*max(wh[0],190)+4*i
        draw.text((x+5,26),name,fill='#222222')
        sheet.paste(panel,(x,header))
    draw.text((5,header+wh[1]+3),'Blue: wall-band pixels    Magenta: arc    Green: leaf    Ochre: gap / linear ink',fill='#333333')
    sheet.save(path)
    return box


def visual_card(source,item,scales,path,analysis_view,factors):
    tr=item['traces'][0]
    pts=np.array(tr['predicted_arc_px']+tr['opposite_arc_px']+[tr['hinge_px']])
    margin=220/np.array(scales)
    low=np.maximum(0,np.floor(pts.min(axis=0)-margin).astype(int))
    high=np.minimum(source.size,np.ceil(pts.max(axis=0)+margin+1).astype(int))
    box=[*low,*high]
    crop=source.crop(box)
    # Display undistorted physical aspect even for unequal source axis scales.
    physical=np.array(crop.size)*scales
    wh=tuple(max(1,round(v*300/max(physical))) for v in physical)
    raw=crop.resize(wh,Image.Resampling.NEAREST)
    analysis_box=[*(low*factors),*(high*factors)]
    detail=analysis_view.transform(wh,Image.Transform.EXTENT,analysis_box,Image.Resampling.NEAREST).convert('RGB')
    control=detail.copy()
    draw=ImageDraw.Draw(control)
    def xy(p):
        return tuple((p[i]-low[i]+.5)*wh[i]/crop.size[i]-.5 for i in (0,1))
    for j in range(0,len(tr['opposite_arc_px'])-2,4):
        draw.line([xy(p) for p in tr['opposite_arc_px'][j:j+3]],fill='#ed9e1a',width=2)
    for p in tr['observed_arc_px']:
        x,y=xy(p)
        draw.ellipse((x-1,y-1,x+1,y+1),fill='#cb1179')
    evidence=tr['curve_direction']
    sheet=Image.new('RGB',(3*(wh[0]+6),wh[1]+56),'#fafafa')
    caption=ImageDraw.Draw(sheet)
    caption.text((4,4),'VISUAL REVIEW   |   ' +item['kind'],fill='#111111')
    for i,(panel,label) in enumerate(((raw,'Original'),(detail,'Local contrast'),(control,'Observed / reverse control'))):
        x=i*(wh[0]+6)
        caption.text((x+3,22),label,fill='#222222')
        sheet.paste(panel,(x,38))
    if evidence:
        caption.text((4,wh[1]+39),f"Source medians {evidence['source_curve_median']} vs {evidence['opposite_curve_median']}; favored sectors {evidence['favored_sectors']}/5",fill='#333333')
    sheet.save(path)


def execute(image_path, rooms_path, out, scales, radius_limits, tolerance_mm, cap, physical=False, region_ids=()):
    data=json.loads(rooms_path.read_text(encoding='utf8'))
    if not scales and physical:
        a,b,d,e,_,_=data['source_to_mm']
        x,y=math.hypot(a,d),math.hypot(b,e)
        if x*y==0 or abs(a*b+d*e)>1e-3*x*y:
            raise ValueError('source_to_mm uses a singular or sheared pixel grid; supply a rectified source')
        scales=[x,y]
    scales=list(scales or [])
    if len(scales)==1:
        scales*=2
    if len(scales)!=2 or any(not math.isfinite(v) or v<=0 for v in [*scales,*radius_limits,tolerance_mm]) or radius_limits[0]>radius_limits[1]:
        raise ValueError('give positive X and Y mm/px scales and an increasing positive radius interval')
    if not 1<=cap<=4:
        raise ValueError('per-side candidate cap must be 1..4')
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise ValueError('output directory must be new or empty')
    with Image.open(image_path) as f:
        rgba=ImageOps.exif_transpose(f).convert('RGBA')
        source=Image.new('RGB',rgba.size,'white')
        source.paste(rgba,mask=rgba.getchannel('A'))
    scale=max(24.,*scales)
    size=tuple(max(1,round(d*s/scale)) for d,s in zip(source.size,scales))
    factors=np.array(size)/source.size
    work=source.resize(size,Image.Resampling.LANCZOS) if size!=source.size else source
    gray=get_ink(work)
    detextured,diagonal=suppress_diagonal_texture(gray)
    enhanced=normalized_gray(detextured)
    analysis_view=Image.fromarray(enhanced.astype('uint8'))
    codes={k:axis_codes(gray,k,scale) for k in ('H','V')}
    spec=existing_rooms(data,physical,region_ids,source.size)
    rooms=room_inputs(spec,source.size,factors,size,codes,scale)
    out.mkdir(parents=True,exist_ok=True)
    results=[]
    for n,room in enumerate(rooms,1):
        sides=[]
        for i,(a,b) in enumerate(zip(room['poly'],room['poly'][1:]+room['poly'][:1]),1):
            axis='H' if a[1]==b[1] else 'V'
            tol=max(4,math.ceil(tolerance_mm/scale))
            h=host_axis(a,b,codes[axis],scale,tol)
            found,budget=scan_side(gray,enhanced,h,scale,tol,radius_limits,rooms,room['id'],cap)
            for site in found:
                for c in site['traces']:
                    if 'curve_direction' not in c:
                        c['curve_direction']=opposite_controls(gray,c)
            geom,items=original_side(h,found,factors,scale)
            side_id=f'R{n:02d}-S{i:02d}'
            card=side_id+'.png'
            box=make_card(source,geom,items,scales,radius_limits[1],side_id,out/card)
            for item in items:
                if item['kind'] in ('visual_curve_only_unconfirmed','partial_swing_at_band_gap','arc_leaf_host_unresolved'):
                    item['local_review_card']=f"{side_id}-C{item['id']:02d}.png"
                    visual_card(source,item,scales,out/item['local_review_card'],analysis_view,factors)
            status='review_candidates' if items else 'no_retained_signal' if h['spans'] else 'host_and_entry_unreadable'
            sides.append(dict(id=side_id,status=status,**geom,candidates=items,budget=budget,card=card,card_box_px=box))
        strong=any(c['kind'] in ('arc_leaf_and_host','complementary_swing_traces') for s in sides for c in s['candidates'])
        gap=any(c['kind']=='band_gap_no_swing' for s in sides for c in s['candidates'])
        action='verify_traced_crossing' if strong else 'inspect_apparent_open_route' if gap and room['mode']=='open' else 'entry_unresolved_inspect_full_perimeter'
        if room['source']=='seed_estimate' and (room['uncovered_seeds_px'] or not all(room['seed_bounds_found'].values())):
            action='provisional_boundary_recheck_room_extent'
        results.append(dict(id=room['id'],mode=room['mode'],geometry_source=room['source'],
                            perimeter_px=[unmap(p,factors) for p in room['poly']],seed_bounds_found=room['seed_bounds_found'],uncovered_seeds_px=room['uncovered_seeds_px'],
                            room_access_review=action,sides=sides))
    report=dict(status='review_source',source=image_path.name,source_sha256=sha256(image_path.read_bytes()).hexdigest(),
                coordinates='EXIF-displayed source pixels, x right, y down',source_size_px=source.size,mm_per_px_xy=scales,
                normalized_mm_per_px=scale,visual_only_diagonal_texture=diagonal,radius_interval_mm=radius_limits,normal_search_mm=tolerance_mm,
                max_curve_sites_per_side=cap,max_additional_gaps_per_side=2,rooms=results)
    (out/'room-entry-survey.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    return report


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('image',type=Path)
    p.add_argument('rooms',type=Path)
    p.add_argument('--mm-per-px',type=float,nargs='+',metavar='SCALE',help='one uniform scale or X Y; physical-rooms can supply source_to_mm')
    p.add_argument('--radius-mm',type=float,nargs=2,default=[300,1250],metavar=('MIN','MAX'))
    p.add_argument('--tolerance-mm',type=float,default=300,help='normal reach of the hinge search on each side of rough and fitted axes')
    p.add_argument('--per-side',type=int,default=2,help='curve site budget 1..4; up to two independent gaps are also listed')
    p.add_argument('--out',type=Path,required=True)
    inputs=p.add_mutually_exclusive_group()
    inputs.add_argument('--from-physical-rooms',action='store_true',help='read only room IDs, modes and clear interior seeds from existing physical-rooms.json')
    inputs.add_argument('--from-regions',nargs='+',metavar='ROOM_REGION_ID',help='use selected complete room boundary crops in existing source-reading.json')
    a=p.parse_args()
    try:
        r=execute(a.image,a.rooms,a.out,a.mm_per_px,a.radius_mm,a.tolerance_mm,a.per_side,a.from_physical_rooms,a.from_regions)
    except (OSError,ValueError,TypeError,KeyError) as e:
        p.error(str(e))
    print(f"{len(r['rooms'])} rooms; review {sum(len(v['sides']) for v in r['rooms'])} complete sides at {a.out}")


if __name__=='__main__':
    main()
