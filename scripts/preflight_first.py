"""Run the author's image, geometry and functional checks together before first submission.

The original pixel marks for both readers, independently preserved, are tested
against the reopened CAD. Open unresolved crops and repair the drawing before freeze.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import ezdxf
from native_readback import capture,console_path,digest
from drafting_version import version
from first_readback import audit as native_checks
from render_source_overlay import draw
from recheck_source_marks import audit as marks_audit, contact_sheet
from screen_furniture_pairs import screen
from survey_against_model import compare,show
from check_portal_rooms import audit as room_connections, write_svg as draw_rooms
from check_registered_frames import audit as frame_checks, write_svg as draw_frames
from repair_wall_contacts import inspect as contact_checks


def read(path):return json.loads(path.read_text())
def write(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2))
def main(a):
    src=a.drawing.resolve();out=a.out;out.mkdir(exist_ok=True,parents=True)
    folder=out/'native';reg=read(a.registration);ledger=read(a.ledger)
    layers=[reg['layers']['movable'],reg['layers']['fixed']];console=console_path(a.console)
    if folder.exists():
        cap=read(folder/'capture.json')
        if cap['source_sha256']!=digest(src) or cap['layers']!=layers or any(digest(folder/n)!=v for n,v in cap['files'].items()):
            raise ValueError('Use a fresh output folder for the edited drawing or different layer configuration')
    else:
        for attempt in range(2):
            try:
                ref = ledger.get('source_reference') if version(ledger)>=(1,5) else None
                capture(src,folder,console,layers,reference=ref);break
            except (RuntimeError,subprocess.TimeoutExpired):
                if attempt:raise
    proof=folder/'readback.dxf';doc=ezdxf.readfile(proof)
    summary={'dwg_sha256':digest(folder/'plan.dwg'),'geometry':native_checks(folder,a.ledger,out/'native-checks',layers,console)}
    overlay=out/'overlay.png';plain=out/'plain.png'
    draw(a.image,proof,reg,overlay,2,plain)
    lst={}
    for label,path in [('first',a.own),('independent',a.reader),('use',getattr(a,'use_reading',None))]:
        if path is None:continue
        source=read(path)
        if digest(a.image)!=source['source_sha256']:raise ValueError(label+': wrong original image')
        tag=out/label;tag.mkdir(exist_ok=True)
        result=marks_audit(doc,reg,source)
        write(tag/'marks.json',result)
        if source.get('portals'):
            graphics={}
            frames=frame_checks(doc,reg,source,getattr(a,'band_px',None),graphics)
            write(tag/'actual-frames.json',frames)
            draw_frames(tag/'actual-frames.svg',frames,graphics)
            summary.setdefault('registered_frame_checks',{})[label]=frames['status']
        contact_sheet(a.image,overlay,plain,source['regions'],tag/'views')
        lst[label]={k:sum(x['status']==k for x in result['marks']) for k in ('复查','核对象','吻合')}
    summary['source_marks']=lst
    for index,path in enumerate(getattr(a,'room_witness',[]) or [],1):
        graphics={}
        witness=read(path)
        if reg.get("grid_registration"):
            if witness.get("source_sha256",reg.get("source_sha256")) != reg.get("source_sha256"):
                raise ValueError("Room witnesses refer to another image")
            witness={**witness,"source_to_mm":reg["source_to_mm"],"grid_registration":reg["grid_registration"]}
        result=room_connections(doc,ledger,witness,drawing=graphics)
        write(out/f'room-connections-{index}.json',result)
        draw_rooms(out/f'room-connections-{index}.svg',graphics,result)
        summary.setdefault('physical_room_connections',[]).append(result['status'])
    if getattr(a,'contact_job',None):
        result=contact_checks(doc,ledger,read(a.contact_job))
        write(out/'actual-wall-contacts.json',result)
        summary['wall_contact_items']={key:row['status'] for key,row in result['items'].items()}
    
    if a.survey:
        survey=read(a.survey)
        if digest(a.image)!=survey['sha256']:raise ValueError('Different original image was surveyed')
        checks,issues=compare(doc,reg,survey,dict(x.split('=',1) for x in a.tone))
        loc=[z for z in checks if z['contrast_only']>=.45]
        tag=out/'survey';tag.mkdir(exist_ok=True);write(tag/'comparison.json',{'observations':checks,'decode':issues});show(a.image,loc,tag)
        summary['image_only_locators_to_read']=len(loc)
    bodies=screen(doc,ledger,reg['layers'],read(a.relations) if a.relations else [])
    write(out/'furniture.json',bodies)
    summary['furniture']={'total':len(bodies['coverage']),'wall_intersections':len(bodies['wall_intersections']),
        'furniture_intersections':len(bodies['furniture_intersections']),'door_body_intersections':len(bodies['door_body_intersections']),
        'unmeasured':[v['handle'] for v in bodies['coverage'] if not v['area_found']]}
    summary['next']='逐个看原图及实际图形，处理确定错项。碰撞允许的上下关系须有依据；没有由自动检查批准首稿。'
    write(out/'summary.json',summary)
    return summary


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('image','drawing','registration','ledger','own','out'):parser.add_argument(name,type=Path)
    for flag in ('reader','survey','relations','console','use-reading'):parser.add_argument('--'+flag,type=Path)
    parser.add_argument('--tone',action='append',default=[])
    parser.add_argument('--band-px',type=float)
    parser.add_argument('--room-witness',action='append',type=Path,default=[])
    parser.add_argument('--contact-job',type=Path)
    try:print(json.dumps(main(parser.parse_args()),ensure_ascii=False))
    except (OSError,ValueError,KeyError,RuntimeError,subprocess.TimeoutExpired,ezdxf.DXFError) as err:
        print(str(err),file=sys.stderr);sys.exit(2)
