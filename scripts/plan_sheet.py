"""Style one editable model and proof its saved A3 layout from an AutoCAD DWG readback."""
import argparse
import json
import math
from pathlib import Path

import ezdxf
from ezdxf import bbox
from ezdxf.math import BoundingBox2d
from ezdxf.path import make_path
from shapely.geometry import LineString
from shapely.ops import polygonize, unary_union

# Model ACI and paper lineweight in hundredths of mm. Standard monochrome.ctb
# uses the object's lineweight; explicit grey wall hatches use true colour.
PENS = {'bearing':(8,40),'partition':(9,35),'door':(3,18),'window':(4,20),
        'fixed':(151,25),'movable':(30,20),'dimension':(4,15),'ancillary':(8,13)}
LEGACY_PENS={**PENS,'dimension':(7,15)}
LAYOUT='Plan-A3'
CTB='monochrome.ctb'
SHEET=(420.,297.)
MARGIN=10.
# Fixed paper coordinates; model geometry is never scaled to fit this frame.
VIEW_RECT=(15.,76.,405.,273.)
FONT_STYLE="PLAN-SHEET-TEXT"
WALL_GREYS={"bearing":66,"partition":178}
REQUIRED_METADATA=("project","unit_type","drawing_title","revision","date","source","limitations")
DEFAULT_METADATA=dict(project='项目未提供',unit_type='户型未提供',drawing_title='户型平面图',
    revision='版本未提供',date='日期未提供',source='营销原图；文件名未提供',
    limitations='按原图重建；未作现场复核；未标尺寸为推定。',units='mm')


def style_blocks(doc,layers):
    """On existing plan block paths, mark genuine outer edges and keep interior strokes fine."""
    seen=set()
    def style(name):
        if name in seen:return
        seen.add(name)
        paths=[]
        for entity in doc.blocks[name]:
            if entity.dxftype()=='INSERT':
                entity.dxf.lineweight=-2
                style(entity.dxf.name)
            elif entity.dxftype() in ('LINE','LWPOLYLINE','POLYLINE','CIRCLE','ELLIPSE','ARC','SPLINE'):
                vertices=[(v.x,v.y) for v in make_path(entity).flattening(.15)]
                if len(vertices)>1:
                    path=LineString(vertices)
                    if path.length:paths.append((entity,path))
        if not paths:return
        faces=list(polygonize(unary_union([p for _,p in paths])))
        outline=unary_union(faces).boundary if faces else None
        for entity,path in paths:
            if outline is None:
                entity.dxf.lineweight=-2
                continue
            # Paths may be in unit-block coordinates and are subsequently scaled.
            tol=max(path.length*1e-8,1e-9)
            outer=path.intersection(outline.buffer(tol)).length
            entity.dxf.lineweight=-2 if outer>=path.length*.5 else 13
    for ref in doc.modelspace().query('INSERT'):
        if ref.dxf.layer in (layers['movable'],layers['fixed'],layers['ancillary']):
            style(ref.dxf.name)


def sheet_font(configured=None):
    """Use an installed Unicode font; never download or silently replace CJK."""
    candidates=[Path(configured).expanduser()] if configured else [
        Path('/System/Library/Fonts/Supplemental/Arial Unicode.ttf'),
        Path('/System/Library/Fonts/PingFang.ttc'),
        Path('C:/Windows/Fonts/simhei.ttf'),
    ]
    for candidate in candidates:
        if candidate.is_file():return str(candidate.resolve())
    raise FileNotFoundError('Set paper.font to an installed Unicode font file')


def _paper_tag(entity,kind):
    from presentation import SHEET_APP
    if SHEET_APP not in entity.doc.appids:entity.doc.appids.add(SHEET_APP)
    entity.set_xdata(SHEET_APP,[(1000,kind)])
    return entity


def _wrap(text,width,height,font):
    from ezdxf.fonts import fonts
    face=fonts.make_font(font,height)
    lines=[]
    for paragraph in str(text).splitlines() or ['']:
        line=''
        for char in paragraph:
            if line and face.text_width(line+char)>width:
                lines.append(line);line=char
            else:line+=char
        lines.append(line)
    return lines


def _room_width_factor(value):
    """Limit deliberate room-name compression; preserve the old width by default."""
    if isinstance(value,bool):raise ValueError('Room label width_factor must be a finite number from 0.6 to 1.0')
    try:factor=float(value)
    except (TypeError,ValueError):
        raise ValueError('Room label width_factor must be a finite number from 0.6 to 1.0') from None
    if not math.isfinite(factor) or not .6<=factor<=1.:
        raise ValueError('Room label width_factor must be a finite number from 0.6 to 1.0')
    return factor


def _cell(paper,text,box,kind,font,height=3.5,max_lines=2,layer=None):
    """Write actual CAD TEXT, wrapping by installed-font width inside its cell."""
    from ezdxf.enums import TextEntityAlignment
    from presentation import annotation_layers,resolve_roles
    PAPER_LAYERS=annotation_layers(resolve_roles(paper.doc))
    left,bottom,right,top=box
    lines=_wrap(text,right-left-6,height,font)
    step=height*1.45
    if len(lines)>max_lines or len(lines)*step>top-bottom-1:
        raise ValueError(f'Sheet {kind} text does not fit its paper cell; shorten it')
    baseline=top-2-height
    entities=[]
    for line in lines:
        e=paper.add_text(line,dxfattribs={'layer':layer or PAPER_LAYERS['title'],
            'style':FONT_STYLE,'height':height,'color':256})
        e.set_placement((left+3,baseline),align=TextEntityAlignment.LEFT)
        entities.append(_paper_tag(e,kind));baseline-=step
    return entities


def _draw_paper(doc,paper,vp,scale,settings,font,layers):
    from presentation import annotation_layers,modern_layers
    PAPER_LAYERS=annotation_layers(layers);modern=modern_layers(layers)
    from ezdxf.enums import TextEntityAlignment
    metadata=dict(settings.get('metadata') or {})
    missing=[key for key in REQUIRED_METADATA if not str(metadata.get(key,'')).strip()]
    meta={key:str(metadata.get(key) or value) for key,value in DEFAULT_METADATA.items()}
    if meta['units'].casefold()!='mm':raise ValueError('The plan model and paper legend require millimetres (mm)')
    frame=_paper_tag(paper.add_lwpolyline([(10,10),(410,10),(410,287),(10,287)],close=True,
        dxfattribs={'layer':PAPER_LAYERS['frame']}),'frame')
    # Persist the minimum declared room scope in actual CAD XDATA. A later
    # readback must compare that scope with the actual room TEXT entities.
    from presentation import SHEET_APP
    expected=settings.get('expected_room_names')
    if expected is None:expected=[str(row.get('name','')).strip() for row in settings.get('room_labels') or []]
    if not isinstance(expected,(list,tuple)) or any(not isinstance(v,str) or not v.strip() for v in expected):
        raise ValueError('expected_room_names must be a list of nonempty room names')
    expected=list(dict.fromkeys(v.strip() for v in expected))
    frame.set_xdata(SHEET_APP,[(1000,'frame'),(1000,'contract-v1')]+
        [(1000,'missing:'+key) for key in missing]+
        [(1000,'expected-room:'+name) for name in expected]+
        [(1000,'wall-layer:'+role+':'+layers[role]) for role in WALL_GREYS]+
        ([(1000,'room-space:model')] if modern else []))
    def line(a,b):paper.add_line(a,b,dxfattribs={'layer':PAPER_LAYERS['title']})
    line((10,70),(410,70));line((10,273),(410,273));line((150,10),(150,70))
    for y in (22,34,46,58):line((150,y),(410,y))
    line((280,46),(280,58))
    for x in (220,300,355):line((x,34),(x,46))
    _cell(paper,meta['drawing_title'],(150,58,410,70),'drawing_title',font,5.,1)
    _cell(paper,'项目：'+meta['project'],(150,46,280,58),'project',font)
    _cell(paper,'户型：'+meta['unit_type'],(280,46,410,58),'unit_type',font)
    for text,box,kind in [
        ('版本：'+meta['revision'],(150,34,220,46),'revision'),
        ('日期：'+meta['date'],(220,34,300,46),'date'),
        ('比例 1:'+str(scale),(300,34,355,46),'scale'),
        ('单位 '+meta['units'],(355,34,410,46),'units'),
        ('来源：'+meta['source'],(150,22,410,34),'source'),
        ('限制：'+meta['limitations'],(150,10,410,22),'limitations')]:
        _cell(paper,text,box,kind,font)
    _cell(paper,meta['project']+' · '+meta['unit_type'],(10,273,410,287),'heading',font,4.5,1)
    actual_wall_roles={role for role in WALL_GREYS if any(e.dxf.layer==layers[role] for e in doc.modelspace())}
    legend=settings.get('legend')
    if legend is None:legend=[row for row in [
        {'role':'bearing','label':'深色墙：按原图深浅分类'},
        {'role':'partition','label':'浅色墙：按原图深浅分类'}] if row['role'] in actual_wall_roles]
    if len(legend)!=len(actual_wall_roles) or {row.get('role') for row in legend}!=actual_wall_roles:
        raise ValueError('Paper legend must identify exactly the source-tone wall categories present in the model')
    _cell(paper,'墙类图例（按源图表达）',(10,58,150,70),'legend_heading',font,3.5,1,PAPER_LAYERS['legend'])
    for y,item in zip((48,35),legend):
        grey=WALL_GREYS[item['role']]
        h=paper.add_hatch(dxfattribs={'layer':PAPER_LAYERS['legend']})
        h.rgb=(grey,grey,grey);h.paths.add_polyline_path([(15,y),(27,y),(27,y+5),(15,y+5)],is_closed=True)
        _paper_tag(h,'legend_'+item['role'])
        paper.add_lwpolyline([(15,y),(27,y),(27,y+5),(15,y+5)],close=True,
            dxfattribs={'layer':PAPER_LAYERS['legend']})
        _cell(paper,item['label'],(29,y-2,150,y+10),'legend_label',font,3.5,1,PAPER_LAYERS['legend'])
    _cell(paper,'墙类未经结构鉴定；以本图来源为据。',(10,10,150,32),
          'legend_limit',font,3.5,3,PAPER_LAYERS['legend'])
    def project(point):
        return (vp.dxf.center.x+(float(point[0])-vp.dxf.view_center_point.x)/scale,
                vp.dxf.center.y+(float(point[1])-vp.dxf.view_center_point.y)/scale)
    default_width=_room_width_factor(settings.get('room_label_width_factor',1.))
    room_records=[]
    obstacles = None
    if modern and settings.get("check_leaders") and any(
            row.get("anchor_mm") is not None for row in settings.get("room_labels", [])):
        from check_cad_logic import straight_parts
        from check_registered_frames import expand
        from source_geometry_review import wall_material
        wall_layers = [layers[k] for k in WALL_GREYS if layers[k] in doc.layers]
        _, wall, issues = wall_material(doc,wall_layers,[1,0,0,1,0,0])
        if issues:
            raise ValueError("Resolve unreadable walls before routing room leaders")
        frame = [line for entity, layer, _ in expand(doc)[0]
                 if layer in (layers["window"], layers["ancillary"])
                 for line in straight_parts(entity)]
        obstacles = wall.buffer(-.05), frame
    for item in settings.get('room_labels') or []:
        name=str(item.get('name','')).strip();point=item.get('point_mm')
        if not name or not point or len(point)!=2 or not all(math.isfinite(float(v)) for v in point):
            raise ValueError('Each paper room label needs a name and finite model point_mm [x,y]')
        height=float(item.get('height_paper_mm',3.5))
        if not 3.5<=height<=5:raise ValueError('Paper room labels use 3.5 to 5 mm text')
        width=_room_width_factor(item.get('width_factor',default_width))
        x,y=project(point)
        left,bottom,right,top=VIEW_RECT
        if not left+5<x<right-5 or not bottom+5<y<top-5:
            raise ValueError('Room label falls outside the saved paper viewport: '+name)
        target=doc.modelspace() if modern else paper
        text_height=height*scale if modern else height
        place=tuple(map(float,point)) if modern else (x,y)
        e=target.add_text(name,dxfattribs={'layer':PAPER_LAYERS['rooms'],'style':FONT_STYLE,
            'height':text_height,'width':width,'color':256})
        e.set_placement(place,align=TextEntityAlignment.MIDDLE_CENTER);_paper_tag(e,'room_label')
        record=dict(name=name,point_mm=list(map(float,point)),paper_point_mm=[x,y],
                    height_paper_mm=height,width_factor=width,handle=e.dxf.handle,
                    space='model' if modern else 'paper',model_text_height_mm=text_height if modern else None)
        anchor=item.get('anchor_mm')
        if anchor is not None:
            if len(anchor)!=2 or not all(math.isfinite(float(v)) for v in anchor):
                raise ValueError('Room leader anchor_mm must contain two finite model coordinates')
            ax,ay=project(anchor)
            if not left+1<ax<right-1 or not bottom+1<ay<top-1:
                raise ValueError('Room leader anchor falls outside the saved viewport: '+name)
            # The line terminates above/below the text, so it does not strike
            # through the room name. It is a real editable paper LINE.
            start=(place[0],place[1]+text_height*.85 if ay>=y else place[1]-text_height*.85)
            endpoint=tuple(map(float,anchor)) if modern else (ax,ay)
            if obstacles:
                stroke = LineString([start, endpoint])
                wall, frame = obstacles
                if stroke.intersection(wall).length > .05 or any(stroke.crosses(s) for s in frame):
                    raise ValueError(f"{name}: room leader crosses wall or frame; move label into clear space")
            line_entity=_paper_tag(target.add_line(start,endpoint,
                dxfattribs={'layer':PAPER_LAYERS['rooms']}),'room_leader')
            record.update(anchor_mm=list(map(float,anchor)),leader_handle=line_entity.dxf.handle)
        room_records.append(record)
    return meta,missing,legend,room_records


def prepare_sheet(doc,layers,settings=None,*,custom_scale=False):
    """Create editable A3 paper annotations and a locked 1:scale model viewport.

    Optional settings.metadata: project, unit_type, drawing_title, revision,
    date, units (mm), source, limitations. settings.room_labels is a list of
    {name, point_mm:[model_x,model_y], height_paper_mm:3.5, optional anchor_mm,
    optional width_factor}. room_label_width_factor defaults to 1.0; each label
    may override it. Permitted factors are 0.6 to 1.0, at unchanged text height.
    expected_room_names can declare a larger minimum room scope. Without it,
    the supplied labels declare that scope. Old callers can create candidates.
    """
    from presentation import annotation_layers,modern_layers,SHEET_APP,layer_spaces,prune_empty_layer_definitions
    settings=dict(settings or {})
    modern=modern_layers(layers);PAPER_LAYERS=annotation_layers(layers)
    pens=PENS if modern else LEGACY_PENS
    for role,(colour,pen) in pens.items():
        if layers[role] in doc.layers:
            layer=doc.layers.get(layers[role]);layer.dxf.color=colour;layer.dxf.lineweight=pen
            if modern and role=='dimension':layer.dxf.discard('true_color')
    for role,name in PAPER_LAYERS.items():
        if name not in doc.layers:doc.layers.add(name,color=4 if modern and role!='frame' else 7,
                                              lineweight=35 if role=='frame' else 13)
        if modern:
            layer=doc.layers.get(name);layer.dxf.color=7 if role=='frame' else 4
            layer.dxf.discard('true_color')
    # Rebuilding a candidate replaces only the room annotations created by this
    # writer. Existing manually authored model text is never renamed or deleted.
    if modern:
        for entity in list(doc.modelspace()):
            if entity.has_xdata(SHEET_APP) and entity.get_xdata(SHEET_APP)[0].value in ('room_label','room_leader'):
                doc.modelspace().delete_entity(entity)
    font=sheet_font(settings.get('font'))
    if FONT_STYLE not in doc.styles:doc.styles.add(FONT_STYLE,font=font)
    else:doc.styles.get(FONT_STYLE).dxf.font=font
    for e in doc.modelspace():
        if e.dxftype()=='ARC' and e.dxf.layer==layers['door']:e.dxf.lineweight=13
        if e.dxftype()=='HATCH':
            for role,grey in WALL_GREYS.items():
                if e.dxf.layer==layers[role]:e.rgb=(grey,grey,grey)
        if modern and e.dxf.layer==layers['dimension']:
            e.dxf.color=256;e.dxf.discard('true_color')
            if e.dxftype()=='DIMENSION':
                colours=e.override()
                for key in ('dimclrd','dimclre','dimclrt'):
                    setattr(doc.dimstyles.get(e.dxf.dimstyle).dxf,key,256)
                    colours[key]=256
                colours.commit()
    style_blocks(doc,layers)
    printable=lambda:[e for e in doc.modelspace() if doc.layers.get(e.dxf.layer).dxf.plot
        and not doc.layers.get(e.dxf.layer).is_off() and not doc.layers.get(e.dxf.layer).is_frozen()]
    ext=bbox.extents(printable())
    if not ext.has_data:raise ValueError('A plotted plan must have model geometry')
    left,bottom,right,top=VIEW_RECT;available=(right-left,top-bottom)
    def fits(scale,e):return e.size.x<=available[0]*scale and e.size.y<=available[1]*scale
    choice=settings.get('scale')
    scales=[50,100,200]
    if choice is not None:
        value=float(choice)
        if not math.isfinite(value) or value<=0:
            raise ValueError('Print scale denominator must be positive and finite')
        if not custom_scale and value not in scales:
            raise ValueError('A3 supports scale denominators 50, 100 or 200')
        scales=[int(value) if value.is_integer() else value]
    for scale in scales:
        if not fits(scale,ext):continue
        if 'PLAN-MM' in doc.dimstyles:
            style=doc.dimstyles.get('PLAN-MM');style.dxf.dimtxt=2.5*scale;style.dxf.dimasz=1.7*scale
            style.dxf.dimlwd=PENS['dimension'][1];style.dxf.dimlwe=PENS['dimension'][1]
            style.dxf.dimscale=1;style.dxf.dimexo=.8*scale;style.dxf.dimexe=1.*scale
            for en in doc.modelspace().query('DIMENSION'):
                if en.dxf.dimstyle==style.dxf.name:
                    from presentation import APP,position
                    if en.has_xdata(APP):position(en,scale)
                    else:en.override().render()
        ext=bbox.extents(printable())
        if fits(scale,ext):break
    else:raise ValueError('The model does not fit the chosen A3 scale and title-block reservation')
    if LAYOUT in doc.layouts:doc.layouts.delete(LAYOUT)
    paper=doc.layouts.new(LAYOUT)
    paper.page_setup(size=SHEET,margins=(0,0,0,0),units='mm',scale=(1,1),
        name='ISO_full_bleed_A3',device='DWG To PDF.pc3')
    paper.dxf_layout.dxf.paper_size='ISO_full_bleed_A3_(420.00_x_297.00_MM)'
    paper.dxf_layout.dxf.plot_type=5;paper.set_plot_style(CTB);paper.plot_viewport_borders(False)
    viewport_layer=PAPER_LAYERS.get('viewport','Defpoints')
    vp=paper.add_viewport(center=((left+right)/2,(bottom+top)/2),size=available,
        view_center_point=ext.center.xy,view_height=available[1]*scale,dxfattribs={'layer':viewport_layer})
    vp.dxf.flags|=16384;doc.layers.get(viewport_layer).dxf.plot=0
    meta,missing,legend,rooms=_draw_paper(doc,paper,vp,scale,settings,font,layers)
    removed=prune_empty_layer_definitions(doc) if modern else []
    checks=audit_sheet(doc)
    if checks['structural_issues']:raise ValueError('; '.join(checks['structural_issues']))
    return dict(layout=LAYOUT,paper_mm=list(SHEET),scale=scale,text_paper_mm=2.5,
        ctb=CTB,pens_mm={k:v[1]/100 for k,v in pens.items()},metadata=meta,
        metadata_missing=missing,legend=legend,room_labels=rooms,font=font,
        room_label_width_factor=_room_width_factor(settings.get('room_label_width_factor',1.)),
        room_label_space='model' if modern else 'paper',layer_spaces=layer_spaces(doc),
        empty_custom_layers_removed=removed,
        viewport_handle=vp.dxf.handle,viewport_rect_mm=list(VIEW_RECT),paper_audit=checks)


def audit_sheet(doc):
    """Inspect actual paper entities, not PDF decorations or a metadata promise."""
    from presentation import annotation_layers,resolve_roles,modern_layers,SHEET_APP,layer_spaces
    roles=resolve_roles(doc);modern=modern_layers(roles);PAPER_LAYERS=annotation_layers(roles)
    issues=[];incomplete=[];kinds={};labels=[];entities={};contract=[]
    if LAYOUT not in doc.layouts:return dict(passed=False,status='incomplete',
        issues=['缺少Plan-A3真实纸空间布局'],structural_issues=['缺少Plan-A3真实纸空间布局'],completeness_issues=[])
    paper=doc.layouts.get(LAYOUT)
    for e in paper:
        if e.has_xdata(SHEET_APP):
            tags=e.get_xdata(SHEET_APP);kind=tags[0].value
            kinds.setdefault(kind,[]).append(e.dxf.handle);entities.setdefault(kind,[]).append(e)
            if kind=='frame':contract.extend(t.value for t in tags[1:] if t.code==1000)
            if kind=='room_label':labels.append(e.dxf.handle)
            layer=doc.layers.get(e.dxf.layer)
            if layer.is_off() or layer.is_frozen() or not layer.dxf.plot or e.dxf.get('invisible',0):
                issues.append(e.dxf.handle+': 图框、图签或图例在纸空间不可见或不出图')
            if modern and e.dxftype()=='TEXT' and (
                    layer.dxf.color!=4 or e.dxf.color!=256 or e.dxf.hasattr('true_color')):
                issues.append(e.dxf.handle+': 文字须为ACI4图层及BYLAYER颜色')
    room_space='model' if 'room-space:model' in contract else 'paper'
    if room_space=='model':
        if entities.get('room_label'):issues.append('新版房名不得重复留在纸空间')
        for e in doc.modelspace():
            if not e.has_xdata(SHEET_APP):continue
            kind=e.get_xdata(SHEET_APP)[0].value
            if kind in ('room_label','room_leader'):
                kinds.setdefault(kind,[]).append(e.dxf.handle);entities.setdefault(kind,[]).append(e)
                if kind=='room_label':labels.append(e.dxf.handle)
    required=(*REQUIRED_METADATA,'units','scale','frame')
    for key in required:
        if key not in kinds:issues.append('纸空间缺少实际图元：'+key)
    from presentation import LEGACY
    wall_layers={v.split(':',2)[1]:v.split(':',2)[2] for v in contract if v.startswith('wall-layer:')}
    if not wall_layers:wall_layers={role:roles[role] if roles[role] in doc.layers else LEGACY[role] for role in WALL_GREYS}
    actual_wall_roles={role for role,name in wall_layers.items() if any(e.dxf.layer==name for e in doc.modelspace())}
    for role in WALL_GREYS:
        if role in actual_wall_roles and 'legend_'+role not in kinds:issues.append('纸空间缺少实际采用墙类图例：'+role)
        if role not in actual_wall_roles and 'legend_'+role in kinds:incomplete.append('纸空间列入未采用墙类：'+role)
    declared_missing={v.split(':',1)[1] for v in contract if v.startswith('missing:')}
    prefixes=dict(project='项目：',unit_type='户型：',revision='版本：',date='日期：',source='来源：',limitations='限制：')
    actual_metadata={}
    for key in REQUIRED_METADATA:
        rows=entities.get(key,[])
        texts=[e.dxf.text for e in rows if e.dxftype()=='TEXT']
        value=''.join(texts).removeprefix(prefixes.get(key,'' )).strip()
        actual_metadata[key]=value
        if not value or len(texts)!=len(rows) or '未提供' in value or (
                key in declared_missing and value==DEFAULT_METADATA[key]):
            incomplete.append('纸空间元数据未完成：'+key)
    expected=[v.split(':',1)[1] for v in contract if v.startswith('expected-room:')]
    actual_names=[e.dxf.text.strip() for e in entities.get('room_label',[]) if e.dxftype()=='TEXT']
    viewport_layer=PAPER_LAYERS.get('viewport','Defpoints')
    vps=[v for v in paper.query('VIEWPORT') if v.dxf.flags&16384 and v.dxf.layer==viewport_layer]
    scale=vps[0].dxf.view_height/vps[0].dxf.height if len(vps)==1 else 1.
    actual_room_labels=[]
    for e in entities.get('room_label',[]):
        if e.dxftype()!='TEXT':continue
        actual_room_labels.append(dict(name=e.dxf.text.strip(),handle=e.dxf.handle,
            height_paper_mm=e.dxf.height/scale if room_space=='model' else e.dxf.height,
            model_text_height_mm=e.dxf.height if room_space=='model' else None,
            width_factor=e.dxf.width,space=room_space))
        layer=doc.layers.get(e.dxf.layer)
        if e.dxf.layer!=PAPER_LAYERS['rooms']:issues.append(e.dxf.handle+': 房名层错误')
        if layer.is_off() or layer.is_frozen() or not layer.dxf.plot or e.dxf.get('invisible',0):
            issues.append(e.dxf.handle+': 房名在所属空间不可见或不出图')
        if room_space=='model':
            if layer.dxf.color!=4 or e.dxf.color!=256 or e.dxf.hasattr('true_color'):
                issues.append(e.dxf.handle+': 房名须为ACI4图层及BYLAYER颜色')
            if any(e.dxf.layer.casefold() in {name.casefold() for name in vp.frozen_layers} for vp in vps):
                issues.append(e.dxf.handle+': 房名层在纸空间视口被冻结')
            if e.dxf.height/scale<3.5:issues.append(e.dxf.handle+': 模型房名折算纸空间名义高度小于3.5mm')
        try:_room_width_factor(e.dxf.width)
        except ValueError:issues.append(e.dxf.handle+': 纸空间房名宽度系数须为0.6至1.0')
    if not expected:incomplete.append('未声明必要房名范围，纸图完整性待核')
    for name in expected:
        if actual_names.count(name)!=1:incomplete.append('必要房名应有一个实际TEXT：'+name)
    if any(not name or '未提供' in name for name in actual_names):incomplete.append('房名包含空值或占位内容')
    for name in set(actual_names)-set(expected):incomplete.append('实际房名不在声明范围：'+name)
    if len(vps)!=1:issues.append('纸空间须有一个锁定的户型视口')
    for e in doc.modelspace():
        paper_only={name for role,name in PAPER_LAYERS.items() if room_space!='model' or role!='rooms'}
        if e.dxf.layer in paper_only:issues.append(e.dxf.handle+': 图签专用层不得放入模型空间')
    drawn=[e for e in paper if e.dxftype()!='VIEWPORT' and doc.layers.get(e.dxf.layer).dxf.plot]
    ext=bbox.extents(drawn)
    if (not ext.has_data or ext.extmin.x<9.5 or ext.extmin.y<9.5
            or ext.extmax.x>410.5 or ext.extmax.y>287.5):issues.append('纸空间图框、文字或图例超出图框')
    for e in paper:
        if e.dxftype()=='TEXT' and e.dxf.layer in (PAPER_LAYERS['title'],PAPER_LAYERS['rooms'],PAPER_LAYERS['legend']) and e.dxf.height<3.5:
            issues.append(e.dxf.handle+': 纸空间中文文字小于3.5mm')
    spaces=layer_spaces(doc)
    if modern:
        for row in spaces:
            if row['empty'] and not row['system']:
                issues.append('空自定义图层：'+row['name'])
    return dict(passed=not issues and not incomplete,status='complete' if not issues and not incomplete else 'incomplete',
        issues=issues+incomplete,structural_issues=issues,completeness_issues=incomplete,
        actual_metadata=actual_metadata,expected_room_names=expected,actual_room_names=actual_names,
        actual_room_labels=actual_room_labels,
        room_label_space=room_space,layer_spaces=spaces,
        actual_wall_roles=sorted(actual_wall_roles),
        actual_paper_entity_count=len(drawn),tagged_entity_kinds={k:len(v) for k,v in kinds.items()},room_label_count=len(labels))


def stock_ctb():
    dirs=sorted(Path('/Applications/Autodesk').glob('AutoCAD */AutoCAD *.app/Contents/Resources/UserDataCache/Plotters/Plot Styles'))
    for directory in reversed(dirs):
        if (directory/CTB).is_file():return directory/CTB
    raise FileNotFoundError('Select an installed monochrome.ctb with --ctb')


def proof(native,output,ctb=None):
    """Render the saved CAD viewport at paper scale using its actual CTB."""
    import pymupdf
    from ezdxf.addons.drawing import Frontend,RenderContext,config,layout,pymupdf as pdf_backend
    from native_readback import digest
    native=Path(native);output=Path(output);ctb=Path(ctb) if ctb else stock_ctb()
    cap=json.loads((native/'capture.json').read_text())
    for key in ('plan.dwg','readback.dxf'):
        if digest(native/key)!=cap['files'][key]:raise ValueError('Native print inputs changed: '+key)
    doc=ezdxf.readfile(native/'readback.dxf');paper=doc.layouts.get(LAYOUT)
    from presentation import annotation_layers,resolve_roles
    viewport_layer=annotation_layers(resolve_roles(doc)).get('viewport','Defpoints')
    if paper.get_plot_style_filename().casefold()!=ctb.name.casefold():
        raise ValueError('CTB and stored layout plot style differ')
    # AutoCAD exports inactive paper viewports with id/status 0 on macOS.
    # The generated view is locked; the layout's system viewport is not.
    vps=[v for v in paper.query('VIEWPORT') if v.dxf.flags & 16384 and v.dxf.layer==viewport_layer]
    if len(vps)!=1:raise ValueError('Expected one printable floor plan viewport')
    vp=vps[0];scale=vp.dxf.view_height/vp.dxf.height
    if any(abs(a-b)>.01 for a,b in zip((paper.dxf_layout.dxf.paper_width,paper.dxf_layout.dxf.paper_height),SHEET)):
        raise ValueError('The reopened layout is not A3 landscape')
    backend=pdf_backend.PyMuPdfBackend()
    ctx=RenderContext(doc,ctb=str(ctb),export_mode=True)
    cfg=config.Configuration(background_policy=config.BackgroundPolicy.WHITE,lineweight_policy=config.LineweightPolicy.ABSOLUTE)
    # Validate the true saved paper annotations and viewport before rendering.
    paper_checks=audit_sheet(doc)
    if paper_checks['structural_issues']:raise ValueError('; '.join(paper_checks['structural_issues']))
    if abs(vp.dxf.view_twist_angle)>1e-8:
        raise ValueError('Only the generated orthogonal paper viewport is supported')
    if (vp.frozen_layers or tuple(vp.dxf.view_direction_vector)!=(0.,0.,1.) or
        max(abs(v) for v in vp.dxf.view_target_point)>1e-5):
        raise ValueError('Unsupported frozen layers, direction, or target in the saved viewport')
    left,bottom,right,top=VIEW_RECT
    if (abs(vp.dxf.width-(right-left))>.01 or abs(vp.dxf.height-(top-bottom))>.01 or
        abs(vp.dxf.center.x-(left+right)/2)>.01 or abs(vp.dxf.center.y-(bottom+top)/2)>.01):
        raise ValueError('The saved viewport is not the generated A3 plan region')
    center=vp.dxf.view_center_point;half_x=vp.dxf.width*scale/2;half_y=vp.dxf.view_height/2
    printable=[e for e in doc.modelspace() if doc.layers.get(e.dxf.layer).dxf.plot
        and not doc.layers.get(e.dxf.layer).is_off() and not doc.layers.get(e.dxf.layer).is_frozen()]
    extent=bbox.extents(printable)
    if (not extent.has_data or extent.extmin.x<center.x-half_x-.01 or extent.extmax.x>center.x+half_x+.01 or
        extent.extmin.y<center.y-half_y-.01 or extent.extmax.y>center.y+half_y+.01):
        raise ValueError('The reopened viewport clips the model; check its center and scale')
    # AutoCAD can export the plan viewport as id/status 0. Activate only the
    # in-memory object read from its hash-bound copy; never rewrite native input.
    activation=dict(original_id=vp.dxf.id,original_status=vp.dxf.status)
    for other in paper.query('VIEWPORT'):
        if other is not vp:other.dxf.id=1;other.dxf.status=1
    vp.dxf.id=2;vp.dxf.status=2
    frontend=Frontend(ctx,backend,config=cfg)
    # Render the real viewport first, then the real paper entities. This keeps
    # actual paper room labels above model strokes without adding PDF ornaments.
    frontend.draw_layout(paper,finalize=False,filter_func=lambda e:e.dxftype()=='VIEWPORT')
    frontend.draw_layout(paper,finalize=True,filter_func=lambda e:e.dxftype()!='VIEWPORT'
        and bool(doc.layers.get(e.dxf.layer).dxf.plot))
    # ezdxf 1.4.x truncates PDF media dimensions and its coordinate output space
    # to integer points. That shrinks a 400 mm A3 frame by about 0.185 mm.
    # Replay the same recorded CAD vectors into a full-precision page instead.
    # This changes the PDF backend's unit mapping, not CAD geometry or ornaments.
    page_spec=layout.Page(*SHEET)
    exact_settings=layout.Settings(fit_page=False,scale=1,
        output_coordinate_space=max(SHEET)*72/25.4)
    player=backend.player()
    transform=layout.Layout(BoundingBox2d([(0,0),SHEET]),flip_y=True).get_placement_matrix(
        page_spec,settings=exact_settings,top_origin=True)
    player.transform(transform)
    renderer=pdf_backend.PyMuPdfRenderBackend(page_spec,exact_settings)
    try:
        renderer.page_width_in_pt=SHEET[0]*72/25.4
        renderer.page_height_in_pt=SHEET[1]*72/25.4
        renderer.page.set_mediabox(pymupdf.Rect(0,0,renderer.page_width_in_pt,renderer.page_height_in_pt))
        renderer.content_shape=renderer.page.new_shape()
        player.replay(renderer)
        content=renderer.get_pdf_bytes()
    finally:
        renderer.doc.close()
    with pymupdf.open(stream=content,filetype='pdf') as result:
        page=result[0]
        strokes=page.get_drawings()
        if len(result)!=1 or not strokes or page.get_images():raise ValueError('Expected one nonempty vector proof')
        pixmap=page.get_pixmap(matrix=pymupdf.Matrix(.5,.5),colorspace=pymupdf.csGRAY,alpha=False)
        if min(pixmap.samples)>=245:
            raise ValueError('The exported page has no visible plan ink')
        paper_mm=[round(v*25.4/72,2) for v in (page.rect.width,page.rect.height)]
        if max(abs(v-w) for v,w in zip(paper_mm,SHEET))>.05:raise ValueError('Unexpected PDF page size')
        pens=sorted({round(x['width']*25.4/72,3) for x in strokes if x.get('width')})
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_bytes(content)
    for key in ('plan.dwg','readback.dxf'):
        if digest(native/key)!=cap['files'][key]:raise ValueError('Native evidence changed during proof: '+key)
    return dict(file=output.name,dwg_sha256=cap['files']['plan.dwg'],pdf_sha256=digest(output),
                engine='ezdxf / PyMuPDF from AutoCAD DWG readback',layout=LAYOUT,scale=scale,
                page_mm=paper_mm,vector_paths=len(strokes),stroke_widths_mm=pens,ctb=ctb.name,
                paper_audit=paper_checks,temporary_viewport_activation=activation,
                rendered_scope='actual readback paperspace and saved locked viewport',raster_images=0)


if __name__=='__main__':
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument('native',type=Path);cli.add_argument('output',type=Path);cli.add_argument('--ctb',type=Path)
    o=cli.parse_args();print(json.dumps(proof(o.native,o.output,o.ctb),ensure_ascii=False))

