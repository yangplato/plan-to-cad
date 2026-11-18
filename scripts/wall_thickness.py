"""Select 100/120/200/240mm from locally calibrated wall-face pixels.

V1.2's superseded defaults remain available only for explicit frozen replay.
Source reading and any ambiguous selection still require independent review.
"""
from copy import deepcopy
import math
import re
from drafting_version import version

POLICY = "wall-thickness-calibrated-pixels/1"
LEGACY_POLICY = "wall-thickness-240-120/1"
CHOICES = (100.0, 120.0, 200.0, 240.0)
DEFAULTS = {"main": 240.0, "partition": 120.0}
EPS = 1e-6  # numerical equality only


def number(value, name):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name}: thickness must be a finite positive millimetre number")
    return float(value)


def sha(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value.lower())


def enabled(scene):
    value = str(scene.get("drafting_version", "")).lower().removeprefix("v")
    match = re.fullmatch(r"(\d+)\.(\d+)(?:\.\d+)?", value)
    return bool(match and tuple(map(int, match.groups())) >= (1, 2))


def _legacy_width(wall, source_sha256=None):
    """Return the permitted width; also usable by custom rectangle builders.

    No evidence means the class default, even if a previous inferred width was
    supplied. This function does not move geometry. Scene preparation separately
    requires a fixed reference before changing an existing width.
    """
    ident = wall.get("id", "wall")
    kind = wall.get("element_kind", "wall")
    if kind == "column":
        return number(wall.get("width_mm"), f"{ident}/column")
    if kind != "wall":
        raise ValueError(f"{ident}: element_kind must be wall or column")
    role = wall.get("wall_class")
    if role not in DEFAULTS:
        raise ValueError(f"{ident}: declare wall_class main or partition; wall colour is not a thickness or structural proof")
    if "width_mm" in wall:
        number(wall["width_mm"], ident)
    evidence = wall.get("thickness_evidence")
    if evidence is None:
        return DEFAULTS[role]
    if not isinstance(evidence, dict) or evidence.get("kind") not in {"printed_dimension", "building_document"}:
        raise ValueError(f"{ident}: pixel estimates/model values do not authorize a non-default wall thickness")
    value = number(evidence.get("value_mm"), f"{ident}/explicit thickness")
    if not sha(evidence.get("source_sha256")) or not isinstance(evidence.get("locator"), str) or not evidence["locator"].strip():
        raise ValueError(f"{ident}: explicit thickness needs source hash and dimension/page locator")
    if evidence["kind"] == "printed_dimension":
        if not sha(source_sha256) or evidence["source_sha256"].lower() != source_sha256.lower():
            raise ValueError(f"{ident}: printed thickness evidence belongs to another source image")
    elif not isinstance(evidence.get("source_ref"), str) or not evidence["source_ref"].strip():
        raise ValueError(f"{ident}: building-document thickness needs a traceable source_ref")
    if "width_mm" in wall and abs(number(wall["width_mm"], ident)-value) > EPS:
        raise ValueError(f"{ident}: requested width and explicit source thickness disagree")
    return value


def pixel_policy(scene):
    value = str(scene.get('drafting_version', '1.3.0')).lower().removeprefix('v')
    match = re.fullmatch(r'(\d+)\.(\d+)(?:\.\d+)?', value)
    return bool(match and tuple(map(int, match.groups())) >= (1, 3))


def pixel_selection(wall, source_sha256, projection, preferred_mm=None):
    """Measure original pixels through the same local grid used by geometry."""
    ident=wall.get('id','wall');ev=wall.get('thickness_evidence')
    if not isinstance(ev,dict) or ev.get('kind')!='calibrated_pixels':
        raise ValueError(f'{ident}: supply calibrated wall-face pixels; no 240/120 default applies')
    if (not sha(source_sha256) or not sha(ev.get('source_sha256')) or ev['source_sha256'].lower()!=source_sha256.lower()
            or not isinstance(ev.get('locator'),str) or not ev['locator'].strip()
            or not isinstance(ev.get('calibration_basis'),str) or not ev['calibration_basis'].strip()):
        raise ValueError(f'{ident}: pixel evidence needs matching source hash, locator and dimension calibration_basis')
    pts=ev.get('face_points_px')
    if (not isinstance(pts,list) or len(pts)!=2 or
            any(not isinstance(p,(list,tuple)) or len(p)!=2 or
                any(type(v) not in (int,float) or not math.isfinite(v) for v in p) for p in pts)):
        raise ValueError(f'{ident}: use two finite original-image face_points_px')
    if 'a' in wall and 'b' in wall:
        a,b=wall['a'],wall['b']
        if (a[0]==b[0])==(a[1]==b[1]):
            raise ValueError(f'{ident}: use an orthogonal nonzero wall section')
        normal=0 if a[0]==b[0] else 1
    else:
        if ev.get('axis') not in ('x','y'):
            raise ValueError(f'{ident}: custom builders must declare the pixel section axis')
        normal=0 if ev['axis']=='x' else 1
    if abs(pts[0][1-normal]-pts[1][1-normal])>EPS or abs(pts[0][normal]-pts[1][normal])<=EPS:
        raise ValueError(f'{ident}: face pixels must cross the same perpendicular section')
    error=ev.get('edge_uncertainty_px')
    if type(error) not in (int,float) or not math.isfinite(error) or error<0:
        raise ValueError(f'{ident}: record nonnegative per-edge pixel uncertainty from this image')
    if projection is None:
        raise ValueError(f'{ident}: provide the dimension-calibrated projection')
    project=projection.world if hasattr(projection,'world') else projection
    mm=abs(project(pts[1])[normal]-project(pts[0])[normal])
    differences=[]
    for first in (-error,error):
        for second in (-error,error):
            p,q=list(pts[0]),list(pts[1]);p[normal]+=first;q[normal]+=second
            differences.append(project(q)[normal]-project(p)[normal])
    if not math.isfinite(mm) or not all(math.isfinite(v) for v in differences):
        raise ValueError(f'{ident}: invalid calibrated span')
    low=0.0 if min(differences)<=0<=max(differences) else min(abs(v) for v in differences)
    high=max(abs(v) for v in differences)
    candidates=[v for v in CHOICES if low-EPS<=v<=high+EPS]
    if not candidates:
        raise ValueError(f'{ident}: no 100/120/200/240 candidate fits the observed pixel range; recheck faces/scale')
    basis = ev.get("selection_basis","single pixel-supported candidate")
    if preferred_mm in candidates:
        selected = preferred_mm
        basis = "本户普通外墙统一选档，且在本截面原图候选范围内"
    elif len(candidates)==1:
        selected=candidates[0]
    else:
        selected=wall.get('width_mm')
        if (type(selected) not in (int,float) or selected not in candidates or
                not isinstance(ev.get('selection_basis'),str) or not ev['selection_basis'].strip()):
            raise ValueError(f'{ident}: ambiguous thickness candidates {candidates}; retain candidates and check another section/control/net span')
    return dict(measured_mm=mm,measured_range_mm=[low,high],candidates_mm=candidates,
                selected_mm=float(selected),selection_basis=basis)


def resolve_width(wall, source_sha256=None, *, drafting_version='1.3.0', projection=None, preferred_mm=None):
    """Current custom-builder API. It selects width and never moves geometry."""
    if not pixel_policy({'drafting_version':drafting_version}):
        return _legacy_width(wall,source_sha256)
    kind=wall.get('element_kind','wall')
    if kind=='column':return number(wall.get('width_mm'),f"{wall.get('id')}/column")
    if kind!='wall':raise ValueError('element_kind must be wall or column')
    if 'width_mm' in wall:number(wall['width_mm'],wall.get('id','wall'))
    ev=wall.get('thickness_evidence')
    if isinstance(ev,dict) and ev.get('kind') in ('printed_dimension','building_document'):
        # Keep explicit dimensions, including a clearly marked value outside the four inferred sizes.
        return _legacy_width(dict(wall,wall_class='main'),source_sha256)
    return pixel_selection(wall,source_sha256,projection,preferred_mm)['selected_mm']


def _pixel_projection(scene):
    from dimension_grid import GridProjection,solve_grid
    if 'dimension_grid' in scene:
        matrix,report=solve_grid(scene)
        registration={'source_to_mm':matrix,'grid_registration':report}
    else:
        report=scene.get('grid_registration',{})
        registration=scene
    if (isinstance(report.get('scale_provenance'),dict) and report['scale_provenance'].get('kind')=='assumed_wall_thickness'
            and version(scene)<(1,5)):
        raise ValueError('Do not use an assumed wall thickness to prove the same wall thickness')
    return GridProjection(registration),report


def _bound_controls(scene, ident):
    controls = scene.get("dimension_grid", {}).get("controls", {})
    return [name for name, c in controls.items() for b in c.get("bindings", [])
            if b.get("wall") == ident and b.get("at") in {"center", "min_face", "max_face"}]


def prepare(scene):
    """Copy new scenes, choose widths, and register a fixed reference once."""
    if not enabled(scene):
        return scene
    result = deepcopy(scene)
    modern=pixel_policy(result)
    geometry_v16 = version(result) >= (1,6)
    if geometry_v16:
        from wall_geometry_v16 import bind_controls, ordinary_preference
        bind_controls(result)
    policy=POLICY if modern else LEGACY_POLICY
    rows = result.get("walls", [])
    if not isinstance(rows, list):
        raise ValueError("walls must be an array")
    needs_pixels=modern and any(isinstance(w.get('thickness_evidence'),dict) and w['thickness_evidence'].get('kind')=='calibrated_pixels' for w in rows)
    grid = result.get('dimension_grid', result.get('grid_registration', {}))
    ref = grid.get('reference',grid.get('scale_provenance'))
    provisional = (version(result)>=(1,5) and grid.get('mode')=='unmarked' and
                   isinstance(ref,dict) and ref.get('kind')=='assumed_wall_thickness')
    projection,calibration=_pixel_projection(result) if needs_pixels or provisional else (None,{})
    for row in rows:
        preferred = ordinary_preference(result,row) if geometry_v16 else None
        is_anchor = provisional and row['id']==calibration['scale_provenance']['anchor_wall']
        if is_anchor:
            if (row.get('thickness_evidence') or {}).get('kind') in ('printed_dimension','building_document'):
                raise ValueError('Use the documented wall thickness as a known scale reference')
            selected = number(calibration['scale_provenance']['value_mm'],row['id'])
        else:
            selected = resolve_width(row, result.get("source_sha256"),drafting_version=result['drafting_version'],projection=projection,preferred_mm=preferred)
        measurement=None
        if modern and not is_anchor and isinstance(row.get('thickness_evidence'),dict) and row['thickness_evidence'].get('kind')=='calibrated_pixels':
            measurement=pixel_selection(row,result.get('source_sha256'),projection,preferred)
            axis='x' if row['a'][0]==row['b'][0] else 'y'
            if not provisional and axis in calibration.get('unmeasured_axes',[]):
                raise ValueError(f"{row.get('id')}: {axis} wall thickness has no independently calibrated dimension scale")
        if row.get("element_kind", "wall") == "column":
            continue
        old = row.get("width_mm")
        previous = row.get("wall_thickness_review")
        if previous is not None:
            selected_before = previous.get("selected_mm") if isinstance(previous, dict) else None
            if (not isinstance(previous, dict) or previous.get("policy") != policy or
                    previous.get("wall_class") != row.get("wall_class") or
                    previous.get("applied") is not True or old is None or
                    type(selected_before) not in (int, float) or not math.isfinite(selected_before) or
                    abs(number(old, row["id"])-selected_before) > EPS or
                    abs(selected-old) > EPS):
                raise ValueError(f"{row.get('id')}: stale/prepared wall-thickness record; start from preserved source observations")
            continue
        changed = old is not None and abs(old-selected) > EPS
        bindings = _bound_controls(result, row.get("id"))
        reference = "bound_controls" if bindings else row.get("thickness_reference")
        if reference is None and not changed:
            reference = "center"
        if reference not in {"center", "min_face", "max_face", "bound_controls"}:
            raise ValueError(f"{row.get('id')}: changing inferred thickness requires thickness_reference center/min_face/max_face or a bound control")
        if reference == "bound_controls" and not bindings:
            raise ValueError(f"{row.get('id')}: no bound fixed control exists")
        row["width_mm"] = selected
        row["wall_thickness_review"] = {
            "policy": policy, "wall_class": row.get("wall_class"), "previous_mm": old,
            "selected_mm": selected, "changed": changed, "fixed_reference": reference,
            "bound_control_ids": bindings, "applied": False,
            "basis": "calibrated_pixels" if measurement else ("explicit_dimension" if row.get("thickness_evidence") else "legacy_user_default"),
            "evidence_check": "format/source binding only; actual source reading requires independent review"}
        if measurement:row['wall_thickness_review'].update(measurement)
        if is_anchor or (provisional and measurement):
            row['wall_thickness_review']['basis'] = 'assumed_wall_thickness' if is_anchor else 'provisional_pixel_scale'
            row['wall_thickness_review']['scale_reference'] = calibration['scale_provenance']
    result["wall_thickness_policy"] = policy
    return result


def fixed_stations(source, work, matrix):
    """WCS normal stations for the existing connected-wall constraint solver."""
    result = []
    a, b, d, e, tx, ty = matrix
    if max(abs(b), abs(d)) > EPS or a <= 0 or e >= 0:
        raise ValueError("wall-thickness changes require a rectified orthogonal source frame")
    by_id = {w["id"]: w for w in work["walls"]}
    for wall in source["walls"]:
        record = wall.get("wall_thickness_review") or {}
        if not record or record.get("applied") or record["fixed_reference"] == "bound_controls":
            continue
        row = by_id[wall["id"]]
        p, q = row["a"], row["b"]
        if (p[0] == q[0]) == (p[1] == q[1]):
            raise ValueError(f"{wall['id']}: fixed wall reference requires a nonzero orthogonal segment")
        ni = 0 if p[0] == q[0] else 1
        center = (a*p[0]+tx) if ni == 0 else (e*p[1]+ty)
        old = record["previous_mm"] if record["previous_mm"] is not None else record["selected_mm"]
        delta = (record["selected_mm"]-old)/2
        center += {"center": 0, "min_face": delta, "max_face": -delta}[record["fixed_reference"]]
        result.append((wall["id"], ni, center))
    return result


def mark_applied(scene):
    for wall in scene.get("walls", []):
        record = wall.get("wall_thickness_review")
        if record:
            record["applied"] = True
    return scene


def records(scene):
    return {w["id"]: deepcopy(w["wall_thickness_review"]) for w in scene.get("walls", [])
            if w.get("wall_thickness_review")}
