# plan-to-cad · 平面一键CAD

当前版本：V1.3.0。墙厚保留240、200、120、100毫米四档；在标注尺寸定标后，按墙两侧的像素差及局部分段比例合理选择。尺寸采用非白颜色。图层统一英文编码，房名在模型空间，图框与图签在布局。保留源图实体核验与每案短复盘。

把售楼户型图或扫描平面图交给 Codex，得到一张可以继续编辑的毫米制 CAD。墙、门窗和家具在同一张图里，可以分别开关图层。

## 开始画一张图

1. 将原图拖进 Codex。优先使用能看清尺寸数字和尺寸界线的图片。告诉 Codex 成果要存在哪个项目目录。
2. 在对话中输入下面的指令。

   > 用 $plan-to-cad 把这张户型图画成分层 CAD。家具和门从内置的室内平面图块库选。画完逐间审查，把原图叠在 CAD 里打开，并告诉我哪些尺寸是推定的。

3. 如果还有同户型的人工 DWG，可补一句“先冻结图片独立稿，再用这份 DWG 对照”。如果原图没有标尺寸，请提供一段能确认两端位置的真实长度，以便定标。

## 打开和使用成果

- 在 DWG 中打开或关闭 `A-XREF`，即可切换叠图和纯线稿。把图纸发给别人时，将 DWG 和旁边的原图文件放在同一文件夹。
- 墙体、门、窗、活动家具、固定家具及设备各有对应图层。可以单独隐藏家具查看结构，也可以选中图块移动。
- 打开 A3 线稿 PDF 检查打印效果。审查记录列出原图明标、推定尺寸和本次修正。
- 模型空间直接显示房名。图框、图签和图例保存在`Plan-A3`布局。图层清单分别列模型和布局的实体数。交付前检查PDF实际纸幅、比例、线宽、字高和留白，并查看整页及正常阅读局部。几何和纸面任一关键项失败，均应返修。
- 原图没有工程轴网时，成果中的线只称营销控制线。审查分开核标数、真实墙面和空间净距；图像读数范围不等于施工容差。

## 经验怎样保留

开工只查相关方法。每案在作业目录留一份短复盘，记录问题、方法、改前改后实量与证据、复验、未查项、终稿哈希和实际用时。无新增经验时，只引用本案既有记录。经验不会自动写进全局规则；验证并经用户采纳后，才合并到原有对应条款。[轻量经验与复盘输入](references/experience.md)

## 使用内置图库

[室内平面图块库（DXF）](assets/interior-plan-blocks.dxf) 随技能提供，包含家具、柜体、厨卫设备、门和楼梯等 97 个毫米制图块。通常只需提供户型原图。想指定某个图块时，用 CAD 打开图库，选好后把编号告诉 Codex，例如“北卧用 BD04 双枕双人床”。有项目专用图库时，可随图提供并指定优先使用。[图库选用方法](references/plan-families.md)

## 首次安装

解压发布包，将整个 `plan-to-cad` 文件夹放入 `~/.codex/skills/`，保留其中的 `assets`、`references` 和 `scripts`，再打开一个新的 Codex 会话。

后台生成和复开 DWG 已在 macOS、Python 3.12、已授权的 AutoCAD 2025 for Mac 上验证。第一次使用时，可让 Codex 按技能目录的 `requirements.txt` 配置 Python 环境。如果想手动跑一份合成示例，在技能目录执行以下命令。

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python examples/smoke_job.py work/demo-input
python scripts/run_first_plan.py work/demo-input/job.json work/demo-output
```

[作业参数与图像配准](references/draft-from-schedule.md) · [绘图、叠图与复量检查表](references/stage-checks.md) · [组件与素材说明](THIRD_PARTY_NOTICES.md)
