"""Place an unchanged source image under a new model using its pixel-to-mm registration."""
from math import isfinite
from pathlib import Path

from ezdxf.math import Vec2
from PIL import Image

from dimension_grid import GridProjection
from native_readback import digest

from presentation import SOURCE_LAYER,LEGACY_SOURCE_LAYER,modern_layers
LAYER = LEGACY_SOURCE_LAYER  # Existing integrations may still import this name.


def fit_axis(pairs, slope, offset):
    """Fit an affine axis minimising the largest residual at the printed control points."""
    if not pairs:
        return slope, offset, 0.0
    candidates = {slope}
    for i, (x, u) in enumerate(pairs):
        for y, v in pairs[i + 1:]:
            candidates.add((v - u) / (y - x))
    best = None
    for value in sorted(candidates):
        residuals = [station - value * px for px, station in pairs]
        shift = (min(residuals) + max(residuals)) / 2
        err = max(abs(r - shift) for r in residuals)
        score = (err, abs(value - slope))
        if best is None or score < best[0]:
            best = score, value, shift
    return best[1], best[2], best[0][0]


def attach(doc, image_path, registration, clip_px=None, reference_name=None):
    """Add one raster below the CAD entities. Ship the source under filename beside the DWG."""
    image_path = Path(image_path)
    layer_name=SOURCE_LAYER if modern_layers(registration.get('layers',{})) else LEGACY_SOURCE_LAYER
    filename = image_path.name if reference_name is None else reference_name
    if not isinstance(filename, str) or filename in ("", ".", "..") or any(c in filename for c in "/\\:\0"):
        raise ValueError("Use a single relative filename for the original image")
    model = doc.modelspace()
    if doc.units != 4 or model.query(f'IMAGE[layer=="{layer_name}"]'):
        raise ValueError("Expected a millimetre model without an existing marketing image reference")
    source_hash = digest(image_path)
    if registration.get("source_sha256", source_hash) != source_hash:
        raise ValueError("The reference image differs from the registered source")
    with Image.open(image_path) as image:
        width, height = image.size
    declared_size = registration.get("image_size")
    if declared_size is not None and list(declared_size) != [width, height]:
        raise ValueError("Image dimensions do not match registration")
    if clip_px is not None:
        x0, y0, x1, y1 = map(float, clip_px)
        if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
            raise ValueError("Reference crop must be inside the original image")
        clip_px = [x0, y0, x1, y1]
    projection = GridProjection(registration)
    a, b, d, e, tx, ty = projection.matrix
    grid = registration.get("grid_registration", {}).get("axes", {})
    a, tx, dx = fit_axis(grid.get("x", []), a, tx)
    e, ty, dy = fit_axis(grid.get("y", []), e, ty)
    if not all(map(isfinite, (a, b, d, e, tx, ty))) or abs(a*e - b*d) < 1e-12:
        raise ValueError("Invalid reference transformation")

    # ezdxf IMAGE uses a lower-left insertion point and pixel centres for clipping.
    u, v = Vec2(a, d), Vec2(-b, -e)
    insert = Vec2(tx, ty) - u*0.5 - v*(height-0.5)
    layer = doc.layers.get(layer_name) if layer_name in doc.layers else doc.layers.new(layer_name)
    layer.on()
    layer.thaw()
    layer.lock()
    layer.dxf.plot = 0
    definition = doc.add_image_def(filename=filename, size_in_pixel=(width, height))
    ref = model.add_image(definition, insert, (u.magnitude*width, v.magnitude*height),
                          dxfattribs={"layer":layer_name, "fade":52})
    ref.dxf.u_pixel = (u.x, u.y, 0)
    ref.dxf.v_pixel = (v.x, v.y, 0)
    if clip_px:
        ref.set_boundary_path([(x0, y0), (x1, y1)])
    model.set_redraw_order([*model.get_redraw_order(), (ref.dxf.handle, "1")])
    return {"filename":filename, "handle":ref.dxf.handle, "layer":layer_name, "source_sha256":source_hash,
            "image_size_px":[width, height], "clip_px":clip_px, "image_to_mm":[a, b, d, e, tx, ty],
            "max_axis_residual_mm":{"x":dx, "y":dy}}
