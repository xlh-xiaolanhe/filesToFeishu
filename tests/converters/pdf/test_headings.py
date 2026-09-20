from pathlib import Path

from reportlab.pdfgen import canvas

from files_to_feishu.converters.pdf.headings import assign_heading_levels
from files_to_feishu.models import Element, ParsedDocument


def heading(text, size=0, page=1, level=1):
    return Element(
        kind="heading",
        text=text,
        page=page,
        level=level,
        bbox=[30, 40, 270, 40 + size] if size else [],
    )


def test_hierarchy_survives_page_breaks_and_numbering_restarts():
    elements = [
        heading("七、常用类型与语法", 28),
        heading("8. type", 24),
        heading("1. 基本用法", 16),
        heading("2. 联合类型", 16, page=2),
        heading("9. 一个特殊情况", 24, page=3),
        heading("代码段1（正常）", 16, page=4),
        heading("代码段2（特殊）", 16, page=4),
        heading("为什么会这样？", 16, page=4),
        heading("10. 复习类相关知识", 24, page=5),
        heading("八、泛型", 28, page=6),
    ]
    parsed = ParsedDocument(pages=6, elements=elements)
    before = parsed.model_copy(deep=True)
    result = assign_heading_levels(parsed, {})
    assert [e.level for e in result.elements] == [1, 2, 3, 3, 2, 3, 3, 3, 2, 1]
    assert [e.text for e in result.elements] == [e.text for e in before.elements]
    assert assign_heading_levels(result, {}) == result


def test_explicit_levels_and_non_heading_content_are_preserved():
    elements = [
        heading("Title", 30),
        heading("Section", 20, level=2),
        heading("Detail", 16, level=3),
        Element(kind="code", page=1, text="1.2.3"),
    ]
    parsed = ParsedDocument(pages=1, elements=elements)
    assert assign_heading_levels(parsed, {}) == parsed.model_copy(deep=True)
    flat = ParsedDocument(
        pages=1, elements=[heading("First", 20, level=2), heading("Second", 16, level=2)]
    )
    assign_heading_levels(flat, {})
    assert [e.level for e in flat.elements] == [2, 2]


def test_decimal_numbering_retains_parent_even_with_same_font():
    parsed = ParsedDocument(
        pages=1,
        elements=[
            heading("七、语法", 28),
            heading("14. 比较", 24),
            heading("14.1. 类型", 24),
            heading("14.2. 接口", 24),
            heading("15. 其它", 24),
        ],
    )
    assign_heading_levels(parsed, {})
    assert [e.level for e in parsed.elements] == [1, 2, 3, 3, 2]


def test_numbering_is_a_conservative_fallback_without_geometry():
    parsed = ParsedDocument(
        pages=2,
        elements=[
            heading("七、语法"),
            heading("9. 特殊情况"),
            heading("代码段1", page=2),
            heading("代码段2", page=2),
            heading("10. 其它", page=2),
        ],
    )
    assign_heading_levels(parsed, {})
    assert [e.level for e in parsed.elements] == [1, 2, 3, 3, 2]


def test_native_font_sizes_override_misleading_multiline_box_heights(tmp_path: Path):
    source = tmp_path / "headings.pdf"
    pdf = canvas.Canvas(str(source), pagesize=(300, 400))
    pdf.setFont("Helvetica-Bold", 28)
    pdf.drawString(30, 355, "Document title")
    pdf.setFont("Helvetica-Bold", 24)
    pdf.drawString(30, 300, "1. Section")
    pdf.setFont("Helvetica-Bold", 16)
    pdf.drawString(30, 245, "Long detail heading")
    pdf.drawString(30, 220, "on two lines")
    pdf.setFont("Helvetica-Bold", 24)
    pdf.drawString(30, 160, "2. Next section")
    pdf.save()
    parsed = ParsedDocument(
        pages=1,
        elements=[
            Element(kind="heading", page=1, text="Document title", bbox=[25, 18, 270, 48]),
            Element(kind="heading", page=1, text="1. Section", bbox=[25, 75, 270, 103]),
            Element(
                kind="heading",
                page=1,
                text="Long detail heading on two lines",
                bbox=[25, 139, 270, 182],
            ),
            Element(kind="heading", page=1, text="2. Next section", bbox=[25, 215, 270, 243]),
        ],
    )
    assign_heading_levels(parsed, {}, source)
    assert [e.level for e in parsed.elements] == [1, 2, 3, 2]
