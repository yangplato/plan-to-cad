# 把床头或直柜放在指定的实墙面

先从原图认出物件、房间侧、靠墙段和大致位置。床头与柜背以实物边为准。直柜若占满两个墙角或墙角至洞口的一整段，可按该段裸露墙面定宽。仅占墙面一部分的柜体，按图给宽度和进深。

## 从原图登记到已放置的 DXF

`scripts/place_hosted_family.py` 使用 `build_observed_structure.py` 的原图登记。每件家具给出如下字段。

| 字段 | 含义 |
|---|---|
| `id`、`host` | 本件标识、原图登记中的直墙 ID。 |
| `family` | `bed_single`、`bed_double` 或 `cabinet`。柜体是单独可编辑的直柜外形。 |
| `side` | 房间在墙的哪一侧。使用 CAD 世界坐标的 `+x`、`-x`、`+y` 或 `-y`，它也是从背边向床尾或柜前的方向。原图向下通常是 CAD 的 `-y`。 |
| `near_px` 或 `near_mm` | 二选一。给原图像素点或 CAD 毫米点。沿墙坐标确定落位，另一坐标按指定墙的实面求得。请选在对应墙段内；落在洞口会返回该墙的可用区间。 |
| `mode` | 缺省的 `centered` 将物件的宽度中心对准给定位置。`full_span` 仅用于整段柜，两个端点按所选墙段上同一段连续裸露实墙确定。 |
| `depth_mm`、`width_mm` | 直柜要有进深。居中直柜还要有宽度。整段柜自动取宽。床按目录放置；给一个尺寸会等比缩放，给两个会分轴缩放。 |

墙段范围由挂接墙的两个轴端、真实墙材并集的同侧边界及登记洞口共同限定。窗、门洞和凸出的相交墙会中断该面。若整柜确实横跨多个独立登记的共线墙段，应按真实分段核实柜段或另选实际长背复核。

以下是**合成输入的字段示例**。标识、坐标和尺寸均须由本案原图确定。

```json
[
  {"id":"床1","family":"bed_double","host":"房间北墙","side":"-y",
   "near_px":[410,25],"width_mm":1600,"depth_mm":2000},
  {"id":"柜1","family":"cabinet","host":"房间西墙","side":"+x",
   "near_mm":[90,2750],"mode":"full_span","depth_mm":550}
]
```

```sh
python scripts/place_hosted_family.py 原图登记.json 家具.json --write-new 已放置.dxf
python scripts/place_hosted_family.py 原图登记.json 家具.json \
  --drawing 已建结构.dxf --ledger 毫米结构台账.json --write-new 已放置.dxf
```

省略 `--write-new` 可先试放。提供输出时，程序保存临时 DXF，复开后读取实墙、家具及门扇图元。各件均通过才生成目标文件。原有图层映射来自原图登记的 `layers`。同批家具互相检查占地。已有图中的其他家具可在 Python 函数的 `obstacles` 中传入其实际轮廓。

## 从现有绘图脚本调用

```python
from build_observed_structure import create_structure
from place_hosted_family import place_hosted_family, live_body, check_hosted_family

doc, ledger = create_structure(scene)
objects = {}
for item in items:
    ref, measure = place_hosted_family(
        doc, ledger, item, source_to_mm=scene["source_to_mm"],
        layers=scene.get("layers"), obstacles=objects)
    objects[item["id"]] = live_body(ref)
    print(item["id"], ref.dxf.handle, measure["actual_rear_mm"],
          measure["wall_contact_mm"], measure["normal_gap_mm"])
```

每件返回模型空间的 INSERT 和实量。定位或碰撞失败会报出原因并撤销该件。报告中的 `rear_segments_mm` 来自实体外边界。`actual_rear_mm` 只计外边界上真正平直的背边，例如扣除圆角后的床头。`wall_contact_mm` 是同面且覆实墙的实际长度。`normal_gap_mm` 为正表示背后有缝，为负表示进入墙面。`body_in_wall_mm2` 检查整个实物，包括两侧和床尾。整段柜还要同时贴合所选墙段的两个沿墙端点。保存了原始句柄的同一 DXF 可用 `check_hosted_family(doc, ledger, item, ref, source_to_mm=...)` 复量当前实际位置。

## 另查现有柜体是否挡门

从当前图的闭合硬轮廓取占地。工具已放置的直柜可用 `live_body(ref)`。其他柜体的层级句柄和轮廓选法见[指定实物轮廓](wall-contact-fit.md)。以下函数可一次检查多件。

```python
from align_furniture_to_wall import footprints
from place_hosted_family import screen_door_swings

cabinets = {"玄关柜": footprints(doc, cabinet_outline_specs)}
rows = screen_door_swings(doc, ledger, cabinets)
conflicts = [row for row in rows if row["collision"]]
```

程序按台账句柄读取模型空间中每扇平开门的实际 ARC 及开启扇外缘。它将柜体占地裁到开启角域，再比较铰点到裁后实物的最短距离与实测弧半径。正的 `sector_radial_overlap_mm` 表示有实体进入该扇区。`open_leaf_overlap_mm2` 表示柜体与实际开启扇实物重叠的面积。相切按零侵入处理。毫米和平方毫米的舍入界限分别为 0.00001 和 0.0001。门扇与柜体有高度错开的情况需要另核。该项检查覆盖已登记平开扇的名义开启扇区及开启位置的厚度，门把手、使用通道和抽屉拉出所需净距须按实际构造另查。
