"""Match a separate source-image inventory to a furniture schedule and native CAD readbacks."""
import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import re
import sys

from dimension_grid import GridProjection
import ezdxf
from ezdxf.addons.drawing import Frontend, RenderContext
from ezdxf.addons.drawing.recorder import Recorder
from shapely.geometry import Point
from shapely.ops import polygonize, unary_union

from check_furniture_layers import audit_layer
from check_layer_views import STATES
from check_seating_groups import read_lines


ATTACHMENT_ROLE = {
    'freestanding': 'movable', 'built_in': 'fixed', 'services': 'fixed',
    'secured': 'fixed', 'architectural': 'ancillary',
}


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def finite(n):
    return type(n) in (int, float) and math.isfinite(n)


def source_rows(source, bindings):
    if not re.fullmatch('[0-9a-f]{64}', source.get('source_sha256', '')):
        raise ValueError('清单须带原图文件的 SHA-256')
    if not isinstance(source['items'], list) or not source['items']:
        raise ValueError('先逐房登记本次范围内的家具及设备')
    if not isinstance(bindings, dict):
        raise ValueError('绑定表应为 {原图件号: CAD对象编号}')
    output = {}
    source_ids = set()
    for row in source['items']:
        for key in ('id', 'room', 'use', 'role', 'attachment', 'evidence'):
            if not isinstance(row.get(key), str) or not row[key].strip():
                raise ValueError(f'原图行缺少 {key}: {row.get("id")}')
        ident = row['id']
        target = bindings.get(ident, ident)
        if not isinstance(target, str) or not target or target in output or ident in source_ids:
            raise ValueError('原图件号与 CAD 对象必须一对一：' + ident)
        source_ids.add(ident)
        point = row['anchor_px']
        if not isinstance(point, list) or len(point) != 2 or not all(finite(v) and v >= 0 for v in point):
            raise ValueError('原图内部锚点需要两个非负像素坐标：' + ident)
        if not isinstance(row.get('group', ''), str):
            raise ValueError('组合使用同一文字编号：' + ident)
        issues = []
        if row['attachment'] not in ATTACHMENT_ROLE:
            issues.append('安装关系未判定')
        elif ATTACHMENT_ROLE[row['attachment']] != row['role']:
            issues.append('安装关系与用途层不符')
        if row['role'] not in ('movable', 'fixed', 'ancillary'):
            issues.append('用途层未判定')
        output[target] = {**row, 'object_id': target, 'issues': issues}
    if set(bindings) - source_ids:
        raise ValueError('绑定表有未知原图件号：' + str(sorted(set(bindings) - source_ids)))
    return output


def schedule_members(schedule):
    members = {}
    def add(key, role, family):
        if key in members:
            raise ValueError('编排中有重复物件：' + key)
        members[key] = {'role': role, 'family': family}
    dining = []
    for row in schedule:
        ident, role = row['id'], row['role']
        if row['type'] == 'dining':
            add(ident, role, row['table_family'])
            count = sum(len(row['layout_mm'][field]) for field in ('upper_offsets_mm', 'lower_offsets_mm'))
            seats = [ident + f'/chair-{i+1}' for i in range(count)]
            for seat in seats:
                add(seat, role, row['chair_family'])
            dining.append([ident, *seats])
        else:
            add(ident, role, row.get('family') or row.get('library_block') or row.get('function'))
    return members, dining


def summarize(rows, issues):
    counts = Counter((r['room'], r.get('group', ''), r['use'], r['role']) for r in rows.values())
    grouping = [dict(room=k[0], group=k[1], use=k[2], role=k[3], count=v) for k,v in counts.items()]
    return dict(passed=not issues and not any(r['issues'] for r in rows.values()),
                source_count=len(rows), counts=grouping, items=list(rows.values()), issues=issues)


def precheck(source, schedule, bindings=None):
    rows = source_rows(source, bindings or {})
    planned, dining = schedule_members(schedule)
    for ident, row in rows.items():
        item = planned.get(ident)
        if item is None:
            row['issues'].append('编排漏件')
            continue
        row['scheduled'] = item
        if row['role'] != item['role']:
            row['issues'].append('编排用途层与原图不符')
        if row.get('family') and row['family'] != item['family']:
            row['issues'].append('编排图块用途与指定族不符')
    for members in dining:
        if len(members) < 2 or not all(k in rows for k in members):
            continue
        groups = {(rows[k]['room'], rows[k].get('group','')) for k in members}
        if len(groups) != 1 or not rows[members[0]].get('group'):
            rows[members[0]]['issues'].append('桌椅来自不同房间或缺少同一原图组合')
    extras = sorted(set(planned) - rows.keys())
    return summarize(rows, [{'unmatched_schedule_objects':extras}] if extras else [])


def check_anchor(ref, row, projection):
    point = Point(projection.world(row['anchor_px']))
    lines, notes = read_lines(ref)
    if not lines or notes:
        row['issues'].append({'无法可靠取实物线': notes})
        return
    lines = unary_union(list(lines.values()))
    faces = unary_union(list(polygonize(lines)))
    # A complete perimeter may have internal detail. For an open symbol a hull
    # localizes the object, but does not prove which space it occupies.
    closed = not faces.is_empty and faces.buffer(.25).covers(lines)
    area = faces if closed else lines.convex_hull
    distance = point.distance(area)
    row['anchor_check'] = dict(method='closed_perimeters' if closed else 'stroke_hull',
                               distance_mm=round(distance, 3))
    if distance > .25:
        row['issues'].append('原图内部锚点落在现图物件之外')


def live_insert(doc, handle):
    ref = doc.entitydb.get(handle)
    if ref is not None and ref.is_alive and ref.dxftype() == 'INSERT' and ref.mcount == 1:
        if ref.dxf.owner == doc.modelspace().block_record_handle:
            return ref


def readback(source, ledger, registration, native, bindings=None):
    rows = source_rows(source, bindings or {})
    issues = []
    if registration.get('source_sha256') != source['source_sha256']:
        issues.append('原图清单与图纸登记的原图 SHA-256 不匹配')
    projection = GridProjection(registration)
    matrix = projection.matrix
    if len(matrix) != 6 or not all(finite(v) for v in matrix) or abs(matrix[0]*matrix[3]-matrix[1]*matrix[2]) < 1e-10:
        raise ValueError('登记的原图坐标变换无效')
    layers = registration['layers']
    if len({layers[k].casefold() for k in ('movable','fixed','ancillary')}) != 3:
        raise ValueError('三种用途需要不同的图层')
    native = Path(native)
    capture = read(native/'capture.json')
    for name in ['plan.dwg','readback.dxf', *[s+'.dxf' for s in STATES]]:
        if capture['files'].get(name) != digest(native/name):
            issues.append('原生保存或状态文件与采集摘要不符：' + name)
    doc = ezdxf.readfile(native/'readback.dxf')
    if doc.units != 4:
        raise ValueError('原生回读需为毫米模型')
    used = set()
    for ident, row in rows.items():
        record = ledger.get(ident)
        if record is None:
            row['issues'].append('成图台账漏件')
            continue
        handle = record['handle'].upper()
        if handle in used:
            row['issues'].append('多件登记到了同一图元')
        used.add(handle)
        row['handle'] = handle
        ref = live_insert(doc, handle)
        if ref is None:
            row['issues'].append('复开模型缺少此独立块')
            continue
        row['actual_layer'] = ref.dxf.layer
        row['actual_block'] = ref.dxf.name
        if ref.dxf.layer.casefold() != layers.get(row['role'], '').casefold():
            row['issues'].append('实际图层与原图用途不符')
        if ref.dxf.name.casefold() != record['block'].casefold():
            row['issues'].append('实际图块与所绑定图块不符')
        check_anchor(ref, row, projection)
    extras = sorted(set(ledger) - rows.keys())
    if extras:
        issues.append({'unmatched_ledger_objects':extras})
    furniture_layers = {layers[k].casefold() for k in ('fixed','movable')}
    known_blocks = {rec['block'].casefold() for rec in ledger.values()}
    generated_prefixes = ('mpc_f1_', 'mpc_fix1_', 'mpc_hosted_', 'plan_body_', 'plan_lib_')
    for entity in doc.modelspace():
        if entity.dxf.handle.upper() in used:
            continue
        block = entity.dxf.name.casefold() if entity.dxftype() == 'INSERT' else ''
        known = block in known_blocks or block.startswith(generated_prefixes)
        if entity.dxf.layer.casefold() in furniture_layers or known:
            issues.append({'unmatched_furniture_entity':entity.dxf.handle,'layer':entity.dxf.layer})
    for layer in (layers['fixed'],layers['movable']):
        faults = audit_layer(doc, layer)['errors']
        if faults:
            issues.append({'layer':layer,'block_chain':faults})
    for state, flags in STATES.items():
        view = ezdxf.readfile(native/(state+'.dxf'))
        actual_flags = []
        for role in ('movable','fixed'):
            layer = view.layers.get(layers[role])
            actual_flags.append(layer.is_off() or layer.is_frozen())
        if view.units != 4 or tuple(actual_flags) != flags:
            issues.append('原生开关状态不符合登记：' + state)
        recorder = Recorder()
        frontend = Frontend(RenderContext(view), recorder)
        for row in rows.values():
            if 'handle' not in row:continue
            ref = live_insert(view, row['handle'])
            if ref is None:
                row['issues'].append('状态文件缺少模型空间块：' + state)
                continue
            before = len(recorder.records)
            frontend.draw_entities([ref])
            count = len(recorder.records) - before
            row.setdefault('draw_records', {})[state] = count
            hidden = (row['role'] == 'movable' and flags[0]) or (row['role'] == 'fixed' and flags[1])
            if (not count) != hidden:
                row['issues'].append('逐件可见性不符：' + state)
            elif not hidden and count != row['draw_records']['both_on']:
                row['issues'].append('单独打开用途层时，块内可见记录数发生变化：' + state)
    return summarize(rows, issues)


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cmds = cli.add_subparsers(dest='mode', required=True)
    pre = cmds.add_parser('precheck')
    post = cmds.add_parser('readback')
    for parser in (pre, post):
        parser.add_argument('inventory', type=Path)
        parser.add_argument('--bindings', type=Path)
        parser.add_argument('--image', type=Path, help='核对实际原图文件的 SHA-256')
    pre.add_argument('schedule',type=Path)
    for field in ('ledger','registration','native'):
        post.add_argument(field,type=Path)
    args = cli.parse_args()
    try:
        source = read(args.inventory)
        if args.image and source['source_sha256'] != digest(args.image):
            raise ValueError('输入图片与原图清单的 SHA-256 不匹配')
        bindings = read(args.bindings) if args.bindings else {}
        if args.mode == 'precheck':
            result = precheck(source, read(args.schedule), bindings)
        else:
            result = readback(source, read(args.ledger), read(args.registration), args.native, bindings)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result['passed'] else 1
    except (OSError, UnicodeError, KeyError, TypeError, ValueError, ezdxf.DXFError) as exc:
        print(json.dumps({'input_error':str(exc)},ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
