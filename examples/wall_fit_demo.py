import json,sys,math
from pathlib import Path
import ezdxf
from ezdxf.math import Matrix44
folder=Path(sys.argv[1]);folder.mkdir(parents=True,exist_ok=False)
d=ezdxf.new('R2018');d.units=4
for n in ['walls','furniture']:d.layers.new(n)
m=d.modelspace()
def wall(xy):
    h=m.add_hatch(color=7,dxfattribs={'layer':'walls'});h.paths.add_polyline_path(xy,is_closed=True)
def ins(name,xy,**opts):return m.add_blockref(name,xy,dxfattribs={'layer':'furniture',**opts})
def write(name,cfg):
    cfg.update(wall_layers={'walls':'fills'},max_move_mm=1800,max_slide_mm=1500)
    (folder/(name+'.json')).write_text(json.dumps(cfg,indent=2))
def v(m,xy):return tuple(m.transform((*xy,0)))[:2]
def direction(m,xy):return tuple(m.transform_direction((*xy,0)))[:2]
ang=math.radians(37); goal=Matrix44.scale(-1,1,1)@Matrix44.z_rotate(ang)@Matrix44.translate(5200,5100,0)
wall([v(goal,xy) for xy in [(-160,-160),(1650,-160),(1650,0),(0,0),(0,1450),(-160,1450)]])
st=d.blocks.new('L_middle');shape=st.add_lwpolyline([(0,0),(1200,0),(1200,450),(450,450),(450,1000),(0,1000)],close=True)
root=d.blocks.new('L_mirrored');child=root.add_blockref('L_middle',(0,0));ref=ins('L_mirrored',v(goal,(75,60)),rotation=37,xscale=-1)
pa=[ref.dxf.handle,child.dxf.handle,shape.dxf.handle]
backs=[dict(id='横肢实背',path=pa,segment=0,toward_wall=direction(goal,(0,-1)),near_face_mm=v(goal,(450,0)),pick_radius_mm=90),
       dict(id='竖肢实背',path=pa,segment=5,toward_wall=direction(goal,(-1,0)),near_face_mm=v(goal,(0,600)),pick_radius_mm=90)]
write('lcase',dict(backs=backs,physical_outlines=[dict(path=pa)]))
for x,X in [(0,1200),(1550,3000)]:wall([(x,0),(X,0),(X,160),(x,160)])
def cabinet(name,x,y,width):
    b=d.blocks.new(name);g=b.add_lwpolyline([(0,0),(width,0),(width,-420),(0,-420)],close=True)
    e=ins(name,(x,y));pa=[e.dxf.handle,g.dxf.handle]
    write(name,dict(backs=[dict(id='背',path=pa,segment=0,toward_wall=[0,1],near_face_mm=[800,0],pick_radius_mm=50)],physical_outlines=[dict(path=pa)]));return pa
cabinet('over_opening',1000,-45,800)
cabinet('point_only',1200,0,300)
cabinet('on_far_face',250,160,500)
def equipment(name,vertices,where):
    b=d.blocks.new(name);g=b.add_lwpolyline(vertices,close=True)
    ref=ins(name,where);return dict(path=[ref.dxf.handle,g.dxf.handle])
host=equipment('worktop',[(0,0),(1500,0),(1500,620),(0,620)],(1600,3500))
sink=equipment('basin',[(0,0),(560,0),(560,410),(0,410)],(1790,3570))
(folder/'sink.json').write_text(json.dumps({'host':host,'sink':sink}))
d.saveas(folder/'input.dxf')
