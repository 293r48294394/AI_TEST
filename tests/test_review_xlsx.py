"""可读摘录与 Excel 复核表：可读性、导出导入闭环、闸门。"""

from __future__ import annotations

from pathlib import Path

import pytest

from atp.eval.excerpt import excerpt_for, format_body, question_of
from atp.eval.golden_lint import lint
from atp.eval.review import apply_with_gate
from atp.eval.review_xlsx import export_review_xlsx, read_verdicts_xlsx
from atp.parsers.prd import parse_file, parse_text

ROOT = Path(__file__).resolve().parent.parent
PRD_PATH = ROOT / "data" / "prd" / "coupon-center-prd.md"
GOLDEN_PATH = ROOT / "data" / "golden" / "coupon-golden.json"


@pytest.fixture(scope="module")
def prd():
    return parse_file(PRD_PATH, prd_id="coupon-center-prd")


@pytest.fixture(scope="module")
def doc():
    from atp.eval.review import load_doc

    return load_doc(GOLDEN_PATH)


# ------------------------------------------------------------------ excerpt


def test_table_rows_are_readable():
    body = "| 字段 | 类型 |\n| --- | --- |\n| 券名称 | 文本 |"
    lines = format_body(body)
    assert "　字段 ｜ 类型" in lines
    assert "　券名称 ｜ 文本" in lines
    assert all("---" not in ln for ln in lines)
    assert all("|" not in ln for ln in lines)


def test_excerpt_locates_section_and_keeps_table(prd):
    e = excerpt_for(prd, "PRD 4.1 表 4-1")
    assert "4.1" in e.section
    text = e.text()
    assert "券名称 ｜ 文本 ｜ 是" in text
    assert "|" not in text


def test_excerpt_filters_by_question(prd):
    e = excerpt_for(prd, "PRD 9 待定问题 Q2 答复", question=question_of("PRD 9 待定问题 Q2 答复"))
    assert "Q2" in e.text()
    assert "Q1" not in e.text()


def test_excerpt_falls_back_when_section_missing(prd):
    e = excerpt_for(prd, "PRD 12.9 条款 12.9.1")
    assert "未定位" in e.section


def test_excerpt_clips_long_section(prd):
    e = excerpt_for(prd, "PRD 4.1 表 4-1", max_lines=3)
    assert len(e.lines) == 4 and "截断" in e.lines[-1]


# -------------------------------------------------------------------- xlsx


def test_export_has_expected_headers_and_readable_cell(prd, doc, workdir):
    path = workdir / "review.xlsx"
    export_review_xlsx(prd, doc, path)
    from openpyxl import load_workbook

    wb = load_workbook(path)
    assert set(wb.sheetnames) == {"复核", "说明"}
    ws = wb["复核"]
    header = [c.value for c in ws[1]]
    assert header[0] == "ID" and "结论" in header and "PRD 原文（可读）" in header
    cell = ws["I2"].value
    assert "券名称 ｜ 文本" in cell
    assert "\n" in cell, "必须保留换行，Excel 里才能逐行读"


def test_roundtrip_export_fill_import(prd, doc, workdir):
    path = workdir / "review.xlsx"
    export_review_xlsx(prd, doc, path)

    from openpyxl import load_workbook

    wb = load_workbook(path)
    ws = wb["复核"]
    ws["G2"] = "通过"
    ws["G4"] = "改"
    ws["H4"] = "期望=HTTP 200，返回 couponId，门槛落库 0.00"
    ws["G7"] = "删"
    ws["H7"] = "与 G-012 重复"
    wb["说明"]["B1"] = "张三"
    wb.save(path)

    verdicts, annotator, errors = read_verdicts_xlsx(path)
    assert annotator == "张三"
    assert not errors
    # 行 2 = G-001，行 4 = G-003，行 7 = G-006
    assert verdicts["G-001"].status == "通过"
    assert verdicts["G-003"].status == "改"
    assert "门槛落库" in verdicts["G-003"].fields["expectation"]
    assert verdicts["G-006"].status == "删"

    rows, report, lint_report = apply_with_gate(
        prd=prd, doc=doc, verdicts=verdicts, annotator="张三", allow_partial=True
    )
    assert report.ok and lint_report is not None
    assert report.passed == ["G-001"] and report.edited == ["G-003"] and report.deleted == ["G-006"]
    # 应用后仍须通过质检
    assert lint(prd, rows).ok


def test_invalid_verdict_in_sheet_is_reported(prd, doc, workdir):
    path = workdir / "bad.xlsx"
    export_review_xlsx(prd, doc, path)
    from openpyxl import load_workbook

    wb = load_workbook(path)
    wb["复核"]["G2"] = "已通过"  # 自由写法
    wb.save(path)
    verdicts, _, errors = read_verdicts_xlsx(path)
    assert not verdicts
    assert any("非法" in e for e in errors)


def test_edit_without_content_is_reported(prd, doc, workdir):
    path = workdir / "noedit.xlsx"
    export_review_xlsx(prd, doc, path)
    from openpyxl import load_workbook

    wb = load_workbook(path)
    wb["复核"]["G2"] = "改"
    wb.save(path)
    verdicts, _, errors = read_verdicts_xlsx(path)
    assert not verdicts
    assert any("修改内容" in e for e in errors)


def test_edit_that_breaks_source_is_blocked_by_gate(prd, doc, workdir):
    path = workdir / "brokensource.xlsx"
    export_review_xlsx(prd, doc, path)
    from openpyxl import load_workbook

    wb = load_workbook(path)
    ws = wb["复核"]
    ws["G2"] = "改"
    ws["H2"] = "出处=PRD 12.9 条款"
    wb.save(path)
    verdicts, _, errors = read_verdicts_xlsx(path)
    assert not errors
    rows, report, lint_report = apply_with_gate(prd=prd, doc=doc, verdicts=verdicts, annotator="张三", allow_partial=True)
    assert report.ok
    assert lint_report is not None and not lint_report.ok
    assert any(i.code == "source.unresolved" for i in lint_report.errors)
