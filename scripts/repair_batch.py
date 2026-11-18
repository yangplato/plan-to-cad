"""Bind one submitted correction to the first native DWG and its complete repair list."""
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from drafting_version import version
from freeze_source_inventory import verified_inventory
from native_readback import digest


def data(path):
    return json.loads(path.read_text(encoding="utf-8"))


def assessment_hash(assessment):
    import hashlib
    return hashlib.sha256(json.dumps(assessment, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def unchanged_reference_layer(first, candidate, blind_id, proof):
    """Check an explicitly disputed layer observation against the actual saved native files."""
    import ezdxf
    if not isinstance(proof,dict) or proof.get("check")!="source_reference_layer_defaults":
        return False
    native=first/"review/native"
    ref=data(first/"ledger.json").get("source_reference") or {}
    blind=data(first/"review/cad-only-frozen.json")
    name=ref.get("layer")
    row=next((c for c in blind.get("checks",[]) if c.get("id")==blind_id and c.get("kind")=="confirmed"),{})
    if (not name or not row or name not in row.get("location","") or proof.get("layer")!=name
            or proof.get("initial_dxf_sha256")!=digest(native/"readback.dxf")
            or blind.get("dxf_sha256")!=proof["initial_dxf_sha256"]):
        return False
    if row.get("subject")!="source_reference_layer_defaults":
        # Older frozen forms have no typed subject. Accept only an observation
        # which already recorded all three expected properties of this exact image.
        text=row.get("evidence","")
        if not all(k in text for k in (ref["handle"],"IMAGE","关闭","锁定","不打印")):
            return False
    for folder in dict.fromkeys((first,candidate)):
        native=folder/"review/native"
        files=data(native/"capture.json").get("files",{})
        if any(files.get(k)!=digest(native/k) for k in ("plan.dwg","readback.dxf")):
            return False
        local=data(folder/"ledger.json").get("source_reference") or {}
        doc=ezdxf.readfile(native/"readback.dxf")
        if (local.get("layer")!=name or name not in doc.layers or local.get("source_sha256")!=ref["source_sha256"]
                or not (native/local["filename"]).is_file() or digest(native/local["filename"])!=ref["source_sha256"]):
            return False
        layer=doc.layers.get(name)
        entity=doc.entitydb.get(local.get("handle",""))
        if (entity is None or entity.dxftype()!="IMAGE" or entity.dxf.layer!=name or
                entity.image_def is None or entity.image_def.dxf.filename.replace(chr(92),"/")!=local["filename"]
                or not entity.image_def.dxf.get("loaded",0) or not layer.is_off() or not layer.is_locked()
                or layer.dxf.get("plot",1)!=0):
            return False
    return True


def overturned(first, assessment):
    return {item["id"] for item in assessment.get("issues",[]) if item.get("kind")=="inference"
            and item.get("native_reclassification") and unchanged_reference_layer(
                first,first,item["id"],item["native_reclassification"])}


def provenance(first, assessment, source_hash):
    if first is None and assessment is None:
        return {"stage": "initial"}
    if first is None or assessment is None:
        raise ValueError("修订作业同时填写 revision_of 和 repair_list")
    ledger = data(first/"ledger.json")
    if (version(ledger) < (1,6) or ledger.get("repair_batch", {}).get("stage") != "initial"
            or ledger.get("source_sha256") != source_hash):
        raise ValueError("集中修正只能引用本案 V1.6 冻结首稿")
    if verified_inventory(first) is None:
        raise ValueError("启动集中修正前须先封存首稿盲审和独立原图清单")
    first_dwg = digest(first/"review/native/plan.dwg")
    issues = assessment.get("issues", [])
    ids = [i.get("id") for i in issues if i.get("kind") == "repair"]
    if (assessment.get("dwg_sha256") != first_dwg or not assessment.get("reviewer")
            or not ids or len(set(ids)) != len(ids) or any(not i for i in ids)):
        raise ValueError("一次集中返修需要针对冻结首稿的独审问题清单")
    blind = data(first/"review/cad-only-frozen.json")
    if set(blind.get("open_defects", [])) - set(ids) - overturned(first,assessment):
        raise ValueError("确认错误须按原编号纳入一次集中修正")
    return {"stage": "revised", "initial_dwg_sha256": first_dwg,
            "assessment_sha256": assessment_hash(assessment), "repairs": sorted(ids)}


def seal_submission(first, candidate_hash, original_hash, assessment):
    target = first/"review/final-submission.json"
    record = {"initial": original_hash, "submitted": candidate_hash, "assessment": assessment_hash(assessment)}
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent, delete=False) as stream:
        temp = Path(stream.name)
        json.dump(record, stream, sort_keys=True)
    try:
        try:
            os.link(temp, target)
        except FileExistsError:
            if data(target) != record:
                raise ValueError("本轮已登记其他终稿；一次集中返修已用完")
    finally:
        temp.unlink()
