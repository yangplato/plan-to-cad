# 把实物背边贴到正确墙面

从已保存的毫米图提取实墙和物件。先核实原图指向哪面墙，家具哪条直边应贴墙。选床头板主体、柜背板外边或确实贴墙的侧板。端饰、枕头、开门符号、装饰投影都单独识别。圆角只取两端切点之间的直段。实际设计仅由床柱、踢脚或安装件接触时先核实构造；不要为消除正常构造空隙而把实物推进墙内。图源或块义不明时列明待核。

## 选取实际几何

- `wall_layers` 和可选的 `nonwall_fill_layers` 按 [墙材回读](acceptance.md)填写。模型必须已经扣除门窗洞。此工具沿实际墙材并集的边界选面；墙体如果本身堵住门窗，会影响家具判断，先验墙洞。
- `backs[].path` 依次写模型空间 INSERT 句柄、途经的嵌套 INSERT 句柄、最终实体句柄。对闭合多段线再写零起始的 `segment`。对一条整直线或实际为直线的样条省略 `segment`。镜像和旋转均从读回图计算。多个背边写多行；数件组合柜可用多个 INSERT 作为同一组刚性移动。
- `toward_wall` 为现有世界坐标下由实物背部指向墙内的方向。`near_face_mm` 是在正确房间一侧、真实墙面上选择的二维定位点。`pick_radius_mm` 取能区分邻墙的选点半径；尽量选实墙中部。它仅帮助识别墙面，不改变共边精度。多个墙面等距时须另选点。
- `physical_outlines` 取完整实物硬轮廓。每项可写 `{"path":[...]}`、用 `segment` 指实际边，或用 `{"parts":[{"path":[...]},{"path":[...],"reverse":true},...]}` 将实际分段按次序闭合。转角柜不得用封住内角空区的大矩形替代。床头两端的实物突出也应计入完整轮廓。需要扣除洞/立体高度、仅有示意遮挡等情况应另测。
- `obstacles` 可列相邻同高硬物真实闭合轮廓；原图清楚的门洞与活动净区还要另查。`max_move_mm` 是本案允许家具平移的上限，`max_slide_mm` 是沿墙找可用墙段的范围。根据原图布局和读图误差填写。

## 实测、调整、复开

```sh
python scripts/align_furniture_to_wall.py 回读.dxf 输入.json
python scripts/align_furniture_to_wall.py 可编辑.dxf 输入.json --write-new 调整后.dxf
```

先看 `before` 确认原有实量，再看 `after_proposed_move`。正的 `normal_gap_mm` 表示背边与所选平面尚有距离，负值表示越过该面。`physical_contact_mm` 只计同平面且对着实墙的直段。另查整件 `body_in_wall_mm2` 与选定相邻实物的相交面积。只有各直背全长都得到正确一侧实墙支承，且实物轮廓无冲突、平移在上限内，才可写新文件；程序只移动列出的家具，并在新 DXF 保存、复开后复量。再次转为 DWG 后，用独立读回的 DXF 重新测原位；源文件转换改变句柄时，从新文件重新选取等价边。

**背边跨洞或实墙长度不足时**，`unsupported_intervals_from_back_start_mm` 列出从背边起点量的各缺段；`back_intervals_over_solid_mm` 给出两侧可依靠的实际范围。是否为窗、门或墙端，按原图和该处门窗确定。先看 `verified_alternative_shifts_world_xy_mm` 中只需沿原墙调位的选项，再核对原图落点、门扇与真实使用净距。选定后可以写新副本。

```sh
python scripts/align_furniture_to_wall.py 可编辑.dxf 输入.json --alternative 1 --write-new 沿墙调位.dxf
```

若允许的移动范围内没有可用整段，定制柜可在覆实墙的两个区间分别配置柜段，或按一段实际净长更换短柜。床、餐椅等完整成品应改选合适规格或重新定位。若转角柜两个背面要求互相矛盾，可将原本可分开的柜肢分别选取，再检查转角拼接。所有选择都要保留原图使用关系。若改变承重墙、已标尺寸或房间布局才可解决，应提交具体矛盾给用户决定。已知设计仅允许部分接触时，记录证据和真实区间，保留该构造。

## 复跑一个合成关节

以下生成的示例仅包含合成物件、多个墙段和一个墙洞。请使用新的临时目录。

```sh
python examples/wall_fit_demo.py ./work/家具贴墙示例
python scripts/align_furniture_to_wall.py ./work/家具贴墙示例/input.dxf ./work/家具贴墙示例/lcase.json --write-new ./work/家具贴墙示例/合位.dxf
python scripts/align_furniture_to_wall.py ./work/家具贴墙示例/input.dxf ./work/家具贴墙示例/over_opening.json
```

第一件是旋转、镜像且带嵌套的L柜。两肢应分别全长共边。第二件的直背跨洞350 mm。其余 `point_only.json`、`on_far_face.json` 分别测试一个角碰墙、柜背在远侧墙皮而柜体侵墙的错误。

同一示例另含独立的水槽和台面实物外缘。可用下面操作复核支承。实际圆角槽、独立台面开孔和盆体应按其安装语义选取实物边或开孔边，另查柜内空间和墙材冲突。

```sh
PYTHONPATH=scripts python - ./work/家具贴墙示例 <<'PY'
import json, sys
from pathlib import Path
import ezdxf
from align_furniture_to_wall import footprints
root = Path(sys.argv[1])
doc = ezdxf.readfile(root / "input.dxf")
item = json.loads((root / "sink.json").read_text())
host = footprints(doc, [item["host"]])
sink = footprints(doc, [item["sink"]])
print(host.covers(sink), sink.difference(host).area, host.boundary.distance(sink))
PY
```

示例应得到全覆盖、越界0 mm²、最小边距70 mm。退出码0表示存在通过检查的拟议调整，原位置是否已通过看 `before.fit`。退出码1表示当前拟议平移未通过，2表示输入或对象无法核实。
