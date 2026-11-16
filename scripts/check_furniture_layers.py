#!/usr/bin/env python3
"""Read a DXF without changing it. Requires ezdxf. Exit 0=pass, 1=issues, 2=input error."""
import argparse
import json
import sys

import ezdxf
from ezdxf.enums import InsertUnits


def visible(entity):
    # Review drawable objects even if someone has temporarily turned off a layer.
    if entity.dxf.get('invisible', 0):
        return False
    return entity.dxftype() not in {'ATTRIB', 'ATTDEF'} or not entity.is_invisible


def audit_layer(doc, layer):
    errors = []
    checked = set()
    insert_count = 0
    instance_count = 0

    def error(reason, path, entity=None):
        item = {'reason': reason, 'path': list(path)}
        if entity is not None:
            item.update(type=entity.dxftype(), handle=entity.dxf.handle, layer=entity.dxf.layer)
        errors.append(item)

    def check_child(entity, path):
        if entity.dxf.layer != '0' and (entity.dxftype() == 'INSERT' or visible(entity)):
            error('块内对象位于非 0 层', path, entity)

    def visit(insert, path, active):
        name = insert.dxf.name
        key = name.casefold()
        here = (*path, name)
        if key in active:
            error('块引用形成循环', here, insert)
            return
        if key in checked:
            return
        block = doc.blocks.get(name)
        if block is None:
            error('缺少块定义', here, insert)
            return
        if block.block.is_xref or block.block.is_xref_overlay:
            error('外部参照的内容未解析', here, insert)
            return
        active.add(key)
        for child in block:
            check_child(child, here)
            if child.dxftype() == 'INSERT':
                for attr in child.attribs:
                    check_child(attr, here)
                visit(child, here, active)
        active.remove(key)
        checked.add(key)

    exists = layer in doc.layers
    if not exists:
        error('图层不存在', [layer])
    for entity in doc.modelspace():
        if entity.dxf.layer.casefold() != layer.casefold():
            continue
        if entity.dxftype() != 'INSERT':
            error('模型空间家具层含有非 INSERT 对象', [layer], entity)
            continue
        insert_count += 1
        instance_count += entity.mcount  # Unique positions in an MINSERT grid.
        for attr in entity.attribs:
            check_child(attr, [layer, entity.dxf.name])
        visit(entity, [layer], set())
    return {'layer': layer, 'exists': exists, 'insert_entities': insert_count,
            'instances': instance_count, 'errors': errors}


def main():
    parser = argparse.ArgumentParser(description='只读检查模型空间家具插入层和嵌套块。')
    parser.add_argument('dxf', help='待检查的 DXF 路径')
    parser.add_argument('--layers', nargs=2, metavar=('MOVABLE', 'FIXED'),
                        default=['活动家具', '固定家具及设备'], help='两个独立的家具图层名称')
    args = parser.parse_args()
    if any(not name.strip() for name in args.layers) or args.layers[0].casefold() == args.layers[1].casefold():
        parser.error('请提供两个非空、不同的图层名。')
    try:
        doc = ezdxf.readfile(args.dxf)
        code = doc.header.get('$INSUNITS')
        try:
            unit = InsertUnits(code).name
        except ValueError:
            unit = '未知' if code is not None else '未声明'
        groups = [audit_layer(doc, layer) for layer in args.layers]
    except (OSError, ezdxf.DXFError, UnicodeError, RecursionError) as exc:
        print(json.dumps({'error': f'DXF 读取或检查失败：{exc}'}, ensure_ascii=False), file=sys.stderr)
        return 2
    passed = all(not group['errors'] for group in groups)
    result = {'passed': passed, 'model_units': {'$INSUNITS': code, 'name': unit}, 'groups': groups,
              'scope': '仅检查模型空间中已归入指定两层的对象及其 INSERT 块链。实例数按顶层插入计，阵列按位置计。家具是否漏画或错归其他层，须对照原图；关层效果须在原生 CAD 中实测。'}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if passed else 1


if __name__ == '__main__':
    sys.exit(main())
