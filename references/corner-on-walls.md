# 两面贴墙的 L 形定制柜

在原图看清柜体两翼后，用 `type: hosted_corner`、`family: corner_wardrobe`、`role: fixed` 登记一件。四项尺寸均来自该图或本案注明的推定。本工具从两个实际室内墙面的交点生成一个可编辑六顶点闭合块，使用固定家具层，块内线条在 0 层。

以下数字仅演示字段。两翼沿正交墙伸出，局部室内点来自原图。墙面方向用模型空间坐标，`inside_px` 使用原图像素。

```json
{
  "id": "closet-A", "type": "hosted_corner", "family": "corner_wardrobe", "role": "fixed",
  "host": "south", "side": "+y", "return_host": "west", "return_side": "+x",
  "inside_px": [84, 132],
  "width_mm": 1500, "depth_mm": 1100, "back_depth_mm": 500, "return_depth_mm": 420
}
```

- `host` 和 `return_host` 分别指主翼、返回翼所靠的墙。两项 `side` 均指从各自的墙面指向室内。局部室内点须在两面墙共同朝向的一侧，并能从内角直达。已知模型毫米点时可改用 `inside_mm`。
- `width_mm`、`depth_mm` 分别是从同一个内角量出的主背总长和返回背总长。后两项是两翼各自的柜深。
- 每翼可跨连续的共面墙段。用 `support_segments`、`return_support_segments` 分别列出组成该翼墙面的全部 ID，包括对应的 `host` 或 `return_host`。不同墙厚可用实际室内面共面的墙段。台阶面、斜墙、弧墙及真正断开的墙段需另行建模。
- `wings` 按翼列出实际背边、实墙区间、接触长度、法向缝隙、无支承区间、相交洞口 ID、柜体缺失面积。区间的零点是两室内面的交点，正向指向该翼末端。实体轮廓、洞口及门弧均从当前 CAD 读取或与结构登记交叉核对。
- 测家具之间的干涉时，函数可接收另行登记的 `obstacles` 占地面。整户仍使用全屋家具碰撞检查。

也可单独运行 `python scripts/place_corner_on_walls.py scene.json corner-items.json --write-new checked.dxf`。脚本保存并复开新图，再从原生块实量两翼。现有图可加 `--drawing existing.dxf --ledger ledger.json`。调用 `check_corner_on_walls(doc, ledger, item, ref, source_to_mm=...)` 可复核改过的块。
