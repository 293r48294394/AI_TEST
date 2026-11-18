"""基准集复核录入：把你写的「通过/改/删」应用到 JSON，并自带校验。

为什么不让你手改 JSON：42 条里改错一个引号就整份解析失败，而且"改了之后有没有把
source 改坏"没人查。这里做成：**先在临时副本上应用 → 跑质检 → 0 error 才落盘**，
不合格直接拒绝写入。

给你的录入格式（一行一条，字段用 `;` 分隔）：

    G-001 通过
    G-007 改 标题=满100减20商品券与8元运费券叠加时先算商品券; 期望=应付=100.00，coupon_use 记录两笔; 分类=boundary
    G-013 删 与 G-041 场景重复，重复计入会虚高召回

字段可用：标题 / 期望 / 分类 / 优先级 / 出处 / 隐性 / 功能
`隐性` 取值 true/false；其余为文本。
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..util import read_json, write_json

VERDICTS = ("通过", "改", "删")
FIELD_ALIASES = {
    "标题": "title",
    "期望": "expectation",
    "分类": "category",
    "优先级": "priority",
    "出处": "source",
    "隐性": "implicit_rule",
    "功能": "feature",
    "说明": "note",
}
_LINE = re.compile(r"^(G-\d{3})\s+(通过|改|删)\s*(.*)$")
_CATEGORIES = {"normal", "exception", "boundary", "security", "compat", "performance"}
_PRIORITIES = {"P0", "P1", "P2"}


@dataclass
class Verdict:
    gid: str
    status: str
    note: str = ""
    fields: dict = field(default_factory=dict)


@dataclass
class ApplyReport:
    passed: list[str] = field(default_factory=list)
    edited: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    undecided: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        return (
            f"通过 {len(self.passed)} / 修改 {len(self.edited)} / 删除 {len(self.deleted)}"
            f" / 未判定 {len(self.undecided)}"
        )


def parse_verdicts(text: str) -> tuple[dict[str, Verdict], list[str]]:
    verdicts: dict[str, Verdict] = {}
    errors: list[str] = []
    # Windows 编辑器常写 BOM，不处理会导致第一行"不是以 G- 开头"
    for lineno, raw in enumerate(text.lstrip("\ufeff").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _LINE.match(line)
        if not m:
            if line.startswith("G-"):
                errors.append(f"第 {lineno} 行格式不对（缺 通过/改/删）：{line[:60]}")
            else:
                errors.append(f"第 {lineno} 行不是以 G-xxx 开头：{line[:60]}")
            continue
        gid, status, rest = m.group(1), m.group(2), m.group(3).strip()
        if gid in verdicts:
            errors.append(f"{gid} 重复出现（第 {lineno} 行）")
            continue
        verdict = Verdict(gid=gid, status=status)
        if status == "删":
            verdict.note = rest.lstrip("说明:").strip()
        elif status == "改":
            if re.search(r"G-\d{3}\s+(?:通过|改|删)", rest):
                errors.append(f"{gid} 这一行里混进了另一条判定，请一条一行")
                continue
            fields = _parse_fields(rest)
            if not fields:
                errors.append(f"{gid} 标为『改』但没写要改哪些字段（格式：标题=xxx; 期望=xxx）")
            verdict.fields = fields
            verdict.note = fields.pop("note", "")
        verdicts[gid] = verdict
    return verdicts, errors


def _parse_fields(rest: str) -> dict:
    out: dict = {}
    if not rest:
        return out
    # 允许用 ; 或 | 分隔；值里的逗号不受影响
    for part in re.split(r"[;|]", rest):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, _, value = part.partition("=")
        key, value = key.strip(), value.strip()
        target = FIELD_ALIASES.get(key)
        if not target:
            out.setdefault("_unknown_keys", []).append(key)
            continue
        if target == "implicit_rule":
            out[target] = value.lower() in {"true", "1", "是", "yes", "y"}
        else:
            out[target] = value
    return out


def apply_verdicts(
    rows: list[dict],
    verdicts: dict[str, Verdict],
    *,
    annotator: str,
    version: str = "v1.0",
    allow_partial: bool = False,
) -> tuple[list[dict], ApplyReport]:
    report = ApplyReport()
    by_id = {str(r.get("id")): r for r in rows}
    unknown = sorted(set(verdicts) - set(by_id))
    if unknown:
        report.errors.append(f"以下 id 不存在于基准集：{unknown}")

    out: list[dict] = []
    for row in rows:
        gid = str(row.get("id"))
        verdict = verdicts.get(gid)
        if verdict is None:
            report.undecided.append(gid)
            out.append(copy.deepcopy(row))
            continue
        if verdict.status == "删":
            report.deleted.append(gid)
            continue
        new_row = copy.deepcopy(row)
        if verdict.status == "改":
            for key, value in verdict.fields.items():
                if key.startswith("_"):
                    continue
                new_row[key] = value
            # 改了分类/优先级要检查枚举
            if new_row.get("category") not in _CATEGORIES:
                report.errors.append(f"{gid} 修改后的 category 非法：{new_row.get('category')}")
            if new_row.get("priority") not in _PRIORITIES:
                report.errors.append(f"{gid} 修改后的 priority 非法：{new_row.get('priority')}")
            report.edited.append(gid)
        else:
            report.passed.append(gid)
        if verdict.note:
            new_row["review_note"] = verdict.note
        out.append(new_row)

    if report.undecided and not allow_partial:
        report.errors.append(
            f"还有 {len(report.undecided)} 条未判定：{report.undecided[:10]}{'…' if len(report.undecided) > 10 else ''}"
            "（确实想只复核一部分，加 --partial）"
        )

    # 元信息：只有真正复核过才改 annotator
    payload_meta = {"annotator": annotator, "version": version, "reviewed": len(rows) - len(report.undecided)}
    out_meta = payload_meta
    return out, _with_meta(report, out_meta)


def _with_meta(report: ApplyReport, meta: dict) -> ApplyReport:
    setattr(report, "meta", meta)
    return report


def load_doc(path: Path) -> dict:
    data = read_json(path)
    if not isinstance(data, dict) or "test_points" not in data:
        raise ValueError(f"{path} 不是预期结构（需要顶层 test_points）")
    return data


def save_doc(path: Path, doc: dict, rows: list[dict], *, annotator: str, version: str) -> None:
    new_doc = dict(doc)
    new_doc["test_points"] = rows
    new_doc["annotator"] = annotator
    new_doc["version"] = version
    new_doc["review_status"] = "human-reviewed"
    write_json(path, new_doc)


def render_template(doc: dict) -> str:
    """生成待填模板：每行只有 id + 标题提示，状态留空（不会默认通过）。"""
    lines = [
        "# 基准集复核录入表",
        "# 每行格式：G-XXX 通过   |   G-XXX 改 标题=...; 期望=...; 分类=boundary   |   G-XXX 删 理由",
        "# 判定依据见 docs/review-guide.md：出处真实 / 可判定 / 可观测 / 不重复 / 分类正确",
        "# 全部写完后执行：python run.py review-apply --golden data/golden/coupon-golden.json --verdicts <本文件> --annotator 你的名字",
        "",
    ]
    for row in doc["test_points"]:
        lines.append(f"{row['id']}   # [{row.get('category')}] {row.get('title')}")
    return "\n".join(lines) + "\n"
