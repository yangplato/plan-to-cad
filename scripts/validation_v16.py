"""Bind V1.6 source and geometry checks to the same final native capture."""
import fcntl
import hashlib
import json

import ezdxf
from freeze_source_inventory import verified_inventory
from native_readback import digest
from source_evidence import run as compare_source
from wall_geometry_v16 import audit as measure_walls
from check_room_material import check as room_material


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def if_present(path):
    return path if path.is_file() else None


def source_findings(report):
    """Two readers can disagree even when no defect in the actual drawing has been established."""
    failed, pending=set(),set()
    for key in report["failures"]:
        if key.startswith(("check/","nature/")):
            pending.add("双份原图清单/读图分歧/"+key)
        else:
            failed.add("双份原图清单/"+key)
    pending.update("双份原图清单/"+key for key in report["pending"])
    if report["status"]!="pass" and not (failed or pending):
        pending.add("双份原图清单/缺逐项实量")
    return failed,pending


def probe_verified(folder):
    report_file = folder/"environment/report.json"
    if not report_file.is_file():
        return False
    report = read(report_file)
    edit = report.get("dimension_edit", {})
    if (report.get("status") != "pass" or edit.get("status") != "通过"
            or edit.get("drawing_sha256") != report.get("dwg_sha256")):
        return False
    for key, filename in (("dwg_sha256", "plan.dwg"), ("dxf_sha256", "readback.dxf")):
        path = folder/"environment/native"/filename
        if not path.is_file() or digest(path) != report.get(key):
            return False
    return True


def final_checks(initial, candidate, ledger):
    native = candidate/"review/native"
    image = native/ledger["source_reference"]["filename"]
    source_hash = digest(image)
    inputs = candidate/"source-inputs"
    bands = ledger.get("source_read_bands_px", {})
    independent = verified_inventory(initial)
    panels = if_present(inputs/"panels.json")
    author = if_present(inputs/"author.json")
    doc = ezdxf.readfile(native/"readback.dxf")
    geometry = measure_walls(doc, ledger,
                             read(candidate/"edge-relations.json"), source_sha256=source_hash)
    guides = if_present(inputs/"guides.json")
    registration = candidate/"registration.json"
    ledger_file = candidate/"ledger.json"
    tone = if_present(candidate/"review/wall-colours/wall-tone-stations.json")
    inputs_digest = [digest(p) if p else None for p in (image, native/"plan.dwg", native/"readback.dxf",
                     registration, ledger_file, panels, author, independent, guides, tone)]
    key = hashlib.sha256(json.dumps(inputs_digest).encode()).hexdigest()
    evidence = candidate/"review/source-evidence-v16"/key
    evidence.mkdir(parents=True, exist_ok=True)
    with (evidence/"review.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        inventory = compare_source(image, panels, author, independent, candidate/"registration.json",
                                   native, evidence, ledger_file=ledger_file, wall_guides=guides,
                                   read_band_px=bands.get("along"), normal_band_px=bands.get("normal"),
                                   wall_tone_report=tone)
    failed, pending = source_findings(inventory)
    try:
        material = room_material(doc, ledger)
    except (ValueError,TypeError,KeyError,ArithmeticError,ezdxf.DXFError) as exc:
        material = {"status":"待核", "checks":[], "decode_issues":[str(exc)]}
    for item in material["checks"]:
        key = "墙材和室内净区/"+item["id"]+"/"+item["check"]
        if item["status"]=="失败":
            failed.add(key)
        elif item["status"]!="通过":
            pending.add(key)
    if material["decode_issues"] or (not material["checks"] and material["status"]!="通过"):
        pending.add("墙材和室内净区/实体解码或实量缺项")
    failed.update("墙窗与邻件/"+key for key in geometry["failures"])
    pending.update("墙窗与邻件/"+key for key in geometry["pending"])
    if geometry["status"]!="pass" and not (geometry["failures"] or geometry["pending"]):
        pending.add("墙窗与邻件/缺逐项实量")
    original_panels = initial/"source-inputs/panels.json"
    if independent and (not panels or not original_panels.is_file() or digest(panels) != digest(original_panels)):
        pending.add("双份原图清单/独审后图版范围已改变")
    environment_ok = probe_verified(candidate)
    if not environment_ok:
        pending.add("运行环境/缺本次原生保存、复开与改点证据")
    result = {"source_sha256": source_hash, "dwg_sha256": digest(native/"plan.dwg"),
              "dxf_sha256": digest(native/"readback.dxf"), "environment_passed": environment_ok,
              "source_inventory": inventory, "wall_geometry": geometry, "room_material": material,
              "source_evidence_directory": str(evidence.relative_to(candidate))}
    return failed, pending, result
