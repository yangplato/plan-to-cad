"""Read back the layer table, wall boundary links, and actual drawn dimension witnesses."""
from collections import Counter
import json
from pathlib import Path
import sys
import ezdxf
from presentation import APP,SIDES,resolve_roles,annotation_layers,modern_layers,layer_spaces


def audit(doc,ledger):
    issues=[];walls=0;counts=Counter();details=[]
    model=doc.modelspace()
    roles=resolve_roles(doc,ledger.get('layer_roles'));modern=modern_layers(roles)
    paper_layers=annotation_layers(roles)
    allowed=set(roles.values())|{'0','Defpoints','VIEWPORTS'}|set(paper_layers.values())
    reference = ledger.get('source_reference')
    if reference:
        allowed.add(reference['layer'])
        image = doc.entitydb.get(reference['handle'])
        if (image is None or image.dxftype() != 'IMAGE' or image.dxf.owner != model.layout_key
                or image.dxf.layer != reference['layer'] or image.image_def is None
                or image.image_def.dxf.filename != reference['filename']):
            issues.append("原图参照实体或相对路径已变")
        if reference['layer'] not in doc.layers or not doc.layers.get(reference['layer']).is_locked() or doc.layers.get(reference['layer']).dxf.plot:
            issues.append("参照层须锁定且不出图")
    extra=sorted(layer.dxf.name for layer in doc.layers if layer.dxf.name not in allowed)
    if extra:issues.append("额外图层："+", ".join(extra))
    permitted=allowed-{'0','Defpoints','VIEWPORTS'}-set(paper_layers.values())
    if modern:permitted.add(paper_layers['rooms'])
    for e in model:
        if reference and e.dxf.layer == reference['layer'] and e.dxf.handle != reference['handle']:
            issues.append(f"{e.dxf.handle}: 非登记原图参照图元")
        if e.dxf.layer not in permitted:issues.append(f"{e.dxf.handle}: 非业务层 {e.dxf.layer}")
        if e.dxftype()=='HATCH' and e.dxf.layer in (roles['bearing'],roles['partition']):
            walls+=1
            for path in e.paths:
                handles=path.source_boundary_objects
                if not handles or any((b:=doc.entitydb.get(h)) is None or b.dxftype()!='LWPOLYLINE'
                                      or not b.closed or b.dxf.layer!=e.dxf.layer
                                      or e.dxf.handle not in b.get_reactors() for h in handles):
                    issues.append(f"{e.dxf.handle}: 墙填充和关联的闭合轮廓需同层")
    for row in ledger['dimension_presentation']:
        e=doc.entitydb.get(row['handle'])
        if e is None or e.dxftype()!='DIMENSION' or not e.has_xdata(APP):
            issues.append(f"{row['handle']}: 未读回原生尺寸");continue
        if modern:
            layer=doc.layers.get(e.dxf.layer)
            if (e.dxf.layer!=roles['dimension'] or doc.layers.get(e.dxf.layer).dxf.color!=4
                    or e.dxf.color!=256 or e.dxf.hasattr('true_color')):
                issues.append(f"{row['handle']}: 尺寸须为ACI4图层及BYLAYER颜色")
            if any(e.override().get(key)!=256 for key in ('dimclrd','dimclre','dimclrt')):
                issues.append(f"{row['handle']}: 尺寸线、界线和字颜色须为BYLAYER")
            if layer.is_off() or layer.is_frozen() or not layer.dxf.plot or e.dxf.get('invisible',0):
                issues.append(f"{row['handle']}: 尺寸在模型空间不可见或不出图")
        side,kind,env=[t.value for t in e.get_xdata(APP)]
        if side!=row['side'] or kind!=row['kind'] or abs(e.get_measurement()-row['measurement_mm'])>.01:
            issues.append(f"{row['handle']}: 尺寸类别、方向或量值改变")
        axis,sign=SIDES[side];ni=1-axis
        base=e.dxf.defpoint[ni]
        block=e.get_geometry_block();gaps=[]
        for line in block.query('LINE') if block else ():
            a,b=line.dxf.start,line.dxf.end
            if abs(a[axis]-b[axis])<.01 and abs(a[ni]-b[ni])>1:
                gaps.append(min(sign*(a[ni]-env),sign*(b[ni]-env)))
        if len(gaps)<2 or min(gaps)<=.1:issues.append(f"{row['handle']}: 原生界线未留足可见空隙")
        counts[side,kind]+=1
        details.append(dict(handle=e.dxf.handle,side=side,kind=kind,baseline=sign*(base-env),
                            ink_gap_mm=min(gaps,default=None),actual_mm=e.get_measurement(),basis=row['basis']))
    for side in SIDES:
        if counts[side,'overall']!=1:issues.append(f"{side}: 应有一条外围总尺寸")
        lines=[v for v in details if v['side']==side]
        inner=[v['baseline'] for v in lines if v['kind']=='segment']
        if inner and any(v['baseline']<=max(inner) for v in lines if v['kind']=='overall'):
            issues.append(f"{side}: 总尺寸线应位于分段线外侧")
    paper_result=None
    if any(layer in doc.layers for layer in paper_layers.values()):
        from plan_sheet import audit_sheet
        paper_result=audit_sheet(doc)
        issues.extend(paper_result['issues'])
    return dict(passed=not issues,issues=issues,wall_hatches=walls,dimensions=details,
                paper=paper_result,layer_roles=roles,layer_spaces=layer_spaces(doc))

if __name__=='__main__':
    d=ezdxf.readfile(sys.argv[1]);record=json.loads(Path(sys.argv[2]).read_text())
    result=audit(d,record);print(json.dumps(result,ensure_ascii=False,indent=2));sys.exit(not result['passed'])
