"""macOS AutoCAD Core Console: save/reopen a disposable copy and export native layer states.

Usage: python native_readback.py SOURCE.dxf NEW_WORK_FOLDER [--layers MOVABLE FIXED]
Requires a locally licensed AutoCAD with AcCoreConsole, plus ezdxf for layer handles.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory, gettempdir

import ezdxf


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_console(args, **options):
    # All background drawing jobs under the same macOS user share a single console slot.
    # Never interrupt a foreground AutoCAD session. The kernel unlocks this file on exit.
    import fcntl  # Required only for the macOS native CAD runner.
    lock = Path(gettempdir()) / f"plan-to-cad-console-{os.getuid()}.lock"
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(lock, flags, 0o600), "rb+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        return subprocess.run(args, **options)


def console_path(override):
    options = [override] if override else sorted(Path("/Applications/Autodesk").glob(
        "AutoCAD */AutoCAD *.app/Contents/Helpers/AcCoreConsole.app/Contents/MacOS/AcCoreConsole"))
    if len(options) != 1 or not options[0].is_file() or not os.access(options[0], os.X_OK):
        raise ValueError("Select the installed AutoCAD Core Console executable with --console")
    return options[0].resolve()


def capture(source, destination, console, layers, *, reference=None):
    if destination.exists() or not destination.parent.is_dir():
        raise ValueError("Choose a new work folder inside an existing directory")
    suffix = source.suffix.lower()
    if not source.is_file() or suffix not in (".dwg", ".dxf"):
        raise ValueError("Expected a local DWG or DXF source")
    if layers and layers[0].casefold() == layers[1].casefold():
        raise ValueError("Use two distinct layer names")
    with TemporaryDirectory(prefix="cad-readback-") as location:
        job = Path(location)
        if reference:
            name = reference["filename"]
            image = (source.parent/name).resolve()
            if Path(name).is_absolute() or ".." in Path(name).parts or source.parent.resolve() not in image.parents:
                raise ValueError("Image reference must be inside the drawing directory")
            if not image.is_file() or digest(image)!=reference["source_sha256"]:
                raise ValueError("Image reference missing or changed beside the drawing")
            (job/name).parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(image,job/name)

        def run(tag, drawing, commands, outputs):
            script = job / (tag + ".scr")
            script.write_text("\n".join(['(setvar "FILEDIA" 0)', *commands, "_.QUIT", ""]),
                              encoding="ascii")
            process = run_console([str(console), "/i", str(drawing), "/s", str(script),
                                      "/isolate", job.name + tag, str(job / "profile")],
                                     cwd=job, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, timeout=120)
            if process.returncode or not all((job / out).is_file() and (job / out).stat().st_size for out in outputs):
                raise RuntimeError(tag + " failed: " + process.stdout[-1000:].decode(errors="replace"))

        def export(filename):
            return f'(command "_.SAVEAS" "_DXF" "" (strcat (getvar "DWGPREFIX") "{filename}"))'

        dwg = job / "plan.dwg"
        original_hash = digest(source)
        if suffix == ".dwg":
            shutil.copyfile(source, dwg)
        else:
            staged = job / "source.dxf"
            shutil.copyfile(source, staged)
            run("save", staged, ['(command "_.ZOOM" "_E")', '(command "_.SAVEAS" "_2018" (strcat (getvar "DWGPREFIX") "plan.dwg"))'], ["plan.dwg"])
        frozen_hash = digest(dwg)
        run("reopen", dwg, [export("readback.dxf")], ["readback.dxf"])
        outputs = ["plan.dwg", "readback.dxf"]
        if reference:
            saved = ezdxf.readfile(job/"readback.dxf")
            paths = [e.image_def.dxf.filename.replace("\\","/") for e in saved.modelspace().query("IMAGE") if e.image_def]
            if paths.count(name)!=1:
                raise ValueError("Reopened drawing did not retain its source image path")
            outputs.append(name)
        if layers:
            doc = ezdxf.readfile(job / "readback.dxf")
            if not all(layer in doc.layers for layer in layers):
                raise ValueError("Both selected furniture layers must exist in the readback")
            commands = []
            for idx, layer in enumerate(layers, 1):
                handle = doc.layers.get(layer).dxf.handle
                if any(ch not in "0123456789ABCDEFabcdef" for ch in handle):
                    raise ValueError("Invalid DXF layer handle")
                commands.append(f'(setq n{idx} (cdr (assoc 2 (entget (handent "{handle}")))))')
            states = [("both_on", "_ON", "_ON"), ("movable_off", "_OFF", "_ON"),
                      ("fixed_off", "_ON", "_OFF"), ("both_off", "_OFF", "_OFF")]
            commands.extend(['(setvar "CLAYER" "0")', '(command "_.-LAYER" "_THAW" n1 "_THAW" n2 "")'])
            for label, one, two in states:
                commands.extend([f'(command "_.-LAYER" "{one}" n1 "{two}" n2 "")',
                                 export(label + ".dxf")])
            files = [name + ".dxf" for name, _, _ in states]
            run("states", dwg, commands, files)
            for name, one, two in states:
                state_doc = ezdxf.readfile(job / (name + ".dxf"))
                actual = [state_doc.layers.get(layer).is_off() or state_doc.layers.get(layer).is_frozen()
                          for layer in layers]
                if actual != [one == "_OFF", two == "_OFF"]:
                    raise RuntimeError("Native state mismatch: " + name)
            outputs.extend(files)
        if digest(source) != original_hash or digest(dwg) != frozen_hash:
            raise RuntimeError("The source or frozen DWG changed during readback")
        report = {"source_sha256": original_hash, "console": str(console), "layers": layers,
                  "files": {name: digest(job / name) for name in outputs}}
        with TemporaryDirectory(prefix=".cad-export-", dir=destination.parent) as shipping:
            ready = Path(shipping) / "complete"
            ready.mkdir()
            for name in outputs:
                (ready/name).parent.mkdir(parents=True,exist_ok=True)
                shutil.copyfile(job / name, ready / name)
            (ready / "capture.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            if destination.exists():
                raise FileExistsError(str(destination))
            ready.rename(destination)


def main():
    args = argparse.ArgumentParser(description=__doc__)
    args.add_argument("source", type=Path)
    args.add_argument("work_folder", type=Path)
    args.add_argument("--console", type=Path)
    args.add_argument("--layers", nargs=2, metavar=("MOVABLE", "FIXED"))
    options = args.parse_args()
    try:
        capture(options.source.resolve(), options.work_folder.resolve(), console_path(options.console), options.layers)
    except (ValueError, OSError, RuntimeError, subprocess.TimeoutExpired, ezdxf.DXFError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
