# 从源图观察表一次生成墙、门窗和家具

新作业先登记[分段控制点](dimension-grid.md)和[逐条家具接边](object-edges.md)。脚本生成的是工作稿。正式交付接着执行[独审和局部复查](working-release.md)。

新建两份工作数据。scene.json 是本案原图读出的墙轴、颜色、门窗、房间净区和标数。objects.json 是逐房、逐件的家具及设备表。每件各占一行，每行给唯一 id、功能、类别、毫米宽深和摆放方式。床头凳、边几、单椅等有独立占地的物件单列。看不清的同一轮廓可记录两种用途，但只在最终图中放一件。

```sh
python scripts/build_plan_objects.py scene.json objects.json 已建模型
```

命令生成 source.dxf、结构台账、图元句柄、支承关系和餐椅试退记录。默认按块名读取室内平面图块库；项目指定独立图库时，为对应的 `library` 条目提供文件名，并在命令中增加 `--library-root 项目图库目录`。

| type | 用法 |
|---|---|
| hosted | 库床与直柜。给 family、host、side、near_px 和实际尺寸。造块时即在当前 CAD 的实墙上求背边。整段定制直柜可给 mode=full_span。 |
| dining | 长桌与独立餐椅。给 center_px、桌长方向 axis_toward_px、两侧椅中心的沿桌偏移、收椅量、完整桌椅宽深与本案试退距离。 |
| fixture | 盆、灶、洗衣机、淋浴屏、X 形附属框或 L 柜。给 family、back_px、toward_px、宽深与实际选用的细节。设备嵌在台面时用 support 指向对应台面 id。 |
| family | 已核 CAD 库的沙发、坐便器等。给 family、back_px、toward_px、毫米宽深和功能层。 |
| library | 室内平面图块库或项目图库的俯视块。给 library_block、从图块确认的 library_front_vector。选外部库时另给 library_file。再给原图上的 back_px/toward_px、毫米宽深。庭院椅、脚凳、圆几等按可见结构与使用面区分。 |
| outline | 原图确为单一轮廓的台面、简单端几等。给 function 和闭合 outline_px。水槽、灶和坐便器须有能辨用途的符号。 |

role 取 movable、fixed 或 ancillary。raw 坐标均指原图像素，向下为正。hosted 的 side 为 CAD 房间侧 +x/-x/+y/-y。已知为 L 柜等需终检背边的 fixture 可另给 contact，注明 family、side 或主背/返翼的宿主。临窗留空的柜与贴实墙的柜分别登记，留空的须有源图依据。

同一条实墙在颜色变化处分了材料段时，可给 support_segments 列出相接、同室内面的多个墙 id。程序仍按模型中的实墙材质并集求支承，窗和门洞会打断支承。原图未标家具的中心可沿这面墙在小范围内移动时，给 center_zone_px 的两个边界点。程序先试观察到的位置，仅在该区内寻找最近的完整实墙接触，另存实际移量。不能把门洞、另一面墙或另一房间纳入备选范围。

以下数值为虚构例子。每件只使用自己本案的尺寸、原图比例和图块。

```json
[
 {"id":"床","type":"hosted","family":"bed_double","role":"movable","host":"墙甲","side":"-x","near_px":[300,100],"support_segments":["墙甲","墙乙"],"center_zone_px":[[300,96],[300,105]],"width_mm":1600,"depth_mm":1950},
 {"id":"台面","type":"outline","role":"fixed","function":"厨房操作台","outline_px":[[34,30],[95,30],[95,50],[34,50]]},
 {"id":"盆","type":"fixture","family":"sink","role":"fixed","back_px":[70,32],"toward_px":[70,40],"width_mm":680,"depth_mm":430,"support":"台面"},
 {"id":"两人桌","type":"dining","role":"movable","center_px":[140,108],"axis_toward_px":[155,108],"table_family":"table_compact","chair_family":"dining_woven","layout_mm":{"upper_offsets_mm":[0],"lower_offsets_mm":[0],"tuck_mm":90,"table_width_mm":900,"table_depth_mm":700,"chair_width_mm":445,"chair_depth_mm":460},"pull_mm":300}
]
```

保存后的工作图按实际对象表核查。一套可被整组移动的专用定制构件可以保留为一个块，检查盆、灶或柜背时要能在块内分别读出实际安装边与宿主边。对未知异形图库件或特殊室内构造，仍可在这张 CAD 中补充真正的可编辑实体；在检查表中注明补充对象和实量。

对每把餐椅试退，用 seat_motion.py 读取登记与复开图。从座椅原位到指定距离检查整段扫过范围。正确抽椅方向由同一桌的实际长边与座位所在侧决定。凹形物件只用保守外包测到重叠时，须再查真实座面、四脚与扶手。餐厅以外的休闲椅、脚凳和小几也要按同组占地检查；把额外放置的一件删除前须核对是不是误计了原图同一个物件。

## 一份任务表运行到可复开的 DWG

```json
{
  "image":"原营销图.jpg",
  "scene":"scene.json",
  "objects":"objects.json",
  "own_reading":"自读/source-reading.json",
  "peer_reading":"同档读图/source-reading.json",
  "wall_guides":"guides.json",
  "room_witnesses":["编号对应后的房间见证.json"],
  "source_geometry_review":"独立源图几何预期.json",
  "uncertainty_px":2
}
```

```sh
python scripts/run_first_plan.py job.json 新工作目录
```

图片与所有数据路径以 job.json 所在目录为基准。从已存文件测色段时，scene.json 同时携带 source_sha256、holdouts、holdout_limit_mm。每个 holdout 提供未用于建立比例的原图刻线 source_xy 和独立 CAD 毫米控制值 cad_mm。任务表可补 guide_report、normal_uncertainty_px、source_survey、extra_contacts 或 seating_groups。无额外信息的字段省略即可。额外靠墙登记用对象 id；座具组可按表内 id 定位成员，构件的实际线、座面和脚点通过 --inspect 识别。

新入口一律声明源图实体核验为必查。`source_geometry_review`指向`source-geometry/1`规格，原图预期先固定；缺少时仍可生成候选，但摘要为待核，不可整户放行。规格未填回读或DWG哈希时，入口在实际原生复开后绑定；已填而不符时不能覆盖成新值。也可由独审者在本轮assessment顶层补该规格，门槛会按同版成品重新计算。字段与命令见[验收入口](acceptance.md)。

图框设置放`scene.paper`。`metadata`填写`project/unit_type/drawing_title/revision/date/source/limitations`，单位默认mm；`room_labels`逐项填`name`与模型坐标`point_mm`，由锁定视口投到真实纸空间。必要房名清单默认取这些名称，也可显式填`expected_room_names`。未填元数据或房名范围可出候选PDF，但纸图完整性不能通过。字高、字宽与文字位置须看实际字形和PDF，不用理论字数乘字高代替检查。

1. 在建图之前，先用[从原像素拟合洞口收头](fit-opening-stations.md)测每道洞口的两端。只在当前源图的墙面断点确有独立佐证时采用坐标提案。同时核对本洞和同轴相邻构件的用途。
2. 建入 CAD 后，用[沿轴检查深浅分色](wall-tone-stations.md)查整段及每一处分界，在相同母轴量换色点的像素和毫米差。两段中心的颜色各自正确时，过渡位置仍可能错。侧墙、尺寸标数和结构性质的调整仍按委托方的授权范围。
3. 输出中的 source.dxf 保留编辑几何。review/native/plan.dwg 为保存并复开的工作 DWG。author-summary.json、review/whole-furniture-strokes.json、review/seat-motion.json 和逐房视图给出要处理的实际对象。即使退椅已通过，凡近距离布置的休闲座具及边几仍按[成组座具的脚位、座面与扶手](seating-groups.md)核实。完整轮廓线相交、脚落入另一座具的实际支承区、仅保守外框重叠，是不同的证据。若两件在同一高度有实碰，在原图能支持的房间局部内移动、转向，或查是否把同一座具的脚凳误记为第二把椅子。
4. 逐项修改 scene.json 或 objects.json，在新目录复跑。绘图者实际查看保存文件和原图后，才将自己的首稿提供给外审。预示遮挡的原图证据、未经证实的墙类和未标家具尺寸都保留原判依据。

在整户首稿试验中，任务表可加 use_reading、furniture_source 和 object_bindings。use_reading 指向用途观察者封存的原图门窗清单，furniture_source 指向同一观察者的逐件清单。只有源图编号与建模编号不同时才填 object_bindings。分别检查保存模型中的实扇、轨扇和每件物体的真实归层。柜旁有独立柜身时，按活动或安装证据决定角色。两翼都应贴实墙的转角柜用 hosted_corner。

工作目录里的 首稿待查位置.json 按具体对象列出洞口、墙背、空间、家具碰撞和逐件关层读差。先开对应局部，确定来源是读图、建模、家具选型或合理遮挡。改完从保存的版本重跑，有依据的模糊判断留原图局部和实量。另见[两条真实柜背及正交墙角](corner-on-walls.md)。

工作表可设 `apply_printed_tones: true`。此时先根据本案原图、事先取样的色段及 guides 里的 printed_tone_scope，为初稿的临时墙类生成新的 scene-working.json，然后编译。原始 scene.json、宿主、墙厚、净洞与标数均保留。只有用户授权按图面深浅归类、且尚未确认真实结构属性的墙段才可进入 provisional_wall_ids。scope.basis 写本案已有的依据。原图没有足够色证时，保留源图推断及待核范围。详见[墙芯色段](apply-wall-tones.md)。

`functional_clearances` 可列出一个或多个局部使用净距任务文件，格式见[使用净距](use-clearance.md)。对象可给已存 CAD 的 handle，也可给对应编排表的 object_id；后者会解析到当次真实模型句柄。输出在 review/use-clearance-*.json。每项写清本案使用者、动作方向与拟采用的净距。

整屋入口还会从实际座位前方自动生成 review/seat-fronts.json，列出对象对、占据前方的连续宽度和各截面的实量。无需先给每个座位登记图块内线段，也不必自定统一的合格数。若表中没有识别到原图的沙发或方向，检查用途字段、背边锚点和面向，再用已保存的工作稿复测。在实际可坐用的前方选择有依据的净距，并按房间空间决定调整量。

## V1 原图参照

V1.2作业在模型空间的A-XREF层附着原图；旧版显式复做保留旧层名。该层锁定并设为不打印。输出 DWG 旁的 `source-plan.<原扩展名>` 是原图副本，按相对文件名加载。交付时把它与 DWG 放在同一目录。作业可填写 `reference_clip_px`，格式为原图从左上起算的 `[左, 上, 右, 下]`。裁切仅影响 CAD 中的显示范围。

在 `ledger.json` 的 `source_reference` 核对原图哈希、文件名、图元句柄和配准残差。分段控制线使用各自校准值；未变形的原图参照每个方向只能用一组比例与平移。最大残差用于解释刻线与底图间的读差。
