"""Record a user's CAD delivery feedback and read the checks for the next job."""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
from tempfile import NamedTemporaryFile
import unicodedata


MARKER = "<!-- plan-to-cad-feedback:v1 -->"
INTRO = ("# 平面一键 CAD 交付反馈错题集\n\n" + MARKER + "\n\n"
         "每条保留用户反馈及其来源。可直接编辑缩进的适用条件与核对动作。"
         "更正历史反馈时另记一条，并填写被替代编号。\n")
FIELDS = {"来源": "source", "用户反馈": "feedback", "适用条件": "condition", "核对动作": "check"}
ID = re.compile(r"CAD-[0-9a-f]{16}")


def book_location():
    config = Path.home() / ".config" / "plan-to-cad"
    value = os.environ.get("PLAN_TO_CAD_FEEDBACK_BOOK")
    if value is None:
        pointer = config / "feedback-book-path.txt"
        if not pointer.exists():
            return (config / "feedback-book.md").resolve(), False
        value = pointer.read_text(encoding="utf-8").strip()
    if not value or len(value.splitlines()) != 1 or not Path(value).is_absolute():
        raise ValueError("共享错题集配置须为单行绝对文件路径")
    return Path(value).resolve(), True


def normalize(value):
    if not isinstance(value, str):
        raise ValueError("来源、反馈、条件与核对动作均须为文字")
    value = unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n")).strip()
    if not value or "\0" in value:
        raise ValueError("错题集字段不能为空或含空字符")
    return value


def entry(value):
    if not isinstance(value, dict) or not set(FIELDS.values()).issubset(value):
        raise ValueError("每条反馈须包含 source、feedback、condition 和 check")
    if set(value) - set(FIELDS.values()) - {"supersedes"}:
        raise ValueError("反馈含不能识别的字段")
    row = {key: normalize(value[key]) for key in FIELDS.values()}
    old = value.get("supersedes")
    if old is not None:
        if not isinstance(old, str) or not ID.fullmatch(old):
            raise ValueError("supersedes 须为被替代条目的完整编号")
        row["supersedes"] = old
    identity = json.dumps([row["source"], row["feedback"]], ensure_ascii=False, separators=(",", ":"))
    row["id"] = "CAD-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return row


def encode(row):
    lines = ["", "## " + row["id"]]
    if "supersedes" in row:
        lines += ["", "替代编号 " + row["supersedes"]]
    for label, key in FIELDS.items():
        lines += ["", label, ""]
        lines.extend("    " + line for line in row[key].split("\n"))
    return "\n".join(lines) + "\n"


def apply_row(rows, replaced, row):
    key, old = row["id"], row.get("supersedes")
    if key in rows:
        if old != rows[key].get("supersedes") or any(row[k]!=rows[key][k] for k in ("source","feedback")):
            raise ValueError(f"{key} 的来源、原话或替代关系与已有记录冲突")
        return False
    if old is not None:
        if old not in rows or old in replaced:
            raise ValueError(f"{old} 不存在或已被替代，请核对当前生效编号")
        replaced.add(old)
    rows[key] = row
    return True


def decode(text):
    lines = text.split("\n")
    if MARKER not in lines[:5]:
        raise ValueError("文件缺少错题集格式标记，已保留原文件")
    rows, replaced = {}, set()
    starts = [i for i,line in enumerate(lines) if line.startswith("## ")]
    for start, stop in zip(starts, starts[1:] + [len(lines)]):
        key = lines[start][3:]
        if not ID.fullmatch(key) or key in rows:
            raise ValueError(f"错题编号无效或重复：{key}")
        fields, content = {}, {}
        current = None
        for line in lines[start+1:stop]:
            if not line:
                continue
            if line.startswith("    ") and current is not None:
                content[current].append(line[4:])
            elif line in FIELDS and FIELDS[line] not in content:
                current = FIELDS[line]
                content[current] = []
            elif line.startswith("替代编号 ") and "supersedes" not in fields:
                fields["supersedes"] = line.removeprefix("替代编号 ")
                current = None
            else:
                raise ValueError(f"{key} 中有无法解析的行，请核对字段与四个空格缩进")
        fields.update({k:"\n".join(v) for k,v in content.items()})
        row = entry(fields)
        if key != row["id"]:
            raise ValueError(f"{key} 的用户原话或来源已经改变。请保留原条并新增更正")
        apply_row(rows,replaced,row)
    return rows, replaced


@contextmanager
def writer_lock(path):
    # Lock a stable sibling: the book itself is atomically replaced on commit.
    with path.open("a+b") as lock:
        if os.name == "nt":
            import msvcrt
            if lock.seek(0, os.SEEK_END) == 0:
                lock.write(b"\0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield


def record_feedback(payload):
    batch = payload if isinstance(payload, list) else [payload]
    if not batch:
        raise ValueError("请提供至少一条用户反馈")
    additions = [entry(item) for item in batch]
    path, configured = book_location()
    if not configured:
        path.parent.mkdir(parents=True, exist_ok=True)
    # A missing explicitly configured parent can be an unavailable cloud drive.
    lockfile = path.with_name("." + path.name + ".lock")
    with writer_lock(lockfile):
        before = path.read_bytes() if path.exists() else None
        text = before.decode("utf-8") if before is not None else INTRO
        rows, replaced = decode(text)
        appended, receipts = [], []
        for row in additions:
            saved = apply_row(rows, replaced, row)
            receipts.append({"id":row["id"], "added":saved})
            if saved:
                appended.append(encode(row))
        if appended:
            temp = None
            try:
                with NamedTemporaryFile("wb", dir=path.parent, prefix="."+path.name+"-", delete=False) as f:
                    temp = Path(f.name)
                    if before is not None:
                        os.chmod(f.name,stat.S_IMODE(path.stat().st_mode))
                    separator = "" if text.endswith("\n") else "\n"
                    f.write((text+separator+"".join(appended)).encode("utf-8"))
                    f.flush()
                    os.fsync(f.fileno())
                if (path.read_bytes() if path.exists() else None) != before:
                    raise ValueError("错题集在写入期间被编辑，请按当前版本重试")
                os.replace(temp, path)
            finally:
                if temp is not None:
                    temp.unlink(missing_ok=True)
    return receipts


def read_brief():
    path, configured = book_location()
    if not path.exists():
        if configured:
            raise FileNotFoundError(f"已配置的共享错题集未找到：{path}")
        return "暂无已记录的用户交付反馈。\n"
    rows, replaced = decode(path.read_text(encoding="utf-8"))
    grouped = {}
    for key, row in rows.items():
        if key not in replaced:
            grouped.setdefault((row["condition"],row["check"]),[]).append(key)
    if not grouped:
        return "暂无已记录的用户交付反馈。\n"
    result = ["# 本次制图前核对的交付经验"]
    for (condition,check), keys in grouped.items():
        result += ["", "## " + "、".join(keys)]
        for label,value in (("适用条件",condition),("核对动作",check)):
            result += ["", label, ""]
            result.extend("    "+line for line in value.split("\n"))
    return "\n".join(result)+"\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("read", help="查看当前生效的跨户型检查")
    record = commands.add_parser("record", help="保留实际用户交付反馈")
    record.add_argument("--input", required=True, help="单条或多条 UTF-8 JSON 文件，或 - 从标准输入读取")
    args = parser.parse_args()
    if args.action == "read":
        print(read_brief(), end="")
    else:
        payload = json.load(sys.stdin) if args.input == "-" else json.loads(Path(args.input).read_text(encoding="utf-8"))
        receipts = record_feedback(payload)
        print(json.dumps(receipts,ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except (ValueError,OSError) as exc:
        sys.exit(str(exc))
