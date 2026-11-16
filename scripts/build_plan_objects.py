"""Compile one source-authored furniture schedule into the same millimetre CAD as the structure.

Model authors supply meanings, dimensions, source anchors and supports. The common
writer places actual families and generates a handle ledger for repeatable measurements.
"""
import argparse,hashlib,json,math
from functools import lru_cache
from pathlib import Path
import ezdxf
from ezdxf.math import Vec2
from ezdxf.path import make_path
from build_observed_structure import create_structure,LAYERS
from place_hosted_family import place_hosted_family, check_hosted_family, BEDS, NORMALS, _cabinet, _material
from align_furniture_to_wall import EPS, wallsides
from place_plan_families import catalog,place_family,place_dining
from place_plan_fixtures import place_fixture
from interior_block_library import BUNDLED, place_bundled, resolve_file


def prune_unused_import_layers(doc, roles):
    """Drop unused foreign layer definitions only in a newly compiled plan.

    Used geometry, nested blocks and nonprinting viewport references are retained.
    DXF layer names compare without case; removal records keep their table spelling.
    Do not apply this helper to the user's existing or manually edited drawing.
    """
    from presentation import PAPER_LAYERS,LEGACY_PAPER_LAYERS,modern_layers,prune_empty_layer_definitions
    if modern_layers(roles):return prune_empty_layer_definitions(doc)
    allowed={name.casefold() for name in
             set(roles.values())|set(LEGACY_PAPER_LAYERS.values())|{'0','Defpoints','VIEWPORTS','10-原图参照'}}
    used=set()
    for entity in doc.entitydb.values():
        if not entity.is_alive:continue
        if entity.dxf.is_supported('layer'):used.add(entity.dxf.layer.casefold())
        if entity.dxftype()=='VIEWPORT':used.update(name.casefold() for name in entity.frozen_layers)
    removed=[]
    for layer in list(doc.layers):
        name=layer.dxf.name
        if name.casefold() not in allowed and name.casefold() not in used:
            doc.layers.remove(name);removed.append(name)
    return removed


def system(scene):
    a,b,d,e,x0,y0=scene['source_to_mm']
    def world(p):return Vec2(a*p[0]+b*p[1]+x0,d*p[0]+e*p[1]+y0)
    return world


def draw_outline(doc,item,world,layer):
    points=[world(p) for p in item['outline_px']]
    if len(points)<3 or not item.get('function'):
        raise ValueError('A custom contour needs three or more perimeter points and its actual function')
    pos=points[0];vertices=[p-pos for p in points]
    from shapely.geometry import Polygon
    polygon=Polygon([tuple(v) for v in vertices])
    if not polygon.is_valid or polygon.area<=0:raise ValueError('Invalid custom footprint')
    code=hashlib.sha256(json.dumps([item['id'],list(map(tuple,vertices))]).encode()).hexdigest()[:12]
    block=doc.blocks.new('PLAN_BODY_'+code)
    block.block_record.dxf.units=4
    block.add_lwpolyline(vertices,close=True,dxfattribs={'layer':'0','lineweight':13})
    return doc.modelspace().add_blockref(block.name,pos,dxfattribs={'layer':layer})


@lru_cache(maxsize=32)
def library_geometry(file,block_name,front_x,front_y):
    library=ezdxf.readfile(file)
    block=library.blocks.get(block_name)
    if block is None or block.block.is_xref or block.block.is_xref_overlay:
        raise ValueError('Choose one explicit plan-view block in the neutral DXF library')
    front=Vec2(front_x,front_y).normalize()
    right=Vec2(-front.y,front.x)
    outlines=[]
    def collect(e):
        if e.dxftype()=='INSERT':
            for member in e.virtual_entities():collect(member)
        elif e.dxftype() in ('LINE','ARC','CIRCLE','ELLIPSE','LWPOLYLINE','POLYLINE','SPLINE'):
            points=[Vec2(v) for v in make_path(e).flattening(.35)]
            if len(points)>1:outlines.append([(v.dot(right),v.dot(front)) for v in points])
        elif e.dxftype() not in ('TEXT','MTEXT','ATTDEF','ATTRIB','WIPEOUT'):
            raise ValueError('Check unsupported neutral block member: '+e.dxftype())
    for e in block:collect(e)
    if not outlines:raise ValueError('Neutral block has no measurable plan geometry')
    us=[p[0] for path in outlines for p in path];vs=[p[1] for path in outlines for p in path]
    u0,u1=min(us),max(us);v0,v1=min(vs),max(vs)
    if min(u1-u0,v1-v0)<=0:raise ValueError('Neutral block must span both local axes')
    return [[((x-(u0+u1)/2)/(u1-u0),-(y-v0)/(v1-v0)) for x,y in path] for path in outlines]


def place_library(doc,row,world,layer,root):
    file=resolve_file(root,row.get('library_file'))
    if file==BUNDLED:
        back=world(row['back_px'])
        return place_bundled(doc,row,back,world(row['toward_px'])-back,layer)
    front=tuple(row['library_front_vector'])
    paths=library_geometry(str(file),row['library_block'],*front)
    code=hashlib.sha256(json.dumps([str(file),row['library_block'],front]).encode()).hexdigest()[:12]
    name='PLAN_LIB_'+code
    if name not in doc.blocks:
        block=doc.blocks.new(name);block.block_record.dxf.units=4
        for points in paths:
            closed=Vec2(points[0]).isclose(points[-1])
            block.add_lwpolyline(points[:-1] if closed else points,close=closed,dxfattribs={'layer':'0','lineweight':13})
    pos=world(row['back_px']);facing=world(row['toward_px'])-pos
    width,depth=float(row['width_mm']),float(row['depth_mm'])
    if min(width,depth)<=0 or not facing.magnitude:raise ValueError('Choose a complete width, depth and user-facing direction')
    return doc.modelspace().add_blockref(name,pos,dxfattribs={'layer':layer,'xscale':width,'yscale':depth,
        'rotation':math.degrees(math.atan2(facing.x,-facing.y))})


def continuous_support(ledger, item):
    """Join only the indicated contiguous, coplanar room-side faces when material changes."""
    ids=item.get('support_segments') or [item['host']]
    if len(ids)==1:return ledger,item
    walls={w['id']:w for w in ledger['walls']}
    chosen=[walls[k] for k in ids]
    if item['host'] not in ids or len(set(ids))!=len(ids):
        raise ValueError('Specify each support segment once and include the host')
    n=NORMALS[item['side']];ni=0 if n[0] else 1;ti=1-ni
    first=chosen[0]
    face=first['a'][ni]+n[ni]*first['width_mm']/2
    stations=[]
    for w in chosen:
        if abs(w['a'][ni]-w['b'][ni])>EPS or abs(w['a'][ni]+n[ni]*w['width_mm']/2-face)>EPS:
            raise ValueError('Back supports must lie on the same actual inside-face plane')
        stations.append(sorted((w['a'][ti],w['b'][ti])))
    stations.sort();low,high=stations[0]
    for left,right in stations[1:]:
        if left-high>EPS:raise ValueError('Named support segments do not meet')
        high=max(high,right)
    merged=dict(first);merged['id']='joined-for-furniture:'+item['id']
    merged['a']=list(first['a']);merged['b']=list(first['b'])
    merged['a'][ti]=low;merged['b'][ti]=high
    helper={**ledger,'walls':[*ledger['walls'],merged], 'openings':[]}
    for op in ledger['openings']:
        match=set(op['hosts']).intersection(ids)
        helper['openings'].append({**op,'hosts':[*op['hosts'],merged['id']]} if match else op)
    return helper,{**item,'host':merged['id']}


def place_on_source_wall(doc,ledger,row,scene,roles,world):
    table,item=continuous_support(ledger,row)
    def put(q):
        return place_hosted_family(doc,table,q,source_to_mm=scene['source_to_mm'],layers=roles)
    # In a multi-sided fit the source centre can exceed the available interval before
    # the shared endpoints are solved. Use it only as a seed; check the final solid below.
    defined = any(r.get('kind', 'touch') != 'unknown' for r in row.get('edge_relations', []))
    if defined and row.get('mode', 'centered') == 'centered':
        if row['family'] not in BEDS | {'cabinet'} or ('near_mm' in row) == ('near_px' in row):
            raise ValueError('Give a supported family and one observed centre for its multi-sided fit')
        anchor = world(row['near_px']) if 'near_px' in row else Vec2(row['near_mm'])
        facing = NORMALS[row['side']]; layer = roles[row['role']]
        if row['family'] == 'cabinet':
            width, depth = float(row['width_mm']), float(row['depth_mm'])
            if not (0 < width < math.inf and 0 < depth < math.inf): raise ValueError('Invalid cabinet size')
            ref = _cabinet(doc.modelspace(), width, depth, anchor, facing, layer)
        else:
            ref = place_family(doc.modelspace(), row['family'], anchor, facing,
                               width_mm=row.get('width_mm'), depth_mm=row.get('depth_mm'), layer=layer)
        return ref, {}
    try:
        return put(item)
    except ValueError as initial:
        zone=row.get('center_zone_px')
        if not zone:raise
        first_error=str(initial)
    n=NORMALS[row['side']];ni=0 if n[0] else 1;ti=1-ni
    at=world(row['near_px']) if 'near_px' in row else Vec2(row['near_mm'])
    lo,hi=sorted(world(p)[ti] for p in zone)
    if not lo<=at[ti]<=hi:raise ValueError('The original observed anchor must lie in the permitted zone')
    widths=catalog()
    width=row.get('width_mm') or widths.get(row['family'],{}).get('width_mm',0.)
    choices={float(lo),float(hi),float(at[ti])}
    for p,q,inward in wallsides(_material(doc,ledger)):
        if abs(p[ni]-q[ni])>EPS or inward[ni]*n[ni]>-.999:continue
        u,v=sorted((p[ti],q[ti]))
        choices.update([u+width/2,v-width/2,(u+v)/2])
    # Include intermediate positions for door-sweep and obstacle avoidance. Geometric
    # endpoints above retain exact CAD precision, regardless of the sampling grid.
    count=max(2,min(81,math.ceil((hi-lo)/30)+1))
    choices.update(lo+(hi-lo)*j/(count-1) for j in range(count))
    for station in sorted((z for z in choices if lo-EPS<=z<=hi+EPS), key=lambda z:abs(at[ti]-z)):
        point=list(at);point[ti]=station;trial=dict(item);trial.pop('near_px',None);trial['near_mm']=point
        try:
            ref,report=put(trial)
        except ValueError:continue
        report['source_zone_center_shift_mm']=station-at[ti];return ref,report
    raise ValueError('No fully supported, collision-free placement inside the observed center zone. '+first_error)


def compile_plan(scene,objects,library_root=None):
    from presentation import roles as plan_layers
    from dimension_grid import prepare_plan
    from object_edge_relations import apply_edges, edge_job
    from plan_sheet import prepare_sheet
    scene,objects=prepare_plan(scene,objects)
    doc,ledger=create_structure(scene,library_root)
    world=system(scene);msp=doc.modelspace();roles=plan_layers(scene)
    entities={};schedule={};chairs=[];contacts=[];fits={}
    from design_dimensions import enabled, is_bed, valid_bed_width
    for row in objects:
        if enabled(scene) and is_bed(row) and not valid_bed_width(row):
            raise ValueError(f"{row['id']}: 请选合适的床宽档，并检查床周围的使用净距")
        ident=row['id'];group=row['type'];family=row.get('family')
        if ident in schedule:raise ValueError('Duplicate object: '+ident)
        role=row['role']
        if role not in ('fixed','movable','ancillary'):raise ValueError('Expected functional object layer: '+ident)
        try:
            width,depth=row.get('width_mm'),row.get('depth_mm')
            if group=='hosted':
                ref,report=place_on_source_wall(doc,ledger,row,scene,roles,world)
                fits[ident]=report
                if ref.dxf.layer!=roles[role]:
                    raise ValueError('Hosted family layer conflicts with observed functional category')
                contact={k:row[k] for k in ('id','family','side')}
                if not row.get('support_segments'):contact['host']=row['host']
                contacts.append({**contact,'handle':ref.dxf.handle})
            elif group=='hosted_corner':
                from place_corner_on_walls import place_corner_on_walls
                if family!='corner_wardrobe' or role!='fixed':
                    raise ValueError('An installed corner wardrobe needs family corner_wardrobe and role fixed')
                ref,report=place_corner_on_walls(doc,ledger,row,source_to_mm=scene['source_to_mm'],layers=roles)
                fits[ident]=report
                contact={k:row[k] for k in ('id','family','side')}
                for segment_key,host_key in [('support_segments','host'),('return_support_segments','return_host')]:
                    segments=row.get(segment_key) or [row[host_key]]
                    if len(segments)==1:contact[host_key]=segments[0]
                contacts.append({**contact,'handle':ref.dxf.handle})
            elif group in ('fixture','family'):
                anchor=world(row['back_px']);toward=world(row['toward_px']);facing=toward-anchor
                if facing.magnitude<1e-8:raise ValueError('Mark a point in front of the rear edge')
                if group=='family':ref=place_family(msp,family,anchor,facing,width_mm=width,depth_mm=depth,layer=roles[role])
                else:ref=place_fixture(msp,family,anchor,facing,width,depth,layer=roles[role],**row.get('fixture_options',{}))
                if row.get('contact'):
                    contacts.append({'id':ident,'handle':ref.dxf.handle,**row['contact']})
            elif group=='library':
                ref=place_library(doc,row,world,roles[role],library_root)
            elif group=='outline':
                ref=draw_outline(doc,row,world,roles[role])
                if row.get('contact'):contacts.append({'id':ident,'handle':ref.dxf.handle,**row['contact']})
            elif group=='dining':
                center=world(row['center_px']);toward=world(row['axis_toward_px'])-center
                if toward.magnitude<1e-8:raise ValueError('Give the direction of the table long axis')
                settings=row['layout_mm']
                dining=place_dining(msp,row['table_family'],row['chair_family'],center,
                    angle_deg=toward.angle_deg,layer=roles[role],**settings)
                ref=dining['table']
                sides=[1]*len(settings['upper_offsets_mm'])+[-1]*len(settings['lower_offsets_mm'])
                normal=toward.normalize().orthogonal()
                seats=[]
                for i,(seat,sign) in enumerate(zip(dining['chairs'],sides),1):
                    key=ident+f'/chair-{i}'
                    if key in entities:raise ValueError('A generated chair ID overlaps an existing object: '+key)
                    entities[key]=seat
                    seats.append({'id':key,'handle':seat.dxf.handle,'pull_vector_mm':list(normal*sign)})
                chairs.append({'table_handle':ref.dxf.handle,'seats':seats,'pull_mm':row['pull_mm']})
            else:raise ValueError('Unknown object type '+group)
        except (ValueError,KeyError,TypeError) as err:
            raise ValueError(f"{ident}: {err}") from err
        schedule[ident]=row
        if ident in entities:raise ValueError('A furniture ID overlaps a generated chair ID')
        entities[ident]=ref
    relations=[]
    for ident,row in schedule.items():
        host=row.get('support')
        if host:
            if host not in entities:raise ValueError(f'{ident}: actual support {host} missing')
            relations.append({'handles':[entities[ident].dxf.handle,entities[host].dxf.handle],
                'host':entities[host].dxf.handle,'basis':row.get('support_reason','原图登记的独立台面支承该设备')})
    for group in chairs:
        for seat in group['seats']:
            relations.append({'handles':[group['table_handle'],seat['handle']],'basis':'整椅在桌板下方收放'})
    for rec in scene.get('intentional_projections',[]):
        pairs=rec['objects']
        if len(pairs)!=2 or not rec.get('basis'):raise ValueError('Each intentional projection needs two IDs and source evidence')
        relations.append({'handles':[entities[k].dxf.handle for k in pairs],'basis':rec['basis']})
    registration={k:scene[k] for k in ('source_sha256','holdouts','holdout_limit_mm','grid_registration') if k in scene}
    registration.update(source_to_mm=scene['source_to_mm'],layers=roles)
    drawing_ids={i:{'handle':e.dxf.handle,'layer':e.dxf.layer,'block':e.dxf.name} for i,e in entities.items()}
    for row in objects:
        if row['type'] == 'library':
            drawing_ids[row['id']]['source'] = {
                'file': row.get('library_file', BUNDLED.name),
                'block': row['library_block'],
            }
    edges=edge_job(objects,drawing_ids,scene.get('edge_review_reach_mm',75))
    placement=apply_edges(doc,ledger,edges,relations)
    for change in placement['moves']:
        ident=change['object']; row=schedule[ident]
        if row['type'] != 'hosted': continue
        table,item=continuous_support(ledger,row)
        actual=entities[ident]; item=dict(item); item.pop('near_px',None)
        item['near_mm']=[actual.dxf.insert.x,actual.dxf.insert.y]
        zone=row.get('center_zone_px')
        if zone:
            ti=1 if NORMALS[row['side']][0] else 0
            loc=item['near_mm'][ti]; low,high=sorted(world(p)[ti] for p in zone)
            if not low-EPS <= loc <= high+EPS: raise ValueError(f'{ident}: shared edges require a centre outside the source zone')
        fits[ident]=check_hosted_family(doc,table,item,actual,source_to_mm=scene['source_to_mm'])
        if not fits[ident]['fit']: raise ValueError(f'{ident}: edge-constrained placement lacks full solid backing or enters a door sweep')
    sheet=prepare_sheet(doc,roles,scene.get('paper',{}))
    removed_layers=prune_unused_import_layers(doc,roles)
    removed_layers=list(dict.fromkeys(sheet.get('empty_custom_layers_removed',[])+removed_layers))
    sheet['unused_import_layer_definitions_removed']=removed_layers
    from presentation import layer_spaces
    sheet['layer_spaces']=layer_spaces(doc)
    return doc,{'edge-relations':edges,'edge-placement':placement,'sheet':sheet,'ledger':ledger,'registration':registration,'objects':drawing_ids,'contacts':{'items':contacts},'relations':relations,'dining':chairs,'placement-measures':fits}


if __name__=='__main__':
    cli=argparse.ArgumentParser(description=__doc__);cli.add_argument('scene',type=Path);cli.add_argument('schedule',type=Path);cli.add_argument('out',type=Path)
    cli.add_argument('--library-root',type=Path)
    args=cli.parse_args()
    if args.out.exists() and any(args.out.iterdir()):cli.error('Use a new version folder')
    doc,outputs=compile_plan(json.loads(args.scene.read_text()),json.loads(args.schedule.read_text()),args.library_root)
    args.out.mkdir(parents=True,exist_ok=True);doc.saveas(args.out/'source.dxf')
    for name,report in outputs.items():(args.out/(name+'.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(len(outputs['objects']),'object handles; generated',args.out)
