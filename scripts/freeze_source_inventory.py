"""Seal an independent source inventory after the CAD-only observations are frozen."""
import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import ezdxf
from blind_observations import normalize
from check_cad_logic import inspect
from native_readback import digest


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def evidence_files(initial):
    return {"blind": initial/"review/cad-only-frozen.json", "author": initial/"source-inputs/author.json",
            "panels": initial/"source-inputs/panels.json", "dwg": initial/"review/native/plan.dwg",
            "readback": initial/"review/native/readback.dxf"}


def seal(initial, source_inventory):
    files = evidence_files(initial)
    for name, path in files.items():
        if name in ("author", "panels"):
            continue
        if not path.is_file():
            raise ValueError(f"缺少首稿 {name}：{path}")
    blind = read(files["blind"])
    if blind.get("dxf_sha256") != digest(files["readback"]):
        raise ValueError("请先封存本版首稿盲审")
    normalize(blind, inspect(ezdxf.readfile(files["readback"])))
    original = read(initial/"registration.json")["source_sha256"]
    payload_bytes = source_inventory.read_bytes()
    inventory = json.loads(payload_bytes)
    author = read(files["author"]) if files["author"].is_file() else {}
    if (inventory.get("source_sha256") != original or not inventory.get("session_id")
            or author and (author.get("source_sha256") != original or not author.get("session_id")
                           or inventory["session_id"] == author["session_id"])
            or inventory.get("source_only") is not True):
        raise ValueError("独立清点须标明相同原图、不同读图会话及只读原图的阶段")
    destination = initial/"review/independent-source"
    if destination.exists():
        raise FileExistsError(destination)
    with TemporaryDirectory(dir=destination.parent) as temp:
        stage = Path(temp)/"complete"
        stage.mkdir()
        payload = stage/"inventory.json"
        payload.write_bytes(payload_bytes)
        if files["panels"].is_file():
            from PIL import Image, ImageOps
            from source_evidence import manifest, inventory as validate_inventory
            ref = read(initial/"ledger.json").get("source_reference", {}).get("filename")
            if not ref:
                raise ValueError("首稿缺少图版原图参照")
            with Image.open(initial/"review/native"/ref) as image:
                panels, target, _ = manifest(files["panels"], original, ImageOps.exif_transpose(image).size)
            validate_inventory(payload, original, panels, target)
        receipt = {"source_sha256": original, "inventory": digest(payload),
                   **{key: digest(path) if path.is_file() else None for key, path in files.items()}}
        (stage/"receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
        stage.rename(destination)
    return receipt


def verified_inventory(initial):
    folder = initial/"review/independent-source"
    receipt = folder/"receipt.json"
    if not receipt.is_file():
        return None
    files = dict(evidence_files(initial), inventory=folder/"inventory.json")
    evidence = read(receipt)
    if (evidence.get("source_sha256") != read(initial/"registration.json")["source_sha256"]
            or any(k not in evidence or (digest(path) if path.is_file() else None) != evidence[k]
                   for k, path in files.items())):
        raise ValueError("独立原图清单或其冻结时的首稿证据已改变")
    return files["inventory"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("initial", type=Path)
    parser.add_argument("independent_inventory", type=Path)
    args = parser.parse_args()
    print(json.dumps(seal(args.initial, args.independent_inventory), indent=2))
