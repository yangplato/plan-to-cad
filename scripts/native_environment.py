"""Probe the active AutoCAD runtime using an isolated, dimensioned millimetre drawing."""
import argparse
import json
import subprocess
from pathlib import Path

import ezdxf

from check_dimension_regeneration import check
from native_readback import capture, console_path, digest


def probe(destination, console=None):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    sample = destination / "probe.dxf"
    native = destination / "native"
    result = {"schema": 1, "status": "unavailable", "stage": "locate_console"}
    try:
        executable = console_path(Path(console) if console else None)
        result["console"] = str(executable)
        result["stage"] = "build_sample"
        doc = ezdxf.new("R2018", setup=True)
        doc.units = 4
        model = doc.modelspace()
        model.add_line((0, 0), (1000, 0))
        dimension = model.add_linear_dim(base=(0, 140), p1=(0, 0), p2=(1000, 0),
                                         override={"dimlfac": 1, "dimdec": 0})
        dimension.render()
        doc.saveas(sample)
        result["stage"] = "native_save_and_reopen"
        capture(sample, native, executable, None)
        result["dwg_sha256"] = digest(native / "plan.dwg")
        result["dxf_sha256"] = digest(native / "readback.dxf")
        result["stage"] = "native_dimension_edit"
        edit = check(native, executable)
        result["dimension_edit"] = edit
        rows = edit.get("evidence", [])
        if (edit.get("drawing_sha256") != result["dwg_sha256"] or edit["status"] != "通过"
                or len(rows) != 1 or abs(rows[0]["before_mm"] - 1000) > 0.01
                or abs(rows[0]["after_mm"] - 1100) > 0.01):
            raise ValueError("1000 mm sample did not regenerate as 1100 mm")
        result.update(status="pass", stage="complete")
    except (OSError, ValueError, RuntimeError, ezdxf.DXFError, subprocess.TimeoutExpired) as exc:
        result["error"] = str(exc)
    (destination / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("new_folder", type=Path)
    parser.add_argument("--console", type=Path)
    args = parser.parse_args()
    report = probe(args.new_folder, args.console)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["status"] == "pass" else 2)
