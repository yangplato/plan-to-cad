# 平面家具与设备图例

## 室内平面图块库

随包图块采用毫米单位。可直接打开 DXF 查看几何，或从索引查找类别、块名和尺寸。在对象表使用 `type:"library"`，按索引填写 `library_block`。例如，双人床可选 `BD04_双枕双人床`。核对枕侧、床尾和本房墙面，再设置前向、放置点和占地尺寸。使用内置库可省略 `library_file` 和 `library_root`。显式使用外部 DXF 时，填写项目图库目录及其下的文件名。[图库 DXF](../assets/interior-plan-blocks.dxf) · [块索引](../assets/interior-plan-blocks.json) · [对象表参数](draft-from-schedule.md) · [门图例](drafting-format.md)

## 20 个家具与设备示意图例

使用 `family` 类对象或参数化桌椅组合时，按下表选键名。示意块的尺寸是绘图默认值。特定产品按厂家尺寸或本案证据定位。

用 `python scripts/place_plan_families.py --full` 查看当前生效目录中各图例的来源、尺寸、朝向和后靠实线。键名保留既有作业使用的名称。新作业应记录实际目录、版本、块名和适配。复做采用了外部图库的历史稿时，使用该版原目录。

| 类别 | 图例键名 | 定位依据 |
|---|---|---|
| 餐椅、办公椅 | `dining_woven`、`dining_plywood`、`dining_cantilever`、`desk_casters` | 辨认靠背、座面及脚轮的实际占地，逐把核对朝向。 |
| 床 | `bed_double`、`bed_single` | 枕侧为床头，按床垫名义宽选型。 |
| 沙发、休闲椅 | `sofa_two`、`sofa_three`、`shell_lounge` | 长靠背居后，入座侧朝前。 |
| 桌几 | `table_compact`、`table_large`、`round_side_table`、`round_side_table_large`、`round_coffee_table` | 矩形桌的长边为后边，圆桌可选后切点定位。 |
| 坐墩、长凳 | `round_footstool`、`padded_footstool`、`slatted_bench`、`timber_bench` | 量座面。根据原图复核席位与通行。 |
| 厨卫设备 | `sink_inset`、`toilet_open` | 水槽量槽沿与台面的关系。坐便器水箱朝墙，便盆朝使用侧。 |

## 使用外部 JSON 目录

`MPCAD_FAMILY_CATALOG` 可指定与 `assets/plan-families.json` 同结构的完整授权目录。未设置时，脚本先查 `~/.config/plan-to-cad/plan-families.json`；设置了 `XDG_CONFIG_HOME` 时使用该配置根目录。两个来源都未提供时，程序使用包内原创图例。外部 DWG／DXF 库采用[项目图库选用方法](sources.md)，DWG 文件不能直接填入这个 JSON 变量。

所有目录采用毫米。路径归一到后靠包络线中点，局部 −Y 为前向。矩形桌板覆椅时，遮挡范围取该块宽深确定的完整矩形。`rear_contact.intervals_x_mm` 是 Y=0 上各段实际后靠线，`point_contacts_x_mm` 是其余后切点，`total_flat_mm` 是共线实边总长。外包碰到墙时，仍要核对柜身、桌板或床头等实际部件。回读外部块的内容、朝向与缩放后再量接触边。

## 单件

将本 Skill 的 `scripts/` 加入导入路径。`face_point` 为本案墙面上的靠点，`roomward` 为由该处指向室内的法向。

```python
from place_plan_families import catalog, place_family, place_dining

seat = place_family(msp, "toilet_open", back=face_point, facing=roomward)
```

函数返回原生 INSERT。默认使用块的实量。传 `width_mm` 或 `depth_mm` 中的一项会等比缩放。两项都传会分别调整两轴，仅在本案两向占地有依据时使用。定制家具或目录不匹配的对象可依据原图和使用常识自行绘制。柜体、台面等按本案墙面生成。默认把水槽、坐便器放入“固定家具及设备”，其余放入“活动家具”。`layer=` 可指定项目图层，块内线条均在 0 层。

## 对坐餐椅

```python
group = place_dining(msp, "table_compact", "dining_woven", center=table_center,
                     upper_offsets_mm=observed_upper_seat_centers,
                     lower_offsets_mm=observed_lower_seat_centers,
                     tuck_mm=observed_tuck, angle_deg=table_angle)
chairs = group["chairs"]
assembly_bounds = group["bounds_mm"]
```

每个列表的长度给出该侧可据图放置的椅数，空列表表示该侧不放椅。列表值为座位中心沿桌板长边方向、相对桌心的有符号毫米距离。局部上侧是 +Y，下侧是 −Y，两侧椅面都朝桌心。整组绕桌心逆时针旋转 `angle_deg`。`tuck_mm` 是完整椅面从桌边伸入桌底的长度，需要从图中判断。椅子和桌板各自保留可移动的 INSERT。推入量大于零时，桌板自带原生 WIPEOUT 遮挡，移走桌板即可看见椅子的完整图形。单椅尺寸由各自的块决定，组合占地使用返回的全组世界坐标包围盒。桌宽、桌深、椅宽、椅深可分别传入 `table_width_mm`、`table_depth_mm`、`chair_width_mm`、`chair_depth_mm`。

新户型的席数、错位、推入量、朝向、过道和靠墙关系均须重新读图。椅端位于短边、弧桌或不规则桌边等情况，可按各自实际方向用单件函数放置；桌板遮挡轮廓需另按实际桌边生成。
