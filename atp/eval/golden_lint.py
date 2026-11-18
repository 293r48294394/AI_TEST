"""基准集质检：把"这是人工标注的"从声明变成可执行的检查。

为什么需要它：基准集如果没有被机器检查过，"人工标注"就只是一句话。
真正会毁掉评测的是四类脏数据，而且都很隐蔽：
  1. source 指向的章节/表格/脚注在 PRD 里根本不存在（凭印象写的出处）；
  2. title 不可判定（"优惠券功能正常"——测不出 pass/fail）；
  3. expectation 不可观测（"返回成功"——没有状态码/字段/文案）；
  4. 两条记录其实是同一个场景（重复计入会虚高召回）。

这里全部用确定性规则检查，输出 error/warn 两级。
error = 必须修，否则评测结论不可信；warn = 需要人判断，不阻断。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ..parsers.prd import ParsedPRD, parse_file
from ..util import read_json, similarity

CATEGORIES = ("normal", "exception", "boundary", "security", "compat", "performance")
PRIORITIES = ("P0", "P1", "P2")
REQUIRED_KEYS = ("id", "feature", "category", "title", "expectation", "priority", "source")

# 可观测信号：出现任意一个，说明期望结果能被断言
OBSERVABLE = (
    "HTTP", "状态码", "返回", "提示", "错误码", "埋点", "字段", "落库", "日志",
    "库存", "金额", "状态", "次数", "条数", "耗时", "延迟", "QPS", "P99", "余额",
    "403", "400", "404", "500", "COUPON_", "ROLE_", "ORDER_",
    "记录", "明细", "展示", "可用", "失效", "应付", "折扣", "门槛",
)
# 可判定信号：标题里要能看出"在什么条件下、看什么"
DECIDABLE = (
    "返回", "提示", "状态", "第", "恰好", "低于", "高于", "超", "等于", "不变",
    "禁止", "拒绝", "回滚", "返还", "恢复", "释放", "不计", "不会", "仅", "只",
)

SECTION_RE = re.compile(r"^\d+(?:\.\d+)*$")
TABLE_RE = re.compile(r"^\d+-\d+$")
FOOTNOTE_RE = re.compile(r"^脚注[①-⑩]?$")
QUESTION_RE = re.compile(r"^Q\d+$")
MARKERS = "①②③④⑤⑥⑦⑧⑨⑩"


@dataclass
class LintIssue:
    level: str  # error | warn
    code: str
    gid: str
    message: str


@dataclass
class LintReport:
    issues: list[LintIssue] = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    @property
    def errors(self) -> list[LintIssue]:
        return [i for i in self.issues if i.level == "error"]

    @property
    def warns(self) -> list[LintIssue]:
        return [i for i in self.issues if i.level == "warn"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def by_code(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for issue in self.issues:
            out[issue.code] = out.get(issue.code, 0) + 1
        return out


def load_rows(path: Path) -> list[dict]:
    data = read_json(path)
    rows = data["test_points"] if isinstance(data, dict) else data
    return list(rows)


def lint(prd: ParsedPRD, rows: list[dict], *, dup_threshold: float = 0.92) -> LintReport:
    rep = LintReport()

    # ---- 1. 结构与枚举
    ids = []
    for idx, row in enumerate(rows):
        gid = str(row.get("id", f"row{idx}"))
        ids.append(gid)
        missing = [k for k in REQUIRED_KEYS if k not in row or row[k] in (None, "")]
        if missing:
            rep.issues.append(LintIssue("error", "schema.missing_key", gid, f"缺少字段 {missing}"))
            continue
        if row["category"] not in CATEGORIES:
            rep.issues.append(LintIssue("error", "schema.category", gid, f"category 非法：{row['category']}"))
        if row["priority"] not in PRIORITIES:
            rep.issues.append(LintIssue("error", "schema.priority", gid, f"priority 非法：{row['priority']}"))
        if not re.fullmatch(r"G-\d{3}", gid):
            rep.issues.append(LintIssue("error", "schema.id_format", gid, "id 需形如 G-001"))

    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        rep.issues.append(LintIssue("error", "schema.duplicate_id", ",".join(sorted(dupes)), f"id 重复：{sorted(dupes)}"))
    expected_ids = [f"G-{n:03d}" for n in range(1, len(rows) + 1)]
    if ids != expected_ids:
        rep.issues.append(
            LintIssue("warn", "schema.id_sequence", "-", f"id 不是从 G-001 连续编号（实际首尾 {ids[:1]}…{ids[-1:]}）")
        )

    # ---- 2. 标题可判定 / 期望可观测
    for row in rows:
        gid = str(row.get("id", "?"))
        title = str(row.get("title", ""))
        expectation = str(row.get("expectation", ""))
        if len(title) < 10:
            rep.issues.append(LintIssue("error", "title.too_short", gid, f"标题过短，不可判定：{title!r}"))
        if not re.search(r"\d", title) and not any(k in title for k in DECIDABLE):
            rep.issues.append(
                LintIssue("error", "title.not_decidable", gid, f"标题没有具体条件/预期，无法判定 pass/fail：{title!r}")
            )
        if len(expectation) < 8 or not any(k in expectation for k in OBSERVABLE):
            rep.issues.append(
                LintIssue("warn", "expectation.not_observable", gid, f"期望结果缺少可观测信号（状态码/字段/提示/埋点）：{expectation!r}")
            )
        if "（未写）" in expectation or not expectation.strip():
            rep.issues.append(LintIssue("error", "expectation.empty", gid, "期望结果为空"))

    # ---- 3. source 可定位（最容易造假的一项）
    for row in rows:
        gid = str(row.get("id", "?"))
        source = str(row.get("source", ""))
        problems = _check_source(prd, source)
        for p in problems:
            rep.issues.append(LintIssue("error", "source.unresolved", gid, f"{p}（source={source!r}）"))

    # ---- 4. 隐性规则的自洽性
    for row in rows:
        gid = str(row.get("id", "?"))
        src = str(row.get("source", ""))
        implicit = bool(row.get("implicit_rule"))
        if implicit and "条款" in src and not any(k in src for k in ("脚注", "表", "待定", "修订", "说明")):
            rep.issues.append(
                LintIssue(
                    "warn",
                    "implicit.source_is_clause",
                    gid,
                    f"标为隐性规则，但出处只有正文条款：{src!r}（可能标错，或该补脚注/表格出处）",
                )
            )
        if not implicit and MARKERS and any(m in src for m in MARKERS):
            rep.issues.append(
                LintIssue("warn", "implicit.missing_flag", gid, f"出处含脚注符号，但 implicit_rule=false：{src!r}")
            )

    # ---- 5. 场景重复
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            sim = similarity(str(rows[i].get("title", "")), str(rows[j].get("title", "")))
            if sim >= dup_threshold:
                rep.issues.append(
                    LintIssue(
                        "error",
                        "duplicate.scenario",
                        str(rows[i].get("id")),
                        f"与 {rows[j].get('id')} 场景重复（相似度 {sim:.2f}），重复计入会虚高召回",
                    )
                )

    # ---- 统计
    rep.stats = {
        "total": len(rows),
        "by_category": {c: sum(1 for r in rows if r.get("category") == c) for c in CATEGORIES},
        "by_priority": {p: sum(1 for r in rows if r.get("priority") == p) for p in PRIORITIES},
        "implicit": sum(1 for r in rows if r.get("implicit_rule")),
        "features": len({r.get("feature") for r in rows}),
        "errors": len(rep.errors),
        "warns": len(rep.warns),
        "prd_chunks": len(prd.chunks),
    }
    return rep


# --------------------------------------------------------------- source 解析


def _check_source(prd: ParsedPRD, source: str) -> list[str]:
    """把 source 拆成"章节号 / 表号 / 脚注 / 问号"，逐个回查 PRD。"""
    if not source.strip():
        return ["source 为空"]

    tokens = [t for t in re.split(r"[\s　]+", source.strip()) if t]
    sections = {s.heading for s in prd.sections}
    problems: list[str] = []
    checked = 0
    prev = ""
    last_section_body = ""

    for token in tokens:
        token = token.strip("，,。;；")
        if not token or token in {"PRD", "条款", "表", "说明", "答复", "待定问题", "版本记录", "修订说明"}:
            prev = token
            continue
        if token.startswith("v") and re.fullmatch(r"v\d+(?:\.\d+)*", token):
            if token not in prd.text:
                problems.append(f"版本号 {token} 在 PRD 中不存在")
            checked += 1
        elif FOOTNOTE_RE.match(token):
            marker = token[-1] if len(token) > 2 else ""
            if marker and marker not in (last_section_body or prd.text):
                problems.append(f"脚注 {marker} 不在 {_section_label(last_section_body) or 'PRD'} 中")
            elif not marker and "脚注" not in prd.text and not any(m in prd.text for m in MARKERS):
                problems.append("PRD 中没有任何脚注")
            checked += 1
        elif QUESTION_RE.match(token):
            if token not in prd.text:
                problems.append(f"{token} 在 PRD 中不存在")
            checked += 1
        elif TABLE_RE.match(token):
            if f"表 {token}" not in prd.text and f"表{token}" not in prd.text:
                problems.append(f"表 {token} 在 PRD 中不存在")
            checked += 1
        elif SECTION_RE.match(token):
            body = _section_body(prd, token)
            if body is None:
                if token in prd.text:
                    checked += 1  # 子条款号（如 4.2.1），出现在正文即可
                    last_section_body = prd.text
                else:
                    problems.append(f"章节 {token} 在 PRD 中不存在")
            else:
                checked += 1
                last_section_body = body
        prev = token

    if checked == 0:
        problems.append("source 里没有任何可校验的定位符（章节号/表号/脚注）")
    return problems


def _section_label(body: str) -> str:
    return "对应章节" if body else ""


def _section_body(prd: ParsedPRD, number: str) -> str | None:
    """按章节号找正文：标题形如 `4.2 用户领取` / `3. 角色与权限`。"""
    pattern = re.compile(rf"^\s*{re.escape(number)}(?:\.|\s|$)")
    for sec in prd.sections:
        if pattern.match(sec.heading):
            return sec.body
    return None


# ----------------------------------------------------------------- 渲染


def render_markdown(rep: LintReport, title: str = "基准集质检报告") -> str:
    lines = [
        f"# {title}",
        "",
        f"- 条目数：{rep.stats.get('total')}",
        f"- 分类分布：{rep.stats.get('by_category')}",
        f"- 优先级：{rep.stats.get('by_priority')}",
        f"- 隐性规则：{rep.stats.get('implicit')}",
        f"- 覆盖功能数：{rep.stats.get('features')}",
        f"- **error：{len(rep.errors)}** ｜ warn：{len(rep.warns)}",
        "",
    ]
    if rep.errors:
        lines += ["## 必须修复（error）", ""]
        for issue in rep.errors:
            lines.append(f"- `{issue.code}` {issue.gid}：{issue.message}")
        lines.append("")
    if rep.warns:
        lines += ["## 需要人工判断（warn）", ""]
        for issue in rep.warns[:40]:
            lines.append(f"- `{issue.code}` {issue.gid}：{issue.message}")
        lines.append("")
    if not rep.issues:
        lines.append("无问题：来源可定位、标题可判定、期望可观测、无重复场景。")
    return "\n".join(lines)


def lint_files(prd_path: Path, golden_path: Path) -> LintReport:
    prd = parse_file(prd_path, prd_id=prd_path.stem)
    rows = load_rows(golden_path)
    return lint(prd, rows)


# ------------------------------------------------------------- 人工复核表单


def render_review_sheet(prd: ParsedPRD, rows: list[dict], *, max_quote: int = 180) -> str:
    """生成逐条复核表单（Markdown 备份版）。

    主推 Excel 版：`python run.py review-export` —— 表格逐行展开、保留换行、结论列下拉。
    这里的 markdown 版只作备份/快速浏览，PRD 原文用 <br> 保留换行，避免挤成一行。
    """
    from .excerpt import excerpt_for, question_of

    lines = [
        "# 基准集人工复核表单（Markdown 备份版）",
        "",
        "> **建议用 Excel 版复核**：`python run.py review-export --golden data/golden/coupon-golden.json`",
        "> 它会生成 `reports/golden-review.xlsx`，表格逐行展开、保留换行、结论列带下拉，填完可 `review-import` 导回。",
        "> 本文件的 PRD 原文列把表格压成了文本，只适合快速浏览。",
        "",
        "复核方式：逐行读「PRD 原文」→ 判断「标题/期望」是否忠实于需求 → 在结论列写 `通过` / `改` / `删`。",
        "",
        "复核完成后必须做三件事，否则这份基准集仍不能称为「人工标注」：",
        "1. 用 `review-apply` 或 `review-import` 写入（会自动改 `annotator`/`version` 并跑质检）；",
        "2. `python run.py lint-golden --prd coupon-center-prd --golden data/golden/coupon-golden.json` 必须 0 error；",
        "3. 再跑评测，README 里的数字才作数。",
        "",
        f"共 {len(rows)} 条。",
        "",
        "| ID | 类别 | 标题 | 期望结果 | 出处 | PRD 原文（节选） | 结论 |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        gid = str(row.get("id", ""))
        source = str(row.get("source", ""))
        e = excerpt_for(prd, source, question=question_of(source), max_lines=12)
        quote = "<br>".join(_md(line) for line in e.lines)
        if len(quote) > max_quote * 4:
            quote = quote[: max_quote * 4] + "…"
        lines.append(
            f"| {gid} | {row.get('category', '')} | {_md(row.get('title', ''))} | {_md(row.get('expectation', ''))} | "
            f"{_md(source)} | {quote} |  |"
        )
    lines += [
        "",
        "> 判定口径：标题必须能测出 pass/fail（有条件、有预期）；期望必须可观测（状态码/提示/字段/埋点/数值）。",
        "> 若某条在 PRD 里找不到依据，直接标「删」——它会让召回率虚高。",
    ]
    return "\n".join(lines)


def _md(text: str) -> str:
    return str(text).replace("|", "／").replace("\n", " ")
