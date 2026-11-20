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
    # 提示必须指向一个真实存在的入口，否则用户被指到死路
    assert "--draft" in str(exc.value)

    # 草稿模式允许导出，文件名带 _draft
    path = export_xlsx(done, workdir, force_draft=True)
    assert "_draft" in path.name


def test_human_edit_sources_are_validated():
    """控制台提交的 sources 必须被正规化成 SourceRef；塞垃圾要报错。

    背景：直接向 list[SourceRef] append 原始 dict 会绕过校验（pydantic 列表原地修改
    不校验），所以人工编辑入口一定要走 model_validate。
    """
    from atp.models import FeatureItem, SourceRef

    ok = FeatureItem.model_validate(
        {"id": "F-01", "name": "券模板", "summary": "", "sources": [{"chunk_id": "P-0004", "quote": "2–20 字"}]}
    )
    assert isinstance(ok.sources[0], SourceRef)
    assert ok.sources[0].chunk_id == "P-0004"

    with pytest.raises(Exception):
        FeatureItem.model_validate({"id": "F-01", "name": "券模板", "summary": "", "sources": [{"foo": 1}]})


def test_export_includes_adoption_sheet_rows(orc, state, workdir):
    done = orc.confirm_ai_draft(state.run_id)
    path = export_xlsx(done, workdir, force_draft=True)
    from openpyxl import load_workbook

    ws = load_workbook(path)["运行信息"]
    values = {ws.cell(row=r, column=1).value: ws.cell(row=r, column=2).value for r in range(1, ws.max_row + 1)}
    assert values["导出类型"] == "草稿"
    assert str(values["人工确认"]) in {"是", "否"}
