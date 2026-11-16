"""Run repeatable geometric checks for a first submission produced with the common builder.

Usage: python first_readback.py NATIVE_CAPTURE CHECKS_MM_JSON OUTPUT_DIRECTORY [--layers MOVABLE FIXED]
Source semantics, full-room identification and furniture ergonomics still require reading the original.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import ezdxf

from check_room_material import check
from check_dimension_regeneration import check as dimensions
from native_readback import console_path,digest
import source_geometry_review


def write(path, report):
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")


def audit(native,ledger_path,output,layers,console):
    output.mkdir(parents=True,exist_ok=True)
    readback=native/"readback.dxf"
    index=json.loads((native/"capture.json").read_text())
    if any(digest(native/n)!=index['files'][n] for n in ('plan.dwg','readback.dxf')):
        raise ValueError("Capture DWG/readback hash mismatch")
    ledger=json.loads(ledger_path.read_text())
    doc=ezdxf.readfile(readback)
    material=check(doc,ledger);write(output/'墙材和室内净区.json',material)
    if ledger.get('grid_registration',{}).get('mode')=='unmarked' and 'dimension_presentation' not in ledger:
        native_count=len(doc.modelspace().query('DIMENSION'))
        dim={'status':'失败' if native_count else '不适用','tested':0,'native_count':native_count,
             'reason':'原图无印刷尺寸，模型空间的原生标注应为零条'}
    else:
        dim=dimensions(native,console)
    write(output/'尺寸实改.json',dim)
    actual={}
    for op in ledger['openings']:
        handles=op.get('symbol_handles',[])
        objects=[doc.entitydb.get(h) for h in handles]
        valid=bool(handles) and all(obj is not None and obj.is_alive for obj in objects)
        actual[op['id']]={'kind':op['kind'],'expected_symbols':len(handles),
                         'resolved':sum(obj is not None and obj.is_alive for obj in objects),
                         'status':'通过' if valid or (op['kind']=='open' and not handles) else '失败'}
    write(output/'洞口符号回读.json',actual)
    cmds={'家具块和层':'check_furniture_layers.py','完整尺寸候选':'measure_furniture.py'}
    statuses={}
    for label,script in cmds.items():
        result=subprocess.run([sys.executable,'-B',str(Path(__file__).parent/script),str(readback),'--layers',*layers],
                              capture_output=True,text=True)
        if result.stdout:
            (output/(label+'.json')).write_text(result.stdout,encoding='utf-8')
        if result.stderr:(output/(label+'.log')).write_text(result.stderr,encoding='utf-8')
        statuses[label]=result.returncode
    report = {'DWG_SHA256':digest(native/'plan.dwg'),'墙材':material['status'],'原生改点':dim['status'],
            '符号全部存在':all(v['status']=='通过' for v in actual.values()),'逐件尺寸和分层退出码':statuses,
            'scope':'本清单来自绘图者的读图。对象是否漏登、门向是否对、两端是否正确、家具是否可用及是否真正贴合仍需对照源图和实物边。部分本体不可自动量测时，逐件补独立手量记录。'}
    spec = source_geometry_review.declared_spec(ledger)
    if spec is not None:
        report['source_geometry_review'] = source_geometry_review.audit(
            doc, spec, dxf_sha256=index['files']['readback.dxf'],
            source_sha256=ledger.get('source_sha256'), dwg_sha256=index['files']['plan.dwg'])
    return report


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('native',type=Path)
    p.add_argument('ledger',type=Path);p.add_argument('output',type=Path)
    p.add_argument('--layers',nargs=2,default=['活动家具','固定家具及设备']);p.add_argument('--console',type=Path)
    args=p.parse_args()
    try:
        report=audit(args.native,args.ledger,args.output,args.layers,console_path(args.console))
        write(args.output/'首稿自检.json',report);print(json.dumps(report,ensure_ascii=False))
        ok=report['墙材']=='通过' and report['原生改点'] in ('通过','不适用') and report['符号全部存在'] and not any(report['逐件尺寸和分层退出码'].values())
        ok = ok and report.get('source_geometry_review', {}).get('status', '通过') == '通过'
        sys.exit(0 if ok else 1)
    except (OSError,KeyError,ValueError,RuntimeError,subprocess.TimeoutExpired,ezdxf.DXFError) as err:
        print(str(err),file=sys.stderr);sys.exit(2)
