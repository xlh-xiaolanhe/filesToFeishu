import pytest
from PIL import Image, ImageDraw
from reportlab.pdfgen import canvas

from files_to_feishu.converters.pdf.cleanup import (
    is_page_number,
    list_text,
    page_without_numbers,
    source_list_markers,
)
from files_to_feishu.converters.pdf.parser import from_layout, render_pages
from files_to_feishu.models import Element


def test_positioned_list_marker_is_removed_but_literal_symbol_is_preserved(tmp_path):
    source = tmp_path / "lists.pdf"
    pdf = canvas.Canvas(str(source), pagesize=(300, 400))
    pdf.setFont("Helvetica", 12)
    # Deliberately emit the visual marker last, as in the reported PDF.
    pdf.drawString(45, 300, "A sentence.")
    pdf.drawString(30, 300, "•")
    pdf.drawString(30, 250, "The symbol is")
    pdf.drawString(115, 250, "•")
    pdf.save()
    items = [
        {
            "self_ref": f"#/texts/{i}",
            "label": "list_item",
            "text": text,
            "marker": "",
            "enumerated": False,
            "prov": [{"page_no": 1, "bbox": {"l": 25, "t": top, "r": 180, "b": top + 18}}],
        }
        for i, (text, top) in enumerate([("A sentence. •", 88), ("The symbol is •", 138)])
    ]
    layout = {"texts": items}
    assert source_list_markers(source, layout) == {"#/texts/0": "•"}
    render_pages(source, tmp_path)
    parsed = from_layout(layout, ["A sentence. • The symbol is •"], tmp_path, source=source)
    assert [(e.kind, e.text) for e in parsed.elements] == [
        ("bullet", "A sentence."),
        ("bullet", "The symbol is •"),
    ]


@pytest.mark.parametrize("text", ["Inside ● text", "Final ●", "const symbol = '●';"])
def test_symbol_is_not_removed_without_marker_evidence(text):
    assert list_text({"text": text, "marker": "", "self_ref": "#/texts/0"}, {}) == text


@pytest.mark.parametrize(
    "value", ["1", "- 1 -", "第 1 页", "第1页 / 共48页", "1 / 48", "Page 1 of 48"]
)
def test_page_numbers_are_removed_only_at_page_margins(value):
    assert is_page_number(value, "page_footer", [270, 386, 280, 392], 400, 1)
    assert not is_page_number(value, "text", [20, 100, 80, 112], 400, 1)


@pytest.mark.parametrize(
    "value,label",
    [
        ("2026", "page_footer"),
        ("Copyright 2026", "page_footer"),
        ("Course title", "page_header"),
        ("1️⃣", "text"),
        ("1", "section_header"),
        ("1", "code"),
        ("1", "list_item"),
        ("2", "text"),
    ],
)
def test_body_numbers_headers_and_code_are_preserved(value, label):
    assert not is_page_number(value, label, [270, 386, 280, 392], 400, 1)


def test_fallback_removes_only_page_number_without_modifying_original(tmp_path):
    original = Image.new("RGB", (600, 800), "white")
    draw = ImageDraw.Draw(original)
    draw.rectangle((540, 772, 560, 784), fill="black")
    draw.rectangle((30, 700, 300, 730), fill="blue")
    original.save(tmp_path / "page-1.png")
    name = page_without_numbers(tmp_path, 1, [[270, 386, 280, 392]], (300, 400))
    with Image.open(tmp_path / name) as image:
        assert image.getpixel((550, 778)) == (255, 255, 255)
        assert image.getpixel((50, 710)) == (0, 0, 255)
    with Image.open(tmp_path / "page-1.png") as image:
        assert image.getpixel((550, 778)) == (0, 0, 0)


def test_fallback_page_does_not_reintroduce_omitted_number(tmp_path):
    Image.new("RGB", (600, 800), "white").save(tmp_path / "page-1.png")
    layout = {
        "texts": [
            {
                "self_ref": "#/texts/0",
                "label": "page_footer",
                "text": "1",
                "prov": [{"page_no": 1, "bbox": {"l": 270, "t": 386, "r": 280, "b": 392}}],
            }
        ]
    }
    parsed = from_layout(layout, ["A paragraph the parser missed. 1"], tmp_path)
    assert parsed.elements[0].asset == "content-page-1.png"
    assert parsed.page_images == ["page-1.png"]
    assert any("覆盖不足" in notice.reason for notice in parsed.notices)


def test_number_inside_recovered_code_is_not_treated_as_page_number(tmp_path):
    Image.new("RGB", (600, 800), "white").save(tmp_path / "page-1.png")
    layout = {
        "texts": [
            {
                "self_ref": "#/texts/0",
                "label": "text",
                "text": "1",
                "prov": [{"page_no": 1, "bbox": {"l": 30, "t": 376, "r": 40, "b": 382}}],
            }
        ]
    }
    code = Element(kind="code", page=1, text="1", bbox=[20, 365, 260, 395])
    parsed = from_layout(layout, ["1"], tmp_path, [code])
    assert [e.text for e in parsed.elements] == ["1"]
    assert parsed.elements[0].kind == "code"


def test_page_containing_only_folio_does_not_trigger_coverage_fallback(tmp_path):
    Image.new("RGB", (600, 800), "white").save(tmp_path / "page-1.png")
    layout = {
        "texts": [
            {
                "self_ref": "#/texts/0",
                "label": "page_footer",
                "text": "1",
                "prov": [{"page_no": 1, "bbox": {"l": 270, "t": 386, "r": 280, "b": 392}}],
            }
        ]
    }
    parsed = from_layout(layout, ["1"], tmp_path)
    assert not parsed.elements
    assert not parsed.notices
