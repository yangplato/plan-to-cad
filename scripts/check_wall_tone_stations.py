#!/usr/bin/env python3
"""Compare source-only wall-core colour runs and change stations with saved DXF material."""
import argparse
from hashlib import sha256
import json
import math
from pathlib import Path
from tempfile import TemporaryDirectory

import ezdxf
import numpy as np
from PIL import Image, ImageDraw, ImageOps
from shapely.affinity import affine_transform
from shapely.geometry import LineString, Polygon, box
from shapely.ops import unary_union

from check_wall_probes import decode, numbers
from dimension_grid import GridProjection
from fit_wall_guides import apply_edits, crossing_ranges, fit_pieces, observe, prepare
from scan_wall_cores import runs

EPS = 1e-6  # Pixel arithmetic only.
STABLE = {"stable_core", "confirmed_core"}
SWAP = [0, 1, 1, 0, 0, 0]


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def source_axes(image, definition, image_hash):
    if definition.get("image_sha256",image_hash) != image_hash:
        raise ValueError("Source reading belongs to another original image")
    if "observations" in definition["axes"][0]:
        if "image_sha256" not in definition:
            raise ValueError("Station report requires its original image hash")
        return definition["axes"], definition["samples"]
    scope, names, codes, samples, guides, negatives = prepare(image, definition)
    readings = [observe(g, scope, codes, names, negatives) for g in guides]
    axes = [item[0] for item in readings]
    marks = {axis["id"]: m for axis, m, _ in readings}
    for guide, (axis, obs, centre) in zip(guides, readings):
        fit_pieces(guide, axis, obs, centre, crossing_ranges(axis, axes, marks), names)
        apply_edits(guide, axis, None)  # Records independently identified voids.
        axis["review_spans"] = guide.get("review_spans", [])
    return axes, samples


def registration(reg, image_hash):
    if reg.get("source_sha256", image_hash) != image_hash:
        raise ValueError("Registration belongs to another original image")
    a, b, d, e, _, _ = numbers(reg["source_to_mm"], 6)
    matrix = np.array([[a, b], [d, e]])
    inv = np.linalg.inv(matrix)
    projection = GridProjection(reg)
    limit, = numbers([reg["holdout_limit_mm"]], 1)
    if not reg["holdouts"] or limit < 0:
        raise ValueError("Independent registration holdouts and a read tolerance in mm are required")
    residuals = []
    for item in reg["holdouts"]:
        uv = numbers(item["source_xy"], 2)
        actual = numbers(item["cad_mm"], 2)
        err = math.dist(projection.world(uv), actual)
        if err > limit + 1e-8:
            raise ValueError(f"Registration residual {err:g} exceeds {limit:g} mm")
        residuals.append(err)
    errors = [limit/projection.min_mm_per_px(i) if axis else limit*np.linalg.norm(inv[i])
              for i, axis in enumerate(projection.axes)]
    return projection, errors, residuals


def cad_material(doc, reg, projection):
    def raw_area(area):
        polygons = [Polygon(projection.source_path(poly.exterior.coords),
                            [projection.source_path(hole.coords) for hole in poly.interiors])
                    for poly in parts(area) if poly.geom_type == "Polygon"]
        return unary_union(polygons)

    names = reg["layers"]
    if names["bearing"].casefold() == names["partition"].casefold():
        raise ValueError("Register two distinct colour layers")
    areas, uncertain, issues, inventory = {}, [], [], {}
    for role in ("bearing", "partition"):
        name = names[role]
        profile = {"wall_layers": {name: reg.get("wall_layers", {}).get(name, "fills")},
                   "nonwall_fill_layers": [s for s in names.values() if s.casefold() != name.casefold()]
                                          + reg.get("nonwall_fill_layers", [])}
        area, unclear, warnings, counts = decode(doc, profile)
        counts = counts[name.casefold()]
        # An empty, independently named layer is a measurable absence of this tone.
        issues.extend(w for w in warnings if counts or w != name.casefold()+": no reliably decoded wall material")
        uncertain.extend((ref, raw_area(g)) for ref, g in unclear)
        areas[role] = raw_area(area)
        inventory[role] = {"layer": name, "entities": counts}
    return areas, uncertain, sorted(set(issues)), inventory


def stable_runs(axis, pixels, colours):
    start, end, n = axis["start"], axis["end"], axis["normal"]
    if not 0 <= start <= end < pixels.shape[1] or not 0 <= n < pixels.shape[0]:
        raise ValueError(f"{axis['id']}: axis outside original image")
    labels = ["source_unread"] * (end-start+1)
    widths = np.zeros(len(labels))
    for obs in axis["observations"]:
        lo, hi = obs["stations_px"]
        if not start <= lo <= hi <= end:
            raise ValueError("Bad source station interval")
        state, role = obs["state"], obs["material"]
        if state not in STABLE:
            labels[lo-start:hi-start+1] = [state]*(hi-lo+1)
            continue
        w = obs["width_px"]
        rows = core_rows(n, w, pixels.shape[0])
        if len(rows) < 3:
            labels[lo-start:hi-start+1] = ["thin_or_short_core"]*(hi-lo+1)
            continue
        rgb, tolerance = colours[role]
        stripe = pixels[rows, lo:hi+1, :]
        agrees = (np.linalg.norm(stripe-rgb, axis=2) <= tolerance+0.01).all(axis=0)
        for t, ok in zip(range(lo, hi+1), agrees):
            labels[t-start] = role if ok else "unstable_colour_band"
            widths[t-start] = w
    def mask(span, reason, inclusive=False):
        lo, hi = sorted(numbers(span, 2))
        for t in range(max(start, math.ceil(lo) if inclusive else math.floor(lo+.5)),
                       min(end, math.floor(hi) if inclusive else math.ceil(hi-.5))+1):
            labels[t-start] = reason
    for item in axis.get("crossings", []):
        mask(item["stations_px"], "crossing_or_T", True)
    for item in axis.get("confirmed_voids", []):
        mask(item["span_px"], "source_void_"+item["kind"])
    for item in axis.get("review_spans", []):
        if not item.get("reason"):
            raise ValueError("A review span needs a source reason")
        mask(item["span_px"], "source_review_"+item["reason"])
    if axis.get("competing_axes"):
        labels[:] = ["competing_source_axes"]*len(labels)
    for lo, hi, label in list(runs(labels)):
        if label in colours and hi-lo+1 < max(4, math.ceil(np.median(widths[lo:hi+1])*1.5)):
            labels[lo:hi+1] = ["thin_or_short_core"]*(hi-lo+1)
    pieces = []
    for lo, hi, label in runs(labels):
        first, last = lo+start, hi+start
        p = dict(stations_px=[first, last], span_px=[first-.5, last+.5], label=label)
        if label in colours:
            w = float(np.median(widths[lo:hi+1]))
            rows = core_rows(n, min(widths[lo:hi+1]), pixels.shape[0])
            p["width_px"] = w
            # Retain two shoulder patches and actual source colours for every run.
            depth = min(max(2, math.ceil(w/2)), max(1, (last-first+1)//2))
            p["shoulders"] = []
            for a, b in ((first, first+depth-1), (last-depth+1, last)):
                vals = pixels[rows, a:b+1, :].reshape(-1, 3)
                p["shoulders"].append(dict(stations_px=[a, b], normal_rows_px=[rows[0], rows[-1]],
                                           rgb_median=np.median(vals,axis=0).tolist(),
                                           rgb_max_distance=float(np.linalg.norm(vals-colours[label][0],axis=1).max())))
        pieces.append(p)
    return pieces, widths


def core_rows(n, width, height):
    return list(range(max(0, math.ceil(n-width*.28)), min(height-1, math.floor(n+width*.28))+1))


def parts(geom):
    if not geom.is_empty and geom.geom_type in ("Polygon","LineString"):
        yield geom
    for g in getattr(geom, "geoms", ()):
        yield from parts(g)


def merged(intervals):
    result = []
    for a, b in sorted(intervals):
        if b-a <= EPS:
            continue
        if result and a <= result[-1][1]+EPS:
            result[-1][1] = max(result[-1][1], b)
        else:
            result.append([a, b])
    return result


def projected(geom, dim, polygons=False):
    return merged((g.bounds[dim], g.bounds[dim+2]) for g in parts(geom)
                  if not polygons or (g.geom_type=="Polygon" and g.area>EPS**2))


def normal_read(total, t, n, w, uncertainty):
    reach = max(18., 2.5*w, 2*uncertainty+w)
    line = LineString([(t,n-reach),(t,n+reach)])
    options = [(a,b) for a,b in projected(total.intersection(line),1)
               if a < n+w+uncertainty and b > n-w-uncertainty]
    if len(options)!=1:
        return ("no_unique_cad_wall_strip", None)
    a,b = options[0]
    offset = (a+b)/2-n
    if abs(b-a-w)>max(2*uncertainty, w*.32):
        return ("width_or_junction_review", offset)
    return ("normal_offset_review" if abs(offset)>uncertainty+EPS else "aligned", offset)


def inspect_run(p, widths, axis, geoms, total, questionable, issues, along_band, normal_band, projection):
    t0,t1 = p["stations_px"]
    ti = int(axis["orientation"] == "V")
    n, start = axis["normal"],axis["start"]
    normal = []
    for t in range(t0,t1+1):
        normal.append(normal_read(total,t,n,widths[t-start],normal_band))
    p["normal_checks"] = []
    reason = [s for s, _ in normal]
    for i,j,s in runs(reason):
        offsets = [(t0+idx,value) for idx,(_,value) in enumerate(normal[i:j+1],start=i) if value is not None]
        delta = float(np.median([value for _,value in offsets])) if offsets else None
        delta_mm = float(np.median([projection.signed_distance(1-ti,n,n+v,t) for t,v in offsets])) if offsets else None
        p["normal_checks"].append(dict(state=s,stations_px=[t0+i,t0+j],
            median_offset_px=delta,median_offset_mm=delta_mm))
    role = p["label"]
    other = next(geom for key,geom in geoms.items() if key!=role)
    ok = geoms[role]
    measurements = []
    for i,j,state in runs(reason):
        lo,hi = t0+i-.5,t0+j+.5
        w = min(widths[t0+i-start:t0+j-start+1])
        if state!="aligned":
            continue
        region = box(lo,n-w*.25,hi,n+w*.25)
        region_issues = [ref for ref,g in questionable if g.intersects(region)]
        wrong = projected(region.intersection(other),0,True)
        missing = projected(region.difference(total),0,True)
        overlap = projected(region.intersection(other).intersection(ok),0,True)
        decisive = [max(lo,p["span_px"][0]+along_band), min(hi,p["span_px"][1]-along_band)]
        def inside(spans):
            return merged((max(a,decisive[0]),min(b,decisive[1])) for a,b in spans)
        proven = inside(wrong)
        unknown = bool(issues or region_issues or inside(overlap) or inside(missing) or decisive[1]<=decisive[0])
        status = "review" if unknown else "wrong_tone" if proven else "within_band"
        measurements.append(dict(span_px=[lo,hi],decisive_span_px=decisive,raw_other_tone_px=wrong,
            decisive_other_tone_px=proven,other_tone_length_mm=sum(projection.signed_distance(ti,a,b,n) for a,b in proven),
            no_material_px=missing,both_tones_px=overlap,ambiguous_geometry=region_issues,status=status,sufficient_tone_evidence=not unknown))
    p["material_checks"] = measurements
    if any(m["status"]=="wrong_tone" for m in measurements):
        p["status"] = "wrong_tone_on_reported_intervals"
    elif any(s!="aligned" for s in reason) or any(m["status"]=="review" for m in measurements):
        p["status"] = "review"
    else:
        p["status"] = "within_band"


def blend_gap(pixels,n,width,left,right,mid,colours):
    """Only accept an observed short, uniform, monotone mixture of the two flank colours."""
    if not mid:
        return True
    if any(p["label"] not in ("no_sampled_core","unstable_colour_band","review") for p in mid):
        return False
    lo,hi = mid[0]["stations_px"][0],mid[-1]["stations_px"][1]
    if hi-lo+1 > min(5,max(2,math.ceil(width*.5))):
        return False
    rows = core_rows(n,width,pixels.shape[0])
    values = pixels[rows,lo:hi+1,:]
    start,tol1 = colours[left]
    end,tol2 = colours[right]
    vector = end-start
    f = ((values-start) @ vector)/(vector @ vector)
    errors = np.linalg.norm(values-(start+f[...,None]*vector),axis=2)
    return bool(np.all(errors<=min(tol1,tol2)) and np.all((f>.015)&(f<.985))
                and np.all(np.diff(f,axis=1)>=-.04) and np.all(np.ptp(f,axis=0)<.14))


def contacts(geoms, lo, hi, y, before, after):
    line = LineString([(lo,y),(hi,y)])
    left = projected(geoms[before].intersection(line),0)
    right = projected(geoms[after].intersection(line),0)
    return [(b+c)/2 for _,b in left for c,_ in right if abs(b-c)<EPS and lo+EPS<b<hi-EPS]


def transition(left,right,mid,axis,geom,total,questionable,issues,pixels,colours,band,normal_band,projection):
    before,after = left["label"],right["label"]
    lo,hi = left["span_px"][1],right["span_px"][0]
    n = axis["normal"]
    w = min(left["width_px"],right["width_px"])
    row = dict(from_tone=before,to_tone=after,source_flanks=[left["shoulders"][1],right["shoulders"][0]],
               flank_separation_span_px=[lo,hi],source_band_px=None,source_mid_px=None)
    if not blend_gap(pixels,n,w,before,after,mid,colours):
        row.update(status="review",reason="intervening_source_gap_or_artifact")
        return row
    row.update(source_band_px=[lo-band,hi+band],source_mid_px=(lo+hi)/2)
    if abs(left["width_px"]-right["width_px"])>max(.32*w,2*normal_band):
        row.update(status="review",reason="different_source_core_widths")
        return row
    # Associate a crossing only inside the two adjacent measured source runs.
    start,end = np.mean(left["span_px"]),np.mean(right["span_px"])
    corridor = box(start,n-w*.25,end,n+w*.25)
    if issues or any(g.intersects(corridor) for _,g in questionable):
        row.update(status="review",reason="ambiguous_saved_geometry")
        return row
    for run in (left,right):
        if any(r["state"]!="aligned" and r["stations_px"][1]>=start and r["stations_px"][0]<=end
               for r in run["normal_checks"]):
            row.update(status="review",reason="normal_alignment_or_wall_width")
            return row
    if any(normal_read(total,t,n,w,normal_band)[0]!="aligned" for t in range(math.ceil(lo),math.floor(hi)+1)):
        row.update(status="review",reason="normal_alignment_at_transition")
        return row
    if (corridor.difference(total).area>EPS or
            corridor.intersection(geom[before]).intersection(geom[after]).area>EPS):
        row.update(status="review",reason="saved_gap_or_overlapping_tones")
        return row
    offsets = [-.24,0,.24]
    rows = [contacts(geom,start,end,n+off*w,before,after) for off in offsets]
    row["cad_contacts_by_core_rail_px"] = rows
    if any(len(r)!=1 for r in rows):
        covering = [key for key,g in geom.items() if corridor.difference(g).area<=EPS]
        if len(covering)==1 and all(not r for r in rows):
            row.update(status="missing_change",reason="one_saved_tone_across_both_flanks",cad_tone=covering[0])
        else:
            row.update(status="review",reason="no_unique_matching_saved_change_between_flanks")
        return row
    points = [r[0] for r in rows]
    # Verify the entire corridor against the expected order, including slivers between rails.
    bad_left = projected(geom[after].intersection(corridor),0,True)
    bad_right = projected(geom[before].intersection(corridor),0,True)
    if not bad_left or not bad_right:
        row.update(status="review",reason="missing_saved_flank")
        return row
    extremes = [bad_left[0][0],bad_right[-1][1]]
    if max(abs(min(points)-extremes[0]),abs(max(points)-extremes[1]))>EPS:
        row.update(status="review",reason="irregular_or_additional_saved_tone_extent",cad_tone_extrema_px=extremes)
        return row
    delta = float(np.median(points))-row["source_mid_px"]
    row.update(cad_station_px=float(np.median(points)),cad_station_range_px=sorted(extremes),
               signed_delta_px=delta,signed_delta_mm=projection.signed_distance(int(axis["orientation"]=="V"),row["source_mid_px"],float(np.median(points)),n))
    low,high = row["source_band_px"]
    if max(points)<low-EPS or min(points)>high+EPS:
        row["status"]="shifted_change"
    elif min(points)>=low-EPS and max(points)<=high+EPS:
        row["status"]="within_band"
    else:
        row.update(status="review",reason="saved_change_straddles_read_band")
    return row


def unresolved_reading(p, pixels, n, w, colours):
    """Describe visible ink only. A narrow pair is a review clue, not a window identity."""
    state = p["label"]
    if state in ("short_core","thin_or_short_core"):
        return "short_coloured_run_or_noise"
    if state in ("no_sampled_core","review","unstable_colour_band"):
        lo,hi = p["stations_px"]
        reach = max(4,math.ceil(w*1.5))
        top,bottom = max(0,math.floor(n-reach)),min(pixels.shape[0]-1,math.ceil(n+reach))
        patch = pixels[top:bottom+1,lo:hi+1]
        matched = np.zeros(patch.shape[:2],dtype=bool)
        for centre,tol in colours.values():
            matched |= np.linalg.norm(patch-centre,axis=2)<=tol+.01
        lengths = [[b-a+1 for a,b,on in runs(matched[:,i]) if on] for i in range(patch.shape[1])]
        if sum(len(v)>=2 and max(v)<=max(2,w*.3) for v in lengths)>=len(lengths)*.75:
            return "parallel_thin_strokes_possible_glazing_or_frame"
        if sum(bool(v) and max(v)>w*1.3 for v in lengths)>=len(lengths)*.75:
            return "broad_cross_section_possible_junction_or_shadow"
        return "unmatched_or_unstable_core_check_gap_shadow_or_image_noise"
    return state


def crop(image, n, lo, hi, w, path, change=None):
    # Native colours and original pixel grid. A vertical wall is transposed for reading top to bottom.
    pad = max(6,math.ceil(w))
    l,r = max(0,math.floor(lo)),min(image.width,math.ceil(hi)+1)
    t,b = max(0,math.floor(n-pad)),min(image.height,math.ceil(n+pad)+1)
    tile = image.crop((l,t,r,b))
    zoom = max(1,min(5,640//max(1,tile.width)))
    tile = tile.resize((tile.width*zoom,tile.height*zoom),Image.Resampling.NEAREST)
    if change:
        # Keep all annotations outside the original image pixels.
        sheet = Image.new("RGB",(max(300,tile.width),tile.height+66),"white")
        sheet.paste(tile)
        draw = ImageDraw.Draw(sheet)
        x = lambda t: (t-l+.5)*zoom
        y = tile.height+3
        if change["source_band_px"]:
            low,high = change["source_band_px"]
            draw.rectangle((x(low),y,x(high),y+5),fill="#afcaed")
            draw.line((x(change["source_mid_px"]),y,x(change["source_mid_px"]),y+11),fill="#143e7c")
        for rail in change.get("cad_contacts_by_core_rail_px",[]):
            for v in rail:
                draw.line((x(v),y+13,x(v),y+21),fill="#b64112",width=2)
        draw.text((2,y+24),f"Original {l}..{r-1} px   Blue: source band",fill="black")
        draw.text((2,y+40),f"Orange: CAD contacts   {change['status']}",fill="black")
        tile = sheet
    tile.save(path)
    return dict(file=path.name,t_start_px=l,normal_start_px=t,zoom=zoom)


def assess_axis(axis, image, colours, geoms, uncertain, issues, projection, reg_error, read, normal_read_band, stage, index):
    ti = int(axis["orientation"]=="V")
    if ti:
        image = image.transpose(Image.Transpose.TRANSPOSE)
        geoms = {key:affine_transform(v,SWAP) for key,v in geoms.items()}
        uncertain = [(ref,affine_transform(v,SWAP)) for ref,v in uncertain]
    ranges = [(projection.min_mm_per_px(i), projection.max_mm_per_px(i)) for i in (ti,1-ti)]
    scales = [lo if math.isclose(lo,hi,rel_tol=1e-12) else None for lo,hi in ranges]
    band = read+reg_error[ti]
    normal_band = normal_read_band+reg_error[1-ti]
    total = unary_union(list(geoms.values()))
    pixels = np.asarray(image)
    pieces,widths = stable_runs(axis,pixels,colours)
    stable = [i for i,p in enumerate(pieces) if p["label"] in colours]
    transitions = []
    for i in stable:
        inspect_run(pieces[i],widths,axis,geoms,total,uncertain,issues,band,normal_band,projection)
    for i,j in zip(stable,stable[1:]):
        left,right = pieces[i],pieces[j]
        if left["label"]==right["label"]:
            continue
        change = transition(left,right,pieces[i+1:j],axis,geoms,total,uncertain,issues,pixels,colours,band,normal_band,projection)
        transitions.append(change)
        if j>i+1 and change.get("reason")!="intervening_source_gap_or_artifact":
            for part in pieces[i+1:j]:
                part["source_blend_between_stable_flanks"] = True
        contacts_px = [v for rail in change.get("cad_contacts_by_core_rail_px",[]) for v in rail]
        low,high = change["source_band_px"] or change["flank_separation_span_px"]
        first = min(low,left["shoulders"][1]["stations_px"][0],*contacts_px)-3
        last = max(high,right["shoulders"][0]["stations_px"][1],*contacts_px)+3
        change["original_colour_crop"] = crop(image,axis["normal"],first,last,max(left["width_px"],right["width_px"]),
            stage/f"axis-{index:03d}-change-{len(transitions):03d}.png",change)
        change["sufficient_tone_evidence"] = change["status"]!="review"
        if change["status"] in ("shifted_change","missing_change"):
            change["interval_edit_hint"] = dict(operation="review_material_split_on_existing_collinear_wall",
                axis_id=axis["id"],along="y" if ti else "x",station_px=change["source_mid_px"],
                accepted_station_band_px=change["source_band_px"],lower_source_span_px=left["span_px"],
                higher_source_span_px=right["span_px"],lower_station_role=left["label"],
                higher_station_role=right["label"],scope="existing_wall_only_keep_axis_width_host_openings_and_dimensions")
    raw = crop(image,axis["normal"],axis["start"],axis["end"],max(widths,default=0),stage/f"axis-{index:03d}.png")
    for p in pieces:
        if p["label"] not in colours:
            if p.get("source_blend_between_stable_flanks"):
                p.update(status="source_blur_band",reason="measured_mixture_between_stable_flanks")
            else:
                w = float(np.median(widths[widths>0])) if np.any(widths>0) else 5.
                p.update(status="review",reason=unresolved_reading(p,pixels,axis["normal"],w,colours))
        p["sufficient_tone_evidence"] = p["status"] in ("within_band","wrong_tone_on_reported_intervals")
    return dict(id=axis["id"],orientation=axis["orientation"],normal_px=axis["normal"],
                scale_along_mm_per_px=scales[0],scale_normal_mm_per_px=scales[1],
                along_mm_per_px_range=ranges[0],normal_mm_per_px_range=ranges[1],
                effective_along_band_px=band,effective_normal_band_px=normal_band,
                original_colour_strip=raw,runs=pieces,changes=transitions)


def run(source, reading, dxf, mapping, out, read_band, normal_band):
    if not all(math.isfinite(v) and v>=0 for v in (read_band,normal_band)):
        raise ValueError("Supply finite nonnegative source reading bands")
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise ValueError("Use a new or empty output directory")
    source_hash = digest(source)
    spec = json.loads(reading.read_text(encoding="utf-8"))
    with Image.open(source) as original:
        image = ImageOps.exif_transpose(original).convert("RGB")
    axes,samples = source_axes(image,spec,source_hash)
    if not axes:
        raise ValueError("No source axes")
    colours = {s["material"]:(np.array(s["rgb"],dtype=float),s["tolerance_rgb"]) for s in samples}
    if set(colours)!={"bearing","partition"}:
        raise ValueError("Register one source sample group for each printed colour role")
    reg = json.loads(mapping.read_text(encoding="utf-8"))
    projection,errors,residuals = registration(reg,source_hash)
    geoms,uncertain,issues,inventory = cad_material(ezdxf.readfile(dxf),reg,projection)
    out.parent.mkdir(parents=True,exist_ok=True)
    with TemporaryDirectory(dir=out.parent) as tmp:
        stage = Path(tmp)/"result"
        stage.mkdir()
        rows = [assess_axis(axis,image,colours,geoms,uncertain,issues,projection,errors,read_band,normal_band,stage,i)
                for i,axis in enumerate(axes,1)]
        states = [p["status"] for r in rows for p in r["runs"]+r["changes"]]
        status = ("different" if any(s in ("wrong_tone_on_reported_intervals","shifted_change","missing_change") for s in states)
                  else "review" if issues or "review" in states or not states else "within_band")
        report = dict(scope="source-marked wall colour intervals only",status=status,
            sha256={"image":source_hash,"reading":digest(reading),"saved_dxf":digest(dxf),"registration":digest(mapping)},
            source_read_band_px=read_band,normal_read_band_px=normal_band,source_colours=samples,
            registration_holdout_residuals_mm=residuals,decoded_layers=inventory,decode_issues=issues,axes=rows)
        (stage/"wall-tone-stations.json").write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+"\n",
                                                    encoding="utf-8")
        stage.replace(out)
    return status


if __name__=="__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("image","guides_or_report","saved_dxf","registration","out"):
        parser.add_argument(name,type=Path)
    parser.add_argument("--read-band-px",type=float,required=True)
    parser.add_argument("--normal-band-px",type=float)
    args = parser.parse_args()
    try:
        status = run(args.image,args.guides_or_report,args.saved_dxf,args.registration,args.out,args.read_band_px,
                     args.read_band_px if args.normal_band_px is None else args.normal_band_px)
    except (KeyError,ValueError,IndexError,TypeError,OSError,np.linalg.LinAlgError,ezdxf.DXFError) as error:
        parser.exit(2,f"wall-tone-stations: {error}\n")
    print(status, args.out/"wall-tone-stations.json")
    raise SystemExit({"within_band":0,"different":1,"review":2}[status])
