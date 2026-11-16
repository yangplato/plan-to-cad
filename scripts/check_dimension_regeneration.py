"""Check regenerated text of simple millimetre dimensions in an isolated CAD copy."""
import argparse
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory

import ezdxf

from check_dimensions import numeric_text, read_entities
from native_readback import run_console, console_path, digest


def check(capture_folder, console):
    drawing = capture_folder / "plan.dwg"
    readback = capture_folder / "readback.dxf"
    manifest = json.loads((capture_folder / "capture.json").read_text(encoding="utf-8"))
    for path in (drawing, readback):
        if digest(path) != manifest["files"][path.name]:
            raise ValueError("The DWG or readback differs from the native capture")
    doc = ezdxf.readfile(readback)
    if doc.units != 4:
        raise ValueError("The captured drawing does not declare millimetres")
    originals, unsupported = read_entities(doc)
    commands = ['(setvar "FILEDIA" 0)', '(setvar "CMDECHO" 0)']
    for row in originals:
        handle = row["handle"]
        if not re.fullmatch("[0-9A-Fa-f]+", handle):
            raise ValueError("Invalid dimension handle")
        ax, ay = row["axis"]
        length = sum((b - a) * v for a, b, v in zip(row["p1_mm"], row["p2_mm"], (ax, ay)))
        change = math.copysign(100.0, length)
        commands.extend([
            f'(setq e (handent "{handle}") d (entget e) q (assoc 14 d))',
            f"(entmod (subst (cons 14 (mapcar '+ (cdr q) '({change*ax:.10f} {change*ay:.10f} 0))) q d))",
            '(entupd e)',
        ])
    if not originals:
        return {"status": "未验证", "reason": "无可测二维原生线性尺寸", "unsupported": unsupported}
    with TemporaryDirectory(prefix="cad-dimension-edit-") as location:
        job = Path(location)
        shutil.copyfile(drawing, job / "test.dwg")
        after = job / "edited.dxf"
        script = job / "edit.scr"
        commands.extend(['_.REGEN', '(command "_.SAVEAS" "_DXF" "" (strcat (getvar "DWGPREFIX") "edited.dxf"))', '_.QUIT', ''])
        script.write_text("\n".join(commands), encoding="ascii")
        run = run_console([str(console), "/i", str(job / "test.dwg"), "/s", str(script),
                              "/isolate", job.name, str(job / "profile")], cwd=job, timeout=120,
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if run.returncode or not after.exists():
            raise RuntimeError("Native edit or export failed: " + run.stdout[-900:].decode(errors="replace"))
        changed = {row["handle"]: row for row in read_entities(ezdxf.readfile(after))[0]}
    evidence = []
    for old in originals:
        new = changed.get(old["handle"])
        target = old["actual_mm"] + 100
        initial = [numeric_text(text) for text in old["block_text"]]
        after_text = new["block_text"] if new else []
        actual = new["actual_mm"] if new else None
        # Annotated/dual-unit text needs the appropriate drafting convention to interpret.
        if not initial or all(value is None for value in initial):
            status = "未验证"
        else:
            matches = [numeric_text(text) for text in after_text]
            follows = any(value is not None and abs(value - target) <= 0.501 for value in matches)
            status = "通过" if actual is not None and abs(actual-target) < 0.05 and follows else "不通过"
        evidence.append(dict(handle=old["handle"], before_mm=old["actual_mm"], expected_mm=target,
                             after_mm=actual, visible_text=after_text, status=status))
    statuses = {row["status"] for row in evidence}
    status = "不通过" if "不通过" in statuses else ("未验证" if unsupported or "未验证" in statuses else "通过")
    return dict(status=status, drawing_sha256=digest(drawing), tested=len(evidence),
                passed=sum(row["status"] == "通过" for row in evidence), evidence=evidence, unsupported=unsupported)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path, help="The output folder of native_readback.py")
    parser.add_argument("--console", type=Path)
    opts = parser.parse_args()
    try:
        report = check(opts.capture, console_path(opts.console))
    except (OSError, ValueError, KeyError, RuntimeError, ezdxf.DXFError, subprocess.TimeoutExpired) as exc:
        report = {"status": "未验证", "reason": str(exc)}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    sys.exit({"通过": 0, "不通过": 1, "未验证": 2}[report["status"]])
