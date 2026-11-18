# V1.6 外墙、连续墙面、窗框和相邻构件

本页适用于正交、毫米制户型。建图时先约束同一物理面，再扣洞、放窗和家具。复核时从本版 DWG 的原生回读重新提取墙面、框线和家具轮廓。模型坐标比较容差为 0.1 mm。原图读数的不确定度单独记录。

## 墙体和图证

在 `scene.wall_geometry_v16.exterior_width_mm` 填本户普通外墙统一厚度，选 200 或 240。每段墙在原有 `width_mm`、`thickness_evidence` 之外，增加 `boundary_role` 与 `boundary_evidence`。角色可为 `exterior`、`interior` 或 `detail`。深浅填充只表示该图的材料表达，不能用于推断内外墙。

`boundary_evidence` 和下列局部构造证据使用同一原图的 SHA-256、可定位的 `locator`、原图像素范围 `bbox_px=[左,上,右,下]`。局部范围须与该构件的位置相符。全户选档须在各段实际像素截面支持的候选值内。若明确标数或唯一候选与全户选档冲突，保留真实读数，复核会报告冲突。改变已登记宽度时，沿用原有 `thickness_reference` 或已绑定的控制面。

柱头、局部墙垛、井壁、明确异厚或真实错台可单独登记。把 `geometry_exception` 放在对应墙上，增加 `kind` 为 `column`、`wall_pier`、`shaft`、`documented_thickness` 或 `intentional_step`。给柱头标明 `element_kind="column"`。明确异厚可用对应印刷标数、建筑资料，或有可靠定标的同图原始截面。原始截面须穿过本段，两侧墙面落在证据裁片内，计入读差后只支持一个厚度档。以假定剪力墙厚定标的图仍保留估算。图证须覆盖获准例外的整段，长墙上的局部柱头应单列。审核会分别列出缺证、未分类和无独立实墙截面的墙段。

## 控制与分材交点

已有物理控制可同时约束多段墙。下面的字段位于 `wall_geometry_v16` 中。示例中的 `HASH` 代表本图完整摘要。

```json
{
  "exterior_width_mm": 200,
  "continuous_faces": [{
    "id":"南外皮", "control":"南外墙面",
    "members":[{"wall":"南1","at":"min_face"},{"wall":"南2","at":"min_face"}],
    "source_sha256":"HASH","locator":"南立面同一外皮","bbox_px":[100,80,420,130]
  }],
  "material_joints": [{
    "id":"南1-南2分界", "anchor_wall":"南1","shared_face":"min_face",
    "members":[{"wall":"南1","end":"b"},{"wall":"南2","end":"a"}],
    "source_sha256":"HASH","locator":"南墙深浅填充分界","bbox_px":[195,80,220,130]
  }]
}
```

`continuous_faces.control` 必须指向已有 `dimension_grid.controls` 中的墙面、柱面或实际墙中线。`at` 可为 `min_face`、`max_face` 或 `center`，方向以世界坐标为准。独立营销刻线保留 `datum` 属性。已证实的同面关系进入现有尺寸约束求解器；模型证据与已固定控制相冲突时，建图报错。

同向分材端按指定墙端合并，端部使用同一切面。直角转折指定两个真实墙端。T 形节点以一个 `end="through"` 指明连续墙，另一个真实支墙端接入。直角节点由实际两条墙线求交。同一墙端不能重复声明。变更后的实墙轮廓和原生填充并集均须重测，分材填充不得有实面积重叠。连续同轴墙体发生重复占长时也会报告。

回读检查枚举普通墙身可采样的所有净段，各取三处实际截面。整户实体另做完整墙轮廓双向差集，查三点之间的局部缺块、凸出和分材重叠。它检查 L、T、十字节点。未注明的相近同向墙端会提示核实。短错台检查沿实际墙并集扫描 25 mm 及以下的直角回折。有证据的真实短折在 `step_details` 逐处登记 `kind`、同图证据、原图两端 `ends_px` 和 `edge_uncertainty_px`（0–2 像素）。像素误差超过两像素时保留待核。25 mm 是候选扫描范围，较大偏差仍受全墙轮廓、控制面与独立叠图检查。

## 窗框、移屏和家具

普通直窗从同一洞口扣墙。沿有向洞端 a→b，在 a 外侧和 b 外侧分别取墙截面。两端实际墙面相符时，四条平行线采用该对实墙面的有符号偏距，两个端封同时贴齐。回读分别核验四条独立轨线的跨度、两侧位置、两端封线和实际墙厚。两端墙厚或偏心不同、无墙端或中间有真柱时，应核对该处开口和柱边，必要时据图分段。

退进窗、幕墙、独立框、折角框在开口的 `geometry_exception` 登记 `kind` 为 `recessed_frame`、`curtain_wall`、`independent_frame`、`corner_frame` 之一。另给原图裁切证据与 `face_offsets_mm=[低,高]`。偏距以洞口 a→b 的左法向为正。特殊框仍查实际端线、支承和穿墙。框接框使用 `jamb_support` 的 `frame` 和另一构件的 `frame_neighbors.a` 或 `.b`。只有一个实际匹配邻框时可自动识别。独立端使用 `free_end`，在 `end_evidence.a` 或 `.b` 登记相应原图位置。

隔墙接连续窗时，在靠近墙身两侧的两个截面量实际墙端到近侧窗框的净距。自动搜索范围为 120 mm 或半墙厚，两者取大。对已登记的关系不受搜索距离限制。正常贴接为 0。按图留缝时在 `frame_wall_ends` 增加 `{"wall":"隔墙","end":"b","window":"连续外窗","gap_mm":20,...同图证据}`。实际穿入框内、与明确图证不符的净距会报告失败；自动发现且缺依据的间隙待核。

保留实际柜体、墙柱以及移屏轨线、端线的句柄。复验由同版接边表重新量取家具轮廓。覆盖全部登记家具、邻窗与推拉构件，无需手列本轮改件。漏登记的实际门窗和家具图层实体、读不出的物体和未分类邻边均留待核。可投影重叠的家具有依据时沿用原接边表。推拉框线穿入柜体或墙体会单独列出。

## 原生复量接口

```python
from wall_geometry_v16 import audit

report = audit(readback_doc, ledger, edge_job, source_sha256=actual_image_sha256)
```

`readback_doc` 是该版 DWG 原生回读 DXF 加载出的 ezdxf 文档。`ledger` 为编译器生成的同版台账，含 `wall_geometry_v16`、`geometry_layers_v16`、`geometry_registration_v16`、`geometry_objects_v16`、`geometry_projections_v16`。沿用原有墙、洞和尺寸基准记录。`edge_job` 为同版 `edge-relations.json`；没有家具时传含空 `items` 和空 `relations` 的真实清单。

`checks` 含检查 ID、状态与测量明细。`failures`、`pending` 是对应的 ID 列表。`status` 优先报告 `fail`，其次为 `pending`，其余为 `pass`。调用方核对 DWG、回读 DXF 与原图哈希，最终审查时重新运行。脚本核对图证的版本、格式、位置和确实存在的模型几何；原图内容、遗漏图元及例外语义仍由独立对图确认。


V1.6.1 对已登记图证的转角弧窗，从原生 ARC、ELLIPSE 或块内的曲线读取各轨的真实两端，分别测端封至实墙、端封或轨端至相邻窗。两端方向可以不同，也可共用邻窗的一条端封。轨线不完整或多段不能唯一对应时列待核；实测端封悬空或曲框穿墙列失败。

弧窗净洞按内外两根原生轨线之间的面积复量。轨线不足、交叉或不能配成同一洞口时列待核。两个独立窗各画一条重合端封时列为编辑错误。家具侵入检查覆盖整段弧线。弧段的墙、窗类别另外核对原图图例、局部墙芯与相邻构件；模型里画得连贯，仍要核对该构件是否画对。

V1.6.2 支持有原图依据的一种实体转角。直墙末端经四分之一椭圆弯接正交直窗，两端相切，墙沿法线等厚。先从原图分别定位两处切点，再在 scene 中填写 `curved_wall_returns`。每项含唯一 `id`、本图 `source_sha256`、`locator`、覆盖整个弯的 `bbox_px`、`wall_id`、`wall_end`、`window_id`、`window_end`。两端取 a 或 b。窗在此端登记 wall 支承。程序继承相接墙的模型厚度和图层，生成闭合轮廓和关联填充，从保存的原生实体复量。内侧折返、证据跨不到两端或与现有实墙重叠时停止该构造，保留原图依据重新判读。其他形状的弯仍需按实际端面和轮廓核对。
