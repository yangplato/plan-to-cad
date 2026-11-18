# 原图图版、墙性与独立清单

## 先分图版

整张原图保留原字节和文件指纹。先读图版标题、图例和适用范围。指定一张用于落图的目标图版。将结构示意、产权小图和其他状态登记为参考图版。参考图版用于解释对象和证据来源。清点、配准、墙色测量与成品覆盖以目标图版的范围为准。

坐标统一使用按 EXIF 正常显示的整页像素。原点在左上，X 向右，Y 向下。图版与裁片使用左、上包含和右、下排除的四元组。图版内的局部像素位置加裁片左上角坐标，即为原图整页像素。现有墙芯导线的 `plan_box_px` 仍沿用其原有四边包含规则。

以下是一页合成示意的完整图版格式。示例数字和文字只说明字段，作业填写自己的原图 SHA-256。

```json
{
  "schema": "plan-source-panels/1",
  "source_sha256": "<原图 SHA-256>",
  "panels": [
    {
      "id": "decorated",
      "role": "target",
      "label": "装修示意",
      "box_px": [40, 160, 480, 400],
      "match_radius_px": 12
    },
    {
      "id": "structure",
      "role": "reference",
      "label": "结构示意",
      "box_px": [40, 20, 480, 145]
    },
    {
      "id": "key",
      "role": "legend",
      "label": "装修示意图例",
      "box_px": [485, 175, 590, 240]
    }
  ],
  "wall_legend": [
    {
      "id": "marked-partition",
      "panel": "key",
      "applies_to": "decorated",
      "box_px": [488, 181, 588, 200],
      "text": "斜线填充表示隔墙",
      "classification": "partition"
    }
  ]
}
```

墙材图例的分类取 `bearing`、`partition` 或 `other`。只有清楚说明目标图版墙体类别的图例才录入 `wall_legend`。例如产权面积颜色、铺地纹样和窗框阴影分别按各自含义记录。图例位于目标图版内时，`panel` 直接指该图版。页边图例须用 `applies_to` 指目标图版。源图无墙材图例时省略 `wall_legend`。

```sh
python scripts/source_evidence.py prepare 原图.jpg 图版.json 原图证据目录
```

默认生成各图版和目标图版局部的左右并列图。左边保留原 RGB，右边将同一裁片增强后放大。应用尺寸、墙色判据及指纹使用未改原图。裁片用于找到证据位置。放大后的像素位置用裁片索引换回整页坐标。

## 两位读图者各自清点

绘图者独立登记目标图版。独立审图者先封存 CAD 盲审，随后只凭原图登记自己的清单。先封存各自观察，再互相比对以及核对原生回读。两人可用相同模型，须有各自独立的读图会话。保留清单哈希与阶段记录。脚本发现同一清单、同一会话或逐条照录时保留待核。实际阶段是否隔离由作业记录证明。

以下是一份格式完整的合成示意。另一份清单用同一原图 SHA-256 和独立的 `session_id`。本地项目编号由各读图者自行选取。

```json
{
  "schema": "plan-source-inventory/1",
  "source_sha256": "<原图 SHA-256>",
  "reader": "原图读图者",
  "session_id": "<独立读图会话标识>",
  "source_only": true,
  "items": [
    {
      "id": "room-east",
      "panel": "decorated",
      "kind": "room",
      "subtype": "bedroom",
      "label": "东卧",
      "box_px": [240, 190, 410, 330],
      "points_px": [[320, 235]],
      "uncertainty_px": 2,
      "evidence": "卧室门接过道，登记卧室内未被墙或家具占用的位置。"
    },
    {
      "id": "wall-east",
      "panel": "decorated",
      "kind": "wall",
      "subtype": "solid",
      "box_px": [399, 205, 414, 300],
      "points_px": [[405, 210], [405, 289]],
      "section_px": [[402, 250], [408, 250]],
      "uncertainty_px": 1.5,
      "wall_nature": {
        "basis": "legend",
        "classification": "partition",
        "legend_id": "marked-partition"
      },
      "evidence": "本段两侧可辨同一墙体的边，斜线填充与本版图例一致。"
    },
    {
      "id": "door-east",
      "panel": "decorated",
      "kind": "door",
      "subtype": "swing",
      "box_px": [270, 231, 323, 278],
      "points_px": [[280, 270], [314, 270]],
      "leaves": [{"hinge": [314, 270], "tip": [314, 238]}],
      "end_support": ["wall", "wall"],
      "uncertainty_px": 2,
      "evidence": "原图有一片扇面与九十度弧，门轴在右端。"
    },
    {
      "id": "window-east",
      "panel": "decorated",
      "kind": "window",
      "subtype": "window",
      "box_px": [289, 316, 364, 328],
      "points_px": [[296, 322], [356, 322]],
      "end_support": ["wall", "wall"],
      "uncertainty_px": 2,
      "evidence": "窗框位于两段墙之间，原图框线横向连续。"
    },
    {
      "id": "bed-east",
      "panel": "decorated",
      "kind": "furniture",
      "subtype": "bed",
      "role": "movable",
      "box_px": [325, 224, 379, 283],
      "points_px": [[352, 247]],
      "uncertainty_px": 2,
      "evidence": "枕头及床垫边可辨。锚点取床垫内。"
    },
    {
      "id": "rail-in-upper-view",
      "panel": "structure",
      "kind": "rail",
      "subtype": "rail",
      "box_px": [309, 120, 450, 135],
      "points_px": [[312, 126], [441, 126]],
      "uncertainty_px": 2,
      "evidence": "参考结构图标出开敞外带及栏边符号。"
    }
  ],
  "absent": {
    "rail": "目标装修示意的本次范围内未见栏杆。结构示意另有参考栏边。"
  }
}
```

- 两份清单逐室列房间、墙段、门、窗、栏杆和主要家具。只有在目标图版内确实未见的类别可填 `absent`，并说明读图范围和依据。另可用 `opening` / `open` 记录无门扇的净开口。
- 房间和家具登记一个锚点。房间裁片须覆盖该空间的房名可能范围。墙段裁片须含可连续的墙芯线和独立读到的两侧墙面截线。遇洞断开。栏杆折角可按实线顺序登记多个折点。直窗和门分别登记一对实际端点。图上曲框可加 `"shape":"curved"`，保留成品分段实量。
- 平开门用 `leaves` 逐扇登记铰点和开启端。移门可加 `min_panels` 和 `min_tracks`，只填原图可确认的数量下限。两侧真实支承分别写 `end_support`。看不清图符可用 `subtype:"unknown"` 或 `symbol_legible:false`。不根据名称补造扇数。
- 家具 `subtype` 采用双方能理解的类型名称。实际按主要独立物件逐件登记，例如餐椅逐把。用途层写 `movable`、`fixed`、`ancillary`，待定写 `unknown`。
- 配对按目标图版中的几何位置和物件类别，不按 ID。局部偏差超过两位读者的读差之和时列为位置差。同址把窗和栏杆读成两种对象时列类型差。只在一份清单出现的对象分别列出。近邻多个候选且无法唯一对应时保持待核。

## 墙性证据和墙色实量

墙体在图上的深浅与它在建筑中的类别各自登记。有墙材图例时优先使用明示图例，写明裁片及实际分类。若只有深浅，填 `"wall_nature":{"basis":"tone_only"}`。结果为墙性待核。

无图例时，可以用独立于深浅的图证形成候选推断。填写 `basis:"inference"`、`classification` 及 `cues`。每条 cue 给 `kind`、`panel`、`box_px` 和 `evidence`。明确构造文字用 `annotation`，同时原样录入 `text`。能定位的专用结构符号用 `structural_symbol`。其他推断至少用两个不同位置和不同类型的证据，其中至少一项是 `junction` 或 `structural_panel_match`。其他可用类型包括 `continuity` 和 `section_geometry`。跨图版匹配须说明是同一段实体及匹配依据。通过的推断字段保留 `source_inference` 状态，属于原图解释。现场结构性质以其专属依据为准。

墙色测量沿用 [现有墙芯导线](color-guides.md) 与 [原生墙色复核](wall-tone-stations.md)。原色取样、导线和报告站点都须落在目标图版。沿轴及法向读差使用本图实读结果。原图、导线、配准和原生 DXF 指纹及读差一致时，可复用既有 `wall-tone-stations.json`。轴上短噪声、阴影、交点和洞口仍待核。一个合格的轴报告只覆盖其中有效的墙材区间。

## 原生回读与输出

```python
from source_evidence import prepare, run

prepare(image, panels_file, source_input_dir)
report = run(image, panels_file, author_file, independent_file,
             registration_file, native_dir, audit_dir,
             ledger_file=ledger_file, wall_guides=wall_guides,
             read_band_px=uncertainty_px, normal_band_px=normal_uncertainty_px,
             wall_tone_report=previous_wall_tone_report)
```

`native_dir` 提供同轮的 `plan.dwg`、`readback.dxf` 和 `capture.json`。输出目录可已存在。解析出错、缺关联文件或一位读者尚未提交时保存待核报告，并记录仍可执行的检查。输入图版、两份清单、配准、原图、DWG 和原生 DXF 都分别留 SHA-256。给台账时也绑定其指纹。

`source-evidence.json` 的 `failures` 和 `pending` 分别保存可追踪的逐项编号，`issues` 给原因。总状态为 `different`、`review` 或 `pass`，优先保留已证实差异。原生检查从实墙轮廓和填充量墙段及独立墙面截线，从实际门窗或栏杆图元复核类型与端部，从房名实体及室内锚点核房间位置，从块内实线和用途层对应家具位置。家具产品、复杂轮廓及使用关系仍需相应专项检查。有指纹和文件头只绑定采集身份。实际打开、图层开关及尺寸再生仍看原生采集的证据。只确认了家具线包络、曲框或多个候选时保留待核。

有任一清单缺项、存在角色冲突、只有墙色而无墙性图证，或者测量不足时，保留相应待核。输出保留范围与原始内容可重复核对。修订图重新走相同的原图清单和原生实体核验。

房间与家具各取一个点时，两名读者应写明所取位置。家具可用 `anchor_kind: footprint_center`（占地中心）、`back_edge_center` 或 `front_edge_center`。房间可用 `clear_area_centroid` 或 `label_center`。两份清单取同一种点时才能按像素读差判错位。任取的房内点只用于寻找对应房间。两点超差且没有共用定位定义时保留待核，再查实际围合或摆放位置。
