"""Bind an independent review, at most one repair, and paper proof to their actual saved DWGs."""
import argparse
import json
import math
from pathlib import Path
from native_readback import digest
from drafting_version import version


def read(file):
    return json.loads(Path(file).read_text(encoding="utf-8"))


def checked_run(folder):
    native = folder / "review/native"
    cap = read(native / "capture.json")
    drawing = digest(native / "plan.dwg")
    expected={"plan.dwg","readback.dxf","both_on.dxf","movable_off.dxf","fixed_off.dxf","both_off.dxf"}
    if not expected <= cap["files"].keys() or any(digest(native/k)!=cap["files"][k] for k in expected):
        raise ValueError("DWG or native readback changed after capture")
    paper = read(folder / "review/print-proof.json")
    if paper["dwg_sha256"] != drawing or digest(folder / "review" / paper["file"]) != paper["pdf_sha256"]:
        raise ValueError("Paper proof does not belong to this version")
    return drawing


def model_findings(folder, drawing):
    """Confirmed geometry failures block release. A reader can resolve localized ambiguities."""
    data=read(folder / "author-summary.json")
    geometry=data["geometry"]
    if data["dwg_sha256"]!=drawing or geometry["DWG_SHA256"]!=drawing:
        raise ValueError("Measurements belong to a different DWG")
    edges=read(folder / "review/object-edge-relations.json")
    hard=set()
    candidates=set()
    source_geometry=data.get('source_geometry_review')
    required=read(folder/'ledger.json').get('source_geometry_review_required') is True
    if source_geometry is None and required:
        candidates.add('源图实体几何/缺独立预期及实量')
    elif source_geometry is not None:
        hashes=source_geometry.get('hashes',{})
        if hashes.get('dwg_sha256')!=drawing or hashes.get('readback_dxf_sha256')!=digest(folder/'review/native/readback.dxf'):
            candidates.add('源图实体几何/证据版本不符')
        hard.update('源图实体几何/'+str(s) for s in source_geometry.get('failed',[]))
        candidates.update('源图实体几何/'+str(s) for s in source_geometry.get('pending',[]))
        if source_geometry.get('status') not in ('通过','需返修','待核'):
            candidates.add('源图实体几何/缺有效状态')
        elif source_geometry['status']!='通过' and not (source_geometry.get('failed') or source_geometry.get('pending')):
            candidates.add('源图实体几何/非通过且无逐项结果')
    paper=read(folder/'review/print-proof.json')
    if required and paper.get('paper_audit',{}).get('passed') is not True:
        candidates.add('纸图/图框图签房名实查未通过')
    grid=read(folder / "registration.json").get("grid_registration",{})
    if not grid.get("mode"):
        hard.add("缺少本案定标方式")
    if geometry["墙材"]!="通过":hard.add("墙材和室内净区")
    if geometry["原生改点"]!="通过" and not (geometry["原生改点"]=="不适用" and grid.get("mode")=="unmarked"):
        hard.add("原生标注改点")
    if not geometry["符号全部存在"]:hard.add("洞口图符缺失")
    if geometry["逐件尺寸和分层退出码"]["家具块和层"]:hard.add("家具块和图层")
    if geometry["逐件尺寸和分层退出码"]["完整尺寸候选"]:candidates.add("家具完整尺寸")
    for row in edges["relations"]:
        if row["status"]=="fail":hard.add(row["id"])
        elif row["status"]!="pass":candidates.add(row["id"])
    for row in edges["collisions"]:
        hard.add("侵入/"+row["type"]+"/"+"/".join(row["items"]))
    for row in edges["coverage_issues"]:
        candidates.add("家具边/"+row["object"])
    for key,value in data.get("registered_frame_checks",{}).items():
        if value!="通过":candidates.add("门窗端接/"+key)
    for i,value in enumerate(data.get("physical_room_connections",[]),1):
        if value!="通过":candidates.add("房间通行/"+str(i))
    for row in data.get("functional_clearances",[]):
        if not row["satisfied"]:candidates.add("使用净距/"+row["job"])
    for reader,counts in data.get("source_marks",{}).items():
        if counts.get("复查"):candidates.add("原图测线/"+reader)
    screen=read(folder/"review/furniture.json")
    for row in screen["door_body_intersections"]:
        candidates.add(f"门扇扫过/{row['opening']}/{row['leaf']}/{row['object']}")
    for row in screen["host_pairs_without_overlap"]:
        candidates.add("设备支承/"+"/".join(row.get("handles",[])))
    for handle in data["furniture"]["unmeasured"]:candidates.add("实际占地/"+handle)
    for ident,state in data.get("wall_contact_items",{}).items():
        if state!="in_contact":candidates.add("靠墙/"+ident)
    for row in data.get("chair_motion",[]):
        if row["status"]!="通过":candidates.add("座椅移动/"+row["handle"])
    seat_fronts=read(folder/"review/seat-fronts.json")
    for row in seat_fronts["seats"]:
        if row.get("candidates"):candidates.add("坐用前方/"+row["id"])
    if seat_fronts.get("coverage_issues"):candidates.add("座位朝向和完整轮廓")
    if data.get("wall_colour_stations") in ("fail","review"):candidates.add("墙芯深浅分界")
    inventory=data.get("source_object_coverage")
    if inventory and not (inventory["schedule_matches"] and inventory["native_matches"]):
        candidates.add("原图家具清点及关层")
    hard.update(data.get("design_logic_failed",[]))
    candidates.update(data.get("design_logic_review",[]))
    return hard,candidates


def merge_source_geometry_findings(pending, candidates, old, fresh, ledger, verification,
                                   drawing, readback_hash, protected_repairs=()):
    """Replace only explicitly rechecked IDs in the same saved-file identity.

    A smaller independent spec must not erase an author's known failure merely
    because both findings use the same display prefix. Explicit repair issues
    also retain the normal repair/recheck workflow.
    """
    prefix = '源图实体几何/'
    old = old if isinstance(old, dict) else {}
    author_spec = ledger.get('source_geometry_review')
    author_spec = author_spec if isinstance(author_spec, dict) else {}
    review_spec = verification.get('source_geometry_review', author_spec)
    review_spec = review_spec if isinstance(review_spec, dict) else {}

    def scope(spec):
        ids = spec.get('required_ids')
        if (not isinstance(ids, list) or not ids or
                not all(isinstance(s, str) and s for s in ids) or len(ids) != len(set(ids))):
            return None
        return set(ids)

    author_scope, reviewed_scope = scope(author_spec), scope(review_spec)
    old_results = old.get('results', {})
    old_ids = set(old_results) if isinstance(old_results, dict) else set()
    old_failed = {str(s) for s in old.get('failed', [])}
    old_pending = {str(s) for s in old.get('pending', [])}
    old_local_pending = {s for s in old_pending
                         if not s.startswith(('identity/', 'wall_decode/', 'coverage/'))
                         and s not in {'schema_or_input', 'input'}}
    # Restore author findings after ordinary measured exceptions. A source
    # check can only be resolved by its own fresh, bound result below.
    pending.update(prefix+s for s in old_failed)
    candidates.update(prefix+s for s in old_pending)
    scope_mismatch = (reviewed_scope is None or
                      (author_scope is not None and author_scope != reviewed_scope) or
                      bool((old_ids | old_failed | old_local_pending)-set(reviewed_scope or ())))
    if scope_mismatch:
        candidates.add(prefix+'作者与审稿检查范围不一致')

    hashes = fresh.get('hashes', {})
    expected = {'dwg_sha256': drawing, 'readback_dxf_sha256': readback_hash,
                'source_sha256': ledger.get('source_sha256')}
    spec_keys = {'dwg_sha256': 'dwg_sha256', 'readback_dxf_sha256': 'dxf_sha256',
                 'source_sha256': 'source_sha256'}
    same_identity = all(isinstance(value, str) and len(value) == 64 and
                        hashes.get(key) == value and review_spec.get(spec_keys[key]) == value
                        for key, value in expected.items())
    if not same_identity:
        candidates.add(prefix+'新审查证据版本不符')
    results = fresh.get('results', {})
    results = results if isinstance(results, dict) else {}
    failed = {str(s) for s in fresh.get('failed', [])}
    waiting = {str(s) for s in fresh.get('pending', [])}
    decoded = not any(s.startswith(('identity/', 'wall_decode/', 'coverage/')) or
                      s in {'schema_or_input', 'input'} for s in waiting)
    resolved = set()
    if same_identity and decoded:
        for ident in set(reviewed_scope or ()) & set(results):
            row = results[ident]
            if (isinstance(row, dict) and row.get('id') == ident and row.get('status') == '通过'
                    and ident not in failed and ident not in waiting):
                resolved.add(ident)
    # A new pending result is not proof that a previous confirmed defect was
    # fixed. Only a current explicit pass may remove that old hard finding.
    protected = set(protected_repairs)
    pending.difference_update(prefix+s for s in resolved if prefix+s not in protected)
    candidates.difference_update(prefix+s for s in resolved)
    complete = (same_identity and decoded and not scope_mismatch and reviewed_scope is not None
                and reviewed_scope <= set(results)
                and not fresh.get('coverage', {}).get('missing_ids'))
    if complete:
        # These are bookkeeping conditions, not case IDs or geometry failures.
        for label in ('缺独立预期及实量', '证据版本不符', '缺有效状态', '非通过且无逐项结果'):
            candidates.discard(prefix+label)
    pending.update(prefix+s for s in failed)
    candidates.update(prefix+s for s in waiting)


def review(initial, assessment, final=None, recheck=None):
    original = checked_run(initial)
    if assessment["dwg_sha256"] != original or not assessment.get("reviewer", "").strip():
        raise ValueError("The initial independent review must identify the reader and reviewed DWG")
    if not assessment.get("paper_observation","").strip():
        raise ValueError("Record what the reviewer actually saw at printed size")
    if (final is None) != (recheck is None):
        raise ValueError("Supply both the revised job folder and its recheck")
    issues = {}
    for item in assessment["issues"]:
        if item["id"] in issues or not all(item.get(k) for k in ("id","location","evidence","finding")):
            raise ValueError("Each issue needs a unique ID, location, observation and evidence")
        if item["kind"] not in ("repair","decision","inference"):
            raise ValueError("Unknown issue kind")
        issues[item["id"]] = item
    repairs = {key for key,item in issues.items() if item["kind"] == "repair"}
    pending = set(repairs)
    final_hash = original
    candidate = initial
    if final is not None:
        candidate = final
        if version(read(candidate/"ledger.json")) < version(read(initial/"ledger.json")):
            raise ValueError("修订稿不能采用较早的验收格式")
        final_hash = checked_run(final)
        original_image=read(initial/"registration.json").get("source_sha256")
        if not original_image or original_image!=read(final/"registration.json").get("source_sha256"):
            raise ValueError("The revised job must refer to the same source image as the initial review")
        if (not repairs or recheck.get("original_sha256") != original or
            recheck.get("dwg_sha256") != final_hash or final_hash == original):
            raise ValueError("The recheck does not address this initial review and changed DWG")
        if not recheck.get("reviewer","").strip() or not recheck.get("paper_observation","").strip():
            raise ValueError("Supply the recheck reader and the revised proof observation")
        addressed = set()
        for item in recheck["repairs"]:
            key = item["id"]
            if key not in repairs or key in addressed or item["result"] not in ("fixed","remains"):
                raise ValueError("Recheck each confirmed defect once by its original ID")
            if not item.get("measurement") or not item.get("affected_checks"):
                raise ValueError("Give the repair evidence and checks of affected parts")
            addressed.add(key)
            if item["result"] == "fixed":
                pending.remove(key)
        if recheck.get("new_defects"):
            pending.update(recheck["new_defects"])
    decisions = [k for k,x in issues.items() if x["kind"] == "decision"]
    declared_open_repairs = set(pending)
    hard,candidates=model_findings(candidate,final_hash)
    if (candidate/"review/图层界线和总尺寸.json").is_file() and not read(candidate/"review/图层界线和总尺寸.json")["passed"]:
        hard.add("编号图层、墙材关联或外围尺寸异常")
    pending.update(hard)
    verification=recheck if recheck is not None else assessment
    for item in verification.get("measured_exceptions",[]):
        if item["id"] not in candidates or not item.get("evidence","").strip():
            raise ValueError("A manual finding must identify an outstanding measurement and its evidence")
        candidates.remove(item["id"])
    # The whole-plan evidence gate runs after ordinary candidate resolutions.
    # A generic measured exception cannot waive a missing room or real endpoint.
    professional=None
    ledger = read(candidate/"ledger.json")
    if version(ledger) >= (0, 14) or verification.get("professional_review") is not None or read(candidate/"author-summary.json").get("professional_review_required"):
        from professional_review import assess_capture
        if final is not None:
            initial_ledger = read(initial/"ledger.json")
            ledger["rounding_chain_ids"] = sorted(set(initial_ledger.get("rounding_chain_ids", []))
                                                  | set(ledger.get("rounding_chain_ids", [])))
        professional=assess_capture(candidate/"review/native",ledger,verification,issues)
        candidates.update("整户专业审查/"+s for s in professional["missing"])
        pending.update(professional["linked_repairs"])
        source_geometry=professional.get('source_geometry_review')
        if source_geometry is not None:
            merge_source_geometry_findings(
                pending, candidates, read(candidate/'author-summary.json').get('source_geometry_review'),
                source_geometry, ledger, verification, final_hash,
                digest(candidate/'review/native/readback.dxf'), protected_repairs=declared_open_repairs)
    if ledger.get('source_geometry_review_required') is True:
        paper=read(candidate/'review/print-proof.json')
        if paper.get('paper_audit',{}).get('passed') is not True:
            candidates.add('纸图/图框图签房名实查未通过')
    status = ("未达到轻改标准" if pending else "待局部复核" if candidates else "待用户决定" if decisions else "可交设计师接手")
    return dict(status=status, initial_dwg_sha256=original, dwg_sha256=final_hash,
                open_repairs=sorted(pending), pending_checks=sorted(candidates), user_decisions=decisions,
                inferences=[k for k,x in issues.items() if x["kind"]=="inference"],
                repair_batches=int(final is not None), professional_review=professional)


def labour(timing):
    if not timing or timing.get("equivalent_scope_quality") is not True:
        return {"status":"待测"}
    base = timing.get("manual_active_min")
    edit = timing.get("handover_active_min")
    wait = timing.get("background_min")
    def valid(v):return type(v) in (int,float) and math.isfinite(v) and v>=0
    if not valid(base) or base<=0 or not valid(edit) or (wait is not None and not valid(wait)):
        raise ValueError("Enter measured active minutes and any separate background minutes")
    saved = (1-edit/base)*100
    return dict(status="已记录",manual_active_min=base,handover_active_min=edit,
                background_min=wait,saving_percent=round(saved,2),target_met=saved>=95)


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("initial",type=Path)
    parser.add_argument("assessment",type=Path)
    parser.add_argument("--revised",type=Path)
    parser.add_argument("--recheck",type=Path)
    parser.add_argument("--timing",type=Path)
    args=parser.parse_args()
    result=review(args.initial,read(args.assessment),args.revised,read(args.recheck) if args.recheck else None)
    result["labour"]=labour(read(args.timing) if args.timing and result["status"]=="可交设计师接手" else None)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    raise SystemExit(0 if result["status"]=="可交设计师接手" else 2)
