"""导出 Excel：用例 / 测试点 / 评审三张表。

闸门规则：评审 gate=revise 时拒绝导出正式文件（抛 ExportBlocked），
只允许导出带"草稿"水印的版本 —— 让"评审不过不能出"成为代码里的事实，而不是流程约定。
"""

from __future__ import annotations

from pathlib import Path

from ..models import ReviewReport, RunState, TestCase, TestMap, TestPoint


class ExportBlocked(RuntimeError):
    pass


def build_workbook(state: RunState, *, force_draft: bool = False) -> tuple[object, str]:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    review: ReviewReport | None = state.review
    is_draft = bool(review and review.gate == "revise")
    if is_draft and not force_draft:
        raise ExportBlocked(
            f"评审未通过（gate=revise, score={review.score}），"
            f"存在 {sum(1 for i in review.issues if i.severity == 'blocker')} 个阻断问题，禁止导出正式用例。"
            "请先修复；确需应急可导出草稿：命令行加 --draft，"
            "控制台点「导出草稿」，或 GET /api/runs/<id>/export.xlsx?draft=1（文件名带 _draft，表内标注草稿）。"
        )

    wb = Workbook()
    head_font = Font(bold=True, color="FFFFFF")
    head_fill = PatternFill("solid", fgColor="2F5597")
    wrap = Alignment(wrap_text=True, vertical="top")

    # ---- 运行信息
    ws0 = wb.active
    ws0.title = "运行信息"
    rows = [
        ("运行 ID", state.run_id),
        ("需求文档", state.prd_id),
        ("模式", state.mode),
        ("LLM 模式", state.llm_mode),
        ("状态", state.status),
        ("评审分数", review.score if review else "未评"),
        ("闸门", review.gate if review else "-"),
        ("人工确认", "是" if state.hitl.get("confirmed") else "否"),
        ("人工改动", (state.hitl.get("diff") or {}).get("summary", "-")),
        ("测试点数", len(state.testpoints.test_points) if state.testpoints else 0),
        ("用例数", len(state.cases.cases) if state.cases else 0),
        ("采纳记录", len(state.adoption)),
        ("导出类型", "草稿" if is_draft else "正式"),
    ]
    for r, (k, v) in enumerate(rows, start=1):
        ws0.cell(row=r, column=1, value=k).font = Font(bold=True)
        ws0.cell(row=r, column=2, value=str(v))
    ws0.column_dimensions["A"].width = 16
    ws0.column_dimensions["B"].width = 60

    # ---- 用例
    ws = wb.create_sheet("用例")
    headers = ["用例ID", "关联测试点", "标题", "类别", "优先级", "前置条件", "步骤", "预期", "测试数据", "标签"]
    for c, h in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=c, value=h)
        cell.font, cell.fill = head_font, head_fill
    cases: list[TestCase] = state.cases.cases if state.cases else []
    for r, case in enumerate(cases, start=2):
        values = [
            case.id,
            case.test_point_id,
            case.title,
            case.category,
            case.priority,
            "\n".join(case.preconditions),
            "\n".join(f"{i}. {s.action}" for i, s in enumerate(case.steps, start=1)),
            "\n".join(f"{i}. {s.expected}" for i, s in enumerate(case.steps, start=1)),
            "\n".join(f"{k}={v}" for k, v in case.test_data.items()),
            ",".join(case.tags),
        ]
        for c, v in enumerate(values, start=1):
            cell = ws.cell(row=r, column=c, value=v)
            cell.alignment = wrap
    for col, width in zip("ABCDEFGHIJ", (10, 12, 44, 10, 8, 24, 44, 44, 24, 14)):
        ws.column_dimensions[col].width = width
    ws.freeze_panes = "A2"

    # ---- 测试点
    ws2 = wb.create_sheet("测试点")
    for c, h in enumerate(["测试点ID", "功能", "类别", "标题", "依据", "优先级", "证据引用"], start=1):
        cell = ws2.cell(row=1, column=c, value=h)
        cell.font, cell.fill = head_font, head_fill
    tm: TestMap | None = state.testmap_final or state.testmap_ai
    feat = {f.id: f.name for f in tm.features} if tm else {}
    points: list[TestPoint] = state.testpoints.test_points if state.testpoints else []
    for r, p in enumerate(points, start=2):
        for c, v in enumerate(
            [p.id, feat.get(p.feature_id, p.feature_id), p.category, p.title, p.rationale, p.priority,
             ",".join(s.chunk_id for s in p.sources)],
            start=1,
        ):
            ws2.cell(row=r, column=c, value=v).alignment = wrap
    for col, width in zip("ABCDEFG", (10, 16, 10, 50, 40, 8, 14)):
        ws2.column_dimensions[col].width = width
    ws2.freeze_panes = "A2"

    # ---- 评审
    ws3 = wb.create_sheet("评审")
    for c, h in enumerate(["级别", "类型", "问题", "关联"], start=1):
        cell = ws3.cell(row=1, column=c, value=h)
        cell.font, cell.fill = head_font, head_fill
    if review:
        for r, issue in enumerate(review.issues, start=2):
            for c, v in enumerate([issue.severity, issue.type, issue.message, issue.ref], start=1):
                ws3.cell(row=r, column=c, value=v).alignment = wrap
        start = len(review.issues) + 3
        ws3.cell(row=start, column=1, value="改进建议").font = Font(bold=True)
        for i, s in enumerate(review.suggestions, start=1):
            ws3.cell(row=start + i, column=1, value=f"{i}. {s}")
    for col, width in zip("ABCD", (10, 14, 80, 14)):
        ws3.column_dimensions[col].width = width

    name = f"{state.prd_id}_{state.run_id}{'_draft' if is_draft else ''}.xlsx"
    return wb, name


def export_xlsx(state: RunState, out_dir: Path, *, force_draft: bool = False) -> Path:
    wb, name = build_workbook(state, force_draft=force_draft)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / name
    wb.save(path)  # type: ignore[attr-defined]
    return path
