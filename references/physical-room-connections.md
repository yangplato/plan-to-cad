# 房间与洞口几何核对

将脚本与现有 `scripts/check_wall_probes.py` 放在同一目录。使用已保存并重新打开的毫米制 DXF。程序只读图纸。报告中每个洞口有净宽、墙厚、实际墙端、洞内残材面积、三个位置的穿墙长度、两侧分区和种子。SVG 含总览和问题洞口局部放大。

```sh
python check_portal_rooms.py saved.dxf ledger.json independent.json --json result.json --svg rooms.svg
python check_portal_rooms.py --self-test
```

`ledger.json` 直接用建图脚本输出。另一个人先从原图登记 `independent.json`。示例中的房间名、数值和图号均为虚构。配准顺序为 `[a,b,d,e,tx,ty]`，即 `X=a*u+b*v+tx, Y=d*u+e*v+ty`。沿用已单独校准的配准值。

```json
{
  "source_to_mm": [10,0,0,-10,0,8000],
  "rooms": [
    {"id":"bed","mode":"closed","seeds_px":[[210,185]]},
    {"id":"living","mode":"open","seeds_px":[[200,630],[520,155]]},
    {"id":"dining","mode":"open","seeds_px":[[760,180]]}
  ],
  "open_links": [["living","dining"]],
  "portals": [{
    "id":"B","rooms":["bed","living"],"kinds":["swing"],
    "approach_px":[[125,345],[125,455]],
    "candidates":[
      {"id":"horizontal","a_px":[80,400],"b_px":[170,400]},
      {"id":"vertical","a_px":[400,360],"b_px":[400,270]}
    ]
  }]
}
```

- `seeds_px` 是普通地面点。非凸房间可登记多点。程序只检查地面是否在同一个连通分区，不用种子到洞口的直线或重心判门向。
- `closed` 表示门关闭后应与其他已登记区域分隔。`open` 可用于开放厨房或客餐厅，通过 `open_links` 指定开放对象。明确无需通行的封闭井道可设 `accessible:false`。`unknown` 保留待核。真正通室外的边界用 `outside`。入户门外是封闭公共前室时，给 `outside` 添加 `mode:"exterior"` 和前室种子。阳台、天井也可使用独立名称和 `exterior`。
- `portals` 用图中对应的洞口 ID 对齐独立观察。`kinds` 可限定可接受的类型。`approach_px` 可省；给出时须按 `rooms` 顺序，在洞口两面各取一个能证实直接穿过该净洞的近点。遇到备选正交宿主，登记多个候选方向。报告分别给出法向有符号距离、穿越位置及这条线穿过现存墙材的长度。共用同一对房间的两处洞口仅凭房间种子不能区分，应补局部近点。
- 程序先在内存封住门窗、框和洞口，计算房间分区，再放开永久敞口核对开放关系。自由框须由保存 DXF 内对应的直线、直线段覆盖其实际跨度。原图不明确、框线重映射、墙材解码不全、多个开放区域不能定位到单一区域时，保留待核。已有可靠净洞内出现墙材、独立近点同侧或穿墙，仍可独立报错。
- 退出码 0、1、2 分别表示给定观测通过、有冲突、待核。脚本不测量门扇弧的开启方向、净通行宽度和家具碰撞。漏登记的房间或原图独有门不能凭几何穷尽，须单独保全原图观察。

自检在内存序列化并重读虚构 DXF，覆盖 10 种情况。正常 L 形厅、开放厨房、入户门外封闭前室通过。把厨房门移到卧室墙、同一对房间选错正交宿主、虚报卧室开通、入户洞被墙封住、卧室漏墙、把卧室门写成永久开口均报错。自由框缺少可核的保存图形时待核。
