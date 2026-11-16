"""Reproduce two registered source-geometry comparisons on frozen R8 DWG readbacks."""
import argparse
from collections import defaultdict
import hashlib
import json
import math
from pathlib import Path
import statistics
import subprocess
import sys
import time

import ezdxf
from shapely.geometry import LineString, Polygon, box
from shapely.ops import unary_union

parser=argparse.ArgumentParser()
parser.add_argument('root',type=Path)
parser.add_argument('skill',type=Path)
parser.add_argument('upstream',type=Path)
parser.add_argument('out',type=Path)
a=parser.parse_args()
out=a.out.resolve()
out.mkdir(parents=True,exist_ok=True)
root=a.root.resolve()
source=root/'input/营销原图.jpg'
judge=root/'staff/blind-audit/source-judge'
scripts=a.skill.resolve()/'scripts'
up=a.upstream.resolve()/'scripts'
sys.path[:0]=[str(judge/'tools'),str(up),str(scripts)]
from extract_geometry import read_geometry
from project_geometry import load_projected
from wallfit import fit

def jread(p):
    return json.loads(p.read_text())
def save(p,d):
    p.write_text(json.dumps(d,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
def digest(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()
def pieces(ob):
    if ob.is_empty:return []
    if ob.geom_type=='LineString':return [ob]
    return [piece for g in getattr(ob,'geoms',[]) for piece in pieces(g)]
def filled(rows, roles):
    polys=[]
    for row in rows:
        if not row.get('solid') or roles.get(row['layer']) not in {'黑','灰'}:continue
        poly=Polygon()
        for ring in row['paths']:
            if len(ring)>=3:poly=poly.symmetric_difference(Polygon(ring).buffer(0))
        polys.append((row['handle'],poly))
    return polys
def src_ticks(row):
    for path in row['paths']:
        for (x0,y0),(x1,y1) in zip(path,path[1:]):
            if abs(x1-x0)<.1 and abs(y1-y0)>8:
                yield ((x0+x1)/2, min(y0,y1), max(y0,y1))
def cell(lo,hi,x0,x1):
    return [[x0,lo],[x1,lo],[x1,hi],[x0,hi]]

stations=[('kitchen_foyer_upper',999,(172,192),(159,259)),('F3',898,(176,193),(159,240))]
def extract_host(rows,polys,wall,roles,station,sy,sx):
    name,xhint,span,limits=station
    mid=sum(span)/2
    edge=LineString([(xhint-10,mid),(xhint+10,mid)])
    intersect=pieces(wall.intersection(edge))
    core=next((p for p in intersect if p.bounds[0]<=xhint<=p.bounds[2]),None)
    framegroups=defaultdict(list)
    for row in rows:
        if roles.get(row['layer'])=='窗':
            for x,lo,hi in src_ticks(row):
                if abs(x-xhint)<6 and limits[0]<=lo and hi<=limits[1]:
                    framegroups[round(lo,1),round(hi,1)].append((x,lo,hi,row['handle']))
    windows=[st for st in framegroups.values() if len({round(e[0],1) for e in st})>=3]
    if core is None:
        matching=[st for st in windows if st[0][1]<mid<st[0][2]]
        assert len(matching)==1, (name,matching)
        xlo,xhi=min(v[0] for v in matching[0]),max(v[0] for v in matching[0])
        target_handles=[v[3] for v in matching[0]]
    else:
        xlo,xhi=core.bounds[0],core.bounds[2]
        target_handles=[h for h,p in polys if p.intersection(core).length>1e-6]
    xc=(xlo+xhi)/2
    line=LineString([(xc,limits[0]),(xc,limits[1])])
    mat=pieces(wall.intersection(line))
    lo=min(p.bounds[1] for p in mat); hi=max(p.bounds[3] for p in mat)
    assert lo < span[0] < span[1] < hi
    seams=sorted({lo,hi,*[z for p in mat for z in (p.bounds[1],p.bounds[3])],
                  *[z for st in windows for z in (max(v[1] for v in st),min(v[2] for v in st)) if lo<z<hi]})
    arcs=[]
    for r in rows:
        if roles.get(r['layer'])!='门' or r['type']!='ARC' or abs(r['center'][0]-xc)>7:continue
        for path in r['paths']:
            ys=[p[1] for p in path]; arcs.append((min(ys),max(ys),r['handle']))
    intervals=[]
    for y0,y1 in zip(seams,seams[1:]):
        if y1-y0<1e-5:continue
        ym=(y0+y1)/2
        if any(p.bounds[1]<=ym<=p.bounds[3] for p in mat):
            kind='wall'
        elif any(max(v[1] for v in st)<=ym<=min(v[2] for v in st) for st in windows):
            kind='window'
        elif any(v[0]-2<=ym<=v[1]+2 for v in arcs):
            kind='door'
        else:kind='hole'
        if intervals and kind==intervals[-1][2]:intervals[-1][1]=y1
        else:intervals.append([y0,y1,kind])
    wall_spec={'a':[xc*sy,lo*sy],'b':[xc*sy,hi*sy],'t':(xhi-xlo)*sx,
               'openings':[{'from':start*sy,'to':end*sy,'type':kind} for start,end,kind in intervals if kind!='wall']}
    return wall_spec,dict(id=name,limits_px=limits,source_roi=span,normal_px=xhint,
        model_center_px=xc,model_faces_or_outer_frame_strokes_px=[xlo,xhi],
        model_at_midpoint='wall' if core is not None else 'window',handles=target_handles,
        target_y=mid,host_intervals=intervals,door_arc_handles=arcs,frame_stroke_groups=windows)

def furniture(rows,roles,sy):
    # Each native INSERT handle groups all its projected child paths, axis-aligned bounding box.
    groups=defaultdict(list)
    for row in rows:
        if roles.get(row['layer']) in {'活动','固定'} and row.get('insert'):
            groups[row['layer'],row['handle']].extend(p for path in row['paths'] for p in path)
    allf=[]
    for (layer,h),points in groups.items():
        xs,ys=zip(*points); l,r=min(xs),max(xs);t,b=min(ys),max(ys)
        allf.append(dict(key='nativeInsertBounds',at=[(l+r)/2*sy,(t+b)/2*sy],w=(r-l)*sy,d=(b-t)*sy,rot=0))
    return allf

def timed_cli(command, output=None, allowed=(0,), repeats=3):
    times=[]
    for _ in range(repeats):
        start=time.perf_counter()
        proc=subprocess.run(list(map(str,command)),capture_output=True,text=True)
        times.append(time.perf_counter()-start)
        assert proc.returncode in allowed,(command,proc.stdout,proc.stderr)
        if output: output.write_text(proc.stdout)
    return {'seconds':times,'median_s':statistics.median(times)}
registered=jread(root/'staff/source-preregistered.json')
assert registered['sha256']==digest(source)
upstream_root=a.upstream.resolve()
commit=(subprocess.check_output(['git','-C',str(upstream_root),'rev-parse','HEAD'],text=True).strip()
        if (upstream_root/'.git').exists() else (upstream_root/'COMMIT').read_text().strip())
pinned={'LICENSE':'c4cc143edbe5e2d4bdcf75e94c3bb2091195f2327bc164d0ebfc384694358de7',
        'scripts/wallfit.py':'2285a70923928d684ec8583f91d3162512d06099264e68fe61810a927a90cb20',
        'scripts/wallaudit.py':'12cfedab1d44937d6549baccfbc16150a0ac52d0fb15fa184cf8a3b7df02ce1f',
        'scripts/imgio.py':'066bf71d1859d396e847176c48c1d80c2fd3d9be2123fd0ba99c268fb730496b'}
assert all(digest(upstream_root/k)==v for k,v in pinned.items())
summary={'source_sha256':digest(source),'frozen_commit':commit,
         'stations':stations,'wallfit':{},'candidates':{},'timings':{},'criteria':'Score one alert per station if it overlaps by at least one original pixel the registered central interval (solid wall trimmed by 3.35px each end; F3, D2 and D3 use preregistered inner ranges). Also report exact midpoint separately; an alert only at an endpoint is reviewed separately. Mark the drawing class match separately from detection.'}
assert summary['frozen_commit']=='d4e7374896e3283883e88d1bcef356eccd320926'
for name,xhint,span,_ in stations:
    for search in [25,6]:
        t0=time.perf_counter()
        result=fit(str(source),'V',xhint,*span,search=search)
        summary['wallfit'][f'{name}-search{search}']=dict(result=result,inprocess_seconds=time.perf_counter()-t0)
for name,xhint,span,_ in stations:
    summary['timings']['upstream_wallfit_'+name]=timed_cli([sys.executable,up/'wallfit.py',source,'V',xhint,*span],out/(name+'-wallfit.txt'))
# Full registered coverage is measured separately. Center strips avoid dimension uncertainty at jambs.
probes={'source':digest(source),'probes':[
    {'id':'kitchen_foyer_upper','kind':'solid','polygon':cell(177,188,998,999.5)},
    {'id':'F3','kind':'clear','polygon':cell(179.06,189.94,895,901),'unverified_ends':['本次仅核对短框净腹，端接另查']}]}
for ident,x in [('D2',999),('D3',898)]:
    probes['probes'].append({'id':ident,'kind':'clear','polygon':cell(201,217,x-3,x+3),'unverified_ends':['仅核对门洞中央可通行性，另查铰接与端部']})
save(out/'probes.json',probes)
scan_spec={'plan_box_px':[684,132,1058,552],
           'samples':[{'material':'black_core','points_px':[[900,169],[940,169],[845,230]],'radius_px':1},
                      {'material':'gray_core','points_px':[[976,169],[846,169],[800,230]],'radius_px':1}],
           'axes':[{'id':name,'a_px':[x,lo],'b_px':[x,hi],'core_width_px':[3,9],'search_px':3,'min_run_px':3} for name,x,(lo,hi),_ in stations]}
save(out/'scan-source.json',scan_spec)
summary['timings']['local_source_scan']=timed_cli([sys.executable,scripts/'scan_wall_cores.py',source,out/'scan-source.json',out/'scan-output.json',out/'scan-output.png'])
for tag in 'KMR':
    q=out/tag;q.mkdir(exist_ok=True)
    dxf=root/'blind'/tag/'native/readback.dxf'
    dwg=root/'blind'/tag/'candidate.dwg'
    cap=jread(dxf.parent/'capture.json')
    assert digest(dwg)==jread(dwg.parent/'freeze.json')['files']['candidate.dwg']==cap['source_sha256']
    assert digest(dxf)==cap['files']['readback.dxf']
    cfg=jread(judge/'evidence'/tag/'config.json')
    mapping=jread(root/'staff/blind-audit'/tag/'controlled-registration.json')
    mapping['source']=digest(source);mapping['wall_layers']={k:'fills' for k,v in cfg['layer_roles'].items() if v in {'黑','灰'}}
    save(q/'mapping.json',mapping)
    t0=time.perf_counter()
    doc=ezdxf.readfile(dxf); geom=read_geometry(doc); save(q/'native_geometry.json',geom)
    rows=load_projected(q/'native_geometry.json',cfg)
    roles=cfg['layer_roles'];polys=filled(rows,roles);wall=unary_union([p for _,p in polys])
    m=mapping['source_to_mm'];sx=abs(m[0]);sy=abs(m[3])
    assert m[1]==m[2]==0 and 0<abs(sx/sy-1)<.005
    models=[extract_host(rows,polys,wall,roles,station,sy,sx) for station in stations]
    spec={'name':tag,'underlay':{'image':str(source),'x':0,'y':0,'mmPerPx':sy},'walls':[w for w,_ in models],'furniture':furniture(rows,roles,sy)}
    save(q/'readback-spec.json',spec)
    convert_seconds=time.perf_counter()-t0
    record={'dwg_sha256':digest(dwg),'readback_sha256':digest(dxf),'native_mapped_furniture_boxes':len(spec['furniture']),
            'registration_mm_per_px':[sx,sy],'adapter_single_s':convert_seconds,'models':[info for _,info in models],'wallaudit':{}}
    summary['candidates'][tag]=record
    record['existing_wall_probe_timing']=timed_cli([sys.executable,scripts/'check_wall_probes.py',dxf,out/'probes.json',q/'mapping.json'],q/'probe-output.json',allowed=(0,1,2))
    current=jread(q/'probe-output.json')
    record['existing_wall_probes']={k:{kk:r[kk] for kk in ['kind','status','wall_percent','clear_zone_status'] if kk in r} for k,r in current['probes'].items()}
    record['global_uncertainties']=current['global_uncertainties']
    # Re-run existing independent source-to-native checks on freshly decoded geometry.
    src_runs=root/'staff/registered-wall-runs.json'
    record['registered_runs_timing']=timed_cli([sys.executable,judge/'tools/independent_wall_runs.py',q/'native_geometry.json',judge/'evidence'/tag/'config.json',src_runs,q/'registered-runs.json',3.35])
    rc=jread(q/'registered-runs.json')['kitchen_foyer_upper']
    record['registered_wall']=rc
    record['registered_openings_timing']=timed_cli([sys.executable,judge/'tools/profile_openings.py',q/'native_geometry.json',judge/'evidence'/tag/'config.json',root/'staff/source-preregistered.json',q/'registered-openings.json'])
    record['registered_F3']=next(r for r in jread(q/'registered-openings.json') if r['id']=='F3')
    for shift in [-2,-1,1,2]:
        shifted={**spec,'underlay':{**spec['underlay'],'x':-shift*sy}}
        path=q/f'normal-shift-{shift:+d}.json'
        save(path,shifted)
        run=q/f'normal-shift-{shift:+d}'
        timed_cli([sys.executable,up/'wallaudit.py',path,'--out',run],repeats=1)
        issues=jread(run/'audit.json')
        spans=[(0,175.35,188.65),(1,182,189)]
        record.setdefault('normal_shift_sensitivity',{})[str(shift)]=[[[it['drawing'],it['model']] for it in issues
            if it['wall']==idx and min(it['to']/sy,hi)-max(it['from']/sy,lo)>=1] for idx,lo,hi in spans]
    for mode,options in [('default',[]),('no-mask',['--no-mask']),('fine',['--step','50','--min-len','150']),('fine-no-mask',['--step','50','--min-len','150','--no-mask'])]:
        run=q/mode
        elapsed=timed_cli([sys.executable,up/'wallaudit.py',q/'readback-spec.json','--out',run,*options])
        issues=jread(run/'audit.json')
        scored=[]
        for idx,(_,_,span,_) in enumerate(stations):
            inside=[it for it in issues if it['wall']==idx and it['from']/sy<=sum(span)/2<=it['to']/sy]
            scored.append([{'drawing':it['drawing'],'model':it['model'],'range_px':[round(it['from']/sy,2),round(it['to']/sy,2)]} for it in inside])
        registered_spans=[('kitchen_foyer_upper',0,175.35,188.65),('F3',1,182,189),('D2',0,201,217),('D3',1,201,217)]
        alerts={}
        counted=set()
        for ident,idx,start,end in registered_spans:
            idxs=[index for index,it in enumerate(issues) if it['wall']==idx and min(it['to']/sy,end)-max(it['from']/sy,start)>=1]
            alerts[ident]=idxs
            counted.update(idxs)
        record['wallaudit'][mode]={'timing':elapsed,'target_midpoint_alerts':scored,'all_issue_count':len(issues),
            'central_interval_alerts_by_id':alerts,'other_raw_issue_indices':sorted(set(range(len(issues)))-counted)}
save(out/'comparison.json',summary)
