"""基准集质检器：每条规则都要能真的抓到脏数据（否则它只是装饰）。"""

from __future__ import annotations

import copy
from pathlib import Path

from atp.eval.golden_lint import lint, lint_files, load_rows
from atp.parsers.prd import parse_text

PRD = """# 测试 PRD v1.0

## 1. 背景
优惠券模块用于营销发券，日均发放 3 万张。

## 4.1 优惠券创建
运营创建券模板，字段见表 4-1。单张券面额不得超过 500 元。

表 4-1 券模板字段

① 券名称在同一活动内必须唯一。

## 4.2 用户领取
用户领取券时校验库存与限领。单用户限领 3 张①②。

## 4.3 下单使用
结算页展示可用券，折扣券与满减券互斥。

## 9 待定问题
Q1：券过期后是否可恢复？A：不可恢复。
"""

GOOD = {
    "id": "G-001",
    "feature": "优惠券创建",
    "category": "normal",
    "title": "创建满100减20券成功并返回待发布状态",
    "expectation": "HTTP 200，返回 couponId，状态为待发布，字段逐项落库",
    "priority": "P1",
    "source": "PRD 4.1 表 4-1",
    "implicit_rule": False,
}


def _prd():
    return parse_text(PRD, prd_id="t")


def _row(**kw):
    row = copy.deepcopy(GOOD)
    row.update(kw)
    return row


def _codes(rows):
    return {i.code for i in lint(_prd(), rows).issues}


def test_good_row_passes():
    rep = lint(_prd(), [_row()])
    assert rep.ok, rep.issues
    assert rep.stats["total"] == 1


def test_missing_key():
    row = _row()
    del row["expectation"]
    assert "schema.missing_key" in _codes([row])


def test_bad_enums_and_id():
    codes = _codes([_row(id="X-1", category="fun", priority="P9")])
    assert {"schema.category", "schema.priority", "schema.id_format"} <= codes


def test_duplicate_id():
    rows = [_row(), _row(title="创建满200减30券成功并返回待发布状态")]
    assert "schema.duplicate_id" in _codes(rows)


def test_short_and_vague_title():
    assert "title.too_short" in _codes([_row(title="短标题")])
    assert "title.not_decidable" in _codes([_row(title="优惠券功能正常可用", expectation="HTTP 200，返回成功")])


def test_empty_expectation():
    assert "expectation.empty" in _codes([_row(expectation="")])


def test_source_table_must_exist():
    assert "source.unresolved" in _codes([_row(source="PRD 4.1 表 9-9")])


def test_source_section_must_exist():
    assert "source.unresolved" in _codes([_row(source="PRD 12.3 条款 12.3.1")])


def test_source_without_locator_is_rejected():
    assert "source.unresolved" in _codes([_row(source="产品需求文档里写的")])


def test_footnote_must_exist_in_section():
    # 4.3 章节没有 ⑦ 脚注
    assert "source.unresolved" in _codes([_row(source="PRD 4.3 脚注⑦")])


def test_duplicate_scenarios():
    rows = [_row(), _row(id="G-002", title="创建满100减20券成功并返回待发布状态")]
    assert "duplicate.scenario" in _codes(rows)


def test_implicit_flag_consistency():
    codes = _codes([_row(implicit_rule=True, source="PRD 4.1 条款 4.1.1")])
    assert "implicit.source_is_clause" in codes
    codes2 = _codes([_row(implicit_rule=False, source="PRD 4.2 脚注①")])
    assert "implicit.missing_flag" in codes2


def test_shipped_golden_set_is_clean():
    """仓库里这份基准集必须零 error —— 这是它能不能被引用的前提。"""
    root = Path(__file__).resolve().parent.parent
    rep = lint_files(root / "data" / "prd" / "coupon-center-prd.md", root / "data" / "golden" / "coupon-golden.json")
    assert rep.ok, [f"{i.gid}: {i.message}" for i in rep.errors]


def test_shipped_golden_distribution_matches_contract():
    root = Path(__file__).resolve().parent.parent
    rows = load_rows(root / "data" / "golden" / "coupon-golden.json")
    assert 30 <= len(rows) <= 50, "基准集规模应在 30–50 条"
    cats = {r["category"] for r in rows}
    assert cats == {"normal", "exception", "boundary", "security", "compat", "performance"}
    assert sum(1 for r in rows if r.get("implicit_rule")) >= 12, "隐性规则应至少 12 条"
