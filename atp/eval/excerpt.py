"""把 PRD 原文渲染成「人能读」的摘录。

问题背景：复核表里如果把 markdown 原始字符（`| 字段 | 类型 |`、`| --- |`）直接塞进一个单元格，
表格就会被压成一串 `/ --- / ---`，人根本没法对着它判断。这里做的是**轻量格式化**：
去掉分隔行、把表格逐行摊开成 `字段 ｜ 类型 ｜ 必填`，保留换行，供 Excel / HTML 渲染。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from ..parsers.prd import ParsedPRD
from .golden_lint import SECTION_RE, TABLE_RE, QUESTION_RE

_SEP_ROW = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")
_MAX_LINES = 48


@dataclass
class Excerpt:
    section: str
    lines: list[str]

    def text(self) -> str:
        return "\n".join(self.lines)


def locate_section(prd: ParsedPRD, source: str) -> tuple[str, str] | None:
    """按 source 里的章节号找到 (标题, 正文)；找不到返回 None。"""
    numbers = [t for t in re.split(r"[\s　]+", source or "") if SECTION_RE.match(t.strip("，,。;；"))]
    # 优先 1 位/2 位段号（4.1），跳过 3 段子条款（4.2.1）
    numbers.sort(key=lambda s: s.count("."))
    for num in numbers:
        num = num.strip("，,。;；")
        pattern = re.compile(rf"^\s*{re.escape(num)}(?:\.|\s|$)")
        for sec in prd.sections:
            if pattern.match(sec.heading):
                return sec.heading, sec.body
    return None


def table_ids(source: str) -> list[str]:
    return [t for t in re.split(r"[\s　]+", source or "") if TABLE_RE.match(t.strip("，,。;；"))]


def format_body(body: str) -> list[str]:
    """markdown 表格 → 逐行文本；去掉分隔行；压缩空行。"""
    out: list[str] = []
    for raw in body.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if _SEP_ROW.match(line) and "-" in line:
            continue  # | --- | --- | 这种分隔行
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            cells = [c for c in cells if c != ""]
            if not cells:
                continue
            out.append("　" + " ｜ ".join(cells))
            continue
        # 去掉 markdown 标题符号，保留层级感
        out.append(re.sub(r"^#{1,6}\s*", "", line).strip())
    return out


def excerpt_for(prd: ParsedPRD, source: str, *, question: str | None = None, max_lines: int = _MAX_LINES) -> Excerpt:
    """按 source 生成可读摘录；找不到章节时退化为"包含该表的块"。"""
    located = locate_section(prd, source)
    if located:
        heading, body = located
        lines = format_body(body)
        if question:
            lines = [ln for ln in lines if question in ln] or lines
        return Excerpt(heading, _clip(lines, max_lines))

    # 退化路径：按表号在块里找
    for tid in table_ids(source):
        for chunk in prd.chunks:
            if f"表 {tid}" in chunk.text:
                return Excerpt(chunk.heading, _clip(format_body(chunk.text), max_lines))

    if question:
        for chunk in prd.chunks:
            if question in chunk.text:
                return Excerpt(chunk.heading, _clip(format_body(chunk.text), max_lines))

    return Excerpt("（未定位到出处）", ["source 里的章节/表号在 PRD 中没有找到，需要人工确认出处是否正确。"])


def _clip(lines: Iterable[str], max_lines: int) -> list[str]:
    rows = list(lines)
    if len(rows) > max_lines:
        return rows[:max_lines] + [f"……（已截断，共 {len(rows)} 行，完整内容见 PRD 原文）"]
    return rows


def question_of(source: str) -> str | None:
    for token in re.split(r"[\s　]+", source or ""):
        if QUESTION_RE.match(token.strip("，,。;；")):
            return token.strip("，,。;；")
    return None
