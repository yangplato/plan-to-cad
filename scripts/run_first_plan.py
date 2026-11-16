"""Build an editable full plan from two source-authored tables and perform first-draft checks.

All paths in the job are relative to the job file unless absolute. This is an
authoring workflow: its reviewed DWG remains a work product until the author submits it.
"""
import argparse
import json
from itertools import combinations
from pathlib import Path
from types import SimpleNamespace

import ezdxf

from drafting_version import RELEASE, version
from build_plan_objects import compile_plan
from shutil import copyfile
from preflight_first import main as preflight
from seat_motion import run as chair_motion
from check_seating_groups import check as detailed_seats, read_lines
from shapely.ops import unary_union
from fit_opening_stations import run as source_faces
from check_wall_tone_stations import run as saved_wall_tones
from check_source_objects import precheck as compare_scheduled_objects, readback as compare_native_objects
from native_readback import digest
from first_draft_actions import save as save_review_positions


def write(path,report):
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')


def furniture_overlaps(doc,objects,relations):
    exempt={frozenset(row['handles']) for row in relations}
    lines={}
    for ident,record in objects.items():
        obj=doc.entitydb.get(record['handle'])
        if obj is None:continue
        members,notes=read_lines(obj)
        if not members:continue
        actual=unary_union(list(members.values()))
        lines[ident]=(obj.dxf.handle,actual,actual.convex_hull,notes)
    output=[]
    for left,right in combinations(lines,2):
        a,la,pa,na=lines[left];b,lb,pb,nb=lines[right]
        if frozenset([a,b]) in exempt or not pa.intersects(pb):continue
        area=pa.intersection(pb).area
        if area<=.01:continue
        cut=la.intersection(lb)
        def count_points(g):
            if g.is_empty:return 0
            if g.geom_type=='Point':return 1
            return sum(count_points(x) for x in getattr(g,'geoms',[]))
        output.append({'items':[left,right],'handles':[a,b],'outer_hull_overlap_mm2':round(area,2),
            'actual_stroke_intersection_points':count_points(cut),'coinciding_stroke_length_mm':round(cut.length,2),
            'unreadable_parts':[na,nb],
            'action':'检查是否同一高度并识别座面、脚位和几沿，原图同一物件不要重复计数。仅外包相交时另核形体。'})
    return output


def bind_group_ids(groups,objects):
    def handle(value):
        return objects[value]['handle'] if value in objects else value
    for g in groups:
        for m in g['members']:m['handle']=handle(m.get('id',m.get('handle')))
        for link in g.get('under_table',[]):
            link['seat']=handle(link['seat']);link['table']=handle(link['table'])
    return groups


def run(jobfile,folder):
    if folder.exists() and any(folder.iterdir()):
        raise ValueError('Use a new output directory; preserve the original observations and earlier model')
    job=json.loads(jobfile.read_text(encoding='utf-8'))
    def path(key,required=True):
        value=job.get(key)
        if not value and not required:return None
        if not value:raise ValueError('Missing job path: '+key)
        return (jobfile.parent/value).resolve()
    def read(p):return json.loads(p.read_text(encoding='utf-8'))
    scene=read(path('scene'));schedule=read(path('objects'))
    scene.setdefault('drafting_version',job.get('drafting_version', RELEASE))
    for original in schedule:
        options=original.get("fixture_options",{})
        if isinstance(options.get("library"),str):
            options["library"]=str((jobfile.parent/options["library"]).resolve())
    actual_source=digest(path('image'))
    if scene.get('source_sha256',actual_source)!=actual_source:
        raise ValueError('The job image differs from the registered source')
    scene.setdefault('source_sha256',actual_source)
    source_scene,source_schedule=scene,schedule
    if job.get('apply_printed_tones'):
        from apply_wall_tones import run as update_printed_tones
        folder.mkdir(exist_ok=True,parents=True)
        before=folder/'original-source-registration.json'
        from dimension_grid import prepare_plan
        scene,schedule=prepare_plan(scene,schedule)
        prepared=folder/'prepared-scene.json'
        write(prepared,scene)
        write(before,{k:scene[k] for k in ('source_to_mm','source_sha256','holdouts','holdout_limit_mm','grid_registration') if k in scene})
        update_printed_tones(path('image'),path('wall_guides'),before,prepared,folder/'source-wall-colours',
            job['uncertainty_px'],job.get('normal_uncertainty_px',job['uncertainty_px']))
        scene=read(folder/'source-wall-colours/scene-working.json')
    doc,products=compile_plan(scene,schedule,path('library_root',False))
    # New authoring runs always declare the independent source-geometry gate.
    # A missing source inventory produces a candidate, never an accepted plan.
    products['ledger']['source_geometry_review_required']=True
    folder.mkdir(exist_ok=True,parents=True)
    image_path = path('image')
    if version(source_scene) >= (1, 0):
        from attach_source_image import attach
        filename = 'source-plan' + image_path.suffix.lower()
        reference = attach(doc, image_path, products['registration'],
                           clip_px=job.get('reference_clip_px'), reference_name=filename)
        products['ledger']['source_reference'] = reference
        copyfile(image_path, folder / filename)
    source=folder/'source.dxf';doc.saveas(source)
    for name,report in products.items():write(folder/(name+'.json'),report)
    inventory_file=path('furniture_source',False)
    if inventory_file:
        inventory=read(inventory_file)
        if inventory['source_sha256']!=digest(path('image')):
            raise ValueError('Furniture observations refer to another source image')
        mapping=path('object_bindings',False)
        bindings=read(mapping) if mapping else {}
        initial_inventory=compare_scheduled_objects(inventory,source_schedule,bindings)
        write(folder/'source-object-coverage.json',initial_inventory)
    # Derive support/seat checks directly from the object ledger, in the exact file we saved.
    # A separate contact job can supplement the automatically registered wall-supported pieces.
    contacts=products['contacts']
    extra=path('extra_contacts',False)
    if extra:
        additions=read(extra)
        by_id={row['id']:row for row in contacts['items']}
        for item in additions['items']:
            ident=item['id']
            if ident not in products['objects']:
                raise ValueError('Contact item missing from the reconstructed object schedule: '+ident)
            by_id[ident]={**by_id.get(ident,{}),**item,'handle':products['objects'][ident]['handle']}
        contacts={**additions,'items':list(by_id.values())}
        write(folder/'contacts.json',contacts)
    witnesses=[(jobfile.parent/p).resolve() for p in job.get('room_witnesses',[])]
    options=SimpleNamespace(drawing=source,image=path('image'),out=folder/'review',registration=folder/'registration.json',
        ledger=folder/'ledger.json',own=path('own_reading'),reader=path('peer_reading',False),survey=path('source_survey',False),
        relations=folder/'relations.json',console=path('console',False),tone=job.get('tone',[]),band_px=job.get('uncertainty_px'),
        room_witness=witnesses,use_reading=path('use_reading',False),
        contact_job=folder/'contacts.json' if contacts['items'] else None)
    measured=preflight(options)
    if version(source_scene) >= (1, 0):
        copyfile(image_path, folder/'review'/'native'/filename)
    from design_dimensions import enabled, audit as design_audit
    if enabled(source_scene):
        report=design_audit(ezdxf.readfile(folder/'review/native/readback.dxf'),products['ledger'],
                            source_schedule,products['objects'],source_scene.get('dimension_logic_probes',[]))
        write(folder/'review/使用逻辑尺寸.json',report)
        measured['design_logic_failed']=[r['id']+' / '+r['kind'] for r in report['failed']]
        measured['design_logic_review']=[r['id']+' / '+r['kind'] for r in report['pending']]
        if report['room_pier_probes']==0: measured['design_logic_review'].append('逐间净距与实际门垛尚未独立复量')
        if report['wall_decode_issues'] or report['uncertain_contours']: measured['design_logic_review'].append('墙材解析需人工复量')
    if products['ledger'].get('dimension_presentation'):
        from audit_presentation import audit as audit_format
        report=audit_format(ezdxf.readfile(folder/'review/native/readback.dxf'),products['ledger'])
        write(folder/'review/图层界线和总尺寸.json',report)
        measured['drawing_format_ok']=report['passed']
    if inventory_file:
        native_inventory=compare_native_objects(inventory,products['objects'],products['registration'],
            folder/'review/native',bindings)
        write(folder/'review/native-object-coverage.json',native_inventory)
        measured['source_object_coverage']={'schedule_matches':initial_inventory['passed'],
            'native_matches':native_inventory['passed'],'source_items':len(inventory['items'])}
    if job.get('functional_clearances'):
        from check_use_clearance import audit as measure_use_clearance
        inspection=ezdxf.readfile(folder/'review/native/readback.dxf')
        checks=[]
        for i,name in enumerate(job['functional_clearances'],1):
            task=read((jobfile.parent/name).resolve())
            for item in task['objects']:
                if 'handle' not in item:
                    item['handle']=products['objects'][item.get('object_id',item['id'])]['handle']
            result=measure_use_clearance(inspection,task)
            write(folder/f'review/use-clearance-{i}.json',result)
            checks.append({'job':name,'satisfied':result['ok'],'coverage':result['coverage_issues']})
        measured['functional_clearances']=checks
    actual=ezdxf.readfile(folder/'review/native/readback.dxf')
    from source_geometry_review import audit as source_geometry_audit, VERSION as SOURCE_REVIEW_VERSION
    geometry_file=path('source_geometry_review',False)
    if geometry_file:
        geometry_spec=read(geometry_file)
        geometry_spec.setdefault('dxf_sha256',digest(folder/'review/native/readback.dxf'))
        geometry_spec.setdefault('dwg_sha256',digest(folder/'review/native/plan.dwg'))
        geometry_spec.setdefault('source_sha256',actual_source)
        measured['source_geometry_input_sha256']=digest(geometry_file)
    else:
        geometry_spec={'review_version':SOURCE_REVIEW_VERSION,'source_sha256':actual_source,
                       'dxf_sha256':digest(folder/'review/native/readback.dxf'),
                       'dwg_sha256':digest(folder/'review/native/plan.dwg')}
    source_geometry=source_geometry_audit(actual,geometry_spec,
        dxf_sha256=digest(folder/'review/native/readback.dxf'),source_sha256=actual_source,
        dwg_sha256=digest(folder/'review/native/plan.dwg'))
    write(folder/'review/源图实体几何核验.json',source_geometry)
    write(folder/'review/源图实体预期.json',geometry_spec)
    measured['source_geometry_review']=source_geometry
    products['ledger']['source_geometry_review']=geometry_spec
    write(folder/'ledger.json',products['ledger'])
    if version(source_scene) >= (0, 14):
        from professional_review import screen
        measured["professional_review_required"]=True
        report=screen(actual, products["ledger"])
        write(folder/"review/整户专业审查候选.json",report)
    from object_edge_relations import audit_edges
    edges=audit_edges(actual,products["ledger"],products["edge-relations"],products["relations"])
    write(folder/"review/object-edge-relations.json",edges)
    measured["edge_relations"]={k:edges[k] for k in ("ok","failed_count","unresolved_count","coverage_issues")}
    from plan_sheet import proof as print_sheet
    paper=print_sheet(folder/"review/native",folder/"review/首稿试印.pdf",path("ctb",False))
    write(folder/"review/print-proof.json",paper)
    measured["print_proof"]=paper
    measured['paper_settings']=products['sheet']
    from screen_front_groups import screen as measure_seat_fronts
    seat_fronts=measure_seat_fronts(actual,{**source_scene,**products["registration"]},source_schedule,products['objects'],
        inventory if inventory_file else None,bindings if inventory_file else None)
    write(folder/'review/seat-fronts.json',seat_fronts)
    measured['seat_fronts']={'seats':len(seat_fronts['seats']),'coverage':seat_fronts['coverage_issues']}
    chairs=chair_motion(actual,products['ledger'],products['registration']['layers'],products['dining'])
    write(folder/'review/seat-motion.json',chairs)
    measured['chair_motion']=[{'handle':v['handle'],'status':v['status']} for v in chairs['chairs']]
    physical=furniture_overlaps(actual,products['objects'],products['relations'])
    write(folder/'review/whole-furniture-strokes.json',physical)
    measured['unexplained_projected_groups']=[q['items'] for q in physical]
    if path('seating_groups',False):
        groups=bind_group_ids(read(path('seating_groups')),products['objects'])
        write(folder/'review/seating-groups.json',detailed_seats(actual,groups))
    if path('wall_guides',False):
        guid=path('wall_guides')
        measured['wall_colour_stations']=saved_wall_tones(path('image'),guid,folder/'review/native/readback.dxf',
            folder/'registration.json',folder/'review/wall-colours',job['uncertainty_px'],job.get('normal_uncertainty_px',job['uncertainty_px']))
        source_faces(path('image'),path('own_reading'),guid,folder/'review/opening-source-stations',path('guide_report',False),registration_path=folder/'registration.json')
    measured['object_ids']=list(products['objects'])
    measured['review_positions']=save_review_positions(folder).name
    write(folder/'author-summary.json',measured)
    return measured


if __name__=='__main__':
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument('job',type=Path);cli.add_argument('new_folder',type=Path)
    args=cli.parse_args();report=run(args.job.resolve(),args.new_folder.resolve());print(report['dwg_sha256'])
