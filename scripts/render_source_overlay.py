"""Inspect a frozen candidate in the original raster frame. Never writes to the CAD."""
from pathlib import Path
import argparse, json, math
from dimension_grid import GridProjection
import ezdxf
from ezdxf.path import make_path
from PIL import Image, ImageDraw
from shapely.geometry import Polygon
from shapely.ops import unary_union

PALETTE={'bearing':(99,45,146),'partition':(28,136,119),'door':(221,46,41),'window':(3,108,180),
 'movable':(166,94,4),'fixed':(48,90,20),'ancillary':(89,106,117)}
def compound(g):
 if g.geom_type=='Polygon':return [g]
 return [x for item in getattr(g,'geoms',()) for x in compound(item)]
def draw(source,model,reg,output,scale=2,plain_output=None):
 projection=GridProjection(reg)
 def px(points):
  coords=projection.source_path([(v[0],v[1]) for v in points])
  return [(round(x*scale),round(y*scale)) for x,y in coords if math.isfinite(x) and math.isfinite(y)]
 layers={v.casefold():k for k,v in reg['layers'].items()}
 raw=Image.open(source).convert('RGB');raw=raw.resize((raw.width*scale,raw.height*scale), Image.Resampling.BICUBIC)
 canvas=Image.new('RGBA',raw.size);dr=ImageDraw.Draw(canvas)
 modelspace=ezdxf.readfile(model).modelspace()
 def paint(ent,role,depth=0):
  kind=ent.dxftype()
  if kind=='INSERT':
   if depth<10:
    for sub in ent.virtual_entities():paint(sub,role,depth+1)
  elif kind=='HATCH' and role in ('bearing','partition'):
   from ezdxf.path import from_hatch
   area=Polygon()
   for path in from_hatch(ent):
    pts=[(v.x,v.y) for v in path.flattening(1)]
    if len(pts)<3:continue
    poly=Polygon(pts)
    if poly.is_valid:area=area.symmetric_difference(poly)
   tmp=Image.new('RGBA',raw.size);dt=ImageDraw.Draw(tmp)
   for poly in compound(area):
    if poly.is_empty:continue
    dt.polygon(px(poly.exterior.coords),fill=PALETTE[role]+(133,))
    for ring in poly.interiors:dt.polygon(px(ring.coords),fill=(0,0,0,0))
   canvas.alpha_composite(tmp)
  elif kind in ('LINE','ARC','CIRCLE','LWPOLYLINE','POLYLINE','ELLIPSE','SPLINE'):
   pts=px(make_path(ent).flattening(1.0))
   if len(pts)>1:dr.line(pts,fill=PALETTE[role]+(215,),width=max(1,round(scale*.55)))
  elif kind in ('SOLID','TRACE') and role in ('bearing','partition'):
   pts=px(ent.wcs_vertices())
   if len(pts)>2:dr.polygon(pts,fill=PALETTE[role]+(120,))
 for role in PALETTE:
  for ent in modelspace:
   if layers.get(ent.dxf.layer.casefold())==role:paint(ent,role)
 if plain_output:
  white=Image.new('RGBA',raw.size,'white')
  Image.alpha_composite(white,canvas).convert('RGB').save(plain_output)
 result=Image.alpha_composite(raw.convert('RGBA'),canvas)
 result.convert('RGB').save(output)
if __name__=='__main__':
 parser=argparse.ArgumentParser(description=__doc__)
 for name in ('source','readback','registration','overlay'):parser.add_argument(name,type=Path)
 parser.add_argument('--plain',type=Path);parser.add_argument('--scale',type=int,default=2,choices=range(1,5))
 args=parser.parse_args()
 draw(args.source,args.readback,json.loads(args.registration.read_text()),args.overlay,args.scale,args.plain)
