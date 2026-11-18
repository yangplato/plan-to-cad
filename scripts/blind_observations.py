"""Typed CAD observations. Generate the legacy index from the reviewed rows."""

SCHEMA = "cad-observations/1"
VISUAL_FIELDS = ("junctions", "access", "symbols", "sheet")
DISPOSITIONS = {"confirmed": "确认错误", "needs_source": "待对图", "normal": "可接受"}


def form(report, dxf_hash):
    checks = [{"id": item["id"], "kind": "", "location": "", "evidence": ""}
              for item in report["findings"]]
    return {"schema": SCHEMA, "dxf_sha256": dxf_hash, "reviewer": "",
            **dict.fromkeys(VISUAL_FIELDS, ""), "checks": checks}


def normalize(record, report):
    if record.get("schema") != SCHEMA or not isinstance(record.get("checks"), list):
        raise ValueError("请用 --form 生成本版表单，并填写 checks")
    if any(not isinstance(record.get(k), str) or not record[k].strip()
           for k in (*VISUAL_FIELDS, "reviewer", "dxf_sha256")):
        raise ValueError("请标明审图者、回读指纹，并填写墙角、通行、符号及纸面观察")
    dispositions, confirmed = {}, []
    for row in record["checks"]:
        if not isinstance(row, dict) or any(not isinstance(row.get(k), str) or not row[k].strip()
                                            for k in ("id", "kind", "evidence", "location")):
            raise ValueError("每条观察须有唯一编号、类型、位置和实查依据")
        ident, kind = row["id"], row["kind"]
        if ident in dispositions or kind not in DISPOSITIONS:
            raise ValueError(f"检查 {ident} 的编号或类型无效，只能选 confirmed / needs_source / normal")
        dispositions[ident] = {"status": DISPOSITIONS[kind], "evidence": row["evidence"],
                               "location": row["location"]}
        if kind == "confirmed":
            confirmed.append(ident)
    missing = {r["id"] for r in report["findings"]} - dispositions.keys()
    if missing:
        raise ValueError("以下 CAD 候选尚未分类：" + ", ".join(sorted(missing)))
    if ("open_defects" in record and record["open_defects"] != confirmed
            or "dispositions" in record and record["dispositions"] != dispositions):
        raise ValueError("观察分类与汇总字段冲突；请更正原始 checks")
    return dict(record, dispositions=dispositions, open_defects=confirmed)
