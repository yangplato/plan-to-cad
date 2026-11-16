# 户型描图的轻量经验

只服务准确还原、减少返工和标准交付。开工按问题查相关条目；不默认通读历史案例或搬用旧尺寸、像素阈值、机器路径。经验命中后仍从本案原图和成品独立核验。

## 当前可回用的方法

| 问题 | 动作与判断依据 | 失败处理 |
|---|---|---|
| 标数相同但墙位不对 | 先按原图认端面身份，再从同截面的真实墙两面及并集取数。[源图与成品复量](professional-review.md) | 保留多候选、缺边界和实材侵入；不能只改标字或用总长闭合放行。 |
| 重复回读耗时 | DWG、DXF及原图哈希一致时复用同版[原生回读](../scripts/native_readback.py)。 | 任一相关文件变更就更新检查；不以“之前通过”覆盖新稿。没有真实计时不报省工率。 |
| 纸面看似完成 | 从真纸空间导出[同版PDF](../scripts/plan_sheet.py)，量纸幅和已知线长，再看整页与正常阅读局部。 | 图签缺项、比例、字高、线宽或留白失败，先返修再交付。 |

以上是检查方法，不是一次必成或跨户型效率保证。完整实量和案例图留各自作业目录。

## 每案只留一份短复盘

复盘只记录本次新增：方法、问题、改前改后实量及证据、复验、未查项、终稿哈希、实际操作与后台用时。无新发现时，明确`new_experience:false`，只写一行引用本案既有记录。未计时写null，不估造分钟数。

```sh
python scripts/job_retrospective.py --input 作业目录/复盘输入.json --job-dir 作业目录
```

输入是明确的JSON，不读取自由文字“0错误”来推导通过。所有文件路径相对作业目录；绝对路径也须在该目录内。`final_file`含`path`和本次已查终稿的`sha256`；脚本按实际文件复算并比对。输出只有该目录的`作业复盘.md`。默认不覆盖；更新同案时显式加`--overwrite`，原子替换前不删除旧稿。

新增经验时必填：

- `job_id`、`new_experience:true`、`final_file:{path,sha256}`。
- `checks`：明确的`id/status/evidence`列表。status为pass、fail、pending或not_applicable；pass/fail须有实际证据文件，不适用须有reason。
- `findings`：每项含`id/problem/method`、`before:{measurement,evidence}`、`after:{measurement,evidence}`和`recheck:{status,evidence,reason可选}`。measurement写实量及对象；尚未修正就如实写明。
- `unverified`：未查项列表；`timing:{active_minutes,background_minutes}`写实测分钟，未知用null。

无新增时不填findings，填`reference`指向本案已有记录；终稿及用时字段仍保留。checks可用于带出已知失败／待核。脚本只整理输入报告状态，已知失败先报，不产生整图验收结论，不自动写经验库、skill或Obsidian。

## 什么可以晋升

个案先留作业复盘。通用方法须有适用条件、独立成品验证和失败处理；经相关正反例或跨户型复核，明确适用边界；软件行为也可用官方依据及隔离回归验证。通过后由用户采纳，再合并到已有对应条款。经验条目只留当前做法、条件和证据入口；重复案例不增新条，失效经验保留历史指针，不堆主入口。
