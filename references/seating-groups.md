# 用已保存的 CAD 检查椅、凳、几的同组使用关系

适用于近距离放置的座具和几。先按原图登记每件独立物体，指出能识别它的背、座、脚或桌沿。用最终复开的毫米 DXF 和同一版登记表运行。脚本只输出 JSON，不编辑 CAD。检查结果限于登记组、登记的邻物及动作。整间房的动线、人的舒适程度、遮挡高度和实际件数仍须根据本图判断。

```sh
python scripts/check_seating_groups.py 已保存.dxf --inspect 91 93
python scripts/check_seating_groups.py 已保存.dxf --groups 座具组.json > 同组检查.json
```

`--inspect` 输出每个实际 INSERT 的图线位置、端点和块内路径。单层路径如 `4A`。嵌套路径如 `4B/38`，前者为嵌套 INSERT，后者为定义内的实体。完整图线包含遮罩下面的家具。逐层变换保留块基点、镜像、非等比缩放、旋转及其组合。曲线先变换至世界坐标，再以 0.25 mm 精度离散。接近该精度的接触须人工放大检查，DXF 重新生成后再次核对句柄。

## 作者登记

输入为组的数组。下例展示字段，句柄、部位路径、方向、区域和动作数值均须换成本图实测。每个未重复列出的脚或构件仍要逐一登记。

```json
[{
  "id": "窗边座具",
  "area_wcs": [[0,0],[2400,0],[2400,1800],[0,1800]],
  "area_basis": "按本图内墙面及需保留的通道界线勾画可用范围",
  "members": [
    {"handle":"91", "kind":"chair", "basis":"左座露出完整弧背及前脚", "movable":true,
     "forward_local":[0,-1],
     "parts":[
       {"role":"seat", "plane":"座面层", "paths":["4B/38"], "basis":"座包外缘"},
       {"role":"frame", "plane":"扶手层", "paths":["4B/39"], "basis":"外侧扶手弧"},
       {"role":"foot", "plane":"floor", "paths":["4B/41"], "basis":"落地脚的闭环"}
     ],
     "pull":{"mm":300, "max_end_tuck_mm2":0, "basis":"背后可沿退椅侧平移，要求座面完全露出"}},
    {"handle":"93", "kind":"table", "basis":"同组高几，桌板及脚可辨",
     "parts":[
       {"role":"top", "plane":"几面层", "paths":["52"], "basis":"几板实边"},
       {"role":"foot", "plane":"floor", "paths":["53"], "basis":"台柱触地闭环"}
     ]}
  ],
  "under_table":[{"seat":"91","table":"93","basis":"本图确认前座可推入几板下，几脚另查"}],
  "repair":{"basis":"本图同一窗边区允许小幅调整椅位", "step_mm":25, "max_shift_mm":250,
            "directions_wcs":[[1,0],[-1,0]], "angles_deg":[-15,15]}
}]
```

- `kind` 可取 `chair`、`stool`、`table`、`obstacle`。同区墙垛、柜、床、门扇所需区域可以从可用区域中排除，或以实际已登记的障碍物 INSERT 加入本组。障碍物用一个或多个闭合 `solid` 部位表示。含门扇的房间仍须另查实际开启区。
- `plane` 表示本图确认可以相撞的同高层。脚统一为 `floor`；贯穿各层的立柱、实心基座或障碍物可用 `solid` 和 `*`。不同椅子的同层构件须使用同一名称。单柱、底盘或悬臂底座可登记实际闭合实体或落地构件，另加 `base_basis` 说明构造。无法判断接地部位时保留待核。
- 一个 `seat`、`top`、`solid` 或闭环 `foot` 须用所选路径构成一个可辨闭边。多个脚各自登记。某条斜腿的末端才是落地点时，可用 `foot` 加 `"at":"start"` 或 `"at":"end"`。剩余杆体如有碰撞意义，另登记为 `frame` 并说明高度。同一线可同时标出杆体及脚端。可辨的图案才列 `detail`。缺边、未知图线、剪裁、阵列、未识别姿态或高度须复核。
- `forward_local` 是顶层块坐标内由背指向入座侧的向量。目录中已核对的标准块可沿用其前向，其它有靠背的椅子须登记。圆凳可无前向。`pull` 按登记距离沿真实椅背方向拉出。另有依据的方向可给 `pull.wcs`。终点需要露出多少座面，用 `max_end_tuck_mm2` 记录允许遮挡面积。没有 `pull` 的物件只查原位。
- 脚点凸包只说明可能的支承范围。脚点在其中时列出侵入者、支承者、脚序号及进入深度。有本图证据表明该范围内不得插入另一只脚时，才给被侵入物件设置 `exclusive_support_basis`。例如，原图及已识别构造确认座具底架在该区域独占。敞开四脚下存在可用空间时，保留待核。
- 原位和退椅均列出实际图线交点或共线长度、座面与其它实体的交叠面积。脚本用实际部位沿整段行程扫掠；座面位于高桌板下按指定物件对逐项另列。登记的默认遮挡只覆盖座面及落地脚；扶手等另有净高依据时，把该椅的准确部位路径列入对应关系的 `extra_paths`。桌脚与椅脚、贯穿座面的立柱、组内第三件及终点座面仍单独核查。世界轴外框和实际图线凸包只提供候选面积。两者相交不产生“需调整”结论。
- 有已证实的冲突时，在同一区域按指定直线方向、步长、上限和转角试移可动件。每个候选均复查整组原位及全部已登记退椅动作。排名量为图上线点的最大位移，旋转绕原 INSERT 基点。只报告登记的离散候选中位移最小的可行解。未找到时回看独立座背、座面和脚，核实是否多计或需要联动调整。CAD 原件不受影响。应用方案并保存后须重新读取 DXF，实际查看该处及周边。

## 可复跑的合成检验

在技能目录运行下段 Python。它生成带真实方向、开线、弧、闭环、独立几脚和旋转缩放嵌套的图块，临时保存后重新读取。测试物件的数值只为构造已知关系。

```python
import copy
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import ezdxf

sys.path.insert(0, "scripts")
from check_seating_groups import check, read_lines, CLEAR, HIT, REVIEW
from place_plan_families import place_family

doc = ezdxf.new()
doc.units = 4
m = doc.modelspace()
profiles = {}

def shape(name, kind, seat, feet, arms=False):
    b = doc.blocks.new(name)
    parts = []
    def record(e, role, level):
        parts.append(dict(role=role, plane=level, paths=[e.dxf.handle], basis="合成图已知部位"))
    if isinstance(seat, int):
        face = b.add_circle((0,0), seat)
    else:
        x, y = seat
        face = b.add_lwpolyline([(-x,-y),(x,-y),(x,y),(-x,y)],close=True)
    record(face, "top" if kind == "table" else "seat", "top" if kind == "table" else "seat")
    if arms:
        record(b.add_line((18,0),(60,0)), "frame", "rail")
        record(b.add_arc((0,0), 18, 85, 170), "frame", "rail")
    for xy in feet:
        record(b.add_circle(xy, 1.5), "foot", "floor")
    profiles[name] = dict(kind=kind, parts=parts, basis="合成图的独立物件")
    if kind == "chair":
        profiles[name]["forward_local"] = [0,-1]
    return b

four = [(-50,-50),(50,-50),(50,50),(-50,50)]
shape("Four", "chair", (12,12), four)
arm = shape("Arm", "chair", (12,12), four, True)
shape("Low", "stool", 8, [(0,0)])
shape("Side", "table", (80,50), [(65,42)])
ob = doc.blocks.new("Obstacle")
edge = ob.add_lwpolyline([(-4,-4),(4,-4),(4,4),(-4,4)],close=True)
profiles["Obstacle"] = dict(kind="obstacle",basis="实体柱",
    parts=[dict(role="solid", plane="*", paths=[edge.dxf.handle],basis="实体闭边")])

def place(name, x, y, angle=0, **spec):
    ref = m.add_blockref(name,(x,y), dxfattribs=dict(rotation=angle))
    return dict(copy.deepcopy(profiles[name]), handle=ref.dxf.handle, **spec)

def grp(id, *members, **kw):
    return dict(id=id, members=list(members), **kw)

a = place("Arm",0,0)
b = place("Arm",48,-35,90)
corner = grp("框角", place("Low",0,0), place("Low",14,14))
chair = place("Four",0,0, exclusive_support_basis="本合成椅的底架独占区")
stool = place("Low",32,32,movable=True)
intrusion = grp("脚位",chair,stool,area_wcs=[[-90,-90],[150,-90],[150,110],[-90,110]],
    area_basis="合成房间",repair=dict(basis="沿该侧可移",directions_wcs=[[1,0]],step_mm=10,max_shift_mm=80))
pending = copy.deepcopy(intrusion)
del pending["members"][0]["exclusive_support_basis"]
pulled = place("Four",0,55,pull=dict(mm=100,basis="背后就坐",max_end_tuck_mm2=0))
table = place("Side",0,0)
allow = dict(seat=pulled["handle"],table=table["handle"],basis="合成几板净高允许")
dining = grp("桌下",pulled,table,under_table=[allow])
blocked = grp("行程",pulled,table,place("Obstacle",0,105),under_table=[allow])
samefoot = place("Low",65,42)
leg = grp("同位脚",samefoot,table,under_table=[dict(allow,seat=samefoot["handle"])])
blind = copy.deepcopy(corner)
blind["members"][0]["parts"] = []
family = place_family(m,"dining_woven",(3000,1200),facing=(1,0))
family_check = grp("目录朝向",dict(kind="chair",basis="仅查朝向",handle=family.dxf.handle),table)

frame_side = doc.blocks.new("Bent")
rails = frame_side.add_lwpolyline([(-40,30),(-40,-30),(40,-30),(40,30)])
seat_edge = frame_side.add_circle((0,-24),2)
floor_edge = frame_side.add_circle((0,-24),1)
profiles["Bent"] = dict(kind="chair",basis="凹形开口架",forward_local=[0,1],parts=[
    dict(role="frame",plane="rail",paths=[rails.dxf.handle],basis="开口架实边"),
    dict(role="seat",plane="seat",paths=[seat_edge.dxf.handle],basis="合成座面"),
    dict(role="foot",plane="floor",paths=[floor_edge.dxf.handle],basis="合成着地点")])
outer_seat = place("Bent",0,0)
inner_seat = place("Bent",0,0)
m.doc.entitydb[inner_seat["handle"]].dxf.xscale = .5
m.doc.entitydb[inner_seat["handle"]].dxf.yscale = .5
interleaved = grp("开口构件",outer_seat,inner_seat)
turn = grp("小幅转向",dict(a, movable=True),place("Obstacle",40,0),
    area_wcs=[[-110,-110],[110,-110],[110,110],[-110,110]],area_basis="合成房间",
    repair=dict(basis="靠背可轻微转向",step_mm=1,max_shift_mm=1,angles_deg=[20]))
unknown_pull = copy.deepcopy(dining)
del unknown_pull["members"][0]["forward_local"]
outer = doc.blocks.new("Composite", base_point=(3,4))
inner_ref = outer.add_blockref("Arm",(6,7),dxfattribs=dict(rotation=33,xscale=1.1,yscale=.8))
outer_ref = m.add_blockref("Composite",(120,30),dxfattribs=dict(rotation=26,xscale=-1.2,yscale=.7))
rail = next(e for e in arm if e.dxftype() == "LINE")
key = inner_ref.dxf.handle + "/" + rail.dxf.handle
matrix = inner_ref.matrix44() @ outer_ref.matrix44()
expected = matrix.transform((18,0,0))

with TemporaryDirectory(dir=".") as folder:
    path = Path(folder)/"synthetic.dxf"
    doc.saveas(path)
    saved = ezdxf.readfile(path)
    lines, notes = read_lines(saved.entitydb[outer_ref.dxf.handle])
    import json, subprocess
    registration = Path(folder)/"groups.json"
    registration.write_text(json.dumps([intrusion]),encoding="utf-8")
    cmd = [sys.executable,"scripts/check_seating_groups.py",str(path)]
    cli = json.loads(subprocess.check_output(cmd+["--groups",str(registration)]))
    inspected = json.loads(subprocess.check_output(cmd+["--inspect",outer_ref.dxf.handle]))
    assert key in inspected[outer_ref.dxf.handle]["paths"]
    assert len(cli["sha256"]) == 64 and cli["groups"][0]["status"] == HIT
    assert not notes and len(lines) == len(profiles["Arm"]["parts"])
    assert max(abs(u-v) for u,v in zip(lines[key].coords[0],expected.xy)) < 1e-5
    results = check(saved, [grp("扶手",a,b),corner,intrusion,pending,dining,blocked,leg,blind,family_check,turn,unknown_pull,interleaved])

arm_row = results[0]["measurements"][0]
assert arm_row["stroke_contacts"] and any(i["roles"] == ["frame","frame"] for i in arm_row["hard"] if "roles" in i)
corner_row = results[1]["measurements"][0]
assert corner_row["axis_box_overlap_mm2"] > 0 and corner_row["status"] == CLEAR
assert results[2]["status"] == HIT and results[2]["possible_revision"]["translation_wcs_mm"] == [20,0]
assert results[3]["status"] == REVIEW
assert results[4]["status"] == CLEAR and results[4]["measurements"][0]["declared_projections"]
pull = results[4]["measurements"][-1]
assert pull["start_tuck_mm2"] > 0 and pull["end_tuck_mm2"] == 0
assert any(r["phase"] == "退椅" and r["status"] == HIT for r in results[5]["measurements"])
assert results[6]["status"] == HIT and results[7]["status"] == REVIEW
assert results[8]["items"][0]["forward_wcs"][0] > .999
assert results[9]["possible_revision"]["rotation_deg"] == 20
assert results[10]["status"] == REVIEW
concave = results[11]["measurements"][0]
assert concave["candidate_hull_overlap_mm2"] > 0 and not concave["stroke_contacts"] and concave["status"] == CLEAR
from shapely.geometry import LineString
from check_seating_groups import line_cross
assert not line_cross(LineString([(-10,8),(0,0),(10,8)]),LineString([(-20,0),(20,0)]))
print("合成检验通过。原位、退椅、移位、转向、嵌套变换、相切及两种命令入口均已核对。")
```
