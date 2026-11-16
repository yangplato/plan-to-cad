#!/usr/bin/env python3
"""Measure both jambs in original pixels along the author's approximate orthogonal host."""
import argparse
import hashlib
import json
import math
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
from PIL import Image, ImageDraw, ImageOps

from dimension_grid import GridProjection
from fit_wall_guides import calibration, pair, write_json
from scan_wall_cores import classify, runs, scan_axis


FRAME_KINDS = {'window', 'sliding', 'sidelight', 'rail'}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def xy(k, t, n):
    return [t, n] if k == 0 else [n, t]


def source_arrays(src, spec):
    scope = spec.get('plan_box_px', [0, 0, src.width-1, src.height-1])
    l, t, r, b = scope
    if any(type(x) is not int for x in scope) or not (0 <= l < r < src.width and 0 <= t < b < src.height):
        raise ValueError('Invalid inclusive plan_box_px')
    names, labels, samples = classify(src, scope, spec['samples'])
    if set(names) != {'bearing', 'partition'}:
        raise ValueError('Use separate bearing and partition core swatches from this image')
    codes = np.full((src.height, src.width), -2, dtype=np.int8)
    codes[t:b+1, l:r+1] = labels
    for x, y, X, Y in spec.get('exclude_boxes_px', []):
        if not (l <= x <= X <= r and t <= y <= Y <= b):
            raise ValueError('Invalid excluded box')
        codes[y:Y+1, x:X+1] = -2
    for x, y in spec.get('nonwall_points_px', []):
        if not (l <= x <= r and t <= y <= b):
            raise ValueError('Invalid nonwall point')
        codes[y, x] = -2
    return codes, names, samples, scope


def axis_for(op, cfg, opts, guide, codes, names, scope, k):
    a, b = op['a'], op['b']
    hint = (a[1-k]+b[1-k])/2
    lo, hi = sorted([a[k], b[k]])
    guard = cfg['station_search_px']
    nearby = [g for g in guide.get('axes', []) if g['orientation'] == 'HV'[k]
              and g['start']-0.5 <= (lo+hi)/2 <= g['end']+0.5
              and abs(g['normal']-hint) <= cfg['normal_search_px']]
    if 'axis_id' in opts:
        nearby = [g for g in nearby if g['id'] == opts['axis_id']]
        if len(nearby) != 1:
            raise ValueError(f"{op['id']}: axis_id is absent or outside the opening's search band")
    issues = []
    if len(nearby) == 1:
        picked = nearby[0]
        if not (picked['start']-0.5 <= lo <= hi <= picked['end']+0.5):
            issues.append('host_guide_does_not_cover_both_ends')
        if picked['competing_axes']:
            issues.append('competing_host_axes')
        return picked['normal'], picked['id'], issues, picked
    if nearby:
        issues.append('multiple_guide_axes')
    hint_i = round(hint)
    min_w, max_w = cfg['core_width_px']
    axis = dict(id=op['id'], a_px=xy(k, max(scope[k], math.floor(lo-guard-max_w)), hint_i),
                b_px=xy(k, min(scope[k+2], math.ceil(hi+guard+max_w)), hint_i),
                core_width_px=[min_w, max_w], search_px=cfg['normal_search_px'], min_run_px=2)
    if 'anchor_px' in opts:
        axis['anchor_px'] = opts['anchor_px']
    l, t, r, btm = scope
    fit = scan_axis(axis, codes[t:btm+1, l:r+1], scope, names, [])
    if fit['competing_axes'] or not any(s['status'] == 'proposal' for s in fit['segments']):
        issues.append('host_axis_not_unique_or_not_visible')
    return float(fit['core_normal_px']), None, issues, {}


def track_profile(gray, stations, n, width):
    """Normal positions of persistent dark or pale ridges inside the frame depth."""
    lo, hi = max(0, math.floor(n-width*.7)-1), min(gray.shape[0]-1, math.ceil(n+width*.7)+1)
    if hi-lo < 4 or not stations:
        return []
    # Median of original pixels along the opening removes an isolated note or cross stroke.
    line = np.median(gray[lo:hi+1, stations], axis=1)
    ridges = []
    for sign in (1, -1):
        row = line*sign
        for i in range(1, len(row)-1):
            strength = min(max(row[max(0,i-2):i])-row[i], max(row[i+1:i+3])-row[i])
            if strength >= 9:
                if ridges and ridges[-1]['polarity'] == sign and ridges[-1]['normal_px'] == lo+i-1:
                    continue
                ridges.append(dict(normal_px=lo+i, polarity=sign, contrast=round(float(strength),1)))
    return ridges


def normal_runs(col, normal, max_w):
    lo, hi = max(0, math.floor(normal-max_w)-1), min(len(col), math.ceil(normal+max_w)+2)
    return [(i+lo, j+lo, int(v)) for i, j, v in runs(col[lo:hi]) if v >= 0]


def core_at(spans, normal, bounds):
    matched = [(i, j, v) for i,j,v in spans if bounds[0] <= j-i+1 <= bounds[1]
               and abs((i+j)/2-normal) <= max(.75, (j-i+1)*.18)]
    return matched[0] if len(matched) == 1 else None


def paired_ridges(ridges):
    return any(sum(r['polarity'] == p for r in ridges) >= 2 for p in (1,-1))


def frame_stations(gray, t_min, t_max, normal, width, side):
    """A perpendicular rail closure gives a low-confidence frame-face alternative only."""
    lo, hi = max(0, math.ceil(normal-width*.4)), min(gray.shape[0]-1, math.floor(normal+width*.4))
    found = []
    if hi-lo < 2:
        return found
    depth = max(3, min(7, round(width*.65)))
    for t in range(max(depth,t_min), min(gray.shape[1]-depth,t_max+1)):
        left = np.median(gray[lo:hi+1, t-depth:t-1], axis=1)
        right = np.median(gray[lo:hi+1, t+2:t+depth+1], axis=1)
        limb = gray[lo:hi+1,t]
        contrast = max(min(np.mean((left-limb)*polarity>14),np.mean((right-limb)*polarity>14))
                       for polarity in (-1,1))
        if contrast < .56:
            continue
        along = list(range(t+side, t+side*(depth+1), side))
        marks = track_profile(gray, along, normal, width)
        if paired_ridges(marks):
            found.append((t, marks))
    groups = []
    for t, marks in found:
        if groups and groups[-1][-1][0] == t-1:
            groups[-1].append((t, marks))
        else:
            groups.append([(t, marks)])
    return [dict(station_px=(group[0][0]+group[-1][0])/2,
                 stroke_span_px=[group[0][0]-.5, group[-1][0]+.5],
                 inside_ridges=group[len(group)//2][1]) for group in groups]


def tonal_face(gray, row, solid, side, xlo, xhi):
    """Locate the midpoint and the 10--90 % width of a locally monotonic colour edge."""
    stations = [solid+side*i for i in range(-3,6)]
    if min(stations)<xlo or max(stations)>xhi:
        return None
    profile = gray[row,stations]
    dark, light = np.median(profile[:2]), np.median(profile[-2:])
    if abs(light-dark)<12:
        return None
    step = (profile-dark)/(light-dark)
    if np.min(np.diff(step)) < -.19:
        return None
    boundaries = []
    for level in (.1,.5,.9):
        crossings = [j+(level-u)/(v-u) for j,(u,v) in enumerate(zip(step,step[1:])) if u<level<=v]
        if len(crossings)!=1:
            return None
        boundaries.append(float(stations[0]+side*crossings[0]))
    return dict(station_px=round(boundaries[1],3), ramp_span_px=sorted(round(v,3) for v in (boundaries[0],boundaries[2])))


def candidate(outer, side, xlo, xhi, n, k, cores, spans, mask, gray, names, kind, guide, mid):
    top, bot, role = cores[outer]
    width = bot-top+1
    t = outer+side*.5
    limit = max(4, math.ceil(width*.8))
    out = []
    for i in range(limit):
        s = outer-side*i
        hit = cores.get(s)
        if not hit or hit[2] != role or abs(hit[1]-hit[0]+1-width) > max(2,width*.35):
            break
        out.append(s)
    inside = []
    for i in range(1,limit+1):
        s = outer+side*i
        if not xlo <= s <= xhi or (s-mid)*side > 0 or cores.get(s):
            break
        inside.append(s)
    if not inside:
        return None
    reasons = []
    if len(out) < max(3,math.ceil(width*.42)):
        reasons.append('short_or_irregular_wall_shoulder')
    if len(inside) < max(3,math.ceil(width*.42)):
        reasons.append('short_clear_inside')
    offsets = sorted({round((top+bot)/2+frac*(width-1)) for frac in (-.30,0,.30)})
    if len(offsets) < 3:
        reasons.append('insufficient_cross_section_resolution')
    # Measure independent along-wall transitions in three parts of the occupied section.
    slices = []
    reach = max(2, math.ceil(width*.75))
    for row in offsets:
        faces = []
        for j in range(-reach, reach+1):
            s = outer+side*j
            if min(s-side,s+2*side) < xlo or max(s-side,s+2*side) > xhi:
                continue
            if (mask[row,s] == role and mask[row,s-side] == role
                    and mask[row,s+side] not in (role,-2) and mask[row,s+2*side] not in (role,-2)):
                faces.append(s+side*.5)
        face = min(faces, key=lambda v:abs(v-t)) if faces else None
        tone = tonal_face(gray,row,round(face-side*.5),side,xlo,xhi) if face is not None else None
        slices.append(dict(normal_px=row, face_px=face, tonal_edge=tone, outer_px=xy(k,outer,row), inner_px=xy(k,outer+side,row),
                           outer_gray=int(gray[row,outer]), inner_gray=int(gray[row,outer+side])))
    measured = [s['face_px'] for s in slices if s['face_px'] is not None]
    if len(measured) < 2:
        reasons.append('fewer_than_two_visible_slice_edges')
    if measured and max(abs(x-t) for x in measured)>1.01:
        reasons.append('stepped_or_inconsistent_slice_edges')
    if np.any(mask[top:bot+1,min(out+inside):max(out+inside)+1] == -2):
        reasons.append('source_excluded_or_occluded')
    # Off-centre wall portions, T returns and projections can look clear on the centerline.
    if any(any(j-i+1 >= max(3,width*.48) and min(j,bot)-max(i,top)+1 >= max(2,width*.18)
               for i,j,_ in spans[s]) for s in inside):
        reasons.append('remaining_solid_or_projecting_return')
    before = outer-side*len(out)
    # A short core-coloured transverse bar can be a mullion in the same ink as the
    # rails. If rails of that colour continue on both sides, a wall pier is ambiguous.
    if len(out) < limit and before in spans:
        remote = [before-side*i for i in range(1,6) if before-side*i in spans]
        wall_gray = float(np.median(gray[top:bot+1,out]))
        def same_ink_rails(stations):
            if len(stations)<2:
                return False
            occupied = np.mean(mask[top:bot+1,stations] == role,axis=1) >= .70
            if sum(bool(v) for _,_,v in runs(occupied)) >= 2:
                return True
            # A thin antialiased rail mixes the ink with its background and can be much
            # paler than the core. Compare the original ridge with both colours.
            ridges = track_profile(gray,stations,n,width)
            profile = np.median(gray[top:bot+1,stations],axis=1)
            for sign in (-1,1):
                background = float(np.max(sign*profile))
                if background-sign*wall_gray<12:
                    continue
                matching = 0
                for ridge in ridges:
                    if ridge['polarity']!=sign:
                        continue
                    tone = float(np.median(gray[ridge['normal_px'],stations]))
                    alpha = (background-sign*tone)/(background-sign*wall_gray)
                    matching += .15 <= alpha <= 1.15
                if matching>=2:
                    return True
            return False
        if same_ink_rails(inside) and same_ink_rails(remote):
            reasons.append('short_post_in_same_ink_as_flanking_rails')
    if len(out) < limit and before in spans and any(j-i+1 > width*1.3 and i <= n <= j for i,j,_ in spans[before]):
        reasons.append('perpendicular_return_at_shoulder')
    ridges = track_profile(gray, inside, n, width)
    if kind in FRAME_KINDS and not paired_ridges(ridges):
        reasons.append('parallel_frame_strokes_not_resolved')
    if guide:
        if any(lo-.5 <= t <= hi+.5 for cross in guide.get('crossings',[]) for lo,hi in [cross['stations_px']]):
            reasons.append('guide_crossing_at_end')
        if any(item['stations_px'][0]-.5 <= t <= item['stations_px'][1]+.5 and item['state'] in ('conflict','excluded','junction')
               for item in guide['observations']):
            reasons.append('guide_marks_conflict_or_junction')
    wall_area = int(np.count_nonzero(mask[top:bot+1,out] == role))
    inner_area = int(np.count_nonzero(mask[top:bot+1,inside] >= 0))
    tone_edges = [s['tonal_edge'] for s in slices if s['tonal_edge']]
    # Keep the sampled-core boundary as a separate observation. Anti-aliasing often
    # erodes a dark core by one pixel even when its colour plateau is unambiguous.
    face = t
    if len(tone_edges)>=2 and max(abs(e['station_px']-t) for e in tone_edges) <= max(2,width*.3):
        face = round(float(np.median([e['station_px'] for e in tone_edges])),3)
    boundary_samples = [t]+measured+[v for e in tone_edges for v in e['ramp_span_px']]
    spread = round(max(.5,max(abs(v-face) for v in boundary_samples)),3)
    return dict(station_px=face, sampled_core_face_px=t, point_px=xy(k,face,n), outer_core_px=xy(k,outer,n), first_inside_px=xy(k,outer+side,n),
                material=names[role], measured_width_px=width, outer_normal_span_px=[top,bot], slice_face_choices_px=sorted(set(measured)),
                outer_stations_px=[min(out),max(out)], inner_stations_px=[min(inside),max(inside)],
                core_area_px2=wall_area, outer_area_px2=width*len(out),
                inner_sampled_area_px2=inner_area, inner_area_px2=width*len(inside), slices=slices,
                inside_ridges=ridges, local_face_half_band_px=spread, review_reasons=reasons)


def fit_end(hint, side, k, n, cores, spans, mask, gray, cfg, names, op, support, guide, mid, projection, axis_flags):
    radius = cfg['station_search_px']
    lo, hi = max(min(cores),math.floor(hint-radius)), min(max(cores),math.ceil(hint+radius))
    walls = []
    for outer in range(lo,hi+1):
        face = outer+side*.5
        if abs(face-hint)>radius or (face-mid)*side>=0 or not cores.get(outer) or outer+side not in cores or cores[outer+side]:
            continue
        c = candidate(outer,side,min(cores),max(cores),n,k,cores,spans,mask,gray,names,op['kind'],guide,mid)
        if c:
            walls.append(c)
    reasons = list(axis_flags)
    qualified = [c for c in walls if not c['review_reasons']]
    if support in ('frame','free','free_end'):
        reasons.append('frame_or_free_support_requires_reading')
    if len(qualified)!=1:
        reasons.append('multiple_clean_breaks' if len(qualified)>1 else 'no_unique_supported_wall_break')
    if len(walls)>1 and qualified:
        for c in walls:
            if c not in qualified and abs(c['station_px']-qualified[0]['station_px'])>1 and len(c['slices'])>=2 and c['outer_area_px2']>=3*c['measured_width_px']:
                reasons.append('other_wall_break_in_search_band')
                break
    success = bool(qualified) and not reasons
    width = float(np.median([c['measured_width_px'] for c in walls])) if walls else float(np.mean(cfg['core_width_px']))
    bars = [] if success else frame_stations(gray,lo,hi,n,width,side)
    for c in bars:
        c['point_px'] = xy(k,c['station_px'],n)
        c['local_face_half_band_px'] = (c['stroke_span_px'][1]-c['stroke_span_px'][0])/2
    def band_mm(i, center, radius, fixed):
        # A half-pixel interval crossing a printed tick can have asymmetric WCS bounds.
        return round(max(abs(projection.signed_distance(i,center,center+d,fixed)) for d in (-radius,radius)),4)
    for c in walls+bars:
        c['local_face_half_band_mm'] = band_mm(k,c['station_px'],c['local_face_half_band_px'],n)
    choices = qualified if success else walls+bars
    ivals = [(p['station_px']-p['local_face_half_band_px'],p['station_px']+p['local_face_half_band_px']) for p in choices]
    interval = [min(v[0] for v in ivals),max(v[1] for v in ivals)] if ivals else None
    uncertainty = dict(along_axis_px=None, along_axis_mm=None, across_axis_px=None, across_axis_mm=None)
    if success:
        chosen = qualified[0]
        along = chosen['local_face_half_band_px']
        across = .5+abs(np.mean(chosen['outer_normal_span_px'])-n)
        uncertainty = dict(along_axis_px=along, along_axis_mm=band_mm(k,chosen['station_px'],along,n),
                           across_axis_px=float(across), across_axis_mm=band_mm(1-k,n,across,chosen['station_px']))
    return dict(uncertainty_half_band=uncertainty, status='high' if success else 'review', rough_station_px=hint, requested_support=support,
                search_interval_px=[lo,hi], selected=qualified[0]['point_px'] if success else None,
                candidate_envelope_px=interval, envelope_width_mm=round(projection.signed_distance(k,*interval,n),4) if interval else None,
                wall_breaks=walls, frame_only_choices=bars, review_reasons=reasons)


def run_portal(op, cfg, opts, guide, arrays, src_gray, projection):
    codes,names,_,scope = arrays
    a,b = (pair(op[item],op['id']) for item in ('a','b'))
    if not all(scope[0] <= x <= scope[2] and scope[1] <= y <= scope[3] for x,y in (a,b)):
        raise ValueError(f"{op['id']}: rough endpoints outside plan_box_px")
    k = int(abs(b[1]-a[1]) > abs(b[0]-a[0]))
    if a[k] == b[k] or abs(a[1-k]-b[1-k]) > max(3, abs(a[k]-b[k])*.12):
        raise ValueError(f"{op['id']}: supply an approximate rectified horizontal or vertical host")
    cfg = {**cfg, **opts}
    cfg.setdefault('core_width_px', [max(3, math.ceil(70/projection.max_mm_per_px(1-k))),
                                    max(4, math.ceil(350/projection.min_mm_per_px(1-k)))])
    cfg.setdefault('station_search_px', max(5,math.ceil(350/projection.min_mm_per_px(k))))
    cfg.setdefault('normal_search_px', max(3,math.ceil(170/projection.min_mm_per_px(1-k))))
    min_w,max_w = cfg['core_width_px']
    if (not all(type(v) is int for v in (min_w, max_w, cfg['station_search_px'],cfg['normal_search_px']))
            or min_w<2 or max_w<min_w or cfg['station_search_px']<=0 or cfg['normal_search_px']<0):
        raise ValueError(f"{op['id']}: invalid pixel search or width interval")
    support = op.get('end_support',op.get('jamb_support',['unspecified','unspecified']))
    if len(support)!=2 or set(support)-{'frame','wall','free','free_end','unspecified'}:
        raise ValueError(f"{op['id']}: invalid support declarations")
    n,axis_id,flags,picked = axis_for(op,cfg,opts,guide,codes,names,scope,k)
    mask = codes.T if k else codes
    gray = src_gray.T if k else src_gray
    lo,hi = sorted([a[k],b[k]])
    margin = cfg['station_search_px']+max_w+3
    indices = range(max(scope[k],math.floor(lo-margin)), min(scope[k+2],math.ceil(hi+margin))+1)
    spans = {s:normal_runs(mask[:,s],n,max_w) for s in indices}
    cores = {s:core_at(v,n,(min_w,max_w)) for s,v in spans.items()}
    direction = 1 if b[k]>a[k] else -1
    ends = {key:fit_end(p[k],side,k,n,cores,spans,mask,gray,cfg,names,op,role,picked,
                        (lo+hi)/2,projection,flags) for p,key,side,role in zip((a,b),('a','b'),(direction,-direction),support)}
    issues = []
    width_mm = None
    if not all(e['selected'] for e in ends.values()):
        issues.append('unresolved_end')
    else:
        f,g = sorted(e['selected'][k] for e in ends.values())
        width_mm = projection.signed_distance(k,f,g,n)
        if f>=g:
            issues.append('invalid_span')
        # Keep a demonstrable interior wall or isolated coloured post in the report. A
        # short mullion can share the wall swatch; the reader decides its actual role.
        for i,j,filled in runs([bool(cores[s]) for s in range(math.ceil(f),math.floor(g)+1)]):
            if filled and j-i+1 >= max(2, math.ceil(min_w*.45)):
                issues.append('sampled_solid_inside_opening')
                break
        if 'printed_width_mm' in opts:
            uncertainty = sum(e['uncertainty_half_band']['along_axis_mm'] for e in ends.values())
            if abs(width_mm-opts['printed_width_mm'])>uncertainty:
                issues.append('conflict_with_printed_opening_width')
    if op.get('symbol_legible') is False:
        issues.append('source_symbol_unreadable')
    if opts.get('dimension_controlled_ends'):
        issues.append('dimension_controlled_endpoint_requires_review')
    return dict(id=op['id'],kind=op['kind'],axis='HV'[k],axis_id=axis_id,normal_px=n, rough_a=a,rough_b=b,
                measured_width_mm=width_mm,
                search_px=cfg['station_search_px'],core_width_px=cfg['core_width_px'],ends=ends,review_reasons=issues)


def separate_neighbors(rows):
    for i,row in enumerate(rows):
        k = 'HV'.index(row['axis'])
        for other in rows[i+1:]:
            if row['axis']!=other['axis'] or abs(row['normal_px']-other['normal_px'])>max(1,min(row['core_width_px'][0],other['core_width_px'][0])*.35):
                continue
            def measured_span(item):
                return sorted((item['ends'][key]['selected'] or item['rough_'+key])[k] for key in ('a','b'))
            a,b = measured_span(row)
            c,d = measured_span(other)
            if min(b,d)>max(a,c):
                for item,neighbor in ((row,other),(other,row)):
                    item['review_reasons'].append('overlap_with_neighbor:'+neighbor['id'])


def card(image,row,dest):
    k = 'HV'.index(row['axis'])
    n = row['normal_px']
    src = image.transpose(Image.Transpose.TRANSPOSE) if k else image
    lo,hi = sorted([row['rough_a'][k],row['rough_b'][k]])
    pad = row['search_px']+row['core_width_px'][1]
    depth = max(9,math.ceil(row['core_width_px'][1]*1.6))
    l,r = max(0,math.floor(lo-pad)), min(src.width,math.ceil(hi+pad)+1)
    u,v = max(0,math.floor(n-depth)), min(src.height,math.ceil(n+depth)+1)
    crop = src.crop((l,u,r,v))
    zoom = min(6,1050/crop.width,240/crop.height)
    w,h = max(1,round(crop.width*zoom)),max(1,round(crop.height*zoom))
    tile = crop.resize((w,h),Image.Resampling.NEAREST)
    board = Image.new('RGB',(max(405,w),2*h+57),'white')
    board.paste(tile,(0,19));board.paste(tile,(0,37+h));draw=ImageDraw.Draw(board)
    draw.text((2,2),f"RAW   {row['axis']} | source stations {l}..{r-1} | axis {n:g}",fill='black')
    draw.text((2,20+h),'rough: blue    supported: green    review: orange    slices: white ticks',fill='black')
    base=h+37
    def X(t):return round((t-l+.5)*zoom-.5)
    def Y(y):return base+round((y-u+.5)*zoom-.5)
    for end in row['ends'].values():
        x = X(end['rough_station_px'])
        draw.line((x,base,x,Y(n)-3),fill='#1a71d6',width=2)
        for p in end['wall_breaks']+end['frame_only_choices']:
            x = X(p['station_px']);good=end['selected']==p['point_px']
            color = '#08a459' if good else '#e87800'
            draw.line((x,Y(n)+4,x,base+h-1),fill=color,width=2)
            for s in p.get('slices',[]):
                if s['face_px'] is not None:
                    at = X(s['face_px']);yn = Y(s['normal_px'])
                    draw.line((at-1,yn,at+1,yn),fill='white',width=1)
    footer = 'Vertical host rotated for reading.' if k else 'Horizontal host.'
    draw.text((2,2*h+40),footer+' JSON uses original image x,y.',fill='black')
    board.save(dest)
    row['source_box_px'] = [u,l,v-1,r-1] if k else [l,u,r-1,v-1]


def run(image_path,reading_path,spec_path,out,guide_path=None,registration_path=None):
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise ValueError('Choose a new or empty output directory')
    digest = sha(image_path)
    reading,spec = read(reading_path),read(spec_path)
    guide = read(guide_path) if guide_path else {}
    if reading.get('source_sha256')!=digest or (guide_path and guide.get('image_sha256')!=digest):
        raise ValueError('The source reading and optional guide report must identify this exact source image')
    if spec.get('source_sha256',digest)!=digest:
        raise ValueError('The wall sample settings belong to another image')
    registration = read(registration_path) if registration_path else {}
    if registration.get('source_sha256',digest)!=digest:
        raise ValueError('Opening registration belongs to another original image')
    matrices = [calibration(item) for item in (registration,spec,guide) if calibration(item)]
    grids = [item['grid_registration'] for item in (registration,spec,guide) if 'grid_registration' in item]
    if any(m!=matrices[0] for m in matrices) or any(g.get('axes')!=grids[0].get('axes') for g in grids):
        raise ValueError('Fit settings, guide report and registration disagree on scale or grid axes')
    matrix = matrices[0] if matrices else None
    scales = [matrix[0],-matrix[3]] if matrix else spec.get('mm_per_px')
    if isinstance(scales,(float,int)):
        scales=[scales,scales]
    if not scales or len(pair(scales,'mm_per_px'))!=2 or min(scales)<=0:
        raise ValueError('Supply positive x/y mm_per_px or source_to_mm')
    system = {'source_to_mm':matrix or [scales[0],0,0,-scales[1],0,0]}
    if grids:
        system['grid_registration'] = grids[0]
    projection = GridProjection(system)
    with Image.open(image_path) as f:
        src=ImageOps.exif_transpose(f).convert('RGB')
    arrays = source_arrays(src,spec)
    gray = np.asarray(src.convert('L'),dtype=np.float32)
    options = spec.get('portal_options',{})
    ops = reading['portals']
    ids = [op['id'] for op in ops]
    if not ops or len(set(ids))!=len(ids) or any(not isinstance(i,str) or not i for i in ids) or set(options)-set(ids):
        raise ValueError('Use unique opening IDs and valid portal_options IDs')
    if any(op['kind'] not in FRAME_KINDS|{'swing','open'} for op in ops):
        raise ValueError('Unsupported opening type')
    rows = [run_portal(op,spec.get('defaults',{}), options.get(op['id'],{}),guide,arrays,gray,projection) for op in ops]
    if matrix:
        for row in rows:
            for end in row['ends'].values():
                end['selected_wcs_mm'] = projection.world(end['selected']) if end['selected'] else None
    separate_neighbors(rows)
    out.parent.mkdir(parents=True,exist_ok=True)
    with TemporaryDirectory(dir=out.parent) as work:
        dst=Path(work)/'results';dst.mkdir();patch=[]
        for idx,row in enumerate(rows,1):
            row['card']=f'opening-{idx:03}.png';card(src,row,dst/row['card'])
            row['status']='review' if row['review_reasons'] else 'proposal'
            if row['status']=='proposal':
                patch.append(dict(id=row['id'],a=row['ends']['a']['selected'],b=row['ends']['b']['selected']))
        write_json(dst/'opening-stations.json',dict(source_sha256=digest,reading_sha256=sha(reading_path),
                   settings_sha256=sha(spec_path),guide_sha256=sha(guide_path) if guide_path else None,
                   source_size_px=list(src.size),mm_per_px=None if any(projection.axes) else scales,
                   registration_sha256=sha(registration_path) if registration_path else None,
                   mm_per_px_ranges=[[projection.min_mm_per_px(i),projection.max_mm_per_px(i)] for i in range(2)],
                   samples=arrays[2],portals=rows))
        write_json(dst/'opening-updates.json',dict(source_sha256=digest,openings=patch))
        dst.replace(out)
    print(f'{len(patch)}/{len(rows)} openings have two supported source faces; remaining openings need local review')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('image','reading','settings','out'):
        parser.add_argument(name,type=Path)
    parser.add_argument('--guide-report',type=Path)
    parser.add_argument('--registration',type=Path,help='Original-image registration, including piecewise printed grid axes')
    args=parser.parse_args()
    try:
        run(args.image,args.reading,args.settings,args.out,args.guide_report,args.registration)
    except (ValueError,KeyError,TypeError,OSError) as exc:
        parser.exit(2,f'fit_opening_stations: {exc}\n')
