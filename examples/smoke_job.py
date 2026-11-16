"""Generate a synthetic two-room test job using only the packaged example and Pillow."""
import argparse
import hashlib
import json
from pathlib import Path

from PIL import Image, ImageDraw


def make_job(out):
    if out.exists() and any(out.iterdir()):
        raise ValueError("Choose an empty job directory")
    out.mkdir(parents=True, exist_ok=True)
    scene = json.loads(Path(__file__).with_name("dimension-grid.json").read_text(encoding="utf-8"))
    scene["drafting_version"] = "1.3.0"
    for wall in scene["walls"]:
        if wall["id"] == "column":
            wall["element_kind"] = "column"
            continue
        wall["thickness_reference"] = "center"
    # The calibration stress example uses intentionally different local image scales.
    # For this drawing, use one scale throughout and print the matching dimension chains.
    scale = 20
    stations = scene["dimension_grid"]["controls"]
    for chain in scene["dimension_chains"]:
        ticks = [stations[key]["px"] for key in chain["controls"]]
        chain["values_mm"] = [scale*abs(b-a) for a, b in zip(ticks, ticks[1:])]
        if chain["id"] == "bottom reversed":
            chain["start_mm"] = 5600
    image = Image.new("RGB", (360, 270), "white")
    draw = ImageDraw.Draw(image)
    for chain in scene["dimension_chains"]:
        horizontal = chain["axis"] == "x"
        line = max(9,chain["line_px"])
        ticks = [stations[key]["px"] for key in chain["controls"]]
        def point(station, offset):
            return (station,offset) if horizontal else (offset,station)
        draw.line([point(ticks[0],line),point(ticks[-1],line)],fill="#555555")
        for at in ticks:
            draw.line([point(at,line-3),point(at,line+3)],fill="#555555")
        for a,b,mm in zip(ticks,ticks[1:],chain["values_mm"]):
            draw.text(point((a+b)/2-7,line+2),str(mm),fill="#333333")
    for wall in scene["walls"]:
        draw.line([tuple(wall["a"]), tuple(wall["b"])], width=round(wall["width_mm"]/scale),
                  fill="#444444" if wall["material"]=="bearing" else "#aaaaaa")
    for op in scene["openings"]:
        if op["id"] == "D-room":
            op["reference_block"] = {"block": "OP02_单扇平开门800"}
        points = [tuple(op["a"]), tuple(op["b"])]
        draw.line(points, fill="white", width=10)
        draw.line(points, fill="#557788", width=1)
    objects = [
        {"id":"table","type":"library","role":"movable","function":"movable table",
         "library_block":"LV02_双边框长茶几","library_front_vector":[0,-1],
         "back_px":[101,80],"toward_px":[101,94],"width_mm":760,"depth_mm":560,
         "freestanding_basis":"free on four sides"},
        {"id":"cabinet","type":"hosted","family":"cabinet","role":"fixed","host":"east",
         "side":"-x","near_px":[295,164],"width_mm":700,"depth_mm":400},
    ]
    draw.rectangle((82,80,120,108),outline="#a4815c")
    draw.rectangle((275,146.5,295,181.5),outline="#746858")
    source = out/"source.png"
    image.save(source)
    reading = {"source_sha256":hashlib.sha256(source.read_bytes()).hexdigest(),
               "marks":[],"regions":[{"id":"floorplan","box":[0,0,345,252]}]}
    scene["source_sha256"] = reading["source_sha256"]
    for wall in scene['walls']:
        if wall.get('element_kind')=='column':continue
        axis=0 if wall['a'][0]==wall['b'][0] else 1
        center=[(a+b)/2 for a,b in zip(wall['a'],wall['b'])]
        pixels=round(wall['width_mm']/scale)
        if pixels%2==0:center[axis]+=.5  # Pillow's even-width stroke pixel edges.
        first,second=list(center),list(center)
        first[axis]-=pixels/2;second[axis]+=pixels/2
        wall['thickness_evidence']=dict(kind='calibrated_pixels',face_points_px=[first,second],
            edge_uncertainty_px=.1,source_sha256=reading['source_sha256'],
            locator='synthetic wall stroke / '+wall['id'],
            calibration_basis='synthetic printed dimension chains, 20mm per pixel on both axes')
    # This is a candidate smoke job. No independent source-review pass is fabricated.
    job = {"image":source.name, "scene":"scene.json","objects":"objects.json","own_reading":"reading.json"}
    for name, data in [("scene",scene),("objects",objects),("reading",reading),("job",job)]:
        (out / (name+".json")).write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding="utf-8")


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder",type=Path)
    make_job(parser.parse_args().folder)
