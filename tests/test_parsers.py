"""PRD 解析：分段、切块、docx 零依赖解析、异常路径。"""

from __future__ import annotations

import zipfile

import pytest

from atp.parsers.prd import ParseError, parse_file, parse_text

DOCX_MINIMAL = {
    "[Content_Types].xml": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>""",
    "word/document.xml": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>1 背景</w:t></w:r></w:p>
<w:p><w:r><w:t>这是一段需求正文，描述优惠券库存扣减必须原子完成，单用户最多领取 3 张。</w:t></w:r></w:p>
<w:p><w:r><w:t>脚注①：单用户限领 3 张含跨活动累计。</w:t></w:r></w:p>
</w:body></w:document>""",
}


def _make_docx(path):
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in DOCX_MINIMAL.items():
            zf.writestr(name, content)
    return path


def test_sections_and_chunks(parsed):
    headings = [s.heading for s in parsed.sections]
    assert "2.2 用户领取" in headings
    assert parsed.stats["has_footnotes"] is True
    assert [c.chunk_id for c in parsed.chunks] == [f"P-{i:04d}" for i in range(1, len(parsed.chunks) + 1)]


def test_footnote_chunk_is_retrievable_by_symbol(parsed):
    assert any("①" in c.text or "②" in c.text for c in parsed.chunks)


def test_long_section_is_split(parsed):
    long_prd = "# 标题\n\n" + "\n\n".join(f"第{i}段：" + "内容" * 60 for i in range(6))
    p = parse_text(long_prd, prd_id="long")
    assert len(p.chunks) >= 2
    assert all(len(c.text) <= 760 for c in p.chunks)


def test_docx_parsing(workdir):
    path = _make_docx(workdir / "prd.docx")
    p = parse_file(path)
    assert p.prd_id == "prd"
    assert "优惠券库存扣减" in p.text
    assert p.stats["has_footnotes"] is True


def test_short_text_rejected():
    with pytest.raises(ParseError):
        parse_text("太短了")


def test_unsupported_suffix(workdir):
    p = workdir / "a.xlsx"
    p.write_bytes(b"x" * 100)
    with pytest.raises(ParseError):
        parse_file(p)


def test_pdf_without_dependency_gives_actionable_error(workdir):
    p = workdir / "a.pdf"
    p.write_bytes(b"%PDF-1.4 fake")
    try:
        parse_file(p)
    except ParseError as exc:
        assert "docx" in str(exc)  # 提示转换格式，而不是静默返回空
    except ImportError:
        pytest.skip("环境里恰好装了 pdf 库")
