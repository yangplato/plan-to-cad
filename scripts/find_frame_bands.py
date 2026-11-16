"""Find short repeated parallel strokes along original wall axes, including side lights.
Input is source pixels only. Narrow louvers, joinery and letter strokes can look alike.
Use room type, true side of a core and an original crop to classify every suggestion.
"""
import argparse,json,math
from pathlib import Path
from itertools import combinations
import numpy as np
from scipy.ndimage import median_filter,binary_closing
from PIL import Image,ImageDraw,ImageOps

def runs(mask):
    z=np.diff(np.r_[False,mask,False].astype(int))
    return list(zip(np.flatnonzero(z==1),np.flatnonzero(z==-1)))

def sources(books,margin,bounds):
    src=[]
    for doc in books:
        for row in doc.get('portals',[])+doc.get('walls',[]):
            a,b=row.get('a'),row.get('b')
            if not (isinstance(a,list) and isinstance(b,list) and len(a)==len(b)==2):continue
            if not all(isinstance(v,(int,float)) for v in a+b):continue
            orientation=int(abs(b[1]-a[1])>abs(b[0]-a[0]))
            i=orientation;j=1-i
            if abs(a[j]-b[j])>max(3,abs(a[i]-b[i])*.12):continue
            n=(a[j]+b[j])/2;t0,t1=sorted((a[i],b[i]))
            if t1<=t0:continue
            src.append({'axis':'HV'[orientation],'center':n,'lo':max(bounds[i],t0-margin),
                        'hi':min(bounds[i+2]-1,t1+margin),'source':row.get('id','axis')})
    return src

def detect(mono,axis,scale):
    vertical=axis['axis']=='V';pic=mono.T if vertical else mono
    center=axis['center'];lo=max(0,math.floor(axis['lo']));hi=min(pic.shape[1]-1,math.ceil(axis['hi']))
    # Only near the registered wall plane. Both thin and broad window frames occur in plan.
    hw=max(4,math.ceil(230/scale));n0=max(2,math.floor(center-hw));n1=min(pic.shape[0]-3,math.ceil(center+hw))
    if n1<=n0 or hi<=lo:return []
    img=median_filter(pic[:,lo:hi+1],size=(1,3),mode='nearest')
    arr=img[n0:n1+1];left=img[n0-1:n1];right=img[n0+1:n1+2]
    # Accept an anti-aliased band with a stronger rising edge on one side; reject monotone steps.
    ridge=np.minimum(left-arr,right-arr)
    ridge=np.maximum(ridge,np.minimum(img[n0-2:n1-1]-arr,img[n0+2:n1+3]-arr)*.75)
    length_min=max(4,int(math.ceil(100/scale)))
    proposals=[]
    for i,j in combinations(range(len(arr)),2):
        sep=j-i
        if not 2<=sep<=max(4,round(170/scale)):continue
        mid=arr[i+1:j].max(axis=0)
        contrast=mid-np.maximum(arr[i],arr[j])
        dark=(ridge[i]>=4.5)&(ridge[j]>=4.5)&(contrast>=9.)
        # Inverted strokes (light frame on dark background) through the same interface.
        light_left=np.minimum(arr[i]-left[i],arr[i]-right[i])
        light_right=np.minimum(arr[j]-left[j],arr[j]-right[j])
        light=(light_left>=6)&(light_right>=6)&(np.minimum(arr[i],arr[j])-arr[i+1:j].min(axis=0)>=11)
        for sign,mask in [('d',dark),('l',light)]:
            strong=binary_closing(mask,structure=np.ones(3,dtype=bool))
            for start,stop in runs(strong):
                if stop-start<length_min or np.mean(mask[start:stop])<.55:continue
                proposals.append({'axis':axis['axis'],'lo':int(lo+start),'hi':int(lo+stop-1),
                    'center':float(n0+(i+j)/2),'strokes':[int(n0+i),int(n0+j)],'polarity':sign,
                    'source_axis':axis['source'],'support':round(float(np.mean(mask[start:stop])),2)})
    return proposals

def merge(rows,tol):
    kept=[]
    for r in sorted(rows,key=lambda v:-(v['hi']-v['lo'])):
        similar=False
        for k in kept:
            if r['axis']!=k['axis'] or abs(r['center']-k['center'])>tol:continue
            overlap=max(0,min(r['hi'],k['hi'])-max(r['lo'],k['lo'])+1)
            if overlap>=.72*(r['hi']-r['lo']+1):similar=True;break
        if not similar:kept.append(r)
    return sorted(kept,key=lambda v:(v['axis'],v['center'],v['lo']))

def output(src,rows,out):
    for i,row in enumerate(rows,1):
        n=row['center'];lo=row['lo'];hi=row['hi'];vertical=row['axis']=='V'
        left,top,right,bottom=(n-15,lo-12,n+16,hi+13) if vertical else (lo-12,n-15,hi+13,n+16)
        l=max(0,int(left));t=max(0,int(top));r=min(src.width,int(right)+1);b=min(src.height,int(bottom)+1)
        cut=src.crop((l,t,r,b))
        if vertical:cut=cut.transpose(Image.Transpose.ROTATE_90)
        z=min(6,510/cut.width,130/cut.height);sz=tuple(max(1,round(v*z)) for v in cut.size)
        raw=cut.resize(sz,Image.Resampling.NEAREST);enh=ImageOps.autocontrast(cut.convert('L'),cutoff=(3,2)).convert('RGB').resize(sz,Image.Resampling.NEAREST)
        card=Image.new('RGB',(max(240,sz[0]),sz[1]*2+23),'white')
        card.paste(raw,(0,23));card.paste(enh,(0,23+sz[1]))
        ImageDraw.Draw(card).text((2,3),f"{i:03d} {row['axis']} source span {lo}-{hi} @ {n:.1f}",fill='black')
        card.save(out/f'{i:03d}.png');row['card']=f'{i:03d}.png'

def portal_atlas(src,books,rows,scale,out):
    # Group all candidates by the actual source opening and both of its neighbouring
    # jamb strips. The reader sees the full uninterrupted raw axis first.
    out.mkdir(exist_ok=True)
    seen=[];index=[]
    for doc in books:
        for op in doc.get('portals',[]):
            pa,pb=np.asarray(op['a'],float),np.asarray(op['b'],float)
            k=int(abs(pb[1]-pa[1])>abs(pb[0]-pa[0]));axis='HV'[k];n=float((pa[1-k]+pb[1-k])/2)
            low,high=sorted((float(pa[k]),float(pb[k])))
            if high-low<2:continue
            if any(a==axis and abs(y-n)*scale<100 and abs(l-low)*scale<140 and abs(h-high)*scale<140 and typ==op['kind'] for a,y,l,h,typ in seen):continue
            seen.append((axis,n,low,high,op['kind']))
            pad=max(14,math.ceil(1600/scale));th=max(8,math.ceil(620/scale))
            bound=(low-pad,n-th,high+pad,n+th) if not k else (n-th,low-pad,n+th,high+pad)
            x,y,X,Y=[max(0,int(math.floor(bound[0]))),max(0,int(math.floor(bound[1]))),min(src.width,int(math.ceil(bound[2]))+1),min(src.height,int(math.ceil(bound[3]))+1)]
            raw=src.crop((x,y,X,Y))
            if k:raw=raw.transpose(Image.Transpose.TRANSPOSE)
            zoom=min(6,920/raw.width,225/raw.height)
            sz=tuple(max(1,round(v*zoom)) for v in raw.size)
            normal=raw.resize(sz,Image.Resampling.NEAREST)
            en=ImageOps.autocontrast(raw.convert('L'),cutoff=(3,2)).convert('RGB').resize(sz,Image.Resampling.NEAREST)
            sheet=Image.new('RGB',(sz[0],2*sz[1]+48),'white');sheet.paste(normal,(0,24));sheet.paste(en,(0,24+sz[1]))
            dr=ImageDraw.Draw(sheet);offset=y if k else x
            for s in (low,high):
                px=round((s-offset)*zoom);dr.line((px,12,px,23),fill='#d74224',width=2)
            oid=len(index)+1
            relevant=[row for row in rows if row['axis']==axis and abs(row['center']-n)<=max(3,120/scale) and row['hi']>=low-pad and row['lo']<=high+pad]
            split={'before':[],'inside':[],'after':[]}
            for row in relevant:
                mid=(row['lo']+row['hi'])/2
                zone='before' if mid<low else 'after' if mid>high else 'inside'
                split[zone].append(row['card'])
            dr.text((3,1),f"{oid:02d} {axis}  source | contrast       marks = observed jambs",fill='black')
            dr.text((3,sz[1]*2+26),f"{op['kind']}    before {len(split['before'])} / inside {len(split['inside'])} / after {len(split['after'])}",fill='black')
            name=f'{oid:02d}.png';sheet.save(out/name)
            index.append({'card':name,'input_id':op['id'],'input_kind':op['kind'],'source_axis':axis,'range_px':[low,high],'nearby_raw_bands':split,
              'inspect':'逐段对照两端外侧、两门垛间的薄框、错轨、收头和可关闭界面；像素候选可能为柜边。'})
    (out/'index.json').write_text(json.dumps(index,ensure_ascii=False,indent=2))

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('image',type=Path);p.add_argument('out',type=Path)
    p.add_argument('inventories',nargs='+',type=Path);p.add_argument('--mm-per-px',type=float,required=True)
    p.add_argument('--box',nargs=4,type=int);p.add_argument('--extend-mm',type=float,default=1600)
    a=p.parse_args();src=ImageOps.exif_transpose(Image.open(a.image)).convert('RGB');g=np.asarray(src.convert('L'),dtype=float)
    if a.mm_per_px<=0 or a.extend_mm<0:p.error('Use a positive scale and nonnegative extension')
    bs=a.box or [0,0,src.width,src.height]
    books=[json.loads(i.read_text()) for i in a.inventories]
    axes=sources(books,a.extend_mm/a.mm_per_px,bs)
    found=merge([s for ax in axes for s in detect(g,ax,a.mm_per_px)],max(2.,75/a.mm_per_px))
    a.out.mkdir(exist_ok=True,parents=True);output(src,found,a.out);portal_atlas(src,books,found,a.mm_per_px,a.out/'by-opening')
    (a.out/'frame-bands.json').write_text(json.dumps({'axes_read':len(axes),'candidates':found,'limits':'原图像素候选，包括细窗框、门轨、栏杆及家具纹线。查看原图邻接后再定类。'},ensure_ascii=False,indent=2));print(len(axes),'axes,',len(found),'parallel-stroke intervals')
