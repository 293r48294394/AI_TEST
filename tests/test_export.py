"""导出：正常导出、闸门拦截、草稿模式、工作表内容。"""

from __future__ import annotations

import pytest

from atp.export.xlsx import ExportBlocked, export_xlsx
from atp.models import Issue, ReviewReport


def test_export_after_pass(orc, state, workdir):
    done = orc.confirm_ai_draft(state.run_id)
    path = export_xlsx(done, workdir, force_draft=True)
    assert path.exists()

    from openpyxl import load_workbook

    wb = load_workbook(path)
    assert {"运行信息", "用例", "测试点", "评审"} <= set(wb.sheetnames)
    ws = wb["用例"]
    assert ws.max_row >= 2  # 表头 + 至少一条用例


def test_export_blocked_when_revise(orc, state, workdir):
    done = orc.confirm_ai_draft(state.run_id)
    done.review = ReviewReport(
        score=40,
        gate="revise",
        coverage={"normal": 1},
        issues=[Issue(type="coverage", severity="blocker", message="缺少边界用例", ref="skill2")],
        suggestions=["补齐边界"],
    )
    with pytest.raises(ExportBlocked) as exc:
        export_xlsx(done, workdir)
    assert "revise" in str(exc.value)

    # 草稿模式允许导出，文件名带 _draft
    path = export_xlsx(done, workdir, force_draft=True)
    assert "_draft" in path.name


def test_export_includes_adoption_sheet_rows(orc, state, workdir):
    done = orc.confirm_ai_draft(state.run_id)
    path = export_xlsx(done, workdir, force_draft=True)
    from openpyxl import load_workbook

    ws = load_workbook(path)["运行信息"]
    values = {ws.cell(row=r, column=1).value: ws.cell(row=r, column=2).value for r in range(1, ws.max_row + 1)}
    assert values["导出类型"] == "草稿"
    assert str(values["人工确认"]) in {"是", "否"}
