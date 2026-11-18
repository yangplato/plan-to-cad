# V1.6 从接图到交付

所有命令从技能根目录运行，使用本机已具备 requirements.txt 依赖的 Python。绘图者在第一次读图前先运行 `python3 scripts/feedback_book.py read`，阅读当期交付反馈核对清单。随后在刚收到原图的**同一运行环境**运行小样。小样包括原生保存、复开、将尺寸从 1000 mm 改为 1100 mm 并核对可见尺寸文字。

```sh
python scripts/native_environment.py 工作区/开工预检
```

程序写入独立目录，使用后台串行锁。预检失败时查看 report.json 中的阶段和原始原因。换运行环境后重新预检。兼容解析器与 AutoCAD 原生证据分别说明。作业入口也在本次进程内运行小样。若环境不可用但输入有效，作业保留 source.dxf，并说明原生 DWG/PDF 的缺项。

## 绘图与独审

1. JOB 和 scene 使用 `drafting_version: "1.6.5"`。按[原图清点协议](source-evidence-v16.md)登记图版、墙芯导线和作者清单。JOB 填 `source_panels`、`source_author_inventory`、`wall_guides` 和本图读差 `uncertainty_px`。准备原图及增强裁片，先看结构、边线和分段，再选墙性和家具。普通外围实墙、控制面、窗框、退台和邻件字段见[几何协议](wall-geometry-v16.md)。
2. 运行 `python scripts/run_first_plan.py 首稿JOB.json R00`。保存原生 DWG 和图纸试样。将关闭底图的 DWG、原生回读 DXF 和纯线纸样复制到独审空间。保留 CAD 的相对参照定义，盲审包中不放原图、独立清单、制图台账或旧结论。
3. 以下命令先为每个机器候选创建一行。审图者查看 CAD，补上机器没有发现的项目。每行填写唯一 id、location、evidence 及 kind。确认的执行错误填 `confirmed`，需原图判定填 `needs_source`，已量明正常填 `normal`。同时填写 reviewer 和四项整图观察。编号和结论从 checks 生成。每个候选均可标待对图，不确定的细部保留疑点。盲审工作区如无作业目录，先在工作区封存，原样复制到 R00/review/cad-only-frozen.json。

```sh
python scripts/check_cad_logic.py R00/review/native/readback.dxf --form 盲审表.json
python scripts/check_cad_logic.py R00/review/native/readback.dxf --freeze 盲审表.json --json R00/review/cad-only-frozen.json
```

4. 封存后才给审图者原图。逐图版、逐室和逐边独立清点，记录实际会话、原图指纹和裁片。先封存这份清单，再交作者台账和叠图。比较结果及已找到的几何偏差一次汇入 `完整独审.json`。沿用[独审与复量字段](professional-review.md)；issues 中的 repair 保留首次确认的 id。首稿已通过时零次修正。缺证的项目在五项说明中列待核。

```sh
python scripts/freeze_source_inventory.py R00 独立清点.json
python scripts/review_working_plan.py R00 完整独审.json
```

## 一次集中修正

首稿保存后查看 `review/room-label-clearance.json`。逐个核实房名笔画与家具、洁具真实线条的相交候选。缺少字体或无法展开图元时，在同版 CAD 或试印图上查看该处。将文字移动、家具移动后出现的遮挡合并到同一轮问题清单。终稿从实际 DWG 回读重新计算，保留可定位的文字及图块句柄。两人原图清单中的不同读法汇入待核；在原生图元或独立对图上证实的执行错误列入返修。

修订 JOB 增加 `revision_of` 指向首稿工作目录，`repair_list` 指向完整独审 JSON。路径相对于修订 JOB。保留首稿观察与两份原始清单。终稿统一核对时按本版原生实体重新复量所有墙、洞、窗、图块和相邻使用空间，回到原问题截面并检查关联部位与新增问题。修后观察填写同一格式，文件名为 cad-recheck.json。此阶段审图者已经看过原图。

```sh
python scripts/run_first_plan.py 修订JOB.json R01
python scripts/check_cad_logic.py R01/review/native/readback.dxf --form 修后表.json
python scripts/check_cad_logic.py R01/review/native/readback.dxf --freeze 修后表.json --json R01/review/cad-recheck.json
python scripts/review_working_plan.py R00 完整独审.json --revised R01 --recheck 修后复验.json
```

终稿核对绑定原始清单、首稿与终稿 DWG 指纹。同版可重复检查，提交过的首稿不能再登记另一份终稿。交付时运行 `python scripts/check_delivery_v16.py R01 项目名-户型名 > 交付目录复开.json`，零修订时用 R00。程序按同名 DWG 和 PDF 核对指纹，从交付目录取图执行隔离的原生复开，并核对毫米单位及相对参照加载。校验说明列出五项、未解决的问题和打印结果。报告中的墙色相异、确认几何错误与证据缺失分别保留。另有同目录恢复副本时列出文件名，不改动使用者的文件。

原图参照层须关闭、锁定、禁止打印，且能随 DWG 加载。纯 CAD 检查该项时，在观察行填 `subject: source_reference_layer_defaults` 并在 location 注明实际层名。若独审者在封存后发现把正确状态误填为确认错误，保留原表。综合独审按同一 ID 记 inference，并加入 `native_reclassification`，内容为 `check: source_reference_layer_defaults`、`layer` 及 `initial_dxf_sha256`。审查脚本复开采集的原生数据并核对原图指纹与图层状态。若早期修单已经按 repair 编号冻结，可在复验表该行用 `result: verified_unchanged` 和同一组证据。报告单列经证实保持正确的项目。其他构件及未证实的参照问题继续逐项处理。
