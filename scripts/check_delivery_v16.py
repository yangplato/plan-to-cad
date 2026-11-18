"""Open the named deliverable DWG from its real directory and match the reviewed files."""
import argparse
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory

import ezdxf
from native_readback import capture, console_path, digest
from review_working_plan import checked_run, read


def verify(candidate, result, console=None):
    result = result.resolve(strict=True)
    stem = result.name
    drawing = result/(stem+".dwg")
    pdf = result/(stem+".pdf")
    expected = checked_run(candidate)
    paper = read(candidate/"review/print-proof.json")
    ledger = read(candidate/"ledger.json")
    report = {"status": "incomplete", "filename": drawing.name, "expected_dwg_sha256": expected,
              "issues": [], "other_dwg_files": sorted(p.name for p in result.glob("*.dwg") if p != drawing)}
    try:
        if digest(drawing) != expected:
            raise ValueError("交付 DWG 与已核图纸的文件指纹不符")
        report["dwg_sha256"] = expected
        if not (result/"校验说明.md").is_file():
            report["issues"].append("缺校验说明")
        if paper.get("pdf_sha256"):
            if not pdf.is_file() or digest(pdf) != paper["pdf_sha256"]:
                report["issues"].append("交付 PDF 与同版试印不符或缺失")
            else:
                report["pdf_sha256"] = paper["pdf_sha256"]
        elif paper.get("status") != "未要求输出PDF" or pdf.is_file():
            report["issues"].append("没有与本版绑定的 PDF 证据")
        reference = ledger.get("source_reference")
        if not reference:
            raise ValueError("缺少原图参照登记")
        with TemporaryDirectory(prefix="plan-final-reopen-") as temp:
            native = Path(temp)/"native"
            capture(drawing, native, console_path(console), None, reference=reference)
            if digest(native/"plan.dwg") != expected:
                raise ValueError("复开样本与交付 DWG 不符")
            doc = ezdxf.readfile(native/"readback.dxf")
            images = [e.image_def for e in doc.modelspace().query("IMAGE") if e.image_def and
                      e.image_def.dxf.filename.replace(chr(92), "/") == reference["filename"]]
            report.update(units=doc.units, readback_sha256=digest(native/"readback.dxf"),
                          reference=reference["filename"], reference_sha256=reference["source_sha256"],
                          reference_loaded=len(images)==1 and bool(images[0].dxf.get("loaded", 0)))
            if doc.units != 4 or not report["reference_loaded"]:
                report["issues"].append("原生回读缺毫米单位或参照加载")
        if digest(drawing) != expected:
            report["issues"].append("核对期间正式文件发生变化")
        if not report["issues"]:
            report["status"] = "pass"
    except (ValueError, OSError, RuntimeError, subprocess.TimeoutExpired, ezdxf.DXFError) as exc:
        report["issues"].append(str(exc))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("result", type=Path)
    parser.add_argument("--console", type=Path)
    args = parser.parse_args()
    report = verify(args.candidate, args.result, args.console)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["status"] == "pass" else 2)
