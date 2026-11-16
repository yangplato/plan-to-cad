# 原图逐件清点与原生图层复核

先逐房看原图。每个独立占地、可单独移动或有独立安装功能的物件各记一行。给床、左右床头柜分别编号。同一组写相同的 group。桌和每把餐椅分别记，台面和可辨的嵌入水盆也分别记。独立、同图层且不可拆分的一套专用定制件可按一套登记，写清可辨部件与套数。另一份表由绘图者编排。先保存原图观察，再绑定两份表。只从绘图台账复制观察会遗漏两表都漏掉的物件。

**先定用途，再定图层。** 床头柜、可搬动的小边柜、床、桌椅用 movable，即使背边贴实墙。按墙裁制的柜体用 fixed，记清台面、转角、嵌入或安装的图证。给排水设备用 fixed，写出盆口、排水或配套设施的依据。建筑附属框用 ancillary。型号、块名、通用矩形、贴墙放置都不足以认定安装方式。不能判断时填 unresolved，列出图上看见的事实和可行解释，检查会将该件留待判断。

未标家具尺寸由绘图者结合原图比例、通常使用尺度、相邻物件和通行空间估算，按保存块的全宽和全深复量。用成组占地判断单件是否放得下。商品规格和图库原长宽不能替代本案判断。

## 独立清单

保存 furniture-source.json。source_sha256 是实际原图文件的 SHA-256。anchor_px 取原图中物件实体内部的清晰位置，使用原始像素。group 填所在组合，独立件可省略。use 写日常用途。attachment 取 freestanding（独立摆放）、built_in（嵌入或按墙定制）、services（接管设备）、secured（有安装证据的固定件）、architectural（建筑附属）、unresolved（待判）。evidence 写该件单独成件及其安装关系的原图线索。family 可选，明确知道拟用族时填写，用于找出错选的族。

```json
{"source_sha256":"填写原图文件的64位小写SHA256","items":[
  {"id":"卧室小柜甲","room":"卧室","use":"床头柜","anchor_px":[114,65],"group":"睡眠区",
   "role":"movable","attachment":"freestanding","evidence":"床侧一只独立短柜，轮廓与墙边柜段断开"},
  {"id":"盆甲","room":"卫生间","use":"洗手盆","anchor_px":[237,106],"group":"洗漱台",
   "role":"fixed","attachment":"services","evidence":"台面内有盆口、龙头和排水点"}
]}
```

编排使用同一物件编号，或另存 bindings.json，以原图件号为键、CAD 对象编号为值。一个原图件只绑定一个独立块。dining 的桌编号是编排行 id，第 1、2 把椅的编号依次是 `桌编号/chair-1`、`桌编号/chair-2`。顺序先 upper_offsets_mm，后 lower_offsets_mm，同侧按输入顺序。源图中的不可拆定制件若在现图被拆为数件，先按相同实物粒度说明和绑定；无法逐件对应的计为待核。

```sh
python scripts/check_source_objects.py precheck furniture-source.json objects.json --image 原图.png
python scripts/check_source_objects.py readback furniture-source.json 已建模型/objects.json 已建模型/registration.json 已建模型/review/native --image 原图.png
```

编号不同时，在两条命令中均加入 `--bindings bindings.json`。两条命令均输出 JSON。退出码 0 表示已登记各件通过当前步骤，1 表示有漏件、错层或其他差异，2 表示输入缺失或无效。

第一步按房间、组合、用途统计件数，逐件比对编排，加查桌椅组合。第二步校验原生文件摘要，根据模型空间实际句柄读取块名、图层、实体线和源图锚点，再在原生导出的四份开关状态中逐件读取可见绘图记录。多余的家具层实体、没绑定的台账对象、重复句柄和源图一整类物件错层都会报告。轮廓不闭合时，锚点检查采用实际线条的凸包，结果标明 stroke_hull。报告中的可见记录只反映该件自身。遮挡、特殊组合块里各部件的功能，以及原图本身是否漏读，仍须看原图与保存图的对应局部。没有台账且画在非家具层的未知散线，需结合图面检查。

直接调用 place_hosted_family.py 时，给每件床或直柜填写 role。旧的独立调用可省略 role，沿用原先的床归活动、直柜归固定。整户编排的每一件都须提供所判定的用途层。
