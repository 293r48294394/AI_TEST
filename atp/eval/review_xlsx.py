"""Excel 复核表：导出可读原文 + 导入判定，闭环。

为什么是 Excel 而不是让人改 JSON：
- 42 行、每行要看 PRD 原文表格，Excel 的合并/换行/筛选最顺手；
- 结论列用下拉，避免"通过/已通过/OK"这类自由写法；
- 填完直接读回来应用，中间不需要手抄或转换格式。

工作表：
  复核  —— ID | 类别 | 隐性 | 标题 | 期望结果 | 出处 | 结论 | 修改内容 | PRD 原文（可读）
  说明  —— 判定五条、填写方式、复核人单元格
"""

from __future__ import annotations

import re
from pathlib import Path

from ..parsers.prd import ParsedPRD
from .excerpt import excerpt_for, question_of
from .review import Verdict, _parse_fields

HEADERS = ["ID", "类别", "隐性", "标题", "期望结果", "出处", "结论", "修改内容（结论=改 时填）", "PRD 原文（可读）"]
VERDICT_OPTIONS = ("通过", "改", "删")
WIDTHS = dict(A=9, B=11, C=7, D=36, E=46, F=22, G=9, H=38, I=64)

CRITERIA = [
    ("判定五条（任一条不过，才判「改」或「删」）", ""),
    ("1. 出处真实且支持它", "PRD 原文那一句，真的能推出这条测试点吗？出处写错→改；原文没这条规则→删"),
    ("2. 可判定", "标题里有「什么条件 + 看什么结果」吗？没有→改标题"),
    ("3. 可观测", "期望里有能断言的东西吗（状态码/字段/提示文案/埋点/数值）？没有→改期望"),
    ("4. 不重复", "和别条是不是同一场景（看语义）？重复→留更具体的一条，删另一条"),
    ("5. 分类正确", "normal/exception/boundary/security/compat/performance 选对了吗？边界=合法范围两端，异常=非法输入或状态"),
    ("", ""),
    ("怎么填", ""),
    ("结论列", "下拉选 通过 / 改 / 删；留空视为未判定，导入时会报错"),
    ("修改内容列", "结论=改 时必填，格式：标题=xxx; 期望=xxx; 分类=boundary（可只写要改的字段）"),
    ("删的理由", "直接写在「修改内容」列，便于以后回溯"),
    ("", ""),
    ("导入命令", 'python run.py review-import --golden data/golden/coupon-golden.json --xlsx reports/golden-review.xlsx --annotator "你的名字"'),
    ("说明", "导入前会自动跑基准集质检，0 error 才落盘；改了出处但章节不存在会被拦下"),
]


def export_review_xlsx(prd: ParsedPRD, doc: dict, path: Path, *, annotator: str = "") -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.worksheet.datavalidation import DataValidation

    wb = Workbook()
    ws = wb.active
    ws.title = "复核"

    head_font = Font(bold=True, color="FFFFFF")
    head_fill = PatternFill("solid", fgColor="2F5597")
    thin = Side(style="thin", color="D9D9D9")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    wrap = Alignment(wrap_text=True, vertical="top")

    for col, name in enumerate(HEADERS, start=1):
        cell = ws.cell(row=1, column=col, value=name)
        cell.font, cell.fill = head_font, head_fill
        cell.alignment = Alignment(wrap_text=True, vertical="center", horizontal="center")
        cell.border = border
    for letter, width in WIDTHS.items():
        ws.column_dimensions[letter].width = width
    ws.freeze_panes = "C2"

    dv = DataValidation(type="list", formula1='"通过,改,删"', allow_blank=True, showDropDown=False)
    dv.error = "只能填 通过 / 改 / 删"
    dv.prompt = "选：通过 / 改 / 删"
    ws.add_data_validation(dv)

    seen: dict[str, int] = {}
    for idx, row in enumerate(doc.get("test_points", []), start=2):
        source = str(row.get("source", ""))
        excerpt = excerpt_for(prd, source, question=question_of(source))
        values = [
            row.get("id", ""),
            row.get("category", ""),
            "是" if row.get("implicit_rule") else "否",
            row.get("title", ""),
            row.get("expectation", ""),
            source,
            "",
            "",
            excerpt.text(),
        ]
        for col, value in enumerate(values, start=1):
            cell = ws.cell(row=idx, column=col, value=value)
            cell.alignment = wrap
            cell.border = border
        ws.cell(row=idx, column=1).font = Font(bold=True)
        ws.row_dimensions[idx].height = 118 if len(excerpt.text()) > 120 else 74
        seen[str(row.get("id"))] = idx
        dv.add(ws.cell(row=idx, column=7))

    # ---- 说明页
    ws2 = wb.create_sheet("说明")
    ws2.column_dimensions["A"].width = 26
    ws2.column_dimensions["B"].width = 96
    ws2["A1"], ws2["B1"] = "复核人", annotator
    ws2["A1"].font = Font(bold=True)
    ws2["B1"].fill = PatternFill("solid", fgColor="FFF2CC")
    ws2["A3"], ws2["B3"] = "共", f"{len(doc.get('test_points', []))} 条"
    for i, (left, right) in enumerate(CRITERIA, start=5):
        ws2.cell(row=i, column=1, value=left).font = Font(bold=True) if left and not left[0].isdigit() else Font()
        ws2.cell(row=i, column=2, value=right).alignment = wrap

    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


def read_verdicts_xlsx(path: Path) -> tuple[dict[str, Verdict], str, list[str]]:
    """读回判定：返回 (verdicts, annotator, errors)。"""
    from openpyxl import load_workbook

    wb = load_workbook(path, data_only=True)
    if "复核" not in wb.sheetnames:
        return {}, "", ["工作簿里没有『复核』工作表"]
    ws = wb["复核"]
    annotator = ""
    if "说明" in wb.sheetnames:
        annotator = str(wb["说明"]["B1"].value or "").strip()

    header = [str(c.value or "").strip() for c in ws[1]]
    try:
        col_id = header.index("ID") + 1
        col_verdict = header.index("结论") + 1
        col_note = header.index("修改内容（结论=改 时填）") + 1
    except ValueError as exc:
        return {}, annotator, [f"表头不符合预期（缺 ID/结论/修改内容）：{exc}"]

    verdicts: dict[str, Verdict] = {}
    errors: list[str] = []
    for row in range(2, ws.max_row + 1):
        gid = str(ws.cell(row=row, column=col_id).value or "").strip()
        if not gid:
            continue
        raw = str(ws.cell(row=row, column=col_verdict).value or "").strip()
        note = str(ws.cell(row=row, column=col_note).value or "").strip()
        if not raw:
            continue
        if raw not in VERDICT_OPTIONS:
            errors.append(f"{gid} 结论非法：{raw!r}（只能是 通过/改/删）")
            continue
        verdict = Verdict(gid=gid, status=raw)
        if raw == "改":
            fields = _parse_fields(note)
            if not fields:
                errors.append(f"{gid} 结论=改，但『修改内容』为空。格式：标题=xxx; 期望=xxx")
                continue
            verdict.fields = fields
            verdict.note = fields.pop("note", "")
        elif raw == "删":
            verdict.note = note
        verdicts[gid] = verdict
    return verdicts, annotator, errors
