"""Resolve project DXFs and place native geometry from the bundled millimetre library."""
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path

import ezdxf
from ezdxf import bbox
from ezdxf.math import Matrix44, Vec2

ASSETS = Path(__file__).resolve().parent.parent / "assets"
BUNDLED = ASSETS / "interior-plan-blocks.dxf"
GUIDES = "辅助-尺寸与净距"


def resolve_file(root, filename):
    """Omitting the file opts into the bundled library; explicit files must exist."""
    if filename is None or (root is None and filename == BUNDLED.name):
        return BUNDLED
    if root is None:
        raise ValueError("An external block file requires library_root")
    base = Path(root).resolve()
    file = (base / filename).resolve()
    if base not in file.parents or file.suffix.lower() != ".dxf" or not file.is_file():
        raise ValueError("Use an existing DXF inside library_root: " + str(filename))
    return file


@lru_cache(maxsize=1)
def index():
    return json.loads(BUNDLED.with_suffix(".json").read_text(encoding="utf-8"))["blocks"]


@lru_cache(maxsize=4)
def library(file):
    return ezdxf.readfile(file)


def place_bundled(doc, row, anchor, facing, layer):
    name = row["library_block"]
    record = index().get(name)
    if record is None:
        raise ValueError(f"Unknown bundled block: {name}")
    src = library(BUNDLED).blocks[name]
    front = Vec2(row["library_front_vector"])
    if min(front.magnitude, facing.magnitude) < 1e-9:
        raise ValueError("Select a non-zero local front and destination facing direction")
    front = front.normalize()
    right = Vec2(-front.y, front.x)
    orientation = hashlib.sha256(f"{front.x:.12g},{front.y:.12g}".encode()).hexdigest()[:10]
    dest_name = f"LIB_{name}_{orientation}"
    if dest_name not in doc.blocks:
        dest = doc.blocks.new(dest_name)
        dest.block_record.dxf.units = 4
        orient = Matrix44.z_rotate(-right.angle)
        for entity in src:
            if entity.dxf.layer == GUIDES:
                continue
            new = entity.copy()
            new.transform(orient)
            new.dxf.layer = "0"
            new.dxf.color = 0
            new.dxf.linetype = "BYBLOCK"
            new.dxf.lineweight = -2
            new.dxf.discard("true_color")
            if new.dxftype() == "POLYLINE":
                for vertex in new.vertices:
                    vertex.dxf.layer = "0"
                if new.seqend is not None:
                    new.seqend.dxf.layer = "0"
            dest.add_entity(new)
        ext = bbox.extents(dest)
        if not ext.has_data or min(ext.size.x, ext.size.y) <= 0:
            raise ValueError(f"Expected measurable geometry in {name}")
        # Keep native curve geometry in mm. Use the rear-face centre for the insertion
        # base point so scaling changes the footprint without moving the wall anchor.
        dest.block.dxf.base_point = ((ext.extmin.x + ext.extmax.x) / 2, ext.extmax.y, 0)
    else:
        dest = doc.blocks[dest_name]
    ext = bbox.extents(dest)
    width = float(row["width_mm"])
    depth = float(row["depth_mm"])
    if not all(math.isfinite(n) and n > 0 for n in (width, depth)):
        raise ValueError("Select positive finite width_mm and depth_mm")
    angle = math.degrees(math.atan2(facing.x, -facing.y))
    return doc.modelspace().add_blockref(dest_name, anchor, dxfattribs={
        "layer": layer, "xscale": width / ext.size.x, "yscale": depth / ext.size.y, "rotation": angle})
