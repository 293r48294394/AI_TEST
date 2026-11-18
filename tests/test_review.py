"""复核录入：格式解析、字段替换、删除、未判定拦截、非法值拦截。"""

from __future__ import annotations

import copy

from atp.eval.golden_lint import lint
from atp.eval.review import apply_verdicts, parse_verdicts, render_template
from atp.parsers.prd import parse_text

PRD = """# 测试 PRD v1.0

## 1 背景
优惠券模块用于营销发券，日均发放 3 万张。

## 4.1 优惠券创建
运营创建券模板，字段见表 4-1。单张券面额不得超过 500 元。

表 4-1 券模板字段

① 券名称在同一活动内必须唯一。

## 4.2 用户领取
用户领取券时校验库存与限领。单用户限领 3 张①②。

## 9 待定问题
Q1：券过期后是否可恢复？A：不可恢复。
"""

ROWS = [
    {
        "id": "G-001",
        "feature": "优惠券创建",
        "category": "normal",
        "title": "创建满100减20券成功并返回待发布状态",
        "expectation": "HTTP 200，返回 couponId，状态为待发布",
        "priority": "P1",
        "source": "PRD 4.1 表 4-1",
        "implicit_rule": False,
    },
    {
        "id": "G-002",
        "feature": "用户领取",
        "category": "boundary",
        "title": "同一用户第 4 次领取同一券时报错",
        "expectation": "HTTP 400，提示已达限领上限，库存不扣减",
        "priority": "P1",
        "source": "PRD 4.2 条款 4.2.1",
        "implicit_rule": False,
    },
]


def test_parse_pass_edit_delete():
    text = """
# 注释行
G-001 通过
G-002 改 标题=同一用户第 4 次领取返回限领拦截; 期望=HTTP 400，提示已达上限; 分类=exception
"""
    verdicts, errors = parse_verdicts(text)
    assert not errors
    assert verdicts["G-001"].status == "通过"
    assert verdicts["G-002"].status == "改"
    assert verdicts["G-002"].fields["category"] == "exception"
    assert "限领拦截" in verdicts["G-002"].fields["title"]


def test_parse_rejects_bad_lines():
    _, errors = parse_verdicts("G-001 也许可以\n随便一行\nG-002")
    assert any("通过/改/删" in e for e in errors)
    assert any("不是以 G-" in e for e in errors)


def test_edit_without_fields_is_error():
    _, errors = parse_verdicts("G-001 改")
    assert errors and "没写要改哪些字段" in errors[0]


def test_apply_passes_edits_and_deletes():
    verdicts, _ = parse_verdicts("G-001 通过\nG-002 删 与 G-003 重复")
    rows, report = apply_verdicts(ROWS, verdicts, annotator="张三")
    assert report.passed == ["G-001"]
    assert report.deleted == ["G-002"]
    assert len(rows) == 1
    assert "review_note" not in rows[0]


def test_apply_keeps_original_untouched():
    verdicts, _ = parse_verdicts("G-001 改 优先级=P0")
    rows, _ = apply_verdicts(ROWS, verdicts, annotator="张三")
    assert ROWS[0]["priority"] == "P1", "必须深拷贝，不能改到原数据"
    assert rows[0]["priority"] == "P0"


def test_undecided_blocks_by_default():
    verdicts, _ = parse_verdicts("G-001 通过")
    _, report = apply_verdicts(ROWS, verdicts, annotator="张三")
    assert not report.ok
    assert "未判定" in report.errors[0]
    _, report2 = apply_verdicts(ROWS, verdicts, annotator="张三", allow_partial=True)
    assert report2.ok


def test_unknown_id_is_error():
    verdicts, _ = parse_verdicts("G-999 通过")
    _, report = apply_verdicts(ROWS, verdicts, annotator="张三")
    assert not report.ok and "不存在" in report.errors[0]


def test_invalid_enum_after_edit_is_error():
    verdicts, _ = parse_verdicts("G-001 改 分类=fun")
    _, report = apply_verdicts(ROWS, verdicts, annotator="张三", allow_partial=True)
    assert not report.ok and "category" in report.errors[0]


def test_edit_that_breaks_source_is_caught_by_lint():
    """改出处到不存在的章节，必须在落盘前被质检拦住。"""
    verdicts, _ = parse_verdicts("G-001 改 出处=PRD 12.9 条款\nG-002 通过")
    rows, report = apply_verdicts(ROWS, verdicts, annotator="张三")
    assert report.ok  # 应用层本身没意见（它只管字段）
    rep = lint(parse_text(PRD, prd_id="t"), rows)
    assert not rep.ok
    assert any(i.code == "source.unresolved" for i in rep.errors)


def test_two_verdicts_on_one_line_is_rejected():
    """一条一行，混行要明确报错，而不是悄悄吃掉第二条。"""
    verdicts, errors = parse_verdicts("G-001 改 出处=PRD 4.1 表 4-1; G-002 通过")
    assert not verdicts
    assert any("一条一行" in e for e in errors)


def test_template_lists_all_ids_without_default_verdict():
    text = render_template({"test_points": ROWS})
    assert "G-001" in text and "G-002" in text
    assert "通过" in text  # 出现在说明里
    # 模板本身不应包含可直接解析的判定，避免"一键全通过"
    verdicts, errors = parse_verdicts(text)
    assert not verdicts
