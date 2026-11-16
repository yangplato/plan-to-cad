"""Extract the objects that need the author's attention from a saved first-plan run."""
import argparse
import json
from pathlib import Path


def assemble(folder):
    review=folder/'review';issues=[]
    def read(path):return json.loads(path.read_text(encoding='utf-8'))
    def add(key,ident,data,path):
        issues.append(dict(topic=key,id=ident,measurement=data,evidence=str(path.relative_to(folder))))
    for file in sorted(review.glob('*/actual-frames.json')):
        for row in read(file)['portals']:
            if row['status']!='通过':
                add('洞口端点与实际图符：'+file.parent.name,row['id'],{'kind':row['kind'],
                    'found':row['status'],'failures':row.get('failures',[]),'pending':row.get('pending',[])},file)
    for file in sorted(review.glob('room-connections-*.json')):
        for row in read(file)['checks']:
            if row['status']!='通过':add('空间通行与闭合',row.get('id'),row,file)
    file=review/'native-object-coverage.json'
    if file.is_file():
        data=read(file)
        for row in data['items']:
            if row['issues']:add('家具、设备锚点及开关',row['id'],row['issues'],file)
        for item in data['issues']:add('全屋物件数量或归层','整户',item,file)
    file=review/'actual-wall-contacts.json'
    if file.is_file():
        for ident,row in read(file)['items'].items():
            if not row['before']['fit']:add('真实背边靠墙',ident,row['before'],file)
    file=review/'object-edge-relations.json'
    if file.is_file():
        edges=read(file)
        for row in edges['relations']:
            if row['status']!='pass':add('接边与留空',row['id'],row,file)
        for row in edges['coverage_issues']+edges['collisions']:add('接边覆盖或相交',row.get('object'),row,file)
    file=review/'whole-furniture-strokes.json'
    if file.is_file():
        for row in read(file):
            add('相邻实物的线和脚位',row['items'],{k:row[k] for k in ('actual_stroke_intersection_points','coinciding_stroke_length_mm','outer_hull_overlap_mm2')},file)
    file=review/'seat-motion.json'
    if file.is_file():
        for row in read(file)['chairs']:
            if row['status']!='通过':add('单椅使用',row['handle'],row,file)
    file=review/'seat-fronts.json'
    if file.is_file():
        data=read(file)
        for seat in data['seats']:
            for row in seat.get('candidates',[]):
                add('按实际前方净距核坐用', [seat['id'],row['id']],
                    {key:row.get(key) for key in ('min_gap_mm','max_gap_mm','continuous_width_mm')},file)
        for limitation in data.get('coverage_issues',[]):add('补核座位朝向或轮廓','',limitation,file)
    file=review/'furniture.json'
    if file.is_file():
        source=read(file)
        for key in ('wall_intersections','host_pairs_without_overlap','door_body_intersections'):
            for row in source[key]:add(key,'',row,file)
    return issues


def save(folder):
    rows=assemble(folder)
    path=folder/'首稿待查位置.json'
    path.write_text(json.dumps({'positions':rows,'note':'按位置查看实线与原图。修正确定错误；原图模糊或真实上下叠放的情况保留其图面和使用依据。该表不代替盲审。'},ensure_ascii=False,indent=2))
    return path


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('saved_plan_run',type=Path)
    a=parser.parse_args();print(save(a.saved_plan_run))

