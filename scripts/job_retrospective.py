#!/usr/bin/env python3
"""Write one job-local retrospective from explicit, evidence-backed input.

This records supplied findings. It does not certify CAD quality or promote rules.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import quote


STATUSES = {"pass", "fail", "pending", "not_applicable"}
STATUS_LABELS = {"pass": "所列复验通过", "fail": "需返修", "pending": "待核", "not_applicable": "不适用"}


class ReportError(ValueError):
    pass


def required_text(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReportError(f"{field} must be a non-empty string")
    return " ".join(value.split())


def text_list(value, field: str) -> list[str]:
    if not isinstance(value, list):
        raise ReportError(f"{field} must be a list")
    return [required_text(item, f"{field}[{i}]") for i, item in enumerate(value)]


def local_file(value, job_dir: Path, field: str) -> Path:
    path = Path(required_text(value, field)).expanduser()
    path = (job_dir / path).resolve() if not path.is_absolute() else path.resolve()
    if not path.is_relative_to(job_dir):
        raise ReportError(f"{field} must be inside the job directory")
    if not path.is_file():
        raise ReportError(f"{field} does not name an existing file: {path}")
    return path


def evidence_files(value, job_dir: Path, field: str) -> list[Path]:
    return [local_file(item, job_dir, field) for item in text_list(value, field)]


def check_record(record, job_dir: Path, field: str) -> dict:
    if not isinstance(record, dict):
        raise ReportError(f"{field} must be an object")
    status = record.get("status")
    if not isinstance(status, str) or status not in STATUSES:
        raise ReportError(f"{field}.status must be one of {sorted(STATUSES)}")
    evidence = evidence_files(record.get("evidence", []), job_dir, f"{field}.evidence")
    if status in {"pass", "fail"} and not evidence:
        raise ReportError(f"{field}: pass/fail requires an evidence file")
    reason = record.get("reason", "")
    if status == "not_applicable":
        reason = required_text(reason, f"{field}.reason")
    elif reason:
        reason = required_text(reason, f"{field}.reason")
    return {"status": status, "evidence": evidence, "reason": reason}


def inline(value: str) -> str:
    return value.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]").replace("<", "&lt;").replace(">", "&gt;")


def file_link(path: Path, job_dir: Path) -> str:
    relative = path.relative_to(job_dir).as_posix()
    return f"[{inline(relative)}]({quote(relative, safe='/._-')})"


def links(paths: list[Path], job_dir: Path) -> str:
    return "、".join(file_link(path, job_dir) for path in paths) or "尚无证据文件"


def timing_text(value) -> str:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ReportError("timing must be an object")
    parts = []
    for key, label in (("active_minutes", "实际操作"), ("background_minutes", "后台等待")):
        number = value.get(key)
        if number is None:
            parts.append(f"{label}未计时")
        elif isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or number < 0:
            raise ReportError(f"timing.{key} must be a finite, non-negative number or null")
        else:
            parts.append(f"{label}{number:g}分钟")
    return "；".join(parts)


def render_report(data, job_dir: Path) -> str:
    if not isinstance(data, dict):
        raise ReportError("input must be a JSON object")
    job_id = required_text(data.get("job_id"), "job_id")
    if type(data.get("new_experience")) is not bool:
        raise ReportError("new_experience must be an explicit boolean")
    final = data.get("final_file")
    if not isinstance(final, dict):
        raise ReportError("final_file must be an object")
    final_path = local_file(final.get("path"), job_dir, "final_file.path")
    digest = hashlib.sha256(final_path.read_bytes()).hexdigest()
    expected = final.get("sha256")
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ReportError("final_file.sha256 must be the explicit SHA-256 of the checked final file")
    if expected != digest:
        raise ReportError("final_file.sha256 does not match the actual file")
    time_note = timing_text(data.get("timing"))
    unverified = text_list(data.get("unverified", []), "unverified")
    raw_checks = data.get("checks", [])
    if not isinstance(raw_checks, list):
        raise ReportError("checks must be a list of explicit check records")
    checks = []
    ids = set()
    for i, raw in enumerate(raw_checks):
        check = check_record(raw, job_dir, f"checks[{i}]")
        check["id"] = required_text(raw.get("id"), f"checks[{i}].id")
        if check["id"] in ids:
            raise ReportError(f"duplicate check id: {check['id']}")
        ids.add(check["id"])
        checks.append(check)
    counts = {status: sum(check["status"] == status for check in checks) for status in STATUSES}
    summary = f"输入检查记录：通过{counts['pass']}、失败{counts['fail']}、待核{counts['pending']}、不适用{counts['not_applicable']}" if checks else "未提供新的结构化检查记录，只引用既有记录"
    if not data["new_experience"]:
        if data.get("findings"):
            raise ReportError("new_experience=false cannot contain new findings")
        reference = local_file(data.get("reference"), job_dir, "reference")
        pending = [check["id"] for check in checks if check["status"] == "pending"] + unverified
        failures = [check["id"] for check in checks if check["status"] == "fail"]
        details = "; ".join(filter(None, [f"已知失败：{'、'.join(map(inline, failures))}" if failures else "", f"未查／待核：{'、'.join(map(inline, pending))}" if pending else ""]))
        return f"无新增经验。沿用{file_link(reference, job_dir)}；{summary}；{details + '；' if details else ''}终稿SHA-256：`{digest}`；{time_note}。\n"

    findings = data.get("findings")
    if not isinstance(findings, list) or not findings:
        raise ReportError("new experience requires explicit findings")
    if not checks:
        raise ReportError("new experience requires explicit checks; prose such as '0 errors' is not a check")
    finding_checks = []
    finding_ids = set()
    for i, finding in enumerate(findings):
        if not isinstance(finding, dict):
            raise ReportError(f"findings[{i}] must be an object")
        finding_id = required_text(finding.get("id"), f"findings[{i}].id")
        if finding_id in finding_ids:
            raise ReportError(f"duplicate finding id: {finding_id}")
        finding_ids.add(finding_id)
        recheck = check_record(finding.get("recheck"), job_dir, f"findings[{i}].recheck")
        finding_checks.append({**recheck, "id": f"本次事项复验：{finding_id}"})
    lines = [f"# {inline(job_id)} · 作业复盘", "", f"终稿：{file_link(final_path, job_dir)}；SHA-256：`{digest}`。", f"实际用时：{time_note}。", "", summary + "。这些是输入报告中的检查状态，不是脚本作出的整图验收。"]
    # Known failures always precede pending items and successful observations.
    for status, heading in (("fail", "已知失败"), ("pending", "待核检查")):
        selected = [check for check in checks + finding_checks if check["status"] == status]
        if selected:
            lines += ["", f"## {heading}"]
            lines += [f"- {inline(check['id'])}：{inline(check['reason']) if check['reason'] else STATUS_LABELS[status]}。证据：{links(check['evidence'], job_dir)}。" for check in selected]
    lines += ["", "## 本次新增"]
    for i, finding in enumerate(findings):
        if not isinstance(finding, dict):
            raise ReportError(f"findings[{i}] must be an object")
        finding_id = required_text(finding.get("id"), f"findings[{i}].id")
        problem = required_text(finding.get("problem"), f"findings[{i}].problem")
        method = required_text(finding.get("method"), f"findings[{i}].method")
        lines += ["", f"### {inline(finding_id)}", f"问题：{inline(problem)}。方法：{inline(method)}。"]
        for stage, label in (("before", "改前"), ("after", "改后")):
            observation = finding.get(stage)
            if not isinstance(observation, dict):
                raise ReportError(f"findings[{i}].{stage} must be an object")
            measurement = required_text(observation.get("measurement"), f"findings[{i}].{stage}.measurement")
            evidence = evidence_files(observation.get("evidence", []), job_dir, f"findings[{i}].{stage}.evidence")
            if not evidence:
                raise ReportError(f"findings[{i}].{stage} requires evidence")
            lines.append(f"{label}：{inline(measurement)}。证据：{links(evidence, job_dir)}。")
        recheck = check_record(finding.get("recheck"), job_dir, f"findings[{i}].recheck")
        lines.append(f"复验：{STATUS_LABELS[recheck['status']]}。{inline(recheck['reason'])} 证据：{links(recheck['evidence'], job_dir)}。")
    lines += ["", "## 未查与经验边界"]
    lines += [f"- {inline(item)}" for item in unverified] or ["- 输入未另列未查项；具体覆盖仍以检查报告为准。"]
    lines += ["- 本案实量与模型观察保存在作业目录。用户交付反馈另记共享错题集，供以后按条件核对。", ""]
    return "\n".join(lines)


def write_report(input_path: Path, job_dir: Path, *, overwrite: bool = False) -> Path:
    job_dir = job_dir.expanduser().resolve()
    if not job_dir.is_dir():
        raise ReportError("job directory must already exist")
    if job_dir == Path(__file__).resolve().parents[1] or job_dir.name in {"references", "scripts", "agents", "assets"} and (job_dir.parent / "SKILL.md").is_file():
        raise ReportError("the skill directory is not a job directory")
    output = job_dir / "作业复盘.md"
    if output.exists() and not overwrite:
        raise ReportError("retrospective already exists; use --overwrite to update this job's record")
    data = json.loads(input_path.expanduser().resolve().read_text(encoding="utf-8"))
    content = render_report(data, job_dir)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=job_dir, prefix=".retrospective-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if data.get("user_feedback"):
            from feedback_book import record_feedback
            try:
                record_feedback(data["user_feedback"])
            except ValueError as exc:
                raise ReportError(f"用户交付反馈登记失败：{exc}") from exc
        os.replace(temporary, output)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="Explicit retrospective JSON; referenced files are relative to --job-dir.")
    parser.add_argument("--job-dir", required=True, type=Path, help="Existing job directory; only 作业复盘.md is written here.")
    parser.add_argument("--overwrite", action="store_true", help="Atomically replace this job's previous retrospective.")
    args = parser.parse_args()
    try:
        output = write_report(args.input, args.job_dir, overwrite=args.overwrite)
    except (ReportError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps({"status": "recorded", "output": str(output), "quality_verdict": "not_assessed_by_this_script"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
