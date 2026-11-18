"""PRD 解析：.md/.txt 原生，.docx 用 stdlib 解压 + XML 提取，.pdf 可选依赖。

为什么自己解 docx：python-docx 在受限环境装不上，而 docx 本质是 zip + word/document.xml，
用 60 行标准库代码就能拿到段落文本与标题层级，对"需求文档提测"这个场景完全够用。
PDF 需要 pdfplumber/pypdf，缺失时明确报错并提示转 docx，而不是静默返回空。
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

_HEADING_NUM = re.compile(r"^(\d+(?:\.\d+){0,3})\s*[、.．]?\s*(\S.*)$")
_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


class ParseError(RuntimeError):
    pass


@dataclass
class Section:
    heading: str
    level: int
    body: str


@dataclass
class Chunk:
    chunk_id: str
    heading: str
    text: str
    order: int

    def as_dict(self) -> dict:
        return {"chunk_id": self.chunk_id, "heading": self.heading, "text": self.text, "order": self.order}


@dataclass
class ParsedPRD:
    prd_id: str
    source: str
    text: str
    sections: list[Section] = field(default_factory=list)
    chunks: list[Chunk] = field(default_factory=list)

    @property
    def stats(self) -> dict:
        return {
            "chars": len(self.text),
            "sections": len(self.sections),
            "chunks": len(self.chunks),
            "has_footnotes": bool(re.search(r"[①-⑩]", self.text)),
            "has_tables": "|" in self.text,
        }


# ------------------------------------------------------------------ 入口


def parse_file(path: Path, prd_id: str | None = None) -> ParsedPRD:
    if not path.exists():
        raise ParseError(f"文件不存在：{path}")
    suffix = path.suffix.lower()
    if suffix == ".docx":
        text = _docx_text(path)
    elif suffix == ".pdf":
        text = _pdf_text(path)
    elif suffix in {".md", ".markdown", ".txt"}:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    else:
        raise ParseError(f"暂不支持的文件类型：{suffix}（支持 .md/.txt/.docx/.pdf）")
    return parse_text(text, source=path.name, prd_id=prd_id or path.stem)


def parse_text(text: str, source: str = "inline", prd_id: str = "prd", max_chunk: int = 700) -> ParsedPRD:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) < 50:
        raise ParseError("文档内容过短（<50 字），无法作为需求文档解析")
    sections = _split_sections(text)
    chunks = _chunk(sections, max_chunk=max_chunk)
    return ParsedPRD(prd_id=prd_id, source=source, text=text, sections=sections, chunks=chunks)


# ------------------------------------------------------------- 分段 / 切块


def _split_sections(text: str) -> list[Section]:
    """按标题切段：优先 Markdown 标题，其次 Word 风格编号标题（4.2 用户领取）。"""
    sections: list[Section] = []
    heading, level, buf = "（前言）", 0, []

    def flush() -> None:
        body = "\n".join(buf).strip()
        if body:
            sections.append(Section(heading=heading, level=level, body=body))

    for line in text.split("\n"):
        md = _MD_HEADING.match(line.strip())
        if md:
            flush()
            heading, level, buf = md.group(2).strip(), len(md.group(1)), []
            continue
        # Word 里的「4.2 用户领取」：只有短行才当标题，避免把正文里的编号当标题
        stripped = line.strip()
        if 0 < len(stripped) <= 40 and _HEADING_NUM.match(stripped) and not stripped.endswith(("。", "；", "，")):
            m = _HEADING_NUM.match(stripped)
            flush()
            heading = stripped
            level = m.group(1).count(".") + 1
            buf = []
            continue
        buf.append(line)
    flush()
    return sections


def _chunk(sections: list[Section], max_chunk: int = 700) -> list[Chunk]:
    """段内按空行/列表项切块，超长再硬切，保留 heading 便于引用。"""
    chunks: list[Chunk] = []
    idx = 0
    for sec in sections:
        head = sec.heading[:60]
        body = sec.body.strip()
        if not body:
            continue
        if len(body) <= max_chunk:
            idx += 1
            chunks.append(Chunk(f"P-{idx:04d}", head, body, idx))
            continue
        paras: list[str] = []
        cur = ""
        for para in re.split(r"\n\s*\n", body):
            para = para.strip()
            if not para:
                continue
            if len(cur) + len(para) + 1 <= max_chunk:
                cur = f"{cur}\n{para}".strip()
            else:
                if cur:
                    paras.append(cur)
                if len(para) <= max_chunk:
                    cur = para
                else:
                    for i in range(0, len(para), max_chunk):
                        paras.append(para[i : i + max_chunk])
                    cur = ""
        if cur:
            paras.append(cur)
        for p in paras:
            idx += 1
            chunks.append(Chunk(f"P-{idx:04d}", head, p, idx))
    return chunks


# ------------------------------------------------------------------- docx


def _docx_text(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as zf:
            xml = zf.read("word/document.xml")
    except KeyError as exc:
        raise ParseError("docx 缺少 word/document.xml，文件可能已损坏") from exc
    except zipfile.BadZipFile as exc:
        raise ParseError("不是合法的 docx（zip）文件") from exc

    root = ET.fromstring(xml)
    lines: list[str] = []
    for para in root.iter(f"{W_NS}p"):
        style = ""
        ppr = para.find(f"{W_NS}pPr")
        if ppr is not None:
            pstyle = ppr.find(f"{W_NS}pStyle")
            if pstyle is not None:
                style = pstyle.get(f"{W_NS}val", "")
        text = "".join(t.text or "" for t in para.iter(f"{W_NS}t")).strip()
        if not text:
            continue
        m = re.search(r"(?:Heading|标题)\s*(\d)", style, re.I)
        if m:
            text = "#" * min(int(m.group(1)) + 1, 6) + " " + text
        lines.append(_flatten_table(para))
    return "\n".join(lines)


def _flatten_table(para) -> str:
    """表格单元格在 XML 里也是 w:p，这里只做文本平铺；行分隔靠原始顺序。"""
    return "".join(t.text or "" for t in para.iter(f"{W_NS}t")).strip()


# -------------------------------------------------------------------- pdf


def _pdf_text(path: Path) -> str:
    for mod in ("pdfplumber", "pypdf"):
        try:
            if mod == "pdfplumber":
                import pdfplumber  # type: ignore

                with pdfplumber.open(str(path)) as pdf:
                    return "\n".join((p.extract_text() or "") for p in pdf.pages)
            import pypdf  # type: ignore

            reader = pypdf.PdfReader(str(path))
            return "\n".join((pg.extract_text() or "") for pg in reader.pages)
        except ImportError:
            continue
    raise ParseError(
        "解析 PDF 需要 pdfplumber 或 pypdf，当前环境未安装。"
        "请把 PRD 另存为 .docx/.md，或运行 python bootstrap.py --pdf"
    )
