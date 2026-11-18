"""确定性质量规则：覆盖检查、查重、可追溯性、空话检测。

这一层刻意用规则实现，而不是交给 LLM 打分：
- 规则可单测、可复现、结果稳定 —— 面试官问"你怎么保证评审不是另一个随机数"时能答；
- LLM 只负责规则做不了的语义判断（语义盲区、步骤是否真的可执行）。
规则给出的分数有明确构成，可以逐条解释为什么扣分。
"""

from __future__ import annotations

from ..models import CATEGORIES, Issue, TestCase, TestMap, TestPoint
from ..util import similarity, tokens

REQUIRED_MIN = {"normal": 3, "exception": 3, "boundary": 3, "security": 1, "compat": 1, "performance": 1}

# 空话词：出现且没有数值/状态时，判为不可执行的模糊描述
VAGUE_WORDS = ("正常", "符合预期", "正确", "无误", "成功即可", "显示正常", "功能正常", "能够正常")
# 可观测信号：数值、状态、码值、单位
OBSERVABLE = ("提示", "错误码", "状态码", "HTTP", "200", "400", "403", "500", "库存", "埋点",
              "字段", "金额", "记录", "日志", "秒", "分钟", "元", "%", "条", "张", "次")

SEVERITY_WEIGHT = {"blocker": 20, "major": 8, "minor": 3}
GATE_MIN_SCORE = 75


def coverage_by_category(items) -> dict[str, int]:
    counts = {c: 0 for c in CATEGORIES}
    for item in items:
        counts[item.category] = counts.get(item.category, 0) + 1
    return counts


def coverage_by_feature(items, tm: TestMap) -> dict[str, int]:
    counts = {f.id: 0 for f in tm.features}
    for item in items:
        key = getattr(item, "feature_id", None) or _via_point(item, tm)
        if key in counts:
            counts[key] += 1
    return counts


def _via_point(case: TestCase, tm: TestMap) -> str:
    return ""


def find_duplicates(items, threshold: float = 0.86) -> list[tuple[str, str, float]]:
    dupes: list[tuple[str, str, float]] = []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            sim = similarity(items[i].title, items[j].title)
            if sim >= threshold:
                dupes.append((items[i].id, items[j].id, sim))
    return dupes


def is_vague(text: str) -> bool:
    """模糊判定：既没有可观测信号，又只由空话构成。"""
    if any(sig in text for sig in OBSERVABLE):
        return False
    if any(w in text for w in VAGUE_WORDS):
        return True
    return len(tokens(text)) < 4


def review_rules(
    tm: TestMap,
    points: list[TestPoint],
    cases: list[TestCase],
) -> tuple[list[Issue], dict[str, int], int]:
    issues: list[Issue] = []

    # 1) 六类覆盖
    counts = coverage_by_category(points)
    for cat, need in REQUIRED_MIN.items():
        if counts.get(cat, 0) < need:
            issues.append(
                Issue(
                    type="coverage",
                    severity="major" if cat in {"security", "performance"} else "blocker",
                    message=f"测试点类别 `{cat}` 仅 {counts.get(cat, 0)} 条，低于门槛 {need} 条",
                    ref="skill2",
                )
            )

    # 2) 功能覆盖：任一功能 0 条测试点 → 阻断
    feat_counts = coverage_by_feature(points, tm)
    empty = [fid for fid, n in feat_counts.items() if n == 0]
    if empty:
        issues.append(
            Issue(type="coverage", severity="blocker", message=f"以下功能模块没有任何测试点：{empty}", ref="skill2")
        )

    # 3) 用例↔测试点可追溯
    point_by_id = {p.id: p for p in points}
    orphan = [c.id for c in cases if c.test_point_id not in point_by_id]
    if orphan:
        issues.append(
            Issue(type="traceability", severity="blocker", message=f"以下用例指向不存在的测试点：{orphan}", ref="skill3")
        )
    uncovered = [pid for pid in point_by_id if pid not in {c.test_point_id for c in cases}]
    if uncovered:
        issues.append(
            Issue(type="traceability", severity="major", message=f"以下测试点没有对应用例：{uncovered}", ref="skill3")
        )

    # 4) 查重（跨用例标题相似度）
    for a, b, sim in find_duplicates(cases):
        issues.append(
            Issue(type="duplicate", severity="major", message=f"用例 {a} 与 {b} 高度相似（{sim:.2f}），疑似重复", ref=a)
        )

    # 5) 空话/不可执行
    vague_cases = [c.id for c in cases if is_vague(c.title)]
    if vague_cases:
        issues.append(
            Issue(type="vague", severity="major", message=f"以下用例标题缺少可观测信号，不可执行：{vague_cases}", ref="skill3")
        )
    vague_steps = [
        c.id for c in cases if any(is_vague(step.expected) and len(step.expected) < 12 for step in c.steps)
    ]
    if vague_steps:
        issues.append(
            Issue(type="vague", severity="minor", message=f"以下用例存在空泛预期（如「正常显示」）：{vague_steps}", ref="skill3")
        )

    # 6) 格式：test_data 缺失 / 步骤过少
    no_data = [c.id for c in cases if not c.test_data]
    if no_data:
        issues.append(
            Issue(type="format", severity="minor", message=f"以下用例 test_data 为空，可执行性不足：{no_data[:10]}", ref="skill3")
        )

    # 7) 证据缺失（仅提示，不阻断：无检索模式下合法）
    no_src = [p.id for p in points if not p.sources]
    if no_src:
        issues.append(
            Issue(
                type="hallucination",
                severity="minor",
                message=f"以下测试点没有证据引用，无法追溯依据：{no_src[:10]}",
                ref="skill2",
            )
        )

    score = max(0, 100 - sum(SEVERITY_WEIGHT.get(i.severity, 0) for i in issues))
    return issues, counts, score


def gate_of(issues: list[Issue], score: int) -> str:
    blockers = [i for i in issues if i.severity == "blocker"]
    return "pass" if not blockers and score >= GATE_MIN_SCORE else "revise"
